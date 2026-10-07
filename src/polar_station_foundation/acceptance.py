"""运行基础服务与冷链判定项目的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock, MutableClock
from .coldchain import ColdChainService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科考机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="站务负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号科考站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="station_operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="station_operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed}
        database.close()
        return result


def _l1_readings() -> list[dict[str, object]]:
    """一号记录器（Asia/Shanghai，+0.2℃）在运输全程的本地时间读数。"""

    local_times = ["16:00", "16:30", "17:00", "17:30", "18:00", "18:30", "19:00", "19:30",
                   "20:00", "20:30", "21:00", "21:30", "22:00", "22:30", "23:00"]
    values = [-78.0, -77.9, -77.5, -77.6, -71.0, -66.0, -63.0, -68.0,
              -74.0, -75.5, -76.0, -76.5, -77.0, -77.2, -77.5]
    return [{"logger_id": "L1", "observed_at": f"2026-10-01T{hour}:00", "temperature": value}
            for hour, value in zip(local_times, values)]


def _l2_readings() -> list[dict[str, object]]:
    """二号记录器（UTC，-0.1℃）与一号重叠的读数。"""

    utc_times = ["08:00", "08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
                 "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00"]
    values = [-78.1, -78.0, -77.6, -77.7, -70.8, -65.5, -62.5, -67.5,
              -73.8, -75.2, -75.9, -76.4, -76.8, -77.0, -77.4]
    return [{"logger_id": "L2", "observed_at": f"2026-10-01T{hour}:00Z", "temperature": value}
            for hour, value in zip(utc_times, values)]


def _l3_readings() -> list[dict[str, object]]:
    """三号记录器（America/New_York）读数，12:00-13:00 UTC 缺口，11:00 UTC 后校准失效。"""

    local_times = ["04:00", "04:30", "05:00", "05:30", "06:00", "06:30", "07:00", "07:30",
                   "09:00", "09:30", "10:00", "10:30", "11:00"]
    values = [-79.0, -79.1, -78.9, -79.2, -79.0, -78.8, -79.1, -79.0,
              -78.9, -79.0, -79.1, -78.8, -79.0]
    return [{"logger_id": "L3", "observed_at": f"2026-10-01T{hour}:00", "temperature": value}
            for hour, value in zip(local_times, values)]


def run_coldchain() -> dict[str, object]:
    """演练转机等待升温偏差：评估、双角色审批、报告引用、撤回与后续责任。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "coldchain.sqlite3")
        clock = MutableClock(datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc))
        foundation = DomainService(database, clock)
        service = ColdChainService(foundation)
        foundation.register_organization(request_id="cc-org", actor_id="bootstrap",
                                         organization_id="org-001", name="示范科考机构")
        foundation.register_actor(request_id="cc-admin", actor_id="bootstrap", new_actor_id="admin-001",
                                  display_name="系统管理员", role="admin", organization_id="org-001")
        foundation.register_actor(request_id="cc-operator", actor_id="admin-001", new_actor_id="operator-001",
                                  display_name="物流操作员", role="operator", organization_id="org-001")
        foundation.register_actor(request_id="cc-researcher", actor_id="admin-001", new_actor_id="researcher-001",
                                  display_name="科研负责人", role="researcher", organization_id="org-001")
        foundation.register_actor(request_id="cc-quality", actor_id="admin-001", new_actor_id="quality-001",
                                  display_name="质量负责人", role="quality", organization_id="org-001")
        foundation.register_site(request_id="cc-site", actor_id="operator-001", site_id="site-001",
                                 organization_id="org-001", name="转运科考站点", timezone_name="Asia/Shanghai")

        plan = service.lock_plan(
            request_id="cc-plan", actor_id="operator-001", site_id="site-001",
            batch_id="batch-microbe-001",
            sample_ids=["sample-a", "sample-b", "sample-c", "sample-d"],
            packages=[
                {"package_id": "p-1", "sample_ids": ["sample-a", "sample-b"],
                 "logger_ids": ["L1", "L2"]},
                {"package_id": "p-2", "sample_ids": ["sample-c", "sample-d"],
                 "logger_ids": ["L3"]},
            ],
            segments=[
                {"name": "干线运输", "start": "2026-10-01T08:00:00Z", "end": "2026-10-01T10:00:00Z"},
                {"name": "转机等待", "start": "2026-10-01T10:00:00Z", "end": "2026-10-01T13:00:00Z"},
                {"name": "末端配送", "start": "2026-10-01T13:00:00Z", "end": "2026-10-01T15:00:00Z"},
            ],
            zones=[
                {"name": "frozen", "lower": None, "upper": -70.0},
                {"name": "excursion", "lower": -70.0, "upper": -60.0, "budget_minutes": 150},
                {"name": "critical", "lower": -60.0, "upper": None, "budget_minutes": 15},
            ],
            loggers=[
                {"logger_id": "L1", "timezone_name": "Asia/Shanghai", "calibration_offset": 0.2,
                 "calibration_valid_from": "2026-10-01T00:00:00Z",
                 "calibration_valid_to": "2026-10-02T00:00:00Z"},
                {"logger_id": "L2", "timezone_name": "UTC", "calibration_offset": -0.1,
                 "calibration_valid_from": "2026-10-01T00:00:00Z",
                 "calibration_valid_to": "2026-10-02T00:00:00Z"},
                {"logger_id": "L3", "timezone_name": "America/New_York", "calibration_offset": 0.0,
                 "calibration_valid_from": "2026-10-01T00:00:00Z",
                 "calibration_valid_to": "2026-10-01T11:00:00Z"},
            ],
            max_hold_minutes=30,
            rules=[
                {"when": "over_budget", "zone": "excursion", "outcome": "restrict_use"},
                {"when": "over_budget", "zone": "critical", "outcome": "destroy"},
                {"when": "gap_overlaps_unfrozen", "outcome": "restrict_use"},
            ],
        )
        plan_replay = service.lock_plan(
            request_id="cc-plan", actor_id="operator-001", site_id="site-001",
            batch_id="batch-microbe-001",
            sample_ids=["sample-a", "sample-b", "sample-c", "sample-d"],
            packages=[
                {"package_id": "p-1", "sample_ids": ["sample-a", "sample-b"],
                 "logger_ids": ["L1", "L2"]},
                {"package_id": "p-2", "sample_ids": ["sample-c", "sample-d"],
                 "logger_ids": ["L3"]},
            ],
            segments=[
                {"name": "干线运输", "start": "2026-10-01T08:00:00Z", "end": "2026-10-01T10:00:00Z"},
                {"name": "转机等待", "start": "2026-10-01T10:00:00Z", "end": "2026-10-01T13:00:00Z"},
                {"name": "末端配送", "start": "2026-10-01T13:00:00Z", "end": "2026-10-01T15:00:00Z"},
            ],
            zones=[
                {"name": "frozen", "lower": None, "upper": -70.0},
                {"name": "excursion", "lower": -70.0, "upper": -60.0, "budget_minutes": 150},
                {"name": "critical", "lower": -60.0, "upper": None, "budget_minutes": 15},
            ],
            loggers=[
                {"logger_id": "L1", "timezone_name": "Asia/Shanghai", "calibration_offset": 0.2,
                 "calibration_valid_from": "2026-10-01T00:00:00Z",
                 "calibration_valid_to": "2026-10-02T00:00:00Z"},
                {"logger_id": "L2", "timezone_name": "UTC", "calibration_offset": -0.1,
                 "calibration_valid_from": "2026-10-01T00:00:00Z",
                 "calibration_valid_to": "2026-10-02T00:00:00Z"},
                {"logger_id": "L3", "timezone_name": "America/New_York", "calibration_offset": 0.0,
                 "calibration_valid_from": "2026-10-01T00:00:00Z",
                 "calibration_valid_to": "2026-10-01T11:00:00Z"},
            ],
            max_hold_minutes=30,
            rules=[
                {"when": "over_budget", "zone": "excursion", "outcome": "restrict_use"},
                {"when": "over_budget", "zone": "critical", "outcome": "destroy"},
                {"when": "gap_overlaps_unfrozen", "outcome": "restrict_use"},
            ],
        )

        batch_readings = _l1_readings() + _l2_readings() + _l3_readings()
        service.ingest_readings(request_id="cc-ingest-1", actor_id="operator-001",
                                site_id="site-001", batch_id="batch-microbe-001",
                                readings=batch_readings)
        ingest_replay = service.ingest_readings(request_id="cc-ingest-1", actor_id="operator-001",
                                                site_id="site-001", batch_id="batch-microbe-001",
                                                readings=batch_readings)
        ingest_duplicate = service.ingest_readings(request_id="cc-ingest-1b", actor_id="operator-001",
                                                   site_id="site-001", batch_id="batch-microbe-001",
                                                   readings=batch_readings)

        clock.set(datetime(2026, 10, 1, 16, 5, tzinfo=timezone.utc))
        assessment_v1 = service.compute_assessment(request_id="cc-assess-1", actor_id="operator-001",
                                                   site_id="site-001", batch_id="batch-microbe-001")
        clock.set(datetime(2026, 10, 1, 16, 10, tzinfo=timezone.utc))
        service.submit_determination(request_id="cc-det-1", actor_id="researcher-001",
                                     site_id="site-001", batch_id="batch-microbe-001",
                                     outcome="continue_use",
                                     rationale="首版评估未超预算，科研侧同意继续")

        clock.set(datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc))
        late_readings = [
            {"logger_id": "L1", "observed_at": "2026-10-01T19:10:00", "temperature": -55.2},
            {"logger_id": "L1", "observed_at": "2026-10-01T19:20:00", "temperature": -54.7},
        ]
        service.ingest_readings(request_id="cc-ingest-late", actor_id="operator-001",
                                site_id="site-001", batch_id="batch-microbe-001",
                                readings=late_readings)
        clock.set(datetime(2026, 10, 1, 16, 35, tzinfo=timezone.utc))
        assessment_v2 = service.compute_assessment(request_id="cc-assess-2", actor_id="operator-001",
                                                   site_id="site-001", batch_id="batch-microbe-001")
        assessment_recompute = service.compute_assessment(request_id="cc-assess-3", actor_id="operator-001",
                                                          site_id="site-001", batch_id="batch-microbe-001")
        v2_detail = service.get_assessment(actor_id="quality-001", site_id="site-001",
                                           batch_id="batch-microbe-001")

        clock.set(datetime(2026, 10, 1, 16, 40, tzinfo=timezone.utc))
        service.submit_determination(request_id="cc-det-2", actor_id="quality-001",
                                     site_id="site-001", batch_id="batch-microbe-001",
                                     outcome="destroy", rationale="critical 温区超预算，质量侧要求销毁")
        clock.set(datetime(2026, 10, 1, 16, 45, tzinfo=timezone.utc))
        disagreement = service.submit_determination(request_id="cc-det-3", actor_id="researcher-001",
                                                    site_id="site-001", batch_id="batch-microbe-001",
                                                    outcome="continue_use",
                                                    rationale="科研侧认为迟到读数需复核")
        clock.set(datetime(2026, 10, 1, 16, 50, tzinfo=timezone.utc))
        service.submit_determination(request_id="cc-det-4", actor_id="researcher-001",
                                     site_id="site-001", batch_id="batch-microbe-001",
                                     outcome="restrict_use",
                                     rationale="复核后科研侧同意限制用途")
        clock.set(datetime(2026, 10, 1, 16, 55, tzinfo=timezone.utc))
        effective = service.submit_determination(request_id="cc-det-5", actor_id="quality-001",
                                                 site_id="site-001", batch_id="batch-microbe-001",
                                                 outcome="restrict_use",
                                                 rationale="质量侧同意限制用途并加强监控")

        clock.set(datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc))
        report = service.create_report(request_id="cc-report-1", actor_id="quality-001",
                                       site_id="site-001", batch_id="batch-microbe-001",
                                       title="转机等待升温偏差处置报告")

        clock.set(datetime(2026, 10, 1, 17, 5, tzinfo=timezone.utc))
        withdrawn = service.withdraw_decision(request_id="cc-withdraw-1", actor_id="quality-001",
                                              site_id="site-001", batch_id="batch-microbe-001",
                                              reason="承运方补充干冰更换记录，需要重新评估")
        isolate_id = next(item["obligation_id"] for item in withdrawn["obligations"]
                          if item["kind"] == "isolate")
        clock.set(datetime(2026, 10, 1, 17, 10, tzinfo=timezone.utc))
        service.complete_obligation(request_id="cc-obligation-1", actor_id="operator-001",
                                    obligation_id=isolate_id)

        disposition = service.disposition_record(actor_id="quality-001", site_id="site-001",
                                                 batch_id="batch-microbe-001")
        valid, _ = foundation.verify_audit()
        result = {
            "status": "ok",
            "plan_replayed": plan_replay["replayed"] and not plan["replayed"],
            "ingest_replayed": ingest_replay["replayed"],
            "ingest_duplicated": ingest_duplicate["duplicated"],
            "assessment_v1": {"version": assessment_v1["version"],
                              "recommended_outcome": assessment_v1["recommended_outcome"]},
            "assessment_v2": {"version": assessment_v2["version"],
                              "recommended_outcome": assessment_v2["recommended_outcome"],
                              "late_evidence": len(v2_detail["result"]["late_evidence"]),
                              "gaps": len(v2_detail["result"]["gaps"]),
                              "drift": len(v2_detail["result"]["drift"])},
            "assessment_recompute_created": assessment_recompute["created"],
            "disagreement_status": disagreement["status"],
            "effective_outcome": effective["outcome"],
            "effective_version": effective["version"],
            "report_snapshot_outcome": report["outcome"],
            "withdrawn_referenced_reports": withdrawn["referenced_by_reports"],
            "outstanding_obligations": [item["kind"] for item in disposition["outstanding_obligations"]],
            "decision_history": len(disposition["decisions"]["history"]),
            "audit_valid": valid,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    foundation = run()
    coldchain = run_coldchain()
    ok = foundation["status"] == "ok" and foundation["audit_valid"] \
        and coldchain["status"] == "ok" and coldchain["audit_valid"]
    result = {"status": "ok" if ok else "error",
              "foundation": foundation, "coldchain": coldchain}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
