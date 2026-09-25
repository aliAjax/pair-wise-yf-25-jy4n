"""业务文件二：补位规则。

按意向顺序为论文寻找并邀请下一位评审人，使每篇论文始终保持
TARGET_ACTIVE 份有效邀请（待响应/已接受/已完成均算有效）。

候选意向优先级：want > maybe > 未表达意向；明确 decline 的人排在最后，
实际不会被邀请。候选还会被以下条件逐个跳过：
- 已有该论文的分配记录（含此前拒绝/撤回者，同一篇不二次邀请）；
- 与论文存在利益冲突；
- 个人负载已满（拒绝/撤回不占负载，空出后仍可受邀于其他论文）。
跳过与补位结果都写入 invitation_events，并在主席页面展示原因。
"""
from __future__ import annotations

import sqlite3

from common import utcnow
import invitations

TARGET_ACTIVE = 2

# 事件原因码 → 主席页面展示文案。
REASON_TEXT = {
    "manual": "主席手动邀请",
    "auto_fill": "主席手动触发补齐",
    "after_decline": "评审人拒绝后自动补位",
    "after_withdraw": "邀请/接受被撤回后自动补位",
    "skipped_already_assigned": "已在该论文的分配名单中（含此前拒绝/撤回）",
    "skipped_conflict": "存在利益冲突",
    "skipped_at_capacity": "负载已满",
    "skipped_declined_interest": "本人意向为不愿评审（decline）",
    "pool_exhausted": "意向名单已遍历完，没有可邀请的人",
    "already_full": "有效邀请已满两份，无需补位",
}

# 意向排序：愿意（want/maybe）优先，未表达意向次之，明确不愿最后。
_INTEREST_RANK = "CASE b.interest WHEN 'want' THEN 0 WHEN 'maybe' THEN 1 ELSE 2 END"


def reason_text(code: str) -> str:
    return REASON_TEXT.get(code, code)


def record_event(
    conn: sqlite3.Connection,
    paper_id: int,
    actor_id: str,
    event_type: str,
    reason: str,
    reviewer_id: str | None = None,
    assignment_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO invitation_events
           (paper_id,reviewer_id,assignment_id,event_type,reason,actor_id,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (paper_id, reviewer_id, assignment_id, event_type, reason, actor_id, utcnow()),
    )


def _candidates(conn: sqlite3.Connection, paper_id: int) -> list[sqlite3.Row]:
    """按意向顺序返回全部评审人（含其负载上限与当前负载、意向、既有分配状态）。"""
    return conn.execute(
        f"""SELECT u.id AS reviewer_id,
                   u.load_limit AS load_limit,
                   b.interest AS interest,
                   a.status AS assignment_status,
                   {_INTEREST_RANK} AS interest_rank,
                   (SELECT COUNT(*) FROM assignments x
                     WHERE x.reviewer_id=u.id
                       AND x.status IN ({','.join('?' for _ in invitations.LOAD_STATUSES)})) AS current_load
              FROM users u
              LEFT JOIN bids b ON b.reviewer_id=u.id AND b.paper_id=?
              LEFT JOIN assignments a ON a.reviewer_id=u.id AND a.paper_id=?
             WHERE u.role='reviewer'
             ORDER BY interest_rank, u.id""",
        (*invitations.LOAD_STATUSES, paper_id, paper_id),
    ).fetchall()


def fill(
    conn: sqlite3.Connection,
    paper_id: int,
    actor_id: str,
    trigger: str,
    audit,
) -> dict:
    """把论文的有效邀请补到 TARGET_ACTIVE 份。

    必须在调用方的事务（BEGIN IMMEDIATE）内执行。返回本次补位结果，
    每个被检查/跳过/邀请的人都在 events 中留下带原因的记录。
    """
    invited: list[dict] = []
    skipped: list[dict] = []
    active = invitations.paper_load(conn, paper_id)

    if active >= TARGET_ACTIVE:
        # 名额已满时不做任何事，也不刷事件，避免噪音。
        return {"target": TARGET_ACTIVE, "active": active, "invited": invited,
                "skipped": skipped, "exhausted": False}

    for cand in _candidates(conn, paper_id):
        if active >= TARGET_ACTIVE:
            break
        rid = cand["reviewer_id"]

        def skip(code: str) -> None:
            record_event(conn, paper_id, actor_id, "skipped", code, reviewer_id=rid)
            skipped.append({"reviewer_id": rid, "reason": code, "reason_text": reason_text(code)})

        if cand["assignment_status"] is not None:
            skip("skipped_already_assigned")
            continue
        if invitations.has_conflict(conn, rid, paper_id):
            skip("skipped_conflict")
            continue
        if cand["current_load"] >= cand["load_limit"]:
            skip("skipped_at_capacity")
            continue
        if cand["interest"] == "decline":
            skip("skipped_declined_interest")
            continue

        assignment_id = invitations.insert_invitation(conn, paper_id, rid, actor_id, audit)
        conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
        record_event(conn, paper_id, actor_id, "invited", trigger,
                     reviewer_id=rid, assignment_id=assignment_id)
        invited.append({"assignment_id": assignment_id, "reviewer_id": rid,
                        "reason": trigger, "reason_text": reason_text(trigger)})
        active += 1

    exhausted = active < TARGET_ACTIVE
    if exhausted:
        record_event(conn, paper_id, actor_id, "exhausted", "pool_exhausted")
    return {"target": TARGET_ACTIVE, "active": active, "invited": invited,
            "skipped": skipped, "exhausted": exhausted}
