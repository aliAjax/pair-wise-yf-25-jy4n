import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app import ReviewServer
from app import ReviewStore
from common import BusinessError


def make_store() -> tuple[ReviewStore, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    store = ReviewStore(Path(tmp.name) / "test.db")
    store.seed()
    with store.connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO users(id,name,role,load_limit) VALUES(?,?,?,?)",
            [
                ("r4", "评审人四号", "reviewer", 3),
                ("r5", "评审人五号", "reviewer", 3),
            ],
        )
    return store, tmp


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp = make_store()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self, author="alice"):
        return self.store.submit_paper(
            author, "可靠分布式提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
        )["id"]

    def _fill(self, paper_id):
        return self.store.auto_fill("chair", paper_id)

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 8)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r2", paper_id)
        self.assertEqual(ctx.exception.status, 403)

    def test_auto_fill_follows_interest_order_with_reasons(self):
        paper_id = self._paper()
        # 遍历顺序：want → maybe → 无意向（按 id）。r1 冲突排在无意向段最先被看到；
        # r3 虽标 maybe 但负载已满被跳过；r4 明确不愿被跳过；r5 无意向但可用而入位。
        self.store.add_conflict("chair", paper_id, "r1", "同机构")
        self.store.bid("r2", paper_id, "want")
        self.store.bid("r3", paper_id, "maybe")
        self.store.bid("r4", paper_id, "decline")
        with self.store.connect() as conn:
            conn.execute("UPDATE users SET load_limit=1 WHERE id='r3'")
        other = self._paper(author="bob")
        self.store.assign("chair", other, "r3")  # r3 负载占满（1/1）。

        result = self._fill(paper_id)
        self.assertEqual([i["reviewer_id"] for i in result["invited"]], ["r2", "r5"])
        self.assertFalse(result["exhausted"])
        reasons = {s["reviewer_id"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(reasons["r3"], "skipped_at_capacity")
        self.assertEqual(reasons["r1"], "skipped_conflict")
        self.assertEqual(reasons["r4"], "skipped_declined_interest")

        overview = self.store.invitation_overview("chair", paper_id)
        self.assertEqual(overview["active_invitations"], 2)
        statuses = {a["reviewer_id"]: a["status"] for a in overview["assignments"]}
        self.assertEqual(statuses["r2"], "invited")
        self.assertEqual(statuses["r5"], "invited")
        event_types = [e["event_type"] for e in overview["events"]]
        self.assertNotIn("exhausted", event_types)
        # 再次补齐是无操作，不刷事件。
        before = len(overview["events"])
        again = self._fill(paper_id)
        self.assertEqual(again["invited"], [])
        self.assertEqual(len(self.store.invitation_overview("chair", paper_id)["events"]), before)

    def test_decline_triggers_backfill_and_frees_load(self):
        paper_id = self._paper()
        self.store.bid("r1", paper_id, "want")
        result = self._fill(paper_id)
        first = next(i for i in result["invited"] if i["reviewer_id"] == "r1")
        invited_ids = {i["reviewer_id"] for i in result["invited"]}
        self.assertIn("r1", invited_ids)

        response = self.store.respond_assignment("r1", first["assignment_id"], False)
        self.assertEqual(response["status"], "declined")
        # 拒绝后自动补位：r1 自己（已 declined）被跳过，后面的人补上来。
        bf = response["backfill"]
        self.assertEqual(len(bf["invited"]), 1)
        new_id = bf["invited"][0]["reviewer_id"]
        self.assertNotEqual(new_id, "r1")
        self.assertEqual(bf["invited"][0]["reason"], "after_decline")
        # 拒绝不占负载。
        load = self.store.invitation_overview("chair", paper_id)["reviewer_load"]
        r1 = next(x for x in load if x["reviewer_id"] == "r1")
        self.assertEqual(r1["current_load"], 0)
        # r1 仍有效邀请两份。
        self.assertEqual(self.store.invitation_overview("chair", paper_id)["active_invitations"], 2)
        # r1 释放后可以受邀于“另一篇”论文。
        other = self._paper(author="bob")
        self.store.assign("chair", other, "r1")

    def test_withdraw_invitation_and_acceptance_backfills(self):
        paper_id = self._paper()
        self.store.bid("r1", paper_id, "want")
        result = self._fill(paper_id)
        a1 = next(i for i in result["invited"] if i["reviewer_id"] == "r1")["assignment_id"]

        # 主席撤回待响应邀请 → 自动补位。
        out = self.store.withdraw_assignment("chair", a1)
        self.assertEqual(out["status"], "withdrawn")
        self.assertEqual(out["backfill"]["invited"][0]["reason"], "after_withdraw")
        self.assertEqual(self.store.invitation_overview("chair", paper_id)["active_invitations"], 2)

        # 评审人接受后再撤回 → 自动补位，撤回不占负载。
        invited = self.store.invitation_overview("chair", paper_id)["assignments"]
        pending = next(a for a in invited if a["status"] == "invited")
        self.store.respond_assignment(pending["reviewer_id"], pending["id"], True)
        out2 = self.store.withdraw_assignment(pending["reviewer_id"], pending["id"])
        self.assertEqual(out2["status"], "withdrawn")
        self.assertEqual(len(out2["backfill"]["invited"]), 1)

        # 状态机保护：已撤回不能再撤回；无关评审人不能撤回别人的邀请。
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_assignment("chair", pending["id"])
        self.assertEqual(ctx.exception.code, "invalid_assignment_state")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_assignment("r3", pending["id"])
        self.assertEqual(ctx.exception.status, 404)

    def test_pool_exhausted_when_everyone_unavailable(self):
        paper_id = self._paper()
        for rid in ("r1", "r2", "r3", "r4", "r5"):
            self.store.bid(rid, paper_id, "decline")
        result = self._fill(paper_id)
        self.assertTrue(result["exhausted"])
        self.assertEqual(result["active"], 0)
        events = self.store.invitation_overview("chair", paper_id)["events"]
        self.assertEqual(events[-1]["event_type"], "exhausted")
        self.assertEqual(events[-1]["reason_text"], "意向名单已遍历完，没有可邀请的人")
        # 有人改变主意为愿意后，补齐可以立即邀到。
        self.store.bid("r1", paper_id, "want")
        again = self._fill(paper_id)
        self.assertEqual([i["reviewer_id"] for i in again["invited"]], ["r1"])

    def test_freed_slot_can_reinvite_same_reviewer_on_another_paper(self):
        p1 = self._paper()
        self.store.bid("r1", p1, "want")
        a1 = self.store.assign("chair", p1, "r1")["id"]
        self.store.respond_assignment("r1", a1, False)  # r1 拒绝，负载释放。
        # p1 不会再把 r1 作为候选；但新论文可以再次邀请 r1。
        p2 = self._paper(author="bob")
        got = self.store.assign("chair", p2, "r1")
        self.assertEqual(got["status"], "invited")
        # 同一篇论文拒绝过的人不能被手动重复邀请。
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", p1, "r1")
        self.assertEqual(ctx.exception.code, "assignment_exists")

    def test_blind_access_revoked_after_decline_or_withdraw(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.store.respond_assignment("r1", a1, False)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r1", paper_id)
        self.assertEqual(ctx.exception.status, 403)
        self.assertNotIn(paper_id, [p["id"] for p in self.store.list_papers("r1")])

    def test_full_fill_then_score_and_decision(self):
        paper_id = self._paper()
        result = self._fill(paper_id)
        self.assertEqual(len(result["invited"]), 2)
        for inv in result["invited"]:
            self.store.respond_assignment(inv["reviewer_id"], inv["assignment_id"], True)
            self.store.submit_review(
                inv["reviewer_id"], inv["assignment_id"], 4, "工作扎实，建议补充更多实验数据。"
            )
        self.store.decide("chair", paper_id, "accept", "两份评审均正面。")
        # 已决定的论文不再触发补位。
        # （此处无待处理邀请，直接确认 overview 仍可读。）
        self.assertEqual(self.store.invitation_overview("chair", paper_id)["active_invitations"], 2)


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.tmp = make_store()
        cls.server = ReviewServer(("127.0.0.1", 0), cls.store)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def _req(self, method, path, user, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "X-User-Id": user},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_fill_respond_withdraw_http_flow(self):
        _, paper = self._req("POST", "/api/papers", "alice",
                             {"title": "HTTP 端到端论文", "abstract": "通过真实 HTTP 接口验证补位流程的端到端行为。"})
        paper_id = paper["id"]
        self._req("POST", f"/api/papers/{paper_id}/bids", "r1", {"interest": "want"})
        status, filled = self._req("POST", f"/api/papers/{paper_id}/assignments/fill", "chair", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(filled["invited"]), 2)

        status, overview = self._req("GET", f"/api/papers/{paper_id}/invitations", "chair")
        self.assertEqual(status, 200)
        self.assertEqual(overview["active_invitations"], 2)
        self.assertTrue(overview["events"])

        # 评审人视角无权访问补位总览。
        status, err = self._req("GET", f"/api/papers/{paper_id}/invitations", "r2")
        self.assertEqual(status, 403)

        a1 = next(i for i in filled["invited"] if i["reviewer_id"] == "r1")
        status, resp = self._req("POST", f"/api/assignments/{a1['assignment_id']}/respond",
                                 "r1", {"accepted": False})
        self.assertEqual(status, 200)
        self.assertIn("backfill", resp)
        self.assertEqual(resp["backfill"]["invited"][0]["reason"], "after_decline")


if __name__ == "__main__":
    unittest.main()
