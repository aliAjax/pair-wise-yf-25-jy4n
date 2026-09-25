"""持久化与领域逻辑：Schema、迁移，以及串联邀请状态与补位规则的事务方法。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import backfill
import invitations
from common import BusinessError, utcnow

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "review.db"
VALID_DECISIONS = {"accept", "reject", "minor_revision", "major_revision"}

# 旧版 assignments 的 CHECK 只允许 invited/accepted/declined/completed，
# 需要加入 withdrawn；SQLite 无法直接改 CHECK，按表重建完成迁移。
_ASSIGNMENTS_COLS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('invited','accepted','declined','completed','withdrawn')),
    score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 5),
    review_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (paper_id, reviewer_id)
"""
_ASSIGNMENTS_DDL = f"CREATE TABLE assignments ({_ASSIGNMENTS_COLS});"
_ASSIGNMENTS_DDL_IF_NOT_EXISTS = f"CREATE TABLE IF NOT EXISTS assignments ({_ASSIGNMENTS_COLS});"

_INVITATION_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS invitation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    reviewer_id TEXT REFERENCES users(id),
    assignment_id INTEGER,
    event_type TEXT NOT NULL CHECK (event_type IN ('invited','skipped','exhausted')),
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);"""


class ReviewStore:
    """领域逻辑。每个公开方法使用独立连接，避免 HTTP 线程共享 SQLite 连接。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._schema_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def init_schema(self) -> None:
        with self._schema_lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('author','reviewer','chair')),
                    load_limit INTEGER NOT NULL DEFAULT 3 CHECK (load_limit >= 0)
                );
                CREATE TABLE IF NOT EXISTS papers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_id TEXT NOT NULL REFERENCES users(id),
                    title TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted','under_review','decided','withdrawn')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (paper_id, version)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS bids (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    interest TEXT NOT NULL CHECK (interest IN ('want','maybe','decline')),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                """
                + _ASSIGNMENTS_DDL_IF_NOT_EXISTS
                + """
                CREATE TABLE IF NOT EXISTS rebuttals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    author_id TEXT NOT NULL REFERENCES users(id),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    decision TEXT NOT NULL CHECK (decision IN ('accept','reject','minor_revision','major_revision')),
                    note TEXT NOT NULL DEFAULT '',
                    decided_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (paper_id) REFERENCES papers(id)
                );
                """
                + _INVITATION_EVENTS_DDL
            )
            self._migrate_assignments(conn)

    def _migrate_assignments(self, conn: sqlite3.Connection) -> None:
        """旧库的 assignments CHECK 缺少 withdrawn：检测后按新定义重建。"""
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='assignments'"
        ).fetchone()
        if sql is None or "withdrawn" in sql["sql"]:
            return
        conn.executescript(
            f"""
            ALTER TABLE assignments RENAME TO assignments_old;
            {_ASSIGNMENTS_DDL}
            INSERT INTO assignments
                (id,paper_id,reviewer_id,status,score,review_text,created_at,updated_at)
                SELECT id,paper_id,reviewer_id,status,score,review_text,created_at,updated_at
                  FROM assignments_old;
            DROP TABLE assignments_old;
            """
        )

    def seed(self) -> None:
        self.init_schema()
        users = [
            ("alice", "Alice 作者", "author", 0),
            ("bob", "Bob 作者", "author", 0),
            ("r1", "评审人一号", "reviewer", 3),
            ("r2", "评审人二号", "reviewer", 3),
            ("r3", "评审人三号", "reviewer", 2),
            ("chair", "程序委员会主席", "chair", 0),
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,load_limit) VALUES(?,?,?,?)", users
            )

    def _user(self, conn: sqlite3.Connection, user_id: str | None) -> sqlite3.Row:
        if not user_id:
            raise BusinessError("缺少 X-User-Id 请求头", 401, "authentication_required")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise BusinessError("用户不存在", 401, "unknown_user")
        return row

    @staticmethod
    def _require(row: sqlite3.Row, role: str) -> None:
        if row["role"] != role:
            raise BusinessError(f"该操作仅允许 {role} 角色", 403, "forbidden")

    def _audit(self, conn: sqlite3.Connection, paper_id: int | None, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(paper_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (paper_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _active_paper(self, conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row:
        paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not paper or paper["status"] not in {"submitted", "under_review"}:
            raise BusinessError("论文不存在或当前不可分配", 409, "paper_unavailable")
        return paper

    def submit_paper(self, user_id: str, title: str, abstract: str) -> dict:
        title, abstract = title.strip(), abstract.strip()
        if len(title) < 3 or len(abstract) < 20:
            raise BusinessError("标题至少 3 字，摘要至少 20 字", 422, "invalid_paper")
        digest = hashlib.sha256(f"{title}\n{abstract}".encode()).hexdigest()
        with self.connect() as conn:
            user = self._user(conn, user_id)
            self._require(user, "author")
            cur = conn.execute(
                "INSERT INTO papers(author_id,title,abstract,created_at) VALUES(?,?,?,?)",
                (user_id, title, abstract, utcnow()),
            )
            paper_id = cur.lastrowid
            conn.execute(
                "INSERT INTO paper_versions(paper_id,version,content_hash,created_at) VALUES(?,?,?,?)",
                (paper_id, 1, digest, utcnow()),
            )
            self._audit(conn, paper_id, user_id, "paper.submit", {"version": 1, "sha256": digest})
            return {"id": paper_id, "status": "submitted", "version": 1, "sha256": digest}

    def _paper_view(self, conn: sqlite3.Connection, paper: sqlite3.Row, viewer: sqlite3.Row) -> dict:
        data = {
            "id": paper["id"],
            "title": paper["title"],
            "abstract": paper["abstract"],
            "status": paper["status"],
            "created_at": paper["created_at"],
        }
        if viewer["role"] == "chair" or viewer["id"] == paper["author_id"]:
            data["author_id"] = paper["author_id"]
        else:
            data["author_id"] = None  # 双盲：评审人看不到作者身份。
        return data

    def list_papers(self, user_id: str) -> list[dict]:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "chair":
                rows = conn.execute("SELECT * FROM papers ORDER BY id").fetchall()
            elif user["role"] == "author":
                rows = conn.execute("SELECT * FROM papers WHERE author_id=? ORDER BY id", (user_id,)).fetchall()
            else:
                placeholders = ",".join("?" for _ in invitations.ACTIVE_STATUSES)
                rows = conn.execute(
                    f"""SELECT p.* FROM papers p
                       LEFT JOIN assignments a
                         ON a.paper_id=p.id AND a.reviewer_id=?
                        AND a.status IN ({placeholders})
                       LEFT JOIN bids b ON b.paper_id=p.id AND b.reviewer_id=?
                       WHERE a.id IS NOT NULL OR b.paper_id IS NOT NULL ORDER BY p.id""",
                    (user_id, *invitations.ACTIVE_STATUSES, user_id),
                ).fetchall()
            return [self._paper_view(conn, row, user) for row in rows]

    def get_paper(self, user_id: str, paper_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if user["role"] == "reviewer":
                self._require_reviewer_access(conn, user["id"], paper_id)
            elif user["role"] == "author" and paper["author_id"] != user_id:
                raise BusinessError("作者只能查看自己的论文", 403, "forbidden")
            return self._paper_view(conn, paper, user)

    @staticmethod
    def _require_reviewer_access(conn: sqlite3.Connection, reviewer_id: str, paper_id: int) -> None:
        # 双盲：只有仍有效（待响应/已接受/已完成）的分配或表达过意向的评审人能看到论文；
        # 拒绝或撤回后授权随之消失，不会借此看到作者身份。
        placeholders = ",".join("?" for _ in invitations.ACTIVE_STATUSES)
        allowed = conn.execute(
            f"""SELECT 1 FROM assignments
                 WHERE paper_id=? AND reviewer_id=? AND status IN ({placeholders})
                UNION
                SELECT 1 FROM bids WHERE paper_id=? AND reviewer_id=?""",
            (paper_id, reviewer_id, *invitations.ACTIVE_STATUSES, paper_id, reviewer_id),
        ).fetchone()
        if not allowed:
            raise BusinessError("评审人未获授权查看该论文", 403, "forbidden")

    def add_conflict(self, chair_id: str, paper_id: int, reviewer_id: str, reason: str) -> dict:
        if not reason.strip():
            raise BusinessError("利益冲突原因不能为空", 422, "invalid_reason")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            if not conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise BusinessError("论文不存在", 404, "not_found")
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            try:
                conn.execute(
                    "INSERT INTO conflicts(reviewer_id,paper_id,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                    (reviewer_id, paper_id, reason.strip(), chair_id, utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("利益冲突已登记", 409, "conflict_exists")
            self._audit(conn, paper_id, chair_id, "conflict.add", {"reviewer_id": reviewer_id, "reason": reason.strip()})
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "reason": reason.strip()}

    def bid(self, reviewer_id: str, paper_id: int, interest: str, note: str = "") -> dict:
        if interest not in {"want", "maybe", "decline"}:
            raise BusinessError("意向必须为 want、maybe 或 decline", 422, "invalid_interest")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            paper = conn.execute("SELECT status FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["status"] not in {"submitted", "under_review"}:
                raise BusinessError("论文不存在或当前不可表达意向", 409, "paper_unavailable")
            if invitations.has_conflict(conn, reviewer_id, paper_id):
                raise BusinessError("存在利益冲突，不能表达评审意向", 409, "conflict_of_interest")
            conn.execute(
                """INSERT INTO bids(reviewer_id,paper_id,interest,note,created_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(reviewer_id,paper_id) DO UPDATE SET interest=excluded.interest,note=excluded.note,created_at=excluded.created_at""",
                (reviewer_id, paper_id, interest, note.strip(), utcnow()),
            )
            self._audit(conn, paper_id, reviewer_id, "bid.set", {"interest": interest, "note": note.strip()})
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "interest": interest}

    def assign(self, chair_id: str, paper_id: int, reviewer_id: str) -> dict:
        """主席手动邀请某位评审人（不触发自动补位）。"""
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = self._active_paper(conn, paper_id)
                reviewer = self._user(conn, reviewer_id)
                self._require(reviewer, "reviewer")
                assignment_id = invitations.invite(conn, paper, reviewer, chair_id, self._audit)
                conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
                backfill.record_event(conn, paper_id, chair_id, "invited", "manual",
                                      reviewer_id=reviewer_id, assignment_id=assignment_id)
                return {"id": assignment_id, "paper_id": paper_id,
                        "reviewer_id": reviewer_id, "status": "invited"}
            except Exception:
                conn.rollback()
                raise

    def auto_fill(self, chair_id: str, paper_id: int) -> dict:
        """主席一键补齐：按意向顺序把有效邀请补到两份。"""
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._active_paper(conn, paper_id)
                return backfill.fill(conn, paper_id, chair_id, "auto_fill", self._audit)
            except Exception:
                conn.rollback()
                raise

    def respond_assignment(self, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            try:
                conn.execute("BEGIN IMMEDIATE")
                status = invitations.respond(conn, assignment_id, reviewer_id, accepted, self._audit)
                result = {"id": assignment_id, "status": status}
                # 拒绝后名额空出：在同一事务内自动按意向顺序补下一位。
                if not accepted:
                    row = conn.execute("SELECT paper_id FROM assignments WHERE id=?", (assignment_id,)).fetchone()
                    paper = conn.execute("SELECT status FROM papers WHERE id=?", (row["paper_id"],)).fetchone()
                    if paper and paper["status"] in {"submitted", "under_review"}:
                        result["backfill"] = backfill.fill(
                            conn, row["paper_id"], reviewer_id, "after_decline", self._audit
                        )
                return result
            except Exception:
                conn.rollback()
                raise

    def withdraw_assignment(self, actor_id: str, assignment_id: int) -> dict:
        """主席撤回待响应邀请，或评审人撤回已接受邀请；随后自动补位。"""
        with self.connect() as conn:
            actor = self._user(conn, actor_id)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = invitations.withdraw(conn, assignment_id, actor, self._audit)
                paper = conn.execute("SELECT status FROM papers WHERE id=?", (row["paper_id"],)).fetchone()
                result = {"id": assignment_id, "status": "withdrawn"}
                if paper and paper["status"] in {"submitted", "under_review"}:
                    result["backfill"] = backfill.fill(
                        conn, row["paper_id"], actor_id, "after_withdraw", self._audit
                    )
                return result
            except Exception:
                conn.rollback()
                raise

    def invitation_overview(self, chair_id: str, paper_id: int) -> dict:
        """主席页面数据：邀请名单、补位事件与原因、各评审人负载。"""
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            if not conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise BusinessError("论文不存在", 404, "not_found")
            assignments = [
                {
                    "id": r["id"],
                    "reviewer_id": r["reviewer_id"],
                    "status": r["status"],
                    "score": r["score"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM assignments WHERE paper_id=? ORDER BY id", (paper_id,)
                ).fetchall()
            ]
            events = [
                {
                    "id": r["id"],
                    "event_type": r["event_type"],
                    "reviewer_id": r["reviewer_id"],
                    "assignment_id": r["assignment_id"],
                    "reason": r["reason"],
                    "reason_text": backfill.reason_text(r["reason"]),
                    "actor_id": r["actor_id"],
                    "created_at": r["created_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM invitation_events WHERE paper_id=? ORDER BY id", (paper_id,)
                ).fetchall()
            ]
            load_rows = conn.execute(
                f"""SELECT u.id AS reviewer_id, u.load_limit AS load_limit,
                           (SELECT COUNT(*) FROM assignments a
                             WHERE a.reviewer_id=u.id
                               AND a.status IN ({','.join('?' for _ in invitations.LOAD_STATUSES)})) AS current_load
                      FROM users u WHERE u.role='reviewer' ORDER BY u.id""",
                invitations.LOAD_STATUSES,
            ).fetchall()
            return {
                "paper_id": paper_id,
                "target_active": backfill.TARGET_ACTIVE,
                "active_invitations": invitations.paper_load(conn, paper_id),
                "assignments": assignments,
                "events": events,
                "reviewer_load": [dict(r) for r in load_rows],
            }

    def submit_review(self, reviewer_id: str, assignment_id: int, score: int, text: str) -> dict:
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise BusinessError("评分必须是 1 到 5 的整数", 422, "invalid_score")
        if len(text.strip()) < 10:
            raise BusinessError("评审意见至少 10 字", 422, "review_too_short")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] != "accepted":
                raise BusinessError("只有已接受邀请的评审人可以提交评审", 409, "invalid_assignment_state")
            conn.execute(
                "UPDATE assignments SET status='completed',score=?,review_text=?,updated_at=? WHERE id=?",
                (score, text.strip(), utcnow(), assignment_id),
            )
            self._audit(conn, row["paper_id"], reviewer_id, "review.submit", {"assignment_id": assignment_id, "score": score})
            return {"id": assignment_id, "status": "completed", "score": score}

    def submit_rebuttal(self, author_id: str, paper_id: int, content: str) -> dict:
        if len(content.strip()) < 10:
            raise BusinessError("Rebuttal 至少 10 字", 422, "rebuttal_too_short")
        with self.connect() as conn:
            author = self._user(conn, author_id)
            self._require(author, "author")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["author_id"] != author_id:
                raise BusinessError("论文不存在或不属于当前作者", 404, "not_found")
            completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
            if completed < 1:
                raise BusinessError("至少收到一份完整评审后才能提交 Rebuttal", 409, "reviews_not_ready")
            try:
                cur = conn.execute(
                    "INSERT INTO rebuttals(paper_id,author_id,content,created_at) VALUES(?,?,?,?)",
                    (paper_id, author_id, content.strip(), utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("每篇论文只能提交一次 Rebuttal", 409, "rebuttal_exists")
            self._audit(conn, paper_id, author_id, "rebuttal.submit", {"rebuttal_id": cur.lastrowid})
            return {"id": cur.lastrowid, "paper_id": paper_id, "content": content.strip()}

    def decide(self, chair_id: str, paper_id: int, decision: str, note: str = "") -> dict:
        if decision not in VALID_DECISIONS:
            raise BusinessError("决定值不合法", 422, "invalid_decision")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或已经决定", 409, "paper_decided")
                completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
                if completed < 2:
                    raise BusinessError("至少需要两份已完成评审才能作出决定", 409, "insufficient_reviews")
                cur = conn.execute(
                    "INSERT INTO decisions(paper_id,decision,note,decided_by,created_at) VALUES(?,?,?,?,?)",
                    (paper_id, decision, note.strip(), chair_id, utcnow()),
                )
                conn.execute("UPDATE papers SET status='decided' WHERE id=?", (paper_id,))
                self._audit(conn, paper_id, chair_id, "decision.record", {"decision": decision, "note": note.strip()})
                return {"id": cur.lastrowid, "paper_id": paper_id, "decision": decision, "note": note.strip()}
            except Exception:
                conn.rollback()
                raise

    def history(self, user_id: str, paper_id: int) -> list[dict]:
        self.get_paper(user_id, paper_id)  # 权限检查。
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log WHERE paper_id=? ORDER BY id", (paper_id,)).fetchall()
            return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]
