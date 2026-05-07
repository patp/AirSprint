#!/usr/bin/env python3
"""AirSprint CLI — agent-friendly interface to AirSprint's owner APIs.

The mobile-app API (prod2.airsprint.com) was decommissioned by AirSprint in
April 2026. The CLI now authenticates against api.airsprint.com (the same
backend the owner web portal uses) via /user/sign-in-email and stores the
returned JWT for downstream calls.
Output: JSON by default (--format human for readable output).
Credentials: AIRSPRINT_USERNAME / AIRSPRINT_PASSWORD env vars, or --username/--password flags.
Token cache: ~/.airsprint_token.json (avoids re-login per invocation).

Exit codes:
  0 = success
  1 = general error
  2 = validation / input error
  3 = not found
  4 = auth failure
"""

import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone as _tz_utc
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

try:
    import truststore
    truststore.inject_into_ssl()
    _truststore_active = True
except ImportError:
    _truststore_active = False

import typer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://prod2.airsprint.com"
LEGACY_BASE_URL = "https://api.airsprint.com/api"
BASIC_AUTH = "Basic VVNFUl9DTElFTlRfQVBQOnBhc3N3b3Jk"
TOKEN_CACHE = Path.home() / ".airsprint_token.json"
LEGACY_TOKEN_CACHE = Path.home() / ".airsprint_legacy_token.json"
DATA_CACHE = Path.home() / ".airsprint_cache.json"  # local mirror: airports, aircraft
DATA_CACHE_TTL = 7 * 24 * 3600  # 7 days
DEFAULT_TZ = ""  # No default — user must set AIRSPRINT_TIMEZONE or pass --timezone

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
    return ssl.create_default_context()


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
) -> dict[str, Any]:
    """Low-level HTTP request. Returns parsed JSON or raises."""
    req = Request(url, data=data, method=method, headers=dict(headers or {}))
    try:
        with urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            json.dumps({"status": "error", "http_code": exc.code, "message": body})
        ) from exc
    except URLError as exc:
        msg = str(exc)
        if "prod2.airsprint.com" in url and "nodename nor servname" in msg:
            msg = (
                "prod2.airsprint.com was decommissioned by AirSprint in April 2026. "
                "This command targets a route that has not yet been migrated to "
                "api.airsprint.com. Use the new typed commands or `raw legacy-*`."
            )
        raise RuntimeError(
            json.dumps({"status": "error", "message": msg})
        ) from exc


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


def _save_token(data: dict[str, Any]) -> None:
    data["_cached_at"] = int(time.time())
    TOKEN_CACHE.write_text(json.dumps(data, indent=2))
    TOKEN_CACHE.chmod(0o600)


def _load_token() -> dict[str, Any] | None:
    if not TOKEN_CACHE.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    cached_at = data.get("_cached_at", 0)
    expires_in = data.get("expires_in", 3600)
    if time.time() - cached_at > expires_in - 120:
        return None  # expired or about to
    return data


def _clear_token() -> None:
    if TOKEN_CACHE.exists():
        TOKEN_CACHE.unlink()


def _do_login(username: str, password: str) -> dict[str, Any]:
    """Login to api.airsprint.com → token dict (prod2-OAuth-compatible shape).

    prod2.airsprint.com was retired in April 2026; this calls the
    sign-in-email endpoint that the owner web portal uses, then reshapes
    the response to look like the old OAuth2 response so the rest of the
    CLI's token-cache code keeps working unchanged.
    """
    resp = _http(
        "POST",
        f"{LEGACY_BASE_URL}/user/sign-in-email",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=json.dumps({"email": username, "password": password}).encode("utf-8"),
    )
    token = resp.get("data", {}).get("authToken")
    if not token:
        raise RuntimeError(
            json.dumps({"status": "error", "message": "No authToken in sign-in-email response", "response": resp})
        )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 7 * 24 * 3600,
        "mfa": resp.get("data", {}).get("mfa", False),
    }


def get_token(username: str | None = None, password: str | None = None) -> str:
    """Return a valid access_token, using cache when possible."""
    cached = _load_token()
    if cached:
        return cached["access_token"]

    u = username or os.environ.get("AIRSPRINT_USERNAME", "")
    p = password or os.environ.get("AIRSPRINT_PASSWORD", "")
    if not u or not p:
        _die("Credentials required. Set AIRSPRINT_USERNAME/AIRSPRINT_PASSWORD or use --username/--password.", EXIT_AUTH)

    data = _do_login(u, p)
    _save_token(data)
    return data["access_token"]


# ---------------------------------------------------------------------------
# Authenticated requests
# ---------------------------------------------------------------------------


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _bearer_json(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }


def api_get(token: str, path: str) -> dict[str, Any]:
    return _http("GET", f"{BASE_URL}{path}", headers=_bearer(token))


def api_post(token: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body or {}).encode("utf-8") if body else None
    hdrs = _bearer_json(token) if data else _bearer(token)
    return _http("POST", f"{BASE_URL}{path}", headers=hdrs, data=data)


