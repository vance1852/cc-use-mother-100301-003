"""冷链判定项目：发运锁定、证据归一化、判断版本与双角色处置。

设计约束：
- 计划在发运时锁定，之后不可修改；
- 判断（评估）只追加新版本，后补证据不会改写旧版本；
- 审批与处置决定只追加，生效指针单独维护，因此已被报告引用的
  决定保持原样，只能被新版本取代而不能被暗中改写；
- 所有写命令经过基础服务的幂等回执与哈希链审计。
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from ..audit import append_event, canonical_json, digest
from ..clock import Clock, SystemClock
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..service import IDENTIFIER, DomainService
from ..storage import Database
from .assessment import EvidenceReading, compute_findings, correct_temperature
from .models import APPROVAL_ROLES, DECISIONS, OUTCOMES, AssessmentView, PlanView, stricter
from .timeutil import parse_observed, parse_utc, to_utc_iso


class ColdChainService:
    """在基础服务边界上实现冷链判定项目。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.foundation = DomainService(database, self.clock)

    def _now(self) -> str:
        return to_utc_iso(self.clock.now())

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _number(self, value: Any, field: str, minimum: float | None = None,
                maximum: float | None = None) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是数值") from exc
        if not math.isfinite(number):
            raise ValidationError(f"{field} 必须是有限数值")
        if minimum is not None and number < minimum:
            raise ValidationError(f"{field} 不能小于 {minimum}")
        if maximum is not None and number > maximum:
            raise ValidationError(f"{field} 不能大于 {maximum}")
        return number

    # ------------------------------------------------------------------
    # 计划：发运时锁定包装组合、运输分段、允许温区、暴露预算、校准与处置规则
    # ------------------------------------------------------------------

    def _validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise ValidationError("config 必须是对象")
        batch_id = self._identifier(config.get("batch_id", ""), "batch_id")

        samples = config.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ValidationError("samples 必须是非空列表")
        normalized_samples = []
        seen_samples: set[str] = set()
        for item in samples:
            if not isinstance(item, dict):
                raise ValidationError("samples 元素必须是对象")
            sample_id = self._identifier(item.get("sample_id", ""), "sample_id")
            if sample_id in seen_samples:
                raise ValidationError(f"样品编号重复: {sample_id}")
            seen_samples.add(sample_id)
            normalized_samples.append({
                "sample_id": sample_id,
                "batch_id": self._identifier(item.get("batch_id", batch_id), "sample.batch_id"),
                "description": str(item.get("description", ""))[:200],
            })

        packages = config.get("packages")
        if not isinstance(packages, list) or not packages:
            raise ValidationError("packages 必须是非空列表")
        normalized_packages = []
        seen_packages: set[str] = set()
        claimed_samples: set[str] = set()
        for item in packages:
            if not isinstance(item, dict):
                raise ValidationError("packages 元素必须是对象")
            package_id = self._identifier(item.get("package_id", ""), "package_id")
            if package_id in seen_packages:
                raise ValidationError(f"包装编号重复: {package_id}")
            seen_packages.add(package_id)
            logger_ids = item.get("logger_ids")
            sample_ids = item.get("sample_ids")
            if not isinstance(logger_ids, list) or not logger_ids:
                raise ValidationError(f"包装 {package_id} 必须至少有一个记录器")
            if not isinstance(sample_ids, list) or not sample_ids:
                raise ValidationError(f"包装 {package_id} 必须至少有一个样品")
            logger_ids = [self._identifier(value, "logger_id") for value in logger_ids]
            sample_ids = [self._identifier(value, "sample_id") for value in sample_ids]
            unknown = [value for value in sample_ids if value not in seen_samples]
            if unknown:
                raise ValidationError(f"包装 {package_id} 引用了未知样品: {unknown}")
            for sample_id in sample_ids:
                if sample_id in claimed_samples:
                    raise ValidationError(f"样品 {sample_id} 被多个包装引用")
                claimed_samples.add(sample_id)
            normalized_packages.append({
                "package_id": package_id,
                "logger_ids": logger_ids,
                "sample_ids": sample_ids,
            })
        if claimed_samples != seen_samples:
            missing = sorted(seen_samples - claimed_samples)
            raise ValidationError(f"以下样品没有分配包装: {missing}")

        segments = config.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValidationError("segments 必须是非空列表")
        normalized_segments = []
        for item in segments:
            if not isinstance(item, dict):
                raise ValidationError("segments 元素必须是对象")
            segment_id = self._identifier(item.get("segment_id", ""), "segment_id")
            start = parse_observed(str(item.get("start", "")), None)
            end = parse_observed(str(item.get("end", "")), None)
            if end <= start:
                raise ValidationError(f"分段 {segment_id} 的结束时间必须晚于开始时间")
            normalized_segments.append({
                "segment_id": segment_id,
                "mode": str(item.get("mode", "unspecified"))[:40],
                "start": start,
                "end": end,
            })
        normalized_segments.sort(key=lambda item: item["start"])
        for previous, current in zip(normalized_segments, normalized_segments[1:]):
            if current["start"] < previous["end"]:
                raise ValidationError("运输分段时间窗口不能重叠")

        zones = config.get("zones")
        if not isinstance(zones, list) or not zones:
            raise ValidationError("zones 必须是非空列表")
        normalized_zones = []
        for item in zones:
            if not isinstance(item, dict):
                raise ValidationError("zones 元素必须是对象")
            name = self._text(item.get("name", ""), "zone.name", 40)
            min_c = self._number(item.get("min_c"), "zone.min_c")
            max_c = self._number(item.get("max_c"), "zone.max_c")
            if max_c <= min_c:
                raise ValidationError(f"温区 {name} 的上限必须大于下限")
            normalized_zones.append({"name": name, "min_c": min_c, "max_c": max_c})
        normalized_zones.sort(key=lambda item: item["max_c"])
        for previous, current in zip(normalized_zones, normalized_zones[1:]):
            if current["min_c"] < previous["max_c"]:
                raise ValidationError("允许温区不能重叠")

        budget = config.get("budget")
        if not isinstance(budget, dict):
            raise ValidationError("budget 必须是对象")
        normalized_budget = {
            "threshold_c": self._number(budget.get("threshold_c"), "budget.threshold_c"),
            "max_minutes": self._number(budget.get("max_minutes"), "budget.max_minutes", 0.0),
            "max_degree_minutes": self._number(
                budget.get("max_degree_minutes"), "budget.max_degree_minutes", 0.0),
        }
        compliant_zone_max = normalized_zones[0]["max_c"]
        if normalized_budget["threshold_c"] != compliant_zone_max:
            raise ValidationError("暴露预算阈值必须与最低允许温区的上限一致")
        if normalized_budget["max_minutes"] <= 0 or normalized_budget["max_degree_minutes"] <= 0:
            raise ValidationError("暴露预算额度必须大于零")

        sensors = config.get("sensors")
        if not isinstance(sensors, list) or not sensors:
            raise ValidationError("sensors 必须是非空列表")
        normalized_sensors = []
        seen_loggers: set[str] = set()
        for item in sensors:
            if not isinstance(item, dict):
                raise ValidationError("sensors 元素必须是对象")
            logger_id = self._identifier(item.get("logger_id", ""), "logger_id")
            if logger_id in seen_loggers:
                raise ValidationError(f"记录器编号重复: {logger_id}")
            seen_loggers.add(logger_id)
            timezone_name = self._text(item.get("timezone_name", ""), "sensor.timezone_name", 80)
            calibrated_at = parse_observed(str(item.get("calibrated_at", "")), None)
            valid_from = parse_observed(str(item.get("valid_from", "")), None)
            valid_to = parse_observed(str(item.get("valid_to", "")), None)
            if not valid_from <= calibrated_at <= valid_to:
                raise ValidationError(f"记录器 {logger_id} 的校准时间必须落在有效期内")
            normalized_sensors.append({
                "logger_id": logger_id,
                "timezone_name": timezone_name,
                "offset_c": self._number(item.get("offset_c", 0.0), "sensor.offset_c"),
                "drift_c_per_day": self._number(
                    item.get("drift_c_per_day", 0.0), "sensor.drift_c_per_day"),
                "calibrated_at": calibrated_at,
                "valid_from": valid_from,
                "valid_to": valid_to,
            })
        known_loggers = {sensor["logger_id"] for sensor in normalized_sensors}
        for package in normalized_packages:
            unknown = [value for value in package["logger_ids"] if value not in known_loggers]
            if unknown:
                raise ValidationError(f"包装 {package['package_id']} 引用了未校准的记录器: {unknown}")

        rules = config.get("rules")
        if not isinstance(rules, dict):
            raise ValidationError("rules 必须是对象")
        policy = rules.get("disposition_policy")
        if not isinstance(policy, dict):
            raise ValidationError("rules.disposition_policy 必须是对象")
        normalized_policy = {}
        for outcome in sorted(OUTCOMES):
            allowed = policy.get(outcome)
            if not isinstance(allowed, list) or not allowed:
                raise ValidationError(f"处置规则缺少结论 {outcome} 的允许决定")
            normalized = []
            for decision in allowed:
                if decision not in DECISIONS:
                    raise ValidationError(f"处置规则包含未知决定: {decision}")
                if decision not in normalized:
                    normalized.append(decision)
            normalized_policy[outcome] = normalized
        normalized_rules = {
            "reading_ttl_minutes": self._number(
                rules.get("reading_ttl_minutes"), "rules.reading_ttl_minutes", 0.0),
            "gap_tolerance_minutes": self._number(
                rules.get("gap_tolerance_minutes"), "rules.gap_tolerance_minutes", 0.0),
            "drift_tolerance_c": self._number(
                rules.get("drift_tolerance_c"), "rules.drift_tolerance_c", 0.0),
            "late_after_minutes": self._number(
                rules.get("late_after_minutes"), "rules.late_after_minutes", 0.0),
            "disposition_policy": normalized_policy,
        }

        return {
            "batch_id": batch_id,
            "samples": normalized_samples,
            "packages": normalized_packages,
            "segments": [{**item, "start": to_utc_iso(item["start"]), "end": to_utc_iso(item["end"])}
                         for item in normalized_segments],
            "zones": normalized_zones,
            "budget": normalized_budget,
            "sensors": [{**item,
                         "calibrated_at": to_utc_iso(item["calibrated_at"]),
                         "valid_from": to_utc_iso(item["valid_from"]),
                         "valid_to": to_utc_iso(item["valid_to"])}
                        for item in normalized_sensors],
            "rules": normalized_rules,
        }

    def create_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                    site_id: str, config: dict[str, Any]):
        """在发运时创建并锁定冷链判定计划。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "site_id": site_id, "config": config}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation.authenticate(connection, actor_id)
            self.foundation.require_role(actor, "admin", "operator")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能为其他组织的场所建立计划")
            plan_id = self._identifier(plan_id, "plan_id")
            normalized = self._validate_config(config)
            config_hash = digest(normalized)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO cc_plans(plan_id,site_id,batch_id,config_json,config_hash,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,'locked',?,?)",
                        (plan_id, site_id, normalized["batch_id"], canonical_json(normalized),
                         config_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("计划编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="coldchain.plan.locked",
                             resource_type="cc_plan", resource_id=plan_id,
                             detail={"site_id": site_id, "batch_id": normalized["batch_id"],
                                     "config_hash": config_hash,
                                     "packages": len(normalized["packages"]),
                                     "segments": len(normalized["segments"])},
                             occurred_at=self._now())
                return "cc_plan", plan_id, {"plan_id": plan_id, "config_hash": config_hash,
                                            "status": "locked"}

            return self.foundation.run_idempotent(
                connection, request_id=request_id, action="coldchain.create_plan",
                payload=payload, create=create)

    # ------------------------------------------------------------------
    # 证据：读数归一化、幂等去重、迟到检测
    # ------------------------------------------------------------------

    def _plan_row(self, connection, plan_id: str):
        row = connection.execute("SELECT * FROM cc_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("冷链判定计划不存在")
        return row

    def record_readings(self, *, request_id: str, actor_id: str, plan_id: str,
                        package_id: str, logger_id: str, readings: list[dict[str, Any]]):
        """上传一个记录器在一个包装上的读数；重复数据幂等去重。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "package_id": package_id,
                   "logger_id": logger_id, "readings": readings}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation.authenticate(connection, actor_id)
            self.foundation.require_role(actor, "admin", "operator")
            plan_row = self._plan_row(connection, plan_id)
            config = json.loads(plan_row["config_json"])
            package = next((item for item in config["packages"]
                            if item["package_id"] == package_id), None)
            if package is None:
                raise ValidationError(f"计划中没有包装 {package_id}")
            if logger_id not in package["logger_ids"]:
                raise ValidationError(f"记录器 {logger_id} 不属于包装 {package_id}")
            sensor = next(item for item in config["sensors"] if item["logger_id"] == logger_id)
            if not isinstance(readings, list) or not readings:
                raise ValidationError("readings 必须是非空列表")
            if len(readings) > 5000:
                raise ValidationError("单次上传不能超过 5000 条读数")

            normalized_readings: dict[str, dict[str, Any]] = {}
            for item in readings:
                if not isinstance(item, dict):
                    raise ValidationError("readings 元素必须是对象")
                raw_observed = str(item.get("observed_at", "")).strip()
                observed_at = parse_observed(raw_observed, sensor["timezone_name"])
                raw_temp = self._number(item.get("temp_c"), "reading.temp_c", -200.0, 200.0)
                key = to_utc_iso(observed_at)
                if key in normalized_readings:
                    if abs(normalized_readings[key]["raw_temp_c"] - raw_temp) > 1e-9:
                        raise ValidationError(f"同一时刻存在冲突读数: {raw_observed}")
                    continue
                corrected, out_of_calibration = correct_temperature(sensor, raw_temp, observed_at)
                normalized_readings[key] = {
                    "observed_at": observed_at, "raw_observed_at": raw_observed,
                    "raw_temp_c": raw_temp, "corrected_temp_c": corrected,
                    "out_of_calibration": out_of_calibration,
                }

            def create():
                upload_id = uuid.uuid4().hex
                received_at = self._now()
                fresh = []
                duplicates = 0
                for key in sorted(normalized_readings):
                    reading = normalized_readings[key]
                    existing = connection.execute(
                        "SELECT * FROM cc_readings WHERE logger_id=? AND observed_at=?",
                        (logger_id, key),
                    ).fetchone()
                    if existing is not None:
                        if abs(existing["raw_temp_c"] - reading["raw_temp_c"]) > 1e-9:
                            raise ConflictError("同一记录器同一时刻已存在不同读数")
                        duplicates += 1
                        continue
                    fresh.append((key, reading))
                connection.execute(
                    "INSERT INTO cc_uploads(upload_id,plan_id,package_id,logger_id,received_at,"
                    "reading_count,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (upload_id, plan_id, package_id, logger_id, received_at,
                     len(fresh), actor_id, self._now()),
                )
                for key, reading in fresh:
                    connection.execute(
                        "INSERT INTO cc_readings(reading_id,upload_id,plan_id,package_id,logger_id,"
                        "observed_at,raw_observed_at,raw_temp_c,corrected_temp_c,out_of_calibration) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, upload_id, plan_id, package_id, logger_id, key,
                         reading["raw_observed_at"], reading["raw_temp_c"],
                         reading["corrected_temp_c"], int(reading["out_of_calibration"])),
                    )
                inserted = len(fresh)
                assessed = connection.execute(
                    "SELECT COUNT(*) AS count FROM cc_assessments WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()["count"]
                requires_reassessment = bool(inserted and assessed)
                append_event(connection, actor_id=actor_id, action="coldchain.readings.recorded",
                             resource_type="cc_upload", resource_id=upload_id,
                             detail={"plan_id": plan_id, "package_id": package_id,
                                     "logger_id": logger_id, "inserted": inserted,
                                     "duplicates": duplicates,
                                     "requires_reassessment": requires_reassessment},
                             occurred_at=self._now())
                return "cc_upload", upload_id, {
                    "upload_id": upload_id, "inserted": inserted, "duplicates": duplicates,
                    "requires_reassessment": requires_reassessment,
                }

            return self.foundation.run_idempotent(
                connection, request_id=request_id, action="coldchain.record_readings",
                payload=payload, create=create)

    # ------------------------------------------------------------------
    # 判断：每次计算生成一个不可改写的版本
    # ------------------------------------------------------------------

    def _load_evidence(self, connection, plan_id: str) -> list[EvidenceReading]:
        rows = connection.execute(
            "SELECT r.*, u.received_at FROM cc_readings r "
            "JOIN cc_uploads u ON u.upload_id = r.upload_id "
            "WHERE r.plan_id=? ORDER BY r.observed_at, r.reading_id",
            (plan_id,),
        ).fetchall()
        return [
            EvidenceReading(
                reading_id=row["reading_id"], upload_id=row["upload_id"],
                package_id=row["package_id"], logger_id=row["logger_id"],
                observed_at=parse_utc(row["observed_at"]),
                corrected_temp_c=float(row["corrected_temp_c"]),
                out_of_calibration=bool(row["out_of_calibration"]),
                received_at=parse_utc(row["received_at"]),
            )
            for row in rows
        ]

    def compute_assessment(self, *, request_id: str, actor_id: str, plan_id: str):
        """基于当前全部证据计算一个新的判断版本；证据未变时不重复建版。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation.authenticate(connection, actor_id)
            self.foundation.require_role(actor, "admin", "operator", "researcher", "quality")
            plan_row = self._plan_row(connection, plan_id)
            config = json.loads(plan_row["config_json"])
            evidence = self._load_evidence(connection, plan_id)
            if not evidence:
                raise ValidationError("没有任何读数证据，无法计算判断")
            config_for_compute = dict(config)
            config_for_compute["segments"] = [
                {**item, "start": parse_utc(item["start"]), "end": parse_utc(item["end"])}
                for item in config["segments"]
            ]
            findings = compute_findings(config_for_compute, evidence, self.clock.now())
            evidence_hash = digest({
                "config_hash": plan_row["config_hash"],
                "reading_ids": sorted(item.reading_id for item in evidence),
            })
            latest = connection.execute(
                "SELECT * FROM cc_assessments WHERE plan_id=? ORDER BY version_no DESC LIMIT 1",
                (plan_id,),
            ).fetchone()

            def create():
                if latest is not None and latest["evidence_hash"] == evidence_hash:
                    return "cc_assessment", latest["assessment_id"], {
                        "assessment_id": latest["assessment_id"],
                        "version_no": latest["version_no"],
                        "outcome": latest["outcome"],
                        "created": False,
                    }
                version_no = (latest["version_no"] if latest else 0) + 1
                assessment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cc_assessments(assessment_id,plan_id,version_no,outcome,"
                    "evidence_hash,findings_json,computed_by,computed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (assessment_id, plan_id, version_no, findings["outcome"], evidence_hash,
                     canonical_json(findings), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="coldchain.assessment.computed",
                             resource_type="cc_assessment", resource_id=assessment_id,
                             detail={"plan_id": plan_id, "version_no": version_no,
                                     "outcome": findings["outcome"],
                                     "evidence_hash": evidence_hash,
                                     "late_evidence": len(findings["late_evidence"]),
                                     "gaps": sum(len(item["gaps"])
                                                 for item in findings["packages"].values()),
                                     "excursions": sum(len(item["excursions"])
                                                      for item in findings["packages"].values())},
                             occurred_at=self._now())
                return "cc_assessment", assessment_id, {
                    "assessment_id": assessment_id, "version_no": version_no,
                    "outcome": findings["outcome"], "created": True,
                }

            return self.foundation.run_idempotent(
                connection, request_id=request_id, action="coldchain.compute_assessment",
                payload=payload, create=create)

    # ------------------------------------------------------------------
    # 处置：科研与质量两个独立角色审批，只产生一个生效版本
    # ------------------------------------------------------------------

    def approve_disposition(self, *, request_id: str, actor_id: str, plan_id: str,
                            assessment_id: str, decision: str, rationale: str):
        """记录一个角色的审批；双方到齐后生成唯一生效的处置版本。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "assessment_id": assessment_id,
                   "decision": decision, "rationale": rationale}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation.authenticate(connection, actor_id)
            self.foundation.require_role(actor, "researcher", "quality")
            approval_role = next(key for key, value in APPROVAL_ROLES.items()
                                 if value == actor.role)
            plan_row = self._plan_row(connection, plan_id)
            if decision not in DECISIONS:
                raise ValidationError("decision 不在允许范围内")
            rationale = self._text(rationale, "rationale", 400)
            assessment = connection.execute(
                "SELECT * FROM cc_assessments WHERE assessment_id=?", (assessment_id,),
            ).fetchone()
            if assessment is None or assessment["plan_id"] != plan_id:
                raise NotFoundError("判断版本不存在")
            latest = connection.execute(
                "SELECT MAX(version_no) AS version_no FROM cc_assessments WHERE plan_id=?",
                (plan_id,),
            ).fetchone()["version_no"]
            if assessment["version_no"] != latest:
                raise ConflictError("只能基于最新判断版本审批；后补证据请先计算新版本")
            plan_config = json.loads(plan_row["config_json"])
            allowed = plan_config["rules"]["disposition_policy"][assessment["outcome"]]
            if decision not in allowed:
                raise ValidationError(f"结论 {assessment['outcome']} 不允许决定 {decision}")

            def create():
                existing = connection.execute(
                    "SELECT * FROM cc_approvals WHERE plan_id=? AND assessment_id=? AND role=?",
                    (plan_id, assessment_id, approval_role),
                ).fetchone()
                if existing is not None:
                    if (existing["decision"] != decision or existing["actor_id"] != actor_id
                            or existing["rationale"] != rationale):
                        raise ConflictError("该角色已提交不同内容的审批；需等待新的判断版本")
                    disposition = connection.execute(
                        "SELECT * FROM cc_dispositions WHERE assessment_id=?", (assessment_id,),
                    ).fetchone()
                    return "cc_approval", existing["approval_id"], {
                        "approval_id": existing["approval_id"], "approval_created": False,
                        "disposition_id": disposition["disposition_id"] if disposition else None,
                        "disposition_version": disposition["version_no"] if disposition else None,
                        "effective_outcome": disposition["outcome"] if disposition else None,
                    }
                approval_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cc_approvals(approval_id,plan_id,assessment_id,role,decision,"
                    "rationale,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (approval_id, plan_id, assessment_id, approval_role, decision,
                     rationale, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="coldchain.approval.recorded",
                             resource_type="cc_approval", resource_id=approval_id,
                             detail={"plan_id": plan_id, "assessment_id": assessment_id,
                                     "role": approval_role, "decision": decision},
                             occurred_at=self._now())
                approvals = {
                    row["role"]: row
                    for row in connection.execute(
                        "SELECT * FROM cc_approvals WHERE plan_id=? AND assessment_id=?",
                        (plan_id, assessment_id),
                    )
                }
                disposition_id = None
                disposition_version = None
                effective_outcome = None
                if len(approvals) == 2:
                    existing_disposition = connection.execute(
                        "SELECT * FROM cc_dispositions WHERE assessment_id=?", (assessment_id,),
                    ).fetchone()
                    if existing_disposition is None:
                        effective_outcome = stricter(approvals["research"]["decision"],
                                                     approvals["quality"]["decision"])
                        last_version = connection.execute(
                            "SELECT MAX(version_no) AS version_no FROM cc_dispositions WHERE plan_id=?",
                            (plan_id,),
                        ).fetchone()["version_no"] or 0
                        disposition_id = uuid.uuid4().hex
                        disposition_version = last_version + 1
                        connection.execute(
                            "INSERT INTO cc_dispositions(disposition_id,plan_id,version_no,"
                            "assessment_id,outcome,research_approval_id,quality_approval_id,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (disposition_id, plan_id, disposition_version, assessment_id,
                             effective_outcome, approvals["research"]["approval_id"],
                             approvals["quality"]["approval_id"], self._now()),
                        )
                        connection.execute(
                            "INSERT INTO cc_disposition_state(plan_id,disposition_id,updated_at) "
                            "VALUES(?,?,?) ON CONFLICT(plan_id) DO UPDATE SET "
                            "disposition_id=excluded.disposition_id, updated_at=excluded.updated_at",
                            (plan_id, disposition_id, self._now()),
                        )
                        append_event(connection, actor_id=actor_id,
                                     action="coldchain.disposition.effective",
                                     resource_type="cc_disposition", resource_id=disposition_id,
                                     detail={"plan_id": plan_id, "version_no": disposition_version,
                                             "assessment_id": assessment_id,
                                             "outcome": effective_outcome},
                                     occurred_at=self._now())
                    else:
                        disposition_id = existing_disposition["disposition_id"]
                        disposition_version = existing_disposition["version_no"]
                        effective_outcome = existing_disposition["outcome"]
                return "cc_approval", approval_id, {
                    "approval_id": approval_id, "approval_created": True,
                    "disposition_id": disposition_id,
                    "disposition_version": disposition_version,
                    "effective_outcome": effective_outcome,
                }

            return self.foundation.run_idempotent(
                connection, request_id=request_id, action="coldchain.approve_disposition",
                payload=payload, create=create)

    # ------------------------------------------------------------------
    # 撤回：旧决定保持原样，生成隔离与通知义务
    # ------------------------------------------------------------------

    def _obligation_specs(self, config: dict[str, Any], outcome: str) -> list[dict[str, str]]:
        specs = []
        batches = sorted({item["batch_id"] for item in config["samples"]})
        for sample in config["samples"]:
            if outcome == "destroy":
                detail = f"确认样品 {sample['sample_id']} 是否已销毁；未销毁的立即转入隔离区"
            else:
                detail = f"将样品 {sample['sample_id']} 移入隔离区并悬挂待判定标识"
            specs.append({"kind": "isolation", "target": sample["sample_id"], "detail": detail})
        for batch_id in batches:
            if outcome == "destroy":
                detail = f"通知质量负责人与承运方：批次 {batch_id} 的销毁决定已撤回，核对实物状态"
            else:
                detail = f"通知批次 {batch_id} 的实验负责人：原处置决定已撤回，暂停使用并回报已使用情况"
            specs.append({"kind": "notification", "target": batch_id, "detail": detail})
        return specs

    def withdraw_disposition(self, *, request_id: str, actor_id: str, plan_id: str, reason: str):
        """撤回当前生效处置；被引用的决定保持原样，义务逐条登记。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation.authenticate(connection, actor_id)
            self.foundation.require_role(actor, "admin", "quality")
            plan_row = self._plan_row(connection, plan_id)
            reason = self._text(reason, "reason", 400)
            state = connection.execute(
                "SELECT * FROM cc_disposition_state WHERE plan_id=?", (plan_id,),
            ).fetchone()
            if state is None:
                raise ConflictError("当前没有生效中的处置决定")
            disposition = connection.execute(
                "SELECT * FROM cc_dispositions WHERE disposition_id=?",
                (state["disposition_id"],),
            ).fetchone()
            if connection.execute(
                    "SELECT 1 FROM cc_withdrawals WHERE disposition_id=?",
                    (disposition["disposition_id"],)).fetchone():
                raise ConflictError("该处置决定已被撤回")

            def create():
                withdrawal_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cc_withdrawals(withdrawal_id,disposition_id,plan_id,reason,"
                    "actor_id,created_at) VALUES(?,?,?,?,?,?)",
                    (withdrawal_id, disposition["disposition_id"], plan_id, reason,
                     actor_id, self._now()),
                )
                connection.execute("DELETE FROM cc_disposition_state WHERE plan_id=?", (plan_id,))
                config = json.loads(plan_row["config_json"])
                obligation_ids = []
                for spec in self._obligation_specs(config, disposition["outcome"]):
                    obligation_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO cc_obligations(obligation_id,plan_id,withdrawal_id,kind,"
                        "target,detail,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
                        (obligation_id, plan_id, withdrawal_id, spec["kind"],
                         spec["target"], spec["detail"], self._now()),
                    )
                    obligation_ids.append(obligation_id)
                append_event(connection, actor_id=actor_id,
                             action="coldchain.disposition.withdrawn",
                             resource_type="cc_disposition",
                             resource_id=disposition["disposition_id"],
                             detail={"plan_id": plan_id, "withdrawal_id": withdrawal_id,
                                     "reason": reason,
                                     "obligations_created": len(obligation_ids)},
                             occurred_at=self._now())
                return "cc_withdrawal", withdrawal_id, {
                    "withdrawal_id": withdrawal_id,
                    "withdrawn_disposition_id": disposition["disposition_id"],
                    "obligations_created": len(obligation_ids),
                    "obligation_ids": obligation_ids,
                }

            return self.foundation.run_idempotent(
                connection, request_id=request_id, action="coldchain.withdraw_disposition",
                payload=payload, create=create)

    def complete_obligation(self, *, request_id: str, actor_id: str,
                            obligation_id: str, note: str):
        """登记一条隔离或通知义务已完成；重复完成保持幂等。"""

        payload = {"actor_id": actor_id, "obligation_id": obligation_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation.authenticate(connection, actor_id)
            self.foundation.require_role(actor, "admin", "operator", "quality")
            note = self._text(note, "note", 400)
            obligation = connection.execute(
                "SELECT * FROM cc_obligations WHERE obligation_id=?", (obligation_id,),
            ).fetchone()
            if obligation is None:
                raise NotFoundError("义务不存在")

            def create():
                if obligation["status"] == "completed":
                    return "cc_obligation", obligation_id, {
                        "obligation_id": obligation_id, "status": "completed",
                        "already_completed": True,
                    }
                connection.execute(
                    "UPDATE cc_obligations SET status='completed', completed_by=?, completed_at=? "
                    "WHERE obligation_id=?",
                    (actor_id, self._now(), obligation_id),
                )
                append_event(connection, actor_id=actor_id,
                             action="coldchain.obligation.completed",
                             resource_type="cc_obligation", resource_id=obligation_id,
                             detail={"plan_id": obligation["plan_id"],
                                     "kind": obligation["kind"],
                                     "target": obligation["target"], "note": note},
                             occurred_at=self._now())
                return "cc_obligation", obligation_id, {
                    "obligation_id": obligation_id, "status": "completed",
                    "already_completed": False,
                }

            return self.foundation.run_idempotent(
                connection, request_id=request_id, action="coldchain.complete_obligation",
                payload=payload, create=create)

    # ------------------------------------------------------------------
    # 报告：引用某个处置版本；被引用的决定不再可能被改写
    # ------------------------------------------------------------------

    def create_report(self, *, request_id: str, actor_id: str, plan_id: str,
                      title: str, disposition_id: str | None = None):
        """登记一份引用处置版本的报告。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "title": title,
                   "disposition_id": disposition_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation.authenticate(connection, actor_id)
            self.foundation.require_role(actor, "admin", "researcher", "quality")
            self._plan_row(connection, plan_id)
            title = self._text(title, "title", 200)
            if disposition_id is None:
                state = connection.execute(
                    "SELECT * FROM cc_disposition_state WHERE plan_id=?", (plan_id,),
                ).fetchone()
                if state is None:
                    raise ConflictError("当前没有生效中的处置决定可供引用")
                disposition_id = state["disposition_id"]
            disposition = connection.execute(
                "SELECT * FROM cc_dispositions WHERE disposition_id=?", (disposition_id,),
            ).fetchone()
            if disposition is None or disposition["plan_id"] != plan_id:
                raise NotFoundError("被引用的处置决定不存在")

            def create():
                report_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cc_reports(report_id,plan_id,title,disposition_id,assessment_id,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (report_id, plan_id, title, disposition_id,
                     disposition["assessment_id"], actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="coldchain.report.cited",
                             resource_type="cc_report", resource_id=report_id,
                             detail={"plan_id": plan_id, "disposition_id": disposition_id,
                                     "assessment_id": disposition["assessment_id"],
                                     "title": title},
                             occurred_at=self._now())
                return "cc_report", report_id, {
                    "report_id": report_id, "disposition_id": disposition_id,
                    "assessment_id": disposition["assessment_id"],
                }

            return self.foundation.run_idempotent(
                connection, request_id=request_id, action="coldchain.create_report",
                payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_plan(self, plan_id: str) -> PlanView:
        row = self.database.connection.execute(
            "SELECT * FROM cc_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("冷链判定计划不存在")
        return PlanView(row["plan_id"], row["site_id"], row["batch_id"],
                        json.loads(row["config_json"]), row["config_hash"],
                        row["status"], row["created_by"], row["created_at"])

    def list_assessments(self, plan_id: str) -> list[AssessmentView]:
        rows = self.database.connection.execute(
            "SELECT * FROM cc_assessments WHERE plan_id=? ORDER BY version_no", (plan_id,),
        ).fetchall()
        return [self._assessment_view(row) for row in rows]

    def get_assessment(self, plan_id: str, version_no: int | None = None) -> AssessmentView:
        if version_no is None:
            row = self.database.connection.execute(
                "SELECT * FROM cc_assessments WHERE plan_id=? ORDER BY version_no DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        else:
            row = self.database.connection.execute(
                "SELECT * FROM cc_assessments WHERE plan_id=? AND version_no=?",
                (plan_id, version_no),
            ).fetchone()
        if row is None:
            raise NotFoundError("判断版本不存在")
        return self._assessment_view(row)

    def _assessment_view(self, row) -> AssessmentView:
        return AssessmentView(row["assessment_id"], row["plan_id"], row["version_no"],
                              row["outcome"], row["evidence_hash"],
                              json.loads(row["findings_json"]),
                              row["computed_by"], row["computed_at"])

    def list_obligations(self, plan_id: str, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM cc_obligations WHERE plan_id=?"
        parameters: list[Any] = [plan_id]
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY created_at, kind, target, obligation_id"
        return [
            {"obligation_id": row["obligation_id"], "withdrawal_id": row["withdrawal_id"],
             "kind": row["kind"], "target": row["target"], "detail": row["detail"],
             "status": row["status"], "completed_by": row["completed_by"],
             "completed_at": row["completed_at"], "created_at": row["created_at"]}
            for row in self.database.connection.execute(query, parameters)
        ]

    def disposition_record(self, plan_id: str) -> dict[str, Any]:
        """组装最终处置记录：暴露账目、共享包装影响、未完成的义务。"""

        connection = self.database.connection
        plan = self.get_plan(plan_id)
        assessments = self.list_assessments(plan_id)
        latest_findings = assessments[-1].findings if assessments else None

        exposure_account = []
        if latest_findings:
            for package_id, package_report in sorted(latest_findings["packages"].items()):
                consumption = package_report["consumption"]
                exposure_account.append({
                    "package_id": package_id,
                    "sample_ids": package_report["sample_ids"],
                    "excursions": package_report["excursions"],
                    "gaps": package_report["gaps"],
                    "consumption": consumption,
                    "budget": latest_findings["budget"],
                })

        disposition_rows = connection.execute(
            "SELECT * FROM cc_dispositions WHERE plan_id=? ORDER BY version_no", (plan_id,),
        ).fetchall()
        state = connection.execute(
            "SELECT * FROM cc_disposition_state WHERE plan_id=?", (plan_id,)).fetchone()
        history = []
        for row in disposition_rows:
            withdrawal = connection.execute(
                "SELECT * FROM cc_withdrawals WHERE disposition_id=?",
                (row["disposition_id"],)).fetchone()
            citations = connection.execute(
                "SELECT report_id, title, created_at FROM cc_reports WHERE disposition_id=? "
                "ORDER BY created_at", (row["disposition_id"],)).fetchall()
            approvals = {
                item["role"]: {"decision": item["decision"], "actor_id": item["actor_id"],
                               "rationale": item["rationale"], "created_at": item["created_at"]}
                for item in connection.execute(
                    "SELECT * FROM cc_approvals WHERE assessment_id=? ORDER BY role",
                    (row["assessment_id"],))
            }
            history.append({
                "disposition_id": row["disposition_id"],
                "version_no": row["version_no"],
                "assessment_id": row["assessment_id"],
                "outcome": row["outcome"],
                "approvals": approvals,
                "created_at": row["created_at"],
                "is_current": bool(state and state["disposition_id"] == row["disposition_id"]),
                "withdrawal": None if withdrawal is None else {
                    "withdrawal_id": withdrawal["withdrawal_id"],
                    "reason": withdrawal["reason"],
                    "actor_id": withdrawal["actor_id"],
                    "created_at": withdrawal["created_at"],
                },
                "cited_by": [{"report_id": item["report_id"], "title": item["title"],
                              "created_at": item["created_at"]} for item in citations],
            })

        latest_assessment = assessments[-1] if assessments else None
        uploads_after = connection.execute(
            "SELECT COUNT(*) AS count FROM cc_uploads WHERE plan_id=? AND received_at>?",
            (plan_id, latest_assessment.computed_at if latest_assessment else ""),
        ).fetchone()["count"]
        total_readings = connection.execute(
            "SELECT COUNT(*) AS count FROM cc_readings WHERE plan_id=?", (plan_id,),
        ).fetchone()["count"]

        outstanding = self.list_obligations(plan_id, status="pending")
        return {
            "plan_id": plan.plan_id,
            "site_id": plan.site_id,
            "batch_id": plan.batch_id,
            "status": plan.status,
            "locked_config": plan.config,
            "assessments": [
                {"assessment_id": item.assessment_id, "version_no": item.version_no,
                 "outcome": item.outcome, "computed_at": item.computed_at,
                 "evidence_hash": item.evidence_hash,
                 "late_evidence_count": len(item.findings["late_evidence"])}
                for item in assessments
            ],
            "latest_outcome": latest_assessment.outcome if latest_assessment else None,
            "evidence_stale": bool(uploads_after) or (latest_assessment is None and total_readings > 0),
            "exposure_account": exposure_account,
            "shared_packaging_impact": (
                latest_findings["impact"]["shared_packages"] if latest_findings else []),
            "affected_sample_ids": (
                latest_findings["impact"]["affected_sample_ids"] if latest_findings else []),
            "drift_findings": latest_findings["drift"] if latest_findings else [],
            "calibration_findings": latest_findings["calibration"] if latest_findings else [],
            "late_evidence_findings": latest_findings["late_evidence"] if latest_findings else [],
            "current_disposition": next(
                (item for item in history if item["is_current"]), None),
            "disposition_history": history,
            "outstanding_obligations": outstanding,
        }
