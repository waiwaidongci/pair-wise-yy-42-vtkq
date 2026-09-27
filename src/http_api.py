from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     StockShortageError, ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _path_id(self, segment: str, label: str) -> int:
            try:
                value = int(segment)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{label}必须是正整数") from exc
            if value < 1:
                raise ValidationError(f"{label}必须是正整数")
            return value

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            payload = {"error": exc.__class__.__name__, "message": str(exc)}
            if isinstance(exc, StockShortageError):
                payload["detail"] = exc.detail
            self._json(status, payload)

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                elif path == "/api/agents":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"agents": service.list_agents(role)})
                elif path == "/api/agent-usage":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"usage": service.agent_usage(role)})
                elif path.startswith("/api/agents/"):
                    agent_id = self._path_id(path.rsplit("/", 1)[-1], "agent_id")
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_agent(agent_id, role))
                elif self._fire_requisitions(path):
                    fire_id = self._path_id(path.split("/")[3], "fire_id")
                    actor, role = self._identity()
                    del actor
                    self._json(200, {
                        "requisitions": service.list_requisitions(fire_id, role)})
                elif self._requisition_returns(path):
                    parts = path.split("/")
                    fire_id = self._path_id(parts[3], "fire_id")
                    requisition_id = self._path_id(parts[5], "requisition_id")
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"returns": service.list_returns(
                        fire_id, requisition_id, role)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        @staticmethod
        def _fire_requisitions(path: str) -> bool:
            parts = path.split("/")
            return (len(parts) == 5 and parts[1] == "api"
                    and parts[2] == "fires" and parts[4] == "requisitions")

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path == "/api/agents":
                    self._json(201, service.create_agent(body, actor, role))
                elif self._fire_requisitions(path):
                    fire_id = self._path_id(path.split("/")[3], "fire_id")
                    self._json(201, service.requisition(fire_id, body, actor, role))
                elif self._requisition_returns(path):
                    parts = path.split("/")
                    fire_id = self._path_id(parts[3], "fire_id")
                    requisition_id = self._path_id(parts[5], "requisition_id")
                    self._json(201, service.return_agent(
                        fire_id, requisition_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        @staticmethod
        def _requisition_returns(path: str) -> bool:
            parts = path.split("/")
            return (len(parts) == 7 and parts[1] == "api"
                    and parts[2] == "fires" and parts[4] == "requisitions"
                    and parts[6] == "returns")

    return Handler
