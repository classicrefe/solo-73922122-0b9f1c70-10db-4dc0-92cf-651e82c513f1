"""包处理流水线：校验 -> 去标识化 ->  staging 写包 -> 原子改名 -> 事务发布。

保证：任何失败/重启都不会留下可下载的半包，也不会留下可供引用的未发布实例。
- 处理包先写入 staging/，完成后 os.replace 原子改名到 packages/；
- 实例目录与包状态在同一事务内提交，提交前其他实例看不到任何记录；
- 下载只认目录库中 status='published' 的包。
"""
from __future__ import annotations

import io
import logging
import os
import zipfile

import pydicom

from .deidentify import (
    MAX_INSTANCES_PER_PACKAGE,
    MIN_INSTANCES_PER_PACKAGE,
    RejectionError,
    instance_uids,
    mapped_uid,
    process_instance,
    read_and_validate,
    sha256_bytes,
)
from .store import Store

log = logging.getLogger("deid.pipeline")

MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB 上限，防御异常大文件


def _is_candidate(member: zipfile.ZipInfo) -> bool:
    name = member.filename
    if member.is_dir() or name.startswith("__MACOSX/"):
        return False
    base = name.rsplit("/", 1)[-1]
    return bool(base) and not base.startswith(".")


def process_package(store: Store, package_id: str) -> None:
    """处理一个已接收的包（在后台线程中运行）。"""
    pkg = store.get_package(package_id)
    if not pkg or pkg["status"] not in ("received", "processing"):
        return  # 已被恢复流程标记或不存在
    project_id = pkg["project_id"]
    store.set_status(package_id, "processing")

    staging_zip = store.staging_dir / f"{package_id}.zip"
    final_zip = store.packages_dir / f"{package_id}.zip"
    try:
        original_zip = store.originals_dir / f"{package_id}.zip"
        if original_zip.stat().st_size > MAX_UPLOAD_BYTES:
            raise RejectionError("上传文件超过 512MiB 大小限制，整包拒收")

        # 1) 读取并校验全部成员（全部在内存完成，不写任何中间文件）
        entries = []  # (filename, 原始字节, Dataset)
        try:
            zf = zipfile.ZipFile(original_zip)
        except zipfile.BadZipFile as exc:
            raise RejectionError(f"上传文件不是有效的 ZIP（{exc}），整包拒收") from exc
        with zf:
            members = [m for m in zf.infolist() if _is_candidate(m)]
            if not (MIN_INSTANCES_PER_PACKAGE <= len(members) <= MAX_INSTANCES_PER_PACKAGE):
                raise RejectionError(
                    f"包内含 {len(members)} 个文件，要求 "
                    f"{MIN_INSTANCES_PER_PACKAGE}~{MAX_INSTANCES_PER_PACKAGE} 份单帧 DICOM，整包拒收"
                )
            for m in members:
                data = zf.read(m)
                ds = read_and_validate(data, m.filename)
                entries.append((m.filename, data, ds))

        # 2) 包内 SOPInstanceUID 不得重复
        seen: dict[str, str] = {}
        for filename, _data, ds in entries:
            sop = str(ds.SOPInstanceUID)
            if sop in seen:
                raise RejectionError(
                    f"包内 SOPInstanceUID {sop} 重复（{seen[sop]} 与 {filename}），整包拒收"
                )
            seen[sop] = filename

        # 3) 建立 UID 映射与引用校验集合
        uid_map: dict[str, str] = {}
        for _filename, _data, ds in entries:
            for u in instance_uids(ds).values():
                uid_map[u] = mapped_uid(project_id, u)
        published_uids = store.published_uids(project_id)

        # 4) 去标识化 + 递归引用同步（引用缺失在此抛 RejectionError）
        processed = []  # (映射后SOP UID, 处理后字节, 实例登记信息)
        for filename, data, ds in entries:
            original_uids = instance_uids(ds)  # 处理前的原始 UID
            process_instance(ds, project_id, uid_map, published_uids, filename)
            mapped_sop = str(ds.SOPInstanceUID)  # 处理后（映射后）的 SOPInstanceUID
            buf = io.BytesIO()
            pydicom.dcmwrite(buf, ds, enforce_file_format=True)
            processed.append((mapped_sop, buf.getvalue(), {
                "sop_uid": original_uids["sop_uid"],
                "study_uid": original_uids["study_uid"],
                "series_uid": original_uids["series_uid"],
                "sha256": sha256_bytes(data),
                "mapped_sop_uid": mapped_sop,
            }))

        # 5) 写处理包到 staging，原子改名到 packages/
        with zipfile.ZipFile(staging_zip, "w", zipfile.ZIP_DEFLATED) as out:
            for mapped_sop, blob, _info in processed:
                out.writestr(f"{mapped_sop}.dcm", blob)
        os.replace(staging_zip, final_zip)

        # 6) 事务发布（冲突检查 + 实例登记 + 状态更新原子完成）
        ok, conflict_uid = store.publish_package(
            package_id, project_id, [info for _m, _b, info in processed],
            f"packages/{package_id}.zip",
        )
        if not ok:
            final_zip.unlink(missing_ok=True)
            store.set_status(
                package_id, "rejected",
                f"SOPInstanceUID {conflict_uid} 与本项目已发布实例同号但原文不同"
                "（同实例异文），整包拒收",
            )
            log.warning("package %s rejected: conflict on %s", package_id, conflict_uid)
            return
        log.info("package %s published (%d instances)", package_id, len(processed))

    except RejectionError as exc:
        final_zip.unlink(missing_ok=True)
        store.set_status(package_id, "rejected", str(exc))
        log.warning("package %s rejected: %s", package_id, exc)
    except Exception as exc:  # 内部错误：失败也不留半包
        log.exception("package %s failed", package_id)
        final_zip.unlink(missing_ok=True)
        store.set_status(package_id, "failed", f"内部错误：{exc}")
    finally:
        staging_zip.unlink(missing_ok=True)
