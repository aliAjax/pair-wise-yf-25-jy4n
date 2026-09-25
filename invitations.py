"""邀请状态机：邀请、应答、撤回与评审人负载统计。

状态流转：
    invited  ──接受──> accepted ──提交评审──> completed
    invited  ──拒绝──> declined
    accepted ──撤回──> withdrawn

负载口径：仅 invited/accepted 占用评审人负载；declined/withdrawn 不占负载，
空闲后可再次受邀（同一行重置为 invited，而不是报错“已分配”）。
"""
from __future__ import annotations

import sqlite3

from app import BusinessError, utcnow

ACTIVE_STATUSES = ("invited", "accepted")  # 进行中，占用负载
SLOT_STATUSES = ("invited", "accepted", "completed")  # 计入论文的“有效邀请”
FREE_STATUSES = ("declined", "withdrawn")  # 已释放，不占负载，可再次受邀


class InvitationService:
    """管理 assignments 表的状态流转。连接与审计取自 ReviewStore。"""

    def __init__(self, store):
        self.store = store

    # ---- 查询 ----

    def load(self, conn: sqlite3.Connection, reviewer_id: str) -> int:
        """当前负载：只统计进行中的邀请，撤回和拒绝不占负载。"""
        return conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ('invited','accepted')",
            (reviewer_id,),
        ).fetchone()[0]

    def valid_count(self, conn: sqlite3.Connection, paper_id: int) -> int:
        """论文当前的有效邀请数：invited/accepted/completed。"""
        return conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status IN ('invited','accepted','completed')",
            (paper_id,),
        ).fetchone()[0]

    def list_for_paper(self, conn: sqlite3.Connection, paper_id: int) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM assignments WHERE paper_id=? ORDER BY id", (paper_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 状态流转 ----

    def _invite_locked(self, conn: sqlite3.Connection, paper_id: int, reviewer_id: str) -> tuple[int, str]:
        """在已有事务内发出邀请；曾拒绝/撤回的记录重置为 invited（再次受邀）。

        调用方需先完成冲突与负载检查。返回 (assignment_id, 审计动作)。
        """
        now = utcnow()
        existing = conn.execute(
            "SELECT id, status FROM assignments WHERE paper_id=? AND reviewer_id=?",
            (paper_id, reviewer_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE assignments SET status='invited',score=NULL,review_text=NULL,updated_at=? WHERE id=?",
                (now, existing["id"]),
            )
            return existing["id"], "assignment.reinvite"
        cur = conn.execute(
            "INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(?,?,?,?)",
            (paper_id, reviewer_id, now, now),
        )
        return cur.lastrowid, "assignment.invite"

    def invite(self, chair_id: str, paper_id: int, reviewer_id: str) -> dict:
        """主席手动邀请评审人；拒绝/撤回过的人可再次受邀。"""
        with self.store.connect() as conn:
            chair = self.store._user(conn, chair_id)
            self.store._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或不可分配", 409, "paper_unavailable")
                reviewer = self.store._user(conn, reviewer_id)
                self.store._require(reviewer, "reviewer")
                if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)).fetchone():
                    raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
                existing = conn.execute(
                    "SELECT status FROM assignments WHERE paper_id=? AND reviewer_id=?",
                    (paper_id, reviewer_id),
                ).fetchone()
                if existing and existing["status"] not in FREE_STATUSES:
                    raise BusinessError("该评审人已被分配此论文", 409, "assignment_exists")
                if self.load(conn, reviewer_id) >= reviewer["load_limit"]:
                    raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
                assignment_id, action = self._invite_locked(conn, paper_id, reviewer_id)
                conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
                self.store._audit(conn, paper_id, chair_id, action, {"assignment_id": assignment_id, "reviewer_id": reviewer_id})
                return {"id": assignment_id, "paper_id": paper_id, "reviewer_id": reviewer_id, "status": "invited"}
            except Exception:
                conn.rollback()
                raise

    def respond(self, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
        """评审人接受或拒绝邀请。"""
        with self.store.connect() as conn:
            reviewer = self.store._user(conn, reviewer_id)
            self.store._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] != "invited":
                raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
            status = "accepted" if accepted else "declined"
            conn.execute("UPDATE assignments SET status=?,updated_at=? WHERE id=?", (status, utcnow(), assignment_id))
            self.store._audit(conn, row["paper_id"], reviewer_id, "assignment.respond", {"assignment_id": assignment_id, "status": status})
            return {"id": assignment_id, "paper_id": row["paper_id"], "status": status}

    def withdraw(self, reviewer_id: str, assignment_id: int) -> dict:
        """评审人撤回已接受的邀请；立即释放负载，之后可再次受邀。"""
        with self.store.connect() as conn:
            reviewer = self.store._user(conn, reviewer_id)
            self.store._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] != "accepted":
                raise BusinessError("只有已接受的邀请可以撤回", 409, "invalid_assignment_state")
            conn.execute("UPDATE assignments SET status='withdrawn',updated_at=? WHERE id=?", (utcnow(), assignment_id))
            self.store._audit(conn, row["paper_id"], reviewer_id, "assignment.withdraw", {"assignment_id": assignment_id})
            return {"id": assignment_id, "paper_id": row["paper_id"], "status": "withdrawn"}
