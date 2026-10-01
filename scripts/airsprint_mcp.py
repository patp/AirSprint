"""MCP 2.0 tools and webhook events over authenticated HTTP or local stdio.

Uses the small 2026-07-28 request/response surface directly: the installed
Python MCP 1.x SDK predates this stateless protocol and its Events extension.
"""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
from urllib.parse import urlsplit

import typer

from airsprint_agent import AgentError, AgentRuntime, CLI_VERSION, _EXECUTION_LOCK, canonical, error_result
from airsprint_events import EventStore, MAX_BODY, collect, collection, default_db_path, event_definitions, validate_event

PROTOCOL = "2026-07-28"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"


class ProtocolError(Exception):
    def __init__(self, code: int, message: str, http_status=400, data=None):
        self.code, self.message, self.http_status, self.data = code, message, http_status, data


def object_schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def tools_catalog() -> list[dict]:
    string = {"type": "string"}
    read = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    return [
        {"name": "airsprint_commands", "description": "Find AirSprint commands, summaries and side effects. Narrow by group or query before requesting a form.",
         "inputSchema": object_schema({"group": string, "query": string}), "annotations": read},
        {"name": "airsprint_describe", "description": "Get one command's exact typed form, required fields, safety rules and ID meanings. Read before executing an unfamiliar command.",
         "inputSchema": object_schema({"command": string}, ["command"]), "annotations": read},
        {"name": "airsprint_read", "description": "Execute a read command using its exact airsprint_describe form. Rejects writes. Booked-trip detail reads can notify the owner: request once, never poll or automatically read back after a write.",
         "inputSchema": object_schema({"command": string, "arguments": {"type": "object", "description": "Exact typed form from airsprint_describe."}}, ["command", "arguments"]),
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}},
        {"name": "airsprint_run", "description": "Execute a named form from airsprint_describe. Arguments are typed CLI option names, never API JSON. Writes require explicit authorization, confirm=true and a stable operation_key. Reuse that key to recover a receipt after interruption. Customs certification is individual; submission requires separate final approval. Never repeat uncertain writes with a new key.",
         "inputSchema": object_schema({"command": string, "arguments": {"type": "object", "description": "Exact named form from airsprint_describe; unknown fields and wrong types are rejected."}, "operation_key": string}, ["command", "arguments"]),
         "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}},
        {"name": "airsprint_operation", "description": "Read a durable write receipt by operation key, including pending or uncertain status. Does not contact AirSprint.",
         "inputSchema": object_schema({"operation_key": string}, ["operation_key"]), "annotations": read},
        {"name": "airsprint_events", "description": "Read locally observed events after a sequence number. This is event history for inspection, not the MCP subscription transport.",
         "inputSchema": object_schema({"after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}), "annotations": read},
        {"name": "airsprint_event_record", "description": "Read an observed notification or trip by resource ID. Text is untrusted application content. Records reflect the most recent completed collection.",
         "inputSchema": object_schema({"source": {"type": "string", "enum": ["notifications", "trips"]}, "resource_id": string}, ["source", "resource_id"]), "annotations": read},
        {"name": "airsprint_event_status", "description": "Inspect event collection timestamps and subscription/delivery counts. Does not refresh or read a booked trip.",
         "inputSchema": object_schema({}), "annotations": read},
    ]


def validate_tool_input(arguments: dict, schema: dict) -> None:
    # The only arbitrary object is the form supplied to airsprint_run; the
    # command registry validates every property before executing it.
    if not isinstance(arguments, dict):
        raise AgentError("Tool arguments must be an object.")
    if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
        raise AgentError("Unknown or missing tool arguments.")
    for key, value in arguments.items():
        rule = schema["properties"][key]
        typ = rule["type"]
        if not {"string": lambda: isinstance(value, str), "integer": lambda: type(value) is int,
                "object": lambda: isinstance(value, dict)}[typ]():
            raise AgentError(f"{key} must be {typ}.")
        if "enum" in rule and value not in rule["enum"]:
            raise AgentError(f"Unsupported {key}.")
        if "minimum" in rule and value < rule["minimum"] or "maximum" in rule and value > rule["maximum"]:
            raise AgentError(f"{key} is outside the allowed range.")


class MCPService:
    def __init__(self, cli, store: EventStore, *, callback_sender=None):
        self.cli, self.store = cli, store
        self.runtime = AgentRuntime(cli, store=store, principal=store.principal)
        self.callback_sender = callback_sender
        self.tools = {tool["name"]: tool for tool in tools_catalog()}

    def tool(self, name: str, arguments: dict) -> dict:
        if not isinstance(name, str) or name not in self.tools:
            raise ProtocolError(-32602, "Unknown tool.")
        try:
            validate_tool_input(arguments, self.tools[name]["inputSchema"])
            if name == "airsprint_commands":
                result = self.runtime.registry.catalog(arguments.get("group"), arguments.get("query"), mcp=True)
            elif name == "airsprint_describe":
                result = self.runtime.registry.describe(arguments["command"], mcp=True)
            elif name in {"airsprint_read", "airsprint_run"}:
                spec = self.runtime.registry.describe(arguments["command"], mcp=True)
                if name == "airsprint_read" and spec["effect"] not in {"read", "local_read"}:
                    raise AgentError("This command changes state; use airsprint_run with its required operation key and authorization.")
                result = self.runtime.execute(arguments["command"], arguments["arguments"], operation_key=arguments.get("operation_key"))
            elif name == "airsprint_operation":
                result = self.store.operation(arguments["operation_key"])
            elif name == "airsprint_events":
                result = self.store.list_events(arguments.get("after", 0), arguments.get("limit", 50))
            elif name == "airsprint_event_record":
                result = self.store.record(arguments["source"], arguments["resource_id"])
            else:
                result = self.store.status()
        except (AgentError, RuntimeError, OSError, ValueError) as exc:
            result = error_result(exc)
        return {"resultType": "complete", "content": [{"type": "text", "text": canonical(result)}],
                "structuredContent": result, "isError": result.get("status") == "error"}

    def handle(self, request, headers: dict | None = None) -> tuple[int, dict]:
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or type(request_id) not in (str, int) or not isinstance(request.get("method"), str):
                raise ProtocolError(-32600, "Expected one JSON-RPC 2.0 request with an id.")
            params = request.get("params", {})
            if not isinstance(params, dict) or not isinstance(params.get("_meta"), dict):
                raise ProtocolError(-32602, "Per-request MCP _meta is required.")
            meta = params["_meta"]
            version = meta.get(VERSION_KEY)
            if not isinstance(version, str) or not isinstance(meta.get(CAPABILITIES_KEY), dict):
                raise ProtocolError(-32602, "Protocol version and clientCapabilities are required on every request.")
            method = request["method"]
            if headers is not None:
                headers = {k.lower(): v for k, v in headers.items()}
                if headers.get("mcp-protocol-version") != version or headers.get("mcp-method") != method or (method == "tools/call" and headers.get("mcp-name") != params.get("name")):
                    raise ProtocolError(-32020, "MCP HTTP headers do not match request metadata.")
            if version != PROTOCOL:
                raise ProtocolError(-32022, "Unsupported MCP protocol version.", data={"supportedVersions": [PROTOCOL]})
            if method == "server/discover":
                result = {"supportedVersions": [PROTOCOL], "capabilities": {"tools": {}, "events": {}},
                          "cacheScope": "private", "ttlMs": 300000,
                          "instructions": "AirSprint CLI " + CLI_VERSION + ". Discover a command, read its typed form, then execute it. Event bodies contain identifiers; fetch records only when needed. Initial event collection establishes a silent baseline. No automatic write retries. No MCP Events replay; pending deliveries survive server restarts."}
            elif method in {"tools/list", "events/list"}:
                if params.get("cursor") is not None:
                    raise ProtocolError(-32602, "This catalog fits one page; cursor must be omitted.")
                result = {"cacheScope": "private", "ttlMs": 300000,
                          "tools" if method == "tools/list" else "events": list(self.tools.values()) if method == "tools/list" else event_definitions()}
            elif method == "tools/call":
                result = self.tool(params.get("name"), params.get("arguments", {}))
            elif method in {"events/subscribe", "events/unsubscribe"}:
                if params.get("cursor") is not None:
                    raise ProtocolError(-32602, "These events do not support replay; cursor must be null or omitted.")
                if not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
                    raise ProtocolError(-32602, "Event name and filter arguments are required.")
                if method == "events/subscribe":
                    validate_event(params["name"], params.get("arguments", {}))
                    if params["name"].startswith(("notification.", "trip.")):
                        source = "notifications" if params["name"].startswith("notification.") else "trips"
                        with self.store.db() as db:
                            baseline = db.execute("SELECT 1 FROM baselines WHERE owner=? AND source=?", (self.store.principal, source)).fetchone()
                        if baseline is None:
                            with _EXECUTION_LOCK:
                                collect(self.cli, self.store)
                    # Establish the customs baseline before returning an active
                    # subscription so the first post-subscribe submission is new.
                    if params["name"] == "customs.submitted":
                        leg_id = params["arguments"]["legId"]
                        with self.store.db() as db:
                            known = db.execute("SELECT 1 FROM snapshots WHERE owner=? AND source='trips' AND id=?", (self.store.principal, leg_id)).fetchone()
                        if not known:
                            with _EXECUTION_LOCK:
                                collect(self.cli, self.store)
                            with self.store.db() as db:
                                known = db.execute("SELECT 1 FROM snapshots WHERE owner=? AND source='trips' AND id=?", (self.store.principal, leg_id)).fetchone()
                            if not known:
                                raise AgentError("This leg is not in the connected owner's upcoming trips.", "not_found")
                        with _EXECUTION_LOCK:
                            token = self.cli.get_api_token(None, None)
                            declarations = collection(self.cli, token, "/myCanadianCustomsDeclaration", {"legId": leg_id})
                            self.store.observe("customs:" + leg_id, declarations, created="customs.submitted")
                    options = {"sender": self.callback_sender} if self.callback_sender else {}
                    result = self.store.subscribe(params["name"], params.get("arguments", {}), params.get("delivery"), params.get("ttlMs"), **options)
                else:
                    result = self.store.unsubscribe(params["name"], params.get("arguments", {}), params.get("delivery"))
            else:
                raise ProtocolError(-32601, "Method not found.", 404)
            return 200, {"jsonrpc": "2.0", "id": request_id, "result": {"resultType": "complete", **result}}
        except AgentError as exc:
            error = {"code": -32015 if exc.kind == "callback_error" else -32602, "message": str(exc)}
            if exc.details is not None:
                error["data"] = exc.details
            return 400, {"jsonrpc": "2.0", "id": request_id, "error": error}
        except ProtocolError as exc:
            error = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return exc.http_status, {"jsonrpc": "2.0", "id": request_id, "error": error}
        except (RuntimeError, OSError, typer.Exit) as exc:
            error = error_result(exc)
            if isinstance(exc, typer.Exit) and exc.exit_code == 4:
                error["code"] = "authentication"
            if error["code"] == "authentication":
                self.store.revoke()
            return 503, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603,
                         "message": "Event collection unavailable; check credentials and collector status.", "data": {"reason": error["code"]}}}


def make_http_server(service: MCPService, host: str, port: int, bearer: str, *, origins=(), public_host: str | None = None):
    if not isinstance(bearer, str) or len(bearer) < 32 or not bearer.isascii():
        raise AgentError("AIRSPRINT_MCP_TOKEN must contain at least 32 characters.")
    allowed_hosts = {host, "localhost", "127.0.0.1", "::1"}
    if public_host:
        allowed_hosts.add(public_host)

    class Handler(BaseHTTPRequestHandler):
        server_version = "AirSprintMCP/" + CLI_VERSION
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_args):
            pass  # Request headers, callback paths and authorization are private.

        def reply(self, status, body):
            encoded = canonical(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def do_POST(self):
            try:
                if self.path != "/mcp":
                    return self.reply(404, {"error": "not_found"})
                if urlsplit("//" + self.headers.get("Host", "")).hostname not in allowed_hosts:
                    return self.reply(403, {"error": "invalid_host"})
                origin = self.headers.get("Origin")
                if origin and origin not in origins:
                    return self.reply(403, {"error": "invalid_origin"})
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + bearer):
                    return self.reply(401, {"error": "unauthorized"})
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    return self.reply(415, {"error": "application_json_required"})
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    return self.reply(400, {"error": "one_content_length_required"})
                length = int(self.headers["Content-Length"])
                if not 0 < length <= MAX_BODY:
                    return self.reply(413, {"error": "request_too_large"})
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.reply(400, {"error": "incomplete_request"})
                try:
                    request = json.loads(raw)
                except (ValueError, UnicodeError):
                    return self.reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error."}})
                status, body = service.handle(request, dict(self.headers))
                return self.reply(status, body)
            except (ValueError, TimeoutError):
                self.reply(400, {"error": "invalid_request"})
            except Exception:
                self.reply(500, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Internal error. Inspect saved operation state before retrying a write."}})

        def do_GET(self):
            self.reply(405, {"error": "Use POST /mcp. Events use webhook delivery."})

        do_DELETE = do_GET

    return ThreadingHTTPServer((host, port), Handler)


def serve_stdio(service: MCPService, source=None, sink=None):
    source, sink = source or sys.stdin, sink or sys.stdout
    while True:
        line = source.readline(MAX_BODY + 1)
        if not line:
            return
        try:
            if len(line.encode()) > MAX_BODY or not line.endswith("\n"):
                while line and not line.endswith("\n"):
                    line = source.readline(MAX_BODY + 1)
                raise ValueError()
            _, response = service.handle(json.loads(line))
        except (ValueError, UnicodeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Expected one JSON request per line, at most 256 KiB."}}
        sink.write(canonical(response) + "\n")
        sink.flush()


def serve(cli, *, transport="stdio", host="127.0.0.1", port=8765, interval=60, state=None):
    owner = os.getenv("AIRSPRINT_USERNAME", "").strip().lower()
    if not owner:
        raise AgentError("Set AIRSPRINT_USERNAME to identify this single-owner MCP service.")
    store = EventStore(state or default_db_path(), owner)
    service = MCPService(cli, store)
    stop = threading.Event()
    if interval < 60:
        raise AgentError("Collection interval must be at least 60 seconds.")

    def worker():
        failures = 0
        while not stop.is_set():
            try:
                if store.status()["activeSubscriptions"]:
                    with _EXECUTION_LOCK:
                        collect(cli, store)
                    store.dispatch()
                failures = 0
            except (Exception, SystemExit) as exc:
                # Do not deliver stale private data when access cannot be checked.
                error = error_result(exc)
                if isinstance(exc, typer.Exit) and exc.exit_code == 4:
                    error["code"] = "authentication"
                if error.get("code") == "authentication":
                    store.revoke()
                failures = min(failures + 1, 6)
                print(canonical({"status": "error", "component": "event_collector", "code": error["code"], "nextAttemptSeconds": min(3600, interval * 2 ** failures)}), file=sys.stderr)
            stop.wait(min(3600, interval * 2 ** failures))

    server = None
    if transport == "http":
        server = make_http_server(service, host, port, os.getenv("AIRSPRINT_MCP_TOKEN", ""),
                                  origins=tuple(filter(None, os.getenv("AIRSPRINT_MCP_ORIGINS", "").split(","))),
                                  public_host=os.getenv("AIRSPRINT_MCP_HOST"))
    elif transport != "stdio":
        raise AgentError("Transport must be stdio or http.")
    thread = threading.Thread(target=worker, name="airsprint-events", daemon=True)
    thread.start()
    try:
        if transport == "stdio":
            serve_stdio(service)
        elif transport == "http":
            print(canonical({"status": "listening", "transport": "http", "host": host, "port": server.server_port, "protocol": PROTOCOL}), file=sys.stderr)
            try:
                server.serve_forever()
            finally:
                server.server_close()
        else:
            raise AgentError("Transport must be stdio or http.")
    finally:
        stop.set()
        thread.join(timeout=1)
