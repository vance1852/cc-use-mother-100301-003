import unittest
from datetime import datetime, timedelta, timezone

from polar_station_foundation.coldchain.schema import COLDCHAIN_SCHEMA
from polar_station_foundation.coldchain.service import ColdChainService
from polar_station_foundation.clock import FixedClock
from polar_station_foundation.errors import ConflictError, PermissionDenied, ValidationError
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database


ORIGIN = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


def series(start_min, end_min, step, temp_c, naive_plus_hours=None):
    """生成读数序列；naive_plus_hours 给出时生成记录器本地裸时间。"""

    readings = []
    for minute in range(start_min, end_min + 1, step):
        instant = ORIGIN + timedelta(minutes=minute)
        if naive_plus_hours is None:
            observed = instant.isoformat(timespec="seconds").replace("+00:00", "Z")
        else:
            observed = (instant + timedelta(hours=naive_plus_hours)).replace(
                tzinfo=None).isoformat(timespec="seconds")
        value = temp_c(minute) if callable(temp_c) else temp_c
        readings.append({"observed_at": observed, "temp_c": value})
    return readings


def plan_config(**overrides):
    config = {
        "batch_id": "B1",
        "samples": [
            {"sample_id": "S1", "batch_id": "B1"},
            {"sample_id": "S2", "batch_id": "B2"},
        ],
        "packages": [
            {"package_id": "P1", "logger_ids": ["L1", "L2"], "sample_ids": ["S1", "S2"]},
        ],
        "segments": [
            {"segment_id": "seg-1", "start": "2026-09-20T08:00:00Z",
             "end": "2026-09-20T12:00:00Z"},
            {"segment_id": "seg-2", "start": "2026-09-20T12:00:00Z",
             "end": "2026-09-20T18:00:00Z"},
        ],
        "zones": [
            {"name": "frozen", "min_c": -90.0, "max_c": -60.0},
            {"name": "excursion", "min_c": -60.0, "max_c": -20.0},
        ],
        "budget": {"threshold_c": -60.0, "max_minutes": 30.0, "max_degree_minutes": 100.0},
        "sensors": [
            {"logger_id": "L1", "timezone_name": "UTC+08:00", "offset_c": 0.5,
             "calibrated_at": "2026-09-19T00:00:00Z", "valid_from": "2026-09-19T00:00:00Z",
             "valid_to": "2026-09-21T00:00:00Z"},
            {"logger_id": "L2", "timezone_name": "UTC", "offset_c": 0.0,
             "calibrated_at": "2026-09-19T00:00:00Z", "valid_from": "2026-09-19T00:00:00Z",
             "valid_to": "2026-09-21T00:00:00Z"},
        ],
        "rules": {
            "reading_ttl_minutes": 15.0,
            "gap_tolerance_minutes": 20.0,
            "drift_tolerance_c": 3.0,
            "late_after_minutes": 60.0,
            "disposition_policy": {
                "within_budget": ["continue"],
                "exceeded": ["restrict", "destroy"],
                "insufficient_evidence": ["restrict", "destroy"],
            },
        },
    }
    config.update(overrides)
    return config


