"""定义冷链判定项目在模块边界使用的数据对象与常量。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: 判定结论：预算内、超预算、证据不足
OUTCOMES = frozenset({"within_budget", "exceeded", "insufficient_evidence"})

#: 处置决定：继续使用、限制用途、销毁；数值越大越严格
DECISIONS = ("continue", "restrict", "destroy")

#: 审批角色与基础服务操作者角色的映射
APPROVAL_ROLES = {"research": "researcher", "quality": "quality"}


def stricter(first: str, second: str) -> str:
    """返回两个处置决定中更严格的一个。"""

    return DECISIONS[max(DECISIONS.index(first), DECISIONS.index(second))]


@dataclass(frozen=True)
class PlanView:
    """发运时已锁定的冷链判定计划。"""

    plan_id: str
    site_id: str
    batch_id: str
    config: dict[str, Any]
    config_hash: str
    status: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class AssessmentView:
    """一次判断版本的只读视图；版本内容生成后不再变化。"""

    assessment_id: str
    plan_id: str
    version_no: int
    outcome: str
    evidence_hash: str
    findings: dict[str, Any]
    computed_by: str
    computed_at: str


@dataclass(frozen=True)
class DispositionView:
    """一个生效处置版本的只读视图；内容一经产生即不可改写。"""

    disposition_id: str
    plan_id: str
    version_no: int
    assessment_id: str
    outcome: str
    research_approval_id: str
    quality_approval_id: str
    created_at: str
