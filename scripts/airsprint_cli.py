#!/usr/bin/env python3
"""AirSprint CLI — agent-friendly interface to AirSprint's current owner API.

The CLI authenticates against api.airsprint.com (the backend used by the
current app and owner portal) via /user/sign-in-email.
Output: JSON by default (--format human for readable output).
Credentials: AIRSPRINT_USERNAME / AIRSPRINT_PASSWORD env vars, or --username/--password flags.
Token cache: ~/.airsprint_api_token.json (avoids re-login per invocation).

Exit codes:
  0 = success
  1 = general error
  2 = validation / input error
  3 = not found
  4 = auth failure
"""

import sys
from pathlib import Path

# Agent-guide output is a frequent offline operation. Avoid importing Typer and
# initializing TLS machinery when this is the only requested action.
if __name__ == "__main__" and sys.argv[1:] == ["--skill"]:
    print((Path(__file__).resolve().parents[1] / "SKILL.md").read_text(), end="")
    raise SystemExit(0)

import json
import os
import re
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone as _tz_utc
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import typer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE_URL = "https://api.airsprint.com/api"
API_TOKEN_CACHE = Path.home() / ".airsprint_api_token.json"
DATA_CACHE = Path.home() / ".airsprint_cache.json"  # local mirror: airports, aircraft
BOOKING_WRITE_GUARD = Path.home() / ".airsprint_last_booking_write.json"
BOOKING_READ_COOLDOWN_SECONDS = 8
DATA_CACHE_TTL = 7 * 24 * 3600  # 7 days
ACCOUNT_CACHE_TTL = 15 * 60
EPOCH_MILLISECONDS_THRESHOLD = 10_000_000_000

_API_REQUEST_COUNT = 0
_API_REQUEST_LOCK = threading.Lock()
_SSL_CONTEXT: ssl.SSLContext | None = None
_SSL_CONTEXT_LOCK = threading.Lock()
_DATA_CACHE_MEMORY: dict[str, Any] | None = None
_DATA_CACHE_MEMORY_MTIME_NS: int | None = None
_DATA_CACHE_MEMORY_PATH: Path | None = None
_AIRPORT_BY_ID: dict[str, tuple[str | None, str]] | None = None
_READ_ONLY_POST_PATHS = frozenset({
    "/airport",
    "/aircraft",
    "/my-accounts",
    "/my-aircraft",
    "/my-flights",
    "/my-leg",
    "/my-notifications",
    "/my-passenger",
    "/my-passport",
    "/my-pet",
    "/my-user",
    "/my-user/connections",
    "/my-user/groups",
    "/myCanadianCustomsDeclaration",
})

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VALIDATION = 2
EXIT_NOT_FOUND = 3
EXIT_AUTH = 4

# ---------------------------------------------------------------------------
# SSL
# ---------------------------------------------------------------------------


def _ssl_ctx() -> ssl.SSLContext:
    """Initialize truststore lazily and reuse one process-wide SSL context."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    with _SSL_CONTEXT_LOCK:
        if _SSL_CONTEXT is None:
            try:
                import truststore

                truststore.inject_into_ssl()
            except ImportError:
                pass
            _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def _atomic_write_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    """Replace a private JSON file atomically so interruptions cannot corrupt it."""
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(json.dumps(payload, indent=2))
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 60,
    api_request: bool = False,
    retry_first_ssl: bool = False,
) -> dict[str, Any]:
    """Low-level JSON request.

    A WRONG_VERSION_NUMBER failure may be retried exactly once only when this
    is the process's first API request and the caller marks it read-only.
    Booking GET/PATCH calls and all writes deliberately opt out.
    """
    global _API_REQUEST_COUNT
    with _API_REQUEST_LOCK:
        first_api_request = api_request and _API_REQUEST_COUNT == 0
        if api_request:
            _API_REQUEST_COUNT += 1
    req = Request(url, data=data, method=method, headers=dict(headers or {}))
    attempts = 2 if first_api_request and retry_first_ssl else 1
    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(json.dumps({
                        "status": "error",
                        "message": "API returned a non-JSON response",
                        "content_type": resp.headers.get("Content-Type", ""),
                    })) from exc
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                json.dumps({"status": "error", "http_code": exc.code, "message": body})
            ) from exc
        except (URLError, ssl.SSLError) as exc:
            msg = str(exc)
            wrong_version = "WRONG_VERSION_NUMBER" in msg.upper()
            if attempt == 0 and attempts == 2 and wrong_version:
                continue
            raise RuntimeError(
                json.dumps({"status": "error", "message": msg})
            ) from exc
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


def _clear_api_token() -> None:
    if API_TOKEN_CACHE.exists():
        API_TOKEN_CACHE.unlink()


# api.airsprint.com helpers (the live owner-portal API)
# ---------------------------------------------------------------------------


def _api_login(username: str, password: str) -> str:
    """Login to api.airsprint.com → authToken."""
    resp = _http(
        "POST",
        f"{API_BASE_URL}/user/sign-in-email",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=json.dumps({"email": username, "password": password}).encode("utf-8"),
        api_request=True,
        retry_first_ssl=True,
    )
    token = resp.get("data", {}).get("authToken")
    if not token:
        raise RuntimeError(
            json.dumps({"status": "error", "message": "No authToken in sign-in response"})
        )
    return token


def _save_api_token(token: str, email: str) -> None:
    data = {"authToken": token, "email": email, "_cached_at": int(time.time())}
    _atomic_write_json(API_TOKEN_CACHE, data)


def _load_api_token() -> str | None:
    if not API_TOKEN_CACHE.exists():
        return None
    try:
        data = json.loads(API_TOKEN_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    # api.airsprint.com tokens don't have expires_in — use 6 hour TTL
    if time.time() - data.get("_cached_at", 0) > 21600:
        return None
    return data.get("authToken")


def get_api_token(username: str | None = None, password: str | None = None) -> str:
    """Return a valid api.airsprint.com authToken, using cache when possible."""
    cached = _load_api_token()
    if cached:
        return cached

    u = username or os.environ.get("AIRSPRINT_USERNAME", "")
    p = password or os.environ.get("AIRSPRINT_PASSWORD", "")
    if not u or not p:
        _die("Credentials required. Set AIRSPRINT_USERNAME/AIRSPRINT_PASSWORD or use --username/--password.", EXIT_AUTH)

    token = _api_login(u, p)
    _save_api_token(token, u)
    return token


def _record_booking_write(path: str) -> None:
    """Record a live booking mutation without performing any read-back."""
    payload = {"path": path, "written_at": time.time()}
    _atomic_write_json(BOOKING_WRITE_GUARD, payload)


def _recent_booking_write() -> dict[str, Any] | None:
    if not BOOKING_WRITE_GUARD.exists():
        return None
    try:
        marker = json.loads(BOOKING_WRITE_GUARD.read_text())
        age = time.time() - float(marker["written_at"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if age >= BOOKING_READ_COOLDOWN_SECONDS:
        try:
            BOOKING_WRITE_GUARD.unlink()
        except FileNotFoundError:
            pass
        return None
    marker["seconds_remaining"] = max(1, int(BOOKING_READ_COOLDOWN_SECONDS - age + 0.999))
    return marker


def _guard_booking_probe(probe: bool = False) -> None:
    """Block an accidental trip/leg read immediately after a live write."""
    marker = _recent_booking_write()
    if not marker or probe:
        return
    _die(
        "No booking probe sent. A live write just targeted "
        f"{marker.get('path', 'a booking')}; wait {marker['seconds_remaining']}s, "
        "then read once. Use --probe only to override intentionally.",
        EXIT_VALIDATION,
    )


def api_get(
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if params:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}{urlencode(params)}"
    live_booking_read = path.startswith(("/trip/", "/leg/"))
    return _http(
        "GET",
        f"{API_BASE_URL}{path}",
        headers={
            "x-airsprint-auth-token": token,
            "Accept": "application/json",
        },
        api_request=True,
        retry_first_ssl=not live_booking_read,
    )


def api_post(token: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    if path in {"/cancel-own", "/trip/book"}:
        _record_booking_write(path)
    result = _http(
        "POST",
        f"{API_BASE_URL}{path}",
        headers={
            "x-airsprint-auth-token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(body or {}).encode("utf-8"),
        api_request=True,
        retry_first_ssl=path in _READ_ONLY_POST_PATHS,
    )
    return result


def api_patch(token: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    if path.startswith(("/trip/", "/leg/")):
        _record_booking_write(path)
    result = _http(
        "PATCH",
        f"{API_BASE_URL}{path}",
        headers={
            "x-airsprint-auth-token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(body or {}).encode("utf-8"),
        api_request=True,
    )
    return result


def api_delete(token: str, path: str) -> dict[str, Any]:
    return _http(
        "DELETE",
        f"{API_BASE_URL}{path}",
        headers={
            "x-airsprint-auth-token": token,
            "Accept": "application/json",
        },
        api_request=True,
    )


def _get_account_ids(token: str) -> list[str]:
    """Get short-lived cached account IDs."""
    items = _get_accounts(token)
    return [item["id"] for item in items if "id" in item]


def _parallel_read_calls(
    tasks: dict[str, Callable[[], dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Run independent catalog/list reads concurrently with stable keys.

    Never pass booked-trip or booked-leg GET/PATCH calls here: those requests
    can notify the owner app and must remain single, sequential probes.
    """
    if not tasks:
        return {}
    if len(tasks) == 1:
        name, task = next(iter(tasks.items()))
        return {name: task()}
    # Initialize truststore/context once before worker threads race to use it.
    _ssl_ctx()
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as executor:
        futures = {name: executor.submit(task) for name, task in tasks.items()}
        return {name: future.result() for name, future in futures.items()}


def _response_data(response: Any) -> Any:
    if not isinstance(response, dict):
        return response
    data = response.get("data", response)
    if isinstance(data, dict) and set(data) == {"data"}:
        return data["data"]
    return data


def _resolve_trip_uuid(token: str, identifier: str) -> str:
    """Resolve a booking code with one bounded leg-list request; never poll."""
    if "-" in identifier:
        return identifier
    account_ids = _get_account_ids(token)
    response = api_post(token, "/my-leg", {
        "sort": [{"departureDate": "ASC"}],
        "page": {"limit": 200, "offset": 0},
        "filter": {"accountId": account_ids},
    })
    items = response.get("data", {}).get("items", [])
    match = next((item for item in items if item.get("bookingId") == identifier), None)
    if not match or not match.get("tripId"):
        _die(f"Trip {identifier} not found", EXIT_NOT_FOUND)
    return str(match["tripId"])


def _manifest_url(envelope: dict[str, Any]) -> str | None:
    data = envelope.get("data", {})
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    return data.get("url") if isinstance(data, dict) else None


def _download_bytes(url: str, timeout: int = 60) -> bytes:
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=timeout, context=_ssl_ctx()) as response:
            return response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            json.dumps({"status": "error", "http_code": exc.code, "message": body})
        ) from exc
    except (URLError, ssl.SSLError) as exc:
        raise RuntimeError(
            json.dumps({"status": "error", "message": str(exc)})
        ) from exc


