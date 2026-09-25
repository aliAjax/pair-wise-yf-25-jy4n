"""业务文件一：邀请状态。

负责单条评审邀请的状态机与邀请/响应/撤回时的检查：
- 状态：invited（待响应）→ accepted（已接受）→ completed（已交评分）；
- declined（拒绝）与 withdrawn（撤回）为终止态，均不占用评审负载，
  但仍保留在该论文的历史中，本人不会被重复邀请到同一篇论文。
"""
from __future__ import annotations

import sqlite3

from common import BusinessError, utcnow

# 仍占用论文“有效邀请”名额的状态：待响应、已接受、已完成。
ACTIVE_STATUSES = ("invited", "accepted", "completed")
# 仍占用评审人个人负载的状态：待响应、已接受（拒绝/撤回/已完成均释放负载）。
LOAD_STATUSES = ("invited", "accepted")
TERMINAL_STATUSES = ("declined", "withdrawn", "completed")


def paper_load(conn: sqlite3.Connection, paper_id: int) -> int:
    """该论文当前的有效邀请数（目标是始终保持 TARGET_ACTIVE 份）。"""
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    return conn.execute(
        f"SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status IN ({placeholders})",
        (paper_id, *ACTIVE_STATUSES),
    ).fetchone()[0]


def reviewer_load(conn: sqlite3.Connection, reviewer_id: str) -> int:
    """评审人当前负载；已拒绝、已撤回的邀请不占负载。"""
    placeholders = ",".join("?" for _ in LOAD_STATUSES)
    return conn.execute(
        f"SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ({placeholders})",
        (reviewer_id, *LOAD_STATUSES),
    ).fetchone()[0]


def has_conflict(conn: sqlite3.Connection, reviewer_id: str, paper_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?",
        (reviewer_id, paper_id),
    ).fetchone() is not None


def insert_invitation(
    conn: sqlite3.Connection,
    paper_id: int,
    reviewer_id: str,
    actor_id: str,
    audit,
) -> int:
    """在调用方已完成冲突/负载/重复检查后写入邀请并记审计，返回 assignment id。"""
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(?,?,?,?)",
        (paper_id, reviewer_id, now, now),
    )
    assignment_id = cur.lastrowid
    audit(conn, paper_id, actor_id, "assignment.invite",
          {"assignment_id": assignment_id, "reviewer_id": reviewer_id})
    return assignment_id


def invite(
    conn: sqlite3.Connection,
    paper: sqlite3.Row,
    reviewer: sqlite3.Row,
    chair_id: str,
    audit,
) -> int:
    """主席手动邀请：执行冲突、负载、重复邀请检查后发出邀请。"""
    if has_conflict(conn, reviewer["id"], paper["id"]):
        raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
    if reviewer_load(conn, reviewer["id"]) >= reviewer["load_limit"]:
        raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
    existing = conn.execute(
        "SELECT status FROM assignments WHERE paper_id=? AND reviewer_id=?",
        (paper["id"], reviewer["id"]),
    ).fetchone()
    if existing is not None:
        raise BusinessError("该评审人已被分配此论文", 409, "assignment_exists")
    return insert_invitation(conn, paper["id"], reviewer["id"], chair_id, audit)


def respond(
    conn: sqlite3.Connection,
    assignment_id: int,
    reviewer_id: str,
    accepted: bool,
    audit,
) -> str:
    """评审人接受/拒绝待响应邀请。拒绝后该名额立即空出，可被补位。"""
    row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
    if not row or row["reviewer_id"] != reviewer_id:
        raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
    if row["status"] != "invited":
        raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
    status = "accepted" if accepted else "declined"
    conn.execute(
        "UPDATE assignments SET status=?,updated_at=? WHERE id=?",
        (status, utcnow(), assignment_id),
    )
    audit(conn, row["paper_id"], reviewer_id, "assignment.respond",
          {"assignment_id": assignment_id, "status": status})
    return status


def withdraw(
    conn: sqlite3.Connection,
    assignment_id: int,
    actor: sqlite3.Row,
    audit,
) -> sqlite3.Row:
    """撤回邀请/接受。

    - 主席可撤回任何仍处于 invited 的邀请；
    - 评审人只能撤回自己已 accepted 的邀请；
    - declined/withdrawn/completed 均为终止态，不可再撤回。
    撤回后不占负载，名额立即空出，可被补位。
    """
    row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
    if not row:
        raise BusinessError("分配不存在", 404, "not_found")
    if actor["role"] == "chair":
        if row["status"] != "invited":
            raise BusinessError("主席只能撤回待响应的邀请", 409, "invalid_assignment_state")
    elif actor["role"] == "reviewer":
        if row["reviewer_id"] != actor["id"]:
            raise BusinessError("分配不属于当前评审人", 404, "not_found")
        if row["status"] != "accepted":
            raise BusinessError("只能撤回自己已接受的邀请", 409, "invalid_assignment_state")
    else:
        raise BusinessError("该操作仅允许 chair 或 reviewer 角色", 403, "forbidden")
    conn.execute(
        "UPDATE assignments SET status='withdrawn',updated_at=? WHERE id=?",
        (utcnow(), assignment_id),
    )
    audit(conn, row["paper_id"], actor["id"], "assignment.withdraw",
          {"assignment_id": assignment_id, "previous_status": row["status"]})
    return conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
