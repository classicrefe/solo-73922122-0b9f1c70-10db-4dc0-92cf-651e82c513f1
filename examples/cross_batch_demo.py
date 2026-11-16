#!/usr/bin/env python3
"""跨批次引用 + 双实例并发演示脚本。

前提：docker compose up（或本地启动两个实例），默认访问
  app1 = http://localhost:8001, app2 = http://localhost:8002

演示内容：
  1. 在 app1 创建研究项目；
  2. 批次1（2 个实例）上传到 app1 → 发布；
  3. 同一文件原样再传到 app2 → 发布且复用（实例数不变）；
  4. 同 SOPInstanceUID 但内容改动过的包传到 app2 → 拒收（同实例异文）；
  5. 批次2（引用批次1 的实例）传到 app2 → 跨批次、跨实例引用解析成功；
  6. 引用未知 UID 的批次传到 app1 → 整包拒收并显示原因；
  7. 从 app1 下载批次2 的处理包，校验引用已被替换为映射后的 UID。
"""
from __future__ import annotations

import argparse
import io
import sys
import time
import uuid
import zipfile

import httpx
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

CT_IMAGE_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
_counter = [0]


def gen_uid() -> str:
    _counter[0] += 1
    return f"1.2.826.0.1.3680043.9.7433.{int(time.time())}.{_counter[0]}"


def mapped_uid(project_id: str, original_uid: str) -> str:
    """与服务端一致的确定性 UID 映射（2.25 UUID 根）。"""
    return "2.25." + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"deid:{project_id}:{original_uid}").int)


def make_dicom(*, patient_name="Zhang^San", sop_uid=None, refs=()) -> bytes:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE_STORAGE
    meta.MediaStorageSOPInstanceUID = sop_uid or gen_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = CT_IMAGE_STORAGE
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = gen_uid()
    ds.SeriesInstanceUID = gen_uid()
    ds.PatientID = "PAT-001"
    ds.PatientName = patient_name
    ds.PatientBirthDate = "19700101"
    ds.Modality = "CT"
    ds.BurnedInAnnotation = "NO"
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    if refs:
        ds.ReferencedImageSequence = []
        for r in refs:
            item = Dataset()
            item.ReferencedSOPClassUID = CT_IMAGE_STORAGE
            item.ReferencedSOPInstanceUID = r
            ds.ReferencedImageSequence.append(item)
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


def make_zip(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)
    return buf.getvalue()


def upload(client: httpx.Client, base: str, project_id: str,
           zip_bytes: bytes, filename: str) -> str:
    r = client.post(f"{base}/api/projects/{project_id}/packages",
                    files={"file": (filename, zip_bytes, "application/zip")})
    r.raise_for_status()
    return r.json()["package_id"]


def wait_done(client: httpx.Client, base: str, package_id: str) -> dict:
    for _ in range(120):
        row = client.get(f"{base}/api/packages/{package_id}").json()
        if row["status"] not in ("received", "processing"):
            return row
        time.sleep(0.5)
    raise TimeoutError(f"包 {package_id} 处理超时")


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app1", default="http://localhost:8001")
    ap.add_argument("--app2", default="http://localhost:8002")
    args = ap.parse_args()

    results: list[bool] = []
    with httpx.Client(timeout=30) as client:
        pid = client.post(f"{args.app1}/api/projects",
                          json={"name": "跨批引用演示"}).json()["project_id"]
        print(f"项目 {pid} 已创建（经由 app1）")

        # 1) 批次1 → app1
        sop_a, sop_b = gen_uid(), gen_uid()
        batch1 = make_zip([("a.dcm", make_dicom(sop_uid=sop_a)),
                           ("b.dcm", make_dicom(sop_uid=sop_b))])
        r1 = wait_done(client, args.app1, upload(client, args.app1, pid, batch1, "batch1.zip"))
        results.append(check("批次1 经 app1 发布", r1["status"] == "published", r1.get("reason") or ""))

        # 2) 同内容再传 app2 → 复用
        r2 = wait_done(client, args.app2, upload(client, args.app2, pid, batch1, "batch1-again.zip"))
        n = client.get(f"{args.app1}/api/projects/{pid}").json()["instance_count"]
        results.append(check("同实例同原文经 app2 复用",
                             r2["status"] == "published" and n == 2,
                             f"已发布实例数={n}（应为2）"))

        # 3) 同 SOP UID 异文 → 拒收
        tampered = make_zip([("a2.dcm", make_dicom(patient_name="Li^Si", sop_uid=sop_a))])
        r3 = wait_done(client, args.app2, upload(client, args.app2, pid, tampered, "tampered.zip"))
        results.append(check("同实例异文被拒收",
                             r3["status"] == "rejected" and "同实例异文" in (r3["reason"] or ""),
                             r3.get("reason") or ""))

        # 4) 批次2 引用批次1（跨批次、跨实例）→ 发布
        batch2 = make_zip([("c.dcm", make_dicom(refs=[sop_a]))])
        r4 = wait_done(client, args.app2, upload(client, args.app2, pid, batch2, "batch2.zip"))
        results.append(check("批次2 跨批引用经 app2 发布",
                             r4["status"] == "published", r4.get("reason") or ""))

        # 5) 引用未知 UID → 拒收并显示原因
        ghost = gen_uid()
        bad = make_zip([("d.dcm", make_dicom(refs=[ghost]))])
        r5 = wait_done(client, args.app1, upload(client, args.app1, pid, bad, "bad.zip"))
        results.append(check("引用未知 UID 整包拒收",
                             r5["status"] == "rejected" and ghost in (r5["reason"] or ""),
                             r5.get("reason") or ""))

        # 6) 从 app1 下载批次2 并校验引用已替换
        dl = client.get(f"{args.app1}/packages/{r4['package_id']}/download")
        ok = False
        detail = ""
        if dl.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
                ds = pydicom.dcmread(io.BytesIO(zf.read(zf.namelist()[0])))
            got = ds.ReferencedImageSequence[0].ReferencedSOPInstanceUID
            want = mapped_uid(pid, sop_a)
            ok = got == want
            detail = f"引用={got}"
        results.append(check("从 app1 下载批次2且引用已映射", ok, detail))

    print("\n结果：" + ("全部通过" if all(results) else "存在失败项"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
