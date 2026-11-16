"""共享卷目录 + SQLite 目录库。

设计要点：
- 原件、处理包存共享卷（originals/ packages/ staging/），SQLite 只存目录元数据。
- WAL 模式 + busy_timeout：转换期间其他上传/下载（读）不被阻塞。
- 发布用 BEGIN IMMEDIATE 事务：双服务实例并发上传时串行化，
  同事务内完成"冲突检查 + 实例登记 + 包状态更新"，杜绝未发布实例被引用。
- 启动恢复：中断的包标记失败，清理 staging 与未发布的包文件，不留可下载半包。
"""
from __future__ import annotations

import os
import socket
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    status TEXT NOT NULL,              -- received/processing/published/rejected/failed
    reason TEXT,
    original_name TEXT,
    original_path TEXT,
    package_path TEXT,
    instance_count INTEGER NOT NULL DEFAULT 0,
    owner TEXT,                        -- 接收该包的服务实例标识
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
    project_id TEXT NOT NULL,
    sop_uid TEXT NOT NULL,             -- 原始 SOPInstanceUID
    sha256 TEXT NOT NULL,              -- 原始文件字节哈希（同实例判重用）
    study_uid TEXT NOT NULL,           -- 原始 StudyInstanceUID
    series_uid TEXT NOT NULL,          -- 原始 SeriesInstanceUID
    mapped_sop_uid TEXT NOT NULL,      -- 替换后的 SOPInstanceUID
    package_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (project_id, sop_uid)
);
CREATE INDEX IF NOT EXISTS idx_packages_project ON packages(project_id);
"""

STATUS_LABELS = {
    "received": "已接收",
    "processing": "处理中",
    "published": "已发布",
    "rejected": "已拒收",
    "failed": "失败",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 启动恢复判定“死亡”在途包的时间阈值：处理 1~20 份单帧实例只需秒级，
# updated_at 超过该阈值的在途包必然属于已死实例。
STALE_PACKAGE_SECONDS = 600
# 清理无主文件的安全宽限：另一存活实例可能刚写入/改名，只清理更老的文件。
ORPHAN_FILE_GRACE_SECONDS = 60


class Store:
    """目录库与共享卷路径。所有方法均可跨线程、跨进程安全调用。"""

    def __init__(self, data_dir: str | os.PathLike, owner: str | None = None):
        self.data_dir = Path(data_dir)
        self.originals_dir = self.data_dir / "originals"
        self.packages_dir = self.data_dir / "packages"
        self.staging_dir = self.data_dir / "staging"
        self.db_path = self.data_dir / "catalog.db"
        self.owner = owner or socket.gethostname()

    # ---------------- 初始化与恢复 ----------------
    def init(self) -> None:
        for d in (self.originals_dir, self.packages_dir, self.staging_dir):
            d.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            with conn:
                conn.executescript(SCHEMA)
                conn.execute("PRAGMA journal_mode=WAL")
        self.recover()

    def recover(self) -> None:
        """启动恢复（不干扰另一存活实例）：

        - 本实例遗留的、以及超时停滞的在途包标记为失败；
        - staging/ 中超过宽限期的文件一律清理（宽限期内的可能正被另一实例写入）；
        - 清理无发布记录且超过宽限期的 packages/ 孤儿文件（可能来自
          “已改名未提交”的崩溃窗口）；宽限期内的文件属于另一实例的在途发布。
        """
        now = _now()
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=STALE_PACKAGE_SECONDS)
        ).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "UPDATE packages SET status='failed', reason=?, updated_at=? "
                    "WHERE status IN ('received','processing') AND owner=?",
                    ("服务重启导致处理中断，请重新上传", now, self.owner),
                )
                conn.execute(
                    "UPDATE packages SET status='failed', reason=?, updated_at=? "
                    "WHERE status IN ('received','processing') AND updated_at < ?",
                    ("处理超时停滞（实例可能已宕机），请重新上传", now, stale_before),
                )
                rows = conn.execute(
                    "SELECT package_id FROM packages WHERE status='published'"
                ).fetchall()
        published = {row["package_id"] for row in rows}
        cutoff = time.time() - ORPHAN_FILE_GRACE_SECONDS
        # staging 是暂存区：只清理超过宽限期的文件，宽限期内的可能正被另一实例写入
        for f in self.staging_dir.glob("*.zip"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        for f in self.packages_dir.glob("*.zip"):
            if f.stem not in published and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ---------------- 项目 ----------------
    def create_project(self, name: str) -> str:
        project_id = "P" + uuid.uuid4().hex[:10].upper()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO projects (project_id, name, created_at) VALUES (?,?,?)",
                    (project_id, name, _now()),
                )
        return project_id

    def get_project(self, project_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT p.*,
                    (SELECT COUNT(*) FROM packages k
                      WHERE k.project_id = p.project_id) AS package_count,
                    (SELECT COUNT(*) FROM instances i
                      WHERE i.project_id = p.project_id) AS instance_count
                FROM projects p ORDER BY p.created_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 包 ----------------
    def create_package(self, project_id: str, original_name: str) -> str:
        package_id = uuid.uuid4().hex[:16]
        now = _now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO packages (package_id, project_id, status, reason,"
                    " original_name, original_path, package_path, instance_count,"
                    " owner, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        package_id, project_id, "received", None, original_name,
                        f"originals/{package_id}.zip", None, 0, self.owner, now, now,
                    ),
                )
        return package_id

    def get_package(self, package_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM packages WHERE package_id=?", (package_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_packages(self, project_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM packages WHERE project_id=? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_status(
        self,
        package_id: str,
        status: str,
        reason: str | None = None,
        package_path: str | None = None,
        instance_count: int | None = None,
    ) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "UPDATE packages SET status=?, reason=?,"
                    " package_path=COALESCE(?, package_path),"
                    " instance_count=COALESCE(?, instance_count), updated_at=?"
                    " WHERE package_id=?",
                    (status, reason, package_path, instance_count, _now(), package_id),
                )

    # ---------------- 实例目录 ----------------
    def published_uids(self, project_id: str) -> set[str]:
        """本项目已发布实例的全部原始 UID（sop/study/series），用于引用校验。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT sop_uid, study_uid, series_uid FROM instances WHERE project_id=?",
                (project_id,),
            ).fetchall()
        uids: set[str] = set()
        for r in rows:
            uids.update((r["sop_uid"], r["study_uid"], r["series_uid"]))
        return uids

    def instance_count(self, project_id: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM instances WHERE project_id=?", (project_id,)
            ).fetchone()
        return int(row["n"])

    def publish_package(
        self, package_id: str, project_id: str, instances: list[dict], package_path: str
    ) -> tuple[bool, str | None]:
        """事务发布：冲突检查 + 实例登记 + 包状态更新，三步原子完成。

        返回 (是否成功, 冲突的原始SOPInstanceUID)。同实例同原文复用（跳过登记），
        同实例异文则整个包发布失败。
        """
        now = _now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for inst in instances:
                row = conn.execute(
                    "SELECT sha256 FROM instances WHERE project_id=? AND sop_uid=?",
                    (project_id, inst["sop_uid"]),
                ).fetchone()
                if row and row["sha256"] != inst["sha256"]:
                    conn.rollback()
                    return False, inst["sop_uid"]
            for inst in instances:
                conn.execute(
                    "INSERT OR IGNORE INTO instances (project_id, sop_uid, sha256,"
                    " study_uid, series_uid, mapped_sop_uid, package_id, published_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        project_id, inst["sop_uid"], inst["sha256"], inst["study_uid"],
                        inst["series_uid"], inst["mapped_sop_uid"], package_id, now,
                    ),
                )
            conn.execute(
                "UPDATE packages SET status='published', reason=NULL, package_path=?,"
                " instance_count=?, updated_at=? WHERE package_id=?",
                (package_path, len(instances), now, package_id),
            )
            conn.commit()
            return True, None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
