from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CHEMICAL_ENTITY, CHEMICAL_ROLES, CREATE_ROLES,
                    ENTITY, RECORD_ROLES, REQUISITION_ROLES, RETURN_ROLES,
                    TITLE, VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)
from .rules import STATES, TERMINAL_STATES


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
        blockers = completion_blockers(
            target, self.repository.open_record_count(item_id),
            self.repository.outstanding_by_item(item_id))
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

    def create_chemical(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, CHEMICAL_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        unit = require_text(payload.get("unit", "升"), "unit", 20)
        total_stock = require_number(payload.get("total_stock"), "total_stock")
        chemical = self.repository.create_chemical(name, unit, total_stock, actor)
        self.repository.append_audit("chemical_create", CHEMICAL_ENTITY,
                                     chemical["id"], actor, {
                                         "name": name, "unit": unit,
                                         "total_stock": total_stock,
                                     })
        return chemical

    def list_chemicals(self, role: str) -> list:
        self._view(role)
        return self.repository.list_chemicals()

    def create_requisition(self, item_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REQUISITION_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("火场已关闭，不能领用药剂")
        chemical_id = payload.get("chemical_id")
        if not isinstance(chemical_id, int) or isinstance(chemical_id, bool) or chemical_id < 1:
            raise ValidationError("chemical_id必须是正整数")
        quantity = require_number(payload.get("quantity"), "quantity")
        if quantity <= 0:
            raise ValidationError("quantity必须大于0")
        field_ref = require_text(payload.get("field_ref"), "field_ref", 100)
        try:
            record, replayed = self.repository.create_requisition(
                chemical_id, item_id, quantity, field_ref, actor)
        except ConflictError as exc:
            self.repository.append_audit("requisition_rejected", ENTITY, item_id,
                                         actor, {"field_ref": field_ref,
                                                 "chemical_id": chemical_id,
                                                 "quantity": quantity,
                                                 "reason": exc.message})
            raise
        record["replayed"] = replayed
        self.repository.append_audit("requisition", ENTITY, item_id, actor, {
            "requisition_id": record["id"], "chemical_id": chemical_id,
            "quantity": quantity, "field_ref": field_ref, "replayed": replayed,
        })
        return record

    def list_requisitions(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_requisitions(item_id)

    def add_return(self, requisition_id: int, payload: Dict[str, Any],
                   actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RETURN_ROLES)
        actor = require_text(actor, "actor", 100)
        quantity = require_number(payload.get("quantity"), "quantity")
        if quantity <= 0:
            raise ValidationError("quantity必须大于0")
        handler = payload.get("handler")
        handler = require_text(handler, "handler", 100) if handler is not None else actor
        record = self.repository.add_return(requisition_id, quantity, handler)
        requisition = record["requisition"]
        self.repository.append_audit("return", ENTITY, requisition["item_id"],
                                     actor, {
                                         "requisition_id": requisition_id,
                                         "return_id": record["id"],
                                         "quantity": quantity, "handler": handler,
                                         "outstanding": requisition["outstanding"],
                                     })
        return record

    def list_returns(self, requisition_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_returns(requisition_id)

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
