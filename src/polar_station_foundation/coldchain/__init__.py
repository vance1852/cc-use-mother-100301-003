"""冷链判定项目：在基础服务边界上处置温控偏差。"""

from .schema import COLDCHAIN_SCHEMA
from .service import ColdChainService

__all__ = ["COLDCHAIN_SCHEMA", "ColdChainService"]
