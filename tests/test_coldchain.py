import unittest
from datetime import datetime, timezone

from polar_station_foundation.api import route
from polar_station_foundation.clock import MutableClock
from polar_station_foundation.coldchain import ColdChainService
from polar_station_foundation.errors import ConflictError, PermissionDenied, ValidationError
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database


class ColdChainTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, self.clock)
        self.service = ColdChainService(self.foundation)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="科考机构一")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                       display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                       display_name="操作员", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="researcher", actor_id="a1", new_actor_id="res1",
                                       display_name="科研负责人", role="researcher", organization_id="o1")
        self.foundation.register_actor(request_id="quality", actor_id="a1", new_actor_id="qua1",
                                       display_name="质量负责人", role="quality", organization_id="o1")
        self.foundation.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                       display_name="审计员", role="auditor", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="op1", site_id="s1",
                                      organization_id="o1", name="科考站点", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _plan(self, **overrides):
        payload = {
            "request_id": "plan-1", "actor_id": "op1", "site_id": "s1", "batch_id": "b1",
            "sample_ids": ["s-1", "s-2", "s-3"],
            "packages": [
                {"package_id": "p1", "sample_ids": ["s-1", "s-2"], "logger_ids": ["L1"]},
                {"package_id": "p2", "sample_ids": ["s-3"], "logger_ids": ["L2"]},
            ],
            "segments": [
                {"name": "干线", "start": "2026-10-01T08:00:00Z", "end": "2026-10-01T12:00:00Z"},
                {"name": "配送", "start": "2026-10-01T12:00:00Z", "end": "2026-10-01T16:00:00Z"},
            ],
            "zones": [
                {"name": "frozen", "lower": None, "upper": -70.0},
                {"name": "excursion", "lower": -70.0, "upper": -60.0, "budget_minutes": 60},
                {"name": "critical", "lower": -60.0, "upper": None, "budget_minutes": 10},
            ],
            "loggers": [
                {"logger_id": "L1", "timezone_name": "UTC", "calibration_offset": 0.5,
                 "calibration_valid_from": "2026-10-01T00:00:00Z",
                 "calibration_valid_to": "2026-10-01T23:59:00Z"},
                {"logger_id": "L2", "timezone_name": "Asia/Shanghai", "calibration_offset": -0.5,
                 "calibration_valid_from": "2026-10-01T00:00:00Z",
                 "calibration_valid_to": "2026-10-01T23:59:00Z"},
            ],
            "max_hold_minutes": 45,
            "rules": [
                {"when": "over_budget", "zone": "excursion", "outcome": "restrict_use"},
                {"when": "over_budget", "zone": "critical", "outcome": "destroy"},
                {"when": "gap_overlaps_unfrozen", "outcome": "restrict_use"},
            ],
        }
        payload.update(overrides)
        return payload

    def _lock(self, **overrides):
        return self.service.lock_plan(**self._plan(**overrides))

    def _ingest(self, readings, request_id="ingest-1"):
        return self.service.ingest_readings(request_id=request_id, actor_id="op1",
                                            site_id="s1", batch_id="b1", readings=readings)

    def _assess(self, request_id="assess-1"):
        return self.service.compute_assessment(request_id=request_id, actor_id="op1",
                                               site_id="s1", batch_id="b1")

    def _decide(self, actor_id, outcome, request_id, rationale="理由"):
        return self.service.submit_determination(request_id=request_id, actor_id=actor_id,
                                                 site_id="s1", batch_id="b1",
                                                 outcome=outcome, rationale=rationale)

    # ---- 发运方案 ----

    def test_lock_plan_is_idempotent_and_immutable(self):
        first = self._lock()
        replay = self._lock()
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        changed = self._plan(request_id="plan-2", max_hold_minutes=60)
        with self.assertRaises(ConflictError):
            self.service.lock_plan(**changed)

    def test_lock_plan_rejects_discontinuous_zones(self):
        zones = [
            {"name": "frozen", "lower": None, "upper": -70.0},
            {"name": "critical", "lower": -60.0, "upper": None, "budget_minutes": 10},
        ]
        with self.assertRaises(ValidationError):
            self._lock(zones=zones)

    def test_lock_plan_rejects_sample_in_two_packages(self):
        packages = [
            {"package_id": "p1", "sample_ids": ["s-1", "s-2"], "logger_ids": ["L1"]},
            {"package_id": "p2", "sample_ids": ["s-2", "s-3"], "logger_ids": ["L2"]},
        ]
        with self.assertRaises(ValidationError):
            self._lock(packages=packages)

    def test_lock_plan_rejects_unknown_timezone(self):
        loggers = [
            {"logger_id": "L1", "timezone_name": "Mars/Olympus", "calibration_offset": 0.0,
             "calibration_valid_from": "2026-10-01T00:00:00Z",
             "calibration_valid_to": "2026-10-01T23:59:00Z"},
        ]
        with self.assertRaises(ValidationError):
            self._lock(loggers=loggers)

    # ---- 读数归集 ----

    def test_ingest_normalizes_timezone_and_applies_calibration(self):
        self._lock()
        # L2 使用 Asia/Shanghai 本地时间，18:00 CST 等于 10:00 UTC
        self._ingest([
            {"logger_id": "L2", "observed_at": "2026-10-01T18:00:00", "temperature": -80.0},
        ])
        row = self.database.connection.execute(
            "SELECT observed_at, raw_temperature, corrected_temperature FROM coldchain_readings"
        ).fetchone()
        self.assertEqual("2026-10-01T10:00:00+00:00".replace("+00:00", "Z"), row["observed_at"])
        self.assertEqual(-80.0, row["raw_temperature"])
        self.assertEqual(-80.5, row["corrected_temperature"])

    def test_ingest_duplicate_is_idempotent_but_conflict_on_different_value(self):
        self._lock()
        readings = [{"logger_id": "L1", "observed_at": "2026-10-01T10:00:00Z", "temperature": -80.0}]
        first = self._ingest(readings)
        again = self._ingest(readings, request_id="ingest-2")
        self.assertEqual(1, first["inserted"])
        self.assertEqual(0, again["inserted"])
        self.assertEqual(1, again["duplicated"])
        with self.assertRaises(ConflictError):
            self._ingest([{"logger_id": "L1", "observed_at": "2026-10-01T10:00:00Z",
                           "temperature": -65.0}], request_id="ingest-3")

    def test_ingest_rejects_logger_outside_plan(self):
        self._lock()
        with self.assertRaises(ValidationError):
            self._ingest([{"logger_id": "L9", "observed_at": "2026-10-01T10:00:00Z",
                           "temperature": -80.0}])

    # ---- 评估 ----

    def test_assessment_merges_overlapping_loggers_with_worst_temperature(self):
        # 调整方案让 L1 与 L2 同时覆盖 p1，检验重叠读数的归并
        self._lock(packages=[
            {"package_id": "p1", "sample_ids": ["s-1", "s-2"], "logger_ids": ["L1", "L2"]},
            {"package_id": "p2", "sample_ids": ["s-3"], "logger_ids": ["L2"]},
        ])
        self._ingest([
            {"logger_id": "L1", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
            {"logger_id": "L1", "observed_at": "2026-10-01T10:00:00Z", "temperature": -65.0},
            {"logger_id": "L1", "observed_at": "2026-10-01T10:30:00Z", "temperature": -65.0},
            {"logger_id": "L1", "observed_at": "2026-10-01T11:00:00Z", "temperature": -80.0},
            # L2 同一时刻本地时间读数更差：-55.0 - 0.5 = -55.5，落入 critical
            {"logger_id": "L2", "observed_at": "2026-10-01T16:00:00", "temperature": -80.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T18:00:00", "temperature": -55.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T18:30:00", "temperature": -55.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T19:00:00", "temperature": -80.0},
        ])
        self._assess()
        detail = self.service.get_assessment(actor_id="qua1", site_id="s1", batch_id="b1")
        result = detail["result"]
        p1 = result["packages"]["p1"]
        # 10:00-11:00 归并后取最坏值 -55.5 → critical 60 分钟，超过 10 分钟预算
        self.assertEqual(60.0, p1["consumption_minutes"]["critical"])
        self.assertEqual("destroy", result["recommended_outcome"])
        self.assertEqual(sorted(["s-1", "s-2", "s-3"]), result["affected_samples"]["excursion"])

    def test_assessment_flags_gap_and_drift_with_impact_scope(self):
        loggers = [
            {"logger_id": "L1", "timezone_name": "UTC", "calibration_offset": 0.0,
             "calibration_valid_from": "2026-10-01T00:00:00Z",
             "calibration_valid_to": "2026-10-01T10:00:00Z"},
            {"logger_id": "L2", "timezone_name": "UTC", "calibration_offset": 0.0,
             "calibration_valid_from": "2026-10-01T00:00:00Z",
             "calibration_valid_to": "2026-10-01T23:59:00Z"},
        ]
        self._lock(loggers=loggers)
        self._ingest([
            {"logger_id": "L1", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
            {"logger_id": "L1", "observed_at": "2026-10-01T09:00:00Z", "temperature": -80.0},
            # 10:30 与 11:00 超出 L1 校准有效期 → 漂移；09:45-10:30 无覆盖 → 缺口
            {"logger_id": "L1", "observed_at": "2026-10-01T10:30:00Z", "temperature": -80.0},
            {"logger_id": "L1", "observed_at": "2026-10-01T11:00:00Z", "temperature": -80.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T16:00:00Z", "temperature": -80.0},
        ])
        self._assess()
        result = self.service.get_assessment(actor_id="qua1", site_id="s1", batch_id="b1")["result"]
        gaps = [gap for gap in result["gaps"] if gap["package_id"] == "p1"]
        self.assertTrue(any(gap["start"] == "2026-10-01T09:45:00Z"
                            and gap["end"] == "2026-10-01T10:30:00Z" for gap in gaps))
        self.assertTrue(all(not gap["overlaps_unfrozen"] for gap in gaps))
        drift = result["drift"]
        self.assertEqual(1, len(drift))
        self.assertEqual("L1", drift[0]["logger_id"])
        self.assertEqual(2, drift[0]["count"])
        self.assertEqual(["s-1", "s-2"], drift[0]["sample_ids"])
        self.assertEqual(["s-1", "s-2"], result["affected_samples"]["drift"])

    def test_assessment_is_content_idempotent(self):
        self._lock()
        self._ingest([
            {"logger_id": "L1", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
        ])
        first = self._assess()
        second = self._assess(request_id="assess-2")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["assessment_id"], second["assessment_id"])

    def test_late_readings_form_new_version_and_keep_old_one(self):
        self._lock()
        self._ingest([
            {"logger_id": "L1", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
            {"logger_id": "L1", "observed_at": "2026-10-01T11:00:00Z", "temperature": -80.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
        ])
        first = self._assess()
        self.clock.advance(hours=2)
        # 迟到证据：观察时间在运输途中，但到达时间晚于首版评估
        self._ingest([
            {"logger_id": "L1", "observed_at": "2026-10-01T10:00:00Z", "temperature": -61.0},
        ], request_id="ingest-late")
        second = self._assess(request_id="assess-2")
        self.assertEqual(1, first["version"])
        self.assertEqual(2, second["version"])
        v2 = self.service.get_assessment(actor_id="qua1", site_id="s1", batch_id="b1")["result"]
        self.assertEqual(1, len(v2["late_evidence"]))
        self.assertEqual("L1", v2["late_evidence"][0]["logger_id"])
        self.assertEqual(["s-1", "s-2"], v2["late_evidence"][0]["sample_ids"])
        v1 = self.service.get_assessment(actor_id="qua1", site_id="s1", batch_id="b1", version=1)
        self.assertEqual(1, v1["version"])
        self.assertEqual([], v1["result"]["late_evidence"])

    # ---- 双角色审批 ----

    def _prepare_assessment(self):
        self._lock()
        self._ingest([
            {"logger_id": "L1", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
        ])
        return self._assess()

    def test_dual_role_agreement_produces_single_effective_version(self):
        self._prepare_assessment()
        pending = self._decide("res1", "continue_use", "det-1")
        self.assertEqual("pending", pending["status"])
        effective = self._decide("qua1", "continue_use", "det-2")
        self.assertEqual("effective", effective["status"])
        self.assertEqual("continue_use", effective["outcome"])
        count = self.database.connection.execute(
            "SELECT COUNT(*) FROM coldchain_effective_decisions"
        ).fetchone()[0]
        self.assertEqual(1, count)

    def test_disagreement_produces_no_effective_version(self):
        self._prepare_assessment()
        self._decide("res1", "continue_use", "det-1")
        disagreed = self._decide("qua1", "destroy", "det-2")
        self.assertEqual("disagreed", disagreed["status"])
        record = self.service.disposition_record(actor_id="qua1", site_id="s1", batch_id="b1")
        self.assertIsNone(record["decisions"]["effective"])
        # 分歧后可以开启新一轮，双方一致后生效
        self._decide("res1", "restrict_use", "det-3")
        effective = self._decide("qua1", "restrict_use", "det-4")
        self.assertEqual("effective", effective["status"])
        self.assertEqual(2, effective["version"])

    def test_same_role_cannot_submit_twice_in_one_round(self):
        self._prepare_assessment()
        self._decide("res1", "continue_use", "det-1")
        with self.assertRaises(ConflictError):
            self._decide("res1", "destroy", "det-2")

    def test_determination_must_use_latest_assessment(self):
        first = self._prepare_assessment()
        self.clock.advance(hours=1)
        self._ingest([{"logger_id": "L1", "observed_at": "2026-10-01T10:00:00Z",
                       "temperature": -80.0}], request_id="ingest-2")
        self._assess(request_id="assess-2")
        with self.assertRaises(ConflictError):
            self.service.submit_determination(request_id="det-1", actor_id="res1",
                                              site_id="s1", batch_id="b1",
                                              outcome="continue_use", rationale="基于旧版",
                                              assessment_id=first["assessment_id"])

    def test_new_assessment_supersedes_stale_pending_round(self):
        self._prepare_assessment()
        self._decide("res1", "continue_use", "det-1")
        self.clock.advance(hours=1)
        self._ingest([{"logger_id": "L1", "observed_at": "2026-10-01T10:00:00Z",
                       "temperature": -55.0}], request_id="ingest-2")
        self._assess(request_id="assess-2")
        quality = self._decide("qua1", "destroy", "det-2")
        self.assertEqual(2, quality["version"])
        self.assertEqual("pending", quality["status"])
        record = self.service.disposition_record(actor_id="qua1", site_id="s1", batch_id="b1")
        statuses = [item["status"] for item in record["decisions"]["history"]]
        self.assertIn("superseded", statuses)

    def test_effective_version_is_replaced_only_by_new_agreement(self):
        self._prepare_assessment()
        self._decide("res1", "continue_use", "det-1")
        first = self._decide("qua1", "continue_use", "det-2")
        self.clock.advance(hours=1)
        self._ingest([{"logger_id": "L1", "observed_at": "2026-10-01T10:00:00Z",
                       "temperature": -55.0}], request_id="ingest-2")
        self._assess(request_id="assess-2")
        self._decide("res1", "restrict_use", "det-3")
        second = self._decide("qua1", "restrict_use", "det-4")
        self.assertEqual("effective", second["status"])
        record = self.service.disposition_record(actor_id="qua1", site_id="s1", batch_id="b1")
        self.assertEqual(second["decision_id"], record["decisions"]["effective"]["decision_id"])
        history = {item["version"]: item["status"] for item in record["decisions"]["history"]}
        self.assertEqual("superseded", history[first["version"]])
        self.assertEqual("effective", history[second["version"]])

    # ---- 报告、撤回与后续责任 ----

    def _prepare_effective(self, outcome="restrict_use"):
        self._prepare_assessment()
        self._decide("res1", outcome, "det-1")
        return self._decide("qua1", outcome, "det-2")

    def test_report_pins_decision_snapshot_and_requires_effective(self):
        self._prepare_assessment()
        with self.assertRaises(ConflictError):
            self.service.create_report(request_id="rep-0", actor_id="qua1", site_id="s1",
                                       batch_id="b1", title="无决定报告")
        self._prepare_effective()
        report = self.service.create_report(request_id="rep-1", actor_id="qua1", site_id="s1",
                                            batch_id="b1", title="偏差处置报告")
        self.assertEqual("restrict_use", report["outcome"])

    def test_withdraw_creates_obligations_and_keeps_report_snapshot(self):
        effective = self._prepare_effective()
        self.service.create_report(request_id="rep-1", actor_id="qua1", site_id="s1",
                                   batch_id="b1", title="偏差处置报告")
        withdrawn = self.service.withdraw_decision(request_id="wd-1", actor_id="qua1",
                                                   site_id="s1", batch_id="b1",
                                                   reason="承运方补充了干冰更换记录")
        self.assertEqual("withdrawn", withdrawn["status"])
        self.assertEqual(1, withdrawn["referenced_by_reports"])
        self.assertEqual({"isolate", "notify"},
                         {item["kind"] for item in withdrawn["obligations"]})
        record = self.service.disposition_record(actor_id="qua1", site_id="s1", batch_id="b1")
        self.assertIsNone(record["decisions"]["effective"])
        self.assertEqual(2, len(record["outstanding_obligations"]))
        # 被报告引用的决定不被改写：快照仍是 restrict_use
        snapshot = record["reports"][0]["decision_snapshot"]
        self.assertEqual("restrict_use", snapshot["outcome"])
        self.assertEqual(effective["version"], snapshot["decision_version"])
        history = {item["version"]: item["status"] for item in record["decisions"]["history"]}
        self.assertEqual("withdrawn", history[effective["version"]])

    def test_complete_obligation_tracks_outstanding_responsibilities(self):
        self._prepare_effective()
        withdrawn = self.service.withdraw_decision(request_id="wd-1", actor_id="qua1",
                                                   site_id="s1", batch_id="b1", reason="复核")
        isolate = next(item for item in withdrawn["obligations"] if item["kind"] == "isolate")
        done = self.service.complete_obligation(request_id="ob-1", actor_id="op1",
                                                obligation_id=isolate["obligation_id"])
        self.assertEqual("done", done["status"])
        record = self.service.disposition_record(actor_id="qua1", site_id="s1", batch_id="b1")
        self.assertEqual(["notify"], [item["kind"] for item in record["outstanding_obligations"]])
        with self.assertRaises(ConflictError):
            self.service.complete_obligation(request_id="ob-2", actor_id="op1",
                                             obligation_id=isolate["obligation_id"])

    def test_withdraw_without_effective_decision_is_rejected(self):
        self._prepare_assessment()
        with self.assertRaises(ConflictError):
            self.service.withdraw_decision(request_id="wd-1", actor_id="qua1",
                                           site_id="s1", batch_id="b1", reason="无生效决定")

    # ---- 处置记录 ----

    def test_disposition_record_explains_budget_consumption_and_shared_packaging(self):
        self._lock()
        self._ingest([
            {"logger_id": "L1", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
            {"logger_id": "L1", "observed_at": "2026-10-01T10:00:00Z", "temperature": -65.0},
            {"logger_id": "L1", "observed_at": "2026-10-01T10:30:00Z", "temperature": -65.0},
            {"logger_id": "L1", "observed_at": "2026-10-01T11:00:00Z", "temperature": -80.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T08:00:00Z", "temperature": -80.0},
            {"logger_id": "L2", "observed_at": "2026-10-01T16:00:00Z", "temperature": -80.0},
        ])
        self._assess()
        record = self.service.disposition_record(actor_id="au1", site_id="s1", batch_id="b1")
        # 每一段暴露如何消耗预算：p1 在 10:00-11:00 处于 excursion，累计 60 分钟
        segments = [item for item in record["exposure_segments"]
                    if item["package_id"] == "p1" and item["kind"] == "exposure"
                    and item.get("zone") == "excursion"]
        self.assertEqual(1, len(segments))
        self.assertEqual(60.0, segments[0]["minutes"])
        self.assertEqual(60, segments[0]["zone_budget_minutes"])
        self.assertEqual(60.0, segments[0]["zone_consumed_cumulative"])
        accounting = {(item["package_id"], item["zone"]): item
                      for item in record["budget_accounting"]}
        self.assertEqual(60.0, accounting[("p1", "excursion")]["consumed_minutes"])
        self.assertEqual(0.0, accounting[("p1", "excursion")]["remaining_minutes"])
        # 共享包装影响：s-1、s-2 同在 p1，被 excursion 波及；s-3 在 p2 未受影响
        packaging = {item["package_id"]: item for item in record["shared_packaging"]}
        self.assertTrue(packaging["p1"]["excursion"])
        self.assertEqual(["s-1", "s-2"], packaging["p1"]["sample_ids"])
        self.assertFalse(packaging["p2"]["excursion"])
        self.assertEqual(["s-1", "s-2"], record["assessment"]["affected_samples"]["excursion"])

    # ---- 权限与接口 ----

    def test_role_permissions_are_enforced(self):
        self._lock()
        with self.assertRaises(PermissionDenied):
            self._decide("op1", "continue_use", "det-x")
        with self.assertRaises(PermissionDenied):
            self.service.ingest_readings(request_id="ingest-x", actor_id="res1",
                                         site_id="s1", batch_id="b1",
                                         readings=[{"logger_id": "L1",
                                                    "observed_at": "2026-10-01T08:00:00Z",
                                                    "temperature": -80.0}])
        with self.assertRaises(PermissionDenied):
            self.service.lock_plan(**self._plan(request_id="plan-x", actor_id="au1",
                                                batch_id="b2"))

    def test_http_routes_dispatch_coldchain_requests(self):
        headers = {"X-Actor-Id": "op1"}
        body = self._plan()
        body.pop("actor_id")
        status, payload = route(self.foundation, "POST", "/coldchain/plans", body, headers,
                                coldchain=self.service)
        self.assertEqual(201, status)
        self.assertTrue(payload["locked"])
        status, _ = route(self.foundation, "POST", "/coldchain/plans", body, headers,
                          coldchain=self.service)
        self.assertEqual(200, status)
        status, payload = route(self.foundation, "GET",
                                "/coldchain/disposition?site_id=s1&batch_id=b1", None, headers,
                                coldchain=self.service)
        self.assertEqual(200, status)
        self.assertEqual("b1", payload["batch_id"])
        status, _ = route(self.foundation, "GET",
                          "/coldchain/disposition?site_id=s1&batch_id=b1", None, headers)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
