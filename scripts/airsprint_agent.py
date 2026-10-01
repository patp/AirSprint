"""Typed command discovery and a persistent runtime shared by CLI agents and MCP.

This module translates named form options to the existing CLI. It never builds
AirSprint API payloads, retries commands, or exposes the maintainer raw surface.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import time
from typing import Any

import typer

try:  # Typer 0.26+ bundles its Click-compatible parser.
    from typer import _click as click
except ImportError:
    import click


SCHEMA_VERSION = "1"
CLI_VERSION = "1.2.0"
_EXECUTION_LOCK = threading.RLock()  # stdout and the legacy CLI presentation settings
_EXCLUDED_GROUPS = {"raw", "agent", "mcp"}
_CREDENTIAL_OPTIONS = {"username", "password", "token", "new-password", "current-password"}
_PRESENTATION_OPTIONS = {"format", "compact"}
_LOCAL_READS = {"auth status", "cache status", "customs review", "events list", "events get", "events status"}
_LOCAL_WRITES = {"auth logout", "cache clear", "customs certify"}
_READS = {
    "summary", "auth verify", "user profile", "user get", "user accounts", "user preferences",
    "account users", "account roles", "trips list", "trips get", "trips show", "trips tripsheet",
    "trips recent", "trips flight-get", "trips leg-get", "leg audit-travel-info",
    "booking info", "booking reserved-days", "booking baggage-types", "explore flights",
    "explore counts", "messages list", "messages settings", "passenger list", "passenger get",
    "passport list", "pet list", "pet get", "customs list", "customs status", "customs link-get",
    "quote flight", "quote roundtrip", "quote cost", "quote hours-exchange", "quote airports",
    "quote aircraft", "quote aircraft-get", "quote airport-nearest", "quote saved-airports",
    "address autocomplete", "hours estimate", "hours power", "hours my-listings",
    "files list", "files resolve", "files get", "files public-get", "content faq", "content faq-get",
    "content faq-categories", "content policies", "content policy-get", "content policy-categories",
    "content system-notice", "content concierge", "content get", "network connections", "network groups",
}
_BOOKED_READS = {
    "trips get", "trips show", "trips tripsheet", "trips flight-get", "trips leg-get",
    "leg audit-travel-info", "leg update-passengers", "leg update-required-info",
    "customs prepare", "customs create", "customs submit",
}
_MCP_EXCLUDED = {
    "auth logout", "auth login", "auth reset-request", "auth reset-confirm", "auth 2fa-setup",
    "auth 2fa-verify", "auth 2fa-sign-in", "auth 2fa-disable", "user change-password",
    "device register-token", "device delete-token", "customs create", "customs link-get",
    "customs update-date", "events collect", "events dispatch", "events init",
}
_GUIDANCE = {
    "customs prepare": [
        "Select the active leg arriving in Canada. Outbound and return IDs differ.",
        "Missing answers are saved as blockers; do not invent answers.",
        "Editing or refreshing a draft clears every certification.",
    ],
    "customs certify": [
        "Show one traveller's complete draft and obtain that person's explicit certification first.",
        "Use the exact full name or leg-passenger UUID. General approval does not certify everyone.",
    ],
    "customs submit": [
        "Requires every individual certification and a separate final instruction to submit.",
        "Submitted, submitting, uncertain, changed, or incomplete drafts are blocked.",
        "Never create another draft or use another operation key to retry an uncertain submission.",
    ],
    "leg update-passengers": [
        "Use actual passenger-profile IDs, not leg-passenger IDs. The CLI preserves the full list.",
    ],
    "leg update-required-info": [
        "Use actual trip-profile passenger IDs; saved profiles with the same name can differ.",
    ],
    "passport list": ["Use passenger-id from the booked leg to include unsaved guests."],
    "customs status": ["A link ID identifies one leg. A declaration ID identifies one traveller's form."],
    "booking create": ["Collect baggage, passport scans and destination addresses before booking."],
}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class AgentError(ValueError):
    def __init__(self, message: str, kind: str = "validation", *, details: Any = None):
        super().__init__(message)
        self.kind = kind
        self.details = details


def error_result(exc: Exception, command: str = "", *, trace: list | None = None) -> dict:
    """One JSON error shape for form errors, parser errors, HTTP errors and RPC."""
    if isinstance(exc, RuntimeError):
        try:
            parsed = json.loads(str(exc))
        except (ValueError, TypeError):
            parsed = None
    else:
        parsed = None
    result = dict(parsed) if isinstance(parsed, dict) else {"message": str(exc)}
    result["status"] = "error"
    if isinstance(exc, click.ClickException):
        result["message"] = exc.format_message()
    http_code = result.get("http_code") or result.get("httpStatusCode")
    kind = getattr(exc, "kind", None) or result.get("code") or (
        "validation" if isinstance(exc, (click.exceptions.UsageError, ValueError))
        else "authentication" if http_code in (401, 403)
        else "not_found" if http_code == 404
        else "rate_limited" if http_code == 429
        else "api_error" if http_code
        else "request_failed"
    )
    uncertain = result.get("requestMayHaveSucceeded") is True or kind == "operation_uncertain" or any(
        item.get("effect") == "write" and item.get("outcome") == "uncertain" for item in trace or [])
    result.update(code=kind, retryable=False, requestMayHaveSucceeded=uncertain)
    if command:
        result["command"] = command
    if getattr(exc, "details", None) is not None:
        result["details"] = exc.details
    if uncertain:
        result["nextAction"] = "Do not repeat the write. Inspect its saved operation and reconcile the outcome."
    elif kind == "validation":
        result["nextAction"] = f"Correct the input using agent describe --command '{command}'." if command else "Use agent commands to find a command, then agent describe for its form."
    elif kind == "authentication":
        result["nextAction"] = "Check auth status and sign in explicitly if needed. Do not repeat a write automatically."
    elif kind == "rate_limited":
        result["nextAction"] = "Wait before a new read. Reconcile any attempted write before repeating it."
    else:
        result.setdefault("nextAction", "Inspect the error and connection. Do not repeat writes automatically.")
    return result


def effects(name: str) -> dict:
    if name in _LOCAL_READS:
        effect = "local_read"
    elif name in _LOCAL_WRITES:
        effect = "local_write"
    elif name in {"customs prepare", "customs create", "cache refresh", "events collect", "user avatar"}:
        effect = "read_and_save_local"
    elif name in _READS:
        effect = "read"
    else:
        effect = "write"  # New commands fail closed until classified.
    return {
        "effect": effect,
        "writesAirSprint": effect == "write",
        "readsBookedTrip": name in _BOOKED_READS,
        "mayNotifyOwner": name in _BOOKED_READS or (effect == "write" and name.split()[0] in {"trips", "leg", "booking"}),
        "automaticRetry": False,
        "automaticReadBack": False,
    }


def _option_name(param: click.Parameter) -> str:
    return next((opt[2:] for opt in param.opts if opt.startswith("--")), param.name or "")


def _option_schema(param: click.Parameter) -> dict:
    kind = param.type
    schema: dict[str, Any] = {"type": "string"}
    if isinstance(kind, click.types.BoolParamType) or getattr(param, "is_bool_flag", False):
        schema["type"] = "boolean"
    elif isinstance(kind, click.types.IntParamType):
        schema["type"] = "integer"
    elif isinstance(kind, click.types.FloatParamType):
        schema["type"] = "number"
    elif getattr(kind, "choices", None) is not None:
        schema["enum"] = list(kind.choices)
    for attr, key in (("min", "minimum"), ("max", "maximum")):
        if getattr(kind, attr, None) is not None:
            schema[key] = getattr(kind, attr)
    help_text = getattr(param, "help", None) or ""
    if schema["type"] == "string" and re.search(r"yes\s*[|/]\s*no", help_text):
        schema["enum"] = ["yes", "no"]
    if getattr(param, "multiple", False):
        schema = {"type": "array", "items": schema, "minItems": 1}
    if help_text:
        schema["description"] = help_text
    # Never evaluate environment-backed defaults (credentials, timezone, etc.).
    if param.default is not None and not callable(param.default) and not getattr(param, "envvar", None):
        default = list(param.default) if isinstance(param.default, tuple) else param.default
        if not (isinstance(default, list) and not default):
            schema["default"] = default
    return schema


def validate_object(value: Any, schema: dict) -> None:
    """Validate the deliberately small, explicit schema vocabulary we generate."""
    if not isinstance(value, dict):
        raise AgentError("Arguments must be an object of named form options.")
    props = schema["properties"]
    unknown = sorted(set(value) - set(props))
    missing = [key for key in schema.get("required", []) if key not in value]
    if unknown or missing:
        raise AgentError("Unknown or missing form options.", details={"unknown": unknown, "missing": missing})

    def check(item: Any, rule: dict, name: str) -> None:
        typ = rule["type"]
        valid = {"string": lambda: isinstance(item, str), "boolean": lambda: type(item) is bool,
                 "integer": lambda: type(item) is int,
                 "number": lambda: type(item) in (float, int) and math.isfinite(item),
                 "array": lambda: isinstance(item, list)}[typ]()
        if not valid:
            raise AgentError(f"{name} must be {typ}; received {type(item).__name__}.")
        if typ == "array":
            if len(item) < rule.get("minItems", 0):
                raise AgentError(f"{name} cannot be empty.")
            for child in item:
                check(child, rule["items"], name)
        if "enum" in rule and item not in rule["enum"]:
            raise AgentError(f"{name} must be one of {rule['enum']}.")
        for key, valid in (("minimum", lambda n: item >= n), ("maximum", lambda n: item <= n)):
            if key in rule and not valid(rule[key]):
                raise AgentError(f"{name} violates {key} {rule[key]}.")

    for key, item in value.items():
        check(item, props[key], key)


class CommandRegistry:
    def __init__(self, cli):
        self.cli = cli
        self.root = typer.main.get_command(cli.app)
        self.commands: dict[str, click.Command] = {}

        def walk(group, prefix: str = ""):
            for key, command in group.commands.items():
                if command.hidden or (not prefix and key in _EXCLUDED_GROUPS):
                    continue
                name = (prefix + " " + key).strip()
                if hasattr(command, "commands"):
                    walk(command, name)
                else:
                    self.commands[name] = command

        walk(self.root)

    def describe(self, name: str, *, mcp: bool = False) -> dict:
        if name not in self.commands or (mcp and name in _MCP_EXCLUDED):
            raise AgentError(f"Unknown or unavailable command: {name}", "not_found")
        command = self.commands[name]
        props, required = {}, []
        for param in command.params:
            key = _option_name(param)
            if getattr(param, "hidden", False) or key in _CREDENTIAL_OPTIONS | _PRESENTATION_OPTIONS or key == "help":
                continue
            props[key.replace("-", "_")] = _option_schema(param)
            if param.required:
                required.append(key.replace("-", "_"))
        metadata = effects(name)
        if metadata["writesAirSprint"] and "confirm" not in props:
            props["confirm"] = {"type": "boolean", "description": "True only after explicit user authorization for this write."}
        schema = {"type": "object", "properties": props, "additionalProperties": False}
        if required:
            schema["required"] = required
        return {"command": name, "description": (command.help or "").strip(),
                "inputSchema": schema, **metadata, "guidance": _GUIDANCE.get(name, []),
                "mcpTool": "airsprint_read" if metadata["effect"] in {"read", "local_read"} else "airsprint_run",
                "operationKey": "Required for writes in the agent runtime. Reuse the same key after an interrupted call; never invent a new key to retry."}

    def catalog(self, group: str | None = None, query: str | None = None, *, mcp=False) -> dict:
        if group and not any(n == group or n.startswith(group + " ") for n in self.commands):
            raise AgentError(f"Unknown command group: {group}", "not_found")
        available = {name: cmd for name, cmd in self.commands.items() if not mcp or name not in _MCP_EXCLUDED}
        result = {"schemaVersion": SCHEMA_VERSION, "cliVersion": CLI_VERSION, "androidVersion": self.cli.ANDROID_APP_VERSION}
        if not group and not query:
            groups = sorted({name.split()[0] for name in available})
            result["groups"] = [{"group": name, "commands": sum(key.split()[0] == name for key in available),
                                 "summary": (self.root.commands[name].help or "").strip().split("\n")[0]} for name in groups]
            result["nextAction"] = "Choose a group or search with query; describe one command before executing it."
        else:
            result["commands"] = [{"command": name, "summary": (cmd.help or "").strip().split("\n")[0], **effects(name)}
                                  for name, cmd in sorted(available.items())
                                  if (not group or name == group or name.startswith(group + " "))
                                  and (not query or query.casefold() in (name + " " + (cmd.help or "")).casefold())]
        return result

    def arguments(self, name: str, values: dict) -> list[str]:
        spec = self.describe(name)
        validate_object(values, spec["inputSchema"])
        command = self.commands[name]
        params = {_option_name(p).replace("-", "_"): p for p in command.params}
        args = name.split()
        for key, value in values.items():
            if key == "confirm" and key not in params:
                continue
            param = params[key]
            flag = "--" + _option_name(param)
            if getattr(param, "is_bool_flag", False):
                if value:
                    args.append(flag)
                elif param.secondary_opts:
                    args.append(param.secondary_opts[0])
                elif param.default is True:
                    raise AgentError(f"{key} has no false form.")
            elif getattr(param, "multiple", False):
                for item in value:
                    args.extend([flag, str(item)])
            else:
                args.extend([flag, str(value)])
        if "compact" in params:
            args.append("--compact")
        return args


class AgentRuntime:
    """One process, one parsed command tree, serialized CLI calls and no retries."""
    def __init__(self, cli, *, store=None, principal: str | None = None):
        self.cli = cli
        self.registry = CommandRegistry(cli)
        self.store = store
        self.principal = principal or os.getenv("AIRSPRINT_USERNAME", "").strip().lower() or "local"

    def execute(self, command: str, arguments: dict | None = None, *, operation_key: str | None = None) -> dict:
        started = time.perf_counter()
        trace: list[dict] = []
        operation = None
        try:
            spec = self.registry.describe(command)
            values = {} if arguments is None else arguments
            args = self.registry.arguments(command, values)
            is_write = spec["effect"] in {"write", "local_write", "read_and_save_local"} and not values.get("dry_run", False)
            if spec["writesAirSprint"] and not values.get("dry_run", False) and values.get("confirm") is not True:
                raise AgentError("This write needs confirm=true after explicit user authorization.")
            if is_write:
                if not isinstance(operation_key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,160}", operation_key):
                    raise AgentError("A write needs a stable operation_key (8–160 letters, digits, '.', '_', ':', '-'). Reuse it after an interrupted call.")
                if self.store is None:
                    from airsprint_events import EventStore, default_db_path
                    self.store = EventStore(default_db_path(), self.principal)
                fingerprint = hashlib.sha256(canonical({"command": command, "arguments": values}).encode()).hexdigest()
                operation, replay = self.store.begin_operation(operation_key, command, fingerprint)
                if replay is not None:
                    replay.setdefault("meta", {})["replayed"] = True
                    return replay
            with _EXECUTION_LOCK:
                out, err = io.StringIO(), io.StringIO()
                token = self.cli._REQUEST_TRACE.set(trace)
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        try:
                            exit_code = self.registry.root.main(args, prog_name="airsprint", standalone_mode=False)
                        except typer.Exit as exc:
                            exit_code = exc.exit_code
                    raw = out.getvalue().strip() or err.getvalue().strip()
                    try:
                        result = json.loads(raw)
                    except ValueError:
                        result = {"status": "error" if exit_code else "ok", "message" if exit_code else "data": raw}
                    if exit_code and result.get("status") != "error":
                        result = {"status": "error", "message": raw}
                    if result.get("status") == "error":
                        result = error_result(RuntimeError(canonical(result)), command, trace=trace)
                finally:
                    self.cli._REQUEST_TRACE.reset(token)
            result.setdefault("meta", {}).update(command=command, schemaVersion=SCHEMA_VERSION,
                                                  elapsedMs=round((time.perf_counter() - started) * 1000, 2),
                                                  apiRequests=len(trace), replayed=False)
        except (AgentError, click.ClickException, RuntimeError, OSError, ValueError) as exc:
            result = error_result(exc, command, trace=trace)
        if operation is not None:
            self.store.finish_operation(operation, result, trace)
        return result


def serve_jsonl(cli, source=None, sink=None) -> None:
    """Read typed command requests from stdin. Each input line has one reply."""
    source, sink = source or sys.stdin, sink or sys.stdout
    runtime = AgentRuntime(cli)
    while True:
        line = source.readline(262145)
        if not line:
            break
        request_id = None
        try:
            if len(line.encode()) > 262144 or not line.endswith("\n"):
                if not line.endswith("\n"):
                    while line and not line.endswith("\n"):
                        line = source.readline(262145)
                raise AgentError("Request must be one newline-terminated JSON object, at most 256 KiB.")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) - {"id", "command", "arguments", "operation_key"}:
                raise AgentError("Expected id, command, arguments and optional operation_key.")
            request_id = request.get("id")
            if type(request_id) not in (str, int) or not isinstance(request.get("command"), str):
                raise AgentError("id must be a string or integer and command must be a command name.")
            result = runtime.execute(request["command"], request.get("arguments", {}), operation_key=request.get("operation_key"))
        except (AgentError, ValueError) as exc:
            result = error_result(exc)
        sink.write(json.dumps({"id": request_id, **result}, ensure_ascii=False, separators=(",", ":")) + "\n")
        sink.flush()


def cli_main(cli) -> int:
    """Keep the normal CLI syntax while making every failure machine readable."""
    trace: list[dict] = []
    token = cli._REQUEST_TRACE.set(trace)
    try:
        code = cli.app(standalone_mode=False)
        return code if isinstance(code, int) else 0
    except typer.Exit as exc:
        return exc.exit_code
    except (click.ClickException, RuntimeError, OSError, ValueError) as exc:
        command = " ".join(a for a in sys.argv[1:3] if not a.startswith("-"))
        print(json.dumps(error_result(exc, command, trace=trace)), file=sys.stderr)
        return 2 if isinstance(exc, (click.exceptions.UsageError, ValueError)) else 1
    finally:
        cli._REQUEST_TRACE.reset(token)
