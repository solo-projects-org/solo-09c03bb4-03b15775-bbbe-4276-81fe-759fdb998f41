"""SQLite 持久化层与进程内串行锁。

设计要点：
- WAL 模式保证读写并发；写事务一律 BEGIN IMMEDIATE。
- 进程内 ``write_lock``（可重入锁）把"修订受理"与"重算落库"串行化，
  保证上传与计算交错时，后台线程不会把已被取代的修订结论发布为现行结论。
- 每个线程/每次事务使用独立连接，避免跨线程共享连接。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from . import config
from .reconcile import reconcile

# 全局写锁：上传受理与分析重算均需持有
write_lock = threading.RLock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(
        str(config.DB_PATH),
        timeout=30.0,
        isolation_level=None,  # 手动事务
        check_same_thread=True,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


@contextmanager
def immediate_transaction() -> Iterator[sqlite3.Connection]:
    """开启 IMMEDIATE 事务，提交/回滚由上下文管理。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read_session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS revisions (
        revision       INTEGER PRIMARY KEY,
        content_sha256 TEXT NOT NULL,
        status         TEXT NOT NULL CHECK (status IN ('RECEIVED','CURRENT','SUPERSEDED')),
        n_records      INTEGER NOT NULL,
        uploaded_at    TEXT NOT NULL,
        superseded_at  TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS records (
        revision   INTEGER NOT NULL,
        subject_id TEXT NOT NULL,
        day        INTEGER NOT NULL CHECK (day BETWEEN 1 AND 30),
        outcome    TEXT NOT NULL CHECK (outcome IN ('READMISSION','DEATH','CENSORED')),
        PRIMARY KEY (revision, subject_id),
        FOREIGN KEY (revision) REFERENCES revisions(revision) ON DELETE CASCADE
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS analyses (
        revision               INTEGER PRIMARY KEY,
        status                 TEXT NOT NULL CHECK (status IN
                                 ('PENDING','COMPLETED','FAILED','SUPERSEDED')),
        n_subjects             INTEGER,
        n_readmission          INTEGER,
        n_death                INTEGER,
        n_censored             INTEGER,
        cumulative_readmission REAL,
        cumulative_death       REAL,
        last_activity_day      INTEGER,
        error                  TEXT,
        started_at             TEXT,
        finished_at            TEXT,
        FOREIGN KEY (revision) REFERENCES revisions(revision) ON DELETE CASCADE
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_days (
        revision            INTEGER NOT NULL,
        day                 INTEGER NOT NULL,
        at_risk             INTEGER NOT NULL,
        readmission_count   INTEGER NOT NULL,
        death_count         INTEGER NOT NULL,
        censored_count      INTEGER NOT NULL,
        readmission_cif     REAL NOT NULL,
        death_cif           REAL NOT NULL,
        survival            REAL NOT NULL,
        PRIMARY KEY (revision, day),
        FOREIGN KEY (revision) REFERENCES revisions(revision) ON DELETE CASCADE
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_records_revision ON records(revision);",
    "CREATE INDEX IF NOT EXISTS idx_days_revision ON analysis_days(revision);",
    # 事件证据批次：针对某一队列修订上传的完整事件 CSV
    """
    CREATE TABLE IF NOT EXISTS evidence_batches (
        batch_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        revision       INTEGER NOT NULL,
        content_sha256 TEXT NOT NULL,
        n_events       INTEGER NOT NULL,
        status         TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED')),
        uploaded_at    TEXT NOT NULL,
        superseded_at  TEXT,
        FOREIGN KEY (revision) REFERENCES revisions(revision) ON DELETE CASCADE
    );
    """,
    # 证据事件明细：同一患者可有多条
    """
    CREATE TABLE IF NOT EXISTS evidence_events (
        batch_id   INTEGER NOT NULL,
        subject_id TEXT NOT NULL,
        day        INTEGER NOT NULL CHECK (day BETWEEN 1 AND 30),
        event_type TEXT NOT NULL CHECK (event_type IN ('READMISSION','DEATH')),
        FOREIGN KEY (batch_id) REFERENCES evidence_batches(batch_id) ON DELETE CASCADE
    );
    """,
    # 对账结论：每批次在受理事务内同步对账并落库，一份批次一份结论
    """
    CREATE TABLE IF NOT EXISTS reconciliations (
        batch_id    INTEGER PRIMARY KEY,
        revision    INTEGER NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('PASSED','FAILED')),
        n_subjects  INTEGER NOT NULL,
        n_issues    INTEGER NOT NULL,
        computed_at TEXT NOT NULL,
        FOREIGN KEY (batch_id) REFERENCES evidence_batches(batch_id) ON DELETE CASCADE
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS reconciliation_issues (
        batch_id       INTEGER NOT NULL,
        subject_id     TEXT NOT NULL,
        issue_type     TEXT NOT NULL CHECK (issue_type IN
                         ('AMBIGUOUS_FIRST_DAY','EVENT_NOT_RECORDED',
                          'MISSING_EVIDENCE','UNKNOWN_SUBJECT')),
        message        TEXT NOT NULL,
        cohort_day     INTEGER,
        cohort_outcome TEXT,
        evidence_day   INTEGER,
        evidence_event TEXT,
        FOREIGN KEY (batch_id) REFERENCES evidence_batches(batch_id) ON DELETE CASCADE
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_evidence_batches_revision "
    "ON evidence_batches(revision);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_events_batch ON evidence_events(batch_id);",
    "CREATE INDEX IF NOT EXISTS idx_recon_issues_batch ON reconciliation_issues(batch_id);",
]


def init_db() -> None:
    config.ensure_dirs()
    with write_lock, read_session() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)


# ---------------------------------------------------------------------------
# 修订受理
# ---------------------------------------------------------------------------

def get_revision(conn: sqlite3.Connection, revision: int) -> Optional[sqlite3.Row]:
    row = conn.execute(
        "SELECT * FROM revisions WHERE revision = ?;", (revision,)
    ).fetchone()
    return row


def current_revision_no(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT revision FROM revisions WHERE status = 'CURRENT';"
    ).fetchone()
    return row[0] if row else None


def max_revision_no(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT MAX(revision) AS m FROM revisions;").fetchone()
    return row["m"] if row and row["m"] is not None else None


def accept_revision(
    revision: int,
    content_sha256: str,
    rows: list[tuple[str, int, str]],
) -> dict:
    """在单一 IMMEDIATE 事务内受理新修订。

    - 同版同内容：幂等，不做任何写入。
    - 同版异内容：抛出 :class:`RevisionConflict`。
    - 新版号必须等于当前最大版号 + 1。
    - 受理同时把旧 CURRENT 置为 SUPERSEDED（其未完成的分析标记 SUPERSEDED），
      新修订写入 PENDING 分析行，提交后任何查询都不可能再返回旧结论。
    """
    now = _utcnow()
    with write_lock, immediate_transaction() as conn:
        existing = get_revision(conn, revision)
        if existing is not None:
            if existing["content_sha256"] == content_sha256:
                analysis = conn.execute(
                    "SELECT status FROM analyses WHERE revision = ?;", (revision,)
                ).fetchone()
                return {
                    "accepted": False,
                    "idempotent": True,
                    "revision_status": existing["status"],
                    "analysis_status": analysis["status"] if analysis else "PENDING",
                    "n_records": existing["n_records"],
                    "current_revision": current_revision_no(conn),
                }
            raise RevisionConflict(
                f"修订版本 {revision} 已存在且内容不同（SHA-256 不一致），同版异内容拒收。",
                current_revision=current_revision_no(conn),
            )

        max_no = max_revision_no(conn)
        expected = 1 if max_no is None else max_no + 1
        if revision != expected:
            raise RevisionConflict(
                f"修订版本必须递增：下一个可受理版本为 {expected}，收到 {revision}。",
                current_revision=current_revision_no(conn),
            )

        prev_current = current_revision_no(conn)
        if prev_current is not None:
            conn.execute(
                "UPDATE revisions SET status = 'SUPERSEDED', superseded_at = ? "
                "WHERE revision = ?;",
                (now, prev_current),
            )
            # 旧修订尚未完成的分析立即作废，避免后台线程发布旧结论
            conn.execute(
                "UPDATE analyses SET status = 'SUPERSEDED', finished_at = ?, "
                "error = '修订已被新版本取代' WHERE revision = ? AND status = 'PENDING';",
                (now, prev_current),
            )
            # 受理新版队列：旧修订的有效证据批次立即撤销，旧核对资格同时失效；
            # 新修订必须重新上传证据并对账，绝不借用旧版证据或结论
            conn.execute(
                "UPDATE evidence_batches SET status = 'SUPERSEDED', superseded_at = ? "
                "WHERE revision = ? AND status = 'ACTIVE';",
                (now, prev_current),
            )

        conn.execute(
            "INSERT INTO revisions (revision, content_sha256, status, n_records, "
            "uploaded_at, superseded_at) VALUES (?, ?, 'CURRENT', ?, ?, NULL);",
            (revision, content_sha256, len(rows), now),
        )
        conn.executemany(
            "INSERT INTO records (revision, subject_id, day, outcome) VALUES (?, ?, ?, ?);",
            [(revision, sid, day, outcome) for sid, day, outcome in rows],
        )
        conn.execute(
            "INSERT INTO analyses (revision, status, started_at) VALUES (?, 'PENDING', ?);",
            (revision, now),
        )
        return {
            "accepted": True,
            "idempotent": False,
            "revision_status": "CURRENT",
            "analysis_status": "PENDING",
            "n_records": len(rows),
            "current_revision": revision,
        }


# ---------------------------------------------------------------------------
# 分析读取 / 落库
# ---------------------------------------------------------------------------

def load_records(revision: int) -> list[sqlite3.Row]:
    with read_session() as conn:
        return conn.execute(
            "SELECT subject_id, day, outcome FROM records WHERE revision = ? "
            "ORDER BY day, subject_id;",
            (revision,),
        ).fetchall()


def save_analysis_result(revision: int, result: dict) -> None:
    """把重算结果落库；落库前再次确认修订仍为 CURRENT。"""
    now = _utcnow()
    with write_lock, immediate_transaction() as conn:
        rev = get_revision(conn, revision)
        if rev is None:
            raise RevisionConflict(f"修订 {revision} 不存在。")
        if rev["status"] != "CURRENT":
            # 交错场景：计算期间被新修订取代，绝不发布
            conn.execute(
                "UPDATE analyses SET status = 'SUPERSEDED', finished_at = ?, "
                "error = '修订已被新版本取代' WHERE revision = ? AND status = 'PENDING';",
                (now, revision),
            )
            return
        conn.execute(
            "UPDATE analyses SET status = 'COMPLETED', n_subjects = ?, n_readmission = ?, "
            "n_death = ?, n_censored = ?, cumulative_readmission = ?, cumulative_death = ?, "
            "last_activity_day = ?, error = NULL, finished_at = ? WHERE revision = ?;",
            (
                result["n_subjects"],
                result["n_readmission"],
                result["n_death"],
                result["n_censored"],
                result["cumulative_readmission"],
                result["cumulative_death"],
                result["last_activity_day"],
                now,
                revision,
            ),
        )
        conn.execute("DELETE FROM analysis_days WHERE revision = ?;", (revision,))
        conn.executemany(
            "INSERT INTO analysis_days (revision, day, at_risk, readmission_count, "
            "death_count, censored_count, readmission_cif, death_cif, survival) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
            [
                (
                    revision,
                    d["day"],
                    d["at_risk"],
                    d["readmission_count"],
                    d["death_count"],
                    d["censored_count"],
                    d["readmission_cif"],
                    d["death_cif"],
                    d["survival"],
                )
                for d in result["daily"]
            ],
        )


def mark_analysis_failed(revision: int, error: str) -> None:
    now = _utcnow()
    with write_lock, immediate_transaction() as conn:
        conn.execute(
            "UPDATE analyses SET status = 'FAILED', error = ?, finished_at = ? "
            "WHERE revision = ?;",
            (error[:2000], now, revision),
        )


def mark_analysis_pending(revision: int) -> None:
    now = _utcnow()
    with write_lock, immediate_transaction() as conn:
        conn.execute(
            "UPDATE analyses SET status = 'PENDING', error = NULL, started_at = ?, "
            "finished_at = NULL WHERE revision = ?;",
            (now, revision),
        )


def get_analysis_row(revision: int) -> Optional[sqlite3.Row]:
    with read_session() as conn:
        return conn.execute(
            "SELECT * FROM analyses WHERE revision = ?;", (revision,)
        ).fetchone()


def get_daily_rows(revision: int) -> list[sqlite3.Row]:
    with read_session() as conn:
        return conn.execute(
            "SELECT * FROM analysis_days WHERE revision = ? ORDER BY day;", (revision,)
        ).fetchall()


def list_revisions() -> list[sqlite3.Row]:
    with read_session() as conn:
        return conn.execute(
            "SELECT r.*, a.status AS analysis_status, a.error AS analysis_error "
            "FROM revisions r LEFT JOIN analyses a ON a.revision = r.revision "
            "ORDER BY r.revision DESC;",
        ).fetchall()


def pending_revisions() -> list[int]:
    with read_session() as conn:
        rows = conn.execute(
            "SELECT revision FROM analyses WHERE status = 'PENDING' ORDER BY revision;"
        ).fetchall()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# 事件证据：受理、对账与查询
# ---------------------------------------------------------------------------

def _load_reconciliation(
    conn: sqlite3.Connection, batch_id: int
) -> tuple[Optional[dict], list[dict]]:
    recon = conn.execute(
        "SELECT * FROM reconciliations WHERE batch_id = ?;", (batch_id,)
    ).fetchone()
    issues = conn.execute(
        "SELECT subject_id, issue_type, message, cohort_day, cohort_outcome, "
        "evidence_day, evidence_event FROM reconciliation_issues "
        "WHERE batch_id = ? ORDER BY subject_id, issue_type;",
        (batch_id,),
    ).fetchall()
    return (
        dict(recon) if recon is not None else None,
        [dict(i) for i in issues],
    )


def accept_evidence(
    content_sha256: str,
    events: list[tuple[str, int, str]],
    expected_revision: Optional[int] = None,
) -> dict:
    """在单一 IMMEDIATE 事务内受理事件证据并同步完成对账。

    - 证据只针对**现行**修订；``expected_revision`` 非空且与现行不一致时拒收，
      防止"上传期间队列被替换"导致证据挂错版本。
    - 同版同内容（对现行 ACTIVE 批次 SHA-256 相同）重送：幂等，
      直接返回既有批次与对账结论，不做任何写入。
    - 内容不同：旧 ACTIVE 批次立即置 SUPERSEDED（旧核对资格撤销），
      新批次写入 ACTIVE 并在同事务内完成逐人对账、落库结论与问题清单。
    """
    now = _utcnow()
    with write_lock, immediate_transaction() as conn:
        current = current_revision_no(conn)
        if current is None:
            raise NoCurrentRevision("尚无任何队列修订被受理，无法上传事件证据。")
        if expected_revision is not None and expected_revision != current:
            raise RevisionConflict(
                f"证据针对修订 {expected_revision}，但现行修订为 {current}；"
                "队列已更新，请按新现行队列重新上传证据。",
                current_revision=current,
            )

        active = conn.execute(
            "SELECT * FROM evidence_batches WHERE revision = ? AND status = 'ACTIVE';",
            (current,),
        ).fetchone()
        if active is not None and active["content_sha256"] == content_sha256:
            recon, issues = _load_reconciliation(conn, active["batch_id"])
            return {
                "accepted": False,
                "idempotent": True,
                "revision": current,
                "batch": dict(active),
                "reconciliation": recon,
                "issues": issues,
            }

        if active is not None:
            # 替换证据：立即撤销旧批次核对资格
            conn.execute(
                "UPDATE evidence_batches SET status = 'SUPERSEDED', superseded_at = ? "
                "WHERE batch_id = ?;",
                (now, active["batch_id"]),
            )

        cur = conn.execute(
            "INSERT INTO evidence_batches (revision, content_sha256, n_events, status, "
            "uploaded_at, superseded_at) VALUES (?, ?, ?, 'ACTIVE', ?, NULL);",
            (current, content_sha256, len(events), now),
        )
        batch_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO evidence_events (batch_id, subject_id, day, event_type) "
            "VALUES (?, ?, ?, ?);",
            [(batch_id, sid, day, etype) for sid, day, etype in events],
        )
        # 同事务内读取现行队列记录并对账：结论严格绑定该修订，不借用任何旧版数据
        cohort_rows = conn.execute(
            "SELECT subject_id, day, outcome FROM records WHERE revision = ? "
            "ORDER BY subject_id;",
            (current,),
        ).fetchall()
        found = reconcile(cohort_rows, events)
        recon_status = "PASSED" if not found else "FAILED"
        conn.execute(
            "INSERT INTO reconciliations (batch_id, revision, status, n_subjects, "
            "n_issues, computed_at) VALUES (?, ?, ?, ?, ?, ?);",
            (batch_id, current, recon_status, len(cohort_rows), len(found), now),
        )
        conn.executemany(
            "INSERT INTO reconciliation_issues (batch_id, subject_id, issue_type, "
            "message, cohort_day, cohort_outcome, evidence_day, evidence_event) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            [
                (
                    batch_id,
                    i.subject_id,
                    i.issue_type,
                    i.message,
                    i.cohort_day,
                    i.cohort_outcome,
                    i.evidence_day,
                    i.evidence_event,
                )
                for i in found
            ],
        )
        batch = conn.execute(
            "SELECT * FROM evidence_batches WHERE batch_id = ?;", (batch_id,)
        ).fetchone()
        recon, issues = _load_reconciliation(conn, batch_id)
        return {
            "accepted": True,
            "idempotent": False,
            "revision": current,
            "batch": dict(batch),
            "reconciliation": recon,
            "issues": issues,
        }


def get_active_evidence(revision: int) -> Optional[dict]:
    """现行有效（ACTIVE）证据批次及其对账结论；无有效批次返回 None。"""
    with read_session() as conn:
        batch = conn.execute(
            "SELECT * FROM evidence_batches WHERE revision = ? AND status = 'ACTIVE';",
            (revision,),
        ).fetchone()
        if batch is None:
            return None
        recon, issues = _load_reconciliation(conn, batch["batch_id"])
        return {"batch": dict(batch), "reconciliation": recon, "issues": issues}


def list_evidence_batches(revision: int) -> list[dict]:
    """某修订的全部证据批次（含已撤销），附各自对账状态，供审计留痕。"""
    with read_session() as conn:
        rows = conn.execute(
            "SELECT b.*, r.status AS reconciliation_status, r.n_issues AS "
            "reconciliation_n_issues FROM evidence_batches b "
            "LEFT JOIN reconciliations r ON r.batch_id = b.batch_id "
            "WHERE b.revision = ? ORDER BY b.batch_id DESC;",
            (revision,),
        ).fetchall()
        return [dict(r) for r in rows]


def latest_evidence_status(revision: int) -> Optional[str]:
    """某修订最近一次证据批次的对账状态（无论批次是否仍有效）；无证据返回 None。"""
    with read_session() as conn:
        row = conn.execute(
            "SELECT r.status FROM evidence_batches b "
            "JOIN reconciliations r ON r.batch_id = b.batch_id "
            "WHERE b.revision = ? ORDER BY b.batch_id DESC LIMIT 1;",
            (revision,),
        ).fetchone()
        return row[0] if row else None


def get_serving_status() -> dict:
    """供数状态：仅"现行修订分析完成 + 有效证据对账通过"时可供数。"""
    with read_session() as conn:
        current = current_revision_no(conn)
        if current is None:
            return {
                "revision": None,
                "analysis_status": None,
                "evidence_status": "NONE",
                "servable": False,
                "message": "尚无任何修订被受理。",
            }
        row = conn.execute(
            "SELECT status FROM analyses WHERE revision = ?;", (current,)
        ).fetchone()
        analysis_status = row["status"] if row else None
        ev = conn.execute(
            "SELECT r.status AS recon_status, r.n_issues FROM evidence_batches b "
            "JOIN reconciliations r ON r.batch_id = b.batch_id "
            "WHERE b.revision = ? AND b.status = 'ACTIVE';",
            (current,),
        ).fetchone()
        evidence_status = "NONE" if ev is None else ev["recon_status"]
        servable = analysis_status == "COMPLETED" and evidence_status == "PASSED"
        if servable:
            message = "证据核对通过且分析完成，现行风险结果可供数。"
        elif analysis_status != "COMPLETED":
            message = f"现行修订 {current} 分析状态为 {analysis_status}，结论暂不可供数。"
        elif evidence_status == "NONE":
            message = f"现行修订 {current} 尚无有效事件证据，风险结果不可供数。"
        else:
            message = (
                f"事件证据对账未通过（{ev['n_issues']} 个逐人问题），风险结果不可供数。"
            )
        return {
            "revision": current,
            "analysis_status": analysis_status,
            "evidence_status": evidence_status,
            "servable": servable,
            "message": message,
        }


class RevisionConflict(Exception):
    """版本冲突（同版异内容 / 非递增版号 / 修订已失效）。"""

    def __init__(self, message: str, current_revision: Optional[int] = None):
        super().__init__(message)
        self.current_revision = current_revision


class NoCurrentRevision(Exception):
    """尚无任何队列修订被受理。"""
