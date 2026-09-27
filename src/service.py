from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, NotFoundError, StockShortageError,
                     ValidationError, ensure_role, normalize_severity,
                     require_number, require_positive_int, require_text)
from .repository import Repository
from .rules import (AGENT_CREATE_ROLES, AGENT_ENTITY, AUDIT_ROLES,
                    CREATE_ROLES, ENTITY, RECORD_ROLES, REQUISITION_ENTITY,
                    REQUISITION_ROLES, RETURN_ENTITY, RETURN_ROLES,
                    USAGE_VIEW_ROLES, VIEW_ROLES,
                    completion_blockers, escalation_required, fire_close_blockers,
                    fire_is_active, priority_score, response_deadline_hours,
                    role_for_transition, stock_shortage_message,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if target == "closed":
            blockers = blockers + fire_close_blockers(
                self.repository.unreturned_for_fire(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 药剂台账用例 ----
    def create_agent(self, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, AGENT_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        unit = payload.get("unit", "")
        if unit is None:
            unit = ""
        if not isinstance(unit, str):
            raise ValidationError("unit必须是字符串")
        unit = unit.strip()
        if len(unit) > 20:
            raise ValidationError("unit不能超过20个字符")
        stock = require_number(payload.get("stock", 0), "stock")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        agent = self.repository.create_agent(name, unit, stock, external_ref, actor)
        self.repository.append_audit("create", AGENT_ENTITY, agent["id"], actor, {
            "name": name, "unit": unit, "stock": stock,
        })
        return self.enrich_agent(agent)

    def list_agents(self, role: str) -> list:
        ensure_role(role, USAGE_VIEW_ROLES)
        usage = {row["agent_id"]: row for row in self.agent_usage(role)}
        result = []
        for agent in self.repository.list_agents():
            merged = self.enrich_agent(agent)
            row = usage.get(agent["id"])
            if row is not None:
                merged["outstanding_total"] = row["outstanding_total"]
                merged["available"] = row["available"]
                merged["by_fire"] = row["by_fire"]
            result.append(merged)
        return result

    def get_agent(self, agent_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, USAGE_VIEW_ROLES)
        agent = self.enrich_agent(self.repository.get_agent(agent_id))
        for row in self.agent_usage(role):
            if row["agent_id"] == agent_id:
                agent["outstanding_total"] = row["outstanding_total"]
                agent["available"] = row["available"]
                agent["by_fire"] = row["by_fire"]
        return agent

    def agent_usage(self, role: str) -> list:
        """按火场汇总活动领用与余量。"""
        ensure_role(role, USAGE_VIEW_ROLES)
        active_fires = [item["id"] for item in self.repository.list_items()
                        if fire_is_active(item["status"])]
        usage = self.repository.agent_usage(active_fires)
        for row in usage:
            row["name"] = self.repository.get_agent(row["agent_id"])["name"]
        return usage

    def requisition(self, fire_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        """现场领用：带现场单号，整批补传重放沿用第一次结果。"""
        ensure_role(role, REQUISITION_ROLES)
        actor = require_text(actor, "actor", 100)
        fire_id = require_positive_int(fire_id, "fire_id")
        agent_id = require_positive_int(payload.get("agent_id"), "agent_id")
        field_ticket = require_text(payload.get("field_ticket"), "field_ticket", 100)
        quantity = require_number(payload.get("quantity"), "quantity", 0.000001)
        # 先校验存在性，保证重放单和首次提交拿到一致的404语义
        self.repository.get_item(fire_id)
        agent = self.repository.get_agent(agent_id)
        try:
            result = self.repository.submit_requisition(
                field_ticket, fire_id, agent_id, quantity, actor)
        except StockShortageError as exc:
            raise StockShortageError(
                stock_shortage_message(agent["name"], exc.detail, agent["unit"]),
                exc.detail) from exc
        replayed = result.pop("replayed", False)
        req_id = result["id"]
        if replayed:
            first = self.repository.get_requisition(req_id)
            self.repository.append_audit("requisition_replay", REQUISITION_ENTITY,
                                         req_id, actor, {
                                             "field_ticket": field_ticket,
                                             "first_created_by": first["created_by"],
                                             "first_created_at": first["created_at"],
                                         })
            out = self.enrich_requisition(first)
            out["replayed"] = True
            return out
        self.repository.append_audit("requisition", REQUISITION_ENTITY, req_id, actor, {
            "field_ticket": field_ticket, "fire_id": fire_id,
            "agent_id": agent_id, "quantity": quantity,
        })
        out = self.enrich_requisition(result)
        out["replayed"] = False
        return out

    def list_requisitions(self, fire_id: int, role: str) -> list:
        ensure_role(role, USAGE_VIEW_ROLES)
        self.repository.get_item(fire_id)
        rows = self.repository.list_requisitions(fire_id=fire_id)
        return [self.enrich_requisition(r) for r in rows]

    def return_agent(self, fire_id: int, requisition_id: int,
                     payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        """药剂交回：记录数量和经办人，余量随之恢复。"""
        ensure_role(role, RETURN_ROLES)
        actor = require_text(actor, "actor", 100)
        fire_id = require_positive_int(fire_id, "fire_id")
        requisition_id = require_positive_int(requisition_id, "requisition_id")
        quantity = require_number(payload.get("quantity"), "quantity", 0.000001)
        handler = require_text(payload.get("handler", actor), "handler", 100)
        req = self.repository.get_requisition(requisition_id)
        if req["fire_id"] != fire_id:
            raise NotFoundError("该领用单不属于此火场")
        record = self.repository.process_return(requisition_id, quantity, handler)
        self.repository.append_audit("return", RETURN_ENTITY, record["id"], actor, {
            "requisition_id": requisition_id, "fire_id": fire_id,
            "agent_id": req["agent_id"], "quantity": quantity, "handler": handler,
        })
        return self.enrich_return(record)

    def list_returns(self, fire_id: int, requisition_id: int, role: str) -> list:
        ensure_role(role, USAGE_VIEW_ROLES)
        req = self.repository.get_requisition(requisition_id)
        if req["fire_id"] != fire_id:
            raise NotFoundError("该领用单不属于此火场")
        return [self.enrich_return(r)
                for r in self.repository.list_returns(requisition_id)]

    @staticmethod
    def enrich_agent(agent: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(agent)
        result["outstanding_total"] = 0.0
        result["available"] = float(agent["stock"])
        return result

    def enrich_requisition(self, req: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(req)
        result["outstanding"] = float(req["quantity"]) - float(req["returned"])
        return result

    @staticmethod
    def enrich_return(record: Dict[str, Any]) -> Dict[str, Any]:
        return dict(record)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
