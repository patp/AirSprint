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
BASIC_AUTH = "Basic VVNFUl9DTElFTlRfQVBQOnBhc3N3b3Jk"
TOKEN_CACHE = Path.home() / ".airsprint_token.json"
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

app.add_typer(auth_app, name="auth")
app.add_typer(user_app, name="user")
app.add_typer(trips_app, name="trips")
app.add_typer(booking_app, name="booking")
app.add_typer(explore_app, name="explore")
app.add_typer(messages_app, name="messages")
app.add_typer(feedback_app, name="feedback")

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
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
