from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, StockShortageError
from .rules import (ACTIVE_FIRE_STATES, STATES,
                    fire_is_active, requisition_conflict_detail)


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
                CREATE TABLE IF NOT EXISTS agents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT '',
                    stock REAL NOT NULL DEFAULT 0 CHECK(stock >= 0),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_agents_external_ref
                    ON agents(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS requisitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    field_ticket TEXT NOT NULL UNIQUE,
                    fire_id INTEGER NOT NULL REFERENCES items(id),
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    returned REAL NOT NULL DEFAULT 0
                        CHECK(returned >= 0 AND returned <= quantity),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'returned', 'closed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_requisitions_fire_agent
                    ON requisitions(fire_id, agent_id);
                CREATE TABLE IF NOT EXISTS agent_returns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requisition_id INTEGER NOT NULL
                        REFERENCES requisitions(id) ON DELETE CASCADE,
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

    # ---- 药剂资源台账 ----
    def create_agent(self, name: str, unit: str, stock: float,
                     external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO agents(name, unit, stock, version, external_ref,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,1,?,?,?,?)""",
                    (name, unit, stock, external_ref, actor, now, now),
                )
                agent_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("药剂external_ref已存在") from exc
        return self.get_agent(agent_id)

    def get_agent(self, agent_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            raise NotFoundError("药剂不存在")
        return dict(row)

    def list_agents(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM agents ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def _active_fire_states_sql(self) -> str:
        return ",".join("?" for _ in ACTIVE_FIRE_STATES)

    def submit_requisition(self, field_ticket: str, fire_id: int, agent_id: int,
                           quantity: float, actor: str) -> Dict[str, Any]:
        """现场领用登记。整批补传时同field_ticket重放沿用第一次结果，不重复扣减。"""
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT * FROM requisitions WHERE field_ticket=?", (field_ticket,)
            ).fetchone()
            if existing is not None:
                result = dict(existing)
                result["replayed"] = True
                return result
            fire = self.conn.execute("SELECT status FROM items WHERE id=?", (fire_id,)).fetchone()
            if fire is None:
                raise NotFoundError("火场不存在")
            agent = self.conn.execute("SELECT stock FROM agents WHERE id=?", (agent_id,)).fetchone()
            if agent is None:
                raise NotFoundError("药剂不存在")
            if not fire_is_active(fire["status"]):
                raise ConflictError(f"火场已{fire['status']}，不能再领用药剂")
            placeholders = self._active_fire_states_sql()
            rows = self.conn.execute(
                f"""SELECT r.fire_id AS fire_id,
                           SUM(r.quantity - r.returned) AS outstanding
                      FROM requisitions r
                      JOIN items i ON i.id = r.fire_id
                     WHERE r.agent_id=? AND r.status='active'
                       AND i.status IN ({placeholders})
                     GROUP BY r.fire_id""",
                (agent_id, *sorted(ACTIVE_FIRE_STATES)),
            ).fetchall()
            outstanding = {int(r["fire_id"]): float(r["outstanding"] or 0.0) for r in rows}
            detail = requisition_conflict_detail(float(agent["stock"]), outstanding,
                                                 quantity, fire_id)
            if detail["available"] + 1e-9 < quantity:
                raise StockShortageError("药剂余量不足", detail)
            now = utc_now()
            cur = self.conn.execute(
                """INSERT INTO requisitions(field_ticket, fire_id, agent_id, quantity,
                   returned, status, created_by, created_at)
                   VALUES(?,?,?,?,0,'active',?,?)""",
                (field_ticket, fire_id, agent_id, quantity, actor, now),
            )
            requisition_id = int(cur.lastrowid)
        result = self.get_requisition(requisition_id)
        result["replayed"] = False
        return result

    def get_requisition(self, requisition_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM requisitions WHERE id=?", (requisition_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("现场领用单不存在")
        return dict(row)

    def list_requisitions(self, fire_id: Optional[int] = None,
                          agent_id: Optional[int] = None,
                          active_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM requisitions WHERE 1=1"
        params: list = []
        if fire_id is not None:
            sql += " AND fire_id=?"; params.append(fire_id)
        if agent_id is not None:
            sql += " AND agent_id=?"; params.append(agent_id)
        if active_only:
            sql += " AND status='active'"
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def process_return(self, requisition_id: int, quantity: float,
                       handler: str) -> Dict[str, Any]:
        """药剂交回：记录数量和经办人，未归还量随之恢复。"""
        now = utc_now()
        with self._lock, self.conn:
            req = self.conn.execute(
                "SELECT * FROM requisitions WHERE id=?", (requisition_id,)
            ).fetchone()
            if req is None:
                raise NotFoundError("现场领用单不存在")
            outstanding = float(req["quantity"]) - float(req["returned"])
            if outstanding <= 1e-9:
                raise ConflictError("该领用单药剂已全部归还")
            if quantity - outstanding > 1e-9:
                raise ConflictError(
                    f"交回数量超过未归还量（未归还{outstanding:g}）")
            new_returned = float(req["returned"]) + quantity
            complete = new_returned + 1e-9 >= float(req["quantity"])
            new_status = "returned" if complete else "active"
            self.conn.execute(
                "UPDATE requisitions SET returned=?, status=? WHERE id=?",
                (new_returned, new_status, requisition_id),
            )
            cur = self.conn.execute(
                """INSERT INTO agent_returns(requisition_id, quantity, handler, created_at)
                   VALUES(?,?,?,?)""",
                (requisition_id, quantity, handler, now),
            )
            return_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM agent_returns WHERE id=?", (return_id,)
            ).fetchone()
        return dict(row)

    def list_returns(self, requisition_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM agent_returns"
        params: tuple = ()
        if requisition_id is not None:
            sql += " WHERE requisition_id=?"
            params = (requisition_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def unreturned_for_fire(self, fire_id: int) -> List[Dict[str, Any]]:
        placeholders = self._active_fire_states_sql()
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT r.agent_id AS agent_id, a.name AS name, a.unit AS unit,
                           SUM(r.quantity - r.returned) AS outstanding,
                           GROUP_CONCAT(r.id) AS requisition_ids
                      FROM requisitions r
                      JOIN agents a ON a.id = r.agent_id
                      JOIN items i ON i.id = r.fire_id
                     WHERE r.fire_id=? AND r.status='active'
                       AND i.status IN ({placeholders})
                     GROUP BY r.agent_id
                     HAVING outstanding > 0.0000001
                     ORDER BY r.agent_id""",
                (fire_id, *sorted(ACTIVE_FIRE_STATES)),
            ).fetchall()
        result = []
        for row in rows:
            ids = [int(part) for part in str(row["requisition_ids"]).split(",") if part]
            result.append({"agent_id": int(row["agent_id"]), "name": row["name"],
                           "unit": row["unit"], "outstanding": float(row["outstanding"]),
                           "requisition_ids": ids})
        return result

    def agent_usage(self, active_fire_ids: List[int]) -> List[Dict[str, Any]]:
        """按火场汇总活动领用：agents总量 + 每火场未归还 + 余量。"""
        placeholders = self._active_fire_states_sql()
        sql = f"""SELECT r.agent_id AS agent_id, r.fire_id AS fire_id,
                         SUM(r.quantity - r.returned) AS outstanding
                    FROM requisitions r
                    JOIN items i ON i.id = r.fire_id
                   WHERE r.status='active' AND i.status IN ({placeholders})
                   GROUP BY r.agent_id, r.fire_id
                   ORDER BY r.agent_id, r.fire_id"""
        params: list = [*sorted(ACTIVE_FIRE_STATES)]
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            agents = self.conn.execute("SELECT * FROM agents ORDER BY id").fetchall()
        usage: Dict[int, Dict[int, float]] = {}
        for row in rows:
            usage.setdefault(int(row["agent_id"]), {})[int(row["fire_id"])] = float(
                row["outstanding"] or 0.0)
        wanted = set(active_fire_ids)
        result = []
        for agent in agents:
            by_fire = usage.get(int(agent["id"]), {})
            selected = {fid: qty for fid, qty in by_fire.items() if fid in wanted}
            total_outstanding = sum(by_fire.values())
            stock = float(agent["stock"])
            result.append({
                "agent_id": int(agent["id"]), "name": agent["name"],
                "unit": agent["unit"], "stock": stock,
                "outstanding_total": total_outstanding,
                "available": max(0.0, stock - total_outstanding),
                "by_fire": [{"fire_id": fid, "outstanding": selected[fid]}
                            for fid in sorted(selected)],
            })
        return result

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
