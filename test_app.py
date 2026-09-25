import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

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


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

    def test_backfill_orders_by_interest_and_skips_conflict(self):
        paper_id = self._paper()
        self.store.bid("r1", paper_id, "want")
        self.store.bid("r2", paper_id, "want")
        self.store.bid("r3", paper_id, "maybe")
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        run = self.store.run_backfill("chair", paper_id)
        self.assertEqual(run["result"], "filled")
        self.assertEqual(run["valid_after"], 2)
        entries = {e["reviewer_id"]: e for e in run["entries"]}
        self.assertEqual(entries["r1"]["outcome"], "skipped")
        self.assertEqual(entries["r1"]["reason"], "存在利益冲突")
        self.assertEqual(entries["r2"]["outcome"], "invited")
        self.assertEqual(entries["r3"]["outcome"], "invited")  # want 满员后才轮到 maybe
        statuses = {a["reviewer_id"]: a["status"] for a in self.store.invitation_overview("chair", paper_id)["assignments"]}
        self.assertEqual(statuses, {"r2": "invited", "r3": "invited"})

    def test_backfill_skips_capacity_and_continues(self):
        # r3 上限为 2，先用两篇论文占满。
        for _ in range(2):
            other = self._paper()
            assignment_id = self.store.assign("chair", other, "r3")["id"]
            self.store.respond_assignment("r3", assignment_id, True)
        paper_id = self._paper()
        self.store.bid("r3", paper_id, "want")
        self.store.bid("r2", paper_id, "maybe")
        run = self.store.run_backfill("chair", paper_id)
        entries = {e["reviewer_id"]: e for e in run["entries"]}
        self.assertEqual(entries["r3"]["outcome"], "skipped")
        self.assertEqual(entries["r3"]["reason"], "负载已满")
        self.assertEqual(entries["r2"]["outcome"], "invited")

    def test_decline_triggers_auto_backfill(self):
        paper_id = self._paper()
        for reviewer in ("r1", "r2", "r3"):
            self.store.bid(reviewer, paper_id, "want")
        run = self.store.run_backfill("chair", paper_id)
        a1 = next(e["assignment_id"] for e in run["entries"] if e["reviewer_id"] == "r1")
        result = self.store.respond_assignment("r1", a1, False)
        self.assertEqual(result["status"], "declined")
        backfill = result["backfill"]
        self.assertEqual(backfill["cause"], "declined")
        invited = [e["reviewer_id"] for e in backfill["entries"] if e["outcome"] == "invited"]
        self.assertEqual(invited, ["r3"])  # 自动补下一位
        overview = self.store.invitation_overview("chair", paper_id)
        self.assertEqual(overview["valid_count"], 2)

    def test_withdrawn_and_declined_free_load_and_allow_reinvite(self):
        # r3 上限为 2，先接受两篇占满负载。
        p1, p2 = self._paper(), self._paper()
        a1 = self.store.assign("chair", p1, "r3")["id"]
        a2 = self.store.assign("chair", p2, "r3")["id"]
        self.store.respond_assignment("r3", a1, True)
        self.store.respond_assignment("r3", a2, True)
        p3 = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", p3, "r3")
        self.assertEqual(ctx.exception.code, "reviewer_at_capacity")
        # 撤回不占负载，空闲后仍可再次受邀。
        result = self.store.withdraw_assignment("r3", a1)
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["backfill"]["cause"], "withdrawn")
        reinvited = self.store.assign("chair", p3, "r3")
        self.assertEqual(reinvited["status"], "invited")
        # 拒绝同样不占负载，可再次受邀（同一行重置）。
        declined = self.store.respond_assignment("r3", reinvited["id"], False)
        again = self.store.assign("chair", p3, "r3")
        self.assertEqual(again["id"], declined["id"])
        self.assertEqual(again["status"], "invited")

    def test_auto_backfill_skips_declined_but_reinvites_withdrawn(self):
        paper_id = self._paper()
        self.store.bid("r1", paper_id, "want")
        self.store.bid("r2", paper_id, "want")
        run = self.store.run_backfill("chair", paper_id)
        ids = {e["reviewer_id"]: e["assignment_id"] for e in run["entries"]}
        self.store.respond_assignment("r1", ids["r1"], False)
        self.store.respond_assignment("r2", ids["r2"], True)
        result = self.store.withdraw_assignment("r2", ids["r2"])
        entries = {e["reviewer_id"]: e for e in result["backfill"]["entries"]}
        self.assertEqual(entries["r1"]["outcome"], "skipped")
        self.assertIn("拒绝", entries["r1"]["reason"])
        self.assertEqual(entries["r2"]["outcome"], "invited")  # 撤回者自动再次受邀
        self.assertEqual(entries["r2"]["assignment_id"], ids["r2"])

    def test_chair_view_shows_reasons_and_requires_chair(self):
        paper_id = self._paper()
        self.store.bid("r1", paper_id, "want")
        self.store.bid("r2", paper_id, "maybe")
        self.store.add_conflict("chair", paper_id, "r2", "合作论文")
        self.store.run_backfill("chair", paper_id)
        view = self.store.invitation_overview("chair", paper_id)
        self.assertEqual(view["target_valid"], 2)
        self.assertEqual(view["valid_count"], 1)
        reasons = {c["reviewer_id"]: c["reason"] for c in view["candidates"]}
        self.assertEqual(reasons["r1"], "已有有效邀请")
        self.assertEqual(reasons["r2"], "存在利益冲突")
        self.assertTrue(view["backfill_runs"])
        self.assertEqual(view["backfill_runs"][0]["cause"], "manual")
        with self.assertRaises(BusinessError) as ctx:
            self.store.invitation_overview("r1", paper_id)
        self.assertEqual(ctx.exception.status, 403)


class HttpSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import ReviewServer

        cls.tmp = tempfile.TemporaryDirectory()
        store = ReviewStore(Path(cls.tmp.name) / "http.db")
        store.seed()
        cls.server = ReviewServer(("127.0.0.1", 0), store)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _api(self, method, path, user, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            method,
            path,
            body=json.dumps(body) if body is not None else None,
            headers={"X-User-Id": user, "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def test_backfill_and_withdraw_endpoints(self):
        status, paper = self._api("POST", "/api/papers", "alice", {
            "title": "面向边缘缓存的调度",
            "abstract": "本文研究边缘缓存场景下的任务调度问题，并给出理论分析与实验评估。",
        })
        self.assertEqual(status, 201)
        paper_id = paper["id"]
        self._api("POST", f"/api/papers/{paper_id}/bids", "r1", {"interest": "want"})
        self._api("POST", f"/api/papers/{paper_id}/bids", "r2", {"interest": "maybe"})
        status, run = self._api("POST", f"/api/papers/{paper_id}/backfill", "chair")
        self.assertEqual(status, 201)
        self.assertEqual(run["valid_after"], 2)
        # 拒绝触发自动补位，响应中带回补位结果。
        a1 = run["entries"][0]["assignment_id"]
        status, declined = self._api("POST", f"/api/assignments/{a1}/respond", "r1", {"accepted": False})
        self.assertEqual(status, 200)
        self.assertEqual(declined["backfill"]["cause"], "declined")
        # 撤回后自动再次受邀。
        a2 = run["entries"][1]["assignment_id"]
        self._api("POST", f"/api/assignments/{a2}/respond", "r2", {"accepted": True})
        status, withdrawn = self._api("POST", f"/api/assignments/{a2}/withdraw", "r2")
        self.assertEqual(status, 200)
        invited = [e["reviewer_id"] for e in withdrawn["backfill"]["entries"] if e["outcome"] == "invited"]
        self.assertEqual(invited, ["r2"])
        # 主席页面展示补位结果与原因；非主席拒绝访问。
        status, view = self._api("GET", f"/api/papers/{paper_id}/invitations", "chair")
        self.assertEqual(status, 200)
        self.assertEqual(view["target_valid"], 2)
        self.assertGreaterEqual(len(view["backfill_runs"]), 3)
        status, _ = self._api("GET", f"/api/papers/{paper_id}/invitations", "r1")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
