import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service


class RequisitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.chemical = self.service.create_chemical(
            {"name": "阻燃剂A", "unit": "升", "total_stock": 100},
            "storekeeper", "logistics")
        self.scene_a = self.service.create_item(
            {"title": "东坡火场", "description": "东侧火线", "severity": "high",
             "quantity": 12, "threshold": 6, "external_ref": "WF-A"},
            "creator", "field_commander")
        self.scene_b = self.service.create_item(
            {"title": "西坡火场", "description": "西侧火线", "severity": "high",
             "quantity": 9, "threshold": 6, "external_ref": "WF-B"},
            "creator", "field_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _requisition(self, scene, quantity, field_ref):
        return self.service.create_requisition(
            scene["id"], {"chemical_id": self.chemical["id"], "quantity": quantity,
                          "field_ref": field_ref},
            "squad-1", "logistics")

    def _available(self):
        chemicals = self.service.list_chemicals("viewer")
        return chemicals[0]["available"]

    def test_replay_reuses_first_result(self):
        first = self._requisition(self.scene_a, 40, "FR-1")
        self.assertFalse(first["replayed"])
        self.assertEqual(self._available(), 60)
        replay = self._requisition(self.scene_a, 40, "FR-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(self._available(), 60)

    def test_overdraw_rejected_with_conflicting_scene(self):
        self._requisition(self.scene_a, 80, "FR-A")
        with self.assertRaises(ConflictError) as ctx:
            self._requisition(self.scene_b, 30, "FR-B")
        message = str(ctx.exception)
        self.assertIn("余量不足", message)
        self.assertIn("WF-A", message)
        self.assertIn("80升", message)
        self.assertEqual(self._available(), 20)

    def test_return_records_handler_and_restores_balance(self):
        requisition = self._requisition(self.scene_a, 80, "FR-A")
        returned = self.service.add_return(
            requisition["id"], {"quantity": 50, "handler": "张师傅"},
            "storekeeper", "logistics")
        self.assertEqual(returned["handler"], "张师傅")
        self.assertEqual(returned["requisition"]["outstanding"], 30)
        self.assertEqual(self._available(), 70)
        self._requisition(self.scene_b, 30, "FR-B")
        self.assertEqual(self._available(), 40)
        with self.assertRaises(ConflictError) as ctx:
            self.service.add_return(
                requisition["id"], {"quantity": 40, "handler": "张师傅"},
                "storekeeper", "logistics")
        self.assertIn("超过未归还", str(ctx.exception))

    def test_close_blocked_until_chemicals_returned(self):
        requisition = self._requisition(self.scene_a, 25, "FR-A")
        current = self.service.get_item(self.scene_a["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        message = str(ctx.exception)
        self.assertIn("阻燃剂A", message)
        self.assertIn("25升", message)
        self.service.add_return(requisition["id"], {"quantity": 25, "handler": "张师傅"},
                                "storekeeper", "logistics")
        current = self.service.get_item(self.scene_a["id"], "viewer")
        closed = self.service.transition(current["id"], STATES[-1], current["version"],
                                         "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"], STATES[-1])

    def test_concurrent_scenes_cannot_overdraw(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def take(scene, ref):
            barrier.wait()
            try:
                self._requisition(scene, 70, ref)
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=take, args=(self.scene_a, "FR-A")),
                   threading.Thread(target=take, args=(self.scene_b, "FR-B"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["ok", "rejected"])
        self.assertEqual(self._available(), 30)

    def test_validation_and_permission(self):
        with self.assertRaises(ValidationError):
            self.service.create_requisition(
                self.scene_a["id"], {"chemical_id": self.chemical["id"],
                                     "quantity": 10},
                "squad-1", "logistics")
        with self.assertRaises(ValidationError):
            self._requisition(self.scene_a, 0, "FR-Z")
        with self.assertRaises(PermissionDenied):
            self._requisition_as_viewer()
        with self.assertRaises(PermissionDenied):
            self.service.create_chemical(
                {"name": "阻燃剂B", "unit": "升", "total_stock": 10},
                "intruder", "viewer")

    def _requisition_as_viewer(self):
        return self.service.create_requisition(
            self.scene_a["id"], {"chemical_id": self.chemical["id"], "quantity": 1,
                                 "field_ref": "FR-V"},
            "squad-1", "viewer")

    def test_chemical_summary_aggregates_by_scene(self):
        self._requisition(self.scene_a, 40, "FR-A")
        self._requisition(self.scene_b, 25, "FR-B")
        summary = self.service.list_chemicals("viewer")[0]
        self.assertEqual(summary["checked_out"], 65)
        self.assertEqual(summary["available"], 35)
        scenes = {entry["item_ref"]: entry["outstanding"] for entry in summary["scenes"]}
        self.assertEqual(scenes, {"WF-A": 40, "WF-B": 25})


if __name__ == "__main__":
    unittest.main()
