"""SQLite 持久层。

元数据（答卷、字段修订、原始回传、操作队列、附件索引、冲突、审计日志）全部入库；
附件二进制内容按 SHA-256 内容寻址落盘到 blob 目录，数据库只存哈希与路径。
所有写操作在 Engine 层用同一把锁串行化，保证操作队列严格 FIFO。
"""
from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA = [
    # 问卷模板（演示系统只有一份）
    """
    CREATE TABLE IF NOT EXISTS survey (
        id         TEXT PRIMARY KEY,
        title      TEXT NOT NULL,
        version    INTEGER NOT NULL,
        definition TEXT NOT NULL   -- JSON: {fields:[...], attachments:[...]}
    )""",
    # 答卷主表
    """
    CREATE TABLE IF NOT EXISTS response (
        id         TEXT PRIMARY KEY,
        survey_id  TEXT NOT NULL,
        status     TEXT NOT NULL,  -- editing/conflict/ready/published/withdrawn
        head_rev   INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    # 每个字段的当前值（服务端头部快照）
    """
    CREATE TABLE IF NOT EXISTS response_field (
        response_id TEXT NOT NULL,
        field_key   TEXT NOT NULL,
        value       TEXT,          -- JSON 文本
        rev         INTEGER NOT NULL,
        origin_device TEXT,
        PRIMARY KEY (response_id, field_key)
    )""",
    # 字段级修订历史（一次合并可能产生多个字段行，共用同一 rev）
    """
    CREATE TABLE IF NOT EXISTS field_revision (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        response_id TEXT NOT NULL,
        field_key   TEXT NOT NULL,
        rev         INTEGER NOT NULL,
        value       TEXT,
        origin_device TEXT,
        reason      TEXT NOT NULL,  -- initial/auto-merge/manual/branch
        changed_at  TEXT NOT NULL
    )""",
    # 每个修订号对应的整卷指纹（3-way 合并的 base 与指纹核验都靠它）
    """
    CREATE TABLE IF NOT EXISTS rev_fingerprint (
        response_id TEXT NOT NULL,
        rev         INTEGER NOT NULL,
        fingerprint TEXT NOT NULL,
        fields_json TEXT NOT NULL,
        PRIMARY KEY (response_id, rev)
    )""",
    # 设备同步位点：设备认为自己基于哪个 rev / 指纹在编辑
    """
    CREATE TABLE IF NOT EXISTS device_cursor (
        response_id      TEXT NOT NULL,
        device_id        TEXT NOT NULL,
        base_rev         INTEGER NOT NULL,
        base_fingerprint TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        PRIMARY KEY (response_id, device_id)
    )""",
    # 原始回传存证：收据只代表这一层可靠保存，与后续合并/发布无关
    """
    CREATE TABLE IF NOT EXISTS raw_submission (
        receipt_id         TEXT PRIMARY KEY,
        client_submission_id TEXT NOT NULL,
        device_id          TEXT,
        response_id        TEXT,
        kind               TEXT NOT NULL,   -- answers/withdraw/attach/delete_attach
        payload            TEXT NOT NULL,
        received_at        TEXT NOT NULL,
        integrity_status   TEXT NOT NULL,   -- valid/duplicate/fingerprint_mismatch/invalid_json
        duplicate_of       TEXT,
        note               TEXT
    )""",
    # 操作队列：按 seq 严格 FIFO 处理，旧任务不能越过新撤回
    """
    CREATE TABLE IF NOT EXISTS op_queue (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_id   TEXT NOT NULL,
        response_id  TEXT NOT NULL,
        device_id    TEXT,
        op_type      TEXT NOT NULL,
        state        TEXT NOT NULL,        -- pending/applied/conflicted/blocked/idempotent
        client_op_id TEXT,
        payload      TEXT NOT NULL,
        processed_at TEXT,
        result       TEXT                  -- JSON：裁决依据/阻断原因
    )""",
    # 附件索引（blob 在磁盘上）
    """
    CREATE TABLE IF NOT EXISTS attachment (
        response_id   TEXT NOT NULL,
        attachment_id TEXT NOT NULL,       -- 设备端生成的稳定 ID
        slot          TEXT NOT NULL,
        file_name     TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        size_bytes    INTEGER NOT NULL,
        version       INTEGER NOT NULL,
        status        TEXT NOT NULL,       -- present/deleted
        origin_device TEXT,
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (response_id, attachment_id)
    )""",
    # 冲突登记：每条都带 basis（自动裁决依据/冲突原因），页面原样展示
    """
    CREATE TABLE IF NOT EXISTS conflict (
        id            TEXT PRIMARY KEY,
        response_id   TEXT NOT NULL,
        conflict_type TEXT NOT NULL,  -- field/fingerprint/attachment_name/attachment_version/attachment_delete
        status        TEXT NOT NULL,  -- open/resolved
        field_key     TEXT,
        attachment_id TEXT,
        basis         TEXT NOT NULL,
        detail        TEXT NOT NULL,  -- JSON：base/left/right、原始收据等
        resolution    TEXT,
        created_at    TEXT NOT NULL,
        resolved_at   TEXT
    )""",
    # 整份分支保留（指纹冲突时选择"保留整份分支"的产物）
    """
    CREATE TABLE IF NOT EXISTS branch (
        id            TEXT PRIMARY KEY,
        response_id   TEXT NOT NULL,
        name          TEXT NOT NULL,
        fields_json   TEXT NOT NULL,
        source_receipt TEXT,
        device_id     TEXT,
        created_at    TEXT NOT NULL
    )""",
    # 审计日志
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        response_id TEXT,
        action      TEXT NOT NULL,
        detail      TEXT NOT NULL,
        receipt_id  TEXT,
        seq         INTEGER,
        at          TEXT NOT NULL
    )""",
    # 幂等表：同一 (答卷, 设备, 客户端操作号) 只生效一次
    """
    CREATE TABLE IF NOT EXISTS idempotency (
        response_id  TEXT NOT NULL,
        device_id    TEXT NOT NULL,
        client_op_id TEXT NOT NULL,
        result       TEXT NOT NULL,
        at           TEXT NOT NULL,
        PRIMARY KEY (response_id, device_id, client_op_id)
    )""",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short_id(prefix: str) -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


class Store:
    """SQLite + blob 磁盘存储。"""

    def __init__(self, db_path: str, blob_dir: str):
        self.blob_dir = blob_dir
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        os.makedirs(blob_dir, exist_ok=True)
        # 演示环境单进程多线程：一把 RLock 串行化所有业务操作
        # autocommit 模式：由代码显式 commit，避免 sqlite3 隐式事务残留锁/残留数据
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.lock:
            for ddl in SCHEMA:
                self.conn.executescript(ddl)
            self.conn.commit()

    # ---------- 基础工具 ----------
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def commit(self) -> None:
        self.conn.commit()

    def audit(self, response_id: str | None, action: str, detail: str,
              receipt_id: str | None = None, seq: int | None = None) -> None:
        self.execute(
            "INSERT INTO audit_log(response_id, action, detail, receipt_id, seq, at)"
            " VALUES (?,?,?,?,?,?)",
            (response_id, action, detail, receipt_id, seq, now_iso()),
        )

    # ---------- blob 内容寻址 ----------
    def write_blob(self, content_base64: str) -> tuple[str, int, str]:
        """存附件内容，返回 (sha256, 字节数, 磁盘路径)。相同内容只落盘一次。"""
        raw = base64.b64decode(content_base64)
        digest = hashlib.sha256(raw).hexdigest()
        path = os.path.join(self.blob_dir, digest)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(raw)
        return digest, len(raw), path

    def blob_path(self, content_hash: str) -> str:
        return os.path.join(self.blob_dir, content_hash)