def api_put(token: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    return _http(
        "PUT",
        f"{BASE_URL}{path}",
        headers=_bearer_json(token),
        data=json.dumps(body).encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Legacy API (api.airsprint.com) — used for quotes & estimates
# ---------------------------------------------------------------------------


def _legacy_login(username: str, password: str) -> str:
    """Login to the legacy API → authToken."""
    resp = _http(
        "POST",
        f"{LEGACY_BASE_URL}/user/sign-in-email",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=json.dumps({"email": username, "password": password}).encode("utf-8"),
    )
    token = resp.get("data", {}).get("authToken")
    if not token:
        raise RuntimeError(
            json.dumps({"status": "error", "message": "No authToken in legacy login response"})
        )
    return token


def _save_legacy_token(token: str, email: str) -> None:
    data = {"authToken": token, "email": email, "_cached_at": int(time.time())}
    LEGACY_TOKEN_CACHE.write_text(json.dumps(data, indent=2))
    LEGACY_TOKEN_CACHE.chmod(0o600)


def _load_legacy_token() -> str | None:
    if not LEGACY_TOKEN_CACHE.exists():
        return None
    try:
        data = json.loads(LEGACY_TOKEN_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    # Legacy tokens don't have expires_in — use 6 hour TTL
    if time.time() - data.get("_cached_at", 0) > 21600:
        return None
    return data.get("authToken")


def get_legacy_token(username: str | None = None, password: str | None = None) -> str:
    """Return a valid legacy authToken, using cache when possible."""
    cached = _load_legacy_token()
    if cached:
        return cached

    u = username or os.environ.get("AIRSPRINT_USERNAME", "")
    p = password or os.environ.get("AIRSPRINT_PASSWORD", "")
    if not u or not p:
        _die("Credentials required. Set AIRSPRINT_USERNAME/AIRSPRINT_PASSWORD or use --username/--password.", EXIT_AUTH)

    token = _legacy_login(u, p)
    _save_legacy_token(token, u)
    return token


def legacy_get(token: str, path: str) -> dict[str, Any]:
    return _http(
        "GET",
        f"{LEGACY_BASE_URL}{path}",
        headers={
            "x-airsprint-auth-token": token,
            "Accept": "application/json",
        },
    )


def legacy_post(token: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return _http(
        "POST",
        f"{LEGACY_BASE_URL}{path}",
        headers={
            "x-airsprint-auth-token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(body or {}).encode("utf-8"),
    )


def _get_legacy_account_ids(token: str) -> list[str]:
    """Get account IDs from the legacy API."""
    resp = legacy_post(token, "/my-accounts", {})
    items = resp.get("data", {}).get("items", [])
    return [item["id"] for item in items if "id" in item]


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
        ts = float(epoch_ms) / 1000 if float(epoch_ms) > 1e12 else float(epoch_ms)
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
    help="AirSprint CLI — agent-friendly interface to prod2.airsprint.com",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

auth_app = typer.Typer(help="Authentication commands", no_args_is_help=True)
user_app = typer.Typer(help="User & account commands", no_args_is_help=True)
trips_app = typer.Typer(help="Trip & flight commands", no_args_is_help=True)
booking_app = typer.Typer(help="Booking commands (create, update, cancel)", no_args_is_help=True)
explore_app = typer.Typer(help="Explore empty legs & shared flights", no_args_is_help=True)
messages_app = typer.Typer(help="In-app message commands", no_args_is_help=True)
feedback_app = typer.Typer(help="Feedback commands", no_args_is_help=True)
quote_app = typer.Typer(help="Quotes & cost estimates (via legacy api.airsprint.com)", no_args_is_help=True)
cache_app = typer.Typer(help="Local data mirror (airports, aircraft) at ~/.airsprint_cache.json", no_args_is_help=True)
raw_app = typer.Typer(help="Raw API escape hatches (legacy + prod2). Use when no typed command exists.", no_args_is_help=True)
account_app = typer.Typer(help="Account-user management (invite, update, roles)", no_args_is_help=True)
passenger_app = typer.Typer(help="Saved passengers", no_args_is_help=True)
passport_app = typer.Typer(help="Saved passports & passport documents", no_args_is_help=True)
pet_app = typer.Typer(help="Saved pets & pet documents", no_args_is_help=True)
customs_app = typer.Typer(help="Canadian customs declarations", no_args_is_help=True)
address_app = typer.Typer(help="Address autocomplete & saved addresses", no_args_is_help=True)
hours_app = typer.Typer(help="Hours-exchange marketplace (estimate, power, listings)", no_args_is_help=True)
files_app = typer.Typer(help="File uploads & retrieval", no_args_is_help=True)
content_app = typer.Typer(help="Content: FAQ, policies, system notices, concierge", no_args_is_help=True)
social_app = typer.Typer(help="Follow / follower social graph", no_args_is_help=True)

app.add_typer(auth_app, name="auth")
app.add_typer(user_app, name="user")
app.add_typer(trips_app, name="trips")
app.add_typer(booking_app, name="booking")
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
app.add_typer(social_app, name="social")

# Common options
Username = typer.Option(None, "--username", "-u", envvar="AIRSPRINT_USERNAME", help="Login email")
Password = typer.Option(None, "--password", "-p", envvar="AIRSPRINT_PASSWORD", help="Login password")
Format = typer.Option("json", "--format", "-f", help="Output format: json | human")
Compact = typer.Option(False, "--compact", envvar="AIRSPRINT_COMPACT", help="Strip null/empty/noisy fields and use minimal JSON. Token-efficient for agents.")
Timezone = typer.Option(None, "--timezone", "--tz", envvar="AIRSPRINT_TIMEZONE", help="Timezone (e.g. America/Montreal). Required for local time. Env: AIRSPRINT_TIMEZONE")


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
        data = _do_login(u, p)
        _save_token(data)
        safe = {k: v for k, v in data.items() if k != "access_token"}
        safe["access_token"] = data["access_token"][:8] + "..."
        _out(safe, fmt)
    except RuntimeError as e:
        _die(str(e), EXIT_AUTH)


@auth_app.command("logout")
def auth_logout():
    """Clear cached token."""
    _clear_token()
    _out({"message": "Token cleared"})


@auth_app.command("status")
def auth_status(fmt: str = Format):
    """Check if cached token is valid."""
    cached = _load_token()
    if cached:
        _out({
            "authenticated": True,
            "email": cached.get("email", "?"),
            "expires_in": cached.get("expires_in"),
            "cached_at": _fmt_epoch(cached.get("_cached_at"), fmt="%Y-%m-%d %H:%M:%S"),
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
    token = get_legacy_token(username, password)
    resp = legacy_get(token, "/me")
    _out(resp.get("data", resp), fmt)


@user_app.command("accounts")
def user_accounts(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get account info (shares, aircraft, access levels, hours)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-accounts", {})
    items = resp.get("data", {}).get("items", [])
    _out(items, fmt)


@user_app.command("preferences")
def user_preferences(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get notification settings (GET /my-notification-settings)."""
    token = get_legacy_token(username, password)
    data = legacy_get(token, "/my-notification-settings")
    _out(data, fmt)


@user_app.command("set-preferences")
def user_set_preferences(
    body: str = typer.Option(..., "--body", help="JSON body with notification-setting fields"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Update notification settings (POST /my-notification-settings)."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
    token = get_legacy_token(username, password)
    data = legacy_post(token, "/my-notification-settings", payload)
    _out(data, fmt)


@user_app.command("update")
def user_update(
    body: str = typer.Option(..., "--body", help="JSON body with account fields to update"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Update account info."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
    token = get_token(username, password)
    data = api_post(token, "/user/updateAccountInfo", payload)
    _out(data, fmt)


# ---------------------------------------------------------------------------
# trips
# ---------------------------------------------------------------------------


@trips_app.command("list")
def trips_list(
    upcoming: bool = typer.Option(True, "--upcoming/--past", help="Show upcoming (default) or past trips"),
    limit: int = typer.Option(25, "--limit", "-n", help="Max trips to return"),
    timezone: Optional[str] = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """List trips (including interchange flights).

    Uses the legacy API which returns all trip types including interchange.
    """
    token = get_legacy_token(username, password)
    account_ids = _get_legacy_account_ids(token)
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
    resp = legacy_post(token, "/my-leg", payload)
    items = resp.get("data", {}).get("items", [])
    _out(items, fmt, compact)


@trips_app.command("get")
def trips_get(
    booking_id: str = typer.Option(..., "--id", help="Booking ID (e.g. IYIBL)"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get a specific trip by trip-UUID or booking code (e.g. BAKEW)."""
    token = get_legacy_token(username, password)
    trip_uuid = booking_id
    # If it doesn't look like a UUID, treat it as a booking code and resolve via /my-leg.
    if "-" not in booking_id:
        account_ids = _get_legacy_account_ids(token)
        resp = legacy_post(token, "/my-leg", {
            "sort": [{"departureDate": "ASC"}],
            "page": {"limit": 200, "offset": 0},
            "filter": {"accountId": account_ids},
        })
        items = resp.get("data", {}).get("items", [])
        match = next((i for i in items if i.get("bookingId") == booking_id), None)
        if not match or not match.get("tripId"):
            _die(f"Trip {booking_id} not found", EXIT_NOT_FOUND)
        trip_uuid = match["tripId"]
    try:
        data = legacy_get(token, f"/trip/{trip_uuid}")
    except RuntimeError as exc:
        msg = str(exc)
        if "404" in msg or "not found" in msg.lower():
            _die(f"Trip {booking_id} not found", EXIT_NOT_FOUND)
        raise
    _out(data.get("data", data), fmt)


@trips_app.command("tripsheet")
def trips_tripsheet(
    booking_id: str = typer.Option(..., "--id", help="Trip UUID or booking code (e.g. BAKEW)"),
    output: str = typer.Option("-", "--output", "-o", help="Output file path (- for stdout info)"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
):
    """Download trip sheet (manifest) PDF (GET /trip/manifest/{id}).

    The endpoint returns a JSON envelope with a presigned S3 URL; this command
    follows the URL and saves the PDF (or reports the URL with --output -).
    """
    token = get_legacy_token(username, password)
    trip_uuid = booking_id
    if "-" not in booking_id:
        account_ids = _get_legacy_account_ids(token)
        resp = legacy_post(token, "/my-leg", {
            "sort": [{"departureDate": "ASC"}],
            "page": {"limit": 200, "offset": 0},
            "filter": {"accountId": account_ids},
        })
        items = resp.get("data", {}).get("items", [])
        match = next((i for i in items if i.get("bookingId") == booking_id), None)
        if not match or not match.get("tripId"):
            _die(f"Trip {booking_id} not found", EXIT_NOT_FOUND)
        trip_uuid = match["tripId"]
    try:
        envelope = legacy_get(token, f"/trip/manifest/{trip_uuid}")
    except RuntimeError as exc:
        msg = str(exc)
        if "404" in msg or "not found" in msg.lower() or "Flight not found" in msg:
            _die(f"No manifest available for {booking_id} (flight may not have departed yet)", EXIT_NOT_FOUND)
        raise
    pdf_url = envelope.get("data", {}).get("data", {}).get("url") or envelope.get("data", {}).get("url")
    if not pdf_url:
        _die(f"No manifest URL returned for {booking_id}", EXIT_NOT_FOUND)
    if output == "-":
        _out({"url": pdf_url, "message": "Use --output FILE to download the PDF."})
        return
    req = Request(pdf_url, method="GET")
    try:
        with urlopen(req, timeout=60, context=_ssl_ctx()) as resp:
            content = resp.read()
            Path(output).write_bytes(content)
            _out({"message": f"Saved to {output}", "size_bytes": len(content)})
    except HTTPError as e:
        _die(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", EXIT_ERROR)


_FEATURE_REMOVED_MSG = (
    "This endpoint is not available on the current api.airsprint.com — "
    "the prod2 mobile API was decommissioned and AirSprint did not port this "
    "feature to the new web API. Check the owners.airsprint.com portal."
)


@trips_app.command("invoice")
def trips_invoice(
    trip_id: str = typer.Option(..., "--id", help="Trip ID"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """[REMOVED] Per-trip invoice not available on current API."""
    _die(f"trips invoice: {_FEATURE_REMOVED_MSG}", EXIT_ERROR)


@trips_app.command("invoices")
def trips_invoices(
    timezone: Optional[str] = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """[REMOVED] Invoices listing not available on current API."""
    _die(f"trips invoices: {_FEATURE_REMOVED_MSG}", EXIT_ERROR)


@trips_app.command("preflight")
def trips_preflight(
    timezone: Optional[str] = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """[REMOVED] Preflight info not available on current API."""
    _die(f"trips preflight: {_FEATURE_REMOVED_MSG}", EXIT_ERROR)


@trips_app.command("flight-feedback")
def trips_flight_feedback(
    trip_id: str = typer.Option(..., "--id", help="Trip ID"),
    body: str = typer.Option(..., "--body", help="JSON feedback body"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Submit flight feedback for a completed trip."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
    token = get_legacy_token(username, password)
    payload.setdefault("tripId", trip_id)
    data = legacy_post(token, "/booking-survey/create", payload)
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
    """Compose booking prep data: accounts, aircraft, passengers, saved airports.

    Run this BEFORE creating a booking to get valid reference values
    (accountId, aircraftId, departureAirportId, passenger ids, etc.).
    """
    token = get_legacy_token(username, password)
    accounts = legacy_post(token, "/my-accounts", {}).get("data", {}).get("items", [])
    aircraft = legacy_post(token, "/my-aircraft", {}).get("data", {}).get("items", [])
    passengers = legacy_post(token, "/my-passenger", {"sort": [], "page": {"limit": 200, "offset": 0}, "filter": {}}).get("data", {}).get("items", [])
    airports = legacy_post(token, "/airport", {"sort": [], "page": {"limit": 50, "offset": 0}, "filter": {"saved": True}}).get("data", {}).get("items", [])
    _out({
        "accounts": accounts,
        "aircraft": aircraft,
        "passengers": passengers,
        "savedAirports": airports,
    }, fmt)


@booking_app.command("create")
def booking_create(
    body: str = typer.Option(..., "--body", help='JSON body for POST /trip/book'),
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
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)

    for key in ("legs", "baggage", "shareSettings"):
        if key not in payload:
            _die(f'Body must contain "{key}" (see --help for full schema).', EXIT_VALIDATION)

    if dry_run:
        _out({"dry_run": True, "payload": payload, "message": "Would POST /trip/book"}, fmt)
        return

    token = get_legacy_token(username, password)
    data = legacy_post(token, "/trip/book", payload)
    _out(data, fmt)


@booking_app.command("update")
def booking_update(
    body: str = typer.Option(..., "--body", help="(unused — endpoint not available)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """[REMOVED] Trip-update endpoint not available on current API.

    The new api.airsprint.com has no equivalent of the old PUT /user/updateTrip.
    The owners.airsprint.com web portal also does not expose a working
    "Modify Flight" action — modifications now go through the concierge.
    """
    _die("booking update: " + _FEATURE_REMOVED_MSG, EXIT_ERROR)


@booking_app.command("cancel")
def booking_cancel(
    booking_id: Optional[str] = typer.Option(None, "--id", help="Booking code (e.g. BAKEW) — resolved to tripId"),
    trip_id: Optional[str] = typer.Option(None, "--trip-id", help="Trip UUID (alternative to --id)"),
    leg_id: Optional[str] = typer.Option(None, "--leg-id", help="Cancel a single leg by leg UUID"),
    leg_ids: Optional[str] = typer.Option(None, "--leg-ids", help="Comma-separated leg UUIDs"),
    reason: str = typer.Option(..., "--reason", help="Cancellation reason (required by API)"),
    authorizer: Optional[str] = typer.Option(None, "--authorizer", help="(deprecated, ignored — kept for compat)"),
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
        token_for_lookup = get_legacy_token(username, password)
        account_ids = _get_legacy_account_ids(token_for_lookup)
        resp = legacy_post(token_for_lookup, "/my-leg", {
            "sort": [{"departureDate": "ASC"}],
            "page": {"limit": 200, "offset": 0},
            "filter": {"accountId": account_ids},
        })
        items = resp.get("data", {}).get("items", [])
        match = next((i for i in items if i.get("bookingId") == booking_id), None)
        if not match or not match.get("tripId"):
            _die(f"Booking {booking_id} not found", EXIT_NOT_FOUND)
        payload["tripId"] = match["tripId"]

    if dry_run:
        _out({"dry_run": True, "payload": payload, "message": "Would POST /cancel-own"}, fmt)
        return

    token = get_legacy_token(username, password)
    data = legacy_post(token, "/cancel-own", payload)
    _out(data, fmt)


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
    token = get_legacy_token(username, password)
    now = datetime.now(tz=_tz_utc.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    resp = legacy_post(token, "/my-flights", {
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
    """Get dashboard counts (unread messages, upcoming trips)."""
    token = get_legacy_token(username, password)
    account_ids = _get_legacy_account_ids(token)

    # Unread notifications count
    notif_resp = legacy_post(token, "/my-notifications", {
        "sort": [], "page": {"limit": 1, "offset": 0},
        "filter": {"isRead": False},
    })
    unread = notif_resp.get("data", {}).get("total", 0)

    # Upcoming trips count
    now = datetime.now(tz=_tz_utc.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    trips_resp = legacy_post(token, "/my-leg", {
        "sort": [], "page": {"limit": 1, "offset": 0},
        "filter": {"departureTime": {"min": now}, "accountId": account_ids},
    })
    upcoming = trips_resp.get("data", {}).get("total", 0)

    # Empty legs count
    flights_resp = legacy_post(token, "/my-flights", {
        "sort": [], "page": {"limit": 1, "offset": 0},
        "filter": {"departureTime": {"min": now}, "type": ["EMPTY_LEG"], "locked": False},
    })
    empty_legs = flights_resp.get("data", {}).get("total", 0)

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
    token = get_legacy_token(username, password)
    filt: dict[str, Any] = {}
    if unread is True:
        filt["isRead"] = False
    resp = legacy_post(token, "/my-notifications", {
        "sort": [],
        "page": {"limit": limit, "offset": 0},
        "filter": filt,
    })
    items = resp.get("data", {}).get("items", [])
    _out(items, fmt)


@messages_app.command("read")
def messages_read(
    message_id: str = typer.Option(..., "--id", help="Message ID to mark as read"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Mark a single message as read."""
    token = get_token(username, password)
    data = api_post(token, "/user/readUserMessage", {"messageId": message_id})
    _out(data, fmt)


@messages_app.command("read-all")
def messages_read_all(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Mark all messages as read."""
    token = get_token(username, password)
    data = api_post(token, "/user/readAllUserMessages")
    _out(data, fmt)


@messages_app.command("delete")
def messages_delete(
    message_id: str = typer.Option(..., "--id", help="Message ID to delete"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Delete a message."""
    token = get_token(username, password)
    data = api_post(token, "/user/deleteUserMessage", {"messageId": message_id})
    _out(data, fmt)


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------


@feedback_app.command("subjects")
def feedback_subjects(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """List available feedback subjects."""
    token = get_token(username, password)
    data = api_get(token, "/user/feedback/subject")
    _out(data, fmt)


@feedback_app.command("submit")
def feedback_submit(
    body: str = typer.Option(..., "--body", help="JSON feedback body"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Submit feedback to AirSprint."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
    token = get_legacy_token(username, password)
    data = legacy_post(token, "/feedback/create", payload)
    _out(data, fmt)


# ---------------------------------------------------------------------------
# Local data cache (airports, aircraft) — persistent disk mirror
# ---------------------------------------------------------------------------


def _load_data_cache() -> dict[str, Any]:
    if not DATA_CACHE.exists():
        return {}
    try:
        return json.loads(DATA_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_data_cache(cache: dict[str, Any]) -> None:
    DATA_CACHE.write_text(json.dumps(cache, indent=2))
    DATA_CACHE.chmod(0o600)


def _cache_section_fresh(cache: dict[str, Any], key: str) -> bool:
    section = cache.get(key) or {}
    return bool(section) and (time.time() - section.get("_cached_at", 0)) < DATA_CACHE_TTL


def _refresh_airports(token: str, cache: dict[str, Any]) -> None:
    """Fetch all airports the user can see and mirror them locally."""
    items: list[dict[str, Any]] = []
    offset = 0
    page = 200
    while True:
        resp = legacy_post(token, "/airport", {
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
    resp = legacy_post(token, "/aircraft")
    items = resp.get("data", {}).get("items", [])
    by_id = {
        a["id"]: {"name": a.get("aircraftName", a.get("name", ""))}
        for a in items if "id" in a
    }
    cache["aircraft"] = {"_cached_at": int(time.time()), "by_id": by_id}


def _refresh_my_aircraft(token: str, cache: dict[str, Any]) -> None:
    resp = legacy_post(token, "/my-aircraft")
    items = resp.get("data", {}).get("items", [])
    cache["my_aircraft"] = {"_cached_at": int(time.time()), "items": items}


def _resolve_airport(token: str, icao: str) -> str:
    """Resolve ICAO code to legacy API airport UUID, using local mirror first."""
    icao = icao.upper()
    cache = _load_data_cache()

    # Try cached mirror first
    section = cache.get("airports") or {}
    by_icao = section.get("by_icao") or {}
    if icao in by_icao:
        return by_icao[icao]["id"]

    # Fall back to single-airport lookup; opportunistically extend cache
    resp = legacy_post(token, "/airport", {
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
    if not _cache_section_fresh(cache, "my_aircraft"):
        _refresh_my_aircraft(token, cache)
        _save_data_cache(cache)
    items = (cache.get("my_aircraft") or {}).get("items") or []
    if not items:
        _die("No aircraft found on account", EXIT_NOT_FOUND)
    return items[0]["aircraftId"]


# ---------------------------------------------------------------------------
# quote (legacy API — api.airsprint.com)
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
    token = get_legacy_token(username, password)

    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
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
        resp = legacy_post(token, "/flight-quote", payload)
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
    token = get_legacy_token(username, password)
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
        resp = legacy_post(token, "/flight-quote", payload)
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

    This calls the legacy API (api.airsprint.com) for server-side cost breakdown.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
    token = get_legacy_token(username, password)
    try:
        resp = legacy_post(token, "/trip/misc-cost-estimate", payload)
        _out(resp.get("data", resp), fmt)
    except RuntimeError as e:
        _die(str(e), EXIT_ERROR)


@quote_app.command("hours-exchange")
def quote_hours_exchange(
    body: str = typer.Option(..., "--body", help="JSON body for hours exchange estimate"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Estimate hours exchange value.

    This calls the legacy API (api.airsprint.com) for server-side hours valuation.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
    token = get_legacy_token(username, password)
    try:
        resp = legacy_post(token, "/hour-exchange/estimate", payload)
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

    token = get_legacy_token(username, password)
    filt: dict[str, Any] = {}
    if query:
        filt["query"] = query
    if saved:
        filt["saved"] = True
    resp = legacy_post(token, "/airport", {
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

    token = get_legacy_token(username, password)
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
    """Refresh the local mirror (airports, aircraft, my-aircraft).

    Stored at ~/.airsprint_cache.json with a 7-day TTL.
    """
    token = get_legacy_token(username, password)
    cache = _load_data_cache()
    _refresh_airports(token, cache)
    _refresh_aircraft(token, cache)
    _refresh_my_aircraft(token, cache)
    _save_data_cache(cache)
    _out({
        "airports": len((cache.get("airports") or {}).get("by_icao") or {}),
        "aircraft": len((cache.get("aircraft") or {}).get("by_id") or {}),
        "my_aircraft": len((cache.get("my_aircraft") or {}).get("items") or []),
        "path": str(DATA_CACHE),
    }, fmt)


@cache_app.command("status")
def cache_status(fmt: str = Format):
    """Show cache contents and freshness."""
    cache = _load_data_cache()
    if not cache:
        _out({"exists": False, "path": str(DATA_CACHE)}, fmt)
        return
    out: dict[str, Any] = {"exists": True, "path": str(DATA_CACHE), "ttl_seconds": DATA_CACHE_TTL}
    for key in ("airports", "aircraft", "my_aircraft"):
        section = cache.get(key) or {}
        cached_at = section.get("_cached_at", 0)
        if not cached_at:
            out[key] = {"present": False}
            continue
        age = int(time.time() - cached_at)
        count = (
            len(section.get("by_icao") or {}) if key == "airports"
            else len(section.get("by_id") or {}) if key == "aircraft"
            else len(section.get("items") or [])
        )
        out[key] = {
            "present": True,
            "count": count,
            "age_seconds": age,
            "fresh": age < DATA_CACHE_TTL,
            "cached_at": _fmt_epoch(cached_at, fmt="%Y-%m-%d %H:%M:%S"),
        }
    _out(out, fmt)


@cache_app.command("clear")
def cache_clear():
    """Delete the local data cache."""
    if DATA_CACHE.exists():
        DATA_CACHE.unlink()
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
    """Single-call dashboard: accounts, hours, upcoming trips, empty legs, unread messages.

    Replaces 4+ separate calls (`user accounts`, `trips list`, `explore flights`,
    `explore counts`) with one compound query — ideal for agents that just want context.
    """
    token = get_legacy_token(username, password)
    now = datetime.now(tz=_tz_utc.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # accounts (also yields the IDs we need for trip filters)
    accts_resp = legacy_post(token, "/my-accounts", {})
    accounts = accts_resp.get("data", {}).get("items", [])
    account_ids = [a["id"] for a in accounts if "id" in a]

    upcoming: list[dict[str, Any]] = []
    upcoming_total = 0
    if account_ids:
        trips_resp = legacy_post(token, "/my-leg", {
            "sort": [{"departureDate": "ASC"}],
            "page": {"limit": upcoming_limit, "offset": 0},
            "filter": {"departureTime": {"min": now}, "accountId": account_ids},
        })
        upcoming = trips_resp.get("data", {}).get("items", []) or []
        upcoming_total = trips_resp.get("data", {}).get("total", len(upcoming))

    legs_resp = legacy_post(token, "/my-flights", {
        "sort": [{"departureTimestamp": "ASC"}],
        "page": {"limit": empty_legs_limit, "offset": 0},
        "filter": {"departureTime": {"min": now}, "type": ["EMPTY_LEG"], "locked": False},
    })
    empty_legs = legs_resp.get("data", {}).get("items", []) or []
    empty_legs_total = legs_resp.get("data", {}).get("total", len(empty_legs))

    notif_resp = legacy_post(token, "/my-notifications", {
        "sort": [], "page": {"limit": 1, "offset": 0},
        "filter": {"isRead": False},
    })
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
        return json.loads(s)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
        return {}  # unreachable


# ---------------------------------------------------------------------------
# raw — generic escape hatches for any endpoint
# ---------------------------------------------------------------------------


@raw_app.command("legacy-get")
def raw_legacy_get(
    path: str = typer.Option(..., "--path", help='Path on api.airsprint.com (e.g. "/my-saved-airports/")'),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """GET against api.airsprint.com (legacy API)."""
    token = get_legacy_token(username, password)
    _out(legacy_get(token, path), fmt, compact)


@raw_app.command("legacy-post")
def raw_legacy_post(
    path: str = typer.Option(..., "--path", help="Path on api.airsprint.com"),
    body: str = typer.Option("{}", "--body", help="JSON body (default empty)"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """POST against api.airsprint.com (legacy API)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, path, _parse_json(body)), fmt, compact)


@raw_app.command("prod2-get")
def raw_prod2_get(
    path: str = typer.Option(..., "--path", help='Path on prod2.airsprint.com (e.g. "/user/preferences")'),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """GET against prod2.airsprint.com (mobile API)."""
    token = get_token(username, password)
    _out(api_get(token, path), fmt, compact)


@raw_app.command("prod2-post")
def raw_prod2_post(
    path: str = typer.Option(..., "--path", help="Path on prod2.airsprint.com"),
    body: str = typer.Option("{}", "--body", help="JSON body (default empty)"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """POST against prod2.airsprint.com (mobile API)."""
    token = get_token(username, password)
    _out(api_post(token, path, _parse_json(body)), fmt, compact)


@raw_app.command("prod2-put")
def raw_prod2_put(
    path: str = typer.Option(..., "--path", help="Path on prod2.airsprint.com"),
    body: str = typer.Option(..., "--body", help="JSON body"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """PUT against prod2.airsprint.com (mobile API)."""
    token = get_token(username, password)
    _out(api_put(token, path, _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# account — account-user management
# ---------------------------------------------------------------------------


@account_app.command("users")
def account_users(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List users on the account (POST /my-account-users)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-account-users", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", resp), fmt, compact)


@account_app.command("user-get")
def account_user_get(
    user_id: str = typer.Option(..., "--id", help="Account-user ID"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get an account-user by ID (GET /my-account-user/{id})."""
    token = get_legacy_token(username, password)
    _out(legacy_get(token, f"/my-account-user/{user_id}"), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/account-user/invite", payload), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/account-user/update", payload), fmt, compact)


@account_app.command("roles")
def account_roles(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List account-user roles (POST /account-user-role)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/account-user-role", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
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
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-passenger", {"sort": [], "page": {"limit": limit, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@passenger_app.command("get")
def passenger_get(
    passenger_id: str = typer.Option(..., "--id"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get a saved passenger (GET /my-passenger/{id})."""
    token = get_legacy_token(username, password)
    _out(legacy_get(token, f"/my-passenger/{passenger_id}"), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-passenger/create", payload), fmt, compact)


# ---------------------------------------------------------------------------
# passport — saved passports & docs
# ---------------------------------------------------------------------------


@passport_app.command("list")
def passport_list(
    limit: int = typer.Option(100, "--limit"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List saved passports (POST /my-passport)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-passport", {"sort": [], "page": {"limit": limit, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@passport_app.command("get")
def passport_get(
    passport_id: str = typer.Option(..., "--id"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get a saved passport (GET /my-passport/{id})."""
    token = get_legacy_token(username, password)
    _out(legacy_get(token, f"/my-passport/{passport_id}"), fmt, compact)


@passport_app.command("create")
def passport_create(
    body: str = typer.Option(..., "--body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a saved passport (POST /my-passport/create)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/my-passport/create"}, fmt, compact)
        return
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-passport/create", payload), fmt, compact)


@passport_app.command("upload-init")
def passport_upload_init(
    body: str = typer.Option(..., "--body", help="JSON body — typically describes file metadata"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Begin a passport document upload — returns a presigned upload URL (POST /my-passport/document/upload-init)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-passport/document/upload-init", _parse_json(body)), fmt, compact)


@passport_app.command("attach")
def passport_attach(
    body: str = typer.Option(..., "--body", help="JSON body — references the uploaded file"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Attach a previously-uploaded document to a passport (POST /my-passport/document/attach)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-passport/document/attach", _parse_json(body)), fmt, compact)


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
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-pet", {"sort": [], "page": {"limit": limit, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@pet_app.command("get")
def pet_get(
    pet_id: str = typer.Option(..., "--id"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get a saved pet (GET /my-pet/{id})."""
    token = get_legacy_token(username, password)
    _out(legacy_get(token, f"/my-pet/{pet_id}"), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-pet/create", payload), fmt, compact)


@pet_app.command("upload-init")
def pet_upload_init(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Begin a pet document upload (POST /my-pet/document/upload-init)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-pet/document/upload-init", _parse_json(body)), fmt, compact)


@pet_app.command("attach")
def pet_attach(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Attach an uploaded document to a pet (POST /my-pet/document/attach)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-pet/document/attach", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# customs — Canadian customs declarations
# ---------------------------------------------------------------------------


@customs_app.command("list")
def customs_list(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List my Canadian customs declarations (POST /myCanadianCustomsDeclaration)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/myCanadianCustomsDeclaration", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@customs_app.command("declaration")
def customs_declaration(
    body: str = typer.Option("{}", "--body", help="Optional filter body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get the customs-declaration form/template (POST /canadian-custom-declaration)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/canadian-custom-declaration", _parse_json(body)), fmt, compact)


@customs_app.command("create")
def customs_create(
    body: str = typer.Option(..., "--body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a customs declaration (POST /canadianCustomsDeclaration/create)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/canadianCustomsDeclaration/create"}, fmt, compact)
        return
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/canadianCustomsDeclaration/create", payload), fmt, compact)


@customs_app.command("create-public")
def customs_create_public(
    body: str = typer.Option(..., "--body"),
    fmt: str = Format, compact: bool = Compact,
):
    """Create a public (link-based) customs declaration — no auth required (POST /canadianCustomsDeclaration/create-public)."""
    payload = _parse_json(body)
    _out(_http("POST", f"{LEGACY_BASE_URL}/canadianCustomsDeclaration/create-public",
               headers={"Content-Type": "application/json", "Accept": "application/json"},
               data=json.dumps(payload).encode("utf-8")), fmt, compact)


@customs_app.command("link-create")
def customs_link_create(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a customs-declaration link to share with a passenger (POST /canadian-customs-declaration-link/create)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/canadian-customs-declaration-link/create", _parse_json(body)), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/empty-leg/book", payload), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/shared-flight/book", payload), fmt, compact)


@booking_app.command("trip-book")
def booking_trip_book(
    body: str = typer.Option(..., "--body"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Book a trip via the legacy API (POST /trip/book). Compare with `booking create` which uses prod2."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/trip/book"}, fmt, compact)
        return
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/trip/book", payload), fmt, compact)


@booking_app.command("cancel-own")
def booking_cancel_own(
    body: str = typer.Option(..., "--body", help='JSON body, e.g. {"legId":"...", "reason":"..."}'),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Self-cancel a leg/trip via the legacy API (POST /cancel-own)."""
    payload = _parse_json(body)
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/cancel-own"}, fmt, compact)
        return
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/cancel-own", payload), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/flight/lock", payload), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/reserve-day", payload), fmt, compact)


@booking_app.command("survey")
def booking_survey(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Submit a post-booking survey (POST /booking-survey/create)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/booking-survey/create", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# trips — manifest & recent
# ---------------------------------------------------------------------------


@trips_app.command("manifest")
def trips_manifest(
    trip_id: str = typer.Option(..., "--id", help="Trip or leg ID"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get the trip manifest (GET /trip/manifest/{id})."""
    token = get_legacy_token(username, password)
    _out(legacy_get(token, f"/trip/manifest/{trip_id}"), fmt, compact)


@trips_app.command("manifest-send")
def trips_manifest_send(
    body: str = typer.Option(..., "--body", help="JSON body — recipients & trip ID"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Email the trip manifest (POST /trip/manifest/send)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/trip/manifest/send", _parse_json(body)), fmt, compact)


@trips_app.command("recent")
def trips_recent(
    limit: int = typer.Option(20, "--limit"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List recent legs (POST /leg/recent/list)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/leg/recent/list", {"sort": [], "page": {"limit": limit, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@trips_app.command("recent-save")
def trips_recent_save(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Save a leg to recents (POST /leg/recent/save)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/leg/recent/save", _parse_json(body)), fmt, compact)


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
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/airport/nearest", {
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
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-saved-airports/", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


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
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/address/autocomplete", {
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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-address/create", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# hours — exchange marketplace (estimate already at quote.hours-exchange)
# ---------------------------------------------------------------------------


@hours_app.command("estimate")
def hours_estimate(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Estimate hours-exchange value (POST /hour-exchange/estimate). Mirror of `quote hours-exchange`."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/hour-exchange/estimate", _parse_json(body)), fmt, compact)


@hours_app.command("power")
def hours_power(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Hours-exchange power calculation (POST /hour-exchange/power)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/hour-exchange/power", _parse_json(body)), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/hours-exchange-listing/create", payload), fmt, compact)


@hours_app.command("my-listings")
def hours_my_listings(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List my hours-exchange listings (POST /my-hours-exchange-listing)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-hours-exchange-listing", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
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
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-file", {"sort": [], "page": {"limit": limit, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@files_app.command("public-create")
def files_public_create(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a public-file record (POST /file-public/create)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/file-public/create", _parse_json(body)), fmt, compact)


# ---------------------------------------------------------------------------
# content — FAQ, policy, system notice, concierge
# ---------------------------------------------------------------------------


@content_app.command("faq")
def content_faq(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List FAQ entries (POST /faq)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/faq", {"sort": [], "page": {"limit": 200, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@content_app.command("faq-categories")
def content_faq_categories(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List FAQ categories (POST /faq-category)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/faq-category", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@content_app.command("policies")
def content_policies(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List policies (POST /policy)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/policy", {"sort": [], "page": {"limit": 200, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@content_app.command("policy-categories")
def content_policy_categories(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List policy categories (POST /policy-category)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/policy-category", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@content_app.command("system-notice")
def content_system_notice(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get current system notice (POST /system-notice)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/system-notice", {}), fmt, compact)


@content_app.command("required-info")
def content_required_info(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get required-info prompts (POST /required-info)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/required-info", {}), fmt, compact)


@content_app.command("concierge")
def content_concierge(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get concierge contact info (POST /concierge)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/concierge", {}), fmt, compact)


# ---------------------------------------------------------------------------
# social — follow / followers
# ---------------------------------------------------------------------------


@social_app.command("followers")
def social_followers(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List my followers (POST /my-user/followers)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-user/followers", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@social_app.command("following")
def social_following(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List who I follow (POST /my-user/following)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-user/following", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@social_app.command("requests")
def social_requests(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List pending follower requests (POST /my-user/follower-requests)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/my-user/follower-requests", {"sort": [], "page": {"limit": 100, "offset": 0}, "filter": {}})
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@social_app.command("follow")
def social_follow(
    user_id: str = typer.Option(..., "--user", help="User ID to follow"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Follow a user (POST /user/follow)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/user/follow", {"userId": user_id}), fmt, compact)


@social_app.command("accept")
def social_accept(
    user_id: str = typer.Option(..., "--user", help="Follower user ID to accept"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Accept a follower request (POST /user/follower/accept)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/user/follower/accept", {"userId": user_id}), fmt, compact)


@social_app.command("decline")
def social_decline(
    user_id: str = typer.Option(..., "--user", help="Follower user ID to decline"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Decline a follower request (POST /user/follower/decline)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/user/follower/decline", {"userId": user_id}), fmt, compact)


# ---------------------------------------------------------------------------
# user — me / change-password / avatar (additional)
# ---------------------------------------------------------------------------


@user_app.command("me")
def user_me(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get my full user record (POST /my-user). Richer than `user profile`."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-user", {}), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-user/change-password", _parse_json(body)), fmt, compact)


@user_app.command("avatar")
def user_avatar(
    user_id: str = typer.Option(..., "--id", help="User ID"),
    output: str = typer.Option("-", "--output", "-o", help="Output file path or - for metadata only"),
    username: Optional[str] = Username, password: Optional[str] = Password,
):
    """Download a user avatar (GET /my-user/avatar/{id})."""
    token = get_legacy_token(username, password)
    url = f"{LEGACY_BASE_URL}/my-user/avatar/{user_id}"
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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-notification-settings", {}), fmt, compact)


@messages_app.command("settings-update")
def messages_settings_update(
    body: str = typer.Option(..., "--body"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Update notification settings (POST /my-notification-settings/update)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-notification-settings/update", _parse_json(body)), fmt, compact)


@messages_app.command("update")
def messages_update(
    body: str = typer.Option(..., "--body", help="JSON body — e.g. mark messages read/unread"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Bulk-update notifications (POST /my-notifications/update)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/my-notifications/update", _parse_json(body)), fmt, compact)


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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/user/2fa/setup", _parse_json(body)), fmt, compact)


@auth_app.command("2fa-verify")
def auth_2fa_verify(
    body: str = typer.Option(..., "--body", help='JSON body, e.g. {"code":"123456"}'),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Verify a 2FA code during setup (POST /user/2fa/verify)."""
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/user/2fa/verify", _parse_json(body)), fmt, compact)


@auth_app.command("2fa-sign-in")
def auth_2fa_sign_in(
    body: str = typer.Option(..., "--body"),
    fmt: str = Format, compact: bool = Compact,
):
    """Complete a 2FA sign-in (POST /user/2fa/sign-in) — no auth header required."""
    _out(_http("POST", f"{LEGACY_BASE_URL}/user/2fa/sign-in",
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
    token = get_legacy_token(username, password)
    _out(legacy_post(token, "/user/2fa/disable", _parse_json(body)), fmt, compact)


@auth_app.command("reset-request")
def auth_reset_request(
    email: str = typer.Option(..., "--email"),
    fmt: str = Format, compact: bool = Compact,
):
    """Request a password-reset email (POST /user/request-reset-password). No auth required."""
    _out(_http("POST", f"{LEGACY_BASE_URL}/user/request-reset-password",
               headers={"Content-Type": "application/json", "Accept": "application/json"},
               data=json.dumps({"email": email}).encode("utf-8")), fmt, compact)


@auth_app.command("reset-confirm")
def auth_reset_confirm(
    body: str = typer.Option(..., "--body", help='JSON body, e.g. {"token":"...", "newPassword":"..."}'),
    fmt: str = Format, compact: bool = Compact,
):
    """Confirm a password reset (POST /user/reset-password). No auth required."""
    _out(_http("POST", f"{LEGACY_BASE_URL}/user/reset-password",
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
