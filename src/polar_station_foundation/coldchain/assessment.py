"""冷链判定的纯计算核心：归并重叠读数并核算暴露预算。

输入是已经完成时区与校准归一化的读数；输出是稳定的判定发现，
包括缺口、漂移、迟到证据、逐段暴露消耗与影响范围。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta

from .timeutil import parse_utc, to_utc_iso


@dataclass(frozen=True)
class EvidenceReading:
    """一条已归一化的温度读数及其到达时间。"""

    reading_id: str
    upload_id: str
    package_id: str
    logger_id: str
    observed_at: datetime
    corrected_temp_c: float
    out_of_calibration: bool
    received_at: datetime


def correct_temperature(sensor: dict, raw_temp_c: float, observed_at: datetime) -> tuple[float, bool]:
    """按传感器校准档案修正读数，并标记是否超出校准有效期。"""

    offset = float(sensor.get("offset_c", 0.0))
    drift_per_day = float(sensor.get("drift_c_per_day", 0.0))
    calibrated_at = sensor.get("calibrated_at")
    if isinstance(calibrated_at, str):
        calibrated_at = parse_utc(calibrated_at)
    corrected = raw_temp_c + offset
    if calibrated_at and drift_per_day:
        days = (observed_at - calibrated_at).total_seconds() / 86400.0
        corrected += drift_per_day * days
    valid_from = sensor.get("valid_from")
    valid_to = sensor.get("valid_to")
    if isinstance(valid_from, str):
        valid_from = parse_utc(valid_from)
    if isinstance(valid_to, str):
        valid_to = parse_utc(valid_to)
    out_of_calibration = bool(
        (valid_from and observed_at < valid_from) or (valid_to and observed_at > valid_to)
    )
    return round(corrected, 4), out_of_calibration


def _round(value: float) -> float:
    return round(value, 4)


def _minutes(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0


def _merged_intervals(series_by_logger: dict[str, list[tuple[datetime, float]]],
                      start: datetime, end: datetime, ttl: timedelta):
    """把多个记录器的阶梯保持序列归并为统一时间轴。

    每条读数在半开区间 [观测时刻, 观测时刻+ttl) 内有效；每个区间内取
    各记录器存活读数中的最差值（最高温），没有存活记录器的区间即为
    证据缺口。读数到期时刻也作为事件点，否则缺口会被保持值覆盖。
    """

    points = {start, end}
    times: dict[str, list[datetime]] = {}
    for logger_id, series in series_by_logger.items():
        times[logger_id] = [t for t, _ in series]
        for t, _ in series:
            if start - ttl <= t <= end:
                points.add(min(max(t, start), end))
                expiry = t + ttl
                if start < expiry < end:
                    points.add(expiry)
    ordered = sorted(points)
    intervals = []
    for index in range(len(ordered) - 1):
        a, b = ordered[index], ordered[index + 1]
        if b <= a:
            continue
        held: dict[str, float] = {}
        for logger_id, series in series_by_logger.items():
            cursor = bisect_right(times[logger_id], a) - 1
            if cursor >= 0 and a - times[logger_id][cursor] < ttl:
                held[logger_id] = series[cursor][1]
        intervals.append((a, b, held))
    return intervals


def _attribute_segment(segments: list[dict], start: datetime, end: datetime) -> str | None:
    """按最大重叠把一段暴露归属到运输分段。"""

    best_segment, best_overlap = None, 0.0
    for segment in segments:
        overlap = _minutes(max(start, segment["start"]), min(end, segment["end"]))
        if overlap > best_overlap:
            best_segment, best_overlap = segment["segment_id"], overlap
    return best_segment


def _zone_for(zones: list[dict], temp_c: float) -> str:
    for zone in sorted(zones, key=lambda item: item["max_c"]):
        if zone["min_c"] < temp_c <= zone["max_c"]:
            return zone["name"]
    return "out_of_range"


def compute_findings(config: dict, readings: list[EvidenceReading],
                     now: datetime) -> dict:
    """归并证据并核算暴露预算，返回可持久化的判定发现。"""

    rules = config["rules"]
    budget = config["budget"]
    threshold = float(budget["threshold_c"])
    ttl = timedelta(minutes=float(rules["reading_ttl_minutes"]))
    gap_tolerance = float(rules["gap_tolerance_minutes"])
    drift_tolerance = float(rules["drift_tolerance_c"])
    late_after = float(rules["late_after_minutes"])

    segments = [
        {"segment_id": item["segment_id"], "start": item["start"], "end": item["end"]}
        for item in config["segments"]
    ]
    segments.sort(key=lambda item: item["start"])
    packages = {item["package_id"]: item for item in config["packages"]}
    sample_batch = {item["sample_id"]: item.get("batch_id", config["batch_id"])
                    for item in config["samples"]}

    by_package: dict[str, list[EvidenceReading]] = {pid: [] for pid in packages}
    for reading in readings:
        if reading.package_id in by_package:
            by_package[reading.package_id].append(reading)

    package_reports: dict[str, dict] = {}
    drift_findings: list[dict] = []
    calibration_findings: list[dict] = []
    late_findings: list[dict] = []
    affected_packages: set[str] = set()

    for package_id, package in packages.items():
        loggers = list(package["logger_ids"])
        package_readings = sorted(by_package[package_id], key=lambda item: item.observed_at)
        series_by_logger: dict[str, list[tuple[datetime, float]]] = {lid: [] for lid in loggers}
        for reading in package_readings:
            series_by_logger[reading.logger_id].append(
                (reading.observed_at, reading.corrected_temp_c))

        if package_readings:
            window_start = min(segments[0]["start"], package_readings[0].observed_at)
            window_end = max(segments[-1]["end"], package_readings[-1].observed_at)
        else:
            window_start, window_end = segments[0]["start"], segments[-1]["end"]

        # 暴露：在完整证据窗口上归并，连续越限区间合并为一段暴露。
        full_intervals = _merged_intervals(series_by_logger, window_start, window_end, ttl)
        excursions: list[dict] = []
        current: dict | None = None
        for a, b, held in full_intervals:
            merged = max(held.values()) if held else None
            if merged is not None and merged > threshold:
                if current is None:
                    current = {"start": a, "end": b, "peak_c": merged, "degree_minutes": 0.0}
                else:
                    current["end"] = b
                    current["peak_c"] = max(current["peak_c"], merged)
                current["degree_minutes"] += (merged - threshold) * _minutes(a, b)
            elif current is not None:
                excursions.append(current)
                current = None
        if current is not None:
            excursions.append(current)

        consumed_minutes = 0.0
        consumed_degree = 0.0
        excursion_reports = []
        for excursion in excursions:
            duration = _minutes(excursion["start"], excursion["end"])
            degree = _round(excursion["degree_minutes"])
            consumed_minutes += duration
            consumed_degree += excursion["degree_minutes"]
            excursion_reports.append({
                "segment_id": _attribute_segment(segments, excursion["start"], excursion["end"]),
                "zone": _zone_for(config["zones"], excursion["peak_c"]),
                "start": to_utc_iso(excursion["start"]),
                "end": to_utc_iso(excursion["end"]),
                "duration_minutes": _round(duration),
                "peak_c": _round(excursion["peak_c"]),
                "degree_minutes": degree,
                "budget_minutes_consumed": _round(duration),
                "budget_degree_minutes_consumed": degree,
                "remaining_minutes_after": _round(float(budget["max_minutes"]) - consumed_minutes),
                "remaining_degree_minutes_after": _round(
                    float(budget["max_degree_minutes"]) - consumed_degree),
            })
        if excursion_reports:
            affected_packages.add(package_id)

        # 缺口：仅在运输分段窗口内评估没有存活记录器的区间。
        gap_reports = []
        for segment in segments:
            for a, b, held in _merged_intervals(series_by_logger, segment["start"], segment["end"], ttl):
                if held:
                    continue
                duration = _minutes(a, b)
                if duration <= 0:
                    continue
                if gap_reports and gap_reports[-1]["_raw_end"] == a and \
                        gap_reports[-1]["segment_id"] == segment["segment_id"]:
                    gap_reports[-1]["_raw_end"] = b
                    gap_reports[-1]["duration_minutes"] = _round(
                        gap_reports[-1]["duration_minutes"] + duration)
                else:
                    gap_reports.append({"segment_id": segment["segment_id"], "_raw_start": a,
                                        "_raw_end": b, "duration_minutes": _round(duration)})
        gaps = []
        for gap in gap_reports:
            exceeds = gap["duration_minutes"] > gap_tolerance
            gaps.append({
                "segment_id": gap["segment_id"],
                "start": to_utc_iso(gap["_raw_start"]),
                "end": to_utc_iso(gap["_raw_end"]),
                "duration_minutes": gap["duration_minutes"],
                "exceeds_tolerance": exceeds,
            })
            if exceeds:
                affected_packages.add(package_id)

        # 漂移：同包装内记录器两两比较重叠区间的持续偏差。
        pair_stats: dict[tuple[str, str], dict] = {}
        for a, b, held in full_intervals:
            active = sorted(held)
            for i in range(len(active)):
                for j in range(i + 1, len(active)):
                    key = (active[i], active[j])
                    stats = pair_stats.setdefault(
                        key, {"count": 0, "total": 0.0, "first": a, "last": a})
                    stats["count"] += 1
                    stats["total"] += abs(held[key[0]] - held[key[1]])
                    stats["last"] = a
        for (first, second), stats in sorted(pair_stats.items()):
            if stats["count"] < 3:
                continue
            mean_difference = stats["total"] / stats["count"]
            if mean_difference <= drift_tolerance:
                continue
            drift_findings.append({
                "type": "sensor_drift",
                "package_id": package_id,
                "loggers": [first, second],
                "mean_difference_c": _round(mean_difference),
                "compared_intervals": stats["count"],
                "window": {"start": to_utc_iso(stats["first"]), "end": to_utc_iso(stats["last"])},
                "affected_sample_ids": list(package["sample_ids"]),
            })
            affected_packages.add(package_id)

        # 校准：超出校准有效期的读数按记录器汇总。
        expired_by_logger: dict[str, list[EvidenceReading]] = {}
        for reading in package_readings:
            if reading.out_of_calibration:
                expired_by_logger.setdefault(reading.logger_id, []).append(reading)
        for logger_id, expired in sorted(expired_by_logger.items()):
            calibration_findings.append({
                "type": "calibration_expired",
                "package_id": package_id,
                "logger_id": logger_id,
                "reading_count": len(expired),
                "window": {"start": to_utc_iso(expired[0].observed_at),
                           "end": to_utc_iso(expired[-1].observed_at)},
                "affected_sample_ids": list(package["sample_ids"]),
            })
            affected_packages.add(package_id)

        consumed_minutes = _round(consumed_minutes)
        consumed_degree = _round(consumed_degree)
        package_reports[package_id] = {
            "loggers": loggers,
            "sample_ids": list(package["sample_ids"]),
            "coverage": {
                "reading_count": len(package_readings),
                "first_reading": to_utc_iso(package_readings[0].observed_at) if package_readings else None,
                "last_reading": to_utc_iso(package_readings[-1].observed_at) if package_readings else None,
            },
            "excursions": excursion_reports,
            "gaps": gaps,
            "consumption": {
                "minutes": consumed_minutes,
                "degree_minutes": consumed_degree,
                "exceeded_minutes": consumed_minutes > float(budget["max_minutes"]),
                "exceeded_degree_minutes": consumed_degree > float(budget["max_degree_minutes"]),
                "exceeded": consumed_minutes > float(budget["max_minutes"])
                            or consumed_degree > float(budget["max_degree_minutes"]),
            },
        }

    # 迟到证据：到达时间明显晚于观测窗口的上传。
    uploads_seen: dict[str, dict] = {}
    for reading in readings:
        upload = uploads_seen.setdefault(reading.upload_id, {
            "upload_id": reading.upload_id, "package_id": reading.package_id,
            "logger_id": reading.logger_id, "received_at": reading.received_at,
            "first": reading.observed_at, "last": reading.observed_at,
        })
        upload["first"] = min(upload["first"], reading.observed_at)
        upload["last"] = max(upload["last"], reading.observed_at)
        upload["received_at"] = max(upload["received_at"], reading.received_at)
    for upload in sorted(uploads_seen.values(), key=lambda item: item["upload_id"]):
        delay = _minutes(upload["last"], upload["received_at"])
        if delay <= late_after:
            continue
        package = packages.get(upload["package_id"], {"sample_ids": []})
        late_findings.append({
            "type": "late_evidence",
            "upload_id": upload["upload_id"],
            "package_id": upload["package_id"],
            "logger_id": upload["logger_id"],
            "delay_minutes": _round(delay),
            "window": {"start": to_utc_iso(upload["first"]), "end": to_utc_iso(upload["last"])},
            "affected_sample_ids": list(package["sample_ids"]),
        })
        affected_packages.add(upload["package_id"])

    exceeded = any(report["consumption"]["exceeded"] for report in package_reports.values())
    insufficient = any(
        report["coverage"]["reading_count"] == 0
        or any(gap["exceeds_tolerance"] for gap in report["gaps"])
        for report in package_reports.values()
    )
    if exceeded:
        outcome = "exceeded"
    elif insufficient:
        outcome = "insufficient_evidence"
    else:
        outcome = "within_budget"

    affected_samples = sorted({
        sample_id
        for package_id in affected_packages
        for sample_id in packages[package_id]["sample_ids"]
    })
    shared_packages = []
    for package_id, package in sorted(packages.items()):
        batch_ids = sorted({sample_batch.get(sample, config["batch_id"])
                            for sample in package["sample_ids"]})
        if len(batch_ids) > 1:
            shared_packages.append({
                "package_id": package_id,
                "sample_ids": list(package["sample_ids"]),
                "batch_ids": batch_ids,
                "affected": package_id in affected_packages,
            })

    return {
        "computed_at": to_utc_iso(now),
        "outcome": outcome,
        "budget": {
            "threshold_c": threshold,
            "max_minutes": float(budget["max_minutes"]),
            "max_degree_minutes": float(budget["max_degree_minutes"]),
        },
        "packages": package_reports,
        "drift": drift_findings,
        "calibration": calibration_findings,
        "late_evidence": late_findings,
        "impact": {
            "affected_sample_ids": affected_samples,
            "shared_packages": shared_packages,
        },
    }
