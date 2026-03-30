#!/usr/bin/env python3
"""AirSprint CLI — agent-friendly interface to prod2.airsprint.com.

Auth: OAuth2 password grant against /oauth/token.
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
from datetime import datetime, timezone
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
    import certifi
except ImportError:
    certifi = None

import typer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://prod2.airsprint.com"
LEGACY_BASE_URL = "https://api.airsprint.com/api"
BASIC_AUTH = "Basic VVNFUl9DTElFTlRfQVBQOnBhc3N3b3Jk"
TOKEN_CACHE = Path.home() / ".airsprint_token.json"
LEGACY_TOKEN_CACHE = Path.home() / ".airsprint_legacy_token.json"
DEFAULT_TZ = "America/Montreal"

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
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
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
        raise RuntimeError(
            json.dumps({"status": "error", "message": str(exc)})
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
    """OAuth2 password grant → token dict."""
    form = urlencode({
        "grant_type": "password",
        "username": username,
        "password": password,
        "deviceToken": "",
        "deviceType": "CLI",
    }).encode("utf-8")
    resp = _http(
        "POST",
        f"{BASE_URL}/oauth/token",
        headers={
            "Authorization": BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        data=form,
    )
    if "access_token" not in resp:
        raise RuntimeError(
            json.dumps({"status": "error", "message": "No access_token in response", "response": resp})
        )
    return resp


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


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _out(data: Any, fmt: str = "json") -> None:
    """Print data as JSON (default) or human-readable."""
    if fmt == "json":
        print(json.dumps({"status": "ok", "data": data}, indent=2, default=str))
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


def _parse_local_dt(value: str, tz: str = DEFAULT_TZ) -> str:
    """Parse a date/time string as local time and return UTC ISO 8601.

    Accepts:
      - Already UTC: 2026-04-15T14:00:00Z → passed through
      - ISO with offset: 2026-04-15T10:00:00-04:00 → converted to UTC
      - Local (no offset): 2026-04-15T10:00 → interpreted in --timezone, converted to UTC
      - Date only: 2026-04-15 → midnight in --timezone, converted to UTC
    """
    value = value.strip()

    # Already has Z or offset → parse directly and convert
    if value.endswith("Z") or "+" in value[10:] or value[10:].count("-") > 0 and "T" in value:
        # Check if it has a real offset (not just the date hyphens)
        tail = value[19:] if len(value) > 19 else ""
        if value.endswith("Z") or "+" in tail or (tail and tail[0] == "-"):
            return value  # already has timezone info, pass through

    # No offset → treat as local time
    if "T" not in value:
        value = f"{value}T00:00"  # date only → midnight

    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        _die(f"Cannot parse date: {value}. Use YYYY-MM-DDTHH:MM or YYYY-MM-DD", EXIT_VALIDATION)

    if ZoneInfo:
        try:
            local_dt = naive.replace(tzinfo=ZoneInfo(tz))
            utc_dt = local_dt.astimezone(timezone.utc)
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass

    # Fallback: assume UTC if no ZoneInfo
    return naive.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_epoch(epoch_ms: Any, tz: str = DEFAULT_TZ, fmt: str = "%a %b %d, %H:%M") -> str:
    if not epoch_ms:
        return "-"
    try:
        ts = float(epoch_ms) / 1000 if float(epoch_ms) > 1e12 else float(epoch_ms)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "-"
    if ZoneInfo:
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

app.add_typer(auth_app, name="auth")
app.add_typer(user_app, name="user")
app.add_typer(trips_app, name="trips")
app.add_typer(booking_app, name="booking")
app.add_typer(explore_app, name="explore")
app.add_typer(messages_app, name="messages")
app.add_typer(feedback_app, name="feedback")
app.add_typer(quote_app, name="quote")

# Common options
Username = typer.Option(None, "--username", "-u", envvar="AIRSPRINT_USERNAME", help="Login email")
Password = typer.Option(None, "--password", "-p", envvar="AIRSPRINT_PASSWORD", help="Login password")
Format = typer.Option("json", "--format", "-f", help="Output format: json | human")
Timezone = typer.Option(DEFAULT_TZ, "--timezone", "--tz", help="Timezone for date display")


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
    token = get_token(username, password)
    data = api_get(token, "/user/getInitialUserInfo")
    _out(data, fmt)


@user_app.command("accounts")
def user_accounts(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get account info (shares, access levels)."""
    token = get_token(username, password)
    data = api_get(token, "/user/getAccountInfo")
    _out(data, fmt)


