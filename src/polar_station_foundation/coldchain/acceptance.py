"""离线端到端验收：复现转机等待期间 -80°C 微生物样品升温偏差。

场景：三个记录器使用不同时区与校准区间，承运方恢复制冷后，
其中一台记录器的数据迟到。验收覆盖：发运锁定、时区与校准归一化、
缺口/漂移/迟到证据、双角色审批、撤回义务、报告引用与最终处置记录。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..clock import FixedClock
from ..service import DomainService
from ..storage import Database
from .schema import COLDCHAIN_SCHEMA
from .service import ColdChainService


ORIGIN = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


def _true_temp(minutes: float) -> float:
    """包装 P1 内样品的真实温度曲线：转机等待时升温，随后恢复制冷。"""

    if minutes < 420:            # 08:00-15:00 UTC
        return -78.0
    if minutes < 430:            # 15:00-15:10 升温
        return -78.0 + (minutes - 420) * 3.0
    if minutes < 470:            # 15:10-15:50 峰值保持
        return -48.0
    if minutes < 490:            # 15:50-16:10 恢复制冷
        return -48.0 - (minutes - 470) * 1.4
    return -76.0


def _series(start_min: int, end_min: int, step: int, offset: float,
            tz_delta_hours: float | None, bias: float = 0.0) -> list[dict]:
    """生成一台记录器的原始读数；tz_delta_hours 为 None 时带显式 Z。"""

    readings = []
    for minute in range(start_min, end_min + 1, step):
        instant = ORIGIN + timedelta(minutes=minute)
        if tz_delta_hours is None:
            observed = instant.isoformat(timespec="seconds").replace("+00:00", "Z")
        else:
            local = (instant + timedelta(hours=tz_delta_hours)).replace(tzinfo=None)
            observed = local.isoformat(timespec="seconds")
        readings.append({"observed_at": observed,
                         "temp_c": round(_true_temp(minute) - offset + bias, 2)})
    return readings


def _steady_series(start_min: int, end_min: int, step: int) -> list[dict]:
    """包装 P2 记录器：全程合规的 -78°C。"""

    return [{"observed_at": (ORIGIN + timedelta(minutes=minute)).isoformat(timespec="seconds")
                            .replace("+00:00", "Z"),
             "temp_c": -78.0}
            for minute in range(start_min, end_min + 1, step)]


def _plan_config() -> dict:
    return {
        "batch_id": "B1",
        "samples": [
            {"sample_id": "S1", "batch_id": "B1", "description": "冻存微生物样品一"},
            {"sample_id": "S2", "batch_id": "B1", "description": "冻存微生物样品二"},
            {"sample_id": "S3", "batch_id": "B2", "description": "冻存微生物样品三（拼箱）"},
            {"sample_id": "S4", "batch_id": "B2", "description": "冻存微生物样品四"},
        ],
        "packages": [
            {"package_id": "P1", "logger_ids": ["L1", "L2", "L3"],
             "sample_ids": ["S1", "S2", "S3"]},
            {"package_id": "P2", "logger_ids": ["L4"], "sample_ids": ["S4"]},
        ],
        "segments": [
            {"segment_id": "leg-warehouse-airport", "mode": "air",
             "start": "2026-09-20T08:00:00Z", "end": "2026-09-20T14:00:00Z"},
            {"segment_id": "layover", "mode": "ground",
             "start": "2026-09-20T14:00:00Z", "end": "2026-09-20T20:00:00Z"},
            {"segment_id": "leg-airport-station", "mode": "air",
             "start": "2026-09-20T20:00:00Z", "end": "2026-09-21T02:00:00Z"},
        ],
        "zones": [
            {"name": "frozen", "min_c": -90.0, "max_c": -60.0},
            {"name": "excursion", "min_c": -60.0, "max_c": -20.0},
        ],
        "budget": {"threshold_c": -60.0, "max_minutes": 30.0, "max_degree_minutes": 80.0},
        "sensors": [
            {"logger_id": "L1", "timezone_name": "Asia/Shanghai", "offset_c": 0.5,
             "drift_c_per_day": 0.0, "calibrated_at": "2026-09-15T00:00:00Z",
             "valid_from": "2026-09-15T00:00:00Z", "valid_to": "2026-10-15T00:00:00Z"},
            {"logger_id": "L2", "timezone_name": "UTC", "offset_c": -0.3,
             "drift_c_per_day": 0.0, "calibrated_at": "2026-09-15T00:00:00Z",
             "valid_from": "2026-09-15T00:00:00Z", "valid_to": "2026-10-15T00:00:00Z"},
            {"logger_id": "L3", "timezone_name": "America/Anchorage", "offset_c": 1.2,
             "drift_c_per_day": 0.0, "calibrated_at": "2026-09-10T00:00:00Z",
             "valid_from": "2026-09-10T00:00:00Z", "valid_to": "2026-09-20T15:30:00Z"},
            {"logger_id": "L4", "timezone_name": "UTC", "offset_c": 0.0,
             "drift_c_per_day": 0.0, "calibrated_at": "2026-09-15T00:00:00Z",
             "valid_from": "2026-09-15T00:00:00Z", "valid_to": "2026-10-15T00:00:00Z"},
        ],
        "rules": {
            "reading_ttl_minutes": 20.0,
            "gap_tolerance_minutes": 30.0,
            "drift_tolerance_c": 3.0,
            "late_after_minutes": 720.0,
            "disposition_policy": {
                "within_budget": ["continue"],
                "exceeded": ["restrict", "destroy"],
                "insufficient_evidence": ["restrict", "destroy"],
            },
        },
    }


def _stored_response(database: Database, request_id: str) -> dict:
    """读取幂等回执中保存的响应体。"""

    row = database.connection.execute(
        "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,),
    ).fetchone()
    return json.loads(row["response_json"]) if row else {}


def run() -> dict[str, object]:
    """执行完整偏差处置链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "coldchain-acceptance.sqlite3",
                            extra_schema=COLDCHAIN_SCHEMA)
        clock = FixedClock(datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc))
        foundation = DomainService(database, clock)
        service = ColdChainService(database, clock)

        def set_clock(value: datetime) -> None:
            service.clock = FixedClock(value)
            foundation.clock = service.clock

        foundation.register_organization(request_id="acc-org", actor_id="bootstrap",
                                         organization_id="org-polar", name="极地科考机构")
        foundation.register_actor(request_id="acc-admin", actor_id="bootstrap",
                                  new_actor_id="admin-1", display_name="系统管理员",
                                  role="admin", organization_id="org-polar")
        foundation.register_actor(request_id="acc-operator", actor_id="admin-1",
                                  new_actor_id="op-1", display_name="站务操作员",
                                  role="operator", organization_id="org-polar")
        foundation.register_actor(request_id="acc-research", actor_id="admin-1",
                                  new_actor_id="res-1", display_name="科研负责人",
                                  role="researcher", organization_id="org-polar")
        foundation.register_actor(request_id="acc-quality", actor_id="admin-1",
                                  new_actor_id="qa-1", display_name="质量负责人",
                                  role="quality", organization_id="org-polar")
        foundation.register_site(request_id="acc-site", actor_id="op-1", site_id="st-1",
                                 organization_id="org-polar", name="冰原科考站",
                                 timezone_name="Asia/Shanghai")

        # 发运时锁定计划：包装组合、运输分段、允许温区、暴露预算、校准与处置规则
        set_clock(datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc))
        service.create_plan(request_id="acc-plan", actor_id="op-1", plan_id="plan-001",
                            site_id="st-1", config=_plan_config())

        # 到达科考站后上传 L1/L2/L4；L1 在升温前传输中断，L2 从恢复后开始
        set_clock(datetime(2026, 9, 21, 2, 30, tzinfo=timezone.utc))
        service.record_readings(
            request_id="acc-up-l1", actor_id="op-1", plan_id="plan-001", package_id="P1",
            logger_id="L1", readings=_series(0, 410, 10, 0.5, 8.0))
        replay_l1 = service.record_readings(
            request_id="acc-up-l1", actor_id="op-1", plan_id="plan-001", package_id="P1",
            logger_id="L1", readings=_series(0, 410, 10, 0.5, 8.0))
        service.record_readings(
            request_id="acc-up-l1-again", actor_id="op-1", plan_id="plan-001", package_id="P1",
            logger_id="L1", readings=_series(0, 410, 10, 0.5, 8.0))
        service.record_readings(
            request_id="acc-up-l2", actor_id="op-1", plan_id="plan-001", package_id="P1",
            logger_id="L2", readings=_series(510, 1080, 10, -0.3, None))
        service.record_readings(
            request_id="acc-up-l4", actor_id="op-1", plan_id="plan-001", package_id="P2",
            logger_id="L4", readings=_steady_series(0, 1080, 15))

        # 第一版判断：转机等待期间存在证据缺口，结论为证据不足
        set_clock(datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc))
        service.compute_assessment(request_id="acc-assess-1", actor_id="qa-1", plan_id="plan-001")
        v1_view = service.get_assessment("plan-001", 1)
        service.approve_disposition(request_id="acc-appr-r1", actor_id="res-1", plan_id="plan-001",
                                    assessment_id=v1_view.assessment_id, decision="restrict",
                                    rationale="证据不足，科研侧先限制用途等待完整数据")
        service.approve_disposition(request_id="acc-appr-q1", actor_id="qa-1", plan_id="plan-001",
                                    assessment_id=v1_view.assessment_id, decision="restrict",
                                    rationale="质量侧同样限制用途，样品暂存隔离区")
        service.create_report(request_id="acc-report-1", actor_id="qa-1", plan_id="plan-001",
                              title="九月冷链偏差月报（初版）")

        # L3 数据迟到：从记录器内存补传，系统要求重新判定
        set_clock(datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc))
        service.record_readings(
            request_id="acc-up-l3", actor_id="op-1", plan_id="plan-001", package_id="P1",
            logger_id="L3", readings=_series(390, 540, 10, 1.2, -8.0, bias=4.0))
        stale_record = service.disposition_record("plan-001")

        # 第二版判断：归并迟到证据后，累计暴露超出预算
        set_clock(datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc))
        service.compute_assessment(request_id="acc-assess-2", actor_id="qa-1", plan_id="plan-001")
        v2_view = service.get_assessment("plan-001", 2)
        stale_approval_error = ""
        try:
            service.approve_disposition(request_id="acc-appr-stale", actor_id="res-1",
                                        plan_id="plan-001", assessment_id=v1_view.assessment_id,
                                        decision="restrict", rationale="试图沿用旧版本")
        except Exception as exc:  # 旧判断版本不得再审批
            stale_approval_error = type(exc).__name__
        service.withdraw_disposition(request_id="acc-withdraw", actor_id="qa-1", plan_id="plan-001",
                                     reason="迟到证据显示累计暴露超预算，撤回初版限制用途决定")
        service.approve_disposition(request_id="acc-appr-r2", actor_id="res-1", plan_id="plan-001",
                                    assessment_id=v2_view.assessment_id, decision="restrict",
                                    rationale="科研侧认为可限制用于非关键实验")
        service.approve_disposition(request_id="acc-appr-q2", actor_id="qa-1", plan_id="plan-001",
                                    assessment_id=v2_view.assessment_id, decision="destroy",
                                    rationale="累计暴露严重超预算，质量侧要求销毁")
        service.create_report(request_id="acc-report-2", actor_id="qa-1", plan_id="plan-001",
                              title="九月冷链偏差月报（终版）")

        # 履行一条隔离义务，其余保持待办
        obligations = service.list_obligations("plan-001")
        first_isolation = next(item for item in obligations if item["kind"] == "isolation")
        service.complete_obligation(request_id="acc-obl-1", actor_id="op-1",
                                    obligation_id=first_isolation["obligation_id"],
                                    note="样品已移入隔离柜并挂牌")

        record = service.disposition_record("plan-001")
        valid, event_count = foundation.verify_audit()
        p1_account = next(item for item in record["exposure_account"]
                          if item["package_id"] == "P1")
        result = {
            "status": "ok",
            "plan_locked": service.get_plan("plan-001").status == "locked",
            "upload_l1_replayed": replay_l1.replayed,
            "upload_l1_duplicates": _stored_response(database, "acc-up-l1-again")["duplicates"],
            "assessment_v1_outcome": v1_view.outcome,
            "assessment_v2_outcome": v2_view.outcome,
            "late_upload_flagged": _stored_response(database, "acc-up-l3")["requires_reassessment"],
            "late_evidence_findings": len(v2_view.findings["late_evidence"]),
            "drift_findings": len(v2_view.findings["drift"]),
            "calibration_findings": len(v2_view.findings["calibration"]),
            "stale_evidence_detected": stale_record["evidence_stale"],
            "stale_approval_rejected": stale_approval_error,
            "disposition_v1_outcome": record["disposition_history"][0]["outcome"],
            "disposition_v1_cited": bool(record["disposition_history"][0]["cited_by"]),
            "disposition_v1_withdrawn": record["disposition_history"][0]["withdrawal"] is not None,
            "current_outcome": record["current_disposition"]["outcome"]
                               if record["current_disposition"] else None,
            "excursion_segments": [item["segment_id"] for item in p1_account["excursions"]],
            "budget_minutes_consumed": p1_account["consumption"]["minutes"],
            "shared_package_affected": record["shared_packaging_impact"][0]["affected"]
                                       if record["shared_packaging_impact"] else None,
            "affected_samples": record["affected_sample_ids"],
            "obligations_created": _stored_response(database, "acc-withdraw")["obligations_created"],
            "obligations_outstanding": len(record["outstanding_obligations"]),
            "audit_events": event_count,
            "audit_valid": valid,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    checks = [
        result["status"] == "ok",
        result["audit_valid"],
        result["plan_locked"],
        result["upload_l1_replayed"],
        result["upload_l1_duplicates"] == 42,
        result["assessment_v1_outcome"] == "insufficient_evidence",
        result["assessment_v2_outcome"] == "exceeded",
        result["late_upload_flagged"],
        result["late_evidence_findings"] == 1,
        result["drift_findings"] >= 1,
        result["calibration_findings"] == 1,
        result["stale_evidence_detected"],
        result["stale_approval_rejected"] == "ConflictError",
        result["disposition_v1_outcome"] == "restrict",
        result["disposition_v1_cited"],
        result["disposition_v1_withdrawn"],
        result["current_outcome"] == "destroy",
        result["excursion_segments"] == ["layover"],
        result["budget_minutes_consumed"] > 30.0,
        result["shared_package_affected"],
        result["affected_samples"] == ["S1", "S2", "S3"],
        result["obligations_created"] == 6,
        result["obligations_outstanding"] == 5,
    ]
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
