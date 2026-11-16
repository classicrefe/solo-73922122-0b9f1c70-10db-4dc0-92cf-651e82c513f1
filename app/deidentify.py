"""DICOM 去标识化与引用同步核心逻辑。

规则（按需求约定）：
- 删除 PatientName、PatientBirthDate 及全部私有标签（递归到序列条目内）。
- PatientID 按项目稳定替换为伪名；Study/Series/SOP Instance UID 按项目稳定替换。
- 递归同步序列内的实例引用：任何 *InstanceUID 引用必须指向本包内或本项目
  已发布实例，否则整包拒收。
- SOP Class UID、传输语法、像素字节保持不变。
"""
from __future__ import annotations

import hashlib
import io
import uuid

import pydicom
from pydicom.multival import MultiValue

# 仅接受未压缩传输语法
ALLOWED_TRANSFER_SYNTAXES = {
    "1.2.840.10008.1.2": "Implicit VR Little Endian",
    "1.2.840.10008.1.2.1": "Explicit VR Little Endian",
    "1.2.840.10008.1.2.2": "Explicit VR Big Endian",
}

# 递归删除的身份字段
REMOVE_KEYWORDS = {"PatientName", "PatientBirthDate"}

# 每个实例必须具备的 UID 属性
REQUIRED_UID_KEYWORDS = (
    "SOPClassUID",
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
)

MIN_INSTANCES_PER_PACKAGE = 1
MAX_INSTANCES_PER_PACKAGE = 20


class RejectionError(Exception):
    """导致整包拒收的校验/处理错误，消息会展示在网页上。"""


def mapped_uid(project_id: str, original_uid: str) -> str:
    """按项目确定性映射 UID（2.25 UUID 根），跨批次、跨服务实例稳定一致。"""
    return "2.25." + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"deid:{project_id}:{original_uid}").int
    )


def mapped_patient_id(project_id: str, patient_id: str) -> str:
    """按项目确定性生成 PatientID 伪名。"""
    digest = hashlib.sha256(f"deid:{project_id}:{patient_id}".encode()).hexdigest()
    return "PT" + digest[:12].upper()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_and_validate(data: bytes, filename: str):
    """解析并校验单个 DICOM 文件，返回 Dataset；不合格抛 RejectionError。"""
    try:
        ds = pydicom.dcmread(io.BytesIO(data))
    except Exception as exc:  # InvalidDicomError 等
        raise RejectionError(f"{filename}: 无法解析为 DICOM 文件（{exc}）") from exc

    ts = str(getattr(ds.file_meta, "TransferSyntaxUID", "") or "")
    if ts not in ALLOWED_TRANSFER_SYNTAXES:
        raise RejectionError(
            f"{filename}: 传输语法 {ts or '缺失'} 不是未压缩语法"
            "（仅接受 Implicit/Explicit VR LE 与 Explicit VR BE），整包拒收"
        )

    frames = getattr(ds, "NumberOfFrames", None)
    if frames is not None and str(frames) != "1":
        raise RejectionError(
            f"{filename}: NumberOfFrames={frames}，仅接受单帧实例，整包拒收"
        )

    burned_in = str(getattr(ds, "BurnedInAnnotation", "") or "").strip().upper()
    if burned_in == "YES":
        raise RejectionError(
            f"{filename}: BurnedInAnnotation=YES，像素含烧录文字，整包拒收"
        )

    for kw in REQUIRED_UID_KEYWORDS:
        if not str(getattr(ds, kw, "") or "").strip():
            raise RejectionError(f"{filename}: 缺少必需属性 {kw}，整包拒收")
    return ds


def _walk_clean(ds, project_id, uid_map, published_uids, errors, path):
    """递归清理并同步引用。就地修改 ds，错误追加到 errors。

    uid_map: 本包内 原始UID -> 映射UID（study/series/sop 三类）。
    published_uids: 本项目已发布实例的原始 UID 集合（sop/study/series）。
    """
    for elem in list(ds):
        kw = elem.keyword or ""
        tag = elem.tag

        if tag.is_private:
            del ds[tag]
            continue
        if kw in REMOVE_KEYWORDS:
            del ds[tag]
            continue
        if elem.VR == "SQ":
            for i, item in enumerate(elem.value):
                _walk_clean(
                    item, project_id, uid_map, published_uids, errors,
                    f"{path}{kw}[{i + 1}].",
                )
            continue
        if kw == "PatientID":
            if str(elem.value or "").strip():
                elem.value = mapped_patient_id(project_id, str(elem.value))
            continue
        if elem.VR != "UI" or not kw:
            continue
        if "Class" in kw:
            # SOPClassUID / ReferencedSOPClassUID / MediaStorageSOPClassUID 等保持不变
            continue

        values = list(elem.value) if isinstance(elem.value, MultiValue) else [elem.value]
        new_values = []
        changed = False
        for raw in values:
            v = str(raw or "").strip()
            if not v:
                # 空引用（type 2 空值）不参与映射与校验
                new_values.append(raw)
                continue
            if v in uid_map:
                new_values.append(uid_map[v])
                changed = True
            elif kw.endswith("InstanceUID"):
                if v in published_uids:
                    new_values.append(mapped_uid(project_id, v))
                    changed = True
                else:
                    errors.append(
                        f"{path}{kw} 引用的 UID {v} 既不在本包内，"
                        "也不是本项目已发布实例"
                    )
                    new_values.append(v)
            else:
                # FrameOfReferenceUID、TransferSyntaxUID 等保持原样
                new_values.append(v)
        if changed:
            if isinstance(elem.value, MultiValue):
                elem.value = new_values
            else:
                elem.value = new_values[0]


def process_instance(ds, project_id, uid_map, published_uids, filename):
    """对单个实例执行去标识化与引用同步（就地修改）。失败抛 RejectionError。"""
    errors: list[str] = []
    _walk_clean(ds.file_meta, project_id, uid_map, published_uids, errors, "FileMeta.")
    _walk_clean(ds, project_id, uid_map, published_uids, errors, "")
    if errors:
        raise RejectionError(f"{filename}: " + "；".join(errors) + "，整包拒收")


def instance_uids(ds) -> dict:
    """提取实例的三类原始 UID。"""
    return {
        "sop_uid": str(ds.SOPInstanceUID),
        "study_uid": str(ds.StudyInstanceUID),
        "series_uid": str(ds.SeriesInstanceUID),
    }
