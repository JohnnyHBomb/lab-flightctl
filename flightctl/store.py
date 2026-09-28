"""Durable SQLite state for the Flightctl authority.

The store is intentionally boring: all mutation callers obtain an IMMEDIATE
SQLite transaction, and audit/idempotency rows are written in that same
transaction as the entity change.  JSON blobs preserve the frozen wire records
without making the runtime depend on a test helper or a schema package.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


class StoreError(RuntimeError):
    """The durable state could not be opened or committed."""


class StoreUnavailable(StoreError):
    """The database is corrupt or otherwise unavailable."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _load(value: str | bytes | None) -> Any:
    return json.loads(value) if value is not None else None


def utc_text(value: datetime | None = None) -> str:
    value = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


class SQLiteStore:
    """A process-safe wrapper around one SQLite database file."""

    def __init__(self, path: str | Path = ":memory:", *, site_id: str = "site-a", controller_id: str = "controller-a", initialize: bool = True) -> None:
        self.path = str(path)
        self.site_id = site_id
        self.controller_id = controller_id
        self._lock = threading.RLock()
        self.available = True
        self._connection: sqlite3.Connection | None = None
        try:
            self._connection = sqlite3.connect(self.path, timeout=0.25, isolation_level=None, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 250")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            if initialize:
                self._initialize()
        except (sqlite3.DatabaseError, OSError) as exc:
            self.available = False
            if self._connection is not None:
                try:
                    self._connection.close()
                except sqlite3.Error:
                    pass
            self._connection = None
            self.open_error = str(exc)

    @property
    def connection(self) -> sqlite3.Connection:
        if not self.available or self._connection is None:
            raise StoreUnavailable(getattr(self, "open_error", "SQLite store unavailable"))
        return self._connection

    def _initialize(self) -> None:
        connection = self.connection
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lanes (
                lane_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                generation INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
                lease_id TEXT PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                lane_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                principal_key TEXT NOT NULL,
                state TEXT NOT NULL,
                reservation_status TEXT NOT NULL,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS leases_lane_idx ON leases(lane_id, state);
            CREATE INDEX IF NOT EXISTS leases_principal_idx ON leases(principal_key, state);
            CREATE TABLE IF NOT EXISTS bookings (
                booking_id TEXT PRIMARY KEY,
                lane_id TEXT,
                principal_key TEXT NOT NULL,
                state TEXT NOT NULL,
                start_ts REAL NOT NULL,
                end_ts REAL NOT NULL,
                revision INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS bookings_lane_time_idx ON bookings(lane_id, start_ts, end_ts);
            CREATE TABLE IF NOT EXISTS queue_entries (
                queue_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                principal_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                state TEXT NOT NULL,
                predecessor TEXT,
                wait_deadline REAL NOT NULL,
                last_seen REAL NOT NULL,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS queue_lane_order_idx ON queue_entries(lane_id, state, sequence);
            CREATE TABLE IF NOT EXISTS queue_dependencies (
                queue_id TEXT NOT NULL,
                dependency_queue_id TEXT NOT NULL,
                PRIMARY KEY(queue_id, dependency_queue_id),
                FOREIGN KEY(queue_id) REFERENCES queue_entries(queue_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS queue_admissions (
                queue_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                FOREIGN KEY(queue_id) REFERENCES queue_entries(queue_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                expires_ts REAL NOT NULL,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS occupants (
                occupant_id TEXT PRIMARY KEY,
                lane_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                state TEXT NOT NULL,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                request_id TEXT PRIMARY KEY,
                principal_key TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                status INTEGER NOT NULL,
                response_json TEXT NOT NULL,
                stored_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                occurred_ts REAL NOT NULL,
                record_json TEXT NOT NULL
            );
            """
        )
        connection.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')")
        connection.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('site_id',?)", (self.site_id,))
        connection.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('controller_id',?)", (self.controller_id,))

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a transaction, failing rather than waiting indefinitely."""

        connection = self.connection
        with self._lock:
            begun = False
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                begun = True
                yield connection
                connection.execute("COMMIT")
            except Exception:
                if begun:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise

    def close(self) -> None:
        if self._connection is not None:
            with self._lock:
                self._connection.close()
                self._connection = None
                self.available = False

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    def get_meta(self, key: str, *, connection: sqlite3.Connection | None = None) -> str | None:
        row = (connection or self.connection).execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_meta(self, key: str, value: Any, *, connection: sqlite3.Connection | None = None) -> None:
        (connection or self.connection).execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    @staticmethod
    def principal_key(principal: Mapping[str, Any]) -> str:
        return _json({key: principal.get(key) for key in ("site_id", "tenant_id", "issuer", "subject")})

    # ---- lane and lease records -----------------------------------------

    def put_lane(self, lane: Mapping[str, Any], *, connection: sqlite3.Connection | None = None) -> None:
        record = dict(lane)
        lane_id = str(record["lane_id"])
        state = str(record.get("state", "free"))
        generation = int(record.get("generation", 0))
        now = str(record.get("updated_at", utc_text()))
        (connection or self.connection).execute(
            "INSERT INTO lanes(lane_id,state,generation,record_json,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(lane_id) DO UPDATE SET record_json=excluded.record_json, state=excluded.state, generation=excluded.generation, updated_at=excluded.updated_at",
            (lane_id, state, generation, _json(record), now),
        )

    def get_lane(self, lane_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        row = (connection or self.connection).execute("SELECT record_json FROM lanes WHERE lane_id=?", (lane_id,)).fetchone()
        return dict(_load(row[0])) if row else None

    def all_lanes(self, *, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        rows = (connection or self.connection).execute("SELECT record_json FROM lanes ORDER BY lane_id").fetchall()
        return [dict(_load(row[0])) for row in rows]

    def put_lease(self, lease: Mapping[str, Any], *, reservation_status: str = "acknowledged", connection: sqlite3.Connection | None = None) -> None:
        record = dict(lease)
        (connection or self.connection).execute(
            "INSERT INTO leases(lease_id,token,lane_id,generation,principal_key,state,reservation_status,record_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(lease_id) DO UPDATE SET token=excluded.token,lane_id=excluded.lane_id,generation=excluded.generation,principal_key=excluded.principal_key,state=excluded.state,reservation_status=excluded.reservation_status,record_json=excluded.record_json,updated_at=excluded.updated_at",
            (
                record["lease_id"],
                record["token"],
                record["lane"]["lane_id"],
                int(record["generation"]),
                self.principal_key(record["principal"]),
                record.get("state", "starting"),
                reservation_status,
                _json(record),
                utc_text(),
            ),
        )

    def get_lease(self, *, token: str | None = None, lease_id: str | None = None, connection: sqlite3.Connection | None = None) -> tuple[dict[str, Any], str] | None:
        if token is None and lease_id is None:
            raise ValueError("token or lease_id required")
        sql = "SELECT record_json,reservation_status FROM leases WHERE token=?" if token is not None else "SELECT record_json,reservation_status FROM leases WHERE lease_id=?"
        value = token if token is not None else lease_id
        row = (connection or self.connection).execute(sql, (value,)).fetchone()
        return (dict(_load(row[0])), str(row[1])) if row else None

    def leases(self, *, connection: sqlite3.Connection | None = None) -> list[tuple[dict[str, Any], str]]:
        rows = (connection or self.connection).execute("SELECT record_json,reservation_status FROM leases ORDER BY lease_id").fetchall()
        return [(dict(_load(row[0])), str(row[1])) for row in rows]

    # ---- other durable records ------------------------------------------

    def put_booking(self, booking: Mapping[str, Any], *, connection: sqlite3.Connection | None = None) -> None:
        record = dict(booking)
        start = datetime.fromisoformat(str(record["start"]).replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(str(record["end"]).replace("Z", "+00:00")).timestamp()
        lane = record.get("lane")
        (connection or self.connection).execute(
            "INSERT INTO bookings(booking_id,lane_id,principal_key,state,start_ts,end_ts,revision,record_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(booking_id) DO UPDATE SET lane_id=excluded.lane_id,principal_key=excluded.principal_key,state=excluded.state,start_ts=excluded.start_ts,end_ts=excluded.end_ts,revision=excluded.revision,record_json=excluded.record_json,updated_at=excluded.updated_at",
            (record["booking_id"], lane.get("lane_id") if isinstance(lane, Mapping) else None, self.principal_key(record["principal"]), record["state"], start, end, int(record["revision"]), _json(record), utc_text()),
        )

    def get_booking(self, booking_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        row = (connection or self.connection).execute("SELECT record_json FROM bookings WHERE booking_id=?", (booking_id,)).fetchone()
        return dict(_load(row[0])) if row else None

    def all_bookings(self, *, lane_id: str | None = None, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if lane_id is None:
            rows = (connection or self.connection).execute("SELECT record_json FROM bookings ORDER BY start_ts,booking_id").fetchall()
        else:
            rows = (connection or self.connection).execute("SELECT record_json FROM bookings WHERE lane_id=? ORDER BY start_ts,booking_id", (lane_id,)).fetchall()
        return [dict(_load(row[0])) for row in rows]

    def put_queue(self, entry: Mapping[str, Any], *, connection: sqlite3.Connection | None = None) -> None:
        record = dict(entry)
        (connection or self.connection).execute(
            "INSERT INTO queue_entries(queue_id,lane_id,principal_key,sequence,state,predecessor,wait_deadline,last_seen,record_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(queue_id) DO UPDATE SET state=excluded.state,predecessor=excluded.predecessor,wait_deadline=excluded.wait_deadline,last_seen=excluded.last_seen,record_json=excluded.record_json,updated_at=excluded.updated_at",
            (record["queue_id"], record["lane"]["lane_id"], self.principal_key(record["principal"]), int(record["sequence"]), record["state"], record.get("predecessor"), float(record["wait_deadline"]["deadline_s"]), float(record.get("_last_seen_monotonic", record["wait_deadline"].get("monotonic_anchor_s", 0.0))), _json(record), utc_text()),
        )

    def put_queue_dependencies(self, queue_id: str, dependencies: Iterable[str], *, connection: sqlite3.Connection | None = None) -> None:
        db = connection or self.connection
        db.execute("DELETE FROM queue_dependencies WHERE queue_id=?", (queue_id,))
        db.executemany("INSERT INTO queue_dependencies(queue_id,dependency_queue_id) VALUES(?,?)", [(queue_id, str(item)) for item in dependencies])

    def queue_dependencies(self, queue_id: str, *, connection: sqlite3.Connection | None = None) -> list[str]:
        rows = (connection or self.connection).execute("SELECT dependency_queue_id FROM queue_dependencies WHERE queue_id=? ORDER BY dependency_queue_id", (queue_id,)).fetchall()
        return [str(row[0]) for row in rows]

    def get_queue(self, queue_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        row = (connection or self.connection).execute("SELECT record_json FROM queue_entries WHERE queue_id=?", (queue_id,)).fetchone()
        return dict(_load(row[0])) if row else None

    def all_queue(self, *, lane_id: str | None = None, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if lane_id is None:
            rows = (connection or self.connection).execute("SELECT record_json FROM queue_entries ORDER BY sequence,queue_id").fetchall()
        else:
            rows = (connection or self.connection).execute("SELECT record_json FROM queue_entries WHERE lane_id=? ORDER BY sequence,queue_id", (lane_id,)).fetchall()
        return [dict(_load(row[0])) for row in rows]

    def put_queue_admission(self, queue_id: str, admission: Mapping[str, Any], *, connection: sqlite3.Connection | None = None) -> None:
        (connection or self.connection).execute(
            "INSERT INTO queue_admissions(queue_id,record_json) VALUES(?,?) ON CONFLICT(queue_id) DO UPDATE SET record_json=excluded.record_json",
            (str(queue_id), _json(admission)),
        )

    def get_queue_admission(self, queue_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        row = (connection or self.connection).execute("SELECT record_json FROM queue_admissions WHERE queue_id=?", (str(queue_id),)).fetchone()
        return dict(_load(row[0])) if row else None

    def delete_queue_admission(self, queue_id: str, *, connection: sqlite3.Connection | None = None) -> None:
        (connection or self.connection).execute("DELETE FROM queue_admissions WHERE queue_id=?", (str(queue_id),))

    def put_approval(self, approval: Mapping[str, Any], *, connection: sqlite3.Connection | None = None) -> None:
        record = dict(approval)
        expires = datetime.fromisoformat(str(record["expires"]).replace("Z", "+00:00")).timestamp()
        (connection or self.connection).execute(
            "INSERT INTO approvals(approval_id,state,expires_ts,record_json,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(approval_id) DO UPDATE SET state=excluded.state,expires_ts=excluded.expires_ts,record_json=excluded.record_json,updated_at=excluded.updated_at",
            (record["id"], record["state"], expires, _json(record), utc_text()),
        )

    def get_approval(self, approval_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        row = (connection or self.connection).execute("SELECT record_json FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return dict(_load(row[0])) if row else None

    def put_occupant(self, occupant: Mapping[str, Any], *, connection: sqlite3.Connection | None = None) -> None:
        record = dict(occupant)
        (connection or self.connection).execute(
            "INSERT INTO occupants(occupant_id,lane_id,generation,state,record_json,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(occupant_id) DO UPDATE SET state=excluded.state,record_json=excluded.record_json,updated_at=excluded.updated_at",
            (record["occupant_id"], record["lane"]["lane_id"], int(record["generation"]), record["state"], _json(record), utc_text()),
        )

    def get_occupant(self, occupant_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        row = (connection or self.connection).execute("SELECT record_json FROM occupants WHERE occupant_id=?", (occupant_id,)).fetchone()
        return dict(_load(row[0])) if row else None

    def all_occupants(self, *, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        rows = (connection or self.connection).execute("SELECT record_json FROM occupants ORDER BY occupant_id").fetchall()
        return [dict(_load(row[0])) for row in rows]

    def put_batch(self, batch: Mapping[str, Any], *, connection: sqlite3.Connection | None = None) -> None:
        record = dict(batch)
        (connection or self.connection).execute(
            "INSERT INTO batches(batch_id,record_json,updated_at) VALUES(?,?,?) ON CONFLICT(batch_id) DO UPDATE SET record_json=excluded.record_json,updated_at=excluded.updated_at",
            (record["batch_id"], _json(record), utc_text()),
        )

    def get_batch(self, batch_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        row = (connection or self.connection).execute("SELECT record_json FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(_load(row[0])) if row else None

    # ---- idempotency and audit ------------------------------------------

    def get_idempotency(self, request_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        row = (connection or self.connection).execute("SELECT request_id,principal_key,scope_json,fingerprint,status,response_json,stored_at FROM idempotency WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            return None
        return {"request_id": row[0], "principal_key": row[1], "idempotency_scope": _load(row[2]), "request_fingerprint": row[3], "status": row[4], "response": _load(row[5]), "stored_at": row[6]}

    def put_idempotency(self, request_id: str, principal_key: str, scope: Mapping[str, Any], fingerprint: str, status: int, response: Mapping[str, Any], *, connection: sqlite3.Connection | None = None, stored_at: str | None = None) -> None:
        (connection or self.connection).execute(
            "INSERT INTO idempotency(request_id,principal_key,scope_json,fingerprint,status,response_json,stored_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(request_id) DO UPDATE SET principal_key=excluded.principal_key,scope_json=excluded.scope_json,fingerprint=excluded.fingerprint,status=excluded.status,response_json=excluded.response_json,stored_at=excluded.stored_at",
            (request_id, principal_key, _json(scope), fingerprint, int(status), _json(response), stored_at or utc_text()),
        )

    def claim_idempotency(self, request_id: str, principal_key: str, scope: Mapping[str, Any], fingerprint: str, response: Mapping[str, Any], *, connection: sqlite3.Connection | None = None, stored_at: str | None = None) -> bool:
        """Claim a request ID before any external effect can be attempted."""

        cursor = (connection or self.connection).execute(
            "INSERT INTO idempotency(request_id,principal_key,scope_json,fingerprint,status,response_json,stored_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING",
            (request_id, principal_key, _json(scope), fingerprint, 202, _json(response), stored_at or utc_text()),
        )
        return cursor.rowcount == 1

    def finalize_idempotency(self, request_id: str, principal_key: str, fingerprint: str, status: int, response: Mapping[str, Any], *, scope: Mapping[str, Any] | None = None, connection: sqlite3.Connection | None = None, stored_at: str | None = None) -> None:
        cursor = (connection or self.connection).execute(
            "UPDATE idempotency SET scope_json=?,status=?,response_json=?,stored_at=? WHERE request_id=? AND principal_key=? AND fingerprint=?",
            (_json(scope or {"scope": "authenticated-principal"}), int(status), _json(response), stored_at or utc_text(), request_id, principal_key, fingerprint),
        )
        if cursor.rowcount != 1:
            raise StoreError("request ownership was lost before finalization")

    def idempotency_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0])

    def put_event(self, event: Mapping[str, Any], *, occurred_ts: float | None = None, connection: sqlite3.Connection | None = None) -> None:
        record = dict(event)
        # The audit table is deliberately defensive: a raw token anywhere in
        # an event is a programming error, not something to redact later.
        db = connection or self.connection
        token_rows = db.execute("SELECT token FROM leases").fetchall()
        known_tokens = [str(row[0]) for row in token_rows if row[0]]
        if _contains_secret(record, secrets=known_tokens):
            raise StoreError("raw token in audit event")
        db.execute(
            "INSERT INTO events(event_id,occurred_ts,record_json) VALUES(?,?,?)",
            (record["event_id"], float(occurred_ts if occurred_ts is not None else time.time()), _json(record)),
        )

    def events(self, *, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        db = connection or self.connection
        rows = db.execute("SELECT record_json FROM events ORDER BY occurred_ts,event_id").fetchall()
        token_rows = db.execute("SELECT token FROM leases").fetchall()
        known_tokens = [str(row[0]) for row in token_rows if row[0]]
        return [_redact_export(dict(_load(row[0])), secrets=known_tokens) for row in rows]

    def event_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def reservation_count(self, lane_id: str | None = None) -> int:
        if lane_id is None:
            row = self.connection.execute("SELECT COUNT(*) FROM leases WHERE state IN ('starting','running','quarantined')").fetchone()
        else:
            row = self.connection.execute("SELECT COUNT(*) FROM leases WHERE lane_id=? AND state IN ('starting','running','quarantined')", (lane_id,)).fetchone()
        return int(row[0])

    def recover_pending_reservations(self) -> int:
        """Retain exclusion after a crash between reserve and grant commit."""

        changed = 0
        with self.transaction() as connection:
            rows = connection.execute("SELECT lease_id,lane_id,record_json FROM leases WHERE reservation_status='pending'").fetchall()
            for row in rows:
                record = dict(_load(row[2]))
                record["state"] = "quarantined"
                record["reservation"] = dict(record.get("reservation", {}), state="quarantined")
                connection.execute("UPDATE leases SET state='quarantined',reservation_status='uncertain',record_json=?,updated_at=? WHERE lease_id=?", (_json(record), utc_text(), row[0]))
                lane = self.get_lane(str(row[1]), connection=connection)
                if lane is not None:
                    lane["state"] = "quarantined"
                    lane["uncertainty_reason"] = "controller restart during reservation"
                    self.put_lane(lane, connection=connection)
                changed += 1
            pending_rows = connection.execute("SELECT request_id,scope_json FROM idempotency WHERE scope_json LIKE '%\"in_progress\":true%'").fetchall()
            for row in pending_rows:
                request_id = str(row[0])
                response = {"schema": 1, "request_id": request_id, "status": 503, "data": None, "error": {"code": "unknown", "message": "controller restarted during an external effect", "retryable": True, "failure_class": "state"}}
                scope = _load(row[1])
                if not isinstance(scope, dict):
                    scope = {"scope": "authenticated-principal", "controller_id": self.controller_id}
                scope.pop("in_progress", None)
                connection.execute(
                    "UPDATE idempotency SET scope_json=?,status=503,response_json=?,stored_at=? WHERE request_id=?",
                    (_json(scope), _json(response), utc_text(), request_id),
                )
            if changed or pending_rows:
                self.set_meta("recovery_required", "1", connection=connection)
        return changed

    def recovery_required(self) -> bool:
        return self.get_meta("recovery_required") == "1"

    def mark_reconciled(self, *, connection: sqlite3.Connection | None = None) -> None:
        self.set_meta("recovery_required", "0", connection=connection)

    def dump(self) -> dict[str, Any]:
        token_rows = self.connection.execute("SELECT token FROM leases").fetchall()
        known_tokens = [str(row[0]) for row in token_rows if row[0]]
        return _redact_export({
            "lanes": self.all_lanes(),
            "leases": [record for record, _status in self.leases()],
            "bookings": self.all_bookings(),
            "queue": self.all_queue(),
            "approvals": [dict(_load(row[0])) for row in self.connection.execute("SELECT record_json FROM approvals ORDER BY approval_id")],
            "occupants": self.all_occupants(),
            "events": self.events(),
        }, secrets=known_tokens)


def _contains_secret(value: Any, *, secrets: Iterable[str] = ()) -> bool:
    secret_values = tuple(item for item in secrets if item)
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"token", "raw_token", "secret"}:
                return True
            if _contains_secret(item, secrets=secret_values):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_secret(item, secrets=secret_values) for item in value)
    elif isinstance(value, str):
        return any(secret in value for secret in secret_values)
    return False


def _redact_export(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    secret_values = tuple(item for item in secrets if item)
    if isinstance(value, Mapping):
        return {key: _redact_export(item, secrets=secret_values) for key, item in value.items() if str(key).lower() not in {"token", "raw_token", "secret"}}
    if isinstance(value, list):
        return [_redact_export(item, secrets=secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_export(item, secrets=secret_values) for item in value)
    if isinstance(value, str):
        for secret in secret_values:
            value = value.replace(secret, "[redacted]")
    return value


Store = SQLiteStore
Database = SQLiteStore
