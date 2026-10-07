import unittest

from polar_station_foundation.acceptance import run, run_coldchain


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])

    def test_coldchain_acceptance(self):
        result = run_coldchain()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["plan_replayed"])
        self.assertTrue(result["ingest_replayed"])
        self.assertEqual(43, result["ingest_duplicated"])
        self.assertEqual({"version": 1, "recommended_outcome": "continue_use"},
                         result["assessment_v1"])
        self.assertEqual(2, result["assessment_v2"]["version"])
        self.assertEqual("destroy", result["assessment_v2"]["recommended_outcome"])
        self.assertEqual(2, result["assessment_v2"]["late_evidence"])
        self.assertEqual(1, result["assessment_v2"]["gaps"])
        self.assertEqual(1, result["assessment_v2"]["drift"])
        self.assertFalse(result["assessment_recompute_created"])
        self.assertEqual("disagreed", result["disagreement_status"])
        self.assertEqual("restrict_use", result["effective_outcome"])
        self.assertEqual(3, result["effective_version"])
        self.assertEqual("restrict_use", result["report_snapshot_outcome"])
        self.assertEqual(1, result["withdrawn_referenced_reports"])
        self.assertEqual(["notify"], result["outstanding_obligations"])
        self.assertEqual(3, result["decision_history"])


if __name__ == "__main__":
    unittest.main()
