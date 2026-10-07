"""冷链样品温控偏差的评估、双角色审批与处置记录服务。

在基础服务（主体、场所、幂等、审计）的边界上实现冷链判定项目：

- 发运时锁定包装组合、运输分段、允许温区、累计暴露预算、记录器校准与处置规则；
- 运输途中归并多个记录器的重叠读数，识别缺口、漂移与迟到证据及其影响范围；
- 科研与质量两个互相独立的角色基于评估版本决定继续使用、限制用途或销毁；
- 审批只产生一个生效版本，后补记录只形成新的判断版本，被报告引用的决定不被改写；
- 处置记录说明每段暴露如何消耗预算、共享包装波及的样品，以及撤回决定后仍未完成
  的隔离与通知责任。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService

OUTCOMES = frozenset({"continue_use", "restrict_use", "destroy"})
OUTCOME_RANK = {"continue_use": 0, "restrict_use": 1, "destroy": 2}
RULE_EVENTS = frozenset({"over_budget", "gap_overlaps_unfrozen"})
OBLIGATION_TEMPLATE = (
    ("isolate", "将批次样品转入隔离存储并加贴待复审标识"),
    ("notify", "通知接收方、承运方与质量负责人该批次决定已被撤回"),
)


def _parse_instant(value: Any, field: str, default_tz: ZoneInfo | None = None) -> datetime:
    """把 ISO 时间字符串解析为 UTC 时间，缺失时区时按默认时区解释。"""

    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 时间字符串")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"{field} 时间格式无效") from None
    if parsed.tzinfo is None:
        if default_tz is None:
            raise ValidationError(f"{field} 缺少时区信息")
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed.astimezone(timezone.utc)


def _format_instant(value: datetime) -> str:
    """把 UTC 时间格式化为稳定的 ISO 字符串。"""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _minutes(start: datetime, end: datetime) -> float:
    """计算两个时刻之间的分钟数。"""

    return round((end - start).total_seconds() / 60.0, 3)


class ColdChainService:
    """在基础服务边界上实现冷链判定项目。"""

    def __init__(self, foundation: DomainService) -> None:
        self.foundation = foundation
        self.database = foundation.database

    # ---- 基础工具 ----

    def _now(self) -> str:
        return self.foundation._now()

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create) -> tuple[dict[str, Any], bool]:
        """按 request_id 去重写入，重复请求返回首次响应。"""

        request_id = self.foundation._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return json.loads(row["response_json"]), True
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return response, False

    def _check_site_scope(self, connection, actor, site_id: str) -> None:
        row = connection.execute(
            "SELECT organization_id FROM sites WHERE site_id=?", (site_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        if actor.role != "admin" and actor.organization_id != row["organization_id"]:
            raise PermissionDenied("不能操作其他组织的场所")

    def _plan_row(self, connection, site_id: str, batch_id: str):
        row = connection.execute(
            "SELECT * FROM coldchain_plans WHERE site_id=? AND batch_id=?", (site_id, batch_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("该批次尚未锁定发运方案")
        return row

    def _latest_assessment_row(self, connection, site_id: str, batch_id: str):
        return connection.execute(
            "SELECT * FROM coldchain_assessments WHERE site_id=? AND batch_id=? "
            "ORDER BY version DESC LIMIT 1",
            (site_id, batch_id),
        ).fetchone()

    def _latest_decision_row(self, connection, site_id: str, batch_id: str):
        return connection.execute(
            "SELECT * FROM coldchain_decisions WHERE site_id=? AND batch_id=? "
            "ORDER BY version DESC LIMIT 1",
            (site_id, batch_id),
        ).fetchone()

    def _effective_decision_row(self, connection, site_id: str, batch_id: str):
        return connection.execute(
            "SELECT d.* FROM coldchain_effective_decisions e "
            "JOIN coldchain_decisions d ON d.decision_id=e.decision_id "
            "WHERE e.site_id=? AND e.batch_id=?",
            (site_id, batch_id),
        ).fetchone()

    # ---- 发运方案 ----

    def lock_plan(self, *, request_id: str, actor_id: str, site_id: str, batch_id: str,
                  sample_ids: list[str], packages: list[dict[str, Any]],
                  segments: list[dict[str, Any]], zones: list[dict[str, Any]],
                  loggers: list[dict[str, Any]], max_hold_minutes: float,
                  rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """在发运时锁定批次的包装、分段、温区、预算、校准与处置规则。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id,
                   "sample_ids": sample_ids, "packages": packages, "segments": segments,
                   "zones": zones, "loggers": loggers, "max_hold_minutes": max_hold_minutes,
                   "rules": rules}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            self._check_site_scope(connection, actor, site_id)
            batch_id = self.foundation._identifier(batch_id, "batch_id")
            plan = self._normalize_plan(sample_ids=sample_ids, packages=packages,
                                        segments=segments, zones=zones, loggers=loggers,
                                        max_hold_minutes=max_hold_minutes, rules=rules or [])
            plan_hash = digest(plan)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT plan_id, payload_hash FROM coldchain_plans WHERE site_id=? AND batch_id=?",
                    (site_id, batch_id),
                ).fetchone()
                if existing:
                    if existing["payload_hash"] != plan_hash:
                        raise ConflictError("该批次已锁定不同的发运方案")
                    return "coldchain_plan", batch_id, {"batch_id": batch_id, "locked": True,
                                                        "plan_id": existing["plan_id"]}
                plan_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO coldchain_plans(plan_id,site_id,batch_id,payload_json,payload_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (plan_id, site_id, batch_id, canonical_json(plan), plan_hash,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="coldchain.plan_locked",
                             resource_type="coldchain_plan", resource_id=batch_id,
                             detail={"site_id": site_id, "plan_hash": plan_hash,
                                     "samples": len(plan["sample_ids"]),
                                     "packages": len(plan["packages"])},
                             occurred_at=self._now())
                return "coldchain_plan", batch_id, {"batch_id": batch_id, "locked": True,
                                                    "plan_id": plan_id}

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="coldchain.lock_plan",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def _normalize_plan(self, *, sample_ids: Any, packages: Any, segments: Any,
                        zones: Any, loggers: Any, max_hold_minutes: Any,
                        rules: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(sample_ids, list) or not sample_ids:
            raise ValidationError("sample_ids 必须是非空数组")
        samples: list[str] = []
        for item in sample_ids:
            text = str(item).strip()
            if not text or len(text) > 64:
                raise ValidationError("样品编号不能为空且不能超过 64 个字符")
            samples.append(text)
        if len(set(samples)) != len(samples):
            raise ValidationError("样品编号存在重复")
        sample_set = set(samples)

        if not isinstance(loggers, list) or not loggers:
            raise ValidationError("loggers 必须是非空数组")
        logger_map: dict[str, dict[str, Any]] = {}
        for entry in loggers:
            if not isinstance(entry, dict):
                raise ValidationError("记录器配置必须是对象")
            logger_id = self.foundation._identifier(str(entry.get("logger_id", "")), "logger_id")
            if logger_id in logger_map:
                raise ValidationError("记录器编号存在重复")
            timezone_name = str(entry.get("timezone_name", "")).strip()
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValidationError(f"记录器 {logger_id} 的时区无效") from None
            try:
                offset = float(entry.get("calibration_offset"))
            except (TypeError, ValueError):
                raise ValidationError("校准偏移必须是数字") from None
            valid_from = _parse_instant(entry.get("calibration_valid_from"), "calibration_valid_from")
            valid_to = _parse_instant(entry.get("calibration_valid_to"), "calibration_valid_to")
            if valid_from >= valid_to:
                raise ValidationError("校准有效期起点必须早于终点")
            logger_map[logger_id] = {
                "logger_id": logger_id, "timezone_name": timezone_name,
                "calibration_offset": offset,
                "calibration_valid_from": _format_instant(valid_from),
                "calibration_valid_to": _format_instant(valid_to),
            }

        if not isinstance(packages, list) or not packages:
            raise ValidationError("packages 必须是非空数组")
        package_list: list[dict[str, Any]] = []
        seen_samples: set[str] = set()
        seen_packages: set[str] = set()
        for entry in packages:
            if not isinstance(entry, dict):
                raise ValidationError("包装配置必须是对象")
            package_id = self.foundation._identifier(str(entry.get("package_id", "")), "package_id")
            if package_id in seen_packages:
                raise ValidationError("包装编号存在重复")
            seen_packages.add(package_id)
            raw_samples = entry.get("sample_ids")
            if not isinstance(raw_samples, list) or not raw_samples:
                raise ValidationError("包装内必须包含样品")
            package_samples = [str(item).strip() for item in raw_samples]
            if any(item not in sample_set for item in package_samples):
                raise ValidationError("包装包含未登记的样品")
            if seen_packages and seen_samples.intersection(package_samples):
                raise ValidationError("同一样品不能出现在多个包装中")
            seen_samples.update(package_samples)
            raw_loggers = entry.get("logger_ids")
            if not isinstance(raw_loggers, list) or not raw_loggers:
                raise ValidationError("包装必须至少绑定一个记录器")
            logger_ids = [self.foundation._identifier(str(item), "logger_id") for item in raw_loggers]
            if any(item not in logger_map for item in logger_ids):
                raise ValidationError("包装绑定了未登记的记录器")
            package_list.append({"package_id": package_id, "sample_ids": package_samples,
                                 "logger_ids": logger_ids})
        if seen_samples != sample_set:
            raise ValidationError("所有样品都必须装入包装")

        if not isinstance(segments, list) or not segments:
            raise ValidationError("segments 必须是非空数组")
        segment_list: list[dict[str, Any]] = []
        for entry in segments:
            if not isinstance(entry, dict):
                raise ValidationError("运输分段必须是对象")
            name = str(entry.get("name", "")).strip()
            if not name:
                raise ValidationError("运输分段名称不能为空")
            start = _parse_instant(entry.get("start"), "segment.start")
            end = _parse_instant(entry.get("end"), "segment.end")
            if start >= end:
                raise ValidationError("运输分段起点必须早于终点")
            segment_list.append({"name": name, "start": _format_instant(start),
                                 "end": _format_instant(end)})
        segment_list.sort(key=lambda item: item["start"])
        for previous, following in zip(segment_list, segment_list[1:]):
            if previous["end"] > following["start"]:
                raise ValidationError("运输分段时间窗口存在重叠")

        zone_list = self._normalize_zones(zones)

        if not isinstance(max_hold_minutes, (int, float)) or isinstance(max_hold_minutes, bool) \
                or max_hold_minutes <= 0:
            raise ValidationError("max_hold_minutes 必须是正数")

        zone_names = {zone["name"] for zone in zone_list}
        rule_list: list[dict[str, Any]] = []
        for entry in rules:
            if not isinstance(entry, dict):
                raise ValidationError("处置规则必须是对象")
            when = entry.get("when")
            if when not in RULE_EVENTS:
                raise ValidationError("处置规则触发条件无效")
            outcome = entry.get("outcome")
            if outcome not in OUTCOMES:
                raise ValidationError("处置规则结论无效")
            rule: dict[str, Any] = {"when": when, "outcome": outcome}
            if when == "over_budget":
                zone_name = str(entry.get("zone", "")).strip()
                if zone_name not in zone_names:
                    raise ValidationError("处置规则引用了未知温区")
                rule["zone"] = zone_name
            rule_list.append(rule)

        return {"sample_ids": samples, "packages": package_list, "segments": segment_list,
                "zones": zone_list, "loggers": list(logger_map.values()),
                "max_hold_minutes": float(max_hold_minutes), "rules": rule_list}

    def _normalize_zones(self, zones: Any) -> list[dict[str, Any]]:
        if not isinstance(zones, list) or not zones:
            raise ValidationError("zones 必须是非空数组")
        normalized: list[dict[str, Any]] = []
        for entry in zones:
            if not isinstance(entry, dict):
                raise ValidationError("温区配置必须是对象")
            name = str(entry.get("name", "")).strip()
            if not name:
                raise ValidationError("温区名称不能为空")
            try:
                lower = None if entry.get("lower") is None else float(entry.get("lower"))
                upper = None if entry.get("upper") is None else float(entry.get("upper"))
            except (TypeError, ValueError):
                raise ValidationError("温区边界必须是数字或空") from None
            if lower is not None and upper is not None and lower >= upper:
                raise ValidationError("温区下界必须小于上界")
            budget = entry.get("budget_minutes")
            try:
                budget = None if budget is None else float(budget)
            except (TypeError, ValueError):
                raise ValidationError("温区预算必须是数字或空") from None
            if budget is not None and budget < 0:
                raise ValidationError("温区预算不能为负数")
            normalized.append({"name": name, "lower": lower, "upper": upper,
                               "budget_minutes": budget})
        names = [zone["name"] for zone in normalized]
        if len(set(names)) != len(names):
            raise ValidationError("温区名称存在重复")
        ordered = sorted(normalized, key=lambda zone: float("-inf") if zone["lower"] is None else zone["lower"])
        if ordered[0]["lower"] is not None or ordered[-1]["upper"] is not None:
            raise ValidationError("温区必须覆盖全部温度范围")
        for previous, following in zip(ordered, ordered[1:]):
            if previous["upper"] != following["lower"]:
                raise ValidationError("温区必须连续且不重叠")
        return ordered

    # ---- 读数归集 ----

    def ingest_readings(self, *, request_id: str, actor_id: str, site_id: str,
                        batch_id: str, readings: list[dict[str, Any]]) -> dict[str, Any]:
        """归集记录器读数：时区归一、校准修正，重复读数幂等。"""

        if not isinstance(readings, list) or not readings:
            raise ValidationError("readings 必须是非空数组")
        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id,
                   "readings": readings}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator")
            self._check_site_scope(connection, actor, site_id)
            plan = json.loads(self._plan_row(connection, site_id, batch_id)["payload_json"])
            loggers = {entry["logger_id"]: entry for entry in plan["loggers"]}

            def create() -> tuple[str, str, dict[str, Any]]:
                inserted = 0
                duplicated = 0
                for item in readings:
                    if not isinstance(item, dict):
                        raise ValidationError("读数必须是对象")
                    logger_id = self.foundation._identifier(str(item.get("logger_id", "")), "logger_id")
                    logger = loggers.get(logger_id)
                    if logger is None:
                        raise ValidationError("记录器不在发运方案中")
                    observed = _parse_instant(item.get("observed_at"), "observed_at",
                                              ZoneInfo(logger["timezone_name"]))
                    try:
                        raw = float(item.get("temperature"))
                    except (TypeError, ValueError):
                        raise ValidationError("温度读数必须是数字") from None
                    if raw < -150.0 or raw > 100.0:
                        raise ValidationError("温度读数超出合理范围")
                    corrected = round(raw + logger["calibration_offset"], 3)
                    drift = not (_parse_instant(logger["calibration_valid_from"], "valid_from")
                                 <= observed
                                 <= _parse_instant(logger["calibration_valid_to"], "valid_to"))
                    observed_text = _format_instant(observed)
                    reading_hash = digest([logger_id, observed_text, raw, corrected])
                    existing = connection.execute(
                        "SELECT reading_hash FROM coldchain_readings "
                        "WHERE site_id=? AND batch_id=? AND logger_id=? AND observed_at=?",
                        (site_id, batch_id, logger_id, observed_text),
                    ).fetchone()
                    if existing:
                        if existing["reading_hash"] != reading_hash:
                            raise ConflictError("同一记录器同一时间已存在不同读数")
                        duplicated += 1
                        continue
                    connection.execute(
                        "INSERT INTO coldchain_readings(reading_id,site_id,batch_id,logger_id,observed_at,"
                        "raw_temperature,corrected_temperature,drift,ingested_at,reading_hash) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, site_id, batch_id, logger_id, observed_text,
                         raw, corrected, 1 if drift else 0, self._now(), reading_hash),
                    )
                    inserted += 1
                append_event(connection, actor_id=actor_id, action="coldchain.readings_ingested",
                             resource_type="coldchain_batch", resource_id=batch_id,
                             detail={"site_id": site_id, "inserted": inserted,
                                     "duplicated": duplicated},
                             occurred_at=self._now())
                response = {"batch_id": batch_id, "inserted": inserted, "duplicated": duplicated}
                return "coldchain_batch", batch_id, response

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="coldchain.ingest_readings",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    # ---- 暴露评估 ----

    def compute_assessment(self, *, request_id: str, actor_id: str, site_id: str,
                           batch_id: str) -> dict[str, Any]:
        """按当前全部读数计算评估；输入变化时只追加新版本。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator", "researcher", "quality")
            self._check_site_scope(connection, actor, site_id)
            plan = json.loads(self._plan_row(connection, site_id, batch_id)["payload_json"])
            rows = connection.execute(
                "SELECT * FROM coldchain_readings WHERE site_id=? AND batch_id=? "
                "ORDER BY logger_id, observed_at",
                (site_id, batch_id),
            ).fetchall()
            if not rows:
                raise ValidationError("没有可用读数，无法进行评估")
            input_hash = digest([[row["logger_id"], row["observed_at"], row["raw_temperature"],
                                  row["corrected_temperature"]] for row in rows])
            latest = self._latest_assessment_row(connection, site_id, batch_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if latest is not None and latest["input_hash"] == input_hash:
                    stored = json.loads(latest["result_json"])
                    response = {"assessment_id": latest["assessment_id"],
                                "version": latest["version"], "created": False,
                                "recommended_outcome": stored["recommended_outcome"]}
                    return "coldchain_assessment", latest["assessment_id"], response
                version = 1 if latest is None else latest["version"] + 1
                result = self._evaluate(plan, rows, latest, version)
                assessment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO coldchain_assessments(assessment_id,site_id,batch_id,version,"
                    "input_hash,result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (assessment_id, site_id, batch_id, version, input_hash,
                     canonical_json(result), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="coldchain.assessment_computed",
                             resource_type="coldchain_assessment", resource_id=assessment_id,
                             detail={"site_id": site_id, "batch_id": batch_id, "version": version,
                                     "recommended_outcome": result["recommended_outcome"],
                                     "late_evidence": len(result["late_evidence"]),
                                     "gaps": len(result["gaps"]), "drift": len(result["drift"])},
                             occurred_at=self._now())
                response = {"assessment_id": assessment_id, "version": version, "created": True,
                            "recommended_outcome": result["recommended_outcome"]}
                return "coldchain_assessment", assessment_id, response

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="coldchain.compute_assessment",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def get_assessment(self, *, actor_id: str, site_id: str, batch_id: str,
                       version: int | None = None) -> dict[str, Any]:
        """读取指定（默认最新）评估版本的完整结果。"""

        connection = self.database.connection
        actor = self.foundation._actor(connection, actor_id)
        self._check_site_scope(connection, actor, site_id)
        self._plan_row(connection, site_id, batch_id)
        if version is None:
            row = self._latest_assessment_row(connection, site_id, batch_id)
        else:
            row = connection.execute(
                "SELECT * FROM coldchain_assessments WHERE site_id=? AND batch_id=? AND version=?",
                (site_id, batch_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError("评估版本不存在")
        return {"assessment_id": row["assessment_id"], "version": row["version"],
                "created_by": row["created_by"], "created_at": row["created_at"],
                "result": json.loads(row["result_json"])}

    def _evaluate(self, plan: dict[str, Any], rows: list[Any],
                  previous: Any, version: int) -> dict[str, Any]:
        """归并重叠读数并计算预算消耗、缺口、漂移与迟到证据。"""

        zones = plan["zones"]
        budgets = {zone["name"]: zone["budget_minutes"] for zone in zones
                   if zone["budget_minutes"] is not None}
        segments = [{"name": item["name"],
                     "start": _parse_instant(item["start"], "segment.start"),
                     "end": _parse_instant(item["end"], "segment.end")}
                    for item in plan["segments"]]
        max_hold_seconds = plan["max_hold_minutes"] * 60.0
        logger_package: dict[str, str] = {}
        package_samples: dict[str, list[str]] = {}
        for package in plan["packages"]:
            package_samples[package["package_id"]] = list(package["sample_ids"])
            for logger_id in package["logger_ids"]:
                logger_package[logger_id] = package["package_id"]
        readings = [{"logger_id": row["logger_id"],
                     "observed": _parse_instant(row["observed_at"], "observed_at"),
                     "corrected": row["corrected_temperature"],
                     "drift": bool(row["drift"]),
                     "ingested_at": _parse_instant(row["ingested_at"], "ingested_at")}
                    for row in rows]

        packages_out: dict[str, Any] = {}
        gaps_all: list[dict[str, Any]] = []
        drift_all: list[dict[str, Any]] = []
        rule_hits: list[dict[str, Any]] = []
        batch_rank = 0
        for package in plan["packages"]:
            package_id = package["package_id"]
            logger_ids = set(package["logger_ids"])
            package_readings = [item for item in readings if item["logger_id"] in logger_ids]
            intervals = self._merged_intervals(package_readings, segments, max_hold_seconds)
            intervals = self._coalesce(intervals, zones)
            consumption: dict[str, float] = {}
            for interval in intervals:
                if interval["kind"] == "exposure":
                    zone = interval["zone"]
                    consumption[zone] = round(consumption.get(zone, 0.0) + interval["minutes"], 3)
            gaps: list[dict[str, Any]] = []
            for index, interval in enumerate(intervals):
                if interval["kind"] != "gap":
                    continue
                neighbors = [intervals[index - 1] if index > 0 else None,
                             intervals[index + 1] if index + 1 < len(intervals) else None]
                overlaps = any(neighbor is not None and neighbor["kind"] == "exposure"
                               and neighbor.get("zone") in budgets for neighbor in neighbors)
                entry = {"package_id": package_id, "segment": interval["segment"],
                         "start": interval["start"], "end": interval["end"],
                         "minutes": interval["minutes"], "overlaps_unfrozen": overlaps,
                         "sample_ids": list(package["sample_ids"])}
                gaps.append(entry)
                gaps_all.append(entry)
            drift_by_logger: dict[str, list[dict[str, Any]]] = {}
            for item in package_readings:
                if item["drift"]:
                    drift_by_logger.setdefault(item["logger_id"], []).append(item)
            for logger_id, items in sorted(drift_by_logger.items()):
                drift_all.append({
                    "package_id": package_id, "logger_id": logger_id,
                    "first_observed": _format_instant(min(item["observed"] for item in items)),
                    "last_observed": _format_instant(max(item["observed"] for item in items)),
                    "count": len(items), "sample_ids": list(package["sample_ids"]),
                })
            recommendation = "continue_use"
            over_budget: list[str] = []
            for zone_name, budget in budgets.items():
                if consumption.get(zone_name, 0.0) > budget:
                    over_budget.append(zone_name)
                    outcome = self._rule_outcome(plan["rules"], "over_budget", zone_name)
                    rule_hits.append({"package_id": package_id, "when": "over_budget",
                                      "zone": zone_name, "outcome": outcome})
                    if OUTCOME_RANK[outcome] > OUTCOME_RANK[recommendation]:
                        recommendation = outcome
            if any(gap["overlaps_unfrozen"] for gap in gaps):
                outcome = self._rule_outcome(plan["rules"], "gap_overlaps_unfrozen", None)
                rule_hits.append({"package_id": package_id, "when": "gap_overlaps_unfrozen",
                                  "outcome": outcome})
                if OUTCOME_RANK[outcome] > OUTCOME_RANK[recommendation]:
                    recommendation = outcome
            remaining = {zone: round(budget - consumption.get(zone, 0.0), 3)
                         for zone, budget in budgets.items()}
            packages_out[package_id] = {
                "sample_ids": list(package["sample_ids"]),
                "logger_ids": list(package["logger_ids"]),
                "intervals": intervals,
                "consumption_minutes": consumption,
                "remaining_minutes": remaining,
                "over_budget": over_budget,
                "recommended_outcome": recommendation,
            }
            batch_rank = max(batch_rank, OUTCOME_RANK[recommendation])

        late: list[dict[str, Any]] = []
        if previous is not None:
            threshold = _parse_instant(previous["created_at"], "created_at")
            for item in readings:
                if item["ingested_at"] > threshold:
                    package_id = logger_package.get(item["logger_id"])
                    late.append({"package_id": package_id, "logger_id": item["logger_id"],
                                 "observed_at": _format_instant(item["observed"]),
                                 "ingested_at": _format_instant(item["ingested_at"]),
                                 "sample_ids": package_samples.get(package_id, [])})

        excursion_samples: set[str] = set()
        for package_id, data in packages_out.items():
            if any(interval["kind"] == "exposure" and interval.get("zone") in budgets
                   for interval in data["intervals"]):
                excursion_samples.update(data["sample_ids"])
        affected = {
            "excursion": sorted(excursion_samples),
            "gap": sorted({sample for gap in gaps_all for sample in gap["sample_ids"]}),
            "drift": sorted({sample for item in drift_all for sample in item["sample_ids"]}),
            "late": sorted({sample for item in late for sample in item["sample_ids"]}),
        }
        rank_outcome = {rank: outcome for outcome, rank in OUTCOME_RANK.items()}
        return {
            "version": version,
            "packages": packages_out,
            "budgets_minutes": budgets,
            "gaps": gaps_all,
            "drift": drift_all,
            "late_evidence": late,
            "affected_samples": affected,
            "rule_hits": rule_hits,
            "recommended_outcome": rank_outcome[batch_rank],
        }

    def _merged_intervals(self, readings: list[dict[str, Any]],
                          segments: list[dict[str, Any]],
                          max_hold_seconds: float) -> list[dict[str, Any]]:
        """按最坏温度归并同一包装内多个记录器的重叠读数。"""

        by_logger: dict[str, list[dict[str, Any]]] = {}
        for item in readings:
            by_logger.setdefault(item["logger_id"], []).append(item)
        for items in by_logger.values():
            items.sort(key=lambda item: item["observed"])
        intervals: list[dict[str, Any]] = []
        for segment in segments:
            boundaries = {segment["start"], segment["end"]}
            for items in by_logger.values():
                for item in items:
                    if segment["start"] < item["observed"] < segment["end"]:
                        boundaries.add(item["observed"])
                    expiry = item["observed"] + timedelta(seconds=max_hold_seconds)
                    if segment["start"] < expiry < segment["end"]:
                        boundaries.add(expiry)
            ordered = sorted(boundaries)
            for start, end in zip(ordered, ordered[1:]):
                candidates = []
                for items in by_logger.values():
                    latest = None
                    for item in items:
                        if item["observed"] <= start:
                            latest = item
                        else:
                            break
                    if latest is not None \
                            and (start - latest["observed"]).total_seconds() < max_hold_seconds:
                        candidates.append(latest)
                if candidates:
                    intervals.append({
                        "segment": segment["name"], "start": start, "end": end,
                        "kind": "exposure",
                        "temperature": max(item["corrected"] for item in candidates),
                        "drift": any(item["drift"] for item in candidates),
                        "loggers": sorted({item["logger_id"] for item in candidates}),
                    })
                else:
                    intervals.append({"segment": segment["name"], "start": start, "end": end,
                                      "kind": "gap", "loggers": []})
        return intervals

    def _coalesce(self, intervals: list[dict[str, Any]],
                  zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把相邻且性质相同的区间合并为可报告的暴露分段。"""

        merged: list[dict[str, Any]] = []
        for interval in intervals:
            if interval["kind"] == "exposure":
                zone = self._zone_of(zones, interval["temperature"])
                key = ("exposure", zone, interval["drift"], interval["segment"])
            else:
                zone = None
                key = ("gap", interval["segment"])
            if merged and merged[-1]["_key"] == key and merged[-1]["_end"] == interval["start"]:
                last = merged[-1]
                last["_end"] = interval["end"]
                if interval["kind"] == "exposure":
                    last["temperature_max"] = max(last["temperature_max"], interval["temperature"])
                    last["loggers"] = sorted(set(last["loggers"]) | set(interval["loggers"]))
                continue
            entry: dict[str, Any] = {
                "_key": key, "_start": interval["start"], "_end": interval["end"],
                "segment": interval["segment"], "kind": interval["kind"],
                "loggers": list(interval["loggers"]),
            }
            if interval["kind"] == "exposure":
                entry["zone"] = zone
                entry["temperature_max"] = round(interval["temperature"], 3)
                entry["drift"] = interval["drift"]
            merged.append(entry)
        result = []
        for entry in merged:
            start = entry.pop("_start")
            end = entry.pop("_end")
            entry.pop("_key")
            entry["start"] = _format_instant(start)
            entry["end"] = _format_instant(end)
            entry["minutes"] = _minutes(start, end)
            result.append(entry)
        return result

    @staticmethod
    def _zone_of(zones: list[dict[str, Any]], temperature: float) -> str:
        for zone in zones:
            lower_ok = zone["lower"] is None or temperature >= zone["lower"]
            upper_ok = zone["upper"] is None or temperature < zone["upper"]
            if lower_ok and upper_ok:
                return zone["name"]
        raise ValidationError("温区未覆盖全部温度范围")

    @staticmethod
    def _rule_outcome(rules: list[dict[str, Any]], when: str, zone: str | None) -> str:
        for rule in rules:
            if rule["when"] == when and (zone is None or rule.get("zone") == zone):
                return rule["outcome"]
        return "restrict_use"

    # ---- 双角色审批 ----

    def submit_determination(self, *, request_id: str, actor_id: str, site_id: str,
                             batch_id: str, outcome: str, rationale: str,
                             assessment_id: str | None = None) -> dict[str, Any]:
        """科研或质量角色基于最新评估提交判断，双方一致才产生生效版本。"""

        if outcome not in OUTCOMES:
            raise ValidationError("判断结论必须是 continue_use、restrict_use 或 destroy")
        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id,
                   "outcome": outcome, "rationale": rationale, "assessment_id": assessment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, "researcher", "quality")
            self._check_site_scope(connection, actor, site_id)
            self._plan_row(connection, site_id, batch_id)
            rationale = self.foundation._text(rationale, "rationale", 500)
            assessment = self._latest_assessment_row(connection, site_id, batch_id)
            if assessment is None:
                raise ValidationError("尚未形成评估版本，不能提交判断")
            if assessment_id is not None and assessment_id != assessment["assessment_id"]:
                raise ConflictError("评估已有更新版本，判断必须基于最新评估")
            effective = self._effective_decision_row(connection, site_id, batch_id)
            if effective is not None and effective["assessment_id"] == assessment["assessment_id"]:
                raise ConflictError("当前评估已有生效决定，如需改变请先撤回")
            side = "research" if actor.role == "researcher" else "quality"
            latest_round = self._latest_decision_row(connection, site_id, batch_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                round_row = latest_round
                if round_row is not None and round_row["status"] == "pending" \
                        and round_row["assessment_id"] != assessment["assessment_id"]:
                    connection.execute(
                        "UPDATE coldchain_decisions SET status='superseded' WHERE decision_id=?",
                        (round_row["decision_id"],),
                    )
                    round_row = None
                if round_row is not None and round_row["status"] == "pending":
                    if round_row[f"{side}_actor"] is not None:
                        raise ConflictError("该角色已在本轮提交过判断")
                    connection.execute(
                        f"UPDATE coldchain_decisions SET {side}_actor=?, {side}_outcome=?, "
                        f"{side}_rationale=?, {side}_at=? WHERE decision_id=?",
                        (actor_id, outcome, rationale, now, round_row["decision_id"]),
                    )
                    decision_id = round_row["decision_id"]
                    version = round_row["version"]
                else:
                    version = 1 if latest_round is None else latest_round["version"] + 1
                    decision_id = uuid.uuid4().hex
                    columns = ("decision_id,site_id,batch_id,version,assessment_id,"
                               f"{side}_actor,{side}_outcome,{side}_rationale,{side}_at,"
                               "status,created_by,created_at")
                    connection.execute(
                        f"INSERT INTO coldchain_decisions({columns}) "
                        "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
                        (decision_id, site_id, batch_id, version, assessment["assessment_id"],
                         actor_id, outcome, rationale, now, actor_id, now),
                    )
                row = connection.execute(
                    "SELECT * FROM coldchain_decisions WHERE decision_id=?", (decision_id,)
                ).fetchone()
                status = row["status"]
                final_outcome = None
                if row["research_outcome"] is not None and row["quality_outcome"] is not None:
                    if row["research_outcome"] == row["quality_outcome"]:
                        final_outcome = row["research_outcome"]
                        connection.execute(
                            "UPDATE coldchain_decisions SET status='superseded' "
                            "WHERE site_id=? AND batch_id=? AND status='effective'",
                            (site_id, batch_id),
                        )
                        connection.execute(
                            "UPDATE coldchain_decisions SET status='effective', outcome=?, "
                            "finalized_at=? WHERE decision_id=?",
                            (final_outcome, now, decision_id),
                        )
                        connection.execute(
                            "INSERT INTO coldchain_effective_decisions(site_id,batch_id,decision_id,updated_at) "
                            "VALUES(?,?,?,?) ON CONFLICT(site_id,batch_id) DO UPDATE SET "
                            "decision_id=excluded.decision_id, updated_at=excluded.updated_at",
                            (site_id, batch_id, decision_id, now),
                        )
                        append_event(connection, actor_id=actor_id,
                                     action="coldchain.decision_effective",
                                     resource_type="coldchain_decision", resource_id=decision_id,
                                     detail={"site_id": site_id, "batch_id": batch_id,
                                             "version": version, "outcome": final_outcome,
                                             "assessment_id": assessment["assessment_id"]},
                                     occurred_at=now)
                        status = "effective"
                    else:
                        connection.execute(
                            "UPDATE coldchain_decisions SET status='disagreed' WHERE decision_id=?",
                            (decision_id,),
                        )
                        append_event(connection, actor_id=actor_id,
                                     action="coldchain.decision_disagreed",
                                     resource_type="coldchain_decision", resource_id=decision_id,
                                     detail={"site_id": site_id, "batch_id": batch_id,
                                             "version": version,
                                             "research_outcome": row["research_outcome"],
                                             "quality_outcome": row["quality_outcome"]},
                                     occurred_at=now)
                        status = "disagreed"
                append_event(connection, actor_id=actor_id,
                             action="coldchain.determination_submitted",
                             resource_type="coldchain_decision", resource_id=decision_id,
                             detail={"site_id": site_id, "batch_id": batch_id,
                                     "version": version, "side": side, "outcome": outcome},
                             occurred_at=now)
                response = {"decision_id": decision_id, "version": version,
                            "status": status, "outcome": final_outcome}
                return "coldchain_decision", decision_id, response

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="coldchain.submit_determination",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def withdraw_decision(self, *, request_id: str, actor_id: str, site_id: str,
                          batch_id: str, reason: str) -> dict[str, Any]:
        """撤回当前生效决定，并生成隔离与通知两项后续责任。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "quality")
            self._check_site_scope(connection, actor, site_id)
            self._plan_row(connection, site_id, batch_id)
            reason = self.foundation._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                effective = self._effective_decision_row(connection, site_id, batch_id)
                if effective is None:
                    raise ConflictError("当前没有生效决定可以撤回")
                now = self._now()
                connection.execute(
                    "UPDATE coldchain_decisions SET status='withdrawn', withdrawn_by=?, "
                    "withdrawn_at=?, withdraw_reason=? WHERE decision_id=?",
                    (actor_id, now, reason, effective["decision_id"]),
                )
                connection.execute(
                    "DELETE FROM coldchain_effective_decisions WHERE site_id=? AND batch_id=?",
                    (site_id, batch_id),
                )
                report_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM coldchain_reports WHERE decision_id=?",
                    (effective["decision_id"],),
                ).fetchone()["count"]
                append_event(connection, actor_id=actor_id, action="coldchain.decision_withdrawn",
                             resource_type="coldchain_decision",
                             resource_id=effective["decision_id"],
                             detail={"site_id": site_id, "batch_id": batch_id,
                                     "version": effective["version"], "reason": reason,
                                     "referenced_by_reports": report_count},
                             occurred_at=now)
                obligations = []
                for kind, detail_text in OBLIGATION_TEMPLATE:
                    obligation_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO coldchain_obligations(obligation_id,site_id,batch_id,decision_id,"
                        "kind,detail,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (obligation_id, site_id, batch_id, effective["decision_id"],
                         kind, detail_text, "open", now),
                    )
                    append_event(connection, actor_id=actor_id,
                                 action="coldchain.obligation_created",
                                 resource_type="coldchain_obligation", resource_id=obligation_id,
                                 detail={"site_id": site_id, "batch_id": batch_id, "kind": kind,
                                         "decision_id": effective["decision_id"]},
                                 occurred_at=now)
                    obligations.append({"obligation_id": obligation_id, "kind": kind,
                                        "detail": detail_text})
                response = {"decision_id": effective["decision_id"], "status": "withdrawn",
                            "referenced_by_reports": report_count, "obligations": obligations}
                return "coldchain_decision", effective["decision_id"], response

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="coldchain.withdraw_decision",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def complete_obligation(self, *, request_id: str, actor_id: str,
                            obligation_id: str) -> dict[str, Any]:
        """把一条隔离或通知责任标记为完成。"""

        payload = {"actor_id": actor_id, "obligation_id": obligation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "operator", "quality")
            row = connection.execute(
                "SELECT * FROM coldchain_obligations WHERE obligation_id=?", (obligation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("后续责任不存在")
            self._check_site_scope(connection, actor, row["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "done":
                    raise ConflictError("该后续责任已经完成")
                now = self._now()
                connection.execute(
                    "UPDATE coldchain_obligations SET status='done', completed_by=?, "
                    "completed_at=? WHERE obligation_id=?",
                    (actor_id, now, obligation_id),
                )
                append_event(connection, actor_id=actor_id,
                             action="coldchain.obligation_completed",
                             resource_type="coldchain_obligation", resource_id=obligation_id,
                             detail={"site_id": row["site_id"], "batch_id": row["batch_id"],
                                     "kind": row["kind"]},
                             occurred_at=now)
                response = {"obligation_id": obligation_id, "kind": row["kind"], "status": "done"}
                return "coldchain_obligation", obligation_id, response

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="coldchain.complete_obligation",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    # ---- 报告与处置记录 ----

    def create_report(self, *, request_id: str, actor_id: str, site_id: str,
                      batch_id: str, title: str) -> dict[str, Any]:
        """生成引用当前生效决定的处置报告，报告中的决定快照不再变化。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id,
                   "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self.foundation._actor(connection, actor_id)
            self.foundation._require(actor, "admin", "quality")
            self._check_site_scope(connection, actor, site_id)
            self._plan_row(connection, site_id, batch_id)
            title = self.foundation._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                effective = self._effective_decision_row(connection, site_id, batch_id)
                if effective is None:
                    raise ConflictError("没有生效决定，无法生成处置报告")
                assessment = connection.execute(
                    "SELECT version FROM coldchain_assessments WHERE assessment_id=?",
                    (effective["assessment_id"],),
                ).fetchone()
                snapshot = {"decision_id": effective["decision_id"],
                            "decision_version": effective["version"],
                            "outcome": effective["outcome"],
                            "assessment_id": effective["assessment_id"],
                            "assessment_version": assessment["version"],
                            "finalized_at": effective["finalized_at"]}
                report_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO coldchain_reports(report_id,site_id,batch_id,decision_id,title,"
                    "snapshot_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (report_id, site_id, batch_id, effective["decision_id"], title,
                     canonical_json(snapshot), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="coldchain.report_created",
                             resource_type="coldchain_report", resource_id=report_id,
                             detail={"site_id": site_id, "batch_id": batch_id,
                                     "decision_id": effective["decision_id"],
                                     "decision_version": effective["version"]},
                             occurred_at=self._now())
                response = {"report_id": report_id,
                            "decision_id": effective["decision_id"],
                            "decision_version": effective["version"],
                            "outcome": effective["outcome"]}
                return "coldchain_report", report_id, response

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="coldchain.create_report",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def disposition_record(self, *, actor_id: str, site_id: str,
                           batch_id: str) -> dict[str, Any]:
        """汇总批次的最终处置记录：预算消耗、共享包装影响、决定历史与未尽责任。"""

        connection = self.database.connection
        actor = self.foundation._actor(connection, actor_id)
        self._check_site_scope(connection, actor, site_id)
        plan_row = self._plan_row(connection, site_id, batch_id)
        plan = json.loads(plan_row["payload_json"])

        assessment_row = self._latest_assessment_row(connection, site_id, batch_id)
        assessment = None
        result: dict[str, Any] | None = None
        if assessment_row is not None:
            result = json.loads(assessment_row["result_json"])
            assessment = {"assessment_id": assessment_row["assessment_id"],
                          "version": assessment_row["version"],
                          "created_at": assessment_row["created_at"],
                          "recommended_outcome": result["recommended_outcome"],
                          "gaps": result["gaps"], "drift": result["drift"],
                          "late_evidence": result["late_evidence"],
                          "affected_samples": result["affected_samples"],
                          "rule_hits": result["rule_hits"]}

        budgets = (result or {}).get("budgets_minutes", {})
        budget_accounting: list[dict[str, Any]] = []
        exposure_segments: list[dict[str, Any]] = []
        shared_packaging: list[dict[str, Any]] = []
        for package in plan["packages"]:
            package_id = package["package_id"]
            data = (result or {}).get("packages", {}).get(package_id)
            intervals = data["intervals"] if data else []
            running: dict[str, float] = {}
            for interval in intervals:
                entry = {"package_id": package_id, "segment": interval["segment"],
                         "kind": interval["kind"], "start": interval["start"],
                         "end": interval["end"], "minutes": interval["minutes"],
                         "loggers": interval["loggers"]}
                if interval["kind"] == "exposure":
                    zone = interval["zone"]
                    running[zone] = round(running.get(zone, 0.0) + interval["minutes"], 3)
                    entry.update({"zone": zone,
                                  "temperature_max": interval["temperature_max"],
                                  "drift": interval["drift"],
                                  "zone_budget_minutes": budgets.get(zone),
                                  "zone_consumed_cumulative": running[zone]})
                exposure_segments.append(entry)
            if data:
                for zone, budget in budgets.items():
                    consumed = data["consumption_minutes"].get(zone, 0.0)
                    budget_accounting.append({
                        "package_id": package_id, "zone": zone,
                        "consumed_minutes": consumed, "budget_minutes": budget,
                        "remaining_minutes": round(budget - consumed, 3),
                        "over_budget": consumed > budget,
                    })
            shared_packaging.append({
                "package_id": package_id,
                "sample_ids": list(package["sample_ids"]),
                "logger_ids": list(package["logger_ids"]),
                "excursion": any(item["kind"] == "exposure" and item.get("zone") in budgets
                                 for item in intervals),
                "gap": any(item["kind"] == "gap" for item in intervals),
                "drift": any(item["kind"] == "exposure" and item.get("drift")
                             for item in intervals),
                "recommended_outcome": data["recommended_outcome"] if data else None,
            })

        decision_rows = connection.execute(
            "SELECT * FROM coldchain_decisions WHERE site_id=? AND batch_id=? "
            "ORDER BY version DESC",
            (site_id, batch_id),
        ).fetchall()
        effective = self._effective_decision_row(connection, site_id, batch_id)
        report_rows = connection.execute(
            "SELECT * FROM coldchain_reports WHERE site_id=? AND batch_id=? "
            "ORDER BY created_at, report_id",
            (site_id, batch_id),
        ).fetchall()
        obligation_rows = connection.execute(
            "SELECT * FROM coldchain_obligations WHERE site_id=? AND batch_id=? "
            "ORDER BY created_at, obligation_id",
            (site_id, batch_id),
        ).fetchall()

        def obligation_view(row: Any) -> dict[str, Any]:
            return {"obligation_id": row["obligation_id"], "kind": row["kind"],
                    "detail": row["detail"], "status": row["status"],
                    "decision_id": row["decision_id"], "created_at": row["created_at"],
                    "completed_by": row["completed_by"], "completed_at": row["completed_at"]}

        open_obligations = [obligation_view(row) for row in obligation_rows
                            if row["status"] == "open"]
        return {
            "site_id": site_id,
            "batch_id": batch_id,
            "plan": {"locked_by": plan_row["created_by"], "locked_at": plan_row["created_at"],
                     "sample_ids": plan["sample_ids"], "packages": plan["packages"],
                     "segments": plan["segments"], "zones": plan["zones"],
                     "loggers": plan["loggers"], "max_hold_minutes": plan["max_hold_minutes"],
                     "rules": plan["rules"]},
            "assessment": assessment,
            "budget_accounting": budget_accounting,
            "exposure_segments": exposure_segments,
            "shared_packaging": shared_packaging,
            "decisions": {
                "effective": self._decision_view(effective) if effective else None,
                "history": [self._decision_view(row) for row in decision_rows],
            },
            "reports": [{"report_id": row["report_id"], "title": row["title"],
                         "created_by": row["created_by"], "created_at": row["created_at"],
                         "decision_snapshot": json.loads(row["snapshot_json"])}
                        for row in report_rows],
            "obligations": {
                "open": open_obligations,
                "completed": [obligation_view(row) for row in obligation_rows
                              if row["status"] == "done"],
            },
            "outstanding_obligations": open_obligations,
        }

    @staticmethod
    def _decision_view(row: Any) -> dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "version": row["version"],
            "assessment_id": row["assessment_id"],
            "status": row["status"],
            "outcome": row["outcome"],
            "research": {"actor_id": row["research_actor"], "outcome": row["research_outcome"],
                         "rationale": row["research_rationale"], "at": row["research_at"]},
            "quality": {"actor_id": row["quality_actor"], "outcome": row["quality_outcome"],
                        "rationale": row["quality_rationale"], "at": row["quality_at"]},
            "created_at": row["created_at"],
            "finalized_at": row["finalized_at"],
            "withdrawn_by": row["withdrawn_by"],
            "withdrawn_at": row["withdrawn_at"],
            "withdraw_reason": row["withdraw_reason"],
        }