def _manifest_text(pdf: bytes) -> str:
    """Convert a manifest with AnyDoc first, then fall back to Poppler."""
    failures: list[str] = []
    converters = (
        ("AnyDoc", ["anydoc", "-", "--format", "pdf"]),
        ("Poppler", ["pdftotext", "-layout", "-", "-"]),
    )
    for name, command in converters:
        try:
            result = subprocess.run(
                command,
                input=pdf,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except FileNotFoundError:
            failures.append(f"{name}: executable not found")
            continue
        except subprocess.TimeoutExpired:
            failures.append(f"{name}: timed out after 30 seconds")
            continue
        text = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode == 0 and text:
            return text
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        failures.append(
            f"{name}: {detail[:500] or f'exit {result.returncode} with no output'}"
        )
    _die(
        "Could not convert the trip manifest with AnyDoc or Poppler. "
        "Install AnyDoc or `brew install poppler`. Attempts: "
        + "; ".join(failures),
        EXIT_ERROR,
    )


def _manifest_highlights(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tail_pattern = re.compile(r"\b(?:C-[A-Z]{4}|N[0-9][0-9A-Z]{1,5})\b", re.I)
    tail_numbers: list[str] = []
    for match in tail_pattern.findall(text):
        value = match.upper()
        if value not in tail_numbers:
            tail_numbers.append(value)

    def matching(pattern: str) -> list[str]:
        regex = re.compile(pattern, re.I)
        return [line for line in lines if regex.search(line)]

    passenger_lines: list[str] = []
    for index, line in enumerate(lines):
        if re.search(r"\bpassengers?\b", line, re.I):
            passenger_lines.extend(lines[index:index + 20])

    return {
        "tailNumbers": tail_numbers,
        "crewLines": matching(r"\b(crew|captain|pilot|first officer)\b"),
        "fboLines": matching(r"\bFBO\b|fixed[- ]base"),
        "passengerLines": list(dict.fromkeys(passenger_lines)),
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


# Noisy fields stripped in --compact mode (audit metadata, internal flags, long IDs
# that aren't typically referenced by users/agents).
_COMPACT_DROP = frozenset({
    "createdAt", "updatedAt", "modifiedAt", "version", "__v",
    "createdBy", "updatedBy", "modifiedBy",
    "isDeleted", "deletedAt",
    "tenantId", "organizationId",
})


def _compact(value: Any) -> Any:
    """Recursively strip null/empty values and known-noisy fields. Token-efficient."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in _COMPACT_DROP:
                continue
            cv = _compact(v)
            if cv is None or cv == "" or cv == [] or cv == {}:
                continue
            out[k] = cv
        return out
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _out(data: Any, fmt: str = "json", compact: bool = False) -> None:
    """Print data as JSON (default) or human-readable. `compact` strips noise."""
    if compact:
        data = _compact(data)
    if fmt == "json":
        indent = None if compact else 2
        print(json.dumps({"status": "ok", "data": data}, indent=indent, default=str, separators=(",", ":") if compact else None))
    else:
        if isinstance(data, list):
            for item in data:
                _print_dict(item)
                print()
        elif isinstance(data, dict):
            _print_dict(data)
        else:
            print(data)


def _print_dict(d: dict[str, Any], indent: int = 0) -> None:
    prefix = "  " * indent
    for k, v in d.items():
        if isinstance(v, dict):
            print(f"{prefix}{k}:")
            _print_dict(v, indent + 1)
        elif isinstance(v, list):
            print(f"{prefix}{k}: [{len(v)} items]")
        else:
            print(f"{prefix}{k}: {v}")


def _die(message: str, code: int = EXIT_ERROR) -> None:
    print(json.dumps({"status": "error", "message": message}), file=sys.stderr)
    raise typer.Exit(code)


def _parse_local_dt(value: str, tz: str | None) -> str:
    """Parse a date/time string as local time and return UTC ISO 8601.

    Accepts:
      - Already UTC: 2026-04-15T14:00:00Z → passed through
      - ISO with offset: 2026-04-15T10:00:00-04:00 → converted to UTC
      - Local (no offset): 2026-04-15T10:00 → interpreted in --timezone, converted to UTC
      - Date only: 2026-04-15 → midnight in --timezone, converted to UTC

    If the value has no timezone info, --timezone is REQUIRED.
    """
    value = value.strip()

    # Already has Z or offset → pass through
    if value.endswith("Z") or "+" in value[10:] or value[10:].count("-") > 0 and "T" in value:
        tail = value[19:] if len(value) > 19 else ""
        if value.endswith("Z") or "+" in tail or (tail and tail[0] == "-"):
            return value

    # No offset → this is local time, timezone is required
    if not tz:
        _die("--timezone is required when using local time (no Z or offset). Set AIRSPRINT_TIMEZONE or pass --tz.", EXIT_VALIDATION)

    if "T" not in value:
        value = f"{value}T00:00"  # date only → midnight

    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        _die(f"Cannot parse date: {value}. Use YYYY-MM-DDTHH:MM or YYYY-MM-DD", EXIT_VALIDATION)

    if ZoneInfo:
        try:
            local_dt = naive.replace(tzinfo=ZoneInfo(tz))
            utc_dt = local_dt.astimezone(_tz_utc.utc)
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass

    _die(f"Cannot convert local time: zoneinfo unavailable for {tz}", EXIT_ERROR)


def _fmt_epoch(epoch_ms: Any, tz: str | None = None, fmt: str = "%a %b %d, %H:%M") -> str:
    if not epoch_ms:
        return "-"
    try:
        value = float(epoch_ms)
        ts = value / 1000 if abs(value) >= EPOCH_MILLISECONDS_THRESHOLD else value
        dt = datetime.fromtimestamp(ts, tz=_tz_utc.utc)
    except (TypeError, ValueError, OSError):
        return "-"
    if tz and ZoneInfo:
        try:
            dt = dt.astimezone(ZoneInfo(tz))
        except Exception:
            pass
    return dt.strftime(fmt)


# ---------------------------------------------------------------------------
# Typer app & groups
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="airsprint",
    help="AirSprint CLI — agent-friendly interface to api.airsprint.com",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

auth_app = typer.Typer(help="Authentication commands", no_args_is_help=True)
user_app = typer.Typer(help="User & account commands", no_args_is_help=True)
trips_app = typer.Typer(help="Trip & flight commands", no_args_is_help=True)
booking_app = typer.Typer(help="Booking commands (create and cancel)", no_args_is_help=True)
leg_app = typer.Typer(help="Existing-leg updates with full-list safety", no_args_is_help=True)
explore_app = typer.Typer(help="Explore empty legs & shared flights", no_args_is_help=True)
messages_app = typer.Typer(help="In-app message commands", no_args_is_help=True)
feedback_app = typer.Typer(help="Feedback commands", no_args_is_help=True)
quote_app = typer.Typer(help="Quotes & cost estimates (via api.airsprint.com)", no_args_is_help=True)
cache_app = typer.Typer(help="Local data mirror (airports, aircraft) at ~/.airsprint_cache.json", no_args_is_help=True)
raw_app = typer.Typer(help="Raw api.airsprint.com escape hatches. Use when no typed command exists.", no_args_is_help=True)
account_app = typer.Typer(help="Account-user management (invite, update, roles)", no_args_is_help=True)
passenger_app = typer.Typer(help="Saved passengers", no_args_is_help=True)
passport_app = typer.Typer(help="Saved passports & passport documents", no_args_is_help=True)
pet_app = typer.Typer(help="Saved pets & pet documents", no_args_is_help=True)
customs_app = typer.Typer(help="Canadian customs declarations", no_args_is_help=True)
address_app = typer.Typer(help="Address autocomplete & saved addresses", no_args_is_help=True)
hours_app = typer.Typer(help="Hours-exchange marketplace (estimate, power, listings)", no_args_is_help=True)
files_app = typer.Typer(help="File uploads & retrieval", no_args_is_help=True)
content_app = typer.Typer(help="Content: FAQ, policies, system notices, concierge", no_args_is_help=True)
network_app = typer.Typer(help="My Network connections and flight-sharing groups", no_args_is_help=True)

app.add_typer(auth_app, name="auth")
app.add_typer(user_app, name="user")
app.add_typer(trips_app, name="trips")
app.add_typer(booking_app, name="booking")
app.add_typer(leg_app, name="leg")
app.add_typer(explore_app, name="explore")
app.add_typer(messages_app, name="messages")
app.add_typer(feedback_app, name="feedback")
app.add_typer(quote_app, name="quote")
app.add_typer(cache_app, name="cache")
app.add_typer(raw_app, name="raw")
app.add_typer(account_app, name="account")
app.add_typer(passenger_app, name="passenger")
app.add_typer(passport_app, name="passport")
app.add_typer(pet_app, name="pet")
app.add_typer(customs_app, name="customs")
app.add_typer(address_app, name="address")
app.add_typer(hours_app, name="hours")
app.add_typer(files_app, name="files")
app.add_typer(content_app, name="content")
app.add_typer(network_app, name="network")

# Common options
Username = typer.Option(None, "--username", "-u", envvar="AIRSPRINT_USERNAME", help="Login email")
Password = typer.Option(None, "--password", "-p", envvar="AIRSPRINT_PASSWORD", help="Login password")
Format = typer.Option("json", "--format", "-f", help="Output format: json | human")
Compact = typer.Option(False, "--compact", envvar="AIRSPRINT_COMPACT", help="Strip null/empty/noisy fields and use minimal JSON. Token-efficient for agents.")
Timezone = typer.Option(None, "--timezone", "--tz", envvar="AIRSPRINT_TIMEZONE", help="Timezone (e.g. America/Montreal). Required for local time. Env: AIRSPRINT_TIMEZONE")


def _skill_path() -> Path:
    return Path(__file__).resolve().parents[1] / "SKILL.md"


def _show_skill(value: bool) -> None:
    if not value:
        return
    skill_path = _skill_path()
    if not skill_path.exists():
        _die(f"Agent guide not found: {skill_path}", EXIT_NOT_FOUND)
    typer.echo(skill_path.read_text())
    raise typer.Exit()


@app.callback()
def app_options(
    skill: bool = typer.Option(
        False,
        "--skill",
        callback=_show_skill,
        is_eager=True,
        help="Print the canonical SKILL.md agent guide and exit.",
    ),
):
    """AirSprint owner operations. Use --skill for agent-safe workflows."""


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@auth_app.command("login")
def auth_login(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Authenticate and cache token. Returns token metadata."""
    u = username or os.environ.get("AIRSPRINT_USERNAME", "")
    p = password or os.environ.get("AIRSPRINT_PASSWORD", "")
    if not u or not p:
        _die("Credentials required.", EXIT_AUTH)
    try:
        token = _api_login(u, p)
        _save_api_token(token, u)
        _out({"authenticated": True, "email": u, "token": token[:8] + "..."}, fmt)
    except RuntimeError as e:
        _die(str(e), EXIT_AUTH)


@auth_app.command("logout")
def auth_logout():
    """Clear the cached AirSprint API token."""
    _clear_api_token()
    _out({"message": "Token cleared"})


@auth_app.command("status")
def auth_status(fmt: str = Format):
    """Check if cached token is valid."""
    api_token = _load_api_token()
    if api_token:
        _out({
            "authenticated": True,
            "token_cached": True,
            "message": "A valid API token is cached.",
        }, fmt)
    else:
        _out({"authenticated": False, "message": "No valid token cached"}, fmt)


# ---------------------------------------------------------------------------
# user
# ---------------------------------------------------------------------------


@user_app.command("profile")
def user_profile(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get current user profile."""
    token = get_api_token(username, password)
    resp = api_get(token, "/me")
    _out(resp.get("data", resp), fmt)


@user_app.command("accounts")
def user_accounts(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get account info (shares, aircraft, access levels, hours)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-accounts", {})
    items = resp.get("data", {}).get("items", [])
    _out(items, fmt)


@user_app.command("preferences")
def user_preferences(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get notification settings (GET /my-notification-settings)."""
    token = get_api_token(username, password)
    data = api_get(token, "/my-notification-settings")
    _out(data, fmt)


@user_app.command("set-preferences")
def user_set_preferences(
    body: str = typer.Option(..., "--body", help="JSON body with notification-setting fields"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Update notification settings (POST /my-notification-settings)."""
    payload = _parse_json(body)
    token = get_api_token(username, password)
    data = api_post(token, "/my-notification-settings", payload)
    _out(data, fmt)


@user_app.command("update")
def user_update(
    body: str = typer.Option(..., "--body", help='JSON body — fields to update, e.g. {"firstName":"X","phone":"5551234"}. Wrapped in {"options":...} automatically; pass {"options":...} to override.'),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Update user profile (PATCH /my-user)."""
    payload = _parse_json(body)
    if "options" not in payload:
        payload = {"options": payload}
    token = get_api_token(username, password)
    data = api_patch(token, "/my-user", payload)
    _out(data, fmt)


# ---------------------------------------------------------------------------
# trips
# ---------------------------------------------------------------------------


@trips_app.command("list")
def trips_list(
    upcoming: bool = typer.Option(True, "--upcoming/--past", help="Show upcoming (default) or past trips"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max trips to return"),
    json_output: bool = typer.Option(False, "--json", help="Explicit JSON output alias (JSON is already the default)"),
    timezone: Optional[str] = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """List trips (including interchange flights).

    Uses api.airsprint.com which returns all trip types including interchange.
    """
    token = get_api_token(username, password)
    account_ids = _get_account_ids(token)
    if not account_ids:
        _die("No accounts found", EXIT_ERROR)

    now = datetime.now(tz=_tz_utc.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    time_filter = {"min": now} if upcoming else {"max": now}
    sort_dir = "ASC" if upcoming else "DESC"

    payload = {
        "sort": [{"departureDate": sort_dir}],
        "page": {"limit": limit, "offset": 0},
        "filter": {
            "departureTime": time_filter,
            "accountId": account_ids,
        },
    }
    resp = api_post(token, "/my-leg", payload)
    items = resp.get("data", {}).get("items", [])
    if json_output:
        fmt = "json"
    _out(items, fmt, compact)


@trips_app.command("get")
def trips_get(
    booking_id: str = typer.Option(..., "--id", help="Booking ID (e.g. IYIBL)"),
    probe: bool = typer.Option(
        False,
        "--probe/--no-probe",
        help="Override the default no-probe cooldown after a live booking write.",
    ),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get a specific trip by trip-UUID or booking code (e.g. BAKEW)."""
    _guard_booking_probe(probe)
    token = get_api_token(username, password)
    trip_uuid = _resolve_trip_uuid(token, booking_id)
    try:
        data = api_get(token, f"/trip/{trip_uuid}")
    except RuntimeError as exc:
        msg = str(exc)
        if "404" in msg or "not found" in msg.lower():
            _die(f"Trip {booking_id} not found", EXIT_NOT_FOUND)
        raise
    _out(data.get("data", data), fmt)


@trips_app.command("show")
def trips_show(
    booking_id: str = typer.Option(..., "--id", help="Trip UUID or booking code"),
    probe: bool = typer.Option(
        False,
        "--probe/--no-probe",
        help="Override the default no-probe cooldown after a live booking write.",
    ),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Merge trip JSON with tail/crew/FBO/passenger data from its manifest.

    This performs one trip GET and one manifest GET. It never retries either,
    polls, or reads back after a write.
    """
    _guard_booking_probe(probe)
    token = get_api_token(username, password)
    trip_uuid = _resolve_trip_uuid(token, booking_id)
    trip = _response_data(api_get(token, f"/trip/{trip_uuid}"))
    envelope = api_get(token, f"/trip/manifest/{trip_uuid}")
    pdf_url = _manifest_url(envelope)
    if not pdf_url:
        _die(f"No manifest available for {booking_id}", EXIT_NOT_FOUND)
    text = _manifest_text(_download_bytes(pdf_url))
    _out({
        "trip": trip,
        "manifest": {
            "highlights": _manifest_highlights(text),
            "text": text,
        },
    }, fmt, compact)


@trips_app.command("tripsheet")
def trips_tripsheet(
    booking_id: str = typer.Option(..., "--id", help="Trip UUID or booking code (e.g. BAKEW)"),
    output: str = typer.Option("-", "--output", "-o", help="Output file path (- for stdout info)"),
    probe: bool = typer.Option(
        False,
        "--probe/--no-probe",
        help="Override the default no-probe cooldown after a live booking write.",
    ),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
):
    """Download trip sheet (manifest) PDF (GET /trip/manifest/{id}).

    The endpoint returns a JSON envelope with a presigned S3 URL; this command
    follows the URL and saves the PDF (or reports the URL with --output -).
    """
    _guard_booking_probe(probe)
    token = get_api_token(username, password)
    trip_uuid = _resolve_trip_uuid(token, booking_id)
    try:
        envelope = api_get(token, f"/trip/manifest/{trip_uuid}")
    except RuntimeError as exc:
        msg = str(exc)
        if "404" in msg or "not found" in msg.lower() or "Flight not found" in msg:
            _die(f"No manifest available for {booking_id} (flight may not have departed yet)", EXIT_NOT_FOUND)
        raise
    pdf_url = _manifest_url(envelope)
    if not pdf_url:
        _die(f"No manifest URL returned for {booking_id}", EXIT_NOT_FOUND)
    if output == "-":
        _out({"url": pdf_url, "message": "Use --output FILE to download the PDF."})
        return
    content = _download_bytes(pdf_url)
    Path(output).write_bytes(content)
    _out({"message": f"Saved to {output}", "size_bytes": len(content)})


@trips_app.command("flight-feedback")
def trips_flight_feedback(
    trip_id: str = typer.Option(..., "--id", help="Trip ID"),
    body: str = typer.Option(..., "--body", help="JSON feedback body"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Submit flight feedback for a completed trip."""
    payload = _parse_json(body)
    token = get_api_token(username, password)
    payload.setdefault("tripId", trip_id)
    data = api_post(token, "/booking-survey/create", payload)
    _out(data, fmt)


# ---------------------------------------------------------------------------
# booking
# ---------------------------------------------------------------------------


@booking_app.command("info")
def booking_info(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Compose booking prep data: aircraft, passengers, and saved airports.

    Run this BEFORE creating a booking to get valid reference values
    (aircraftId, departureAirportId, passenger IDs, etc.). The account is
    implicit in the auth token and is not submitted to /trip/book.
    """
    token = get_api_token(username, password)
    responses = _parallel_read_calls({
        "aircraft": lambda: api_post(token, "/my-aircraft", {}),
        "passengers": lambda: api_post(token, "/my-passenger", {
            "sort": [],
            "page": {"limit": 200, "offset": 0},
            "filter": {},
        }),
        "airports": lambda: api_post(token, "/airport", {
            "sort": [],
            "page": {"limit": 50, "offset": 0},
            "filter": {"saved": True},
        }),
    })
    aircraft = responses["aircraft"].get("data", {}).get("items", [])
    passengers = responses["passengers"].get("data", {}).get("items", [])
    airports = responses["airports"].get("data", {}).get("items", [])
    _out({
        "aircraft": aircraft,
        "passengers": passengers,
        "savedAirports": airports,
    }, fmt)


_DESTINATION_ADDRESS_FIELDS = ("street", "city", "state", "zip", "country")


def _destination_address(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    if not all(isinstance(value.get(key), str) and value[key].strip() for key in _DESTINATION_ADDRESS_FIELDS):
        return None
    return {key: value[key].strip() for key in _DESTINATION_ADDRESS_FIELDS}


def _airport_country(airport_id: Any) -> tuple[str | None, str | None]:
    if not isinstance(airport_id, str):
        return None, None
    return _airport_by_id().get(airport_id, (None, None))


def _is_us_country(country: str | None) -> bool:
    if not country:
        return False
    normalized = re.sub(r"[^a-z]", "", country.lower())
    return normalized in {"us", "usa", "unitedstates", "unitedstatesofamerica"}


def _prepare_booking_destination_addresses(
    payload: dict[str, Any],
    address: dict[str, str] | None,
    us_touching_override: bool | None,
) -> dict[str, Any]:
    countries: list[dict[str, Any]] = []
    unresolved: list[str] = []
    us_detected = False
    for leg in payload.get("legs", []):
        for field in ("departureAirportId", "arrivalAirportId"):
            airport_id = leg.get(field)
            country, icao = _airport_country(airport_id)
            countries.append({"airportId": airport_id, "icao": icao, "country": country})
            if country is None:
                unresolved.append(str(airport_id))
            if _is_us_country(country) or (
                country is None and icao and icao.startswith(("K", "P"))
            ):
                us_detected = True

    if us_touching_override is None and unresolved and not us_detected:
        _die(
            "Could not determine every airport country from the local mirror. "
            "Run `cache refresh`, or pass --us-touching/--not-us-touching explicitly.",
            EXIT_VALIDATION,
        )
    us_touching = us_detected if us_touching_override is None else us_touching_override

    if address is None:
        for leg in payload.get("legs", []):
            for passenger in leg.get("passengers", []):
                if isinstance(passenger, dict):
                    address = _destination_address(passenger.get("destinationAddress"))
                    if address:
                        break
            if address:
                break

    if us_touching and address is None:
        _die(
            "US-touching bookings require --destination-address with street, city, "
            "state, zip, and country. No booking was sent.",
            EXIT_VALIDATION,
        )

    if address is not None:
        for index, leg in enumerate(payload.get("legs", [])):
            passengers = leg.get("passengers")
            if not isinstance(passengers, list) or not passengers:
                if us_touching:
                    _die(f'"legs[{index}].passengers" must not be empty for a US trip.', EXIT_VALIDATION)
                continue
            normalized: list[dict[str, Any]] = []
            for passenger in passengers:
                if isinstance(passenger, str):
                    passenger = {"id": passenger}
                if not isinstance(passenger, dict) or not passenger.get("id"):
                    _die(
                        f'Every "legs[{index}].passengers" item must contain a saved passenger ID.',
                        EXIT_VALIDATION,
                    )
                passenger = dict(passenger)
                passenger["destinationAddress"] = dict(address)
                normalized.append(passenger)
            leg["passengers"] = normalized

    return {"usTouching": us_touching, "airports": countries}


@booking_app.command("create")
def booking_create(
    body: str = typer.Option(..., "--body", help='JSON body for POST /trip/book'),
    destination_address: Optional[str] = typer.Option(
        None,
        "--destination-address",
        help='JSON object with street, city, state, zip, country; copied to every passenger on every leg.',
    ),
    us_touching: Optional[bool] = typer.Option(
        None,
        "--us-touching/--not-us-touching",
        help="Override country detection when airport IDs are not in the local cache.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show payload without submitting"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Book a new trip (POST /trip/book).

    Required body schema (top-level keys):
        legs:           [{ departureAirportId, arrivalAirportId, aircraftId,
                           date, numberOfSeats, passengers: [], petIds: [],
                           requestSettings: {} }]
        baggage:        []
        shareSettings:  {}

    Run `airsprint booking info` first to get valid IDs.
    """
    payload = _parse_json(body)

    if "accountId" in payload:
        _die('Remove top-level "accountId"; the booking account is implicit in the token.', EXIT_VALIDATION)

    for key in ("legs", "baggage", "shareSettings"):
        if key not in payload:
            _die(f'Body must contain "{key}" (see --help for full schema).', EXIT_VALIDATION)

    legs = payload["legs"]
    if not isinstance(legs, list) or not legs:
        _die('"legs" must be a non-empty array.', EXIT_VALIDATION)
    leg_required = (
        "departureAirportId", "arrivalAirportId", "aircraftId", "date",
        "numberOfSeats", "passengers", "petIds", "requestSettings",
    )
    for index, leg in enumerate(legs):
        if not isinstance(leg, dict):
            _die(f'"legs[{index}]" must be an object.', EXIT_VALIDATION)
        missing = [key for key in leg_required if key not in leg]
        if missing:
            _die(f'"legs[{index}]" is missing: {", ".join(missing)}', EXIT_VALIDATION)
        settings = leg.get("requestSettings")
        if not isinstance(settings, dict) or not all(
            key in settings for key in ("cateringRequired", "groundTransportationRequired")
        ):
            _die(
                f'"legs[{index}].requestSettings" must include '
                '"cateringRequired" and "groundTransportationRequired".',
                EXIT_VALIDATION,
            )

    share = payload["shareSettings"]
    if not isinstance(share, dict):
        _die('"shareSettings" must be an object.', EXIT_VALIDATION)
    share_required = (
        "specialRequests", "openToShare", "networkType", "seats",
        "petsAllowed", "childrenAllowed",
    )
    missing_share = [key for key in share_required if key not in share]
    if missing_share:
        _die(f'"shareSettings" is missing: {", ".join(missing_share)}', EXIT_VALIDATION)
    if share.get("networkType") not in ("MY_NETWORK", "AIRSPRINT_NETWORK"):
        _die(
            '"shareSettings.networkType" must be MY_NETWORK or AIRSPRINT_NETWORK.',
            EXIT_VALIDATION,
        )
    percentage = share.get("joinerVariableCostPercentage")
    if percentage is not None and (
        not isinstance(percentage, (int, float)) or isinstance(percentage, bool)
        or not 30 <= percentage <= 80
    ):
        _die(
            '"shareSettings.joinerVariableCostPercentage" must be between 30 and 80.',
            EXIT_VALIDATION,
        )
    if share.get("specificGroupsOnly") and not share.get("groupIds"):
        _die(
            '"shareSettings.groupIds" is required when "specificGroupsOnly" is true.',
            EXIT_VALIDATION,
        )

    parsed_address: dict[str, str] | None = None
    if destination_address is not None:
        parsed_address = _destination_address(_parse_json(destination_address))
        if parsed_address is None:
            _die(
                "--destination-address requires non-empty street, city, state, zip, and country.",
                EXIT_VALIDATION,
            )
    destination_check = _prepare_booking_destination_addresses(
        payload,
        parsed_address,
        us_touching,
    )

    if dry_run:
        _out({
            "dry_run": True,
            "payload": payload,
            "destinationCheck": destination_check,
            "message": "Would POST /trip/book exactly once; no read-back would follow.",
        }, fmt)
        return

    token = get_api_token(username, password)
    data = api_post(token, "/trip/book", payload)
    _out(data, fmt)


@booking_app.command("cancel")
def booking_cancel(
    booking_id: Optional[str] = typer.Option(None, "--id", help="Booking code (e.g. BAKEW) — resolved to tripId"),
    trip_id: Optional[str] = typer.Option(None, "--trip-id", help="Trip UUID (alternative to --id)"),
    leg_id: Optional[str] = typer.Option(None, "--leg-id", help="Cancel a single leg by leg UUID"),
    leg_ids: Optional[str] = typer.Option(None, "--leg-ids", help="Comma-separated leg UUIDs"),
    reason: str = typer.Option(..., "--reason", help="Cancellation reason (required by API)"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before the single live cancellation request."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show payload without submitting"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Cancel a trip or specific legs (POST /cancel-own).

    One of --id, --trip-id, --leg-id, or --leg-ids is required.
    --id resolves a booking code (e.g. BAKEW) to its tripId via /my-leg.
    """
    if not any([booking_id, trip_id, leg_id, leg_ids]):
        _die("Provide one of --id, --trip-id, --leg-id, --leg-ids", EXIT_VALIDATION)

    payload: dict[str, Any] = {"reason": reason}
    if leg_id:
        payload["legId"] = leg_id
    elif leg_ids:
        payload["legIds"] = [s.strip() for s in leg_ids.split(",") if s.strip()]
    elif trip_id:
        payload["tripId"] = trip_id
    elif booking_id:
        token_for_lookup = get_api_token(username, password)
        payload["tripId"] = _resolve_trip_uuid(token_for_lookup, booking_id)

    if dry_run:
        _out({"dry_run": True, "payload": payload, "message": "Would POST /cancel-own exactly once"}, fmt)
        return
    if not confirm:
        _die("--confirm required to cancel a booking or leg.", EXIT_VALIDATION)

    token = get_api_token(username, password)
    data = api_post(token, "/cancel-own", payload)
    _out(data, fmt)


# ---------------------------------------------------------------------------
# leg — safe updates to existing booked legs
# ---------------------------------------------------------------------------


def _leg_passenger_rows(leg: dict[str, Any]) -> list[Any]:
    for key in ("passengers", "legPassengers"):
        value = leg.get(key)
        if isinstance(value, list):
            return value
    options = leg.get("options")
    if isinstance(options, dict) and isinstance(options.get("passengers"), list):
        return options["passengers"]
    return []


def _saved_passenger_id(row: Any) -> str | None:
    """Return the saved passenger UUID, never the legPassenger row ID."""
    if isinstance(row, str):
        return row
    if not isinstance(row, dict):
        return None
    for key in ("passengerId", "myPassengerId", "savedPassengerId"):
        if isinstance(row.get(key), str) and row[key]:
            return row[key]
    for key in ("passenger", "myPassenger", "savedPassenger"):
        nested = row.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("id"), str):
            return nested["id"]
    return None


def _passenger_name(row: Any) -> str:
    if not isinstance(row, dict):
        return str(row)
    candidates = [row]
    candidates.extend(
        value for key in ("passenger", "myPassenger", "savedPassenger")
        if isinstance((value := row.get(key)), dict)
    )
    for candidate in candidates:
        name = candidate.get("name") or candidate.get("fullName")
        if isinstance(name, str) and name.strip():
            return name.strip()
        parts = [candidate.get("firstName"), candidate.get("middleName"), candidate.get("lastName")]
        joined = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        if joined:
            return joined
    return _saved_passenger_id(row) or "unknown passenger"


def _leg_passenger_payload(row: Any) -> dict[str, Any] | None:
    saved_id = _saved_passenger_id(row)
    if not saved_id:
        return None
    payload: dict[str, Any] = {"id": saved_id}
    if isinstance(row, dict):
        nested = next(
            (
                row.get(key) for key in ("passenger", "myPassenger", "savedPassenger")
                if isinstance(row.get(key), dict)
            ),
            {},
        )
        for key in ("passportIds", "destinationAddress"):
            value = row.get(key, nested.get(key))
            if value not in (None, "", [], {}):
                payload[key] = value
    return payload


@leg_app.command("update-passengers")
def leg_update_passengers(
    leg_id: str = typer.Option(..., "--leg-id", help="Booked-leg UUID"),
    add: Optional[str] = typer.Option(None, "--add", help="Comma-separated saved passenger UUIDs to add"),
    remove: Optional[str] = typer.Option(None, "--remove", help="Comma-separated saved passenger UUIDs to remove"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Read once and print kept/added/dropped without PATCH"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before the single PATCH"),
    probe: bool = typer.Option(
        False,
        "--probe/--no-probe",
        help="Override the no-probe cooldown if another booking write just occurred.",
    ),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Merge saved-passenger IDs into the leg's complete passenger list.

    The command performs one GET before the PATCH, preserves every current
    passenger unless explicitly removed, sends saved passenger UUIDs rather
    than legPassenger IDs, performs one PATCH, and never reads back.
    """
    add_ids = _parse_ids(add or "", "--add") if add else []
    remove_ids = _parse_ids(remove or "", "--remove") if remove else []
    if not add_ids and not remove_ids:
        _die("Provide --add and/or --remove saved passenger UUIDs.", EXIT_VALIDATION)
    overlap = sorted(set(add_ids) & set(remove_ids))
    if overlap:
        _die("The same passenger cannot be added and removed: " + ", ".join(overlap), EXIT_VALIDATION)

    _guard_booking_probe(probe)
    token = get_api_token(username, password)
    leg = _response_data(api_get(token, f"/leg/{leg_id}"))
    if not isinstance(leg, dict):
        _die(f"Unexpected leg response for {leg_id}; no PATCH sent.", EXIT_ERROR)
    rows = _leg_passenger_rows(leg)
    current: list[dict[str, Any]] = []
    labels: dict[str, str] = {}
    unresolved: list[str] = []
    for row in rows:
        payload = _leg_passenger_payload(row)
        if payload is None:
            unresolved.append(_passenger_name(row))
            continue
        saved_id = payload["id"]
        if saved_id not in labels:
            current.append(payload)
            labels[saved_id] = _passenger_name(row)
    if unresolved:
        _die(
            "Could not resolve saved passenger UUIDs for: " + ", ".join(unresolved)
            + ". No PATCH sent; refusing to risk replacing the full list.",
            EXIT_VALIDATION,
        )

    dropped = [item for item in current if item["id"] in remove_ids]
    kept = [item for item in current if item["id"] not in remove_ids]
    existing_ids = {item["id"] for item in kept}
    added = [{"id": passenger_id} for passenger_id in add_ids if passenger_id not in existing_ids]
    final_passengers = kept + added
    plan = {
        "kept": [{"id": item["id"], "name": labels.get(item["id"], item["id"])} for item in kept],
        "added": added,
        "dropped": [{"id": item["id"], "name": labels.get(item["id"], item["id"])} for item in dropped],
    }
    path = f"/leg/{leg_id}"
    payload = {"options": {"passengers": final_passengers}}
    if dry_run:
        _out({
            "dry_run": True,
            "method": "PATCH",
            "path": path,
            "plan": plan,
            "payload": payload,
            "message": "One GET was made; no PATCH was sent.",
        }, fmt, compact)
        return
    if not confirm:
        _die("--confirm required for the single leg passenger PATCH.", EXIT_VALIDATION)
    result = api_patch(token, path, payload)
    _out({
        "result": result,
        "plan": plan,
        "message": "PATCH sent exactly once; no read-back performed. Wait at least 8 seconds before probing.",
    }, fmt, compact)


# ---------------------------------------------------------------------------
# explore
# ---------------------------------------------------------------------------


@explore_app.command("flights")
def explore_flights(
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """List available empty legs and shared flights."""
    token = get_api_token(username, password)
    now = datetime.now(tz=_tz_utc.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    resp = api_post(token, "/my-flights", {
        "sort": [{"departureTimestamp": "ASC"}],
        "page": {"limit": limit, "offset": 0},
        "filter": {
            "departureTime": {"min": now},
            "type": ["EMPTY_LEG"],
            "locked": False,
        },
    })
    items = resp.get("data", {}).get("items", [])
    _out(items, fmt, compact)


@explore_app.command("counts")
def explore_counts(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get dashboard counts (unread messages, upcoming trips, empty legs)."""
    token = get_api_token(username, password)
    account_ids = _get_account_ids(token)
    now = datetime.now(tz=_tz_utc.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    tasks: dict[str, Callable[[], dict[str, Any]]] = {
        "notifications": lambda: api_post(token, "/my-notifications", {
            "sort": [], "page": {"limit": 1, "offset": 0},
            "filter": {"isRead": False},
        }),
        "empty_legs": lambda: api_post(token, "/my-flights", {
            "sort": [], "page": {"limit": 1, "offset": 0},
            "filter": {
                "departureTime": {"min": now},
                "type": ["EMPTY_LEG"],
                "locked": False,
            },
        }),
    }
    if account_ids:
        tasks["upcoming"] = lambda: api_post(token, "/my-leg", {
            "sort": [], "page": {"limit": 1, "offset": 0},
            "filter": {"departureTime": {"min": now}, "accountId": account_ids},
        })
    responses = _parallel_read_calls(tasks)
    unread = responses["notifications"].get("data", {}).get("total", 0)
    upcoming = responses.get("upcoming", {}).get("data", {}).get("total", 0)
    empty_legs = responses["empty_legs"].get("data", {}).get("total", 0)

    _out({
        "unreadMessages": unread,
        "upcomingTrips": upcoming,
        "emptyLegs": empty_legs,
    }, fmt)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


@messages_app.command("list")
def messages_list(
    unread: Optional[bool] = typer.Option(None, "--unread/--all", help="Filter unread only"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """List in-app notifications/messages."""
    token = get_api_token(username, password)
    filt: dict[str, Any] = {}
    if unread is True:
        filt["isRead"] = False
    resp = api_post(token, "/my-notifications", {
        "sort": [],
        "page": {"limit": limit, "offset": 0},
        "filter": filt,
    })
    items = resp.get("data", {}).get("items", [])
    _out(items, fmt)


@messages_app.command("read")
def messages_read(
    message_id: str = typer.Option(..., "--id", help="Message ID to mark as read (or comma-separated IDs)"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Mark one or more messages as read."""
    token = get_api_token(username, password)
    ids = [s.strip() for s in message_id.split(",") if s.strip()]
    data = api_patch(token, "/my-notifications/update", {"ids": ids, "isRead": True})
    _out(data, fmt)


@messages_app.command("read-all")
def messages_read_all(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Mark all unread messages as read."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-notifications", {
        "sort": [],
        "page": {"limit": 500, "offset": 0},
        "filter": {"isRead": False},
    })
    items = resp.get("data", {}).get("items", [])
    ids = [i["id"] for i in items if i.get("id")]
    if not ids:
        _out({"updated": 0, "message": "No unread notifications"}, fmt)
        return
    data = api_patch(token, "/my-notifications/update", {"ids": ids, "isRead": True})
    _out(data, fmt)


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------


@feedback_app.command("submit")
def feedback_submit(
    body: str = typer.Option(..., "--body", help="JSON feedback body"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Submit feedback to AirSprint."""
    payload = _parse_json(body)
    token = get_api_token(username, password)
    data = api_post(token, "/feedback/create", payload)
    _out(data, fmt)


# ---------------------------------------------------------------------------
# Local data cache (airports, aircraft) — persistent disk mirror
# ---------------------------------------------------------------------------


def _load_data_cache() -> dict[str, Any]:
    global _DATA_CACHE_MEMORY, _DATA_CACHE_MEMORY_MTIME_NS
    global _DATA_CACHE_MEMORY_PATH, _AIRPORT_BY_ID
    try:
        mtime_ns = DATA_CACHE.stat().st_mtime_ns
    except OSError:
        _DATA_CACHE_MEMORY = None
        _DATA_CACHE_MEMORY_MTIME_NS = None
        _DATA_CACHE_MEMORY_PATH = DATA_CACHE
        _AIRPORT_BY_ID = None
        return {}
    if (
        _DATA_CACHE_MEMORY is not None
        and _DATA_CACHE_MEMORY_PATH == DATA_CACHE
        and _DATA_CACHE_MEMORY_MTIME_NS == mtime_ns
    ):
        return _DATA_CACHE_MEMORY
    try:
        cache = json.loads(DATA_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        _DATA_CACHE_MEMORY = None
        _DATA_CACHE_MEMORY_MTIME_NS = None
        _DATA_CACHE_MEMORY_PATH = DATA_CACHE
        _AIRPORT_BY_ID = None
        return {}
    if not isinstance(cache, dict):
        _DATA_CACHE_MEMORY = None
        _DATA_CACHE_MEMORY_MTIME_NS = None
        _DATA_CACHE_MEMORY_PATH = DATA_CACHE
        _AIRPORT_BY_ID = None
        return {}
    _DATA_CACHE_MEMORY = cache
    _DATA_CACHE_MEMORY_MTIME_NS = mtime_ns
    _DATA_CACHE_MEMORY_PATH = DATA_CACHE
    _AIRPORT_BY_ID = None
    return cache


def _save_data_cache(cache: dict[str, Any]) -> None:
    global _DATA_CACHE_MEMORY, _DATA_CACHE_MEMORY_MTIME_NS
    global _DATA_CACHE_MEMORY_PATH, _AIRPORT_BY_ID
    _atomic_write_json(DATA_CACHE, cache)
    _DATA_CACHE_MEMORY = cache
    _DATA_CACHE_MEMORY_MTIME_NS = DATA_CACHE.stat().st_mtime_ns
    _DATA_CACHE_MEMORY_PATH = DATA_CACHE
    _AIRPORT_BY_ID = None


def _cache_section_fresh(
    cache: dict[str, Any],
    key: str,
    ttl: int = DATA_CACHE_TTL,
) -> bool:
    section = cache.get(key) or {}
    return bool(section) and (time.time() - section.get("_cached_at", 0)) < ttl


def _prepare_cache_for_token(cache: dict[str, Any], token: str) -> bool:
    """Invalidate owner-specific cache sections when credentials change."""
    from hashlib import sha256

    owner = sha256(token.encode("utf-8")).hexdigest()[:16]
    if cache.get("_owner") == owner:
        return False
    cache["_owner"] = owner
    cache.pop("accounts", None)
    cache.pop("my_aircraft", None)
    return True


def _refresh_accounts(token: str, cache: dict[str, Any]) -> list[dict[str, Any]]:
    response = api_post(token, "/my-accounts", {})
    items = response.get("data", {}).get("items", []) or []
    accounts = [item for item in items if isinstance(item, dict)]
    cache["accounts"] = {"_cached_at": int(time.time()), "items": accounts}
    return accounts


def _get_accounts(token: str, refresh: bool = False) -> list[dict[str, Any]]:
    cache = _load_data_cache()
    _prepare_cache_for_token(cache, token)
    if refresh or not _cache_section_fresh(cache, "accounts", ACCOUNT_CACHE_TTL):
        _refresh_accounts(token, cache)
        _save_data_cache(cache)
    items = (cache.get("accounts") or {}).get("items") or []
    return [item for item in items if isinstance(item, dict)]


def _refresh_airports(token: str, cache: dict[str, Any]) -> None:
    """Fetch all airports the user can see and mirror them locally."""
    items: list[dict[str, Any]] = []
    offset = 0
    page = 200
    while True:
        resp = api_post(token, "/airport", {
            "sort": [],
            "page": {"limit": page, "offset": offset},
            "filter": {},
        })
        batch = resp.get("data", {}).get("items", [])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < page:
            break
        offset += page
    by_icao: dict[str, dict[str, str]] = {}
    for a in items:
        icao = (a.get("codeICAO") or "").upper()
        if not icao or "id" not in a:
            continue
        by_icao[icao] = {
            "id": a["id"],
            "iata": a.get("codeIATA", ""),
            "name": a.get("name", ""),
            "city": (a.get("address") or {}).get("city", ""),
            "country": (a.get("address") or {}).get("country", ""),
        }
    cache["airports"] = {"_cached_at": int(time.time()), "by_icao": by_icao}


def _refresh_aircraft(token: str, cache: dict[str, Any]) -> None:
    resp = api_post(token, "/aircraft")
    items = resp.get("data", {}).get("items", [])
    by_id = {
        a["id"]: {"name": a.get("aircraftName", a.get("name", ""))}
        for a in items if "id" in a
    }
    cache["aircraft"] = {"_cached_at": int(time.time()), "by_id": by_id}


def _refresh_my_aircraft(token: str, cache: dict[str, Any]) -> None:
    resp = api_post(token, "/my-aircraft")
    items = resp.get("data", {}).get("items", [])
    cache["my_aircraft"] = {"_cached_at": int(time.time()), "items": items}


def _airport_by_id() -> dict[str, tuple[str | None, str]]:
    global _AIRPORT_BY_ID
    if _AIRPORT_BY_ID is None:
        airports = (_load_data_cache().get("airports") or {}).get("by_icao") or {}
        _AIRPORT_BY_ID = {
            airport["id"]: (airport.get("country") or None, icao)
            for icao, airport in airports.items()
            if isinstance(airport, dict) and isinstance(airport.get("id"), str)
        }
    return _AIRPORT_BY_ID


def _resolve_airport(token: str, icao: str) -> str:
    """Resolve ICAO code to api.airsprint.com airport UUID, using local mirror first."""
    icao = icao.upper()
    cache = _load_data_cache()

    # Try cached mirror first
    section = cache.get("airports") or {}
    by_icao = section.get("by_icao") or {}
    if icao in by_icao:
        return by_icao[icao]["id"]

    # Fall back to single-airport lookup; opportunistically extend cache
    resp = api_post(token, "/airport", {
        "sort": [], "page": {"limit": 1, "offset": 0},
        "filter": {"query": icao},
    })
    items = resp.get("data", {}).get("items", [])
    for a in items:
        code = (a.get("codeICAO") or "").upper()
        if code == icao and "id" in a:
            by_icao = section.get("by_icao") or {}
            by_icao[icao] = {
                "id": a["id"],
                "iata": a.get("codeIATA", ""),
                "name": a.get("name", ""),
                "city": (a.get("address") or {}).get("city", ""),
                "country": (a.get("address") or {}).get("country", ""),
            }
            section["by_icao"] = by_icao
            section.setdefault("_cached_at", int(time.time()))
            cache["airports"] = section
            _save_data_cache(cache)
            return a["id"]

    _die(f"Airport not found: {icao}", EXIT_NOT_FOUND)


def _get_default_aircraft(token: str) -> str:
    """Get the first aircraft UUID from the user's account, cached on disk."""
    cache = _load_data_cache()
    _prepare_cache_for_token(cache, token)
    if not _cache_section_fresh(cache, "my_aircraft"):
        _refresh_my_aircraft(token, cache)
        _save_data_cache(cache)
    items = (cache.get("my_aircraft") or {}).get("items") or []
    if not items:
        _die("No aircraft found on account", EXIT_NOT_FOUND)
    return items[0]["aircraftId"]


def _get_account_aircraft_id(token: str, value: str | None = None) -> str:
    """Resolve the account-aircraft record ID required by Hours Exchange."""
    if value:
        return value
    cache = _load_data_cache()
    _prepare_cache_for_token(cache, token)
    if not _cache_section_fresh(cache, "my_aircraft"):
        _refresh_my_aircraft(token, cache)
        _save_data_cache(cache)
    items = (cache.get("my_aircraft") or {}).get("items") or []
    if not items:
        _die("No aircraft found on account", EXIT_NOT_FOUND)
    if len(items) > 1:
        _die(
            "Multiple account aircraft found; pass --account-aircraft-id from `quote aircraft`.",
            EXIT_VALIDATION,
        )
    account_aircraft_id = items[0].get("id")
    if not account_aircraft_id:
        _die("Account aircraft record has no id", EXIT_NOT_FOUND)
    return account_aircraft_id


def _hours_estimate_query(
    token: str,
    body: str | None,
    account_aircraft_id: str | None,
    hours: float | None,
    action: str | None,
) -> dict[str, Any]:
    query = _parse_json(body) if body else {}
    if account_aircraft_id:
        query["accountAircraftId"] = account_aircraft_id
    if hours is not None:
        query["hours"] = hours
    if action:
        query["type"] = action.upper()
    query["accountAircraftId"] = _get_account_aircraft_id(
        token, query.get("accountAircraftId")
    )
    if "hours" not in query:
        _die("--hours is required (or include hours in --body).", EXIT_VALIDATION)
    if query.get("type") not in ("BUY", "SELL"):
        _die("--type must be BUY or SELL (or include type in --body).", EXIT_VALIDATION)
    return query


# ---------------------------------------------------------------------------
# quote (api.airsprint.com)
# ---------------------------------------------------------------------------


@quote_app.command("flight")
def quote_flight(
    departure: Optional[str] = typer.Option(None, "--from", help="Departure ICAO code (e.g. CYQB). Resolved to UUID automatically."),
    arrival: Optional[str] = typer.Option(None, "--to", help="Arrival ICAO code (e.g. KTEB). Resolved to UUID automatically."),
    date: Optional[str] = typer.Option(None, "--date", help="Departure date/time in local time (e.g. 2026-04-15T10:00, 2026-04-15). Converted to UTC using --timezone."),
    body: Optional[str] = typer.Option(None, "--body", help="Full JSON body (overrides --from/--to/--date)"),
    timezone: Optional[str] = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get a flight quote with real server-side pricing from AirSprint.

    Two modes:

    1. Simple: --from CYQB --to KTEB --date 2026-04-15T10:00
       (ICAO auto-resolved, local time converted to UTC via --timezone, uses your default aircraft)

    2. Advanced: --body '{"legs": [{"aircraftId": "UUID", "departureAirportId": "UUID", ...}]}'
       (pass UUIDs directly — get them from `quote airports` and `quote aircraft`)

    Date accepts local time (requires --timezone or AIRSPRINT_TIMEZONE), e.g.:
      --date 2026-04-15T10:00 --tz America/Montreal  → 10:00 AM Eastern
      --date 2026-04-15 --tz America/Montreal         → midnight Eastern
      --date 2026-04-15T14:00:00Z                     → already UTC, no --tz needed
    """
    token = get_api_token(username, password)

    if body:
        payload = _parse_json(body)
    elif departure and arrival and date:
        date_utc = _parse_local_dt(date, timezone)
        dep_id = _resolve_airport(token, departure)
        arr_id = _resolve_airport(token, arrival)
        ac_id = _get_default_aircraft(token)
        payload = {
            "legs": [{
                "aircraftId": ac_id,
                "departureAirportId": dep_id,
                "arrivalAirportId": arr_id,
                "departureDateUTC": date_utc,
            }]
        }
    else:
        _die("Provide either --from/--to/--date or --body", EXIT_VALIDATION)

    try:
        resp = api_post(token, "/flight-quote", payload)
        _out(resp.get("data", resp), fmt)
    except RuntimeError as e:
        _die(str(e), EXIT_ERROR)


@quote_app.command("roundtrip")
def quote_roundtrip(
    departure: str = typer.Option(..., "--from", help="Departure ICAO (e.g. CYQB)"),
    arrival: str = typer.Option(..., "--to", help="Arrival ICAO (e.g. KTEB)"),
    out_date: str = typer.Option(..., "--out", help="Outbound date/time (local; needs --tz)"),
    return_date: str = typer.Option(..., "--return", help="Return date/time (local; needs --tz)"),
    timezone: Optional[str] = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Quote a round-trip in a single call (outbound + return).

    Compound version of `quote flight`: resolves airports once, fetches both legs,
    and returns combined pricing.
    """
    token = get_api_token(username, password)
    out_utc = _parse_local_dt(out_date, timezone)
    ret_utc = _parse_local_dt(return_date, timezone)
    dep_id = _resolve_airport(token, departure)
    arr_id = _resolve_airport(token, arrival)
    ac_id = _get_default_aircraft(token)

    payload = {
        "legs": [
            {
                "aircraftId": ac_id,
                "departureAirportId": dep_id,
                "arrivalAirportId": arr_id,
                "departureDateUTC": out_utc,
            },
            {
                "aircraftId": ac_id,
                "departureAirportId": arr_id,
                "arrivalAirportId": dep_id,
                "departureDateUTC": ret_utc,
            },
        ]
    }
    try:
        resp = api_post(token, "/flight-quote", payload)
        _out(resp.get("data", resp), fmt)
    except RuntimeError as e:
        _die(str(e), EXIT_ERROR)


@quote_app.command("cost")
def quote_cost(
    body: str = typer.Option(..., "--body", help='JSON body, e.g. \'{"legs":[{"aircraft":"CITATION_CJ2_PLUS","quotePrice":750}]}\''),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get miscellaneous cost estimate (catering, ground transport, surcharges).

    This calls api.airsprint.com for server-side cost breakdown.
    """
    payload = _parse_json(body)
    token = get_api_token(username, password)
    try:
        resp = api_post(token, "/trip/misc-cost-estimate", payload)
        _out(resp.get("data", resp), fmt)
    except RuntimeError as e:
        _die(str(e), EXIT_ERROR)


@quote_app.command("hours-exchange")
def quote_hours_exchange(
    hours: Optional[float] = typer.Option(None, "--hours", min=0.01),
    action: Optional[str] = typer.Option(None, "--type", help="BUY or SELL"),
    account_aircraft_id: Optional[str] = typer.Option(None, "--account-aircraft-id", help="Defaults automatically when the account has one aircraft"),
    body: Optional[str] = typer.Option(None, "--body", help='Compatibility JSON, e.g. {"hours":2,"type":"BUY"}'),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Estimate an Hours Exchange purchase or sale (GET /hour-exchange/estimate).

    The API expects query parameters, not a JSON POST body. When the account has
    one aircraft, its account-aircraft ID is selected automatically.
    """
    token = get_api_token(username, password)
    query = _hours_estimate_query(token, body, account_aircraft_id, hours, action)
    try:
        resp = api_get(token, "/hour-exchange/estimate", query)
        _out(resp.get("data", resp), fmt)
    except RuntimeError as e:
        _die(str(e), EXIT_ERROR)


@quote_app.command("airports")
def quote_airports(
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search by ICAO, IATA, or name (e.g. CYQB, Quebec)"),
    saved: bool = typer.Option(False, "--saved", help="Show saved/favourite airports only"),
    limit: int = typer.Option(20, "--limit", help="Max results"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass local mirror, hit the API"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Search airports. Returns id, ICAO, IATA, name, and location.

    Uses local mirror at ~/.airsprint_cache.json (refresh with `cache refresh`).
    `--saved` and `--no-cache` always hit the live API.
    """
    # Local mirror path: free + offline-capable for non-saved searches
    if query and not saved and not no_cache:
        cache = _load_data_cache()
        if _cache_section_fresh(cache, "airports"):
            by_icao = (cache.get("airports") or {}).get("by_icao") or {}
            q = query.strip().lower()
            results = []
            for icao, info in by_icao.items():
                hay = " ".join(str(info.get(f) or "") for f in ("iata", "name", "city", "country")).lower() + " " + icao.lower()
                if q in hay:
                    results.append({
                        "id": info["id"],
                        "icao": icao,
                        "iata": info.get("iata") or "",
                        "name": info.get("name") or "",
                        "city": info.get("city") or "",
                        "country": info.get("country") or "",
                    })
                    if len(results) >= limit:
                        break
            if results:
                _out(results, fmt, compact)
                return

    token = get_api_token(username, password)
    filt: dict[str, Any] = {}
    if query:
        filt["query"] = query
    if saved:
        filt["saved"] = True
    resp = api_post(token, "/airport", {
        "sort": [],
        "page": {"limit": limit, "offset": 0},
        "filter": filt,
    })
    items = resp.get("data", {}).get("items", [])
    results = [
        {
            "id": a["id"],
            "icao": a.get("codeICAO", ""),
            "iata": a.get("codeIATA", ""),
            "name": a.get("name", ""),
            "city": a.get("address", {}).get("city", ""),
            "country": a.get("address", {}).get("country", ""),
        }
        for a in items
    ]
    _out(results, fmt, compact)


@quote_app.command("aircraft")
def quote_aircraft(
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass local mirror, hit the API"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """List all AirSprint aircraft types with UUIDs (needed for quote --body).

    Served from local mirror when fresh; refresh with `cache refresh`.
    """
    cache = _load_data_cache()
    if not no_cache and _cache_section_fresh(cache, "aircraft"):
        by_id = (cache.get("aircraft") or {}).get("by_id") or {}
        results = [{"id": k, "name": v.get("name", "")} for k, v in by_id.items()]
        _out(results, fmt)
        return

    token = get_api_token(username, password)
    _refresh_aircraft(token, cache)
    _save_data_cache(cache)
    by_id = cache["aircraft"]["by_id"]
    results = [{"id": k, "name": v.get("name", "")} for k, v in by_id.items()]
    _out(results, fmt)


# ---------------------------------------------------------------------------
# cache (local data mirror)
# ---------------------------------------------------------------------------


@cache_app.command("refresh")
def cache_refresh(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Refresh the local mirror (accounts, airports, aircraft, my-aircraft).

    Stored at ~/.airsprint_cache.json with a 7-day TTL.
    """
    token = get_api_token(username, password)
    cache = _load_data_cache()
    _prepare_cache_for_token(cache, token)
    accounts = _refresh_accounts(token, cache)
    _refresh_airports(token, cache)
    _refresh_aircraft(token, cache)
    _refresh_my_aircraft(token, cache)
    _save_data_cache(cache)
    _out({
        "accounts": len(accounts),
        "airports": len((cache.get("airports") or {}).get("by_icao") or {}),
        "aircraft": len((cache.get("aircraft") or {}).get("by_id") or {}),
        "my_aircraft": len((cache.get("my_aircraft") or {}).get("items") or []),
        "path": str(DATA_CACHE),
    }, fmt)


@cache_app.command("status")
def cache_status(
    fmt: str = Format,
    compact: bool = Compact,
):
    """Show cache contents and freshness."""
    cache = _load_data_cache()
    if not cache:
        _out({"exists": False, "path": str(DATA_CACHE)}, fmt, compact)
        return
    out: dict[str, Any] = {
        "exists": True,
        "path": str(DATA_CACHE),
        "ttl_seconds": DATA_CACHE_TTL,
        "account_ttl_seconds": ACCOUNT_CACHE_TTL,
    }
    for key in ("accounts", "airports", "aircraft", "my_aircraft"):
        section = cache.get(key) or {}
        cached_at = section.get("_cached_at", 0)
        if not cached_at:
            out[key] = {"present": False}
            continue
        age = int(time.time() - cached_at)
        count = (
            len(section.get("items") or []) if key == "accounts"
            else len(section.get("by_icao") or {}) if key == "airports"
            else len(section.get("by_id") or {}) if key == "aircraft"
            else len(section.get("items") or [])
        )
        out[key] = {
            "present": True,
            "count": count,
            "age_seconds": age,
            "fresh": age < (ACCOUNT_CACHE_TTL if key == "accounts" else DATA_CACHE_TTL),
            "cached_at": _fmt_epoch(cached_at, fmt="%Y-%m-%d %H:%M:%S"),
        }
    _out(out, fmt, compact)


@cache_app.command("clear")
def cache_clear():
    """Delete the local data cache."""
    global _DATA_CACHE_MEMORY, _DATA_CACHE_MEMORY_MTIME_NS
    global _DATA_CACHE_MEMORY_PATH, _AIRPORT_BY_ID
    if DATA_CACHE.exists():
        DATA_CACHE.unlink()
    _DATA_CACHE_MEMORY = None
    _DATA_CACHE_MEMORY_MTIME_NS = None
    _DATA_CACHE_MEMORY_PATH = DATA_CACHE
    _AIRPORT_BY_ID = None
    _out({"message": "Cache cleared", "path": str(DATA_CACHE)})


# ---------------------------------------------------------------------------
# summary (compound dashboard — single command, multiple endpoints)
# ---------------------------------------------------------------------------


@app.command("summary")
def summary(
    timezone: Optional[str] = Timezone,
    upcoming_limit: int = typer.Option(5, "--upcoming-limit", help="Max upcoming trips to include"),
    empty_legs_limit: int = typer.Option(5, "--empty-legs-limit", help="Max empty legs to include"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Dashboard command: accounts, upcoming trips, empty legs, unread messages.

    Replaces 4+ separate calls (`user accounts`, `trips list`, `explore flights`,
    `explore counts`) with one compound query — ideal for agents that just want context.
    """
    token = get_api_token(username, password)
    now = datetime.now(tz=_tz_utc.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # accounts (also yields the IDs we need for trip filters)
    accounts = _get_accounts(token)
    account_ids = [a["id"] for a in accounts if "id" in a]

    tasks: dict[str, Callable[[], dict[str, Any]]] = {
        "empty_legs": lambda: api_post(token, "/my-flights", {
            "sort": [{"departureTimestamp": "ASC"}],
            "page": {"limit": empty_legs_limit, "offset": 0},
            "filter": {
                "departureTime": {"min": now},
                "type": ["EMPTY_LEG"],
                "locked": False,
            },
        }),
        "notifications": lambda: api_post(token, "/my-notifications", {
            "sort": [],
            "page": {"limit": 1, "offset": 0},
            "filter": {"isRead": False},
        }),
    }
    if account_ids:
        tasks["upcoming"] = lambda: api_post(token, "/my-leg", {
            "sort": [{"departureDate": "ASC"}],
            "page": {"limit": upcoming_limit, "offset": 0},
            "filter": {"departureTime": {"min": now}, "accountId": account_ids},
        })
    responses = _parallel_read_calls(tasks)

    trips_resp = responses.get("upcoming", {})
    upcoming = trips_resp.get("data", {}).get("items", []) or []
    upcoming_total = trips_resp.get("data", {}).get("total", len(upcoming))

    legs_resp = responses["empty_legs"]
    empty_legs = legs_resp.get("data", {}).get("items", []) or []
    empty_legs_total = legs_resp.get("data", {}).get("total", len(empty_legs))

    notif_resp = responses["notifications"]
    unread = notif_resp.get("data", {}).get("total", 0)

    # condensed account view — just the high-signal fields actually returned
    accounts_brief = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "ownedAircraftIds": a.get("ownedAircraftIds") or [],
            "accessLevels": a.get("accessLevels") or [],
        }
        for a in accounts
    ]

    _out({
        "accounts": accounts_brief,
        "upcomingTripsTotal": upcoming_total,
        "upcomingTrips": upcoming,
        "emptyLegsTotal": empty_legs_total,
        "emptyLegs": empty_legs,
        "unreadMessages": unread,
    }, fmt, compact)


# ---------------------------------------------------------------------------
# Helper: parse JSON body safely, fail with exit code 2
# ---------------------------------------------------------------------------


def _parse_json(s: str) -> dict[str, Any]:
    try:
        value = json.loads(s)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
        return {}  # unreachable
    if not isinstance(value, dict):
        _die("JSON body must be an object.", EXIT_VALIDATION)
    return value


def _parse_ids(value: str, option_name: str) -> list[str]:
    ids = [item.strip() for item in value.split(",") if item.strip()]
    if not ids:
        _die(f"{option_name} must contain at least one ID.", EXIT_VALIDATION)
    return ids


# ---------------------------------------------------------------------------
# raw — generic escape hatches for any endpoint
# ---------------------------------------------------------------------------


@raw_app.command("api-get")
def raw_api_get(
    path: str = typer.Option(..., "--path", help='Path on api.airsprint.com (e.g. "/my-saved-airports/")'),
    probe: bool = typer.Option(False, "--probe/--no-probe", help="Override recent-booking-write cooldown"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """GET against api.airsprint.com."""
    if path.startswith(("/trip/", "/leg/")):
        _guard_booking_probe(probe)
    token = get_api_token(username, password)
    _out(api_get(token, path), fmt, compact)


@raw_app.command("api-post")
def raw_api_post(
    path: str = typer.Option(..., "--path", help="Path on api.airsprint.com"),
    body: str = typer.Option("{}", "--body", help="JSON body (default empty)"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """POST against api.airsprint.com."""
    token = get_api_token(username, password)
    _out(api_post(token, path, _parse_json(body)), fmt, compact)


@raw_app.command("api-patch")
def raw_api_patch(
    path: str = typer.Option(..., "--path", help="Path on api.airsprint.com"),
    body: str = typer.Option("{}", "--body", help="JSON body (default empty)"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before sending PATCH"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the request without sending it"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """PATCH exactly once with no read-back. Requires --confirm or --dry-run."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": path, "payload": payload}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to send PATCH.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    result = api_patch(token, path, payload)
    _out({
        "result": result,
        "message": "PATCH sent exactly once; no read-back performed.",
    }, fmt, compact)


@raw_app.command("api-delete")
def raw_api_delete(
    path: str = typer.Option(..., "--path", help="Path on api.airsprint.com"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before sending DELETE"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the request without sending it"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """DELETE against api.airsprint.com. Requires --confirm or --dry-run."""
    if dry_run:
        _out({"dry_run": True, "method": "DELETE", "path": path}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to send DELETE.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_delete(token, path), fmt, compact)


# ---------------------------------------------------------------------------
# account — account-user management
# ---------------------------------------------------------------------------


@account_app.command("users")
def account_users(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List users on the account (POST /my-account-users)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-account-users", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", resp), fmt, compact)


@account_app.command("user-get")
def account_user_get(
    user_id: str = typer.Option(..., "--id", help="Account-user ID"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get an account-user by ID (GET /my-account-user/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/my-account-user/{user_id}"), fmt, compact)


@account_app.command("invite")
def account_invite(
    body: str = typer.Option(..., "--body", help="JSON body for invite"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Invite a user to the account (POST /account-user/invite)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/account-user/invite"}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/account-user/invite", payload), fmt, compact)


@account_app.command("user-update")
def account_user_update(
    body: str = typer.Option(..., "--body", help="JSON body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Update an account-user (POST /account-user/update)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/account-user/update"}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/account-user/update", payload), fmt, compact)


@account_app.command("user-delete")
def account_user_delete(
    user_id: str = typer.Option(..., "--id", help="Account-user UUID"),
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Remove an account user (DELETE /my-account-user/{id})."""
    path = f"/my-account-user/{user_id}"
    if dry_run:
        _out({"dry_run": True, "method": "DELETE", "path": path}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to remove an account user.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_delete(token, path), fmt, compact)


@account_app.command("roles")
def account_roles(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List account-user roles (POST /account-user-role)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/account-user-role", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", resp), fmt, compact)


# ---------------------------------------------------------------------------
# passenger — saved passengers
# ---------------------------------------------------------------------------


@passenger_app.command("list")
def passenger_list(
    limit: int = typer.Option(100, "--limit"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List saved passengers (POST /my-passenger)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-passenger", {"sort": [], "page": {"limit": limit, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@passenger_app.command("get")
def passenger_get(
    passenger_id: str = typer.Option(..., "--id"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get a saved passenger (GET /my-passenger/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/my-passenger/{passenger_id}"), fmt, compact)


@passenger_app.command("create")
def passenger_create(
    body: str = typer.Option(..., "--body", help="JSON body for new passenger"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a saved passenger (POST /my-passenger/create)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/my-passenger/create"}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/my-passenger/create", payload), fmt, compact)


@passenger_app.command("update")
def passenger_update(
    passenger_id: str = typer.Option(..., "--id", help="Passenger UUID"),
    body: str = typer.Option(..., "--body", help="JSON fields accepted by the AirSprint passenger form"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Update a saved passenger (PATCH /my-passenger/{id})."""
    path = f"/my-passenger/{passenger_id}"
    payload = _parse_json(body)
    fields = payload.get("options", payload)
    if "selectedPassportId" in fields:
        _die(
            "selectedPassportId does not persist. Use `passport make-primary` to reorder passportIds.",
            EXIT_VALIDATION,
        )
    if "options" not in payload:
        payload = {"options": payload}
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": path, "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_patch(token, path, payload), fmt, compact)


@passenger_app.command("delete")
def passenger_delete(
    passenger_id: str = typer.Option(..., "--id", help="Passenger UUID"),
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Delete a saved passenger (DELETE /my-passenger/{id})."""
    path = f"/my-passenger/{passenger_id}"
    if dry_run:
        _out({"dry_run": True, "method": "DELETE", "path": path}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to delete a saved passenger.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_delete(token, path), fmt, compact)


# ---------------------------------------------------------------------------
# passport — saved passports & docs
# ---------------------------------------------------------------------------


def _passport_epoch_ms(value: Any, field: str, timezone: str | None = None) -> int:
    """Normalize date text, epoch seconds, or epoch milliseconds to ms.

    The Android app parses ``yyyy-MM-dd`` in the device's local timezone before
    reading ``millisecondsSinceEpoch``. It does not use a fixed AirSprint HQ or
    Calgary/Edmonton timezone for passport dates. Require the equivalent IANA
    timezone for timezone-less CLI input so the same calendar date survives
    Android's local-time display path.
    """
    if isinstance(value, bool):
        _die(f'"{field}" must be an ISO date or epoch value.', EXIT_VALIDATION)
    if isinstance(value, (int, float)):
        epoch = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        try:
            epoch = float(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                _die(f'"{field}" must be ISO-8601, epoch seconds, or epoch milliseconds.', EXIT_VALIDATION)
            if parsed.tzinfo is None:
                if not timezone:
                    _die(
                        f'--timezone is required when "{field}" has no Z or UTC offset. '
                        "Set AIRSPRINT_TIMEZONE or pass --tz to match the Android device; "
                        "passport dates do not default to AirSprint HQ time.",
                        EXIT_VALIDATION,
                    )
                if not ZoneInfo:
                    _die("Cannot normalize local passport dates: zoneinfo is unavailable.", EXIT_ERROR)
                try:
                    parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
                except Exception:
                    _die(f"Unknown timezone: {timezone}", EXIT_VALIDATION)
            epoch = parsed.timestamp() * 1000
        else:
            epoch = epoch * 1000 if abs(epoch) < EPOCH_MILLISECONDS_THRESHOLD else epoch
    else:
        _die(f'"{field}" must be ISO-8601, epoch seconds, or epoch milliseconds.', EXIT_VALIDATION)
    if isinstance(value, (int, float)):
        epoch = epoch * 1000 if abs(epoch) < EPOCH_MILLISECONDS_THRESHOLD else epoch
    try:
        year = datetime.fromtimestamp(epoch / 1000, tz=_tz_utc.utc).year
    except (OverflowError, OSError, ValueError):
        _die(f'"{field}" is outside the supported date range.', EXIT_VALIDATION)
    if not 1900 <= year <= 2200:
        _die(f'"{field}" normalized to implausible year {year}; no passport was created.', EXIT_VALIDATION)
    return int(epoch)


def _normalize_passport_create(
    payload: dict[str, Any],
    timezone: str | None = None,
) -> dict[str, Any]:
    normalized = dict(payload)
    if isinstance(payload.get("options"), dict):
        normalized["options"] = dict(payload["options"])
    fields = normalized.get("options", normalized)
    for key in ("dateOfBirth", "expirationDate"):
        if key in fields:
            fields[key] = _passport_epoch_ms(fields[key], key, timezone)
    return normalized


def _entity_id(response: dict[str, Any], *keys: str) -> str | None:
    data = _response_data(response)
    candidates = [data]
    if isinstance(data, dict):
        candidates.extend(data.get(key) for key in keys)
        candidates.append(data.get("item"))
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
            return candidate["id"]
    return None


def _passenger_passport_ids(passenger: Any) -> list[str]:
    data = _response_data(passenger)
    if not isinstance(data, dict):
        return []
    ids = data.get("passportIds")
    if not isinstance(ids, list):
        options = data.get("options")
        ids = options.get("passportIds") if isinstance(options, dict) else []
    return [value for value in ids if isinstance(value, str)] if isinstance(ids, list) else []


@passport_app.command("list")
def passport_list(
    limit: int = typer.Option(100, "--limit", help="Maximum saved passengers to scan"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List exact passport records embedded in saved AirSprint passengers.

    The owner API has no working passport collection route: POST /my-passport
    returns 404. POST /my-passenger includes the saved passport objects, so use
    that single bounded read and preserve AirSprint's field names and values.
    In particular, ``nationality`` is not relabelled or inferred as ``country``.
    """
    token = get_api_token(username, password)
    resp = api_post(token, "/my-passenger", {
        "sort": [],
        "page": {"limit": limit, "offset": 0},
        "filter": {},
    })
    data = resp.get("data", resp)
    passengers = data.get("items", []) if isinstance(data, dict) else data
    passports: list[dict[str, Any]] = []
    if isinstance(passengers, list):
        for passenger in passengers:
            if not isinstance(passenger, dict):
                continue
            passenger_ref = {
                key: passenger[key]
                for key in ("id", "firstName", "lastName")
                if key in passenger
            }
            embedded = passenger.get("passports")
            if not isinstance(embedded, list):
                continue
            for passport in embedded:
                if not isinstance(passport, dict):
                    continue
                record = dict(passport)
                record["passenger"] = passenger_ref
                passports.append(record)
    _out(passports, fmt, compact)


@passport_app.command("get")
def passport_get(
    passport_id: str = typer.Option(..., "--id"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get a saved passport (GET /my-passport/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/my-passport/{passport_id}"), fmt, compact)


@passport_app.command("update-authority")
def passport_update_authority(
    passport_id: str = typer.Option(..., "--id", help="Passport UUID"),
    authority: str = typer.Option(..., "--authority", help="Exact Authority/Autorité printed in the passport"),
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Update only a passport's issuingAuthority with one PATCH.

    Android 6.1.4 sends PATCH /my-passport/{id} with an ``options`` envelope.
    Keep this command narrow: passport number and date updates are not exposed
    because those fields have not persisted reliably in the owner API.
    """
    exact_authority = authority.strip()
    if not exact_authority:
        _die("--authority cannot be empty.", EXIT_VALIDATION)
    path = f"/my-passport/{passport_id}"
    payload = {"options": {"issuingAuthority": exact_authority}}
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": path, "payload": payload}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to update a passport issuing authority.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_patch(token, path, payload), fmt, compact)


@passport_app.command("create")
def passport_create(
    body: str = typer.Option(..., "--body"),
    passenger_id: Optional[str] = typer.Option(
        None,
        "--passenger-id",
        help="Saved passenger UUID; makes the new passport first in passportIds.",
    ),
    timezone: Optional[str] = Timezone,
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a passport with date fields normalized to epoch milliseconds.

    The API accepts milliseconds on create but stores seconds. If
    --passenger-id is supplied, the passenger is read before creation and then
    patched once so the new passport becomes the first displayed passport.
    """
    payload = _normalize_passport_create(_parse_json(body), timezone)
    if dry_run:
        _out({
            "dry_run": True,
            "payload": payload,
            "endpoint": "/my-passport/create",
            "dateUnits": "milliseconds on create; API responses may store seconds",
            "dateTimezone": timezone,
            "makePrimaryForPassenger": passenger_id,
        }, fmt, compact)
        return
    token = get_api_token(username, password)
    prior_ids: list[str] = []
    if passenger_id:
        prior_ids = _passenger_passport_ids(api_get(token, f"/my-passenger/{passenger_id}"))
    created = api_post(token, "/my-passport/create", payload)
    if not passenger_id:
        _out({
            "result": created,
            "dateUnits": "milliseconds sent; API responses may store seconds",
        }, fmt, compact)
        return
    new_id = _entity_id(created, "passport")
    if not new_id:
        _out({
            "result": created,
            "warning": "Passport created but its ID was not returned; passenger passportIds were not changed.",
        }, fmt, compact)
        return
    reordered = [new_id] + [value for value in prior_ids if value != new_id]
    linked = api_patch(token, f"/my-passenger/{passenger_id}", {"options": {"passportIds": reordered}})
    _out({
        "result": created,
        "passengerUpdate": linked,
        "passportIds": reordered,
        "message": "New passport placed first; no selectedPassportId was sent and no read-back was performed.",
    }, fmt, compact)


@passport_app.command("make-primary")
def passport_make_primary(
    passenger_id: str = typer.Option(..., "--passenger-id", help="Saved passenger UUID"),
    passport_id: str = typer.Option(..., "--passport-id", help="Passport UUID to place first"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Make the app display a passport by placing it first in passportIds."""
    token = get_api_token(username, password)
    existing = _passenger_passport_ids(api_get(token, f"/my-passenger/{passenger_id}"))
    reordered = [passport_id] + [value for value in existing if value != passport_id]
    path = f"/my-passenger/{passenger_id}"
    payload = {"options": {"passportIds": reordered}}
    if dry_run:
        _out({
            "dry_run": True,
            "method": "PATCH",
            "path": path,
            "before": existing,
            "after": reordered,
            "payload": payload,
        }, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to reorder passportIds.", EXIT_VALIDATION)
    result = api_patch(token, path, payload)
    _out({
        "result": result,
        "passportIds": reordered,
        "message": "Passenger patched once; no read-back performed.",
    }, fmt, compact)


@passport_app.command("upload-init")
def passport_upload_init(
    body: str = typer.Option(..., "--body", help="JSON body — typically describes file metadata"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Begin a passport document upload — returns a presigned upload URL (POST /my-passport/document/upload-init)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/my-passport/document/upload-init", _parse_json(body)), fmt, compact)


@passport_app.command("attach")
def passport_attach(
    body: str = typer.Option(..., "--body", help="JSON body — references the uploaded file"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Attach a previously-uploaded document to a passport (POST /my-passport/document/attach)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/my-passport/document/attach", _parse_json(body)), fmt, compact)


@passport_app.command("delete")
def passport_delete(
    passport_id: str = typer.Option(..., "--id", help="Passport UUID"),
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Delete a saved passport (DELETE /my-passport/{id})."""
    path = f"/my-passport/{passport_id}"
    if dry_run:
        _out({"dry_run": True, "method": "DELETE", "path": path}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to delete a saved passport.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_delete(token, path), fmt, compact)


# ---------------------------------------------------------------------------
# pet — saved pets & docs
# ---------------------------------------------------------------------------


@pet_app.command("list")
def pet_list(
    limit: int = typer.Option(100, "--limit"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List saved pets (POST /my-pet)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-pet", {"sort": [], "page": {"limit": limit, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@pet_app.command("get")
def pet_get(
    pet_id: str = typer.Option(..., "--id"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get a saved pet (GET /my-pet/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/my-pet/{pet_id}"), fmt, compact)


@pet_app.command("create")
def pet_create(
    body: str = typer.Option(..., "--body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a saved pet (POST /my-pet/create)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/my-pet/create"}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/my-pet/create", payload), fmt, compact)


@pet_app.command("upload-init")
def pet_upload_init(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Begin a pet document upload (POST /my-pet/document/upload-init)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/my-pet/document/upload-init", _parse_json(body)), fmt, compact)


@pet_app.command("attach")
def pet_attach(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Attach an uploaded document to a pet (POST /my-pet/document/attach)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/my-pet/document/attach", _parse_json(body)), fmt, compact)


@pet_app.command("update")
def pet_update(
    pet_id: str = typer.Option(..., "--id", help="Pet UUID"),
    body: str = typer.Option(..., "--body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Update a saved pet (PATCH /my-pet/{id})."""
    path = f"/my-pet/{pet_id}"
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": path, "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_patch(token, path, payload), fmt, compact)


@pet_app.command("delete")
def pet_delete(
    pet_id: str = typer.Option(..., "--id", help="Pet UUID"),
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Delete a saved pet (DELETE /my-pet/{id})."""
    path = f"/my-pet/{pet_id}"
    if dry_run:
        _out({"dry_run": True, "method": "DELETE", "path": path}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to delete a saved pet.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_delete(token, path), fmt, compact)


# ---------------------------------------------------------------------------
# customs — Canadian customs declarations
# ---------------------------------------------------------------------------


def _trip_legs(trip: Any) -> list[dict[str, Any]]:
    data = _response_data(trip)
    if isinstance(data, dict):
        for key in ("legs", "tripLegs", "accountLegs"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        nested = data.get("trip")
        if isinstance(nested, dict):
            return _trip_legs(nested)
    return []


def _leg_departure_date(leg: dict[str, Any]) -> str | None:
    for key in ("departureDate", "departureTime", "date"):
        value = leg.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _leg_departure_country(leg: dict[str, Any]) -> str | None:
    airport = leg.get("departureAirport")
    if isinstance(airport, dict):
        address = airport.get("address")
        country = address.get("country") if isinstance(address, dict) else airport.get("country")
        if isinstance(country, str) and country:
            return country
    country, _ = _airport_country(leg.get("departureAirportId"))
    return country


def _iso_datetime(value: str, field: str = "date") -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _die(f'"{field}" must be an ISO-8601 datetime.', EXIT_VALIDATION)
    if parsed.tzinfo is None:
        _die(f'"{field}" must include Z or a UTC offset.', EXIT_VALIDATION)
    return value


def _leg_passenger_id(row: Any) -> str | None:
    """Return a legPassenger UUID for customs, intentionally using row.id."""
    if isinstance(row, dict) and isinstance(row.get("id"), str):
        return row["id"]
    return None


def _resolve_customs_passengers(leg: dict[str, Any], names: list[str]) -> list[str]:
    rows = _leg_passenger_rows(leg)
    normalized_rows = [
        (row, " ".join(_passenger_name(row).casefold().split()))
        for row in rows
    ]
    resolved: list[str] = []
    for requested in names:
        normalized = " ".join(requested.casefold().split())
        matches = [row for row, name in normalized_rows if name == normalized]
        if not matches:
            matches = [row for row, name in normalized_rows if normalized in name]
        if len(matches) != 1:
            available = ", ".join(_passenger_name(row) for row in rows) or "none"
            _die(
                f'Passenger "{requested}" matched {len(matches)} leg passengers. Available: {available}',
                EXIT_VALIDATION,
            )
        leg_passenger_id = _leg_passenger_id(matches[0])
        if not leg_passenger_id:
            _die(
                f'Passenger "{requested}" has no legPassenger ID; no declaration was sent.',
                EXIT_VALIDATION,
            )
        if leg_passenger_id not in resolved:
            resolved.append(leg_passenger_id)
    return resolved


def _validate_customs_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ids = payload.get("legPassengerIds")
    if not isinstance(ids, list) or not ids or not all(isinstance(value, str) and value for value in ids):
        _die('"legPassengerIds" must be a non-empty array of leg passenger UUIDs.', EXIT_VALIDATION)
    if payload.get("purposeOfTravel") not in {"BUSINESS", "PLEASURE"}:
        _die('"purposeOfTravel" must be BUSINESS or PLEASURE.', EXIT_VALIDATION)
    description = payload.get("travelDescription")
    if not isinstance(description, str) or not description.strip():
        _die('"travelDescription" is required.', EXIT_VALIDATION)
    date = payload.get("date")
    if not isinstance(date, str):
        _die('"date" is required and means the departure leaving Canada.', EXIT_VALIDATION)
    payload["date"] = _iso_datetime(date)
    return payload


@customs_app.command("list")
def customs_list(
    limit: int = typer.Option(100, "--limit", min=1, max=500),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List my Canadian customs declarations (POST /myCanadianCustomsDeclaration)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/myCanadianCustomsDeclaration", {
        "page": {"limit": limit, "offset": 0},
        "filter": {},
    })
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@customs_app.command("declaration")
def customs_declaration(
    body: str = typer.Option("{}", "--body", help="Optional filter body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get the customs-declaration form/template (POST /canadian-custom-declaration)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/canadian-custom-declaration", _parse_json(body)), fmt, compact)


@customs_app.command("create")
def customs_create(
    body: Optional[str] = typer.Option(None, "--body", help="Validated raw JSON alternative to the ergonomic flags"),
    booking_id: Optional[str] = typer.Option(None, "--booking", help="Booking code or trip UUID"),
    leg_id: Optional[str] = typer.Option(None, "--leg-id", help="Specific booked-leg UUID"),
    passengers: Optional[str] = typer.Option(None, "--passengers", help="Comma-separated passenger names"),
    purpose: Optional[str] = typer.Option(None, "--purpose", help="BUSINESS or PLEASURE"),
    description: Optional[str] = typer.Option(None, "--description", help="Travel description"),
    date: Optional[str] = typer.Option(None, "--date", help="ISO departure leaving Canada; defaults from outbound leg"),
    has_pet: Optional[bool] = typer.Option(None, "--has-pet/--no-pet"),
    has_alcohol_or_tobacco: Optional[bool] = typer.Option(None, "--has-alcohol-or-tobacco/--no-alcohol-or-tobacco"),
    has_imported_goods: Optional[bool] = typer.Option(None, "--has-imported-goods/--no-imported-goods"),
    imported_goods_from_us: Optional[bool] = typer.Option(None, "--imported-goods-from-us/--no-imported-goods-from-us"),
    has_high_value_currency: Optional[bool] = typer.Option(None, "--has-high-value-currency/--no-high-value-currency"),
    souvenir_items: Optional[str] = typer.Option(None, "--souvenir-items", help="JSON value accepted by the API"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    probe: bool = typer.Option(False, "--probe/--no-probe", help="Override recent-booking-write cooldown"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create Canadian declarations for named passengers on one outbound leg.

    --booking/--leg-id mode resolves legPassenger IDs and defaults date to the
    outbound departure leaving Canada. One request with several IDs creates one
    declaration per person. Certification/signature must still be done by the
    owner in the AirSprint app.
    """
    token: str | None = None
    if body is not None:
        if any(value is not None for value in (booking_id, leg_id, passengers, purpose, description, date)):
            _die("Use either --body or the booking/leg flags, not both.", EXIT_VALIDATION)
        payload = _validate_customs_payload(_parse_json(body))
    else:
        if not (booking_id or leg_id):
            _die("Provide --booking or --leg-id.", EXIT_VALIDATION)
        if not passengers or not purpose or not description:
            _die("--passengers, --purpose, and --description are required.", EXIT_VALIDATION)
        _guard_booking_probe(probe)
        token = get_api_token(username, password)
        if leg_id:
            leg = _response_data(api_get(token, f"/leg/{leg_id}"))
            if not isinstance(leg, dict):
                _die(f"Unexpected leg response for {leg_id}.", EXIT_ERROR)
        else:
            trip_uuid = _resolve_trip_uuid(token, booking_id or "")
            trip = api_get(token, f"/trip/{trip_uuid}")
            legs = _trip_legs(trip)
            if not legs:
                _die(f"No legs found for {booking_id}.", EXIT_NOT_FOUND)
            canadian_departures = [
                item for item in legs
                if (_leg_departure_country(item) or "").casefold() == "canada"
            ]
            leg = canadian_departures[0] if canadian_departures else legs[0]
        departure_date = date or _leg_departure_date(leg)
        if not departure_date:
            _die("Could not determine the outbound departure; pass --date explicitly.", EXIT_VALIDATION)
        names = [value.strip() for value in passengers.split(",") if value.strip()]
        payload = {
            "legPassengerIds": _resolve_customs_passengers(leg, names),
            "purposeOfTravel": purpose.upper(),
            "travelDescription": description,
            "date": departure_date,
        }
        optional_values = {
            "hasPet": has_pet,
            "hasAlcoholOrTobacco": has_alcohol_or_tobacco,
            "hasImportedGoods": has_imported_goods,
            "importedGoodsFromUS": imported_goods_from_us,
            "hasHighValueCurrency": has_high_value_currency,
        }
        payload.update({key: value for key, value in optional_values.items() if value is not None})
        if souvenir_items is not None:
            try:
                payload["souvenirItems"] = json.loads(souvenir_items)
            except json.JSONDecodeError as exc:
                _die(f"Invalid --souvenir-items JSON: {exc}", EXIT_VALIDATION)
        payload = _validate_customs_payload(payload)
    if dry_run:
        _out({
            "dry_run": True,
            "payload": payload,
            "endpoint": "/canadianCustomsDeclaration/create",
            "declarations": len(payload["legPassengerIds"]),
            "message": "Signature/certification still needs the AirSprint app.",
        }, fmt, compact)
        return
    token = token or get_api_token(username, password)
    result = api_post(token, "/canadianCustomsDeclaration/create", payload)
    _out({
        "result": result,
        "declarations": len(payload["legPassengerIds"]),
        "message": "Created once; signature/certification still needs the AirSprint app.",
    }, fmt, compact)


@customs_app.command("update-date")
def customs_update_date(
    declaration_id: str = typer.Option(..., "--id", help="Customs declaration UUID"),
    date: str = typer.Option(..., "--date", help="ISO departure leaving Canada"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Fix the single customs date field (departure leaving Canada)."""
    path = f"/canadianCustomsDeclaration/{declaration_id}"
    payload = {"options": {"date": _iso_datetime(date)}}
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": path, "payload": payload}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to update a customs declaration date.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    result = api_patch(token, path, payload)
    _out({
        "result": result,
        "message": "Date patched once; signature/certification still needs the app. No read-back performed.",
    }, fmt, compact)


@customs_app.command("create-public")
def customs_create_public(
    body: str = typer.Option(..., "--body"),
    fmt: str = Format, compact: bool = Compact,
):
    """Create a public (link-based) customs declaration — no auth required (POST /canadianCustomsDeclaration/create-public)."""
    payload = _parse_json(body)
    _out(_http("POST", f"{API_BASE_URL}/canadianCustomsDeclaration/create-public",
               headers={"Content-Type": "application/json", "Accept": "application/json"},
               data=json.dumps(payload).encode("utf-8")), fmt, compact)


@customs_app.command("link-create")
def customs_link_create(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a customs-declaration link to share with a passenger (POST /canadian-customs-declaration-link/create)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/canadian-customs-declaration-link/create", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# booking — additional flows
# ---------------------------------------------------------------------------


@booking_app.command("empty-leg")
def booking_empty_leg(
    body: str = typer.Option(..., "--body", help="JSON body for empty-leg booking"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Book an empty leg (POST /empty-leg/book)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/empty-leg/book"}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/empty-leg/book", payload), fmt, compact)


@booking_app.command("shared-flight")
def booking_shared_flight(
    body: str = typer.Option(..., "--body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Book a shared flight (POST /shared-flight/book)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/shared-flight/book"}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/shared-flight/book", payload), fmt, compact)


@booking_app.command("lock")
def booking_lock(
    body: str = typer.Option(..., "--body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Lock (hold) a flight (POST /flight/lock)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/flight/lock"}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/flight/lock", payload), fmt, compact)


@booking_app.command("reserve-day")
def booking_reserve_day(
    body: str = typer.Option(..., "--body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Reserve a day on the calendar (POST /reserve-day)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/reserve-day"}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/reserve-day", payload), fmt, compact)


@booking_app.command("survey")
def booking_survey(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Submit a post-booking survey (POST /booking-survey/create)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/booking-survey/create", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# trips — manifest & recent
# ---------------------------------------------------------------------------


@trips_app.command("manifest-send")
def trips_manifest_send(
    body: str = typer.Option(..., "--body", help="JSON body — recipients & trip ID"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Email the trip manifest (POST /trip/manifest/send)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": "/trip/manifest/send", "payload": payload}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to email a manifest.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_post(token, "/trip/manifest/send", payload), fmt, compact)


@trips_app.command("recent")
def trips_recent(
    limit: int = typer.Option(20, "--limit"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List recent legs (POST /leg/recent/list)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/leg/recent/list", {"sort": [], "page": {"limit": limit, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@trips_app.command("recent-save")
def trips_recent_save(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Save a leg to recents (POST /leg/recent/save)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/leg/recent/save", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# quote — airport-nearest, saved-airports
# ---------------------------------------------------------------------------


@quote_app.command("airport-nearest")
def quote_airport_nearest(
    lat: float = typer.Option(..., "--lat", help="Latitude"),
    lng: float = typer.Option(..., "--lng", help="Longitude"),
    limit: int = typer.Option(5, "--limit"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Find airports nearest a coordinate (POST /airport/nearest)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/airport/nearest", {
        "sort": [], "page": {"limit": limit, "offset": 0},
        "filter": {"latitude": lat, "longitude": lng},
    })
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@quote_app.command("saved-airports")
def quote_saved_airports(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List my saved/favourite airports (POST /my-saved-airports/)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-saved-airports/", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@quote_app.command("saved-airport-delete")
def quote_saved_airport_delete(
    airport_id: str = typer.Option(..., "--id", help="Saved airport UUID"),
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Remove a saved airport (DELETE /my-saved-airports/{id})."""
    path = f"/my-saved-airports/{airport_id}"
    if dry_run:
        _out({"dry_run": True, "method": "DELETE", "path": path}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to remove a saved airport.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_delete(token, path), fmt, compact)


# ---------------------------------------------------------------------------
# address — autocomplete & saved
# ---------------------------------------------------------------------------


@address_app.command("autocomplete")
def address_autocomplete(
    query: str = typer.Option(..., "--query", "-q", help="Partial address text"),
    limit: int = typer.Option(10, "--limit"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Address autocomplete (POST /address/autocomplete)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/address/autocomplete", {
        "sort": [], "page": {"limit": limit, "offset": 0},
        "filter": {"query": query},
    })
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@address_app.command("create")
def address_create(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Save an address (POST /my-address/create)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/my-address/create", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# hours — exchange marketplace (estimate already at quote.hours-exchange)
# ---------------------------------------------------------------------------


@hours_app.command("estimate")
def hours_estimate(
    hours: Optional[float] = typer.Option(None, "--hours", min=0.01),
    action: Optional[str] = typer.Option(None, "--type", help="BUY or SELL"),
    account_aircraft_id: Optional[str] = typer.Option(None, "--account-aircraft-id", help="Defaults automatically when the account has one aircraft"),
    body: Optional[str] = typer.Option(None, "--body", help='Compatibility JSON, e.g. {"hours":2,"type":"SELL"}'),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Estimate Hours Exchange value (GET /hour-exchange/estimate)."""
    token = get_api_token(username, password)
    query = _hours_estimate_query(token, body, account_aircraft_id, hours, action)
    resp = api_get(token, "/hour-exchange/estimate", query)
    _out(resp.get("data", resp), fmt, compact)


@hours_app.command("power")
def hours_power(
    account_aircraft_id: Optional[str] = typer.Option(None, "--account-aircraft-id", help="Defaults automatically when the account has one aircraft"),
    body: Optional[str] = typer.Option(None, "--body", help='Compatibility JSON with accountAircraftId'),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get Hours Exchange buying/selling power (GET /hour-exchange/power)."""
    token = get_api_token(username, password)
    query = _parse_json(body) if body else {}
    if account_aircraft_id:
        query["accountAircraftId"] = account_aircraft_id
    query["accountAircraftId"] = _get_account_aircraft_id(
        token, query.get("accountAircraftId")
    )
    resp = api_get(token, "/hour-exchange/power", query)
    _out(resp.get("data", resp), fmt, compact)


@hours_app.command("listing-create")
def hours_listing_create(
    body: str = typer.Option(..., "--body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List hours for sale on the marketplace (POST /hours-exchange-listing/create)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/hours-exchange-listing/create"}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/hours-exchange-listing/create", payload), fmt, compact)


@hours_app.command("my-listings")
def hours_my_listings(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List my hours-exchange listings (POST /my-hours-exchange-listing)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-hours-exchange-listing", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


@files_app.command("list")
def files_list(
    limit: int = typer.Option(50, "--limit"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List my files (POST /my-file)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-file", {"sort": [], "page": {"limit": limit, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@files_app.command("public-create")
def files_public_create(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a public-file record (POST /file-public/create)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/file-public/create", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# content — FAQ, policy, system notice, concierge
# ---------------------------------------------------------------------------


@content_app.command("faq")
def content_faq(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List FAQ entries (POST /faq)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/faq", {"sort": [], "page": {"limit": 200, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@content_app.command("faq-categories")
def content_faq_categories(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List FAQ categories (POST /faq-category)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/faq-category", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@content_app.command("policies")
def content_policies(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List policies (POST /policy)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/policy", {"sort": [], "page": {"limit": 200, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@content_app.command("policy-categories")
def content_policy_categories(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List policy categories (POST /policy-category)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/policy-category", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@content_app.command("system-notice")
def content_system_notice(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get current system notice (POST /system-notice)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/system-notice", {}), fmt, compact)


@content_app.command("required-info")
def content_required_info(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get required-info prompts (POST /required-info)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/required-info", {}), fmt, compact)


@content_app.command("concierge")
def content_concierge(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get concierge contact info (POST /concierge)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/concierge", {}), fmt, compact)


# ---------------------------------------------------------------------------
# network — My Network connections and flight-sharing groups
# ---------------------------------------------------------------------------


@network_app.command("connections")
def network_connections(
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=500),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List My Network connections (POST /my-user/connections)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-user/connections", {
        "sort": [], "page": {"limit": limit, "offset": 0}, "filter": {},
    })
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@network_app.command("groups")
def network_groups(
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=500),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List My Network groups (POST /my-user/groups)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-user/groups", {
        "sort": [], "page": {"limit": limit, "offset": 0}, "filter": {},
    })
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@network_app.command("group-get")
def network_group_get(
    group_id: str = typer.Option(..., "--id", help="Group UUID"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get a My Network group (GET /my-user/groups/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/my-user/groups/{group_id}"), fmt, compact)


@network_app.command("connect")
def network_connect(
    token_value: str = typer.Option(..., "--token", help="One-time connection token from an AirSprint link or QR code"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Add a connection using a one-time token (POST /user/connections/request)."""
    payload = {"token": token_value}
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": "/user/connections/request", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/user/connections/request", payload), fmt, compact)


@network_app.command("claim")
def network_claim(
    payload_value: str = typer.Option(..., "--payload", help="Connection invite payload"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Claim a connection invitation payload (POST /user/connections/invite/claim)."""
    payload = {"payload": payload_value}
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": "/user/connections/invite/claim", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/user/connections/invite/claim", payload), fmt, compact)


@network_app.command("connection-remove")
def network_connection_remove(
    connection_id: str = typer.Option(..., "--id", help="Connection/user UUID"),
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Remove a connection (DELETE /my-user/connections/{id})."""
    path = f"/my-user/connections/{connection_id}"
    if dry_run:
        _out({"dry_run": True, "method": "DELETE", "path": path}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to remove a connection.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_delete(token, path), fmt, compact)


@network_app.command("group-create")
def network_group_create(
    name: str = typer.Option(..., "--name"),
    members: Optional[str] = typer.Option(None, "--members", help="Comma-separated connection/user UUIDs"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a My Network group (POST /my-user/groups/create)."""
    payload: dict[str, Any] = {"name": name}
    if members:
        payload["memberIds"] = _parse_ids(members, "--members")
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": "/my-user/groups/create", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/my-user/groups/create", payload), fmt, compact)


@network_app.command("group-rename")
def network_group_rename(
    group_id: str = typer.Option(..., "--id", help="Group UUID"),
    name: str = typer.Option(..., "--name"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Rename a My Network group (PATCH /my-user/groups/{id})."""
    path = f"/my-user/groups/{group_id}"
    payload = {"options": {"name": name}}
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": path, "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_patch(token, path, payload), fmt, compact)


@network_app.command("group-members-add")
def network_group_members_add(
    group_id: str = typer.Option(..., "--id", help="Group UUID"),
    members: str = typer.Option(..., "--members", help="Comma-separated connection/user UUIDs"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Add members to a group (POST /my-user/groups/{id}/members)."""
    path = f"/my-user/groups/{group_id}/members"
    payload = {"memberIds": _parse_ids(members, "--members")}
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": path, "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, path, payload), fmt, compact)


@network_app.command("group-member-remove")
def network_group_member_remove(
    group_id: str = typer.Option(..., "--id", help="Group UUID"),
    member_id: str = typer.Option(..., "--member", help="Connection/user UUID"),
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Remove a member from a group (DELETE /my-user/groups/{id}/members/{member})."""
    path = f"/my-user/groups/{group_id}/members/{member_id}"
    if dry_run:
        _out({"dry_run": True, "method": "DELETE", "path": path}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to remove a group member.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_delete(token, path), fmt, compact)


@network_app.command("group-delete")
def network_group_delete(
    group_id: str = typer.Option(..., "--id", help="Group UUID"),
    confirm: bool = typer.Option(False, "--confirm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Delete a My Network group (DELETE /my-user/groups/{id})."""
    path = f"/my-user/groups/{group_id}"
    if dry_run:
        _out({"dry_run": True, "method": "DELETE", "path": path}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to delete a group.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_delete(token, path), fmt, compact)


# ---------------------------------------------------------------------------
# user — me / change-password / avatar (additional)
# ---------------------------------------------------------------------------


@user_app.command("me")
def user_me(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get my full user record (POST /my-user). Richer than `user profile`."""
    token = get_api_token(username, password)
    _out(api_post(token, "/my-user", {}), fmt, compact)


@user_app.command("change-password")
def user_change_password(
    body: str = typer.Option(..., "--body", help='JSON body, e.g. {"currentPassword":"...", "newPassword":"..."}'),
    confirm: bool = typer.Option(False, "--confirm", help="Required — change-password is destructive"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Change your password (POST /my-user/change-password). Requires --confirm."""
    if not confirm:
        _die("--confirm required to actually change the password.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_post(token, "/my-user/change-password", _parse_json(body)), fmt, compact)


@user_app.command("avatar")
def user_avatar(
    user_id: str = typer.Option(..., "--id", help="User ID"),
    output: str = typer.Option("-", "--output", "-o", help="Output file path or - for metadata only"),
    username: Optional[str] = Username, password: Optional[str] = Password,
):
    """Download a user avatar (GET /my-user/avatar/{id})."""
    token = get_api_token(username, password)
    url = f"{API_BASE_URL}/my-user/avatar/{user_id}"
    req = Request(url, method="GET", headers={
        "x-airsprint-auth-token": token, "Accept": "*/*",
    })
    try:
        with urlopen(req, timeout=60, context=_ssl_ctx()) as resp:
            content = resp.read()
            if output == "-":
                _out({"size_bytes": len(content), "content_type": resp.headers.get("Content-Type", "")})
            else:
                Path(output).write_bytes(content)
                _out({"message": f"Saved to {output}", "size_bytes": len(content)})
    except HTTPError as e:
        _die(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", EXIT_ERROR)


# ---------------------------------------------------------------------------
# messages — notification settings
# ---------------------------------------------------------------------------


@messages_app.command("settings")
def messages_settings(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get notification settings (POST /my-notification-settings)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/my-notification-settings", {}), fmt, compact)


@messages_app.command("settings-update")
def messages_settings_update(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Update notification settings (POST /my-notification-settings/update)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/my-notification-settings/update", _parse_json(body)), fmt, compact)


@messages_app.command("update")
def messages_update(
    body: str = typer.Option(..., "--body", help='JSON body — e.g. {"ids":["id1","id2"],"isRead":true}'),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Bulk-update notifications (PATCH /my-notifications/update)."""
    token = get_api_token(username, password)
    _out(api_patch(token, "/my-notifications/update", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# auth — 2FA & password reset
# ---------------------------------------------------------------------------


@auth_app.command("2fa-setup")
def auth_2fa_setup(
    body: str = typer.Option("{}", "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Begin 2FA setup (POST /user/2fa/setup)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/user/2fa/setup", _parse_json(body)), fmt, compact)


@auth_app.command("2fa-verify")
def auth_2fa_verify(
    body: str = typer.Option(..., "--body", help='JSON body, e.g. {"code":"123456"}'),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Verify a 2FA code during setup (POST /user/2fa/verify)."""
    token = get_api_token(username, password)
    _out(api_post(token, "/user/2fa/verify", _parse_json(body)), fmt, compact)


@auth_app.command("2fa-sign-in")
def auth_2fa_sign_in(
    body: str = typer.Option(..., "--body"),
    fmt: str = Format, compact: bool = Compact,
):
    """Complete a 2FA sign-in (POST /user/2fa/sign-in) — no auth header required."""
    _out(_http("POST", f"{API_BASE_URL}/user/2fa/sign-in",
               headers={"Content-Type": "application/json", "Accept": "application/json"},
               data=json.dumps(_parse_json(body)).encode("utf-8")), fmt, compact)


@auth_app.command("2fa-disable")
def auth_2fa_disable(
    body: str = typer.Option("{}", "--body"),
    confirm: bool = typer.Option(False, "--confirm", help="Required — disabling 2FA reduces account security"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Disable 2FA (POST /user/2fa/disable). Requires --confirm."""
    if not confirm:
        _die("--confirm required to disable 2FA.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_post(token, "/user/2fa/disable", _parse_json(body)), fmt, compact)


@auth_app.command("reset-request")
def auth_reset_request(
    email: str = typer.Option(..., "--email"),
    fmt: str = Format, compact: bool = Compact,
):
    """Request a password-reset email (POST /user/request-reset-password). No auth required."""
    _out(_http("POST", f"{API_BASE_URL}/user/request-reset-password",
               headers={"Content-Type": "application/json", "Accept": "application/json"},
               data=json.dumps({"email": email}).encode("utf-8")), fmt, compact)


@auth_app.command("reset-confirm")
def auth_reset_confirm(
    body: str = typer.Option(..., "--body", help='JSON body, e.g. {"token":"...", "newPassword":"..."}'),
    fmt: str = Format, compact: bool = Compact,
):
    """Confirm a password reset (POST /user/reset-password). No auth required."""
    _out(_http("POST", f"{API_BASE_URL}/user/reset-password",
               headers={"Content-Type": "application/json", "Accept": "application/json"},
               data=json.dumps(_parse_json(body)).encode("utf-8")), fmt, compact)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        app()
    except RuntimeError as exc:
        # Surface our structured-error JSON cleanly instead of a Python traceback.
        msg = str(exc)
        try:
            parsed = json.loads(msg)
        except json.JSONDecodeError:
            parsed = {"status": "error", "message": msg}
        sys.stderr.write(json.dumps(parsed) + "\n")
        sys.exit(EXIT_ERROR)
