"""提供冷链证据归一化所需的时间解析与格式化工具。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..errors import ValidationError


UTC_OFFSET = re.compile(r"^UTC([+-])(\d{2}):?(\d{2})?$")


def to_utc_iso(value: datetime) -> str:
    """把带时区时间格式化为稳定的 UTC 秒级文本。"""

    if value.tzinfo is None:
        raise ValidationError("时间必须包含时区或提供记录器时区")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """解析服务内部使用的 UTC 文本。"""

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _timezone_for(name: str):
    """把记录器声明的时区名转换为 tzinfo；支持 IANA 名与 UTC±HH:MM。"""

    name = str(name).strip()
    if not name:
        raise ValidationError("记录器时区不能为空")
    try:
        return ZoneInfo(name)
    except Exception:
        match = UTC_OFFSET.fullmatch(name.upper())
        if match:
            sign, hours, minutes = match.group(1), int(match.group(2)), int(match.group(3) or 0)
            delta = timedelta(hours=hours, minutes=minutes)
            return timezone(delta if sign == "+" else -delta)
        raise ValidationError(f"记录器时区无法识别: {name}")


def parse_observed(value: str, timezone_name: str | None) -> datetime:
    """把记录器原始时间解析为 UTC。

    显式带偏移的文本直接解析；裸时间按记录器声明的时区解释。
    """

    text = str(value).strip()
    if not text:
        raise ValidationError("观测时间不能为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"观测时间格式无效: {text}") from exc
    if parsed.tzinfo is None:
        if not timezone_name:
            raise ValidationError("裸时间必须提供记录器时区")
        parsed = parsed.replace(tzinfo=_timezone_for(timezone_name))
    return parsed.astimezone(timezone.utc)