@user_app.command("preferences")
def user_preferences(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get user preferences."""
    token = get_token(username, password)
    data = api_get(token, "/user/preferences")
    _out(data, fmt)


@user_app.command("set-preferences")
def user_set_preferences(
    body: str = typer.Option(..., "--body", help="JSON body with preference fields"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Update user preferences."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)
    token = get_token(username, password)
    data = api_post(token, "/user/preferences", payload)
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
    timezone: str = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """List all trips."""
    token = get_token(username, password)
    data = api_get(token, f"/user/getMyTrips?arrivalTimeZone={timezone}")
    _out(data, fmt)


@trips_app.command("get")
def trips_get(
    booking_id: str = typer.Option(..., "--id", help="Booking ID"),
    timezone: str = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get a specific trip by booking ID."""
    token = get_token(username, password)
    data = api_get(token, f"/user/getBookingById?bookingId={booking_id}&arrivalTimeZone={timezone}")
    if not data:
        _die(f"Trip {booking_id} not found", EXIT_NOT_FOUND)
    _out(data, fmt)


@trips_app.command("tripsheet")
def trips_tripsheet(
    booking_id: str = typer.Option(..., "--id", help="Booking ID"),
    output: str = typer.Option("-", "--output", "-o", help="Output file path (- for stdout info)"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
):
    """Download trip sheet PDF for a booking."""
    token = get_token(username, password)
    url = f"{BASE_URL}/user/downloadTripSheet?bookingId={booking_id}"
    req = Request(url, method="GET", headers=_bearer(token))
    try:
        with urlopen(req, timeout=60, context=_ssl_ctx()) as resp:
            content = resp.read()
            if not content:
                _die(f"No trip sheet available for {booking_id}", EXIT_NOT_FOUND)
            if output == "-":
                _out({"message": f"Trip sheet available, {len(content)} bytes. Use --output FILE to save."})
            else:
                Path(output).write_bytes(content)
                _out({"message": f"Saved to {output}", "size_bytes": len(content)})
    except HTTPError as e:
        _die(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", EXIT_ERROR)


@trips_app.command("invoice")
def trips_invoice(
    trip_id: str = typer.Option(..., "--id", help="Trip ID"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get invoice for a specific trip."""
    token = get_token(username, password)
    data = api_get(token, f"/user/trip/{trip_id}/invoice")
    _out(data, fmt)


@trips_app.command("invoices")
def trips_invoices(
    timezone: str = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """List all invoices."""
    token = get_token(username, password)
    data = api_get(token, f"/user/invoices?arrivalTimeZone={timezone}")
    _out(data, fmt)


@trips_app.command("preflight")
def trips_preflight(
    timezone: str = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get preflight info for upcoming trips."""
    token = get_token(username, password)
    data = api_get(token, f"/user/preflight-info?arrivalTimeZone={timezone}")
    _out(data, fmt)


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
    token = get_token(username, password)
    data = api_post(token, f"/user/flight-feedback/{trip_id}", payload)
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
    """Get booking prep data: accountId, authorizer, locations, aircraft, passengers, contacts.

    Use this BEFORE creating a booking to get valid reference values.
    """
    token = get_token(username, password)
    data = api_get(token, "/user/getBookingInfo")
    _out(data, fmt)


@booking_app.command("create")
def booking_create(
    body: str = typer.Option(..., "--body", help='JSON body with bookingReq object'),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show payload without submitting"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Book a new trip.

    Requires a JSON body with a top-level "bookingReq" object.
    Run `airsprint booking info` first to get valid accountId, authorizer, locations, and aircraft.

    Rules enforced by the API:
    - departure and destination cannot match
    - booking must be >=8 hours in the future
    - max 10 passengers per leg
    - at least one of departureTimeEpoch or arrivalTimeEpoch required
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)

    if "bookingReq" not in payload:
        _die('Body must contain a "bookingReq" key.', EXIT_VALIDATION)

    if dry_run:
        _out({"dry_run": True, "payload": payload, "message": "Would POST /user/bookTrip"}, fmt)
        return

    token = get_token(username, password)
    data = api_post(token, "/user/bookTrip", payload)
    _out(data, fmt)


@booking_app.command("update")
def booking_update(
    body: str = typer.Option(..., "--body", help='JSON body with bookingReq (include bookingId)'),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show payload without submitting"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Update an existing trip (change legs, add return, etc.).

    Set revisionSource to "App" in the body.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON: {e}", EXIT_VALIDATION)

    if dry_run:
        _out({"dry_run": True, "payload": payload, "message": "Would PUT /user/updateTrip"}, fmt)
        return

    token = get_token(username, password)
    data = api_put(token, "/user/updateTrip", payload)
    _out(data, fmt)


@booking_app.command("cancel")
def booking_cancel(
    booking_id: str = typer.Option(..., "--id", help="Booking ID to cancel"),
    leg_id: Optional[str] = typer.Option(None, "--leg-id", help="Cancel a single leg instead of whole trip"),
    authorizer: str = typer.Option(..., "--authorizer", help="Cancellation authorizer contact ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show payload without submitting"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Cancel a trip or a single leg.

    Get the authorizer ID from `airsprint booking info`.
    """
    if leg_id:
        payload = {
            "bookingReq": {
                "bookingId": booking_id,
                "isCancelBookingRequest": False,
                "cancellationAuthorizer": authorizer,
                "bookingLegs": [{"bookingLegId": leg_id, "isCancelBookingLegRequest": True}],
            }
        }
    else:
        payload = {
            "bookingReq": {
                "bookingId": booking_id,
                "isCancelBookingRequest": True,
                "cancellationAuthorizer": authorizer,
                "bookingLegs": None,
            }
        }

    if dry_run:
        _out({"dry_run": True, "payload": payload, "message": "Would PUT /user/updateTrip"}, fmt)
        return

    token = get_token(username, password)
    data = api_put(token, "/user/updateTrip", payload)
    _out(data, fmt)


# ---------------------------------------------------------------------------
# explore
# ---------------------------------------------------------------------------


@explore_app.command("flights")
def explore_flights(
    timezone: str = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """List available empty legs and shared flights."""
    token = get_token(username, password)
    data = api_get(token, "/user/getEmptyLegDetails")
    _out(data, fmt)


@explore_app.command("counts")
def explore_counts(
    timezone: str = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get dashboard counts (unread messages, upcoming trips, etc.)."""
    token = get_token(username, password)
    epoch = int(time.time() * 1000)
    data = api_get(token, f"/user/getAllCounts?currentEpoch={epoch}&arrivalTimeZone={timezone}")
    _out(data, fmt)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


@messages_app.command("list")
def messages_list(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """List all in-app messages."""
    token = get_token(username, password)
    data = api_get(token, "/user/getUserMessages")
    _out(data, fmt)


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
    token = get_token(username, password)
    data = api_post(token, "/user/feedback", payload)
    _out(data, fmt)


# ---------------------------------------------------------------------------
# quote helpers (legacy API UUID resolution)
# ---------------------------------------------------------------------------

_airport_cache: dict[str, str] = {}  # ICAO → UUID


def _resolve_airport(token: str, icao: str) -> str:
    """Resolve ICAO code to legacy API airport UUID."""
    icao = icao.upper()
    if icao in _airport_cache:
        return _airport_cache[icao]

    # Fetch all airports (cached across calls in same invocation)
    if not _airport_cache:
        for offset in range(0, 2000, 100):
            resp = legacy_post(token, "/airport", {"page": {"limit": 100, "offset": offset}})
            items = resp.get("data", {}).get("items", [])
            if not items:
                break
            for a in items:
                code = a.get("codeICAO", "")
                if code:
                    _airport_cache[code] = a["id"]

    if icao not in _airport_cache:
        _die(f"Airport not found: {icao}", EXIT_NOT_FOUND)
    return _airport_cache[icao]


def _get_default_aircraft(token: str) -> str:
    """Get the first aircraft UUID from the user's account."""
    resp = legacy_post(token, "/my-aircraft")
    items = resp.get("data", {}).get("items", [])
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
    timezone: str = Timezone,
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

    Date accepts local time (default: America/Montreal), e.g.:
      --date 2026-04-15T10:00      → 10:00 AM Eastern
      --date 2026-04-15             → midnight Eastern
      --date 2026-04-15T14:00:00Z   → already UTC, passed through
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
    icao: Optional[str] = typer.Option(None, "--icao", help="Filter by ICAO code (e.g. CYQB)"),
    name: Optional[str] = typer.Option(None, "--name", help="Filter by name substring (e.g. Quebec)"),
    limit: int = typer.Option(20, "--limit", help="Max results"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """List airports with their UUIDs (needed for quote --body).

    Use --icao or --name to filter. Returns id, ICAO, IATA, and name.
    """
    token = get_legacy_token(username, password)
    all_airports = []
    for offset in range(0, 2000, 100):
        resp = legacy_post(token, "/airport", {"page": {"limit": 100, "offset": offset}})
        items = resp.get("data", {}).get("items", [])
        if not items:
            break
        all_airports.extend(items)

    if icao:
        icao_upper = icao.upper()
        all_airports = [a for a in all_airports if icao_upper in (a.get("codeICAO") or "").upper()]
    if name:
        name_lower = name.lower()
        all_airports = [a for a in all_airports if name_lower in (a.get("name") or "").lower()
                        or name_lower in json.dumps(a.get("regionsServed", [])).lower()]

    results = [
        {
            "id": a["id"],
            "icao": a.get("codeICAO", ""),
            "iata": a.get("codeIATA", ""),
            "name": a.get("name", ""),
            "city": a.get("address", {}).get("city", ""),
            "country": a.get("address", {}).get("country", ""),
        }
        for a in all_airports[:limit]
    ]
    _out(results, fmt)


@quote_app.command("aircraft")
def quote_aircraft(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """List all AirSprint aircraft types with UUIDs (needed for quote --body)."""
    token = get_legacy_token(username, password)
    resp = legacy_post(token, "/aircraft")
    items = resp.get("data", {}).get("items", [])
    results = [
        {
            "id": a.get("id", ""),
            "name": a.get("aircraftName", a.get("name", "")),
        }
        for a in items
    ]
    _out(results, fmt)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
