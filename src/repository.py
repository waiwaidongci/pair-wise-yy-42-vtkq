from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (ID_PREFIX, STATES, requisition_conflict_message,
                    requisition_verdict, return_conflict_message,
                    return_verdict)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chemicals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    unit TEXT NOT NULL,
                    total_stock REAL NOT NULL CHECK(total_stock >= 0),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requisitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chemical_id INTEGER NOT NULL REFERENCES chemicals(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    returned_quantity REAL NOT NULL DEFAULT 0,
                    field_ref TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','returned')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chemical_returns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requisition_id INTEGER NOT NULL REFERENCES requisitions(id) ON DELETE CASCADE,
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    handler TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def create_chemical(self, name: str, unit: str, total_stock: float,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO chemicals(name, unit, total_stock, created_by, created_at)
                       VALUES(?,?,?,?,?)""",
                    (name, unit, total_stock, actor, now),
                )
                chemical_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("药剂名称已存在") from exc
        return self.get_chemical(chemical_id)

    def get_chemical(self, chemical_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM chemicals WHERE id=?", (chemical_id,)).fetchone()
        if row is None:
            raise NotFoundError("药剂不存在")
        return dict(row)

    def list_chemicals(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT c.*, COALESCE(SUM(r.quantity - r.returned_quantity), 0) AS checked_out
                   FROM chemicals c
                   LEFT JOIN requisitions r ON r.chemical_id = c.id
                   GROUP BY c.id ORDER BY c.id"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["available"] = item["total_stock"] - item["checked_out"]
            item["scenes"] = self.chemical_holdings(item["id"])
            result.append(item)
        return result

    def chemical_holdings(self, chemical_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.item_id, i.external_ref AS item_ref, i.title AS item_title,
                          SUM(r.quantity - r.returned_quantity) AS outstanding
                   FROM requisitions r JOIN items i ON i.id = r.item_id
                   WHERE r.chemical_id = ?
                   GROUP BY r.item_id
                   HAVING outstanding > 0
                   ORDER BY r.item_id""",
                (chemical_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_requisition(self, chemical_id: int, item_id: int, quantity: float,
                           field_ref: str, actor: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            chemical = self.conn.execute(
                "SELECT * FROM chemicals WHERE id=?", (chemical_id,)).fetchone()
            if chemical is None:
                raise NotFoundError("药剂不存在")
            existing = self.conn.execute(
                "SELECT * FROM requisitions WHERE field_ref=?", (field_ref,)).fetchone()
            if existing is not None:
                return dict(existing), True
            holdings = self.chemical_holdings(chemical_id)
            verdict = requisition_verdict(
                chemical["total_stock"], holdings, item_id, quantity)
            if verdict is not None:
                raise ConflictError(
                    requisition_conflict_message(chemical["name"], chemical["unit"], verdict))
            cur = self.conn.execute(
                """INSERT INTO requisitions(chemical_id, item_id, quantity, returned_quantity,
                   field_ref, status, created_by, created_at)
                   VALUES(?,?,?,0,?,'active',?,?)""",
                (chemical_id, item_id, quantity, field_ref, actor, now),
            )
            requisition_id = int(cur.lastrowid)
        return self.get_requisition(requisition_id), False

    def get_requisition(self, requisition_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """SELECT r.*, c.name AS chemical_name, c.unit AS unit
                   FROM requisitions r JOIN chemicals c ON c.id = r.chemical_id
                   WHERE r.id=?""",
                (requisition_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("领用批次不存在")
        result = dict(row)
        result["outstanding"] = result["quantity"] - result["returned_quantity"]
        return result

    def list_requisitions(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.*, c.name AS chemical_name, c.unit AS unit
                   FROM requisitions r JOIN chemicals c ON c.id = r.chemical_id
                   WHERE r.item_id=? ORDER BY r.id""",
                (item_id,),
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["outstanding"] = entry["quantity"] - entry["returned_quantity"]
            result.append(entry)
        return result

    def add_return(self, requisition_id: int, quantity: float,
                   handler: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                """SELECT r.*, c.name AS chemical_name, c.unit AS unit
                   FROM requisitions r JOIN chemicals c ON c.id = r.chemical_id
                   WHERE r.id=?""",
                (requisition_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("领用批次不存在")
            requisition = dict(row)
            outstanding = requisition["quantity"] - requisition["returned_quantity"]
            verdict = return_verdict(outstanding, quantity)
            if verdict is not None:
                raise ConflictError(return_conflict_message(
                    requisition["chemical_name"], requisition["unit"], verdict))
            cur = self.conn.execute(
                """INSERT INTO chemical_returns(requisition_id, quantity, handler, created_at)
                   VALUES(?,?,?,?)""",
                (requisition_id, quantity, handler, now),
            )
            return_id = int(cur.lastrowid)
            self.conn.execute(
                """UPDATE requisitions
                   SET returned_quantity = returned_quantity + ?,
                       status = CASE WHEN returned_quantity + ? >= quantity
                                     THEN 'returned' ELSE 'active' END
                   WHERE id=?""",
                (quantity, quantity, requisition_id),
            )
        with self._lock:
            saved = self.conn.execute(
                "SELECT * FROM chemical_returns WHERE id=?", (return_id,)).fetchone()
        result = dict(saved)
        result["requisition"] = self.get_requisition(requisition_id)
        return result

    def list_returns(self, requisition_id: int) -> List[Dict[str, Any]]:
        self.get_requisition(requisition_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM chemical_returns WHERE requisition_id=? ORDER BY id",
                (requisition_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def outstanding_by_item(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT c.name, c.unit,
                          SUM(r.quantity - r.returned_quantity) AS outstanding
                   FROM requisitions r JOIN chemicals c ON c.id = r.chemical_id
                   WHERE r.item_id = ?
                   GROUP BY c.id
                   HAVING outstanding > 0
                   ORDER BY c.id""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