class ColdChainTest(unittest.TestCase):
    def setUp(self):
        self.database = Database(extra_schema=COLDCHAIN_SCHEMA)
        self.clock = FixedClock(datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, self.clock)
        self.service = ColdChainService(self.database, self.clock)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="科考机构")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap",
                                       new_actor_id="a1", display_name="管理员",
                                       role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="operator", actor_id="a1",
                                       new_actor_id="op1", display_name="操作员",
                                       role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="research", actor_id="a1",
                                       new_actor_id="res1", display_name="科研负责人",
                                       role="researcher", organization_id="o1")
        self.foundation.register_actor(request_id="quality", actor_id="a1",
                                       new_actor_id="qa1", display_name="质量负责人",
                                       role="quality", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="op1", site_id="s1",
                                      organization_id="o1", name="科考站",
                                      timezone_name="Asia/Shanghai")
        self.plan_seq = 0

    def tearDown(self):
        self.database.close()

    def set_clock(self, value):
        self.clock = FixedClock(value)
        self.service.clock = self.clock
        self.foundation.clock = self.clock

    def create_plan(self, **overrides):
        self.plan_seq += 1
        plan_id = f"plan-{self.plan_seq}"
        self.service.create_plan(request_id=f"req-plan-{self.plan_seq}", actor_id="op1",
                                 plan_id=plan_id, site_id="s1", config=plan_config(**overrides))
        return plan_id

    def upload_baseline(self, plan_id, request_prefix="base"):
        """上传 L2 全程合规基线（-78°C，覆盖两个分段）。"""

        return self.service.record_readings(
            request_id=request_prefix, actor_id="op1", plan_id=plan_id, package_id="P1",
            logger_id="L2", readings=series(0, 600, 10, -78.0))

    def test_plan_is_locked_at_creation(self):
        plan_id = self.create_plan()
        plan = self.service.get_plan(plan_id)
        self.assertEqual("locked", plan.status)
        self.assertEqual("B1", plan.batch_id)
        self.assertEqual("2026-09-20T08:00:00Z", plan.config["segments"][0]["start"])
        with self.assertRaises(ConflictError):
            self.service.create_plan(request_id="req-plan-dup", actor_id="op1",
                                     plan_id=plan_id, site_id="s1", config=plan_config())

    def test_plan_validation_rejects_bad_config(self):
        with self.assertRaises(ValidationError):
            self.create_plan(packages=[{"package_id": "P1", "logger_ids": ["L1"],
                                        "sample_ids": ["S1", "SX"]}])
        with self.assertRaises(ValidationError):
            self.create_plan(segments=[
                {"segment_id": "seg-1", "start": "2026-09-20T08:00:00Z",
                 "end": "2026-09-20T12:00:00Z"},
                {"segment_id": "seg-2", "start": "2026-09-20T11:00:00Z",
                 "end": "2026-09-20T18:00:00Z"},
            ])
        with self.assertRaises(ValidationError):
            self.create_plan(budget={"threshold_c": -55.0, "max_minutes": 30.0,
                                     "max_degree_minutes": 100.0})
        with self.assertRaises(ValidationError):
            bad_rules = plan_config()["rules"]
            bad_rules["disposition_policy"] = {"within_budget": ["continue"]}
            self.create_plan(rules=bad_rules)

    def test_readings_normalized_across_timezones_and_calibration(self):
        plan_id = self.create_plan()
        self.service.record_readings(
            request_id="tz-1", actor_id="op1", plan_id=plan_id, package_id="P1",
            logger_id="L1", readings=[{"observed_at": "2026-09-20T16:00:00", "temp_c": -78.5}])
        row = self.database.connection.execute(
            "SELECT * FROM cc_readings WHERE logger_id='L1'").fetchone()
        self.assertEqual("2026-09-20T08:00:00Z", row["observed_at"])
        self.assertEqual(-78.0, row["corrected_temp_c"])
        self.assertEqual("2026-09-20T16:00:00", row["raw_observed_at"])

    def test_duplicate_data_is_idempotent_and_conflict_rejected(self):
        plan_id = self.create_plan()
        readings = series(0, 60, 10, -78.0, naive_plus_hours=8.0)
        self.service.record_readings(request_id="up-1", actor_id="op1", plan_id=plan_id,
                                     package_id="P1", logger_id="L1", readings=readings)
        again = self.service.record_readings(request_id="up-2", actor_id="op1", plan_id=plan_id,
                                             package_id="P1", logger_id="L1", readings=readings)
        self.assertFalse(again.replayed)
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM cc_readings").fetchone()["count"]
        self.assertEqual(7, count)
        replay = self.service.record_readings(request_id="up-2", actor_id="op1", plan_id=plan_id,
                                              package_id="P1", logger_id="L1", readings=readings)
        self.assertTrue(replay.replayed)
        conflict = [{"observed_at": "2026-09-20T16:00:00", "temp_c": -50.0}]
        with self.assertRaises(ConflictError):
            self.service.record_readings(request_id="up-3", actor_id="op1", plan_id=plan_id,
                                         package_id="P1", logger_id="L1", readings=conflict)

    def test_gap_yields_insufficient_evidence(self):
        plan_id = self.create_plan()
        self.service.record_readings(request_id="g-1", actor_id="op1", plan_id=plan_id,
                                     package_id="P1", logger_id="L1",
                                     readings=series(0, 60, 10, -78.0, naive_plus_hours=8.0))
        self.service.record_readings(request_id="g-2", actor_id="op1", plan_id=plan_id,
                                     package_id="P1", logger_id="L2",
                                     readings=series(150, 600, 10, -78.0))
        self.service.compute_assessment(request_id="g-3", actor_id="qa1", plan_id=plan_id)
        view = self.service.get_assessment(plan_id, 1)
        self.assertEqual("insufficient_evidence", view.outcome)
        gaps = view.findings["packages"]["P1"]["gaps"]
        self.assertEqual(1, len(gaps))
        self.assertEqual("seg-1", gaps[0]["segment_id"])
        self.assertTrue(gaps[0]["exceeds_tolerance"])
        self.assertEqual(75.0, gaps[0]["duration_minutes"])

    def test_excursion_consumes_budget_per_segment(self):
        plan_id = self.create_plan()
        self.upload_baseline(plan_id)
        self.service.record_readings(request_id="e-1", actor_id="op1", plan_id=plan_id,
                                     package_id="P1", logger_id="L1",
                                     readings=series(60, 100, 10, -50.0, naive_plus_hours=8.0))
        self.service.compute_assessment(request_id="e-2", actor_id="qa1", plan_id=plan_id)
        view = self.service.get_assessment(plan_id, 1)
        self.assertEqual("exceeded", view.outcome)
        package = view.findings["packages"]["P1"]
        self.assertEqual(1, len(package["excursions"]))
        excursion = package["excursions"][0]
        self.assertEqual("seg-1", excursion["segment_id"])
        self.assertEqual(55.0, excursion["duration_minutes"])
        self.assertEqual(-25.0, excursion["remaining_minutes_after"])
        self.assertEqual("excursion", excursion["zone"])
        self.assertTrue(package["consumption"]["exceeded"])

    def test_sensor_drift_and_calibration_expiry_are_findings(self):
        sensors = plan_config()["sensors"]
        sensors[1]["valid_to"] = "2026-09-20T10:00:00Z"
        plan_id = self.create_plan(sensors=sensors)
        self.service.record_readings(request_id="d-1", actor_id="op1", plan_id=plan_id,
                                     package_id="P1", logger_id="L1",
                                     readings=series(0, 120, 10, -73.0, naive_plus_hours=8.0))
        self.service.record_readings(request_id="d-2", actor_id="op1", plan_id=plan_id,
                                     package_id="P1", logger_id="L2",
                                     readings=series(0, 600, 10, -78.0))
        self.service.compute_assessment(request_id="d-3", actor_id="qa1", plan_id=plan_id)
        view = self.service.get_assessment(plan_id, 1)
        self.assertEqual(1, len(view.findings["drift"]))
        drift = view.findings["drift"][0]
        self.assertEqual(["L1", "L2"], drift["loggers"])
        self.assertEqual(["S1", "S2"], drift["affected_sample_ids"])
        self.assertEqual(1, len(view.findings["calibration"]))
        self.assertEqual("L2", view.findings["calibration"][0]["logger_id"])

    def test_late_evidence_forces_new_judgment_version(self):
        plan_id = self.create_plan()
        self.upload_baseline(plan_id)
        self.service.compute_assessment(request_id="l-1", actor_id="qa1", plan_id=plan_id)
        self.assertEqual("within_budget", self.service.get_assessment(plan_id, 1).outcome)
        self.set_clock(datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc))
        late = self.service.record_readings(
            request_id="l-2", actor_id="op1", plan_id=plan_id, package_id="P1", logger_id="L1",
            readings=series(60, 100, 10, -50.0, naive_plus_hours=8.0))
        self.assertFalse(late.replayed)
        record = self.service.disposition_record(plan_id)
        self.assertTrue(record["evidence_stale"])
        self.service.compute_assessment(request_id="l-3", actor_id="qa1", plan_id=plan_id)
        v2 = self.service.get_assessment(plan_id, 2)
        self.assertEqual("exceeded", v2.outcome)
        self.assertEqual(1, len(v2.findings["late_evidence"]))
        self.assertEqual("L1", v2.findings["late_evidence"][0]["logger_id"])
        with self.assertRaises(ConflictError):
            self.service.approve_disposition(request_id="l-4", actor_id="res1", plan_id=plan_id,
                                             assessment_id=self.service.get_assessment(plan_id, 1).assessment_id,
                                             decision="restrict", rationale="旧版本")
        self.assertEqual(1, self.service.get_assessment(plan_id, 1).version_no)

    def test_assessment_is_content_idempotent(self):
        plan_id = self.create_plan()
        self.upload_baseline(plan_id)
        self.service.compute_assessment(request_id="c-1", actor_id="qa1", plan_id=plan_id)
        self.service.compute_assessment(request_id="c-2", actor_id="qa1", plan_id=plan_id)
        self.assertEqual(1, len(self.service.list_assessments(plan_id)))

    def test_compute_assessment_requires_evidence(self):
        plan_id = self.create_plan()
        with self.assertRaises(ValidationError):
            self.service.compute_assessment(request_id="c-0", actor_id="qa1", plan_id=plan_id)

    def test_dual_role_approval_produces_single_effective_version(self):
        plan_id = self.create_plan()
        self.upload_baseline(plan_id)
        self.service.compute_assessment(request_id="a-1", actor_id="qa1", plan_id=plan_id)
        assessment_id = self.service.get_assessment(plan_id, 1).assessment_id
        with self.assertRaises(PermissionDenied):
            self.service.approve_disposition(request_id="a-2", actor_id="op1", plan_id=plan_id,
                                             assessment_id=assessment_id, decision="continue",
                                             rationale="操作员不能审批")
        with self.assertRaises(ValidationError):
            self.service.approve_disposition(request_id="a-3", actor_id="res1", plan_id=plan_id,
                                             assessment_id=assessment_id, decision="destroy",
                                             rationale="预算内不允许销毁")
        first = self.service.approve_disposition(request_id="a-4", actor_id="res1",
                                                 plan_id=plan_id, assessment_id=assessment_id,
                                                 decision="continue", rationale="科研同意继续")
        self.assertFalse(first.replayed)
        record = self.service.disposition_record(plan_id)
        self.assertIsNone(record["current_disposition"])
        same = self.service.approve_disposition(request_id="a-5", actor_id="res1",
                                                plan_id=plan_id, assessment_id=assessment_id,
                                                decision="continue", rationale="科研同意继续")
        self.assertFalse(same.replayed)
        self.service.approve_disposition(request_id="a-7", actor_id="qa1", plan_id=plan_id,
                                         assessment_id=assessment_id, decision="continue",
                                         rationale="质量同意继续")
        record = self.service.disposition_record(plan_id)
        self.assertEqual("continue", record["current_disposition"]["outcome"])
        state_rows = self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM cc_disposition_state WHERE plan_id=?",
            (plan_id,)).fetchone()["count"]
        self.assertEqual(1, state_rows)

    def test_same_role_cannot_change_decision_on_same_version(self):
        plan_id = self.create_plan()
        self.upload_baseline(plan_id)
        self.service.record_readings(request_id="x-1", actor_id="op1", plan_id=plan_id,
                                     package_id="P1", logger_id="L1",
                                     readings=series(60, 100, 10, -50.0, naive_plus_hours=8.0))
        self.service.compute_assessment(request_id="x-2", actor_id="qa1", plan_id=plan_id)
        assessment_id = self.service.get_assessment(plan_id, 1).assessment_id
        self.service.approve_disposition(request_id="x-3", actor_id="res1", plan_id=plan_id,
                                         assessment_id=assessment_id, decision="restrict",
                                         rationale="科研限制")
        with self.assertRaises(ConflictError):
            self.service.approve_disposition(request_id="x-4", actor_id="res1", plan_id=plan_id,
                                             assessment_id=assessment_id, decision="destroy",
                                             rationale="改变主意")

    def test_stricter_decision_wins_and_history_is_append_only(self):
        plan_id = self.create_plan()
        self.upload_baseline(plan_id)
        self.service.compute_assessment(request_id="s-1", actor_id="qa1", plan_id=plan_id)
        v1 = self.service.get_assessment(plan_id, 1)
        self.service.approve_disposition(request_id="s-2", actor_id="res1", plan_id=plan_id,
                                         assessment_id=v1.assessment_id, decision="continue",
                                         rationale="科研继续")
        self.service.approve_disposition(request_id="s-3", actor_id="qa1", plan_id=plan_id,
                                         assessment_id=v1.assessment_id, decision="continue",
                                         rationale="质量继续")
        self.set_clock(datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc))
        self.service.record_readings(request_id="s-4", actor_id="op1", plan_id=plan_id,
                                     package_id="P1", logger_id="L1",
                                     readings=series(60, 100, 10, -50.0, naive_plus_hours=8.0))
        self.service.compute_assessment(request_id="s-5", actor_id="qa1", plan_id=plan_id)
        v2 = self.service.get_assessment(plan_id, 2)
        self.service.approve_disposition(request_id="s-6", actor_id="res1", plan_id=plan_id,
                                         assessment_id=v2.assessment_id, decision="restrict",
                                         rationale="科研限制")
        self.service.approve_disposition(request_id="s-7", actor_id="qa1", plan_id=plan_id,
                                         assessment_id=v2.assessment_id, decision="destroy",
                                         rationale="质量销毁")
        record = self.service.disposition_record(plan_id)
        self.assertEqual("destroy", record["current_disposition"]["outcome"])
        self.assertEqual(2, len(record["disposition_history"]))
        self.assertEqual("continue", record["disposition_history"][0]["outcome"])
        self.assertFalse(record["disposition_history"][0]["is_current"])
        self.assertTrue(record["disposition_history"][1]["is_current"])

    def test_withdrawal_keeps_cited_decision_and_tracks_obligations(self):
        plan_id = self.create_plan()
        self.upload_baseline(plan_id)
        self.service.compute_assessment(request_id="w-1", actor_id="qa1", plan_id=plan_id)
        v1 = self.service.get_assessment(plan_id, 1)
        self.service.approve_disposition(request_id="w-2", actor_id="res1", plan_id=plan_id,
                                         assessment_id=v1.assessment_id, decision="continue",
                                         rationale="科研继续")
        self.service.approve_disposition(request_id="w-3", actor_id="qa1", plan_id=plan_id,
                                         assessment_id=v1.assessment_id, decision="continue",
                                         rationale="质量继续")
        self.service.create_report(request_id="w-4", actor_id="qa1", plan_id=plan_id,
                                   title="偏差月报")
        before = self.database.connection.execute(
            "SELECT * FROM cc_dispositions WHERE plan_id=?", (plan_id,)).fetchone()
        before_snapshot = dict(before)
        with self.assertRaises(PermissionDenied):
            self.service.withdraw_disposition(request_id="w-5", actor_id="res1",
                                              plan_id=plan_id, reason="科研不能撤回")
        self.service.withdraw_disposition(request_id="w-6", actor_id="qa1", plan_id=plan_id,
                                          reason="迟到证据显示超预算")
        record = self.service.disposition_record(plan_id)
        self.assertIsNone(record["current_disposition"])
        history = record["disposition_history"][0]
        self.assertEqual("continue", history["outcome"])
        self.assertIsNotNone(history["withdrawal"])
        self.assertEqual(1, len(history["cited_by"]))
        after = self.database.connection.execute(
            "SELECT * FROM cc_dispositions WHERE plan_id=?", (plan_id,)).fetchone()
        self.assertEqual(before_snapshot, dict(after))
        obligations = record["outstanding_obligations"]
        self.assertEqual(4, len(obligations))
        kinds = sorted(item["kind"] for item in obligations)
        self.assertEqual(["isolation", "isolation", "notification", "notification"], kinds)
        target = obligations[0]
        self.service.complete_obligation(request_id="w-7", actor_id="op1",
                                         obligation_id=target["obligation_id"], note="已隔离")
        again = self.service.complete_obligation(request_id="w-8", actor_id="op1",
                                                 obligation_id=target["obligation_id"],
                                                 note="重复登记")
        self.assertFalse(again.replayed)
        self.assertEqual(3, len(self.service.disposition_record(plan_id)["outstanding_obligations"]))
        with self.assertRaises(ConflictError):
            self.service.withdraw_disposition(request_id="w-9", actor_id="qa1",
                                              plan_id=plan_id, reason="重复撤回")

    def test_shared_packaging_impact_lists_all_samples(self):
        plan_id = self.create_plan()
        self.upload_baseline(plan_id)
        self.service.record_readings(request_id="p-1", actor_id="op1", plan_id=plan_id,
                                     package_id="P1", logger_id="L1",
                                     readings=series(60, 100, 10, -50.0, naive_plus_hours=8.0))
        self.service.compute_assessment(request_id="p-2", actor_id="qa1", plan_id=plan_id)
        record = self.service.disposition_record(plan_id)
        shared = record["shared_packaging_impact"]
        self.assertEqual(1, len(shared))
        self.assertEqual("P1", shared[0]["package_id"])
        self.assertEqual(["B1", "B2"], shared[0]["batch_ids"])
        self.assertTrue(shared[0]["affected"])
        self.assertEqual(["S1", "S2"], record["affected_sample_ids"])

    def test_researcher_cannot_upload_readings(self):
        plan_id = self.create_plan()
        with self.assertRaises(PermissionDenied):
            self.service.record_readings(request_id="r-1", actor_id="res1", plan_id=plan_id,
                                         package_id="P1", logger_id="L2",
                                         readings=series(0, 10, 10, -78.0))


if __name__ == "__main__":
    unittest.main()
