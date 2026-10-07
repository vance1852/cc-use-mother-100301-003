"""冷链判定项目的 HTTP/JSON 边界，复用基础服务的无框架路由风格。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..api import route as foundation_route
from ..errors import DomainError, ValidationError
from ..service import DomainService
from ..storage import Database
from .schema import COLDCHAIN_SCHEMA
from .service import ColdChainService


def route_coldchain(service: ColdChainService, method: str, path: str,
                    body: dict[str, Any] | None, headers: dict[str, str] | None = None):
    """把冷链判定相关的 HTTP 语义请求分派到冷链服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "POST" and parsed.path == "/coldchain/plans":
            receipt = service.create_plan(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/coldchain/readings":
            receipt = service.record_readings(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/coldchain/assessments":
            receipt = service.compute_assessment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/coldchain/approvals":
            receipt = service.approve_disposition(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/coldchain/withdrawals":
            receipt = service.withdraw_disposition(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/coldchain/obligations/complete":
            receipt = service.complete_obligation(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/coldchain/reports":
            receipt = service.create_report(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/coldchain/plans":
            plan_id = parse_qs(parsed.query).get("plan_id", [""])[0]
            if not plan_id:
                raise ValidationError("plan_id 不能为空")
            return 200, service.get_plan(plan_id).__dict__
        if method == "GET" and parsed.path == "/coldchain/assessments":
            query = parse_qs(parsed.query)
            plan_id = query.get("plan_id", [""])[0]
            if not plan_id:
                raise ValidationError("plan_id 不能为空")
            version = query.get("version_no", [None])[0]
            if version is None:
                return 200, {"items": [item.__dict__ for item in service.list_assessments(plan_id)]}
            return 200, service.get_assessment(plan_id, int(version)).__dict__
        if method == "GET" and parsed.path == "/coldchain/obligations":
            query = parse_qs(parsed.query)
            plan_id = query.get("plan_id", [""])[0]
            if not plan_id:
                raise ValidationError("plan_id 不能为空")
            status = query.get("status", [None])[0]
            return 200, {"items": service.list_obligations(plan_id, status)}
        if method == "GET" and parsed.path == "/coldchain/disposition-record":
            plan_id = parse_qs(parsed.query).get("plan_id", [""])[0]
            if not plan_id:
                raise ValidationError("plan_id 不能为空")
            return 200, service.disposition_record(plan_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def route_combined(foundation: DomainService, coldchain: ColdChainService,
                   method: str, path: str, body: dict[str, Any] | None,
                   headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """先尝试冷链路由，再回落到基础服务路由。"""

    if urlparse(path).path.startswith("/coldchain/"):
        return route_coldchain(coldchain, method, path, body, headers)
    return foundation_route(foundation, method, path, body, headers)


class ColdChainHandler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为组合路由调用。"""

    foundation: DomainService
    coldchain: ColdChainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route_combined(
            self.foundation, self.coldchain, self.command, self.path, body,
            {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动包含冷链判定项目的本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动极地科考站冷链判定服务")
    parser.add_argument("--database", default="coldchain.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database, extra_schema=COLDCHAIN_SCHEMA)
    ColdChainHandler.foundation = DomainService(database)
    ColdChainHandler.coldchain = ColdChainService(database)
    server = ThreadingHTTPServer((args.host, args.port), ColdChainHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
