"""Durable owner-scoped events, operation receipts and signed HTTPS delivery.

AirSprint has no audited push feed. The collector reads collection endpoints;
it never polls booked trip/leg detail endpoints or writes to AirSprint.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from airsprint_agent import AgentError, canonical, validate_object

MAX_BODY = 262144


def default_db_path() -> Path:
    return Path(os.getenv("AIRSPRINT_AGENT_DB", str(Path.home() / ".airsprint-agent" / "state.sqlite3")))


def iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def event_definitions() -> list[dict]:
    definitions = []
    for name, description, filters in [
        ("notification.created", "A new AirSprint in-app notification, including any app notification category; read its text with events get.", ["resourceId", "tripId", "action"]),
        ("notification.updated", "An existing notification changed, including read status.", ["resourceId", "tripId", "action"]),
        ("trip.created", "A trip leg appeared in the owner's upcoming-trip collection.", ["legId", "bookingId"]),
        ("trip.updated", "A trip leg changed, including schedule, route, status or passenger information.", ["legId", "bookingId"]),
        ("customs.submitted", "A declaration appeared for a monitored leg. One event per declaration.", ["legId"]),
        ("operation.completed", "An agent write finished; inspect its durable receipt by operation key.", ["command"]),
        ("operation.uncertain", "A write may have succeeded. Reconcile it before attempting another write.", ["command"]),
    ]:
        schema = {"type": "object", "properties": {key: {"type": "string"} for key in filters}, "additionalProperties": False}
        if name == "customs.submitted":
            schema["required"] = ["legId"]
        definitions.append({"name": name, "description": description, "delivery": ["webhook"], "inputSchema": schema,
                            "payloadSchema": {"type": "object", "properties": {
                                "resourceId": {"type": "string"}, "legId": {"type": "string"},
                                "bookingId": {"type": "string"}, "command": {"type": "string"},
                                "tripId": {"type": "string"}, "action": {"type": "string"},
                                "operationKey": {"type": "string"}, "status": {"type": "string"},
                                "changedFields": {"type": "array", "items": {"type": "string"}},
                            }, "required": ["resourceId"], "additionalProperties": False}})
    return definitions


def validate_event(name: str, arguments: dict) -> None:
    definition = next((d for d in event_definitions() if d["name"] == name), None)
    if definition is None:
        raise AgentError("Unknown event.", "not_found")
    validate_object(arguments, definition["inputSchema"])
    if any(not value.strip() or len(value) > 200 for value in arguments.values()):
        raise AgentError("Event filters must be nonempty identifiers, at most 200 characters.")


def decode_secret(secret: str) -> bytes:
    try:
        if not isinstance(secret, str) or not secret.startswith("whsec_"):
            raise ValueError()
        key = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError()
        return key
    except (ValueError, TypeError) as exc:
        raise AgentError("Signing secret must be whsec_ followed by base64 encoding of 24–64 bytes.") from exc


def signed_headers(body: bytes, event_id: str, subscription_id: str, secret: str, old_secret: str | None = None) -> dict:
    timestamp = str(int(time.time()))
    message = event_id.encode() + b"." + timestamp.encode() + b"." + body
    signatures = ["v1," + base64.b64encode(hmac.new(decode_secret(key), message, hashlib.sha256).digest()).decode()
                  for key in [secret, old_secret] if key]
    return {"Content-Type": "application/json", "webhook-id": event_id, "webhook-timestamp": timestamp,
            "webhook-signature": " ".join(signatures), "X-MCP-Subscription-Id": subscription_id}


def callback_destination(url: str) -> tuple:
    try:
        if not isinstance(url, str) or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        port = parsed.port or 443
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError()
        for entry in addresses:
            address = ipaddress.ip_address(entry[4][0])
            if (not address.is_global or address.is_multicast or address.is_reserved
                    or (address.version == 6 and (address.is_site_local or address.sixtofour or address.teredo
                        or address in ipaddress.ip_network("64:ff9b::/96")
                        or address in ipaddress.ip_network("64:ff9b:1::/48")
                        or (address.ipv4_mapped and not address.ipv4_mapped.is_global)))):
                raise ValueError()
        return parsed, addresses[0]
    except (ValueError, OSError) as exc:
        raise AgentError("Callback must resolve exclusively to public HTTPS addresses.", "callback_invalid") from exc


def https_post(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    """Resolve once, validate every address, pin the connection and verify TLS host."""
    if len(body) > MAX_BODY:
        raise AgentError("Event exceeds 256 KiB.")
    parsed, address = callback_destination(url)
    family, socktype, proto, _, sockaddr = address
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=10)
    raw = socket.socket(family, socktype, proto)
    try:
        raw.settimeout(10)
        raw.connect(sockaddr)
        connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(MAX_BODY + 1)
    finally:
        connection.close()
        raw.close()


class EventStore:
    def __init__(self, path: Path | str, principal: str):
        self.path, self.principal = Path(path).expanduser().absolute(), principal
        if not principal:
            raise AgentError("An owner principal is required.")
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, mode=0o700)
        if self.path.is_symlink():
            raise AgentError("State database cannot be a symbolic link.")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    owner TEXT, key TEXT, command TEXT, fingerprint TEXT, state TEXT, result TEXT, updated REAL,
                    PRIMARY KEY(owner,key));
                CREATE TABLE IF NOT EXISTS subscriptions (
                    owner TEXT, id TEXT PRIMARY KEY, name TEXT, arguments TEXT, url TEXT, secret TEXT,
                    old_secret TEXT, rotate_until REAL, expires REAL, active INTEGER);
                CREATE TABLE IF NOT EXISTS verifications (owner TEXT, url TEXT, expires REAL, PRIMARY KEY(owner,url));
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, id TEXT UNIQUE, body TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS deliveries (owner TEXT, subscription TEXT, event TEXT, state TEXT,
                    attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0, lease_until REAL DEFAULT 0,
                    PRIMARY KEY(subscription,event));
                CREATE TABLE IF NOT EXISTS snapshots (owner TEXT, source TEXT, id TEXT, body TEXT, digest TEXT,
                    PRIMARY KEY(owner,source,id));
                CREATE TABLE IF NOT EXISTS baselines (owner TEXT, source TEXT, updated REAL, PRIMARY KEY(owner,source));
            """)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin_operation(self, key: str, command: str, fingerprint: str) -> tuple[str, dict | None]:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE owner=? AND key=?", (self.principal, key)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise AgentError("Operation key already belongs to different arguments.", "operation_conflict")
                if row["state"] in {"executing", "uncertain"}:
                    raise AgentError("Previous write is pending or uncertain; inspect its receipt and reconcile it. Do not retry with a new key.", "operation_uncertain")
                return key, json.loads(row["result"])
            pending = db.execute("SELECT key FROM operations WHERE owner=? AND fingerprint=? AND state IN ('executing','uncertain')",
                                 (self.principal, fingerprint)).fetchone()
            if pending:
                raise AgentError("These arguments already have a pending or uncertain operation.", "operation_uncertain", details={"operationKey": pending["key"]})
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?)", (self.principal, key, command, fingerprint, "executing", None, time.time()))
        return key, None

    def finish_operation(self, key: str, result: dict, trace: list) -> None:
        has_effect = any(t.get("effect") == "write" and t.get("outcome") in {"uncertain", "accepted"} for t in trace)
        uncertain = result.get("status") == "error" and (has_effect or result.get("requestMayHaveSucceeded") is True)
        if uncertain:
            result.update(requestMayHaveSucceeded=True, retryable=False,
                          nextAction="Inspect and reconcile this operation. Do not repeat the write.")
        state = "uncertain" if uncertain else "completed"
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT command FROM operations WHERE owner=? AND key=?", (self.principal, key)).fetchone()
            db.execute("UPDATE operations SET state=?,result=?,updated=? WHERE owner=? AND key=?",
                       (state, canonical(result), time.time(), self.principal, key))
            self._emit(db, "operation." + state, {"resourceId": key, "operationKey": key, "command": row[0], "status": result["status"]})

    def operation(self, key: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT key,command,state,result,updated FROM operations WHERE owner=? AND key=?", (self.principal, key)).fetchone()
        if not row:
            raise AgentError("Operation not found.", "not_found")
        return {**dict(row), "result": json.loads(row["result"]) if row["result"] else None}

    def _emit(self, db, name: str, data: dict, timestamp: str | None = None) -> str:
        event_id = "evt_" + secrets.token_hex(16)
        body = canonical({"eventId": event_id, "name": name, "timestamp": timestamp or iso(), "data": data, "cursor": None})
        if len(body.encode()) > MAX_BODY:
            raise AgentError("Event exceeds 256 KiB.")
        db.execute("INSERT INTO events(owner,id,body,created) VALUES (?,?,?,?)", (self.principal, event_id, body, time.time()))
        for sub in db.execute("SELECT * FROM subscriptions WHERE owner=? AND active=1 AND expires>? AND name=?", (self.principal, time.time(), name)):
            if all(data.get(k) == v for k, v in json.loads(sub["arguments"]).items()):
                db.execute("INSERT INTO deliveries(owner,subscription,event,state) VALUES (?,?,?,'pending')", (self.principal, sub["id"], event_id))
        return event_id

    def observe(self, source: str, items: list[dict], *, created: str, updated: str | None = None) -> int:
        """Commit complete collection snapshots and their outbox entries atomically."""
        emitted = 0
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            baseline = db.execute("SELECT 1 FROM baselines WHERE owner=? AND source=?", (self.principal, source)).fetchone()
            for item in items:
                resource_id = str(item["id"])
                body = canonical(stable_record(item))
                digest = hashlib.sha256(body.encode()).hexdigest()
                prior = db.execute("SELECT body,digest FROM snapshots WHERE owner=? AND source=? AND id=?", (self.principal, source, resource_id)).fetchone()
                name = created if prior is None else updated if prior["digest"] != digest else None
                if baseline and name:
                    previous = json.loads(prior["body"]) if prior else {}
                    changed = sorted(k for k in set(item) | set(previous) if stable_record(item).get(k) != previous.get(k))
                    data = {"resourceId": resource_id, "changedFields": changed}
                    for key in ("legId", "bookingId", "tripId", "action"):
                        if isinstance(item.get(key), str):
                            data[key] = item[key]
                    if source == "trips":
                        data["legId"] = resource_id
                    if source.startswith("customs:"):
                        data["legId"] = source.split(":", 1)[1]
                    self._emit(db, name, data, source_time(item.get("createdAt" if prior is None else "updatedAt")))
                    emitted += 1
                db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)", (self.principal, source, resource_id, body, digest))
            db.execute("INSERT OR REPLACE INTO baselines VALUES (?,?,?)", (self.principal, source, time.time()))
        return emitted

    def subscribe(self, name: str, arguments: dict, delivery: dict, ttl_ms=None, *, sender=https_post) -> dict:
        validate_event(name, arguments)
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook" or set(delivery) != {"mode", "url", "secret"}:
            raise AgentError("Only webhook delivery with url and secret is supported.")
        url, secret = delivery["url"], delivery["secret"]
        decode_secret(secret)
        callback_destination(url)
        if ttl_ms is not None and (type(ttl_ms) is not int or ttl_ms <= 0):
            raise AgentError("ttlMs must be a positive integer or null.")
        lifetime = min(86400, ttl_ms / 1000) if ttl_ms is not None else 86400
        sub_id = self.subscription_id(name, arguments, url)
        with self.db() as db:
            verified = db.execute("SELECT expires FROM verifications WHERE owner=? AND url=?", (self.principal, url)).fetchone()
            if name == "customs.submitted" and not db.execute("SELECT 1 FROM snapshots WHERE owner=? AND source='trips' AND id=?", (self.principal, arguments["legId"])).fetchone():
                raise AgentError("Collect upcoming trips first; this leg is not in the connected owner's observed trips.", "not_found")
        if not verified or verified[0] < time.time():
            challenge = secrets.token_urlsafe(32)
            body = canonical({"type": "verification", "challenge": challenge}).encode()
            try:
                code, response = sender(url, body, signed_headers(body, "msg_verification_" + secrets.token_hex(16), sub_id, secret))
                parsed = json.loads(response)
                echoed = parsed.get("challenge") if isinstance(parsed, dict) else None
                if not 200 <= code < 300 or not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
                    raise ValueError()
            except (OSError, ValueError, http.client.HTTPException) as exc:
                reason = "timeout" if isinstance(exc, TimeoutError) else "challenge_failed"
                raise AgentError("Callback verification failed.", "callback_error", details={"reason": reason}) from exc
            with self.db() as db:
                db.execute("INSERT OR REPLACE INTO verifications VALUES (?,?,?)", (self.principal, url, time.time() + 300))
        expires = time.time() + lifetime
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT secret,old_secret,rotate_until FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
            old_secret = old["secret"] if old and old["secret"] != secret else old["old_secret"] if old else None
            rotation = time.time() + 300 if old and old["secret"] != secret else old["rotate_until"] if old else 0
            db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?,1)",
                       (self.principal, sub_id, name, canonical(arguments), url, secret, old_secret, rotation, expires))
        return {"id": sub_id, "refreshBefore": iso(expires), "cursor": None, "truncated": False}

    def subscription_id(self, name: str, arguments: dict, url: str) -> str:
        return "sub_" + hashlib.sha256(canonical([self.principal, url, name, arguments]).encode()).hexdigest()

    def unsubscribe(self, name: str, arguments: dict, delivery: dict) -> dict:
        validate_event(name, arguments)
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook" or not isinstance(delivery.get("url"), str):
            raise AgentError("Webhook delivery URL is required.")
        sub_id = self.subscription_id(name, arguments, delivery["url"])
        with self.db() as db:
            db.execute("UPDATE subscriptions SET active=0 WHERE owner=? AND id=?", (self.principal, sub_id))
            db.execute("UPDATE deliveries SET state='cancelled' WHERE owner=? AND subscription=? AND state='pending'", (self.principal, sub_id))
        return {}

    def revoke(self):
        with self.db() as db:
            db.execute("UPDATE subscriptions SET active=0 WHERE owner=?", (self.principal,))
            db.execute("UPDATE deliveries SET state='cancelled' WHERE owner=? AND state='pending'", (self.principal,))

    def dispatch(self, *, sender=https_post, limit=50) -> dict:
        counts = {"delivered": 0, "deferred": 0, "stopped": 0}
        for _ in range(limit):
            now = time.time()
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("""SELECT d.*,s.url,s.secret,s.old_secret,s.rotate_until,e.body
                    FROM deliveries d JOIN subscriptions s ON s.id=d.subscription JOIN events e ON e.id=d.event
                    WHERE d.owner=? AND d.state='pending' AND d.next_at<=? AND d.lease_until<=?
                    AND s.active=1 AND s.expires>? ORDER BY e.seq LIMIT 1""", (self.principal, now, now, now)).fetchone()
                if row is None:
                    break
                db.execute("UPDATE deliveries SET lease_until=? WHERE subscription=? AND event=?", (now + 30, row["subscription"], row["event"]))
            body = row["body"].encode()
            headers = signed_headers(body, row["event"], row["subscription"], row["secret"], row["old_secret"] if row["rotate_until"] > now else None)
            try:
                status, _ = sender(row["url"], body, headers)
            except (OSError, AgentError, http.client.HTTPException):
                status = 503
            attempts = row["attempts"] + 1
            delivered = 200 <= status < 300
            retry = not delivered and (status in {408, 425, 429} or status >= 500) and attempts < 8
            state = "delivered" if delivered else "pending" if retry else "stopped"
            with self.db() as db:
                db.execute("UPDATE deliveries SET state=?,attempts=?,next_at=?,lease_until=0 WHERE subscription=? AND event=?",
                           (state, attempts, time.time() + min(3600, 2 ** attempts * 5), row["subscription"], row["event"]))
                if status == 410:
                    db.execute("UPDATE subscriptions SET active=0 WHERE owner=? AND id=?", (self.principal, row["subscription"]))
            counts["delivered" if delivered else "deferred" if retry else "stopped"] += 1
        return counts

    def list_events(self, after: int = 0, limit: int = 50) -> dict:
        with self.db() as db:
            rows = db.execute("SELECT seq,body FROM events WHERE owner=? AND seq>? ORDER BY seq LIMIT ?", (self.principal, after, limit + 1)).fetchall()
        return {"items": [{"sequence": row["seq"], **json.loads(row["body"])} for row in rows[:limit]],
                "hasMore": len(rows) > limit, "nextAfter": rows[min(len(rows), limit) - 1]["seq"] if rows else after}

    def record(self, source: str, resource_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT body FROM snapshots WHERE owner=? AND source=? AND id=?", (self.principal, source, resource_id)).fetchone()
        if not row:
            raise AgentError("Observed record not found.", "not_found")
        return {"source": source, "record": json.loads(row[0]), "untrustedContent": True}

    def status(self) -> dict:
        with self.db() as db:
            baselines = [dict(row) for row in db.execute("SELECT source,updated FROM baselines WHERE owner=?", (self.principal,))]
            subscriptions = db.execute("SELECT COUNT(*) FROM subscriptions WHERE owner=? AND active=1 AND expires>?", (self.principal, time.time())).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM deliveries WHERE owner=? AND state='pending'", (self.principal,)).fetchone()[0]
        return {"owner": self.principal, "baselines": baselines, "activeSubscriptions": subscriptions, "pendingDeliveries": pending}


def stable_record(value):
    if isinstance(value, list):
        return [stable_record(item) for item in value]
    if isinstance(value, dict):
        private = {"url", "signedurl", "presignedurl", "downloadurl", "image", "passportnumber", "dateofbirth", "expirationdate", "passport", "passports", "cardnumber", "cvc", "expiry", "cardname", "cardtype", "billingaddress", "phonenumber", "email", "destinationaddress", "address"}
        return {k: stable_record(v) for k, v in value.items() if k.lower() not in private and not k.lower().endswith("url")}
    return value


def source_time(value) -> str | None:
    try:
        if type(value) in (int, float):
            return iso(value / 1000 if value > 100000000000 else value)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return iso(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        pass
    return None


def collection(cli, token: str, path: str, filt: dict, sort=None) -> list[dict]:
    items, seen = [], set()
    for offset in range(0, 5000, 100):
        response = cli.api_post(token, path, {"filter": filt, "sort": sort or [], "page": {"offset": offset, "limit": 100}})
        data = cli._response_data(response)
        if not isinstance(data, dict):
            raise AgentError("Collection returned an invalid envelope; no snapshot advanced.", "incomplete_collection")
        page = data.get("items")
        total = data.get("totalCount", data.get("total"))
        if not isinstance(page, list) or any(not isinstance(i, dict) or not isinstance(i.get("id"), str) for i in page):
            raise AgentError("Collection returned an invalid page; no snapshot advanced.", "incomplete_collection")
        if any(i["id"] in seen for i in page) or len({i["id"] for i in page}) != len(page):
            raise AgentError("Collection repeated an item; no snapshot advanced.", "incomplete_collection")
        seen.update(i["id"] for i in page)
        items.extend(page)
        if total is not None and (type(total) is not int or total < len(items)):
            raise AgentError("Collection total is inconsistent; no snapshot advanced.", "incomplete_collection")
        if len(page) < 100:
            if total is not None and total != len(items):
                raise AgentError("Collection is incomplete; no snapshot advanced.", "incomplete_collection")
            return items
        if total == len(items):
            return items
    raise AgentError("Collection exceeds 5,000 records; narrow monitoring before collecting.", "incomplete_collection")


def collect(cli, store: EventStore) -> dict:
    token = cli.get_api_token(None, None)
    account_ids = [account["id"] for account in cli._get_accounts(token, refresh=True) if isinstance(account.get("id"), str)]
    if not account_ids:
        store.revoke()
        raise AgentError("Connected owner has no accessible accounts.", "authentication")
    notifications = collection(cli, token, "/my-notifications", {})
    trips = collection(cli, token, "/my-leg", {"accountId": account_ids, "departureTime": {"min": iso()}}, [{"departureDate": "ASC"}])
    count = store.observe("notifications", notifications, created="notification.created", updated="notification.updated")
    count += store.observe("trips", trips, created="trip.created", updated="trip.updated")
    with store.db() as db:
        legs = {json.loads(row[0])["legId"] for row in db.execute("SELECT arguments FROM subscriptions WHERE owner=? AND active=1 AND expires>? AND name='customs.submitted'", (store.principal, time.time()))}
    allowed = {trip["id"] for trip in trips}
    for leg_id in legs:
        if leg_id not in allowed:
            with store.db() as db:
                db.execute("UPDATE subscriptions SET active=0 WHERE owner=? AND name='customs.submitted' AND arguments=?", (store.principal, canonical({"legId": leg_id})))
            continue
        declarations = collection(cli, token, "/myCanadianCustomsDeclaration", {"legId": leg_id})
        count += store.observe("customs:" + leg_id, declarations, created="customs.submitted")
    return {"newEvents": count, "notifications": len(notifications), "upcomingLegs": len(trips), "customsLegs": len(legs)}
