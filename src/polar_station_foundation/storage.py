"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coldchain_plans (
    plan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, batch_id)
);
CREATE TABLE IF NOT EXISTS coldchain_readings (
    reading_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL,
    logger_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    raw_temperature REAL NOT NULL,
    corrected_temperature REAL NOT NULL,
    drift INTEGER NOT NULL CHECK(drift IN (0, 1)),
    ingested_at TEXT NOT NULL,
    reading_hash TEXT NOT NULL,
    UNIQUE(site_id, batch_id, logger_id, observed_at)
);
CREATE TABLE IF NOT EXISTS coldchain_assessments (
    assessment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, batch_id, version)
);
CREATE TABLE IF NOT EXISTS coldchain_decisions (
    decision_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    assessment_id TEXT NOT NULL REFERENCES coldchain_assessments(assessment_id),
    research_actor TEXT,
    research_outcome TEXT,
    research_rationale TEXT,
    research_at TEXT,
    quality_actor TEXT,
    quality_outcome TEXT,
    quality_rationale TEXT,
    quality_at TEXT,
    outcome TEXT CHECK(outcome IN ('continue_use', 'restrict_use', 'destroy')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'effective', 'superseded', 'disagreed', 'withdrawn')),
    withdraw_reason TEXT,
    withdrawn_by TEXT,
    withdrawn_at TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    UNIQUE(site_id, batch_id, version)
);
CREATE TABLE IF NOT EXISTS coldchain_effective_decisions (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES coldchain_decisions(decision_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(site_id, batch_id)
);
CREATE TABLE IF NOT EXISTS coldchain_reports (
    report_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES coldchain_decisions(decision_id),
    title TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coldchain_obligations (
    obligation_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES coldchain_decisions(decision_id),
    kind TEXT NOT NULL CHECK(kind IN ('isolate', 'notify')),
    detail TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'done')),
    created_at TEXT NOT NULL,
    completed_by TEXT,
    completed_at TEXT
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
