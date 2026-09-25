"""补位规则：按评审意向顺序为每篇论文补齐有效邀请。

规则：
- 每篇论文保持 TARGET_VALID_INVITATIONS 份有效邀请（invited/accepted/completed）。
- 候选人为表达过 want/maybe 意向的评审人：want 优先于 maybe，同档按意向时间先后。
- 遇利益冲突、负载已满、已有有效邀请、曾拒绝该论文者，跳过并记录原因，继续往后找；
  曾撤回者已释放负载，可以再次受邀。
- 评审人拒绝或撤回后由系统自动补位，主席也可随时手动触发；
  每次补位的结果与原因写入 backfill_runs，供主席页面展示。
"""
from __future__ import annotations

import json
import sqlite3

from app import BusinessError, utcnow
from invitations import SLOT_STATUSES

TARGET_VALID_INVITATIONS = 2
CAUSES = {"manual", "declined", "withdrawn"}
_RESULT_MESSAGES = {
    "filled": "已补齐有效邀请",
    "short": "愿意评审的候选人已用完，仍有空缺",
    "satisfied": "有效邀请已满足，无需补位",
    "paper_closed": "论文已决定或撤稿，无需补位",
}


class BackfillService:
    """候选排序、跳过判定与补位记录。连接与审计取自 ReviewStore。"""

    def __init__(self, store):
        self.store = store

    def _candidates(self, conn: sqlite3.Connection, paper_id: int) -> list[sqlite3.Row]:
        """愿意评审的人：want 优先于 maybe，同档按意向时间（再按 ID 保证稳定）。"""
        return conn.execute(
            """SELECT b.reviewer_id, b.interest, u.load_limit
               FROM bids b JOIN users u ON u.id = b.reviewer_id
               WHERE b.paper_id=? AND b.interest IN ('want','maybe')
               ORDER BY CASE b.interest WHEN 'want' THEN 0 ELSE 1 END,
                        b.created_at, b.reviewer_id""",
            (paper_id,),
        ).fetchall()

    def _skip_reason(self, conn: sqlite3.Connection, paper_id: int, candidate: sqlite3.Row) -> str | None:
        """返回 None 表示可以邀请，否则为跳过原因。"""
        reviewer_id = candidate["reviewer_id"]
        if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)).fetchone():
            return "存在利益冲突"
        row = conn.execute(
            "SELECT status FROM assignments WHERE paper_id=? AND reviewer_id=?",
            (paper_id, reviewer_id),
        ).fetchone()
        if row:
            if row["status"] in SLOT_STATUSES:
                return "已有有效邀请"
            if row["status"] == "declined":
                return "已拒绝该论文，可由主席手动再次邀请"
            # withdrawn：已释放负载，允许再次受邀。
        if self.store.invitations.load(conn, reviewer_id) >= candidate["load_limit"]:
            return "负载已满"
        return None

    def run(self, actor_id: str, paper_id: int, cause: str) -> dict:
        """执行一次补位并记录结果与原因。cause 为 manual / declined / withdrawn。"""
        if cause not in CAUSES:
            raise BusinessError("未知的补位触发方式", 422, "invalid_cause")
        with self.store.connect() as conn:
            actor = self.store._user(conn, actor_id)
            if cause == "manual":
                self.store._require(actor, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper:
                    raise BusinessError("论文不存在", 404, "not_found")
                invitations = self.store.invitations
                valid_before = invitations.valid_count(conn, paper_id)
                entries: list[dict] = []
                if paper["status"] not in {"submitted", "under_review"}:
                    result = "paper_closed"
                elif valid_before >= TARGET_VALID_INVITATIONS:
                    result = "satisfied"
                else:
                    needed = TARGET_VALID_INVITATIONS - valid_before
                    for candidate in self._candidates(conn, paper_id):
                        if needed == 0:
                            break
                        reason = self._skip_reason(conn, paper_id, candidate)
                        if reason:
                            entries.append({"reviewer_id": candidate["reviewer_id"], "outcome": "skipped", "reason": reason})
                            continue
                        assignment_id, action = invitations._invite_locked(conn, paper_id, candidate["reviewer_id"])
                        self.store._audit(conn, paper_id, actor_id, action, {
                            "assignment_id": assignment_id,
                            "reviewer_id": candidate["reviewer_id"],
                            "cause": f"backfill:{cause}",
                        })
                        entries.append({
                            "reviewer_id": candidate["reviewer_id"],
                            "outcome": "invited",
                            "reason": "",
                            "assignment_id": assignment_id,
                        })
                        needed -= 1
                    if any(entry["outcome"] == "invited" for entry in entries):
                        conn.execute("UPDATE papers SET status='under_review' WHERE id=? AND status='submitted'", (paper_id,))
                    result = "filled" if needed == 0 else "short"
                valid_after = invitations.valid_count(conn, paper_id)
                detail = {
                    "cause": cause,
                    "target": TARGET_VALID_INVITATIONS,
                    "valid_before": valid_before,
                    "valid_after": valid_after,
                    "result": result,
                    "message": _RESULT_MESSAGES[result],
                    "entries": entries,
                }
                cur = conn.execute(
                    "INSERT INTO backfill_runs(paper_id,cause,actor_id,detail,created_at) VALUES(?,?,?,?,?)",
                    (paper_id, cause, actor_id, json.dumps(detail, ensure_ascii=False, sort_keys=True), utcnow()),
                )
                self.store._audit(conn, paper_id, actor_id, "backfill.run", {
                    "run_id": cur.lastrowid,
                    "cause": cause,
                    "result": result,
                    "invited": [entry["reviewer_id"] for entry in entries if entry["outcome"] == "invited"],
                })
                return {"run_id": cur.lastrowid, "paper_id": paper_id, **detail}
            except Exception:
                conn.rollback()
                raise

    def chair_view(self, chair_id: str, paper_id: int) -> dict:
        """主席页面总览：邀请状态、候选人可邀性及原因、历次补位结果。"""
        with self.store.connect() as conn:
            chair = self.store._user(conn, chair_id)
            self.store._require(chair, "chair")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            invitations = self.store.invitations
            candidates = []
            for candidate in self._candidates(conn, paper_id):
                reason = self._skip_reason(conn, paper_id, candidate)
                candidates.append({
                    "reviewer_id": candidate["reviewer_id"],
                    "interest": candidate["interest"],
                    "load": invitations.load(conn, candidate["reviewer_id"]),
                    "load_limit": candidate["load_limit"],
                    "eligible": reason is None,
                    "reason": reason or "",
                })
            runs = conn.execute(
                "SELECT * FROM backfill_runs WHERE paper_id=? ORDER BY id DESC", (paper_id,)
            ).fetchall()
            return {
                "paper_id": paper_id,
                "paper_status": paper["status"],
                "target_valid": TARGET_VALID_INVITATIONS,
                "valid_count": invitations.valid_count(conn, paper_id),
                "assignments": invitations.list_for_paper(conn, paper_id),
                "candidates": candidates,
                "backfill_runs": [
                    {"id": row["id"], "actor_id": row["actor_id"], "created_at": row["created_at"], **json.loads(row["detail"])}
                    for row in runs
                ],
            }
