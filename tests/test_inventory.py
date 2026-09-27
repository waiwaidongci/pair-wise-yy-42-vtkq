import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, StockShortageError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class AgentInventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _fire(self, ref):
        return self.service.create_item(
            {"title": f"fire-{ref}", "description": "fire scene",
             "severity": "high", "quantity": 1, "threshold": 1,
             "external_ref": ref}, "chief", "field_commander")

    def _agent(self, stock=100, ref="FOAM-1", name="泡沫灭火剂"):
        return self.service.create_agent(
            {"name": name, "unit": "升", "stock": stock,
             "external_ref": ref}, "keeper", "logistics")

    def test_replay_same_field_ticket_does_not_deduct_twice(self):
        fire = self._fire("F-1")
        agent = self._agent(stock=50)
        payload = {"agent_id": agent["id"], "field_ticket": "TICKET-1",
                   "quantity": 30}
        first = self.service.requisition(fire["id"], payload, "squad-a",
                                         "field_commander")
        self.assertFalse(first["replayed"])
        self.assertEqual(first["outstanding"], 30)
        # 小队离线后整批补传：同一份单沿用第一次结果
        replay = self.service.requisition(fire["id"], payload, "squad-a",
                                          "field_commander")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], first["id"])
        usage = {row["agent_id"]: row
                 for row in self.service.agent_usage("viewer")}
        self.assertEqual(usage[agent["id"]]["outstanding_total"], 30)
        self.assertEqual(usage[agent["id"]]["available"], 20)

    def test_two_fires_overuse_rejected_with_conflict_fires(self):
        fire1 = self._fire("F-2")
        fire2 = self._fire("F-3")
        agent = self._agent(stock=100)
        self.service.requisition(
            fire1["id"], {"agent_id": agent["id"], "field_ticket": "T-2",
                          "quantity": 80}, "squad-a", "field_commander")
        with self.assertRaises(StockShortageError) as caught:
            self.service.requisition(
                fire2["id"], {"agent_id": agent["id"], "field_ticket": "T-3",
                              "quantity": 50}, "squad-b", "field_commander")
        detail = caught.exception.detail
        self.assertEqual(detail["available"], 20)
        conflict_ids = [c["fire_id"] for c in detail["conflict_fires"]]
        self.assertEqual(conflict_ids, [fire1["id"]])
        self.assertIn(str(fire1["id"]), str(caught.exception))
        # 被退回的申请不产生领用单占用
        usage = {row["agent_id"]: row
                 for row in self.service.agent_usage("viewer")}
        self.assertEqual(usage[agent["id"]]["outstanding_total"], 80)

    def test_return_restores_margin_and_records_handler(self):
        fire = self._fire("F-4")
        agent = self._agent(stock=40)
        req = self.service.requisition(
            fire["id"], {"agent_id": agent["id"], "field_ticket": "T-4",
                         "quantity": 30}, "squad-a", "field_commander")
        record = self.service.return_agent(
            fire["id"], req["id"], {"quantity": 20, "handler": "经办人李四"},
            "squad-a", "logistics")
        self.assertEqual(record["handler"], "经办人李四")
        self.assertEqual(record["quantity"], 20)
        enriched = {row["agent_id"]: row
                    for row in self.service.agent_usage("viewer")}
        self.assertEqual(enriched[agent["id"]]["outstanding_total"], 10)
        self.assertEqual(enriched[agent["id"]]["available"], 30)
        # 余量恢复后，另一火场可以领用恢复出来的量
        fire2 = self._fire("F-5")
        again = self.service.requisition(
            fire2["id"], {"agent_id": agent["id"], "field_ticket": "T-5",
                          "quantity": 30}, "squad-b", "field_commander")
        self.assertEqual(again["outstanding"], 30)
        # 不能超过未归还量交回
        with self.assertRaises(ConflictError):
            self.service.return_agent(
                fire["id"], req["id"], {"quantity": 11}, "squad-a",
                "logistics")

    def test_close_blocked_until_unreturned_agents_restored(self):
        fire = self._fire("F-6")
        agent = self._agent(stock=100)
        req = self.service.requisition(
            fire["id"], {"agent_id": agent["id"], "field_ticket": "T-6",
                         "quantity": 40}, "squad-a", "field_commander")
        current = fire
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "ic",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                current["id"], "closed", current["version"], "ic",
                "incident_commander")
        message = str(caught.exception)
        self.assertIn("缺口40", message)
        self.assertIn("泡沫灭火剂", message)
        self.assertIn(str(req["id"]), message)
        self.service.return_agent(
            fire["id"], req["id"], {"quantity": 40}, "squad-a", "logistics")
        closed = self.service.transition(
            current["id"], "closed", current["version"], "ic",
            "incident_commander")
        self.assertEqual(closed["status"], "closed")

    def test_concurrent_requisitions_cannot_oversell(self):
        fire1 = self._fire("F-8")
        fire2 = self._fire("F-9")
        agent = self._agent(stock=100, ref="FOAM-2")
        errors = []

        def grab(fire_id, ticket):
            try:
                self.service.requisition(
                    fire_id, {"agent_id": agent["id"], "field_ticket": ticket,
                              "quantity": 80}, "squad", "field_commander")
            except StockShortageError as exc:
                errors.append(exc)

        t1 = threading.Thread(target=grab, args=(fire1["id"], "C-1"))
        t2 = threading.Thread(target=grab, args=(fire2["id"], "C-2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(errors), 1)
        usage = {row["agent_id"]: row
                 for row in self.service.agent_usage("viewer")}
        self.assertEqual(usage[agent["id"]]["outstanding_total"], 80)

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_agent(
                {"name": "x", "stock": 1}, "k", "field_commander")
        agent = self._agent(stock=5)
        fire = self._fire("F-7")
        with self.assertRaises(PermissionDenied):
            self.service.requisition(
                fire["id"], {"agent_id": agent["id"], "field_ticket": "T-7",
                             "quantity": 1}, "squad", "viewer")


if __name__ == "__main__":
    unittest.main()
