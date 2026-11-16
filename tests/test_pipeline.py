"""关键功能自测：去标识化、引用同步、拒收、并发、恢复、API 流程。"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydicom.uid import ExplicitVRBigEndian, ImplicitVRLittleEndian

from app.deidentify import mapped_patient_id, mapped_uid
from app.pipeline import process_package
from app.store import Store
from tests.helpers import (
    CT_IMAGE_STORAGE,
    PIXEL_BYTES,
    gen_uid,
    make_dicom_bytes,
    make_zip,
    read_dicom,
    unzip,
)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "data", owner="test-instance")
    s.init()
    return s


def submit(store, project_id, zip_bytes, name="batch.zip"):
    """模拟上传并同步处理，返回 package_id。"""
    package_id = store.create_package(project_id, name)
    (store.originals_dir / f"{package_id}.zip").write_bytes(zip_bytes)
    process_package(store, package_id)
    return package_id


def published_files(store, package_id):
    return unzip((store.packages_dir / f"{package_id}.zip").read_bytes())


# ---------------- 去标识化正确性 ----------------

def test_deidentify_fields_and_pixel_integrity(store):
    pid = store.create_project("肺癌队列")
    sop1, sop2 = gen_uid(), gen_uid()
    study, series = gen_uid(), gen_uid()
    zip_bytes = make_zip([
        ("a.dcm", make_dicom_bytes(study_uid=study, series_uid=series, sop_uid=sop1)),
        ("b.dcm", make_dicom_bytes(study_uid=study, series_uid=series, sop_uid=sop2,
                                   refs=[sop1])),
    ])
    pkg = submit(store, pid, zip_bytes)
    pkg_row = store.get_package(pkg)
    assert pkg_row["status"] == "published", pkg_row["reason"]
    assert pkg_row["instance_count"] == 2

    files = published_files(store, pkg)
    assert set(files) == {f"{mapped_uid(pid, sop1)}.dcm", f"{mapped_uid(pid, sop2)}.dcm"}

    ds1 = read_dicom(files[f"{mapped_uid(pid, sop1)}.dcm"])
    # 身份字段与私有标签被删除
    assert "PatientName" not in ds1
    assert "PatientBirthDate" not in ds1
    assert ds1.get((0x0011, 0x1001)) is None
    assert ds1.get((0x0009, 0x0010)) is None
    # PatientID 与 UID 按项目稳定替换
    assert ds1.PatientID == mapped_patient_id(pid, "PAT-001")
    assert ds1.SOPInstanceUID == mapped_uid(pid, sop1)
    assert ds1.StudyInstanceUID == mapped_uid(pid, study)
    assert ds1.SeriesInstanceUID == mapped_uid(pid, series)
    assert ds1.file_meta.MediaStorageSOPInstanceUID == mapped_uid(pid, sop1)
    # SOP Class、传输语法、像素字节不变
    assert ds1.SOPClassUID == CT_IMAGE_STORAGE
    assert str(ds1.file_meta.TransferSyntaxUID) == "1.2.840.10008.1.2.1"
    assert ds1.PixelData == PIXEL_BYTES

    # 包内引用被递归同步到映射后的 UID
    ds2 = read_dicom(files[f"{mapped_uid(pid, sop2)}.dcm"])
    ref = ds2.ReferencedImageSequence[0]
    assert ref.ReferencedSOPInstanceUID == mapped_uid(pid, sop1)
    assert ref.ReferencedSOPClassUID == CT_IMAGE_STORAGE  # Class 不变
    assert ref.get((0x0011, 0x1002)) is None  # 序列内私有标签也被删除


def test_transfer_syntaxes_preserved(store):
    for ts, name in [(ImplicitVRLittleEndian, "implicit"), (ExplicitVRBigEndian, "bigendian")]:
        pid = store.create_project(f"ts-{name}")
        zip_bytes = make_zip([("x.dcm", make_dicom_bytes(transfer_syntax=ts))])
        pkg = submit(store, pid, zip_bytes)
        assert store.get_package(pkg)["status"] == "published"
        ds = read_dicom(next(iter(published_files(store, pkg).values())))
        assert str(ds.file_meta.TransferSyntaxUID) == str(ts)
        assert ds.PixelData == PIXEL_BYTES


def test_patient_id_mapping_stable_across_batches(store):
    pid = store.create_project("稳定性")
    pkg1 = submit(store, pid, make_zip([("a.dcm", make_dicom_bytes())]))
    pkg2 = submit(store, pid, make_zip([("b.dcm", make_dicom_bytes())]))
    ds1 = read_dicom(next(iter(published_files(store, pkg1).values())))
    ds2 = read_dicom(next(iter(published_files(store, pkg2).values())))
    assert ds1.PatientID == ds2.PatientID == mapped_patient_id(pid, "PAT-001")


# ---------------- 跨批引用与拒收 ----------------

def test_cross_batch_reference(store):
    pid = store.create_project("纵向研究")
    sop_a = gen_uid()
    pkg1 = submit(store, pid, make_zip([("a.dcm", make_dicom_bytes(sop_uid=sop_a))]))
    assert store.get_package(pkg1)["status"] == "published"

    # 第二批引用第一批已发布实例
    pkg2 = submit(store, pid, make_zip([("b.dcm", make_dicom_bytes(refs=[sop_a]))]))
    row2 = store.get_package(pkg2)
    assert row2["status"] == "published", row2["reason"]
    ds_b = read_dicom(next(iter(published_files(store, pkg2).values())))
    assert ds_b.ReferencedImageSequence[0].ReferencedSOPInstanceUID == mapped_uid(pid, sop_a)


def test_unknown_reference_rejected_and_no_residue(store):
    pid = store.create_project("拒收")
    ghost = gen_uid()  # 不存在于包内，也未发布
    pkg = submit(store, pid, make_zip([("x.dcm", make_dicom_bytes(refs=[ghost]))]))
    row = store.get_package(pkg)
    assert row["status"] == "rejected"
    assert ghost in row["reason"]
    # 不留可下载半包、不留可供引用的未发布实例
    assert not (store.packages_dir / f"{pkg}.zip").exists()
    assert store.instance_count(pid) == 0
    assert list(store.staging_dir.glob("*.zip")) == []


def test_empty_reference_tolerated(store):
    """type 2 空引用不应误触发拒收。"""
    pid = store.create_project("空引用")
    pkg = submit(store, pid, make_zip([("x.dcm", make_dicom_bytes(refs=[""]))]))
    row = store.get_package(pkg)
    assert row["status"] == "published", row["reason"]


def test_reference_to_other_project_rejected(store):
    pid1 = store.create_project("项目一")
    pid2 = store.create_project("项目二")
    sop_a = gen_uid()
    submit(store, pid1, make_zip([("a.dcm", make_dicom_bytes(sop_uid=sop_a))]))
    # 项目二的包引用项目一已发布实例 → 仍应拒收（只允许本项目）
    pkg = submit(store, pid2, make_zip([("b.dcm", make_dicom_bytes(refs=[sop_a]))]))
    row = store.get_package(pkg)
    assert row["status"] == "rejected"
    assert sop_a in row["reason"]


# ---------------- 输入校验 ----------------

def test_multiframe_rejected(store):
    pid = store.create_project("多帧")
    pkg = submit(store, pid, make_zip([("m.dcm", make_dicom_bytes(number_of_frames=5))]))
    row = store.get_package(pkg)
    assert row["status"] == "rejected" and "NumberOfFrames" in row["reason"]


def test_compressed_rejected(store):
    pid = store.create_project("压缩")
    jpeg = "1.2.840.10008.1.2.4.50"  # JPEG Baseline
    pkg = submit(store, pid, make_zip([("c.dcm", make_dicom_bytes(transfer_syntax=jpeg))]))
    row = store.get_package(pkg)
    assert row["status"] == "rejected" and "传输语法" in row["reason"]


def test_burned_in_rejected(store):
    pid = store.create_project("烧录")
    pkg = submit(store, pid, make_zip([("b.dcm", make_dicom_bytes(burned_in="YES"))]))
    row = store.get_package(pkg)
    assert row["status"] == "rejected" and "烧录" in row["reason"]


def test_package_size_limits(store):
    pid = store.create_project("数量")
    pkg0 = submit(store, pid, make_zip([]))
    assert store.get_package(pkg0)["status"] == "rejected"
    many = make_zip([(f"f{i}.dcm", make_dicom_bytes()) for i in range(21)])
    pkg21 = submit(store, pid, many)
    assert store.get_package(pkg21)["status"] == "rejected"
    ok = make_zip([(f"f{i}.dcm", make_dicom_bytes()) for i in range(20)])
    pkg20 = submit(store, pid, ok)
    assert store.get_package(pkg20)["status"] == "published"


def test_non_dicom_rejected(store):
    pid = store.create_project("非DICOM")
    pkg = submit(store, pid, make_zip([("readme.txt", b"hello")]))
    row = store.get_package(pkg)
    assert row["status"] == "rejected" and "无法解析" in row["reason"]


def test_bad_zip_rejected(store):
    pid = store.create_project("坏zip")
    pkg = submit(store, pid, b"not a zip at all")
    row = store.get_package(pkg)
    assert row["status"] == "rejected" and "ZIP" in row["reason"]


def test_duplicate_sop_uid_in_package_rejected(store):
    pid = store.create_project("重复")
    sop = gen_uid()
    pkg = submit(store, pid, make_zip([
        ("a.dcm", make_dicom_bytes(sop_uid=sop)),
        ("b.dcm", make_dicom_bytes(sop_uid=sop)),
    ]))
    row = store.get_package(pkg)
    assert row["status"] == "rejected" and "重复" in row["reason"]


# ---------------- 并发：同实例同原文复用、异文拒收 ----------------

def test_same_instance_same_content_reused(store):
    pid = store.create_project("复用")
    dcm = make_dicom_bytes()
    pkg1 = submit(store, pid, make_zip([("x.dcm", dcm)]))
    pkg2 = submit(store, pid, make_zip([("x.dcm", dcm)]))
    assert store.get_package(pkg1)["status"] == "published"
    assert store.get_package(pkg2)["status"] == "published"
    assert store.instance_count(pid) == 1  # 复用，不重复登记


def test_same_instance_different_content_rejected(store):
    pid = store.create_project("冲突")
    sop = gen_uid()
    pkg1 = submit(store, pid, make_zip([("a.dcm", make_dicom_bytes(
        sop_uid=sop, patient_name="Zhang^San"))]))
    assert store.get_package(pkg1)["status"] == "published"
    pkg2 = submit(store, pid, make_zip([("b.dcm", make_dicom_bytes(
        sop_uid=sop, patient_name="Li^Si"))]))
    row2 = store.get_package(pkg2)
    assert row2["status"] == "rejected"
    assert "同实例异文" in row2["reason"]
    assert store.instance_count(pid) == 1
    assert not (store.packages_dir / f"{pkg2}.zip").exists()


def test_dual_instance_concurrent_uploads(tmp_path):
    """两个服务实例（两个 Store）共享同一数据目录并发上传。"""
    data = tmp_path / "shared"
    inst_a = Store(data, owner="app1")
    inst_a.init()
    inst_b = Store(data, owner="app2")
    inst_b.init()  # 第二个实例启动恢复不得影响第一个实例
    pid = inst_a.create_project("并发")

    same = make_zip([("s.dcm", make_dicom_bytes())])
    sop_conflict = gen_uid()
    diff1 = make_zip([("d1.dcm", make_dicom_bytes(sop_uid=sop_conflict,
                                                 patient_name="A^A"))])
    diff2 = make_zip([("d2.dcm", make_dicom_bytes(sop_uid=sop_conflict,
                                                 patient_name="B^B"))])

    def run(store, zb):
        return store.get_package(submit(store, pid, zb))["status"]

    with ThreadPoolExecutor(4) as ex:
        # 同实例同原文：两个实例都应发布且复用
        f1 = ex.submit(run, inst_a, same)
        f2 = ex.submit(run, inst_b, same)
        # 同实例异文：恰好一个发布、一个拒收
        f3 = ex.submit(run, inst_a, diff1)
        f4 = ex.submit(run, inst_b, diff2)
        r_same = {f1.result(), f2.result()}
        r_diff = [f3.result(), f4.result()]

    assert r_same == {"published"}
    assert sorted(r_diff) == ["published", "rejected"]
    assert inst_a.instance_count(pid) == 2  # same 复用为 1 个 + 冲突胜出者 1 个


# ---------------- 重启恢复 ----------------

def test_recovery_cleans_interrupted_state(tmp_path):
    data = tmp_path / "data"
    s1 = Store(data, owner="app1")
    s1.init()
    pid = s1.create_project("恢复")
    # 模拟崩溃：一个包停在 processing，staging 有半成品，packages 有未发布孤儿
    pkg = s1.create_package(pid, "crash.zip")
    s1.set_status(pkg, "processing")
    (s1.staging_dir / f"{pkg}.zip").write_bytes(b"half-written")
    (s1.packages_dir / f"{pkg}.zip").write_bytes(b"orphan")
    orphan_ts = time.time() - 3600
    os.utime(s1.staging_dir / f"{pkg}.zip", (orphan_ts, orphan_ts))
    os.utime(s1.packages_dir / f"{pkg}.zip", (orphan_ts, orphan_ts))

    s2 = Store(data, owner="app1")  # 同实例重启
    s2.init()
    row = s2.get_package(pkg)
    assert row["status"] == "failed" and "重启" in row["reason"]
    assert not (s2.staging_dir / f"{pkg}.zip").exists()
    assert not (s2.packages_dir / f"{pkg}.zip").exists()
    assert s2.instance_count(pid) == 0


def test_recovery_preserves_other_instance_inflight(tmp_path):
    data = tmp_path / "data"
    inst_a = Store(data, owner="app1")
    inst_a.init()
    pid = inst_a.create_project("共存")
    pkg = inst_a.create_package(pid, "inflight.zip")  # app1 的在途包
    inst_b = Store(data, owner="app2")
    inst_b.init()  # app2 启动恢复
    assert inst_a.get_package(pkg)["status"] == "received"  # 不受影响


def test_recovery_fails_stale_package_of_dead_instance(tmp_path):
    """另一实例的超时停滞包（实例已死）应被恢复流程标记失败。"""
    data = tmp_path / "data"
    inst_a = Store(data, owner="app1")
    inst_a.init()
    pid = inst_a.create_project("停滞")
    pkg = inst_a.create_package(pid, "stale.zip")
    # 手工把 updated_at 改成 1 小时前，模拟 app1 已死
    import sqlite3
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    with sqlite3.connect(inst_a.db_path) as conn:
        conn.execute("UPDATE packages SET updated_at=? WHERE package_id=?", (old, pkg))
    inst_b = Store(data, owner="app2")
    inst_b.init()
    row = inst_b.get_package(pkg)
    assert row["status"] == "failed" and "停滞" in row["reason"]


# ---------------- API 流程 ----------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEID_DATA_DIR", str(tmp_path / "data"))
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.store import Store as _Store
    app = create_app(_Store(tmp_path / "data", owner="api-test"))
    return TestClient(app)


def test_api_upload_publish_download(client):
    r = client.post("/api/projects", json={"name": "API测试"})
    assert r.status_code == 200
    pid = r.json()["project_id"]

    zip_bytes = make_zip([("a.dcm", make_dicom_bytes())])
    r = client.post(f"/api/projects/{pid}/packages",
                    files={"file": ("batch1.zip", zip_bytes, "application/zip")})
    assert r.status_code == 200
    pkg = r.json()["package_id"]

    for _ in range(100):
        row = client.get(f"/api/packages/{pkg}").json()
        if row["status"] not in ("received", "processing"):
            break
        time.sleep(0.1)
    assert row["status"] == "published", row["reason"]

    r = client.get(f"/packages/{pkg}/download")
    assert r.status_code == 200
    files = unzip(r.content)
    assert len(files) == 1
    ds = read_dicom(next(iter(files.values())))
    assert "PatientName" not in ds


def test_api_rejection_reason_and_download_blocked(client):
    pid = client.post("/api/projects", json={"name": "拒收展示"}).json()["project_id"]
    ghost = gen_uid()
    zip_bytes = make_zip([("x.dcm", make_dicom_bytes(refs=[ghost]))])
    pkg = client.post(f"/api/projects/{pid}/packages",
                      files={"file": ("bad.zip", zip_bytes, "application/zip")}
                      ).json()["package_id"]
    for _ in range(100):
        row = client.get(f"/api/packages/{pkg}").json()
        if row["status"] not in ("received", "processing"):
            break
        time.sleep(0.1)
    assert row["status"] == "rejected"
    assert ghost in row["reason"]
    # 未发布包不能下载
    assert client.get(f"/packages/{pkg}/download").status_code == 409
    # 网页上能看到拒收原因
    page = client.get(f"/projects/{pid}")
    assert page.status_code == 200 and ghost in page.text


def test_concurrent_upload_during_processing(client):
    """转换期间其他上传与下载仍可用。"""
    pid = client.post("/api/projects", json={"name": "并发可用"}).json()["project_id"]
    zips = [make_zip([(f"f{i}.dcm", make_dicom_bytes()) for i in range(20)])
            for _ in range(3)]
    pkgs = [client.post(f"/api/projects/{pid}/packages",
                        files={"file": (f"b{i}.zip", z, "application/zip")}
                        ).json()["package_id"] for i, z in enumerate(zips)]
    # 处理期间项目页与列表可访问
    assert client.get(f"/projects/{pid}").status_code == 200
    assert client.get("/").status_code == 200
    for _ in range(200):
        rows = [client.get(f"/api/packages/{p}").json() for p in pkgs]
        if all(r["status"] == "published" for r in rows):
            break
        time.sleep(0.1)
    assert all(client.get(f"/packages/{p}/download").status_code == 200 for p in pkgs)
