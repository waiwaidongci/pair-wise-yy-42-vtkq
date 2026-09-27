import unittest
from src import rules
from src.domain import ConflictError, ValidationError
class RulesTest(unittest.TestCase):
    def test_priority_deadline_and_escalation(self):
        low=rules.priority_score(rules.SEVERITIES[0],1,10,0); high=rules.priority_score(rules.SEVERITIES[-1],30,10,3)
        self.assertGreater(high,low); self.assertLessEqual(rules.response_deadline_hours(rules.SEVERITIES[-1],30,10),rules.response_deadline_hours(rules.SEVERITIES[0],1,10))
        self.assertTrue(rules.escalation_required(rules.SEVERITIES[-1],1,10)); self.assertTrue(rules.escalation_required(rules.SEVERITIES[0],10,10))
    def test_transition_guards(self):
        self.assertTrue(rules.can_transition(rules.STATES[0],rules.STATES[1]))
        with self.assertRaises(ConflictError): rules.validate_transition(rules.STATES[0],rules.STATES[-1])
        with self.assertRaises(ValidationError): rules.priority_score("not-a-severity",1,1)
    def test_requisition_verdict_and_message(self):
        holdings=[{"item_id":1,"item_ref":"WF-A","outstanding":80.0}]
        self.assertIsNone(rules.requisition_verdict(100,holdings,2,20))
        verdict=rules.requisition_verdict(100,holdings,2,30)
        self.assertEqual(verdict["available"],20); self.assertEqual(verdict["shortfall"],10)
        self.assertEqual([c["item_ref"] for c in verdict["conflicts"]],["WF-A"])
        message=rules.requisition_conflict_message("阻燃剂A","升",verdict)
        self.assertIn("冲突火场",message); self.assertIn("WF-A",message)
        own=rules.requisition_verdict(100,holdings,1,30)
        self.assertEqual(own["conflicts"],[])
    def test_return_verdict_and_close_blockers(self):
        self.assertIsNone(rules.return_verdict(30,30))
        verdict=rules.return_verdict(30,40); self.assertEqual(verdict["excess"],10)
        outstanding=[{"name":"阻燃剂A","unit":"升","outstanding":25.0}]
        blockers=rules.completion_blockers(rules.STATES[-1],1,outstanding)
        self.assertEqual(len(blockers),2); self.assertIn("25升",blockers[1])
        self.assertEqual(rules.completion_blockers(rules.STATES[1],5,outstanding),[])
if __name__=="__main__": unittest.main()
