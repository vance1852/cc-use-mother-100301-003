import unittest

from polar_station_foundation.coldchain.acceptance import run


class ColdChainAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["plan_locked"])
        self.assertTrue(result["upload_l1_replayed"])
        self.assertEqual(42, result["upload_l1_duplicates"])
        self.assertEqual("insufficient_evidence", result["assessment_v1_outcome"])
        self.assertEqual("exceeded", result["assessment_v2_outcome"])
        self.assertTrue(result["late_upload_flagged"])
        self.assertEqual(1, result["late_evidence_findings"])
        self.assertGreaterEqual(result["drift_findings"], 1)
        self.assertEqual(1, result["calibration_findings"])
        self.assertTrue(result["stale_evidence_detected"])
        self.assertEqual("ConflictError", result["stale_approval_rejected"])
        self.assertEqual("restrict", result["disposition_v1_outcome"])
        self.assertTrue(result["disposition_v1_cited"])
        self.assertTrue(result["disposition_v1_withdrawn"])
        self.assertEqual("destroy", result["current_outcome"])
        self.assertEqual(["layover"], result["excursion_segments"])
        self.assertGreater(result["budget_minutes_consumed"], 30.0)
        self.assertTrue(result["shared_package_affected"])
        self.assertEqual(["S1", "S2", "S3"], result["affected_samples"])
        self.assertEqual(6, result["obligations_created"])
        self.assertEqual(5, result["obligations_outstanding"])


if __name__ == "__main__":
    unittest.main()
