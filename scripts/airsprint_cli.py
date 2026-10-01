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
import hashlib
import fcntl
import mimetypes
import os
import re
import ssl
import subprocess
import threading
import time
import unicodedata
from contextvars import ContextVar
from datetime import datetime, timezone as _tz_utc
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
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
ANDROID_APP_VERSION = "6.1.12"
ANDROID_APP_VERSION_CODE = 135
API_TOKEN_CACHE = Path.home() / ".airsprint_api_token.json"
DATA_CACHE = Path.home() / ".airsprint_cache.json"  # local mirror: airports, aircraft
BOOKING_WRITE_GUARD = Path.home() / ".airsprint_last_booking_write.json"
CUSTOMS_DRAFT_DIR = Path(os.environ.get("AIRSPRINT_CUSTOMS_DRAFT_DIR", str(Path(__file__).resolve().parents[1] / "drafts" / "customs")))
BOOKING_READ_COOLDOWN_SECONDS = 8
DATA_CACHE_TTL = 7 * 24 * 3600  # 7 days
ACCOUNT_CACHE_TTL = 15 * 60
EPOCH_MILLISECONDS_THRESHOLD = 10_000_000_000
ANDROID_DOCUMENT_MAX_BYTES = 20 * 1024 * 1024
PASSPORT_SCAN_CONTENT_TYPES = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
})

_API_REQUEST_COUNT = 0
_REQUEST_TRACE: ContextVar[list[dict[str, Any]] | None] = ContextVar("airsprint_request_trace", default=None)
_API_REQUEST_LOCK = threading.Lock()
_SSL_CONTEXT: ssl.SSLContext | None = None
_SSL_CONTEXT_LOCK = threading.Lock()
_DATA_CACHE_MEMORY: dict[str, Any] | None = None
_DATA_CACHE_MEMORY_MTIME_NS: int | None = None
_DATA_CACHE_MEMORY_PATH: Path | None = None
_AIRPORT_BY_ID: dict[str, tuple[str | None, str]] | None = None
_READ_ONLY_POST_PATHS = frozenset({
    "/account-user-role",
    "/address/autocomplete",
    "/airport",
    "/airport/nearest",
    "/aircraft",
    "/baggage-type",
    "/concierge",
    "/faq",
    "/faq-category",
    "/flight-quote",
    "/my-accounts",
    "/my-account-users",
    "/my-aircraft",
    "/my-file",
    "/my-flights",
    "/my-hours-exchange-listing",
    "/my-leg",
    "/my-notifications",
    "/my-passenger",
    "/my-pet",
    "/my-user/connections",
    "/my-user/groups",
    "/myCanadianCustomsDeclaration",
    "/policy",
    "/policy-category",
    "/reserve-day",
    "/system-notice",
    "/trip/misc-cost-estimate",
})
_BOOKING_WRITE_POST_PATHS = frozenset({
    "/cancel-own",
    "/empty-leg/book",
    "/flight/lock",
    "/shared-flight/book",
    "/trip/book",
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
    trace = _REQUEST_TRACE.get()
    trace_entry = None
    if trace is not None and api_request:
        api_path = urlparse(url).path.removeprefix("/api")
        read_only = method == "GET" or api_path in _READ_ONLY_POST_PATHS or api_path in {"/user/sign-in-email", "/user/authenticate"}
        trace_entry = {"method": method, "path": api_path, "effect": "read" if read_only else "write", "outcome": "uncertain"}
        trace.append(trace_entry)
    attempts = 2 if first_api_request and retry_first_ssl else 1
    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    if trace_entry is not None:
                        trace_entry["outcome"] = "accepted"
                    return {}
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(json.dumps({
                        "status": "error",
                        "message": "API returned a non-JSON response",
                        "content_type": resp.headers.get("Content-Type", ""),
                    })) from exc
                if api_request:
                    try:
                        _check_api_response(result)
                    except RuntimeError as exc:
                        if trace_entry is not None:
                            try:
                                response_code = json.loads(str(exc)).get("http_code")
                            except (ValueError, AttributeError):
                                response_code = None
                            trace_entry["outcome"] = "rejected" if type(response_code) is int and 400 <= response_code < 500 else "uncertain"
                        raise
                if trace_entry is not None:
                    trace_entry["outcome"] = "accepted"
                return result
        except HTTPError as exc:
            if trace_entry is not None:
                trace_entry["outcome"] = "rejected" if 400 <= exc.code < 500 else "uncertain"
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


def _check_api_response(response: Any) -> None:
    """Android APIClient checks the envelope's HTTP code even on HTTP 200.

    For example, a refused /flight/lock must remain a failed request. Do not
    retry it or let the output formatter discard the failure envelope.
    """
    if not isinstance(response, dict):
        return
    code = response.get("httpStatusCode")
    if code is None:
        return
    if type(code) is not int:
        raise RuntimeError(json.dumps({
            "status": "error", "message": "API returned an invalid httpStatusCode.",
        }))
    if not 200 <= code < 300:
        message = next((response[key] for key in ("failureMessage", "httpStatusReason", "message")
                        if isinstance(response.get(key), str) and response[key].strip()), "API request failed")
        error = {"status": "error", "http_code": code, "message": message}
        if isinstance(response.get("status"), str):
            error["api_status"] = response["status"]
        raise RuntimeError(json.dumps(error))


def _multipart_value(value: Any, field: str) -> str:
    """Convert one presigned POST field without permitting header injection."""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, separators=(",", ":"))
    text = str(value)
    if "\r" in text or "\n" in text:
        _die(f'Presigned upload field "{field}" contains a newline.', EXIT_ERROR)
    return text


def _multipart_form_body(
    fields: dict[str, Any],
    file_name: str,
    content_type: str,
    content: bytes,
) -> tuple[bytes, str]:
    """Build the form POST used by Android for presigned storage uploads."""
    boundary = f"----AirSprintCLI{os.getpid():x}{time.time_ns():x}"
    chunks: list[bytes] = []
    for raw_name, raw_value in fields.items():
        name = _multipart_value(raw_name, "name").replace('"', "%22")
        value = _multipart_value(raw_value, name)
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode(),
            b"\r\n",
        ])
    safe_name = Path(file_name).name.replace('"', "%22").replace("\r", "").replace("\n", "")
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), boundary


def _post_presigned_multipart(
    url: str,
    fields: dict[str, Any],
    file_path: Path,
    content_type: str,
) -> int:
    """Send Android's one-shot multipart POST to presigned object storage."""
    content = file_path.read_bytes()
    body, boundary = _multipart_form_body(fields, file_path.name, content_type, content)
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urlopen(request, timeout=60, context=_ssl_ctx()) as response:
            status = int(getattr(response, "status", response.getcode()))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(json.dumps({
            "status": "error",
            "http_code": exc.code,
            "message": f"Presigned storage upload failed: {detail}",
        })) from exc
    except (OSError, URLError, ssl.SSLError) as exc:
        raise RuntimeError(json.dumps({
            "status": "error",
            "message": f"Presigned storage upload failed: {exc}",
        })) from exc
    if not 200 <= status < 300:
        raise RuntimeError(json.dumps({
            "status": "error",
            "http_code": status,
            "message": "Presigned storage upload failed",
        }))
    return status


def _document_file(file_value: str, content_type: str | None) -> tuple[Path, str]:
    path = Path(file_value).expanduser()
    if not path.is_file():
        _die(f"Document file not found: {path}", EXIT_NOT_FOUND)
    size = path.stat().st_size
    if size > ANDROID_DOCUMENT_MAX_BYTES:
        _die(
            f"Document is {size} bytes; Android limits uploads to "
            f"{ANDROID_DOCUMENT_MAX_BYTES} bytes.",
            EXIT_VALIDATION,
        )
    mime = (content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream").strip()
    if not mime or "\r" in mime or "\n" in mime:
        _die("Invalid document content type.", EXIT_VALIDATION)
    return path, mime


def _passport_scan_file(file_value: str, content_type: str | None) -> tuple[Path, str]:
    """Validate a passport photo/scan before any AirSprint write is sent."""
    path, mime = _document_file(file_value, content_type)
    if mime not in PASSPORT_SCAN_CONTENT_TYPES:
        _die(
            "Passport photo/scan must be JPEG, PNG, or PDF; "
            f"got {mime or 'an unknown content type'}.",
            EXIT_VALIDATION,
        )
    signatures = {
        "application/pdf": b"%PDF-",
        "image/jpeg": b"\xff\xd8\xff",
        "image/png": b"\x89PNG\r\n\x1a\n",
    }
    expected = signatures[mime]
    with path.open("rb") as stream:
        header = stream.read(len(expected))
    if header != expected:
        _die(
            f"Passport file contents do not match declared content type {mime}.",
            EXIT_VALIDATION,
        )
    return path, mime


def _android_document_upload(
    token: str,
    *,
    file_path: Path,
    content_type: str,
    init_path: str,
    attach_path: str,
    base_payload: dict[str, Any],
) -> dict[str, Any]:
    """Run Android's init -> presigned multipart POST -> attach workflow."""
    init_payload = {
        **base_payload,
        "fileName": file_path.name,
        "contentType": content_type,
        "maxFileSizeBytes": ANDROID_DOCUMENT_MAX_BYTES,
    }
    init_response = api_post(token, init_path, init_payload)
    init_data = _response_data(init_response)
    if not isinstance(init_data, dict):
        raise RuntimeError(json.dumps({
            "status": "error",
            "message": "Upload init returned no data object.",
        }))
    presigned = init_data.get("presignedUpload")
    storage_path = init_data.get("storagePath")
    if not isinstance(presigned, dict) or not isinstance(storage_path, str):
        raise RuntimeError(json.dumps({
            "status": "error",
            "message": "Upload init omitted presignedUpload or storagePath.",
        }))
    upload_url = presigned.get("url")
    upload_fields = presigned.get("fields")
    if not isinstance(upload_url, str) or not isinstance(upload_fields, dict):
        raise RuntimeError(json.dumps({
            "status": "error",
            "message": "Upload init returned an invalid presigned POST.",
        }))
    upload_status = _post_presigned_multipart(
        upload_url,
        upload_fields,
        file_path,
        content_type,
    )
    attach_payload = {
        **base_payload,
        "fileName": file_path.name,
        "contentType": content_type,
        "storagePath": storage_path,
    }
    attached = api_post(token, attach_path, attach_payload)
    return {
        "result": attached,
        "fileName": file_path.name,
        "contentType": content_type,
        "storagePath": storage_path,
        "storageUploadStatus": upload_status,
        "message": "Initialized, uploaded, and attached using the Android workflow.",
    }


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
    data = _response_data(resp)
    token = data.get("authToken") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip():
        user_id = (data.get("userId") or data.get("id")) if isinstance(data, dict) else None
        if isinstance(user_id, str) and user_id:
            raise RuntimeError(json.dumps({
                "status": "error", "requires2fa": True, "userId": user_id, "email": username,
                "message": "Complete sign-in with `auth 2fa-sign-in --user-id USER_ID --code CODE --username EMAIL`.",
            }))
        raise RuntimeError(
            json.dumps({"status": "error", "message": "No authToken in sign-in response"})
        )
    return token


def _save_api_token(token: str, email: str) -> None:
    data = {"authToken": token, "email": email, "_cached_at": int(time.time())}
    _atomic_write_json(API_TOKEN_CACHE, data)


def _load_api_token(email: str | None = None) -> str | None:
    if not API_TOKEN_CACHE.exists():
        return None
    try:
        data = json.loads(API_TOKEN_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    cached_email = data.get("email")
    if email and (not isinstance(cached_email, str) or cached_email.strip().casefold() != email.strip().casefold()):
        return None
    # api.airsprint.com tokens don't have expires_in — use 6 hour TTL.
    cached_at = data.get("_cached_at")
    if not isinstance(cached_at, (int, float)) or not 0 <= time.time() - cached_at <= 21600:
        return None
    token = data.get("authToken")
    return token if isinstance(token, str) and token.strip() else None


def get_api_token(username: str | None = None, password: str | None = None) -> str:
    """Return a valid api.airsprint.com authToken, using cache when possible."""
    u = username or os.environ.get("AIRSPRINT_USERNAME", "")
    cached = _load_api_token(u or None)
    if cached:
        return cached

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
    live_booking_read = path.startswith((
        "/trip/",
        "/leg/",
        "/my-flight/",
        "/my-leg/",
    ))
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
    if path in _BOOKING_WRITE_POST_PATHS:
        _record_booking_write(path)
    result = _http(
        "POST",
        f"{API_BASE_URL}{path}",
        headers={
            "x-airsprint-auth-token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        api_request=True,
        retry_first_ssl=path in _READ_ONLY_POST_PATHS,
    )
    return result


def api_authenticate(auth_token: str) -> dict[str, Any]:
    """Validate an auth token exactly as Android does, without an auth header."""
    return _http(
        "POST",
        f"{API_BASE_URL}/user/authenticate",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=json.dumps({"authToken": auth_token}).encode("utf-8"),
        api_request=True,
        retry_first_ssl=True,
    )


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
    from contextvars import copy_context

    with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as executor:
        futures = {name: executor.submit(copy_context().run, task) for name, task in tasks.items()}
        return {name: future.result() for name, future in futures.items()}


def _response_data(response: Any) -> Any:
    if not isinstance(response, dict):
        return response
    data = response.get("data", response)
    if isinstance(data, dict) and set(data) == {"data"}:
        return data["data"]
    return data


def _response_items(response: Any, limit: int | None = None) -> list[Any]:
    """Extract an API collection without assuming one response envelope shape."""
    data = _response_data(response)
    if isinstance(data, dict):
        items = data.get("items", [])
    elif isinstance(data, list):
        items = data
    else:
        items = []
    result = list(items) if isinstance(items, list) else []
    return result[:limit] if limit is not None else result


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
    matches = {str(item["tripId"]) for item in items
               if str(item.get("bookingId", "")).casefold() == identifier.strip().casefold() and item.get("tripId")}
    if len(matches) > 1:
        _die(f"Booking code {identifier} matches multiple trips. Use the exact trip UUID.", EXIT_VALIDATION)
    total = response.get("data", {}).get("total")
    if (type(total) is int and total > len(items)) or (total is None and len(items) >= 200):
        _die("Booking-code lookup is incomplete. Use the exact trip UUID from trips list; no detail request was sent.", EXIT_VALIDATION)
    if not matches:
        _die(f"Trip {identifier} not found", EXIT_NOT_FOUND)
    return matches.pop()


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


# ---------------------------------------------------------------------------
# Output presentation — the agent-facing view of AirSprint records
# ---------------------------------------------------------------------------
#
# AirSprint responses carry backend mechanics: type discriminators, reverse
# link tables, S3 URLs, FL3XX references, raw epoch integers and transport
# envelopes. Agents need identifiers (passenger, leg, trip, passport, pet,
# airport IDs, booking codes) and human-readable facts, nothing else, so every
# API-derived value goes through _present() before printing. Dry-run previews
# are printed verbatim: they show the exact request the CLI would send. The
# one placeholder is `customs create` without --link-id, whose link ID only
# exists once the app-style link POST has run (the preview says so).
#
# To extend: add backend-only keys to _INTERNAL_KEYS, add record-scoped
# renames to _RECORD_RENAMES (keyed by AirSprint's "object" discriminator),
# add whole-day fields to _DATE_ONLY_KEYS. Never hide or rename identifiers.

_PRESENT_OUTPUT = True  # cleared by the hidden --internal root option
_OUTPUT_TIMEZONE: str | None = None  # set by commands that accept --timezone

_INTERNAL_KEYS = frozenset({
    "object",
    "accountUserIds", "legPassengerIds", "notificationIds", "notificationSettingId",
    "legDraftGroupIds", "bookingSurveyItemIds", "feedbackSubmissionIds",
    "activityLogIds", "hourExchangeListingIds", "accessLevelId",
    "fl3xxDocumentId", "fl3xxExternalReference",
    "icon",  # JSON-string duplicate of a notification's "data"
    "airportImage", "backgroundImage", "featuredImage",
    "arrivalAirportFeaturedImage", "departureAirportFeaturedImage",
})
_RECORD_RENAMES: dict[str, dict[str, str]] = {
    "passenger": {"isActive": "savedProfile", "age": "category"},
    "pet": {"isActive": "savedProfile"},
    "passport": {
        "dateOfBirthTimestamp": "dateOfBirth",
        "expirationDateTimestamp": "expirationDate",
        "image": "scanAttached",
    },
}
_BOOLEAN_PRESENCE_KEYS = frozenset({"scanAttached"})
_DATE_ONLY_KEYS = frozenset({"dateOfBirthTimestamp", "expirationDateTimestamp"})
_EPOCH_KEY_RE = re.compile(r"(At|Time|Timestamp|Deadline|LastFlight)$|^time$")
_EPOCH_MIN_SECONDS = 100_000_000  # 1973; anything smaller is a duration/count
_GENDERS = ("MALE", "FEMALE", "NONE")
# Android's passenger form shows Male / Female / X and maps X to GenderEnum.NONE
# on the wire (AddNewPersonController::submit). The form's isValid requires
# first name, last name, and a gender.
_GENDER_ALIASES = {"X": "NONE"}


def _gender(value: str, option: str = "--gender") -> str:
    normalized = value.strip().upper()
    return _enum(_GENDER_ALIASES.get(normalized, normalized), option, _GENDERS)
_CATEGORIES = ("ADULT", "CHILD", "INFANT")


def _output_timezone() -> str | None:
    return _OUTPUT_TIMEZONE or os.environ.get("AIRSPRINT_TIMEZONE") or None


def _present_epoch(value: int | float, key: str) -> str:
    seconds = value / 1000 if value >= EPOCH_MILLISECONDS_THRESHOLD else value
    try:
        moment = datetime.fromtimestamp(seconds, tz=_tz_utc.utc)
    except (OverflowError, OSError, ValueError):
        return str(value)
    tz = _output_timezone()
    if tz and ZoneInfo:
        try:
            moment = moment.astimezone(ZoneInfo(tz))
        except Exception:
            pass
    if key in _DATE_ONLY_KEYS:
        return moment.strftime("%Y-%m-%d")
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _present(value: Any, key: str | None = None) -> Any:
    """Return the agent-facing view of an AirSprint value (see section note)."""
    if isinstance(value, dict):
        if {"status", "httpStatusCode", "data"} <= set(value):
            return _present(value["data"], key)
        record = value.get("object")
        renames = _RECORD_RENAMES.get(record, {}) if isinstance(record, str) else {}
        shown: dict[str, Any] = {}
        for field, item in value.items():
            if field in _INTERNAL_KEYS:
                continue
            name = renames.get(field, field)
            if field == "age" and item in _CATEGORIES:
                name = "category"
            if name in _BOOLEAN_PRESENCE_KEYS:
                shown[name] = bool(item)
                continue
            shown[name] = _present(item, field)
        return shown
    if isinstance(value, list):
        return [_present(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool) and key:
        if key in _DATE_ONLY_KEYS:
            return _present_epoch(value, key)
        if _EPOCH_KEY_RE.search(key) and value >= _EPOCH_MIN_SECONDS:
            return _present_epoch(value, key)
    return value


def _out(data: Any, fmt: str = "json", compact: bool = False, *, page: dict | None = None) -> None:
    """Print data as JSON (default) or human-readable. `compact` strips noise.

    API-derived data is passed through _present(); dry-run previews (top-level
    "dry_run": true) are printed exactly as they would be sent.
    """
    is_dry_run = isinstance(data, dict) and (data.get("dry_run") is True or data.get("localDraft") is True)
    if _PRESENT_OUTPUT and not is_dry_run:
        data = _present(data)
    if compact and not is_dry_run:
        data = _compact(data)
    if fmt == "json":
        indent = None if compact else 2
        result = {"status": "ok", "data": data}
        if page is not None:
            result["page"] = page
        print(json.dumps(result, indent=indent, default=str, separators=(",", ":") if compact else None))
    else:
        if isinstance(data, list):
            for item in data:
                _print_dict(item)
                print()
        elif isinstance(data, dict):
            _print_dict(data)
        else:
            print(data)


def _collection_page(response: dict, offset: int, limit: int) -> dict:
    data = _response_data(response)
    items = data.get("items", []) if isinstance(data, dict) else []
    total = data.get("total") if isinstance(data, dict) else None
    known = type(total) is int and total >= offset + len(items)
    has_more = offset + len(items) < total if known else len(items) >= limit
    return {"offset": offset, "limit": limit, "returned": len(items), "total": total if known else None,
            "hasMore": has_more, "complete": not has_more and offset == 0,
            "nextOffset": offset + len(items) if has_more and items else None}


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
    from airsprint_agent import AgentError, error_result
    kind = {EXIT_VALIDATION: "validation", EXIT_NOT_FOUND: "not_found", EXIT_AUTH: "authentication"}.get(code, "request_failed")
    print(json.dumps(error_result(AgentError(message, kind), trace=_REQUEST_TRACE.get())), file=sys.stderr)
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# Form-input helpers — every write command is a typed form
# ---------------------------------------------------------------------------
#
# Agents answer questions the way the AirSprint app asks them (yes/no, a
# choice from a list, a comma-separated list of IDs). The CLI alone turns those
# answers into the request bodies Android 6.1.4 sends. Public commands must not
# accept raw JSON bodies; test_public_commands_do_not_accept_raw_json enforces
# that. Maintainers keep the hidden `raw` group for anything else.


def _yes_no(value: str | None, option: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in ("yes", "y", "true"):
        return True
    if normalized in ("no", "n", "false"):
        return False
    _die(f'{option} must be "yes" or "no".', EXIT_VALIDATION)


def _optional_yes_no(value: str | None, option: str) -> bool | None:
    return None if value is None else _yes_no(value, option)


def _enum(value: str, option: str, allowed: tuple[str, ...]) -> str:
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    if normalized not in allowed:
        _die(f"{option} must be one of: " + ", ".join(allowed), EXIT_VALIDATION)
    return normalized


def _optional_enum(value: str | None, option: str, allowed: tuple[str, ...]) -> str | None:
    return None if value is None else _enum(value, option, allowed)


def _csv(value: str | None, option: str, *, required: bool = False) -> list[str]:
    items = [item.strip() for item in (value or "").split(",") if item.strip()]
    if required and not items:
        _die(f"{option} must contain at least one value.", EXIT_VALIDATION)
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _required_text(value: str | None, option: str) -> str:
    if value is None or not value.strip():
        _die(f"{option} is required.", EXIT_VALIDATION)
    return value.strip()


def _int_between(value: int | float | None, option: str, low: int, high: int) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not low <= value <= high:
        _die(f"{option} must be between {low} and {high}.", EXIT_VALIDATION)
    return value


def _key_value_pairs(values: list[str] | None, option: str) -> dict[str, str]:
    """Parse repeated KEY=VALUE options into a dict, refusing duplicates."""
    pairs: dict[str, str] = {}
    for raw in values or []:
        key, sep, item = raw.partition("=")
        key, item = key.strip(), item.strip()
        if not sep or not key or not item:
            _die(f"{option} expects KEY=VALUE, got: {raw}", EXIT_VALIDATION)
        if key in pairs:
            _die(f"{option} lists {key} twice.", EXIT_VALIDATION)
        pairs[key] = item
    return pairs


def _use_timezone(timezone: str | None) -> None:
    """Make --timezone also govern how timestamps are displayed."""
    global _OUTPUT_TIMEZONE
    if timezone:
        try:
            ZoneInfo(timezone)
        except (KeyError, ValueError):
            _die(f"Unknown timezone: {timezone}", EXIT_VALIDATION)
        _OUTPUT_TIMEZONE = timezone


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
raw_app = typer.Typer(
    help="Maintainer-only raw api.airsprint.com access. Hidden from agents; every command needs --allow-raw.",
    no_args_is_help=True,
    hidden=True,
)
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
device_app = typer.Typer(help="Android-compatible notification-device registration", no_args_is_help=True)
agent_app = typer.Typer(help="Structured command discovery and a persistent typed runtime", no_args_is_help=True)
events_app = typer.Typer(help="Inspect durable events and collect changes from safe lists", no_args_is_help=True)
mcp_app = typer.Typer(help="MCP 2.0 tools and signed webhook events", no_args_is_help=True)

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
app.add_typer(raw_app, name="raw", hidden=True)
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
app.add_typer(device_app, name="device")
app.add_typer(agent_app, name="agent")
app.add_typer(events_app, name="events")
app.add_typer(mcp_app, name="mcp")

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
    internal: bool = typer.Option(
        False,
        "--internal",
        hidden=True,
        help="Maintainers: print AirSprint responses verbatim instead of the agent-facing view.",
    ),
):
    """AirSprint owner operations. Use --skill for agent-safe workflows."""
    global _PRESENT_OUTPUT, _OUTPUT_TIMEZONE
    _PRESENT_OUTPUT = not internal
    _OUTPUT_TIMEZONE = None


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
    """Report whether a token is cached locally; does not contact AirSprint."""
    api_token = _load_api_token()
    if api_token:
        _out({
            "authenticated": True,
            "token_cached": True,
            "verified_live": False,
            "message": "An unexpired token is cached locally; use `auth verify` for one live validation.",
        }, fmt)
    else:
        _out({"authenticated": False, "token_cached": False, "message": "No unexpired token cached"}, fmt)


@auth_app.command("verify")
def auth_verify(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Validate the current token once (POST /user/authenticate), as Android does."""
    token = get_api_token(username, password)
    _out(api_authenticate(token), fmt, compact)


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


@user_app.command("get")
def user_get(
    user_id: str = typer.Option(..., "--id", help="AirSprint user UUID"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Get another user by UUID (GET /user/{id}), as used by Android."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/user/{user_id}"), fmt, compact)


@user_app.command("accounts")
def user_accounts(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Get account info (shares, aircraft, access levels, hours)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-accounts")
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


_NOTIFICATION_SETTINGS = (
    # Toggle names shown by `messages settings`, as stored by AirSprint.
    "emptyLegConfirmed", "bookingConfirmed", "bookingSurveyRequest",
    "feedbackSurveyRequest", "flightCompleted", "upcomingTripTomorrow",
    "itineraryUploaded", "itineraryUpdated", "bookingStatusUpdates",
    "flightItineraryAvailable", "flightManifestAvailable",
    "sharedFlightJoinStatus", "sharedFlightStatusYouShared",
    "networkSharedNewFlight", "hoursExchangeMatchFound",
    "hoursExchangeOfferAccepted", "roleChanges", "newUserAddedToAccount",
    "accessRevoked", "billingProfileChanges", "aircraftAssignmentChanges",
    "weeklyDigest", "airsprintPromotions",
)


def _build_notification_settings_body(on: str | None, off: str | None) -> dict[str, Any]:
    """PATCH /my-notification-settings/update body: {"options": {<toggle>: bool}}."""
    changes: dict[str, bool] = {}
    for option, values, state in (("--on", on, True), ("--off", off, False)):
        for name in _csv(values, option):
            if name not in _NOTIFICATION_SETTINGS:
                _die(
                    f"{option}: unknown notification toggle {name!r}. Choose from: "
                    + ", ".join(_NOTIFICATION_SETTINGS),
                    EXIT_VALIDATION,
                )
            if name in changes and changes[name] != state:
                _die(f"{name} is listed in both --on and --off.", EXIT_VALIDATION)
            changes[name] = state
    if not changes:
        _die("Pass --on and/or --off with notification toggle names (see --help).", EXIT_VALIDATION)
    return {"options": changes}


def _notification_settings_update(
    on: str | None, off: str | None, dry_run: bool,
    username: str | None, password: str | None, fmt: str, compact: bool,
) -> None:
    payload = _build_notification_settings_body(on, off)
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": "/my-notification-settings/update", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_patch(token, "/my-notification-settings/update", payload), fmt, compact)


NotificationsOn = typer.Option(None, "--on", help="Comma-separated toggles to enable (names from `messages settings`)")
NotificationsOff = typer.Option(None, "--off", help="Comma-separated toggles to disable")


@user_app.command("set-preferences")
def user_set_preferences(
    on: Optional[str] = NotificationsOn,
    off: Optional[str] = NotificationsOff,
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Turn notification toggles on/off (same as `messages settings-update`)."""
    _notification_settings_update(on, off, dry_run, username, password, fmt, compact)


# ---------------------------------------------------------------------------
# device — notification registration used by the Android app
# ---------------------------------------------------------------------------


@device_app.command("register-token")
def device_register_token(
    registration_token: str = typer.Option(..., "--registration-token", help="FCM/APNs registration token"),
    device_type: Optional[str] = typer.Option(None, "--device-type", help="Optional Android device type string"),
    device_name: Optional[str] = typer.Option(None, "--device-name", help="Optional device display name"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Register a notification token using Android's exact POST payload."""
    registration_token = registration_token.strip()
    if not registration_token:
        _die("--registration-token must not be empty.", EXIT_VALIDATION)
    payload = {"registrationToken": registration_token}
    if device_type and device_type.strip():
        payload["deviceType"] = device_type.strip()
    if device_name and device_name.strip():
        payload["deviceName"] = device_name.strip()
    path = "/account-notification-registration-token-register"
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": path, "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, path, payload), fmt, compact)


@device_app.command("delete-token")
def device_delete_token(
    registration_token: str = typer.Option(..., "--registration-token", help="FCM/APNs registration token"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before deleting the registration"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Delete a notification token using Android's exact POST payload."""
    path = "/account-notification-registration-token-delete"
    registration_token = registration_token.strip()
    if not registration_token:
        _die("--registration-token must not be empty.", EXIT_VALIDATION)
    payload = {"registrationToken": registration_token}
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": path, "payload": payload}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to delete a notification registration token.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_post(token, path, payload), fmt, compact)



_CONTACT_METHODS = ("EMAIL", "PHONE", "SMS")


def _build_user_update_body(
    *,
    first_name: str | None,
    middle_name: str | None,
    last_name: str | None,
    email: str | None,
    phone: str | None,
    mobile: str | None,
    gender: str | None,
    nationality: str | None,
    preferred_contact_method: str | None,
) -> dict[str, Any]:
    """Android updateUser(): {"options": {<only the fields being changed>}}."""
    fields = {
        "firstName": first_name,
        "middleName": middle_name,
        "lastName": last_name,
        "email": email,
        "phone": phone,
        "mobile": mobile,
        "gender": _optional_enum(gender, "--gender", _GENDERS),
        "nationality": _optional_country_code(nationality, "--nationality"),
        "preferredContactMethod": _optional_enum(
            preferred_contact_method, "--preferred-contact-method", _CONTACT_METHODS
        ),
    }
    options = {key: value for key, value in fields.items() if value is not None}
    if not options:
        _die("Pass at least one field to change (see --help).", EXIT_VALIDATION)
    return {"options": options}


def _optional_country_code(value: str | None, option: str) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        _die(f"{option} must be a two-letter country code such as CA or US.", EXIT_VALIDATION)
    return code


@user_app.command("update")
def user_update(
    first_name: Optional[str] = typer.Option(None, "--first-name"),
    middle_name: Optional[str] = typer.Option(None, "--middle-name"),
    last_name: Optional[str] = typer.Option(None, "--last-name"),
    email: Optional[str] = typer.Option(None, "--email"),
    phone: Optional[str] = typer.Option(None, "--phone"),
    mobile: Optional[str] = typer.Option(None, "--mobile"),
    gender: Optional[str] = typer.Option(None, "--gender", help="MALE, FEMALE, or NONE"),
    nationality: Optional[str] = typer.Option(None, "--nationality", help="Two-letter country code, e.g. CA"),
    preferred_contact_method: Optional[str] = typer.Option(
        None, "--preferred-contact-method", help="EMAIL, PHONE, or SMS"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Edit your own profile. Only the fields you pass are changed."""
    payload = _build_user_update_body(
        first_name=first_name, middle_name=middle_name, last_name=last_name,
        email=email, phone=phone, mobile=mobile, gender=gender,
        nationality=nationality, preferred_contact_method=preferred_contact_method,
    )
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": "/my-user", "payload": payload}, fmt)
        return
    token = get_api_token(username, password)
    _out(api_patch(token, "/my-user", payload), fmt)


# ---------------------------------------------------------------------------
# trips
# ---------------------------------------------------------------------------


@trips_app.command("list")
def trips_list(
    upcoming: bool = typer.Option(True, "--upcoming/--past", help="Show upcoming (default) or past trips"),
    limit: int = typer.Option(25, "--limit", "-n", min=1, max=200, help="Max trips to return"),
    offset: int = typer.Option(0, "--offset", min=0, help="Continue from page.nextOffset"),
    timezone: Optional[str] = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """List trips (including interchange flights).

    Uses api.airsprint.com which returns all trip types including interchange.
    """
    _use_timezone(timezone)
    token = get_api_token(username, password)
    account_ids = _get_account_ids(token)
    if not account_ids:
        _die("No accounts found", EXIT_ERROR)

    now = datetime.now(tz=_tz_utc.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    time_filter = {"min": now} if upcoming else {"max": now}
    sort_dir = "ASC" if upcoming else "DESC"

    payload = {
        "sort": [{"departureDate": sort_dir}],
        "page": {"limit": limit, "offset": offset},
        "filter": {
            "departureTime": time_filter,
            "accountId": account_ids,
        },
    }
    resp = api_post(token, "/my-leg", payload)
    items = resp.get("data", {}).get("items", [])
    _out(items, fmt, compact, page=_collection_page(resp, offset, limit))


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
        "aircraft": lambda: api_post(token, "/my-aircraft"),
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


# ---------------------------------------------------------------------------
# Booking forms — typed answers become the exact Android 6.1.4 booking bodies
# ---------------------------------------------------------------------------
#
# Source of truth: blutter output of base.apk 6.1.4 (package airsprint_dxp).
#   app/features/trips/data/booking_api_models.dart
#     TripBookRequestPayload, TripLegPayload, PassengerPayload, PassportPayload,
#     AddressPayload, RequestSettingsPayload, ShareSettingsPayload,
#     BaggageItemPayload, legIsoDate, passengerPayloadFor, passportIdForPassenger
#   app/features/trips/domain/model/book_shared_request_model.dart
#     BookSharedRequest, BookSharedRequestOptions, BookSharedPassenger,
#     BookSharedRequestSettings
#   app/shared/model/flight_lock_request_model.dart  FlightLockRequestModel
# Every key name below is copied from those toJson functions; nothing is
# invented. Agents never see these keys: they answer form questions.

_GROUND_TRANSPORTATION_TYPES = ("DEPARTURE", "ARRIVAL", "BOTH")
_GROUND_TRANSPORTATION_METHODS = (
    "TAXI",
    "SUBURBAN_RENTAL_CAR",
    "SUV_RENTAL_CAR",
    "FULL_SIZE_RENTAL_CAR",
    "MID_SIZE_RENTAL_CAR",
    "COMPACT_RENTAL_CAR",
    "LIMO_AND_DRIVER",
    "SUV_AND_DRIVER",
    "SEDAN_AND_DRIVER",
)
_SHARE_NETWORK_TYPES = ("MY_NETWORK", "AIRSPRINT_NETWORK")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ADDRESS_TEXT_HELP = 'Address written as "STREET; CITY; STATE; ZIP" (add "; UNIT" for a suite or apartment).'
_GROUND_METHOD_HELP = (
    "taxi | suburban-rental-car | suv-rental-car | full-size-rental-car | mid-size-rental-car | "
    "compact-rental-car | limo-and-driver | suv-and-driver | sedan-and-driver"
)


def _address_payload(
    street: str | None,
    street2: str | None,
    city: str | None,
    state: str | None,
    zip_code: str | None,
    option_prefix: str,
) -> dict[str, str] | None:
    """Android AddressPayload.toJson: street, city, state, zip, then street2 only when set.

    All-or-nothing: leave every option empty for "no address", otherwise
    street, city, state, and zip are all required. There is no country key.
    """
    parts = {"street": street, "city": city, "state": state, "zip": zip_code}
    filled = {key: value.strip() for key, value in parts.items() if value and value.strip()}
    unit = (street2 or "").strip()
    if not filled and not unit:
        return None
    missing = [f"{option_prefix}-{key}" for key in parts if key not in filled]
    if missing:
        _die(
            f"{option_prefix}-street, -city, -state, and -zip go together; missing: " + ", ".join(missing),
            EXIT_VALIDATION,
        )
    if unit:
        filled["street2"] = unit
    return filled


def _address_text(value: str | None, option: str) -> dict[str, str] | None:
    """Parse "STREET; CITY; STATE; ZIP[; UNIT]" into an Android AddressPayload."""
    if value is None or not value.strip():
        return None
    parts = [part.strip() for part in value.split(";")]
    if len(parts) not in (4, 5) or not all(parts[:4]):
        _die(f'{option} must look like "STREET; CITY; STATE; ZIP" (optionally "; UNIT").', EXIT_VALIDATION)
    return _address_payload(parts[0], parts[4] if len(parts) == 5 else None, parts[1], parts[2], parts[3], option)


def _leg_wall_clock(value: str, option: str) -> str:
    """Android legIsoDate: DateTime(y, m, d, h, min) with isUtc=false, then toIso8601String().

    The app sends the wall-clock departure time at the departure airport with
    no zone suffix, e.g. 2026-09-01T16:00:00.000.
    """
    try:
        parsed = datetime.strptime(value.strip().replace(" ", "T"), "%Y-%m-%dT%H:%M")
    except ValueError:
        _die(f"{option} must be YYYY-MM-DDTHH:MM, the local time at the departure airport.", EXIT_VALIDATION)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.000")


def _parse_leg_spec(value: str) -> tuple[str, str, str]:
    """--leg "FROM>TO@YYYY-MM-DDTHH:MM" → (from, to, Android leg date)."""
    route, separator, when = value.partition("@")
    departure, arrow, arrival = route.partition(">")
    departure, arrival = departure.strip(), arrival.strip()
    if not separator or not arrow or not departure or not arrival:
        _die(f'--leg must look like "CYUL>KTEB@2026-09-01T09:00", got: {value}', EXIT_VALIDATION)
    return departure, arrival, _leg_wall_clock(when, "--leg time")


_BAGGAGE_HELP = 'NAME=QUANTITY, repeatable (names from `booking baggage-types`), or "none" when travelling without baggage.'


def _baggage_items(values: list[str] | None, option: str = "--baggage", *, required: bool = True) -> list[dict[str, Any]]:
    """Android BaggageItemPayload: {"name", "quantity"}; names come from `booking baggage-types`.

    Booking commands require an explicit answer: itemized bags, or the word
    "none" for an empty baggage list. Silence is refused so a booking is never
    sent with baggage that was simply forgotten.
    """
    entries = [item.strip() for item in (values or []) if item and item.strip()]
    if any(item.lower() == "none" for item in entries):
        if len(entries) > 1:
            _die(f'{option} none cannot be combined with itemized baggage.', EXIT_VALIDATION)
        return []
    if not entries:
        if required:
            _die(
                f'Baggage must be stated: {option} NAME=QUANTITY (repeatable, names from `booking baggage-types`) '
                f'or {option} none. Nothing was sent.',
                EXIT_VALIDATION,
            )
        return []
    items: list[dict[str, Any]] = []
    for name, quantity in _key_value_pairs(entries, option).items():
        if not quantity.isdigit() or int(quantity) < 1:
            _die(f"{option} {name}={quantity}: quantity must be a whole number of at least 1.", EXIT_VALIDATION)
        items.append({"name": name, "quantity": int(quantity)})
    return items


def _email_list(value: str | None, option: str) -> list[str]:
    emails = _csv(value, option, required=True)
    invalid = [email for email in emails if not _EMAIL_RE.match(email)]
    if invalid:
        _die(f"{option} has invalid email addresses: " + ", ".join(invalid), EXIT_VALIDATION)
    return emails


def _build_request_settings(
    *,
    catering: bool,
    catering_request: str | None,
    ground_transportation: bool,
    ground_transportation_type: str | None,
    ground_transportation_method: str | None,
    pickup_address: dict[str, str] | None,
    dropoff_address: dict[str, str] | None,
    arrival_method: str | None = None,
    arrival_pickup_address: dict[str, str] | None = None,
    arrival_dropoff_address: dict[str, str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Android RequestSettingsPayload.toJson and BookSharedRequestSettings.toJson.

    Both start from the literal {cateringRequired, groundTransportationRequired}
    and append each other key only when it holds a value, in this order.
    Android 6.1.10 also sends arrival transport for shared flights. Only the
    new-trip model sends note; existing-flight callers never pass it.
    """
    if catering_request and not catering:
        _die("--catering-request needs --catering yes.", EXIT_VALIDATION)
    ground_details = (
        ground_transportation_type, ground_transportation_method, pickup_address, dropoff_address,
        arrival_method, arrival_pickup_address, arrival_dropoff_address,
    )
    if any(ground_details) and not ground_transportation:
        _die("Ground transportation details need --ground-transportation yes.", EXIT_VALIDATION)
    body: dict[str, Any] = {
        "cateringRequired": catering,
        "groundTransportationRequired": ground_transportation,
    }
    optional = (
        ("cateringRequest", catering_request),
        ("groundTransportationType", ground_transportation_type),
        ("groundTransportationMethod", ground_transportation_method),
        ("groundTransportationPickUpAddress", pickup_address),
        ("groundTransportationDropOffAddress", dropoff_address),
        ("arrivalGroundTransportationMethod", arrival_method),
        ("arrivalGroundTransportationPickUpAddress", arrival_pickup_address),
        ("arrivalGroundTransportationDropOffAddress", arrival_dropoff_address),
        ("note", note),
    )
    for key, value in optional:
        if value not in (None, "", {}):
            body[key] = value
    return body


def _build_share_settings(
    *,
    special_requests: str,
    open_to_share: bool,
    network_type: str,
    group_ids: list[str],
    seats: int,
    pets_allowed: bool,
    children_allowed: bool,
    cost_percentage: int,
) -> dict[str, Any]:
    """Android ShareSettingsPayload.toJson: all nine keys, always present.

    Defaults are Android's own (ShareSettingsPayload.fromShareSettings):
    "" / false / MY_NETWORK / false / [] / 0 / false / false / 50.
    """
    return {
        "specialRequests": special_requests,
        "openToShare": open_to_share,
        "networkType": network_type,
        "specificGroupsOnly": bool(group_ids),
        "groupIds": group_ids,
        "seats": seats,
        "petsAllowed": pets_allowed,
        "childrenAllowed": children_allowed,
        "joinerVariableCostPercentage": cost_percentage,
    }


def _selection_aircraft(name: str) -> str:
    """Android SelectionAircraftEnum.getByName, after uppercasing the name."""
    name = name.upper()
    return "CJ3" if "CJ3" in name else "CJ2" if "CJ2" in name else "EMBRAER"


def _require_booking_share_eligibility(token: str, aircraft_id: str) -> dict[str, str]:
    """Apply Android 6.1.12 ShareEligibility to a new trip's aircraft.

    Read the actual active account, never union entitlements across accounts
    or silently change account selection. Only catalog/profile reads occur.
    New-trip forms have no existing leg status; this command cannot edit a
    booked leg's sharing settings.
    """
    user = _response_data(api_get(token, "/me"))
    active_id = user.get("activeAccountId") if isinstance(user, dict) else None
    if not isinstance(active_id, str) or not active_id.strip():
        _die("Cannot verify sharing: select an active account in the AirSprint app first. No booking sent.", EXIT_VALIDATION)
    accounts = _get_accounts(token, refresh=True)
    matching = [account for account in accounts if account.get("id") == active_id]
    if len(matching) != 1:
        _die("Cannot verify sharing for the active account. Check the account selected in the AirSprint app. No booking sent.", EXIT_VALIDATION)
    access = matching[0].get("accessLevels")
    if not isinstance(access, list) or not all(isinstance(item, dict) for item in access):
        _die("Cannot verify the active account's aircraft access levels. No booking sent.", EXIT_VALIDATION)

    def text(item: dict[str, Any], key: str) -> str:
        value = item.get(key)
        return value.strip() if isinstance(value, str) else ""

    basis = None
    owned_ids = {text(item, "aircraftId") for item in access} - {""}
    if aircraft_id.strip() in owned_ids:
        basis = "owned-aircraft"
    elif any(text(item, "aircraftName")
             and _selection_aircraft(text(item, "aircraftName")) == "EMBRAER"
             and "INFINITY" in text(item, "accessLevelName").upper() for item in access):
        aircraft = _response_data(api_get(token, f"/aircraft/{aircraft_id}"))
        if not isinstance(aircraft, dict) or aircraft.get("id") != aircraft_id or not text(aircraft, "name"):
            _die("Cannot verify the selected aircraft for sharing. No booking sent.", EXIT_VALIDATION)
        if _selection_aircraft(text(aircraft, "name")) == "CJ2":
            basis = "embraer-infinity-cj2"
    if basis is None:
        _die(
            "The active account cannot offer this aircraft for sharing. Android allows an owned aircraft "
            "or a CJ2 with Embraer Infinity access. Use --open-to-share no or select an eligible account "
            "in the AirSprint app. No booking sent.", EXIT_VALIDATION,
        )
    return {"accountId": active_id, "aircraftId": aircraft_id, "basis": basis}


def _build_trip_passenger(
    passenger_id: str,
    passport_id: str | None,
    destination_address: dict[str, str] | None,
) -> dict[str, Any]:
    """Android PassengerPayload.toJson after removeWhere(null): id, destinationAddress?, passport: {id}?.

    passengerPayloadFor never sets customsDeclarationId when booking a trip;
    passportIdForPassenger picks the passenger's selected passport, else the
    first saved one.
    """
    payload: dict[str, Any] = {"id": passenger_id}
    if destination_address:
        payload["destinationAddress"] = dict(destination_address)
    if passport_id:
        payload["passport"] = {"id": passport_id}
    return payload


def _build_trip_leg(
    *,
    departure_airport_id: str,
    arrival_airport_id: str,
    aircraft_id: str,
    date: str,
    number_of_seats: int,
    passengers: list[dict[str, Any]],
    pet_ids: list[str],
    request_settings: dict[str, Any],
) -> dict[str, Any]:
    """Android TripLegPayload.toJson, in key order."""
    return {
        "departureAirportId": departure_airport_id,
        "arrivalAirportId": arrival_airport_id,
        "aircraftId": aircraft_id,
        "date": date,
        "numberOfSeats": number_of_seats,
        "passengers": passengers,
        "petIds": pet_ids,
        "requestSettings": request_settings,
    }


def _build_trip_book_body(
    *,
    legs: list[dict[str, Any]],
    dog_form_submitted: bool,
    baggage: list[dict[str, Any]],
    share_settings: dict[str, Any],
) -> dict[str, Any]:
    """Android TripBookRequestPayload.toJson (POST /trip/book): legs, dogFormSubmitted, baggage, shareSettings.

    dogFormSubmitted is Android's `cdcNotice == CdcNoticeOption.ALREADY_SUBMITTED`.
    The account is implicit in the token; no accountId is ever sent.
    """
    return {
        "legs": legs,
        "dogFormSubmitted": dog_form_submitted,
        "baggage": baggage,
        "shareSettings": share_settings,
    }


def _build_shared_passenger(
    passenger_id: str,
    destination_address: dict[str, str] | None,
) -> dict[str, Any]:
    """Android BookSharedPassenger.toJson (0x70760c): id, then destinationAddress only when set.

    The model's customsDeclarationId gate tests a constant "" (0x707658), so
    the app never sends that key when booking an existing flight; customs
    declarations are attached later with `leg update-required-info --customs`.
    """
    payload: dict[str, Any] = {"id": passenger_id}
    if destination_address:
        payload["destinationAddress"] = dict(destination_address)
    return payload


def _build_shared_booking_body(
    *,
    flight_id: str,
    passengers: list[dict[str, Any]],
    request_settings: dict[str, Any],
    pet_ids: list[str],
    baggage: list[dict[str, Any]],
) -> dict[str, Any]:
    """Android BookSharedRequest.toJson (POST /empty-leg/book and /shared-flight/book).

    {"flightId", "options": {"passengers", "requestSettings", ["petIds"], ["baggage"]}}
    BookSharedRequestOptions.toJson adds petIds and baggage only when the
    lists are non-empty (the trip form, by contrast, always sends them).
    """
    options: dict[str, Any] = {"passengers": passengers, "requestSettings": request_settings}
    if pet_ids:
        options["petIds"] = pet_ids
    if baggage:
        options["baggage"] = baggage
    return {"flightId": flight_id, "options": options}


def _build_flight_lock_body(flight_id: str, lock: bool) -> dict[str, Any]:
    """Android FlightLockRequestModel.toJson (POST /flight/lock): {"id", "lock"}."""
    return {"id": flight_id, "lock": lock}


def _airport_country(airport_id: Any) -> tuple[str | None, str | None]:
    if not isinstance(airport_id, str):
        return None, None
    return _airport_by_id().get(airport_id, (None, None))


def _is_us_country(country: str | None) -> bool:
    if not country:
        return False
    normalized = re.sub(r"[^a-z]", "", country.lower())
    return normalized in {"us", "usa", "unitedstates", "unitedstatesofamerica"}


_ICAO_RE = re.compile(r"^[A-Za-z]{4}$")


def _normalized_country(country: str | None) -> str:
    return re.sub(r"[^a-z]", "", (country or "").lower())


def _booking_route_check(
    payload: dict[str, Any],
    typed_routes: list[tuple[str, str]],
    us_touching_override: bool | None,
    international_override: bool | None,
) -> dict[str, Any]:
    """Classify the trip: does it touch the US, and does any leg cross a border?

    Countries come from the local airport mirror (`cache refresh`); ICAO
    prefixes (K/P = US, C = Canada, ...) are the fallback when a country is
    missing. When the mirror cannot answer, the caller must state
    --us-touching/--not-us-touching and --international/--domestic.
    """
    airports: list[dict[str, Any]] = []
    unresolved: list[str] = []
    us_detected = False
    international_detected = False
    for leg, (typed_departure, typed_arrival) in zip(payload.get("legs", []), typed_routes):
        prefixes: list[str | None] = []
        countries: list[str | None] = []
        for field, typed in (("departureAirportId", typed_departure), ("arrivalAirportId", typed_arrival)):
            airport_id = leg.get(field)
            country, icao = _airport_country(airport_id)
            if not icao and _ICAO_RE.match(typed):
                icao = typed.upper()
            airports.append({"airportId": airport_id, "icao": icao, "country": country})
            if country is None:
                unresolved.append(str(airport_id))
            if _is_us_country(country) or (country is None and icao and icao.startswith(("K", "P"))):
                us_detected = True
            countries.append(_normalized_country(country) or None)
            prefixes.append(icao[0] if icao else None)
        if all(countries):
            if countries[0] != countries[1]:
                international_detected = True
        elif all(prefixes) and prefixes[0] != prefixes[1]:
            international_detected = True

    if unresolved:
        if us_touching_override is None and not us_detected:
            _die(
                "Could not determine every airport country from the local mirror. "
                "Run `cache refresh`, or pass --us-touching/--not-us-touching explicitly.",
                EXIT_VALIDATION,
            )
        if international_override is None and not international_detected:
            _die(
                "Could not determine every airport country from the local mirror. "
                "Run `cache refresh`, or pass --international/--domestic explicitly.",
                EXIT_VALIDATION,
            )
    return {
        "usTouching": us_detected if us_touching_override is None else us_touching_override,
        "international": international_detected if international_override is None else international_override,
        "airports": airports,
    }


def _passenger_passports(passenger: Any) -> list[dict[str, Any]]:
    """Full passport records embedded in a `/my-passenger/{id}` read, if present.

    The passenger detail model carries `passports` objects alongside the
    `passportIds` ordering list; a thin response may carry only the ids, in
    which case a scan cannot be verified and this returns an empty list.
    """
    data = _response_data(passenger)
    if not isinstance(data, dict):
        return []
    records = data.get("passports")
    if not isinstance(records, list):
        options = data.get("options")
        records = options.get("passports") if isinstance(options, dict) else None
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def _passport_scan_attached(passport: dict[str, Any]) -> bool:
    """True when a passport record has its photo/scan on file (`image` non-empty).

    Mirrors how the CLI presents the field as `scanAttached` (`bool(image)`):
    the API stores the uploaded document's path in `image` and leaves it empty
    until one is attached.
    """
    return bool(passport.get("image"))


def _require_international_travel_documents(
    payload: dict[str, Any],
    check: dict[str, Any],
    read_passenger: Callable[[str], Any],
) -> None:
    """Refuse a border-crossing booking when a passenger's travel documents are incomplete.

    Stricter than Android 6.1.4, which only warns. Every passenger on an
    international trip needs a passport on file, and — because AirSprint requires
    the passport photo before departure — that passport must have its scan/photo
    uploaded. A thin response (passport ids only) cannot establish that the
    scan exists, so it must not authorize the booking. The destination address
    is enforced separately by `_apply_booking_destination_address`.
    """
    if not check.get("international"):
        return
    missing_passport: list[str] = []
    missing_scan: list[str] = []
    unverified_scan: list[str] = []
    for leg in payload.get("legs", []):
        for passenger in leg.get("passengers") or []:
            passenger_id = passenger.get("id")
            passport = passenger.get("passport")
            passport_id = passport.get("id") if isinstance(passport, dict) else None
            if not passport_id:
                if passenger_id not in missing_passport:
                    missing_passport.append(passenger_id)
                continue
            records = _passenger_passports(read_passenger(passenger_id))
            if not records:
                if passenger_id not in unverified_scan:
                    unverified_scan.append(passenger_id)
                continue
            selected = next((record for record in records if record.get("id") == passport_id), None)
            if selected is None and passenger_id not in unverified_scan:
                unverified_scan.append(passenger_id)
            elif selected is not None and not _passport_scan_attached(selected) and passenger_id not in missing_scan:
                missing_scan.append(passenger_id)
    clauses: list[str] = []
    if missing_passport:
        clauses.append("passengers without a passport on file: " + ", ".join(missing_passport))
    if missing_scan:
        clauses.append("passengers whose passport has no photo/scan uploaded: " + ", ".join(missing_scan))
    if unverified_scan:
        clauses.append("passengers whose passport photo/scan could not be verified from the response: " + ", ".join(unverified_scan))
    if not clauses:
        return
    message = "This trip crosses a border; " + "; ".join(clauses) + ". "
    if missing_passport:
        message += (
            "Add a passport with `passport create --passenger-id ...` (then `passport list`) "
            "or answer --passport PASSENGER_ID=PASSPORT_ID. "
        )
    if missing_scan:
        message += "Attach the photo/scan with `passport upload-document --id PASSPORT_ID --file ...`. "
    message += "No booking was sent."
    _die(message, EXIT_VALIDATION)


def _apply_booking_destination_address(
    payload: dict[str, Any],
    address: dict[str, str] | None,
    check: dict[str, Any],
) -> None:
    """US-touching and international trips need where the passengers will stay (hotel, residence, ...)."""
    needs_destination = bool(check.get("usTouching") or check.get("international"))
    if needs_destination and address is None:
        _die(
            "US-touching and international bookings require a destination address (hotel, residence, ...): "
            "--destination-street, --destination-city, --destination-state, --destination-zip. "
            "No booking was sent.",
            EXIT_VALIDATION,
        )
    if address is None:
        return
    for index, leg in enumerate(payload.get("legs", [])):
        passengers = leg.get("passengers") or []
        if not passengers and needs_destination:
            _die(f"Leg {index + 1} has no passengers; a US or international trip needs at least one.", EXIT_VALIDATION)
        for passenger in passengers:
            passenger["destinationAddress"] = dict(address)


def _passenger_default_passport_id(passenger: Any) -> str | None:
    """Android passportIdForPassenger: the selected passport, else the first saved one.

    The live owner API does not persist a selected passport, so the first
    saved passport is used. Reads nothing itself; pass a `/my-passenger/{id}`
    response so the caller can share one read with the document guard.
    """
    ids = _passenger_passport_ids(passenger)
    return ids[0] if ids else None


@booking_app.command("create")
def booking_create(
    leg: list[str] = typer.Option(
        ...,
        "--leg",
        help='One per flight leg, in order: "FROM>TO@YYYY-MM-DDTHH:MM" (ICAO codes or airport IDs; local time at the departure airport).',
    ),
    aircraft_id: Optional[str] = typer.Option(None, "--aircraft-id", help="Aircraft ID from `booking info`; defaults to the account's aircraft."),
    passengers: Optional[str] = typer.Option(None, "--passengers", help="Comma-separated saved passenger IDs flying on every leg."),
    passport: Optional[list[str]] = typer.Option(
        None, "--passport",
        help="PASSENGER_ID=PASSPORT_ID, repeatable. Otherwise each passenger's first saved passport is used, as in the app.",
    ),
    seats: Optional[int] = typer.Option(None, "--seats", min=1, help="Seats reserved on each leg (default: number of passengers, at least 1)."),
    pets: Optional[str] = typer.Option(None, "--pets", help="Comma-separated pet IDs from `pet list`."),
    baggage: list[str] = typer.Option(..., "--baggage", help=_BAGGAGE_HELP),
    catering: str = typer.Option("no", "--catering", help="yes | no"),
    catering_request: Optional[str] = typer.Option(None, "--catering-request", help="What to serve (needs --catering yes)."),
    ground_transportation: str = typer.Option("no", "--ground-transportation", help="yes | no"),
    ground_transportation_when: Optional[str] = typer.Option(None, "--ground-transportation-when", help="departure | arrival | both"),
    ground_transportation_method: Optional[str] = typer.Option(None, "--ground-transportation-method", help=_GROUND_METHOD_HELP),
    ground_pickup_address: Optional[str] = typer.Option(None, "--ground-pickup-address", help=_ADDRESS_TEXT_HELP),
    ground_dropoff_address: Optional[str] = typer.Option(None, "--ground-dropoff-address", help=_ADDRESS_TEXT_HELP),
    arrival_ground_method: Optional[str] = typer.Option(None, "--arrival-ground-method", help="Method at the arrival city when --ground-transportation-when both."),
    arrival_pickup_address: Optional[str] = typer.Option(None, "--arrival-pickup-address", help=_ADDRESS_TEXT_HELP),
    arrival_dropoff_address: Optional[str] = typer.Option(None, "--arrival-dropoff-address", help=_ADDRESS_TEXT_HELP),
    note: Optional[str] = typer.Option(None, "--note", help="Note to the flight concierge for every leg."),
    destination_street: Optional[str] = typer.Option(None, "--destination-street", help="Where passengers stay (required for US trips)."),
    destination_street2: Optional[str] = typer.Option(None, "--destination-street2", help="Suite / apartment / unit."),
    destination_city: Optional[str] = typer.Option(None, "--destination-city"),
    destination_state: Optional[str] = typer.Option(None, "--destination-state"),
    destination_zip: Optional[str] = typer.Option(None, "--destination-zip"),
    us_touching: Optional[bool] = typer.Option(
        None,
        "--us-touching/--not-us-touching",
        help="Override US detection when airport countries are not in the local cache.",
    ),
    international: Optional[bool] = typer.Option(
        None,
        "--international/--domestic",
        help="Override border-crossing detection when airport countries are not in the local cache.",
    ),
    dog_form_submitted: str = typer.Option("no", "--dog-form-submitted", help="yes if the CDC dog-import form is already submitted (dogs entering the US)."),
    special_requests: Optional[str] = typer.Option(None, "--special-requests", help="Free text shown with the trip."),
    open_to_share: str = typer.Option("no", "--open-to-share", help="yes | no — offer spare seats to joiners."),
    share_network: str = typer.Option("my-network", "--share-network", help="my-network | airsprint-network"),
    share_groups: Optional[str] = typer.Option(None, "--share-groups", help="Comma-separated group IDs; limits sharing to those groups."),
    share_seats: int = typer.Option(0, "--share-seats", min=0, help="Seats offered to joiners."),
    share_pets_allowed: str = typer.Option("no", "--share-pets-allowed", help="yes | no"),
    share_children_allowed: str = typer.Option("no", "--share-children-allowed", help="yes | no"),
    share_cost_percentage: int = typer.Option(50, "--share-cost-percentage", help="Joiner share of variable cost, 30–80 (app default 50)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show the exact request without submitting"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Book a new trip the way the AirSprint app does (POST /trip/book).

    Answer the booking form with options; the CLI builds Android 6.1.12's exact
    request. Catering, ground transportation, and notes apply to every leg —
    adjust one leg afterwards with `leg update-required-info`. Run
    `booking info` first for aircraft, passenger, and airport IDs.

    Guard rails (nothing is sent when one fails): baggage must be answered
    (`--baggage none` for no bags); a trip that crosses a border needs, for
    every passenger, a passport on file whose photo/scan has been uploaded
    (AirSprint requires the passport image before departure); US-touching or
    international trips need a destination address (hotel, residence, ...).
    Sharing requires an eligible aircraft on the active account.
    """
    catering_flag = _yes_no(catering, "--catering")
    ground_flag = _yes_no(ground_transportation, "--ground-transportation")
    dog_form = _yes_no(dog_form_submitted, "--dog-form-submitted")
    open_flag = _yes_no(open_to_share, "--open-to-share")
    network_type = _enum(share_network, "--share-network", _SHARE_NETWORK_TYPES)
    pets_flag = _yes_no(share_pets_allowed, "--share-pets-allowed")
    children_flag = _yes_no(share_children_allowed, "--share-children-allowed")
    percentage = _int_between(share_cost_percentage, "--share-cost-percentage", 30, 80)
    group_ids = _csv(share_groups, "--share-groups")
    if group_ids and not open_flag:
        _die("--share-groups needs --open-to-share yes.", EXIT_VALIDATION)
    passenger_ids = _csv(passengers, "--passengers")
    pet_ids = _csv(pets, "--pets")
    passport_by_passenger = _key_value_pairs(passport, "--passport")
    unknown = sorted(set(passport_by_passenger) - set(passenger_ids))
    if unknown:
        _die("--passport names passengers not in --passengers: " + ", ".join(unknown), EXIT_VALIDATION)
    baggage_items = _baggage_items(baggage)
    destination = _address_payload(
        destination_street, destination_street2, destination_city, destination_state, destination_zip,
        "--destination",
    )
    request_settings = _build_request_settings(
        catering=catering_flag,
        catering_request=(catering_request or "").strip() or None,
        ground_transportation=ground_flag,
        ground_transportation_type=_optional_enum(ground_transportation_when, "--ground-transportation-when", _GROUND_TRANSPORTATION_TYPES),
        ground_transportation_method=_optional_enum(ground_transportation_method, "--ground-transportation-method", _GROUND_TRANSPORTATION_METHODS),
        pickup_address=_address_text(ground_pickup_address, "--ground-pickup-address"),
        dropoff_address=_address_text(ground_dropoff_address, "--ground-dropoff-address"),
        arrival_method=_optional_enum(arrival_ground_method, "--arrival-ground-method", _GROUND_TRANSPORTATION_METHODS),
        arrival_pickup_address=_address_text(arrival_pickup_address, "--arrival-pickup-address"),
        arrival_dropoff_address=_address_text(arrival_dropoff_address, "--arrival-dropoff-address"),
        note=(note or "").strip() or None,
    )
    leg_specs = [_parse_leg_spec(item) for item in leg]
    number_of_seats = seats if seats is not None else max(1, len(passenger_ids))

    token: str | None = None

    def auth() -> str:
        nonlocal token
        if token is None:
            token = get_api_token(username, password)
        return token

    def airport_id(code: str) -> str:
        return code if _UUID_RE.match(code) else _resolve_airport(auth(), code)

    resolved_aircraft = aircraft_id or _get_default_aircraft(auth())
    share_eligibility = _require_booking_share_eligibility(auth(), resolved_aircraft) if open_flag else None

    passenger_reads: dict[str, Any] = {}

    def read_passenger(passenger_id: str) -> Any:
        # One /my-passenger/{id} read per passenger, shared between default
        # passport resolution and the border-crossing document guard.
        if passenger_id not in passenger_reads:
            passenger_reads[passenger_id] = api_get(auth(), f"/my-passenger/{passenger_id}")
        return passenger_reads[passenger_id]

    passport_ids: dict[str, str | None] = {}
    for passenger_id in passenger_ids:
        passport_ids[passenger_id] = (
            passport_by_passenger.get(passenger_id)
            or _passenger_default_passport_id(read_passenger(passenger_id))
        )

    legs: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    typed_routes: list[tuple[str, str]] = []
    for departure, arrival, date in leg_specs:
        legs.append(_build_trip_leg(
            departure_airport_id=airport_id(departure),
            arrival_airport_id=airport_id(arrival),
            aircraft_id=resolved_aircraft,
            date=date,
            number_of_seats=number_of_seats,
            passengers=[
                _build_trip_passenger(passenger_id, passport_ids[passenger_id], None)
                for passenger_id in passenger_ids
            ],
            pet_ids=list(pet_ids),
            request_settings=dict(request_settings),
        ))
        summary.append({"from": departure, "to": arrival, "departureLocalTime": date[:16]})
        typed_routes.append((departure, arrival))

    payload = _build_trip_book_body(
        legs=legs,
        dog_form_submitted=dog_form,
        baggage=baggage_items,
        share_settings=_build_share_settings(
            special_requests=(special_requests or "").strip(),
            open_to_share=open_flag,
            network_type=network_type,
            group_ids=group_ids,
            seats=share_seats,
            pets_allowed=pets_flag,
            children_allowed=children_flag,
            cost_percentage=int(percentage),
        ),
    )
    route_check = _booking_route_check(payload, typed_routes, us_touching, international)
    _require_international_travel_documents(payload, route_check, read_passenger)
    _apply_booking_destination_address(payload, destination, route_check)

    if dry_run:
        _out({
            "dry_run": True,
            "method": "POST",
            "path": "/trip/book",
            "legs": summary,
            "passengers": passenger_ids,
            "payload": payload,
            "routeCheck": route_check,
            **({"shareEligibility": share_eligibility} if share_eligibility else {}),
            "message": "Would POST /trip/book exactly once; no read-back would follow.",
        }, fmt, compact)
        return

    data = api_post(auth(), "/trip/book", payload)
    _out(data, fmt, compact)


@booking_app.command("cancel")
def booking_cancel(
    leg_id: str = typer.Option(..., "--leg-id", help="Booked-leg UUID"),
    reason: str = typer.Option(..., "--reason", help="Cancellation reason (required by API)"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before the single live cancellation request."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show payload without submitting"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Cancel one leg using Android's exact POST /cancel-own contract.

    Android 6.1.4 sends only legId and reason. The CLI intentionally does not
    loop over a trip or issue a multi-leg cancellation.
    """
    reason = reason.strip()
    if not reason:
        _die("--reason must not be empty.", EXIT_VALIDATION)
    payload = {"legId": leg_id, "reason": reason}

    if dry_run:
        _out({"dry_run": True, "payload": payload, "message": "Would POST /cancel-own exactly once"}, fmt)
        return
    if not confirm:
        _die("--confirm required to cancel a leg.", EXIT_VALIDATION)

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
    _die("Leg response has no complete passenger list; no write sent.", EXIT_ERROR)


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
        # Android 6.1.4's LegPassengerUpdate.toJson (0x801778) has exactly
        # id, customsDeclarationId, destinationAddress and passport; nothing
        # else from the fetched record (such as passportIds) is copied.
        for key in ("customsDeclarationId", "destinationAddress", "passport"):
            value = row.get(key, nested.get(key))
            if value not in (None, "", [], {}):
                payload[key] = _leg_destination_address_update(value) if key == "destinationAddress" else value
        # Detail responses expose the selection as selectedPassportId rather
        # than passport:{id}. Keep it when changing another passenger field.
        if "passport" not in payload and row.get("selectedPassportId"):
            payload["passport"] = {"id": row["selectedPassportId"]}
    return payload


def _audit_leg_travel_info(leg: dict[str, Any]) -> dict[str, Any]:
    """Inspect the actual trip profiles, which may differ from saved profiles."""
    passengers = []
    for row in _leg_passenger_rows(leg):
        if not isinstance(row, dict):
            _die("Cannot audit an incomplete leg-passenger record.", EXIT_ERROR)
        records = row.get("passports") or []
        records = [p for p in records if isinstance(p, dict)] if isinstance(records, list) else []
        selected = row.get("selectedPassportId") or (row.get("passport") or {}).get("id")
        issues = []
        if not _saved_passenger_id(row):
            issues.append("missingPassengerId")
        if not records:
            issues.append("noPassportOnTripProfile")
        passport = next((p for p in records if p.get("id") == selected), None) if selected else next(iter(records), None)
        if selected and passport is None:
            issues.append("selectedPassportNotOnTripProfile")
        if passport is not None:
            if not passport.get("passportNumber"):
                issues.append("missingPassportNumber")
            if not _passport_scan_attached(passport):
                issues.append("missingPassportScan")
        address = row.get("destinationAddress")
        address_complete = isinstance(address, dict) and all(address.get(k) for k in ("street", "city", "state", "zip"))
        passengers.append({
            "name": _passenger_name(row), "passengerId": _saved_passenger_id(row),
            "legPassengerId": _leg_passenger_id(row), "selectedPassportId": selected,
            "passportIds": [p.get("id") for p in records],
            "passportNumberEnding": str(passport.get("passportNumber", ""))[-4:] if passport else None,
            "scanAttached": _passport_scan_attached(passport) if passport else False,
            "destinationAddressComplete": address_complete,
            "issues": issues,
        })
    return {
        "legId": leg.get("id"), "bookingId": leg.get("bookingId"), "passengers": passengers,
        "customsLinkId": leg.get("customsDeclarationId"),
        "message": "Trip-profile IDs can differ from Saved Passengers. Use these passenger IDs for this leg. "
                   "A scan and matching IDs do not verify the printed passport data. "
                   "Use customs link-get to check customsDeclarationAlreadySubmitted for each passenger.",
    }


@leg_app.command("audit-travel-info")
def leg_audit_travel_info(
    leg_id: str = typer.Option(..., "--leg-id"),
    probe: bool = typer.Option(False, "--probe/--no-probe"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Check trip-profile passport links, scans and addresses with one guarded leg read."""
    _guard_booking_probe(probe)
    token = get_api_token(username, password)
    leg = _response_data(api_get(token, f"/my-leg/{leg_id}"))
    if not isinstance(leg, dict):
        _die("Unexpected leg response.", EXIT_ERROR)
    audit = _audit_leg_travel_info(leg)
    link_id = leg.get("customsDeclarationId")
    status = _customs_submission_status(api_get(token, f"/canadian-customs-declaration-link/{link_id}"), leg_id) if link_id else None
    by_id = {p["legPassengerId"]: p for p in status["passengers"]} if status else {}
    for passenger in audit["passengers"]:
        submitted = by_id.get(passenger["legPassengerId"], {})
        passenger["customsSubmissionStatus"] = submitted.get("submissionStatus", "unknown")
        passenger["customsDeclarationAlreadySubmitted"] = submitted.get("customsDeclarationAlreadySubmitted")
    _out(audit, fmt, compact)


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

    The command performs one GET /my-leg/{id} before the PATCH, preserves
    every current passenger unless explicitly removed, sends saved passenger
    UUIDs rather than legPassenger IDs, performs one PATCH, and never reads back.
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
    # Owner reads use /my-leg; /leg is the write route, not its GET counterpart.
    leg = _response_data(api_get(token, f"/my-leg/{leg_id}"))
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


def _build_request_settings_update(
    *,
    catering: bool | None,
    catering_request: str | None,
    ground_transportation: bool | None,
    ground_transportation_type: str | None,
    ground_transportation_method: str | None,
    pickup_address: dict[str, str] | None,
    dropoff_address: dict[str, str] | None,
    arrival_method: str | None,
    arrival_pickup_address: dict[str, str] | None,
    arrival_dropoff_address: dict[str, str] | None,
    note: str | None,
) -> dict[str, Any]:
    """Android LegRequestSettingsUpdate.toJson (leg_update models) with nulls removed, in key order."""
    fields = (
        ("cateringRequired", catering),
        ("cateringRequest", catering_request),
        ("groundTransportationRequired", ground_transportation),
        ("groundTransportationType", ground_transportation_type),
        ("groundTransportationMethod", ground_transportation_method),
        ("groundTransportationPickUpAddress", pickup_address),
        ("groundTransportationDropOffAddress", dropoff_address),
        ("arrivalGroundTransportationMethod", arrival_method),
        ("arrivalGroundTransportationPickUpAddress", arrival_pickup_address),
        ("arrivalGroundTransportationDropOffAddress", arrival_dropoff_address),
        ("note", note),
    )
    return {key: value for key, value in fields if value not in (None, "", {})}


def _leg_destination_address_update(address: dict[str, str]) -> dict[str, str]:
    """Android LegDestinationAddressUpdate.toJson key order: street, street2, city, state, zip (nulls removed)."""
    return {key: address[key] for key in ("street", "street2", "city", "state", "zip") if address.get(key)}


def _build_leg_update_options(
    *,
    number_of_seats: int | None,
    passengers: list[dict[str, Any]] | None,
    pet_ids: list[str] | None,
    baggage: list[dict[str, Any]] | None,
    request_settings: dict[str, Any] | None,
    dog_form_submitted: bool | None,
) -> dict[str, Any]:
    """Android LegUpdateOptions.toJson (PATCH /leg/{id}/required-info) with nulls removed.

    Android's buildPartialLegUpdateOptions sends only the sections that
    changed, always with the complete passenger list. departureAirportId,
    arrivalAirportId, aircraftId, date, and shareSettings exist in the model
    but are not part of the CLI's required-information form.
    """
    fields = (
        ("numberOfSeats", number_of_seats),
        ("passengers", passengers),
        ("petIds", pet_ids),
        ("baggage", baggage),
        ("requestSettings", request_settings),
        ("dogFormSubmitted", dog_form_submitted),
    )
    return {key: value for key, value in fields if value is not None}


@leg_app.command("update-required-info")
def leg_update_required_info(
    leg_id: str = typer.Option(..., "--leg-id", help="Booked-leg UUID"),
    passport: Optional[list[str]] = typer.Option(None, "--passport", help="PASSENGER_ID=PASSPORT_ID, repeatable."),
    customs: Optional[list[str]] = typer.Option(None, "--customs", help="PASSENGER_ID=CUSTOMS_DECLARATION_ID, repeatable."),
    destination_street: Optional[str] = typer.Option(None, "--destination-street", help="Destination for every passenger on the leg."),
    destination_street2: Optional[str] = typer.Option(None, "--destination-street2", help="Suite / apartment / unit."),
    destination_city: Optional[str] = typer.Option(None, "--destination-city"),
    destination_state: Optional[str] = typer.Option(None, "--destination-state"),
    destination_zip: Optional[str] = typer.Option(None, "--destination-zip"),
    seats: Optional[int] = typer.Option(None, "--seats", min=1, help="Seats reserved on the leg."),
    pets: Optional[str] = typer.Option(None, "--pets", help="Comma-separated pet IDs (replaces the leg's pets)."),
    baggage: Optional[list[str]] = typer.Option(None, "--baggage", help="NAME=QUANTITY, repeatable, or \"none\" (replaces the leg's baggage)."),
    dog_form_submitted: Optional[str] = typer.Option(None, "--dog-form-submitted", help="yes | no"),
    catering: Optional[str] = typer.Option(None, "--catering", help="yes | no"),
    catering_request: Optional[str] = typer.Option(None, "--catering-request"),
    ground_transportation: Optional[str] = typer.Option(None, "--ground-transportation", help="yes | no"),
    ground_transportation_when: Optional[str] = typer.Option(None, "--ground-transportation-when", help="departure | arrival | both"),
    ground_transportation_method: Optional[str] = typer.Option(None, "--ground-transportation-method", help=_GROUND_METHOD_HELP),
    ground_pickup_address: Optional[str] = typer.Option(None, "--ground-pickup-address", help=_ADDRESS_TEXT_HELP),
    ground_dropoff_address: Optional[str] = typer.Option(None, "--ground-dropoff-address", help=_ADDRESS_TEXT_HELP),
    arrival_ground_method: Optional[str] = typer.Option(None, "--arrival-ground-method", help=_GROUND_METHOD_HELP),
    arrival_pickup_address: Optional[str] = typer.Option(None, "--arrival-pickup-address", help=_ADDRESS_TEXT_HELP),
    arrival_dropoff_address: Optional[str] = typer.Option(None, "--arrival-dropoff-address", help=_ADDRESS_TEXT_HELP),
    note: Optional[str] = typer.Option(None, "--note", help="Note to the flight concierge."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before the single PATCH"),
    probe: bool = typer.Option(False, "--probe/--no-probe", help="Override recent-booking-write cooldown"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Complete a booked leg's required information (passports, customs, destination, pets, catering, ground transportation).

    Passenger changes are merged onto the leg's complete current passenger
    list after one GET /my-leg/{id} (Android sends every passenger). Only the
    sections you answer are sent, in one PATCH, with no read-back.
    """
    passport_by_passenger = _key_value_pairs(passport, "--passport")
    customs_by_passenger = _key_value_pairs(customs, "--customs")
    destination = _address_payload(
        destination_street, destination_street2, destination_city, destination_state, destination_zip,
        "--destination",
    )
    request_settings: dict[str, Any] | None = None
    request_options = (
        catering, catering_request, ground_transportation, ground_transportation_when,
        ground_transportation_method, ground_pickup_address, ground_dropoff_address,
        arrival_ground_method, arrival_pickup_address, arrival_dropoff_address, note,
    )
    if any(value is not None for value in request_options):
        request_settings = _build_request_settings_update(
            catering=_optional_yes_no(catering, "--catering"),
            catering_request=(catering_request or "").strip() or None,
            ground_transportation=_optional_yes_no(ground_transportation, "--ground-transportation"),
            ground_transportation_type=_optional_enum(ground_transportation_when, "--ground-transportation-when", _GROUND_TRANSPORTATION_TYPES),
            ground_transportation_method=_optional_enum(ground_transportation_method, "--ground-transportation-method", _GROUND_TRANSPORTATION_METHODS),
            pickup_address=_address_text(ground_pickup_address, "--ground-pickup-address"),
            dropoff_address=_address_text(ground_dropoff_address, "--ground-dropoff-address"),
            arrival_method=_optional_enum(arrival_ground_method, "--arrival-ground-method", _GROUND_TRANSPORTATION_METHODS),
            arrival_pickup_address=_address_text(arrival_pickup_address, "--arrival-pickup-address"),
            arrival_dropoff_address=_address_text(arrival_dropoff_address, "--arrival-dropoff-address"),
            note=(note or "").strip() or None,
        ) or None
    pet_ids = _csv(pets, "--pets") if pets is not None else None
    baggage_items = _baggage_items(baggage, required=False) if baggage else None
    dog_form = _optional_yes_no(dog_form_submitted, "--dog-form-submitted")

    passenger_changes = bool(passport_by_passenger or customs_by_passenger or destination)
    if not passenger_changes and not any(
        value is not None for value in (seats, pet_ids, baggage_items, request_settings, dog_form)
    ):
        _die("Nothing to update: answer at least one required-information option.", EXIT_VALIDATION)

    plan: dict[str, Any] | None = None
    merged: list[dict[str, Any]] | None = None
    token: str | None = None
    if passenger_changes:
        _guard_booking_probe(probe)
        token = get_api_token(username, password)
        leg = _response_data(api_get(token, f"/my-leg/{leg_id}"))
        if not isinstance(leg, dict):
            _die(f"Unexpected leg response for {leg_id}; no PATCH sent.", EXIT_ERROR)
        current: list[dict[str, Any]] = []
        labels: dict[str, str] = {}
        unresolved: list[str] = []
        for row in _leg_passenger_rows(leg):
            item = _leg_passenger_payload(row)
            if item is None:
                unresolved.append(_passenger_name(row))
                continue
            if item["id"] not in labels:
                current.append(item)
                labels[item["id"]] = _passenger_name(row)
        if unresolved:
            _die(
                "Could not resolve saved passenger UUIDs for: " + ", ".join(unresolved)
                + ". No PATCH sent.",
                EXIT_VALIDATION,
            )
        current_ids = {item["id"] for item in current}
        unknown_ids = sorted((set(passport_by_passenger) | set(customs_by_passenger)) - current_ids)
        if unknown_ids:
            _die(
                "Required-info updates may not add/drop passengers; unknown saved IDs: "
                + ", ".join(unknown_ids),
                EXIT_VALIDATION,
            )
        merged = []
        updated_ids: list[str] = []
        for current_item in current:
            passenger_id = current_item["id"]
            item = dict(current_item)
            changed = False
            if passenger_id in customs_by_passenger:
                item["customsDeclarationId"] = customs_by_passenger[passenger_id]
                changed = True
            if destination is not None:
                item["destinationAddress"] = _leg_destination_address_update(destination)
                changed = True
            if passenger_id in passport_by_passenger:
                item["passport"] = {"id": passport_by_passenger[passenger_id]}
                changed = True
            if changed:
                updated_ids.append(passenger_id)
            merged.append(item)
        plan = {
            "kept": [
                {"id": item["id"], "name": labels.get(item["id"], item["id"])}
                for item in current if item["id"] not in updated_ids
            ],
            "updated": [
                {"id": item["id"], "name": labels.get(item["id"], item["id"])}
                for item in current if item["id"] in updated_ids
            ],
            "dropped": [],
        }

    options = _build_leg_update_options(
        number_of_seats=seats,
        passengers=merged,
        pet_ids=pet_ids,
        baggage=baggage_items,
        request_settings=request_settings,
        dog_form_submitted=dog_form,
    )
    path = f"/leg/{leg_id}/required-info"
    payload = {"options": options}
    if dry_run:
        _out({
            "dry_run": True,
            "method": "PATCH",
            "path": path,
            "plan": plan,
            "payload": payload,
            "message": "No PATCH was sent; at most one pre-write GET was made.",
        }, fmt, compact)
        return
    if not confirm:
        _die("--confirm required for the single required-info PATCH.", EXIT_VALIDATION)
    token = token or get_api_token(username, password)
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
    limit: int = typer.Option(25, "--limit", "-n", min=1, max=200, help="Max results"),
    offset: int = typer.Option(0, "--offset", min=0, help="Continue from page.nextOffset"),
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
        "page": {"limit": limit, "offset": offset},
        "filter": filt,
    })
    items = resp.get("data", {}).get("items", [])
    _out(items, fmt, page=_collection_page(resp, offset, limit))


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


# Android 6.1.4 FeedbackSurveyCreateRequestModel (POST /feedback/create). Wire
# values come from the app's enum objects (blutter objs.txt off_10); note the
# app's own misspelling DISSASTIFIED, which the API expects verbatim.
_FEEDBACK_SATISFACTION = ("VERY_SATISFIED", "SATISFIED", "NEUTRAL", "DISSASTIFIED", "VERY_DISSATISFIED")
_FEEDBACK_CONDITION = ("EXCELLENT", "GOOD", "FAIR", "POOR", "VERY_POOR")
_FEEDBACK_CREW = ("EXCEPTIONAL", "VERY_GOOD", "SATISFACTORY", "NEEDS_IMPROVEMENT", "POOR")
_FEEDBACK_SPELLING = {"DISSATISFIED": "DISSASTIFIED"}


def _feedback_choice(value: str, option: str, allowed: tuple[str, ...]) -> str:
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    normalized = _FEEDBACK_SPELLING.get(normalized, normalized)
    if normalized not in allowed:
        choices = ", ".join(
            {"DISSASTIFIED": "dissatisfied"}.get(item, item.lower().replace("_", "-")) for item in allowed
        )
        _die(f"{option} must be one of: {choices}", EXIT_VALIDATION)
    return normalized


def _build_feedback_body(
    *,
    leg_id: str,
    quality: str,
    cleanliness: str,
    professionalism: str,
    fbo: str,
    catering: str,
    contact: bool | None,
    additional_feedback: str,
) -> dict[str, Any]:
    """Android FeedbackSurveyCreateRequestModel, in key order.

    contact is sent only when the yes/no question was answered (the model
    skips the key when it is null). score is Android's mapSatisfaction: each
    answer scores 5 (best) down to 1 (worst); the five scores are summed and
    divided by 5.0.
    """
    ratings = (
        (quality, _FEEDBACK_SATISFACTION),
        (cleanliness, _FEEDBACK_CONDITION),
        (professionalism, _FEEDBACK_CREW),
        (fbo, _FEEDBACK_SATISFACTION),
        (catering, _FEEDBACK_CONDITION),
    )
    score = sum(5 - allowed.index(value) for value, allowed in ratings) / 5.0
    body: dict[str, Any] = {
        "legId": leg_id,
        "quality": quality,
        "cleanliness": cleanliness,
        "professionalism": professionalism,
        "fbo": fbo,
        "catering": catering,
    }
    if contact is not None:
        body["contact"] = contact
    body["additionalFeedback"] = additional_feedback
    body["score"] = score
    return body


@feedback_app.command("submit")
def feedback_submit(
    leg_id: str = typer.Option(..., "--leg-id", help="Completed-leg UUID"),
    snacks_and_amenities: str = typer.Option(..., "--snacks-and-amenities", help="In-flight snacks, beverages, amenities: very-satisfied | satisfied | neutral | dissatisfied | very-dissatisfied"),
    aircraft_condition: str = typer.Option(..., "--aircraft-condition", help="excellent | good | fair | poor | very-poor"),
    crew: str = typer.Option(..., "--crew", help="Flight crew professionalism: exceptional | very-good | satisfactory | needs-improvement | poor"),
    fbo: str = typer.Option(..., "--fbo", help="FBO facilities: very-satisfied | satisfied | neutral | dissatisfied | very-dissatisfied"),
    catering_and_transport: str = typer.Option(..., "--catering-and-transport", help="Catering / ground transportation: excellent | good | fair | poor | very-poor"),
    contact_me: Optional[str] = typer.Option(None, "--contact-me", help="yes | no — be contacted about these answers (omitted when unanswered, as in the app)"),
    comments: Optional[str] = typer.Option(None, "--comments", help="Additional feedback"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Rate a completed flight, question by question, as in the app (POST /feedback/create)."""
    payload = _build_feedback_body(
        leg_id=leg_id,
        quality=_feedback_choice(snacks_and_amenities, "--snacks-and-amenities", _FEEDBACK_SATISFACTION),
        cleanliness=_feedback_choice(aircraft_condition, "--aircraft-condition", _FEEDBACK_CONDITION),
        professionalism=_feedback_choice(crew, "--crew", _FEEDBACK_CREW),
        fbo=_feedback_choice(fbo, "--fbo", _FEEDBACK_SATISFACTION),
        catering=_feedback_choice(catering_and_transport, "--catering-and-transport", _FEEDBACK_CONDITION),
        contact=_yes_no(contact_me, "--contact-me") if contact_me is not None else None,
        additional_feedback=(comments or "").strip(),
    )
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": "/feedback/create", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/feedback/create", payload), fmt, compact)


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
    response = api_post(token, "/my-accounts")
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
        "sort": [], "page": {"limit": 100, "offset": 0},
        "filter": {"name": icao},
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
    account_aircraft_id: str | None,
    hours: float | None,
    action: str | None,
) -> dict[str, Any]:
    """GET /hour-exchange/estimate query: accountAircraftId, hours, type (BUY|SELL)."""
    if hours is None:
        _die("--hours is required.", EXIT_VALIDATION)
    if not action:
        _die("--type must be BUY or SELL.", EXIT_VALIDATION)
    return {
        "accountAircraftId": _get_account_aircraft_id(token, account_aircraft_id),
        "hours": hours,
        "type": _enum(action, "--type", ("BUY", "SELL")),
    }


# ---------------------------------------------------------------------------
# quote (api.airsprint.com)
# ---------------------------------------------------------------------------


@quote_app.command("flight")
def quote_flight(
    departure: str = typer.Option(..., "--from", help="Departure airport ICAO code (e.g. CYQB)"),
    arrival: str = typer.Option(..., "--to", help="Arrival airport ICAO code (e.g. KTEB)"),
    date: str = typer.Option(..., "--date", help="Departure date/time, local to --timezone (2026-04-15T10:00 or 2026-04-15)"),
    return_date: Optional[str] = typer.Option(None, "--return-date", help="Add a return leg on this local date/time"),
    pax: int = typer.Option(1, "--pax", min=1, help="Passengers on each leg"),
    aircraft: Optional[str] = typer.Option(None, "--aircraft-id", help="Aircraft UUID from `quote aircraft`; defaults to your account aircraft"),
    timezone: Optional[str] = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Price a one-way or round trip with AirSprint's real quote engine.

    Local dates need --timezone / AIRSPRINT_TIMEZONE; dates ending in Z or an
    offset are used as-is.
    """
    _use_timezone(timezone)
    token = get_api_token(username, password)
    payload = _build_flight_quote_body(
        token,
        departure=departure,
        arrival=arrival,
        date=date,
        return_date=return_date,
        pax=pax,
        aircraft_id=aircraft,
        timezone=timezone,
    )
    try:
        resp = api_post(token, "/flight-quote", payload)
        _out(resp.get("data", resp), fmt)
    except RuntimeError as e:
        _die(str(e), EXIT_ERROR)


def _build_flight_quote_body(
    token: str,
    *,
    departure: str,
    arrival: str,
    date: str,
    return_date: str | None,
    pax: int,
    aircraft_id: str | None,
    timezone: str | None,
) -> dict[str, Any]:
    """Android 6.1.4 /flight-quote legs: aircraftId, departureAirportId, arrivalAirportId, departureDateUTC, pax."""
    dep_id = _resolve_airport(token, departure)
    arr_id = _resolve_airport(token, arrival)
    ac_id = aircraft_id or _get_default_aircraft(token)
    legs = [{
        "aircraftId": ac_id,
        "departureAirportId": dep_id,
        "arrivalAirportId": arr_id,
        "departureDateUTC": _parse_local_dt(date, timezone),
        "pax": pax,
    }]
    if return_date:
        legs.append({
            "aircraftId": ac_id,
            "departureAirportId": arr_id,
            "arrivalAirportId": dep_id,
            "departureDateUTC": _parse_local_dt(return_date, timezone),
            "pax": pax,
        })
    return {"legs": legs}


@quote_app.command("roundtrip")
def quote_roundtrip(
    departure: str = typer.Option(..., "--from", help="Departure ICAO (e.g. CYQB)"),
    arrival: str = typer.Option(..., "--to", help="Arrival ICAO (e.g. KTEB)"),
    out_date: str = typer.Option(..., "--out", help="Outbound date/time (local; needs --tz)"),
    return_date: str = typer.Option(..., "--return", help="Return date/time (local; needs --tz)"),
    pax: int = typer.Option(1, "--pax", min=1, help="Passenger count sent on both legs"),
    timezone: Optional[str] = Timezone,
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Quote a round-trip in a single call (outbound + return).

    Compound version of `quote flight`: resolves airports once, fetches both legs,
    and returns combined pricing.
    """
    _use_timezone(timezone)
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
                "pax": pax,
            },
            {
                "aircraftId": ac_id,
                "departureAirportId": arr_id,
                "arrivalAirportId": dep_id,
                "departureDateUTC": ret_utc,
                "pax": pax,
            },
        ]
    }
    try:
        resp = api_post(token, "/flight-quote", payload)
        _out(resp.get("data", resp), fmt)
    except RuntimeError as e:
        _die(str(e), EXIT_ERROR)


_MISC_COST_AIRCRAFT = ("LEGACY_450", "CITATION_CJ3_PLUS", "CITATION_CJ2_PLUS")


def _build_misc_cost_body(
    *,
    aircraft: str,
    quote_price: float,
    service_area: str,
    service_location: str | None,
    actual_flight_minutes: int | None,
    ground_transportation_method: str | None,
    owned_aircraft: str | None,
    flown_aircraft: str,
) -> dict[str, Any]:
    """Android toTripMiscCostEstimateRequest (POST /trip/misc-cost-estimate) for one leg.

    Keys in Android's order: aircraft and quotePrice and serviceArea (always,
    serviceArea may be ""), then serviceLocation, actualFlightMinutes and
    groundTransportation ({"method", "applyServiceCharge": true}) only when
    set, then interchange {"ownedAircraft", "flownAircraft"} only when the
    owned aircraft type is known (the app also requires a recognised flown
    type; the CLI validates both against the same enum).
    """
    leg: dict[str, Any] = {
        "aircraft": aircraft,
        "quotePrice": float(quote_price),
        "serviceArea": service_area,
    }
    if service_location:
        leg["serviceLocation"] = service_location
    if actual_flight_minutes is not None:
        leg["actualFlightMinutes"] = int(actual_flight_minutes)
    if ground_transportation_method:
        leg["groundTransportation"] = {"method": ground_transportation_method, "applyServiceCharge": True}
    if owned_aircraft:
        leg["interchange"] = {"ownedAircraft": owned_aircraft, "flownAircraft": flown_aircraft}
    return {"legs": [leg]}


@quote_app.command("cost")
def quote_cost(
    aircraft: str = typer.Option(..., "--aircraft", help="legacy-450 | citation-cj3-plus | citation-cj2-plus"),
    quote_price: float = typer.Option(..., "--quote-price", min=0, help="Quoted flight price for the leg"),
    flight_minutes: Optional[int] = typer.Option(None, "--flight-minutes", min=1, help="Flight time in minutes, when known (left out otherwise, as in the app)"),
    service_area: Optional[str] = typer.Option(None, "--service-area", help="Airport service area label, if the airport has one"),
    service_location: Optional[str] = typer.Option(None, "--service-location", help="Airport service location label, if any"),
    ground_transportation_method: Optional[str] = typer.Option(None, "--ground-transportation-method", help=_GROUND_METHOD_HELP),
    owned_aircraft: Optional[str] = typer.Option(None, "--owned-aircraft", help="Aircraft type you own; answer it to price an interchange (flying a different type)"),
    flown_aircraft: Optional[str] = typer.Option(None, "--flown-aircraft", help="Aircraft type actually flown for the interchange (default: --aircraft)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Estimate one leg's miscellaneous costs (catering, ground transport, surcharges) — POST /trip/misc-cost-estimate.

    Run it once per leg for multi-leg trips.
    """
    aircraft_type = _enum(aircraft, "--aircraft", _MISC_COST_AIRCRAFT)
    if flown_aircraft and not owned_aircraft:
        _die("--flown-aircraft describes an interchange; also answer --owned-aircraft. Nothing was sent.", EXIT_VALIDATION)
    payload = _build_misc_cost_body(
        aircraft=aircraft_type,
        quote_price=quote_price,
        service_area=(service_area or "").strip(),
        service_location=(service_location or "").strip() or None,
        actual_flight_minutes=flight_minutes,
        ground_transportation_method=_optional_enum(
            ground_transportation_method, "--ground-transportation-method", _GROUND_TRANSPORTATION_METHODS,
        ),
        owned_aircraft=_enum(owned_aircraft, "--owned-aircraft", _MISC_COST_AIRCRAFT) if owned_aircraft else None,
        flown_aircraft=_enum(flown_aircraft, "--flown-aircraft", _MISC_COST_AIRCRAFT) if flown_aircraft else aircraft_type,
    )
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": "/trip/misc-cost-estimate", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    try:
        resp = api_post(token, "/trip/misc-cost-estimate", payload)
        _out(resp.get("data", resp), fmt, compact)
    except RuntimeError as e:
        _die(str(e), EXIT_ERROR)


@quote_app.command("hours-exchange")
def quote_hours_exchange(
    hours: Optional[float] = typer.Option(None, "--hours", min=0.01),
    action: Optional[str] = typer.Option(None, "--type", help="BUY or SELL"),
    account_aircraft_id: Optional[str] = typer.Option(None, "--account-aircraft-id", help="Defaults automatically when the account has one aircraft"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
):
    """Estimate an Hours Exchange purchase or sale (GET /hour-exchange/estimate).

    The API expects query parameters, not a JSON POST body. When the account has
    one aircraft, its account-aircraft ID is selected automatically.
    """
    token = get_api_token(username, password)
    query = _hours_estimate_query(token, account_aircraft_id, hours, action)
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
        # Android 6.1.4 AirportSearchFilterModel calls this field `name`.
        filt["name"] = query
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
    """List all AirSprint aircraft types with UUIDs (for `--aircraft-id` on `quote flight` / `quote roundtrip`).

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


@quote_app.command("aircraft-get")
def quote_aircraft_get(
    aircraft_id: str = typer.Option(..., "--id", help="Aircraft type UUID"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Get one aircraft type (GET /aircraft/{id}), as used by Android."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/aircraft/{aircraft_id}"), fmt, compact)


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
    _use_timezone(timezone)
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
# raw — maintainer-only escape hatches (hidden; not part of the agent surface)
# ---------------------------------------------------------------------------

AllowRaw = typer.Option(
    False,
    "--allow-raw",
    help="Required. Raw requests bypass the CLI's typed forms and safety checks; maintainers only.",
)


def _require_raw_access(allow_raw: bool) -> None:
    if not allow_raw:
        _die(
            "Raw API access is reserved for maintainers. Use the typed command for this "
            "action (see --skill), or pass --allow-raw deliberately.",
            EXIT_VALIDATION,
        )


@raw_app.command("api-get")
def raw_api_get(
    allow_raw: bool = AllowRaw,
    path: str = typer.Option(..., "--path", help='Path on api.airsprint.com (e.g. "/my-saved-airports/")'),
    probe: bool = typer.Option(False, "--probe/--no-probe", help="Override recent-booking-write cooldown"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """GET against api.airsprint.com."""
    _require_raw_access(allow_raw)
    if path.startswith(("/trip/", "/leg/", "/my-flight/", "/my-leg/")):
        _guard_booking_probe(probe)
    token = get_api_token(username, password)
    _out(api_get(token, path), fmt, compact)


@raw_app.command("api-post")
def raw_api_post(
    allow_raw: bool = AllowRaw,
    path: str = typer.Option(..., "--path", help="Path on api.airsprint.com"),
    body: str = typer.Option("{}", "--body", help="JSON body (default empty)"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """POST against api.airsprint.com."""
    _require_raw_access(allow_raw)
    token = get_api_token(username, password)
    _out(api_post(token, path, _parse_json(body)), fmt, compact)


@raw_app.command("api-patch")
def raw_api_patch(
    allow_raw: bool = AllowRaw,
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
    _require_raw_access(allow_raw)
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
    allow_raw: bool = AllowRaw,
    path: str = typer.Option(..., "--path", help="Path on api.airsprint.com"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before sending DELETE"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the request without sending it"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """DELETE against api.airsprint.com. Requires --confirm or --dry-run."""
    _require_raw_access(allow_raw)
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
    resp = api_post(token, "/my-account-users", {
        "sort": [],
        "page": {"limit": 100, "offset": 0},
    })
    _out(resp.get("data", resp), fmt, compact)



_ACCOUNT_ROLES = (
    "ACCOUNT_OWNER", "FULL_ACCESS", "INDIVIDUAL_ACCESS", "EMPTY_LEG_ACCESS", "PASSENGER_ACCESS",
)


def _resolve_role_id(token: str, role: str) -> str:
    """Android getRoleIdByName(): one /account-user-role lookup filtered by name."""
    response = api_post(token, "/account-user-role", {
        "filter": {"name": role},
        "page": {"limit": 2, "offset": 0},
    })
    data = _response_data(response)
    items = data.get("items") if isinstance(data, dict) else data
    for item in items or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            return item["id"]
    _die(f"Role {role} was not found on this account.", EXIT_NOT_FOUND)


def _resolve_invite_account_id(token: str, account_id: str | None) -> str:
    if account_id:
        return account_id
    account_ids = _get_account_ids(token)
    if len(account_ids) == 1:
        return account_ids[0]
    if not account_ids:
        _die("No accounts found", EXIT_ERROR)
    _die("Several accounts found; pass --account-id (see `user accounts`).", EXIT_VALIDATION)


def _build_account_invite_body(
    *,
    first_name: str,
    last_name: str,
    email: str,
    role_id: str,
    account_id: str,
    passenger_id: str | None,
) -> dict[str, Any]:
    """Android inviteNewAccountUser()/inviteAccountUserFromPassenger() bodies."""
    new_user = {
        "firstName": _required_text(first_name, "--first-name"),
        "lastName": _required_text(last_name, "--last-name"),
        "email": _required_text(email, "--email"),
        "accountUserRoleId": role_id,
        "accountId": account_id,
    }
    if passenger_id:
        return {"passengerId": passenger_id, "newUser": new_user}
    return {"newUser": new_user}


@account_app.command("invite")
def account_invite(
    first_name: str = typer.Option(..., "--first-name"),
    last_name: str = typer.Option(..., "--last-name"),
    email: str = typer.Option(..., "--email"),
    role: Optional[str] = typer.Option(
        None, "--role", help="Access level: " + ", ".join(_ACCOUNT_ROLES),
    ),
    role_id: Optional[str] = typer.Option(None, "--role-id", help="Role UUID from `account roles` (instead of --role)"),
    account_id: Optional[str] = typer.Option(None, "--account-id", help="Needed only when you own several accounts"),
    passenger_id: Optional[str] = typer.Option(
        None, "--passenger-id", help="Invite an existing saved passenger (their passenger UUID)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Invite someone to use the account (POST /account-user/invite)."""
    if bool(role) == bool(role_id):
        _die("Pass exactly one of --role or --role-id.", EXIT_VALIDATION)
    role_name = _optional_enum(role, "--role", _ACCOUNT_ROLES)
    token = get_api_token(username, password)
    resolved_role = role_id or _resolve_role_id(token, role_name or "")
    payload = _build_account_invite_body(
        first_name=first_name, last_name=last_name, email=email,
        role_id=resolved_role,
        account_id=_resolve_invite_account_id(token, account_id),
        passenger_id=passenger_id,
    )
    if dry_run:
        _out({"dry_run": True, "payload": payload, "endpoint": "/account-user/invite"}, fmt, compact)
        return
    _out(api_post(token, "/account-user/invite", payload), fmt, compact)


@account_app.command("user-update")
def account_user_update(
    user_ids: str = typer.Option(..., "--ids", help="Comma-separated account-user UUIDs"),
    role_names: str = typer.Option(..., "--roles", help="Comma-separated Android role names"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Batch-update account roles using Android's exact options envelope."""
    payload = {
        "ids": _parse_ids(user_ids, "--ids"),
        "options": {"roleNames": _parse_ids(role_names, "--roles")},
    }
    if dry_run:
        _out({
            "dry_run": True,
            "method": "PATCH",
            "payload": payload,
            "endpoint": "/account-user/update",
        }, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to update account roles.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_patch(token, "/account-user/update", payload), fmt, compact)


@account_app.command("user-patch")
def account_user_patch(
    user_id: str = typer.Option(..., "--id", help="Account-user UUID from `account users`"),
    first_name: Optional[str] = typer.Option(None, "--first-name"),
    last_name: Optional[str] = typer.Option(None, "--last-name"),
    email: Optional[str] = typer.Option(None, "--email"),
    role_id: Optional[str] = typer.Option(None, "--role-id", help="Role UUID from `account roles`"),
    saved_airports: Optional[str] = typer.Option(
        None, "--saved-airports", help="Comma-separated airport UUIDs (replaces the list; empty string clears it)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Edit one account user's name, email, role, or saved airports."""
    options: dict[str, Any] = {}
    if first_name is not None:
        options["firstName"] = _required_text(first_name, "--first-name")
    if last_name is not None:
        options["lastName"] = _required_text(last_name, "--last-name")
    if email is not None:
        options["email"] = _required_text(email, "--email")
    if role_id is not None:
        options["accountUserRoleId"] = _required_text(role_id, "--role-id")
    if saved_airports is not None:
        options["savedAirportIds"] = _csv(saved_airports, "--saved-airports")
    if not options:
        _die("Pass at least one field to change (see --help).", EXIT_VALIDATION)
    path = f"/my-account-user/{user_id}"
    payload = {"id": user_id, "options": options}
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": path, "payload": payload}, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to patch account access.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    result = api_patch(token, path, payload)
    _out({"result": result, "message": "PATCH sent exactly once; no read-back performed."}, fmt, compact)


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
    resp = api_post(token, "/account-user-role", {
        "sort": [],
        "page": {"limit": 100, "offset": 0},
    })
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


SaveProfile = typer.Option(
    None, "--save-profile",
    help='Required. "yes" keeps the person in your Saved Passengers list; "no" creates '
         "them for this booking only (hidden from the list). Same question as the web form.",
)


@passenger_app.command("create")
def passenger_create(
    first_name: str = typer.Option(..., "--first-name", help="Legal first name"),
    last_name: str = typer.Option(..., "--last-name", help="Legal last name"),
    save_profile: Optional[str] = SaveProfile,
    middle_name: Optional[str] = typer.Option(None, "--middle-name", help="Legal middle name"),
    email: Optional[str] = typer.Option(None, "--email"),
    gender: str = typer.Option(..., "--gender", help="male | female | x (required, as in the app; used for weight and balance)"),
    category: str = typer.Option("ADULT", "--category", help="ADULT (12+) | CHILD (2-11) | INFANT (<2)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Add a person, same questions as the app's "Add New Person" form.

    --save-profile is required: "yes" = add to Saved Passengers, "no" = this
    booking only. Either way the result contains an `id` to use with
    `leg update-passengers --add` or `booking create --passengers`.
    Passports and addresses are attached afterwards with `passport create`
    and `address create`.
    """
    if save_profile is None:
        _die('--save-profile yes|no is required (the web form asks: "add this person to your Saved Passengers list?").', EXIT_VALIDATION)
    saved = _yes_no(save_profile, "--save-profile")
    payload = _build_passenger_create_body(
        first_name=first_name,
        middle_name=middle_name,
        last_name=last_name,
        email=email,
        gender=_gender(gender),
        category=_enum(category, "--category", _CATEGORIES),
        saved=saved,
    )
    if dry_run:
        _out({"dry_run": True, "endpoint": "/my-passenger/create",
              "saveProfile": saved, "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/my-passenger/create", payload), fmt, compact)


def _build_passenger_create_body(
    *,
    first_name: str,
    middle_name: str | None,
    last_name: str,
    email: str | None,
    gender: str,
    category: str,
    saved: bool,
) -> dict[str, Any]:
    """Android 6.1.4 NewPassengersRequestModel.toJson (0x6f4a90), in its key order.

    AddNewPersonController.submit (0x918028) trims every text and turns a
    blank middle name or email into null; toJson then writes middleName and
    picture as "" when null, keeps email null, hard-codes flightPreferences
    to "" (0x6f4ba4), adds `passports` only when the list is non-empty (the
    form always passes an empty one, 0x9182c8) and always sends
    `addresses: []` plus `isActive` (the "add to Saved Passengers?" toggle).
    """
    first, last = first_name.strip(), last_name.strip()
    if not first or not last:
        _die("--first-name and --last-name must not be blank.", EXIT_VALIDATION)
    return {
        "firstName": first,
        "middleName": (middle_name or "").strip(),
        "lastName": last,
        "email": (email or "").strip() or None,
        "gender": gender,
        "age": category,
        "flightPreferences": "",
        "picture": "",
        "addresses": [],
        "isActive": saved,
    }


_PASSENGER_PROFILE_KEYS = ("firstName", "middleName", "lastName", "email", "gender", "age", "flightPreferences")


def _passenger_record(response: Any) -> dict[str, Any]:
    data = _response_data(response)
    if isinstance(data, dict) and "firstName" not in data and isinstance(data.get("options"), dict):
        data = data["options"]
    return data if isinstance(data, dict) else {}


def _build_passenger_update_body(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Android PassengerPersonProfilePage._onSave (0x881fcc): the whole profile form, every time.

    The app never patches one field. After `isValid` (0x87de8c: first and
    last name non-blank after trim, gender set) it sends firstName,
    middleName, lastName, email, gender, age and flightPreferences in that
    order (0x88237c-0x8825e8), trimmed, with a blank middle name or
    preferences as "" and a blank email as null. `picture` is added only after
    a new avatar upload, which the CLI does not do; isActive is never sent.
    """
    merged: dict[str, Any] = {key: current.get(key) for key in _PASSENGER_PROFILE_KEYS}
    merged.update(changes)

    def text(key: str) -> str:
        value = merged.get(key)
        return value.strip() if isinstance(value, str) else ""

    first, last = text("firstName"), text("lastName")
    if not first or not last:
        _die("The profile needs a first and last name; pass --first-name / --last-name.", EXIT_VALIDATION)
    gender = merged.get("gender")
    if gender not in _GENDERS:
        _die("Gender is required, as in the app's profile form: pass --gender male|female|x.", EXIT_VALIDATION)
    age = merged.get("age")
    if age not in _CATEGORIES:
        _die("Category is required: pass --category ADULT|CHILD|INFANT.", EXIT_VALIDATION)
    return {
        "firstName": first,
        "middleName": text("middleName"),
        "lastName": last,
        "email": text("email") or None,
        "gender": gender,
        "age": age,
        "flightPreferences": text("flightPreferences"),
    }


@passenger_app.command("update")
def passenger_update(
    passenger_id: str = typer.Option(..., "--id", help="Passenger UUID"),
    first_name: Optional[str] = typer.Option(None, "--first-name"),
    middle_name: Optional[str] = typer.Option(None, "--middle-name", help='Pass "" to clear'),
    last_name: Optional[str] = typer.Option(None, "--last-name"),
    email: Optional[str] = typer.Option(None, "--email", help='Pass "" to clear'),
    gender: Optional[str] = typer.Option(None, "--gender", help="male | female | x"),
    category: Optional[str] = typer.Option(None, "--category", help="ADULT | CHILD | INFANT"),
    flight_preferences: Optional[str] = typer.Option(None, "--flight-preferences", help='Free text; pass "" to clear'),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Edit a saved person with the app's profile form (PATCH /my-passenger/{id}).

    Pass only what changes. Like the app, the CLI reads the current profile
    and sends the complete form back (first/middle/last name, email, gender,
    category, flight preferences), so `--dry-run` still performs that read.
    Passports are managed with `passport create` / `passport make-primary`,
    addresses with `address create`; the "saved passenger" choice is made
    once, at creation, and the app never changes it afterwards.
    """
    path = f"/my-passenger/{passenger_id}"
    changes: dict[str, Any] = {}
    if first_name is not None:
        changes["firstName"] = _required_text(first_name, "--first-name")
    if middle_name is not None:
        changes["middleName"] = middle_name.strip()
    if last_name is not None:
        changes["lastName"] = _required_text(last_name, "--last-name")
    if email is not None:
        changes["email"] = email.strip()
    if gender is not None:
        changes["gender"] = _gender(gender)
    if category is not None:
        changes["age"] = _enum(category, "--category", _CATEGORIES)
    if flight_preferences is not None:
        changes["flightPreferences"] = flight_preferences.strip()
    if not changes:
        _die("Pass at least one field to change (see --help).", EXIT_VALIDATION)
    token = get_api_token(username, password)
    current = _passenger_record(api_get(token, path))
    if not current:
        _die(f"Passenger {passenger_id} was not found; nothing was changed.", EXIT_ERROR)
    payload = {"options": _build_passenger_update_body(current, changes)}
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": path, "payload": payload, "changed": sorted(changes)}, fmt, compact)
        return
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



def _build_passport_create_body(
    *,
    passenger_id: str,
    passport_number: str,
    date_of_birth: str,
    nationality: str,
    issuing_authority: str,
    expiration_date: str,
    timezone: str | None,
) -> dict[str, Any]:
    """Android PassportCreateRequestModel.toJson(), keys in source order.

    ``image`` is hard-coded to "" by the app; the scan is attached afterwards
    through the document upload workflow. Dates are epoch milliseconds taken
    at device-local midnight.
    """
    return {
        "passengerId": _required_text(passenger_id, "--passenger-id"),
        "image": "",
        "passportNumber": _required_text(passport_number, "--passport-number").upper(),
        "dateOfBirth": _passport_epoch_ms(date_of_birth, "--date-of-birth", timezone),
        "nationality": _optional_country_code(nationality, "--nationality"),
        "issuingAuthority": _required_text(issuing_authority, "--issuing-authority"),
        "expirationDate": _passport_epoch_ms(expiration_date, "--expiration-date", timezone),
    }


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
    passenger_id: Optional[str] = typer.Option(None, "--passenger-id", help="Inspect one actual trip-profile UUID, including an unsaved guest"),
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
    if passenger_id:
        passengers = [_response_data(api_get(token, f"/my-passenger/{passenger_id}"))]
    else:
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
    passenger_id: str = typer.Option(..., "--passenger-id", help="Saved passenger UUID the passport belongs to"),
    passport_number: str = typer.Option(..., "--passport-number", help="Number printed on the passport"),
    date_of_birth: str = typer.Option(..., "--date-of-birth", help="YYYY-MM-DD as printed"),
    nationality: str = typer.Option(..., "--nationality", help="Two-letter country code, e.g. CA"),
    issuing_authority: str = typer.Option(..., "--issuing-authority", help="Authority/Autorité as printed, e.g. QUÉBEC"),
    expiration_date: str = typer.Option(..., "--expiration-date", help="YYYY-MM-DD as printed"),
    file_value: str = typer.Option(
        ...,
        "--file",
        help="Required passport photo/scan (JPEG, PNG, or PDF; maximum 20 MiB)",
    ),
    content_type: Optional[str] = typer.Option(
        None,
        "--content-type",
        help="MIME type; inferred from the filename by default",
    ),
    timezone: Optional[str] = Timezone,
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Required before creating and uploading the passport",
    ),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Add a passport to a saved passenger and attach its photo/scan.

    Dates are YYYY-MM-DD exactly as printed; pass --tz (or set
    AIRSPRINT_TIMEZONE) to the passenger's device timezone. The new passport is
    placed first so the app shows it. Each write is attempted exactly once.
    """
    _use_timezone(timezone)
    payload = _build_passport_create_body(
        passenger_id=passenger_id, passport_number=passport_number,
        date_of_birth=date_of_birth, nationality=nationality,
        issuing_authority=issuing_authority, expiration_date=expiration_date,
        timezone=timezone,
    )
    file_path, mime = _passport_scan_file(file_value, content_type)
    document_base = {"passportId": "<created passport id>"}
    document_init = {
        **document_base,
        "fileName": file_path.name,
        "contentType": mime,
        "maxFileSizeBytes": ANDROID_DOCUMENT_MAX_BYTES,
    }
    if dry_run:
        _out({
            "dry_run": True,
            "steps": [
                {"method": "POST", "path": "/my-passport/create", "payload": payload},
                {
                    "method": "POST",
                    "path": "/my-passport/document/upload-init",
                    "payload": document_init,
                },
                {
                    "method": "POST",
                    "path": "presignedUpload.url",
                    "multipartFile": file_path.name,
                },
                {
                    "method": "POST",
                    "path": "/my-passport/document/attach",
                    "payload": {
                        **document_base,
                        "fileName": file_path.name,
                        "contentType": mime,
                        "storagePath": "<from upload-init>",
                    },
                },
                {
                    "method": "PATCH",
                    "path": f"/my-passenger/{passenger_id}",
                    "payload": {"options": {"passportIds": ["<created passport id>", "<existing ids>"]}},
                    "skippedWhen": "the passenger had no passport before",
                },
            ],
            "payload": payload,
            "requiredDocument": {
                "fileName": file_path.name,
                "contentType": mime,
                "sizeBytes": file_path.stat().st_size,
            },
            "dateTimezone": timezone,
        }, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to create a passport and attach its photo/scan.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    prior_ids = _passenger_passport_ids(api_get(token, f"/my-passenger/{passenger_id}"))
    created = api_post(token, "/my-passport/create", payload)
    new_id = _entity_id(created, "passport")
    if not new_id:
        _die(
            "Passport metadata was created, but AirSprint did not return its UUID, "
            "so the required photo/scan could not be uploaded. Do not rerun create; "
            "find the new UUID with `passport list`, then run `passport upload-document` once.",
            EXIT_ERROR,
        )
    try:
        uploaded = _android_document_upload(
            token,
            file_path=file_path,
            content_type=mime,
            init_path="/my-passport/document/upload-init",
            attach_path="/my-passport/document/attach",
            base_payload={"passportId": new_id},
        )
    except RuntimeError as exc:
        raise RuntimeError(json.dumps({
            "status": "error",
            "message": (
                "Passport metadata was created, but its required photo/scan was not fully attached. "
                "Do not rerun create; retry only `passport upload-document` with the returned UUID."
            ),
            "passportId": new_id,
            "uploadError": str(exc),
        })) from exc
    result: dict[str, Any] = {
        "result": created,
        "documentUpload": uploaded,
        "message": "Passport created and required photo/scan attached; no read-back was performed.",
    }
    if not prior_ids:
        result["passportIds"] = [new_id]
        _out(result, fmt, compact)
        return
    reordered = [new_id] + [value for value in prior_ids if value != new_id]
    try:
        linked = api_patch(
            token,
            f"/my-passenger/{passenger_id}",
            {"options": {"passportIds": reordered}},
        )
    except RuntimeError as exc:
        raise RuntimeError(json.dumps({
            "status": "error",
            "message": (
                "Passport and required photo/scan were created, but passportIds were not reordered. "
                "Do not rerun create; use `passport make-primary` with the returned UUID."
            ),
            "passportId": new_id,
            "documentAttached": True,
            "passengerUpdateError": str(exc),
        })) from exc
    result.update({
        "passengerUpdate": linked,
        "passportIds": reordered,
        "message": (
            "Passport created, required photo/scan attached, and new passport placed first; "
            "no selectedPassportId or read-back was sent."
        ),
    })
    _out(result, fmt, compact)


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
    if passport_id not in existing:
        _die("--passport-id must already belong to this passenger; no PATCH sent.", EXIT_VALIDATION)
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


@passport_app.command("upload-document")
def passport_upload_document(
    passport_id: str = typer.Option(..., "--id", help="Passport UUID"),
    file_value: str = typer.Option(..., "--file", help="Local document path"),
    content_type: Optional[str] = typer.Option(None, "--content-type", help="MIME type; inferred from the filename by default"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before uploading and attaching the document"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Upload and attach a passport document using Android's full workflow."""
    file_path, mime = _passport_scan_file(file_value, content_type)
    base_payload = {"passportId": passport_id}
    init_payload = {
        **base_payload,
        "fileName": file_path.name,
        "contentType": mime,
        "maxFileSizeBytes": ANDROID_DOCUMENT_MAX_BYTES,
    }
    if dry_run:
        _out({
            "dry_run": True,
            "steps": [
                {"method": "POST", "path": "/my-passport/document/upload-init", "payload": init_payload},
                {"method": "POST", "path": "presignedUpload.url", "multipartFile": file_path.name},
                {"method": "POST", "path": "/my-passport/document/attach", "payload": {**base_payload, "fileName": file_path.name, "contentType": mime, "storagePath": "<from upload-init>"}},
            ],
            "sizeBytes": file_path.stat().st_size,
        }, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to upload and attach a passport document.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(_android_document_upload(
        token,
        file_path=file_path,
        content_type=mime,
        init_path="/my-passport/document/upload-init",
        attach_path="/my-passport/document/attach",
        base_payload=base_payload,
    ), fmt, compact)


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
    """List saved pets (bodyless POST /my-pet, matching Android)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/my-pet")
    _out(_response_items(resp, limit), fmt, compact)


@pet_app.command("get")
def pet_get(
    pet_id: str = typer.Option(..., "--id"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get a saved pet (GET /my-pet/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/my-pet/{pet_id}"), fmt, compact)



_PET_SPECIES = ("DOG", "CAT", "RABBIT", "OTHER")
_PET_GENDERS = ("MALE", "FEMALE")
_PET_WEIGHTS = ("SMALL", "MEDIUM", "LARGE")
PetSaveProfile = typer.Option(
    None, "--save-profile",
    help='"yes" keeps the pet in your Saved Pets list; "no" creates it for this booking only.',
)


def _build_pet_create_body(
    *,
    name: str,
    species: str,
    gender: str,
    weight: str,
    saved: bool,
) -> dict[str, Any]:
    """Android NewPetRequestModel.toJson(), keys in source order."""
    return {
        "name": _required_text(name, "--name"),
        "species": _enum(species, "--species", _PET_SPECIES),
        "gender": _enum(gender, "--gender", _PET_GENDERS),
        "weight": _enum(weight, "--weight", _PET_WEIGHTS),
        "vaccineDocuments": [],
        "importFormReceipt": "",
        "isActive": saved,
        "picture": "",
        "note": "",
    }


def _build_pet_update_body(
    *,
    name: str | None,
    species: str | None,
    gender: str | None,
    weight: str | None,
    saved: bool | None,
) -> dict[str, Any]:
    """Android updatePetOwn(): {"options": {<changed fields>}}."""
    fields = {
        "name": name.strip() if name is not None else None,
        "species": _optional_enum(species, "--species", _PET_SPECIES),
        "gender": _optional_enum(gender, "--gender", _PET_GENDERS),
        "weight": _optional_enum(weight, "--weight", _PET_WEIGHTS),
        "isActive": saved,
    }
    options = {key: value for key, value in fields.items() if value is not None}
    if not options:
        _die("Pass at least one field to change (see --help).", EXIT_VALIDATION)
    return {"options": options}


@pet_app.command("create")
def pet_create(
    name: str = typer.Option(..., "--name"),
    species: str = typer.Option(..., "--species", help="DOG, CAT, RABBIT, or OTHER"),
    gender: str = typer.Option(..., "--gender", help="MALE or FEMALE"),
    weight: str = typer.Option(..., "--weight", help="SMALL (under 17 lb), MEDIUM (17-55 lb), or LARGE (over 55 lb)"),
    save_profile: Optional[str] = PetSaveProfile,
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Add a pet, the same fields as the app's "Add New Pet" form (POST /my-pet/create)."""
    saved = _yes_no(save_profile, "--save-profile")
    payload = _build_pet_create_body(
        name=name, species=species, gender=gender, weight=weight, saved=saved,
    )
    if dry_run:
        _out({
            "dry_run": True, "endpoint": "/my-pet/create", "saveProfile": saved, "payload": payload,
        }, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/my-pet/create", payload), fmt, compact)


@pet_app.command("upload-document")
def pet_upload_document(
    pet_id: str = typer.Option(..., "--id", help="Pet UUID"),
    file_value: str = typer.Option(..., "--file", help="Local PDF, JPEG, or PNG path"),
    document_type: str = typer.Option(..., "--document-type", help="vaccinationDocument or importFormReceipt"),
    content_type: Optional[str] = typer.Option(None, "--content-type", help="MIME type; inferred from the filename by default"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before uploading and attaching the document"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Upload and attach a pet document using Android's full workflow."""
    if document_type not in {"vaccinationDocument", "importFormReceipt"}:
        _die(
            "--document-type must be vaccinationDocument or importFormReceipt.",
            EXIT_VALIDATION,
        )
    file_path, mime = _document_file(file_value, content_type)
    if mime not in {"application/pdf", "image/jpeg", "image/png"}:
        _die(
            "Android pet documents support application/pdf, image/jpeg, or image/png.",
            EXIT_VALIDATION,
        )
    base_payload = {"petId": pet_id, "documentType": document_type}
    init_payload = {
        **base_payload,
        "fileName": file_path.name,
        "contentType": mime,
        "maxFileSizeBytes": ANDROID_DOCUMENT_MAX_BYTES,
    }
    if dry_run:
        _out({
            "dry_run": True,
            "steps": [
                {"method": "POST", "path": "/my-pet/document/upload-init", "payload": init_payload},
                {"method": "POST", "path": "presignedUpload.url", "multipartFile": file_path.name},
                {"method": "POST", "path": "/my-pet/document/attach", "payload": {**base_payload, "fileName": file_path.name, "contentType": mime, "storagePath": "<from upload-init>"}},
            ],
            "sizeBytes": file_path.stat().st_size,
        }, fmt, compact)
        return
    if not confirm:
        _die("--confirm required to upload and attach a pet document.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(_android_document_upload(
        token,
        file_path=file_path,
        content_type=mime,
        init_path="/my-pet/document/upload-init",
        attach_path="/my-pet/document/attach",
        base_payload=base_payload,
    ), fmt, compact)



@pet_app.command("update")
def pet_update(
    pet_id: str = typer.Option(..., "--id", help="Pet UUID"),
    name: Optional[str] = typer.Option(None, "--name"),
    species: Optional[str] = typer.Option(None, "--species", help="DOG, CAT, RABBIT, or OTHER"),
    gender: Optional[str] = typer.Option(None, "--gender", help="MALE or FEMALE"),
    weight: Optional[str] = typer.Option(None, "--weight", help="SMALL, MEDIUM, or LARGE"),
    save_profile: Optional[str] = PetSaveProfile,
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Edit a saved pet. Only the fields you pass are changed (PATCH /my-pet/{id})."""
    path = f"/my-pet/{pet_id}"
    payload = _build_pet_update_body(
        name=name, species=species, gender=gender, weight=weight,
        saved=_optional_yes_no(save_profile, "--save-profile"),
    )
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


def _leg_departure_country(leg: dict[str, Any]) -> str | None:
    flight = leg.get("flight")
    if isinstance(flight, dict) and flight.get("departureAirportCountry"):
        return flight["departureAirportCountry"]
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
        if len(matches) != 1:
            available = ", ".join(_passenger_name(row) for row in rows) or "none"
            _die(
                f'Passenger "{requested}" matched {len(matches)} leg passengers. Use the full first and last name. Available: {available}',
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


_CUSTOMS_CURRENCIES = ("CAD", "USD")
_CUSTOMS_MAX_PASSENGERS = 4  # app: "Maximum 4 people residing at the same address per declaration"
_CUSTOMS_CERTIFICATION = "I certify that the above declaration is true and complete. By checking this box, I confirm my agreement."


def _customs_submission_status(response: Any, leg_id: str | None = None) -> dict[str, Any]:
    """The app disables passengers using this server flag, not a signature field."""
    data = _response_data(response)
    leg = data.get("leg") if isinstance(data, dict) else None
    if not isinstance(leg, dict) or not leg.get("id") or (leg_id and leg.get("id") != leg_id):
        _die("Customs link does not identify the requested leg; no declaration submitted.", EXIT_VALIDATION)
    rows = leg.get("passengers")
    if not isinstance(rows, list):
        _die("Cannot verify customs submission status; no declaration submitted.", EXIT_VALIDATION)
    passengers = []
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"] or row["id"] in seen
                or type(row.get("customsDeclarationAlreadySubmitted")) is not bool):
            _die("Incomplete or ambiguous customs submission status; no declaration submitted.", EXIT_VALIDATION)
        seen.add(row["id"])
        submitted = row["customsDeclarationAlreadySubmitted"]
        passengers.append({
            "legPassengerId": row["id"], "name": _passenger_name(row),
            "customsDeclarationAlreadySubmitted": submitted,
            "submissionStatus": "submitted" if submitted else "not-submitted",
        })
    return {"linkId": data.get("id"), "legId": leg["id"], "passengers": passengers}


def _require_customs_not_submitted(status: dict[str, Any], passenger_ids: list[str]) -> None:
    by_id = {p["legPassengerId"]: p for p in status["passengers"]}
    if any(p not in by_id for p in passenger_ids):
        _die("Selected passengers are missing from the customs link; no declaration submitted.", EXIT_VALIDATION)
    already = [by_id[p]["name"] for p in passenger_ids if by_id[p]["customsDeclarationAlreadySubmitted"]]
    if already:
        _die("Already submitted / déjà soumise: " + ", ".join(already)
             + ". Duplicate submission blocked. Select only passengers not yet submitted.", EXIT_VALIDATION)


def _customs_declaration_date(value: str | None, timezone: str | None) -> str:
    """Android: DateFormat("yyyy-MM-dd").tryParse(date).toUtc().toIso8601String().

    The form's "Date" (Traveller Declaration Form section) is a calendar day.
    The app converts local midnight of that day to UTC, so 2026-09-01 in
    America/Montreal is sent as 2026-09-01T04:00:00.000Z.
    """
    text = (value or "").strip()
    if not text:
        _die("--date is required: YYYY-MM-DD, the Traveller Declaration Form date. Nothing was sent.", EXIT_VALIDATION)
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        _die("--date must be YYYY-MM-DD (a calendar day, no time), as in the app's date picker.", EXIT_VALIDATION)
    if not timezone:
        _die(
            "--timezone is required: the app sends the declaration date from the device's local midnight. "
            "Set AIRSPRINT_TIMEZONE or pass --tz.",
            EXIT_VALIDATION,
        )
    if not ZoneInfo:
        _die(f"Cannot convert local time: zoneinfo unavailable for {timezone}", EXIT_ERROR)
    try:
        zone = ZoneInfo(timezone)
    except Exception:
        _die(f"Unknown timezone: {timezone}", EXIT_VALIDATION)
    return day.replace(tzinfo=zone).astimezone(_tz_utc.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _customs_text(value: str | None) -> str | None:
    """Android sends a detail field only when it is non-empty after trim()."""
    text = (value or "").strip()
    return text or None


def _customs_money(value: str | None, option: str) -> float | None:
    """Android: double.parse(alcoholOrTobaccoValueCAD) when the field is set."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        _die(f"{option} must be a number in CAD, e.g. 45.50.", EXIT_VALIDATION)


def _build_customs_body(
    *,
    link_id: str,
    leg_passenger_ids: list[str],
    date: str,
    purpose: str,
    description: str,
    has_pet: bool,
    has_alcohol_or_tobacco: bool,
    alcohol_type: str | None,
    alcohol_volume: str | None,
    alcohol_value_cad: float | None,
    has_imported_goods: bool,
    imported_goods: str | None,
    imported_goods_currency: str | None,
    imported_goods_from_us: bool,
    souvenir_items: str | None,
    has_high_value_currency: bool,
    high_value_currency_description: str | None,
) -> dict[str, Any]:
    """RemoteCustomDeclarationRepository.submitDeclaration() body, keys in app order.

    ``canadianCustomsDeclationLinkId`` keeps the app's misspelling. Detail
    strings are present only when non-empty, exactly like the app. The seven
    payment-authorization keys the app can add (cardType, cardName,
    phoneNumber, billingAddress, cardNumber, expiry, cvc) are deliberately
    not offered by the CLI.
    """
    body: dict[str, Any] = {
        "canadianCustomsDeclationLinkId": link_id,
        "legPassengerIds": leg_passenger_ids,
        "date": date,
        "purposeOfTravel": purpose,
        "travelDescription": description,
        "hasPet": has_pet,
        "hasAlcoholOrTobacco": has_alcohol_or_tobacco,
    }
    if alcohol_type:
        body["alcoholOrTobaccoType"] = alcohol_type
    if alcohol_volume:
        body["alcoholOrTobaccoVolume"] = alcohol_volume
    if alcohol_value_cad is not None:
        body["alcoholOrTobaccoValueCAD"] = alcohol_value_cad
    body["hasImportedGoods"] = has_imported_goods
    if imported_goods:
        body["importedGoodItems"] = imported_goods
    if imported_goods_currency:
        body["importedGoodsCurrency"] = imported_goods_currency
    body["importedGoodsFromUS"] = imported_goods_from_us
    if souvenir_items:
        body["souvenirItems"] = souvenir_items
    body["hasHighValueCurrency"] = has_high_value_currency
    if high_value_currency_description:
        body["highValueCurrencyDescription"] = high_value_currency_description
    return body


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


def _customs_draft_checksum(draft: dict[str, Any]) -> str:
    content = {k: v for k, v in draft.items() if k != "checksum"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _customs_certification_digest(draft: dict[str, Any]) -> str:
    return _customs_draft_checksum({k: draft[k] for k in ("legId", "legPassengerIds", "snapshot", "answers", "family")})


def _customs_draft_filename(draft: dict[str, Any]) -> str:
    """Use the booking and each traveller's full name, never first-name aliases."""
    snapshot = draft["snapshot"]
    slugs = []
    for person in snapshot["passengers"]:
        name = str(person.get("name") or "").strip()
        if len(name.split()) < 2:
            _die("Customs drafts require each traveller's full first and last name.", EXIT_VALIDATION)
        normalized = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
        slug = re.sub(r"[\W_]+", "-", normalized.casefold()).strip("-")
        if not slug:
            _die("Cannot build a draft filename from the traveller's full name.", EXIT_VALIDATION)
        slugs.append(slug)
    if not slugs:
        _die("A customs draft needs at least one named traveller.", EXIT_VALIDATION)
    booking = re.sub(r"[^A-Za-z0-9_-]+", "-", str(snapshot.get("bookingId") or draft["legId"])).strip("-")
    return booking + "-" + "--".join(slugs) + ".json"


def _require_customs_draft_filename(path: Path, draft: dict[str, Any]) -> None:
    expected = _customs_draft_filename(draft)
    if path.name != expected:
        _die(f"Use the full-name draft filename {expected}. First-name-only and other aliases are not supported.", EXIT_VALIDATION)


def _save_customs_draft(path: Path, draft: dict[str, Any]) -> None:
    _require_customs_draft_filename(path, draft)
    draft["checksum"] = _customs_draft_checksum(draft)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write_json(path, draft)


def _load_customs_draft(path: Path) -> dict[str, Any]:
    try:
        draft = json.loads(path.read_text())
    except (OSError, ValueError):
        _die("Cannot read customs draft. Use customs prepare to create it.", EXIT_VALIDATION)
    if (not isinstance(draft, dict) or draft.get("schema") != "airsprint-customs-draft-v1"
            or draft.get("checksum") != _customs_draft_checksum(draft)):
        _die("Invalid or manually modified customs draft. Rebuild it with customs prepare.", EXIT_VALIDATION)
    _require_customs_draft_filename(path, draft)
    return draft


def _customs_leg_snapshot(leg: dict[str, Any], passenger_ids: list[str]) -> dict[str, Any]:
    audit = _audit_leg_travel_info(leg)
    rows = {r.get("id"): r for r in _leg_passenger_rows(leg) if isinstance(r, dict)}
    flight = leg.get("flight") or leg
    itinerary = {k: flight.get(k) for k in (
        "departureTime", "arrivalTime", "departureAirportCode", "arrivalAirportCode",
        "departureAirportCountry", "arrivalAirportCountry", "departureAirportTimezone", "arrivalAirportTimezone",
    )}
    blockers = []
    if str(leg.get("status", "")).upper() == "CANCELLED":
        blockers.append("cancelledLeg")
    if str(itinerary["arrivalAirportCountry"] or "").casefold() not in {"canada", "ca", "can"}:
        blockers.append("CanadianCustomsRequiresLegArrivingInCanada")
    if not all(itinerary[k] for k in ("departureTime", "arrivalTime", "departureAirportCode", "arrivalAirportCode")):
        blockers.append("incompleteItinerary")
    selected = []
    for passenger_id in passenger_ids:
        match = next((p for p in audit["passengers"] if p["legPassengerId"] == passenger_id), None)
        if match is None:
            _die("Draft passenger is no longer on this leg. Prepare a new draft.", EXIT_VALIDATION)
        item = dict(match)
        row = rows[passenger_id]
        passports = row.get("passports") or []
        record = next((p for p in passports if p.get("id") == item["selectedPassportId"]), None) if item["selectedPassportId"] else next(iter(passports), None)
        item["passport"] = {k: record.get(k) for k in (
            "id", "passportNumber", "nationality", "issuingAuthority", "dateOfBirth", "dateOfBirthTimestamp",
            "expirationDate", "expirationDateTimestamp",
        )} if record else None
        item["destinationAddress"] = _leg_destination_address_update(row.get("destinationAddress") or {})
        if not item["destinationAddressComplete"] and any(
            str(itinerary[k] or "").casefold() in {"united states", "usa", "us"}
            for k in ("departureAirportCountry", "arrivalAirportCountry")
        ):
            item["issues"].append("missingDestinationAddress")
        if record:
            for field in ("passportNumber", "nationality", "issuingAuthority"):
                if not record.get(field):
                    item["issues"].append("missingPassportField:" + field)
            for field in ("dateOfBirth", "expirationDate"):
                if not (record.get(field) or record.get(field + "Timestamp")):
                    item["issues"].append("missingPassportField:" + field)
        blockers.extend(item["name"] + ":" + issue for issue in item["issues"])
        selected.append(item)
    return {"legId": leg.get("id"), "bookingId": leg.get("bookingId"), "itinerary": itinerary,
            "passengers": selected, "blockers": blockers}


def _customs_form_from_answers(answers: dict[str, Any], link_id: str, ids: list[str]) -> tuple[dict[str, Any] | None, list[str]]:
    required = ["purpose", "date", "timezone", "has_pet", "has_alcohol_or_tobacco", "has_imported_goods", "has_high_value_currency"]
    missing = [key for key in required if answers.get(key) is None or answers.get(key) == ""]
    purpose = _optional_enum(answers.get("purpose"), "--purpose", ("BUSINESS", "PLEASURE"))
    if purpose == "BUSINESS" and not _customs_text(answers.get("description")):
        missing.append("description")
    flags = {k: _optional_yes_no(answers.get(k), "--" + k.replace("_", "-")) for k in
             ("has_pet", "has_alcohol_or_tobacco", "has_imported_goods", "has_high_value_currency", "imported_goods_from_us")}
    for condition, fields in (
        ("has_alcohol_or_tobacco", ["alcohol_type", "alcohol_volume", "alcohol_value_cad"]),
        ("has_imported_goods", ["imported_goods", "imported_goods_currency", "imported_goods_from_us"]),
        ("has_high_value_currency", ["high_value_currency_description"]),
    ):
        if flags[condition]:
            missing.extend(k for k in fields if answers.get(k) is None or answers.get(k) == "")
    amount = _customs_money(answers.get("alcohol_value_cad"), "--alcohol-value-cad")
    if amount is not None and not (0 <= amount < float("inf")):
        _die("--alcohol-value-cad must be a finite non-negative amount.", EXIT_VALIDATION)
    currency = _optional_enum(answers.get("imported_goods_currency"), "--imported-goods-currency", _CUSTOMS_CURRENCIES)
    date_value = _customs_declaration_date(answers["date"], answers["timezone"]) if answers.get("date") and answers.get("timezone") else None
    if missing:
        return None, ["--" + k.replace("_", "-") for k in dict.fromkeys(missing)]
    return _build_customs_body(
        link_id=link_id, leg_passenger_ids=ids, date=date_value, purpose=purpose,
        description=_customs_text(answers.get("description")) or "", has_pet=flags["has_pet"],
        has_alcohol_or_tobacco=flags["has_alcohol_or_tobacco"],
        alcohol_type=_customs_text(answers.get("alcohol_type")), alcohol_volume=_customs_text(answers.get("alcohol_volume")),
        alcohol_value_cad=amount, has_imported_goods=flags["has_imported_goods"],
        imported_goods=_customs_text(answers.get("imported_goods")), imported_goods_currency=currency,
        imported_goods_from_us=flags["imported_goods_from_us"] or False,
        souvenir_items=_customs_text(answers.get("souvenir_items")), has_high_value_currency=flags["has_high_value_currency"],
        high_value_currency_description=_customs_text(answers.get("high_value_currency_description")),
    ), []


def _customs_draft_preview(draft: dict[str, Any], path: Path) -> dict[str, Any]:
    ids = draft["legPassengerIds"]
    groups = [ids] if draft["family"] else [[p] for p in ids]
    body, missing = _customs_form_from_answers(draft["answers"], draft.get("linkId") or "<link created at submission>", ids)
    status = draft.get("submissionStatus")
    already = [p["name"] for p in (status or {}).get("passengers", [])
               if p["legPassengerId"] in ids and p["customsDeclarationAlreadySubmitted"]]
    blockers = list(draft["snapshot"]["blockers"]) + missing
    blockers.extend(name + ":alreadySubmitted" for name in already)
    forms = [{**body, "legPassengerIds": group} for group in groups] if body else []
    digest = _customs_certification_digest(draft)
    certifications = draft.get("certifications", {})
    pending = [p["name"] for p in draft["snapshot"]["passengers"]
               if certifications.get(p["legPassengerId"], {}).get("digest") != digest]
    return {"localDraft": True, "draft": str(path), "state": draft["state"], "legId": draft["legId"],
            "snapshot": draft["snapshot"], "answers": draft["answers"], "family": draft["family"],
            "forms": len(groups), "declarations": len(ids), "payloads": forms,
            **({"payload": forms[0]} if len(forms) == 1 else {}),
            "submissionStatus": status or "unverified-until-link-created", "blockers": blockers,
            "readyForApproval": not blockers and draft["state"] == "draft",
            "pendingCertifications": pending,
            "readyForSubmission": not blockers and not pending and draft["state"] == "draft",
            "certification": _CUSTOMS_CERTIFICATION,
            "message": "Local draft only. Review passports against scans and every answer with the travellers. "
                       "Certify each person with customs certify, then customs submit --draft ... --confirm. "
                       "Submitted status is per leg and per passenger; preparation sends nothing to AirSprint."}


@customs_app.command("review")
def customs_review(
    draft_file: str = typer.Option(..., "--draft"),
    fmt: str = Format, compact: bool = Compact,
):
    """Review a local customs draft without authentication or network requests."""
    path = Path(draft_file).expanduser().resolve()
    _out(_customs_draft_preview(_load_customs_draft(path), path), fmt, compact)


@customs_app.command("certify")
def customs_certify(
    draft_file: str = typer.Option(..., "--draft"),
    passenger: str = typer.Option(..., "--passenger", help="One exact traveller name or leg-passenger UUID, never a list"),
    approve: str = typer.Option(..., "--approve", help="yes records that traveller's explicit certification; no removes it"),
    fmt: str = Format, compact: bool = Compact,
):
    """Record one person's certification locally after showing and confirming their information."""
    approval = _yes_no(approve, "--approve")
    path = Path(draft_file).expanduser().resolve()
    draft = _load_customs_draft(path)
    preview = _customs_draft_preview(draft, path)
    if draft["state"] != "draft":
        _die("Only an unsubmitted draft can be certified.", EXIT_VALIDATION)
    people = [p for p in draft["snapshot"]["passengers"]
              if passenger == p["legPassengerId"] or passenger.strip().casefold() == p["name"].casefold()]
    if len(people) != 1:
        _die("Select exactly one traveller by full name or leg-passenger UUID.", EXIT_VALIDATION)
    if approval and preview["blockers"]:
        _die("Complete and review the draft first: " + ", ".join(preview["blockers"]), EXIT_VALIDATION)
    person = people[0]
    certifications = draft.setdefault("certifications", {})
    if approval:
        certifications[person["legPassengerId"]] = {
            "digest": _customs_certification_digest(draft), "name": person["name"],
            "certifiedAt": datetime.now(_tz_utc.utc).isoformat(), "statement": _CUSTOMS_CERTIFICATION,
        }
    else:
        certifications.pop(person["legPassengerId"], None)
    _save_customs_draft(path, draft)
    _out({"draft": str(path), "person": person, "answers": draft["answers"], "certification": _CUSTOMS_CERTIFICATION,
          "certified": approval, "pendingCertifications": _customs_draft_preview(draft, path)["pendingCertifications"],
          "message": "Recorded locally. No declaration submitted."}, fmt, compact)


@customs_app.command("submit")
def customs_submit(
    draft_file: str = typer.Option(..., "--draft"),
    confirm: bool = typer.Option(False, "--confirm"),
    probe: bool = typer.Option(False, "--probe/--no-probe"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Submit a reviewed local draft once after fresh leg and submission-status checks."""
    if not confirm:
        _die("--confirm required after individual certifications. Nothing submitted.", EXIT_VALIDATION)
    path = Path(draft_file).expanduser().resolve()
    CUSTOMS_DRAFT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Serialize submissions from this CLI on this machine, including different draft files.
    with (CUSTOMS_DRAFT_DIR / ".submit.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _die("Another customs submission is in progress. Nothing sent.", EXIT_VALIDATION)
        draft = _load_customs_draft(path)
        preview = _customs_draft_preview(draft, path)
        if draft["state"] != "draft":
            _die("Draft is " + draft["state"] + ". It cannot be submitted again. Check customs status.", EXIT_VALIDATION)
        if preview["blockers"]:
            _die("Draft is incomplete: " + ", ".join(preview["blockers"]), EXIT_VALIDATION)
        if preview["pendingCertifications"]:
            _die("Each traveller must be certified first: " + ", ".join(preview["pendingCertifications"]), EXIT_VALIDATION)
        ledger_path = CUSTOMS_DRAFT_DIR / "submissions.json"
        try:
            ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        except (OSError, ValueError):
            _die("Cannot read the customs submission journal. Nothing submitted.", EXIT_VALIDATION)
        if not isinstance(ledger, dict):
            _die("Invalid customs submission journal. Nothing submitted.", EXIT_VALIDATION)
        keys = [draft["legId"] + "/" + p for p in draft["legPassengerIds"]]
        if any(k in ledger for k in keys):
            _die("A submission was already attempted for these leg passengers. Duplicate blocked even from a new draft. Check customs status.", EXIT_VALIDATION)
        _guard_booking_probe(probe)
        token = get_api_token(username, password)
        leg = _response_data(api_get(token, f"/my-leg/{draft['legId']}"))
        if not isinstance(leg, dict):
            _die("Unexpected leg response; draft not submitted.", EXIT_ERROR)
        current = _customs_leg_snapshot(leg, draft["legPassengerIds"])
        if current != draft["snapshot"]:
            _die("Trip, passport or address data changed since preparation. Refresh the draft and review it again. Nothing submitted.", EXIT_VALIDATION)
        link_id = draft.get("linkId") or leg.get("customsDeclarationId")
        if not link_id:
            link = api_post(token, "/canadian-customs-declaration-link/create", {"legId": draft["legId"]})
            link_id = _entity_id(link, "link")
            if not link_id:
                _die("No link ID returned; no declaration submitted.", EXIT_ERROR)
        draft["linkId"] = link_id
        status = _customs_submission_status(api_get(token, f"/canadian-customs-declaration-link/{link_id}"), draft["legId"])
        draft["submissionStatus"] = status
        _save_customs_draft(path, draft)
        _require_customs_not_submitted(status, draft["legPassengerIds"])
        results = []
        for form in preview["payloads"]:
            form["canadianCustomsDeclationLinkId"] = link_id
            draft.update(state="submitting", pendingLegPassengerIds=form["legPassengerIds"], results=results)
            for passenger_id in form["legPassengerIds"]:
                ledger[draft["legId"] + "/" + passenger_id] = {"state": "submitting", "draft": str(path), "linkId": link_id}
            _atomic_write_json(ledger_path, ledger)
            _save_customs_draft(path, draft)  # Before the write: even a killed process cannot silently resubmit.
            try:
                response = api_post(token, "/canadianCustomsDeclaration/create", form)
                records = _response_data(response)
                records = records if isinstance(records, list) else [records]
                if len(records) != len(form["legPassengerIds"]) or any(not isinstance(r, dict) or not r.get("id") for r in records):
                    raise RuntimeError("Submission response did not identify every declaration.")
            except Exception as exc:
                draft["state"] = "uncertain"
                for passenger_id in form["legPassengerIds"]:
                    ledger[draft["legId"] + "/" + passenger_id]["state"] = "uncertain"
                _atomic_write_json(ledger_path, ledger)
                _save_customs_draft(path, draft)
                raise RuntimeError(json.dumps({"status": "error", "message": "Submission stopped; status is uncertain. Do not resubmit. Check customs status.",
                                               "draft": str(path), "linkId": link_id, "completedForms": results, "cause": str(exc)})) from exc
            results.append({"legPassengerIds": form["legPassengerIds"], "submissionStatus": "submitted", "result": response})
            for passenger_id in form["legPassengerIds"]:
                ledger[draft["legId"] + "/" + passenger_id]["state"] = "submitted"
            _atomic_write_json(ledger_path, ledger)
            for p in status["passengers"]:
                if p["legPassengerId"] in form["legPassengerIds"]:
                    p.update(customsDeclarationAlreadySubmitted=True, submissionStatus="submitted")
            draft.update(results=results, submissionStatus=status)
            _save_customs_draft(path, draft)
        draft.update(state="submitted", pendingLegPassengerIds=[], approvedByCaller=True)
        _save_customs_draft(path, draft)
        _out({"draft": str(path), "state": "submitted", "linkId": link_id, "results": results,
              "submissionStatus": status, "message": "Submitted once per form after certification. No read-back performed."}, fmt, compact)


@customs_app.command("prepare")
@customs_app.command("create")
def customs_create(
    booking_id: Optional[str] = typer.Option(None, "--booking", help="Booking code or trip UUID (selects its active leg arriving in Canada)"),
    leg_id: Optional[str] = typer.Option(None, "--leg-id", help="Booked-leg UUID"),
    passengers: Optional[str] = typer.Option(None, "--passengers", help="Comma-separated full first and last names; one form each by default. Use separate commands for different answers."),
    family: Optional[str] = typer.Option(None, "--family", help="yes confirms all named passengers are family living at the same address (max 4); one shared form"),
    purpose: Optional[str] = typer.Option(None, "--purpose", help="BUSINESS | PLEASURE"),
    description: Optional[str] = typer.Option(None, "--description", help="Purpose details; required for BUSINESS"),
    date: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD — the Traveller Declaration Form date (needs --timezone)"),
    has_pet: Optional[str] = typer.Option(None, "--has-pet", help="yes | no — travelling with a pet"),
    has_alcohol_or_tobacco: Optional[str] = typer.Option(None, "--has-alcohol-or-tobacco", help="yes | no; yes needs --alcohol-type, --alcohol-volume, --alcohol-value-cad"),
    alcohol_type: Optional[str] = typer.Option(None, "--alcohol-type", help="Type of alcohol or tobacco, e.g. wine"),
    alcohol_volume: Optional[str] = typer.Option(None, "--alcohol-volume", help='Volume/quantity, e.g. "2 x 750 ml"'),
    alcohol_value_cad: Optional[str] = typer.Option(None, "--alcohol-value-cad", help="Total value in CAD, e.g. 45.50"),
    has_imported_goods: Optional[str] = typer.Option(None, "--has-imported-goods", help="yes | no; yes needs --imported-goods, --imported-goods-currency, --imported-goods-from-us"),
    imported_goods: Optional[str] = typer.Option(None, "--imported-goods", help="Describe the goods in detail"),
    imported_goods_currency: Optional[str] = typer.Option(None, "--imported-goods-currency", help="CAD | USD — currency of purchases"),
    imported_goods_from_us: Optional[str] = typer.Option(None, "--imported-goods-from-us", help="yes | no — are the goods products of the U.S.?"),
    souvenir_items: Optional[str] = typer.Option(None, "--souvenir-items", help="Souvenirs or miscellaneous items (optional)"),
    has_high_value_currency: Optional[str] = typer.Option(None, "--has-high-value-currency", help="yes | no; yes needs --high-value-currency-description"),
    high_value_currency_description: Optional[str] = typer.Option(None, "--high-value-currency-description", help="Currency or monetary instruments of CAD 10,000 or more"),
    link_id: Optional[str] = typer.Option(None, "--link-id", help="Existing declaration link; a missing link is created only at final submission"),
    timezone: Optional[str] = Timezone,
    draft_file: Optional[str] = typer.Option(None, "--draft", help="Local BOOKING-firstname-lastname.json path; reuse to complete or correct answers"),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh trip/passport/address/status data and invalidate certifications"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    probe: bool = typer.Option(False, "--probe/--no-probe", help="Override recent-booking-write cooldown"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create or correct a LOCAL draft; never submit a declaration.

    Omit unanswered questions to save an incomplete draft. Reuse --draft to
    fill answers without a network call; --refresh reads the same leg again.
    Each traveller must then be certified individually before customs submit.
    """
    path = Path(draft_file).expanduser().resolve() if draft_file else None
    existing = _load_customs_draft(path) if path is not None and path.exists() else None
    if existing and existing["state"] != "draft":
        _die("A submitted or uncertain draft cannot be edited. Check customs status before preparing another.", EXIT_VALIDATION)
    if existing and any((booking_id, leg_id, passengers, link_id)):
        _die("To edit a draft, use --draft with answer options or --refresh. Its leg and passengers cannot be replaced.", EXIT_VALIDATION)
    if not existing and bool(booking_id) == bool(leg_id):
        _die("Provide exactly one of --booking or --leg-id.", EXIT_VALIDATION)
    answer_values = {
        "purpose": purpose, "description": description, "date": date, "timezone": timezone,
        "has_pet": has_pet, "has_alcohol_or_tobacco": has_alcohol_or_tobacco,
        "alcohol_type": alcohol_type, "alcohol_volume": alcohol_volume, "alcohol_value_cad": alcohol_value_cad,
        "has_imported_goods": has_imported_goods, "imported_goods": imported_goods,
        "imported_goods_currency": imported_goods_currency, "imported_goods_from_us": imported_goods_from_us,
        "souvenir_items": souvenir_items, "has_high_value_currency": has_high_value_currency,
        "high_value_currency_description": high_value_currency_description,
    }
    answers = dict(existing["answers"]) if existing else {}
    answers.update({k: v for k, v in answer_values.items() if v is not None})
    family_flag = _yes_no(family, "--family") if family is not None else (existing["family"] if existing else False)
    # Validate any supplied answers before auth; incomplete answers remain visible in the draft.
    _customs_form_from_answers(answers, "preview", [])
    names = [] if existing else _csv(passengers, "--passengers", required=True)
    count = len(existing["legPassengerIds"]) if existing else len(names)
    if family_flag and count > _CUSTOMS_MAX_PASSENGERS:
        _die("Family forms cover at most 4 people residing at the same address.", EXIT_VALIDATION)
    if existing and not refresh:
        draft = dict(existing)
    else:
        _guard_booking_probe(probe)
        token = get_api_token(username, password)
        if existing:
            leg_id = existing["legId"]
        if leg_id:
            leg = _response_data(api_get(token, f"/my-leg/{leg_id}"))
        else:
            trip_uuid = _resolve_trip_uuid(token, booking_id or "")
            trip = api_get(token, f"/trip/{trip_uuid}")
            legs = [item for item in _trip_legs(trip) if str(item.get("status", "")).upper() != "CANCELLED"]
            returns = [item for item in legs if str((item.get("flight") or item).get("arrivalAirportCountry", "")).casefold() in {"canada", "ca", "can"}]
            if len(returns) != 1:
                _die("Booking must have exactly one active leg arriving in Canada. Select its --leg-id explicitly.", EXIT_VALIDATION)
            leg = returns[0]
        if not isinstance(leg, dict) or not isinstance(leg.get("id"), str):
            _die("Unexpected leg response.", EXIT_ERROR)
        ids = existing["legPassengerIds"] if existing else _resolve_customs_passengers(leg, names)
        snapshot = _customs_leg_snapshot(leg, ids)
        resolved_link_id = (existing.get("linkId") if existing else link_id) or leg.get("customsDeclarationId")
        status = _customs_submission_status(api_get(token, f"/canadian-customs-declaration-link/{resolved_link_id}"), leg["id"]) if resolved_link_id else None
        draft = {"schema": "airsprint-customs-draft-v1", "state": "draft", "legId": leg["id"],
                 "legPassengerIds": ids, "linkId": resolved_link_id, "snapshot": snapshot, "submissionStatus": status}
    draft.update(answers=answers, family=family_flag, certifications={})
    if path is None:
        path = CUSTOMS_DRAFT_DIR / _customs_draft_filename(draft)
        if path.exists():
            _die(f"Draft already exists: {path}. Use --draft to edit it; it was not overwritten.", EXIT_VALIDATION)
    _require_customs_draft_filename(path, draft)
    if not dry_run:
        _save_customs_draft(path, draft)
    preview = _customs_draft_preview(draft, path)
    if dry_run:
        preview.update(dry_run=True, method="POST", path="/canadianCustomsDeclaration/create")
    _out(preview, fmt, compact)


@customs_app.command("update-date", hidden=True)
def customs_update_date(
    declaration_id: str = typer.Option(..., "--id"),
    date: str = typer.Option(..., "--date"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm"),
):
    """Retired: edit local drafts before certification and submission."""
    _die("Submitted declarations are final in this CLI. Edit the local draft date with customs prepare --draft ... --date ... before submission. No PATCH sent.", EXIT_VALIDATION)


@customs_app.command("link-create")
def customs_link_create(
    leg_id: str = typer.Option(..., "--leg-id", help="Booked-leg UUID"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Create a customs-declaration link to share with a passenger (POST /canadian-customs-declaration-link/create)."""
    payload = {"legId": leg_id}
    if dry_run:
        _out({
            "dry_run": True,
            "method": "POST",
            "path": "/canadian-customs-declaration-link/create",
            "payload": payload,
        }, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/canadian-customs-declaration-link/create", payload), fmt, compact)


@customs_app.command("status")
@customs_app.command("link-get")
def customs_link_get(
    link_id: str = typer.Option(..., "--id", "--link-id", help="Customs declaration link UUID (not a passenger declaration ID)"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Read the link and each passenger's submitted status, without submitting."""
    token = get_api_token(username, password)
    response = api_get(token, f"/canadian-customs-declaration-link/{link_id}")
    _out({"link": response, "submissionStatus": _customs_submission_status(response)}, fmt, compact)


# ---------------------------------------------------------------------------
# booking — additional flows
# ---------------------------------------------------------------------------


@booking_app.command("empty-leg", help="Book seats on an empty leg from `explore flights` (POST /empty-leg/book).")
@booking_app.command("shared-flight", help="Join a shared flight from `explore flights` (POST /shared-flight/book).")
def booking_existing_flight(
    ctx: typer.Context,
    flight_id: str = typer.Option(..., "--flight-id", help="Flight ID from `explore flights`"),
    passengers: str = typer.Option(..., "--passengers", help="Comma-separated saved passenger IDs"),
    destination_street: Optional[str] = typer.Option(None, "--destination-street", help="Where passengers stay (US flights)."),
    destination_street2: Optional[str] = typer.Option(None, "--destination-street2", help="Suite / apartment / unit."),
    destination_city: Optional[str] = typer.Option(None, "--destination-city"),
    destination_state: Optional[str] = typer.Option(None, "--destination-state"),
    destination_zip: Optional[str] = typer.Option(None, "--destination-zip"),
    pets: Optional[str] = typer.Option(None, "--pets", help="Comma-separated pet IDs from `pet list`."),
    baggage: list[str] = typer.Option(..., "--baggage", help=_BAGGAGE_HELP),
    catering: str = typer.Option("no", "--catering", help="yes | no"),
    catering_request: Optional[str] = typer.Option(None, "--catering-request"),
    ground_transportation: str = typer.Option("no", "--ground-transportation", help="yes | no"),
    ground_transportation_when: Optional[str] = typer.Option(None, "--ground-transportation-when", help="departure | arrival | both"),
    ground_transportation_method: Optional[str] = typer.Option(None, "--ground-transportation-method", help=_GROUND_METHOD_HELP),
    ground_pickup_address: Optional[str] = typer.Option(None, "--ground-pickup-address", help=_ADDRESS_TEXT_HELP),
    ground_dropoff_address: Optional[str] = typer.Option(None, "--ground-dropoff-address", help=_ADDRESS_TEXT_HELP),
    arrival_ground_method: Optional[str] = typer.Option(None, "--arrival-ground-method", help=_GROUND_METHOD_HELP),
    arrival_pickup_address: Optional[str] = typer.Option(None, "--arrival-pickup-address", help=_ADDRESS_TEXT_HELP),
    arrival_dropoff_address: Optional[str] = typer.Option(None, "--arrival-dropoff-address", help=_ADDRESS_TEXT_HELP),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Book an existing flight with Android's exact BookSharedRequest body."""
    path = "/empty-leg/book" if ctx.command.name == "empty-leg" else "/shared-flight/book"
    passenger_ids = _csv(passengers, "--passengers", required=True)
    destination = _address_payload(
        destination_street, destination_street2, destination_city, destination_state, destination_zip,
        "--destination",
    )
    payload = _build_shared_booking_body(
        flight_id=flight_id,
        passengers=[
            _build_shared_passenger(passenger_id, destination)
            for passenger_id in passenger_ids
        ],
        request_settings=_build_request_settings(
            catering=_yes_no(catering, "--catering"),
            catering_request=(catering_request or "").strip() or None,
            ground_transportation=_yes_no(ground_transportation, "--ground-transportation"),
            ground_transportation_type=_optional_enum(ground_transportation_when, "--ground-transportation-when", _GROUND_TRANSPORTATION_TYPES),
            ground_transportation_method=_optional_enum(ground_transportation_method, "--ground-transportation-method", _GROUND_TRANSPORTATION_METHODS),
            pickup_address=_address_text(ground_pickup_address, "--ground-pickup-address"),
            dropoff_address=_address_text(ground_dropoff_address, "--ground-dropoff-address"),
            arrival_method=_optional_enum(arrival_ground_method, "--arrival-ground-method", _GROUND_TRANSPORTATION_METHODS),
            arrival_pickup_address=_address_text(arrival_pickup_address, "--arrival-pickup-address"),
            arrival_dropoff_address=_address_text(arrival_dropoff_address, "--arrival-dropoff-address"),
        ),
        pet_ids=_csv(pets, "--pets"),
        baggage=_baggage_items(baggage),
    )
    if dry_run:
        _out({
            "dry_run": True,
            "method": "POST",
            "path": path,
            "payload": payload,
            "message": f"Would POST {path} exactly once; no read-back would follow.",
        }, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, path, payload), fmt, compact)


@booking_app.command("lock")
def booking_lock(
    flight_id: str = typer.Option(..., "--flight-id", help="Flight ID from `explore flights`"),
    release: bool = typer.Option(False, "--release", help="Release a hold instead of placing one"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Hold a flight while you finish booking it, or release the hold (POST /flight/lock)."""
    payload = _build_flight_lock_body(flight_id, not release)
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": "/flight/lock", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/flight/lock", payload), fmt, compact)


@booking_app.command("reserved-days")
def booking_reserved_days(
    limit: int = typer.Option(90, "--limit", min=1, max=500),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List reserved calendar days (read-only POST /reserve-day)."""
    payload = {"sort": [], "page": {"limit": limit, "offset": 0}}
    token = get_api_token(username, password)
    response = api_post(token, "/reserve-day", payload)
    _out(_response_items(response, limit), fmt, compact)


@booking_app.command("baggage-types")
def booking_baggage_types(
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """List baggage types (POST /baggage-type with no request body), as Android does."""
    token = get_api_token(username, password)
    response = api_post(token, "/baggage-type")
    _out(_response_items(response), fmt, compact)


# Android 6.1.4 BookingSurveyCreateRequestModel (POST /booking-survey/create).
# Wire values from the app's enum objects (blutter objs.txt off_10).
_SURVEY_AGREEMENT = ("STRONGLY_AGREE", "AGREE", "DISAGREE", "STRONGLY_DISAGREE")
_SURVEY_CONCIERGE_INTEREST = ("AGREE", "DISAGREE", "AGREE_PRIVATE")
_SURVEY_CONCIERGE_HELP = ("YES", "NO", "NOT_APPLICABLE")
_SURVEY_YES_NO = ("YES", "NO")


def _build_booking_survey_body(
    *,
    leg_id: str,
    booking_experience: str,
    response_time: str,
    concierge_interest: str,
    concierge_help: str,
    flight_itinerary_time: str,
    additional_feedback: str,
) -> dict[str, Any]:
    """Android BookingSurveyCreateRequestModel, in key order. Surveys are keyed by legId, never tripId."""
    return {
        "legId": leg_id,
        "bookingExperience": booking_experience,
        "responseTime": response_time,
        "conciergeInterest": concierge_interest,
        "conciergeHelp": concierge_help,
        "flightItineraryTime": flight_itinerary_time,
        "additionalFeedback": additional_feedback,
    }


@booking_app.command("survey")
def booking_survey(
    leg_id: str = typer.Option(..., "--leg-id", help="Completed-leg UUID"),
    booking_experience: str = typer.Option(..., "--booking-experience", help='"My concierge offered a seamless, personalized booking experience": strongly-agree | agree | disagree | strongly-disagree'),
    response_time: str = typer.Option(..., "--response-time", help='"My concierge responded in a timely fashion": strongly-agree | agree | disagree | strongly-disagree'),
    concierge_interest: str = typer.Option(..., "--concierge-interest", help='"My concierge took an interest in my reason for travel": agree | disagree | agree-private (I prefer to keep it private)'),
    concierge_help: str = typer.Option(..., "--concierge-help", help="Happy with catering/transportation pricing and options arranged by the concierge: yes | no | not-applicable"),
    itinerary_on_time: str = typer.Option(..., "--itinerary-on-time", help="Final itinerary received in a timely fashion: yes | no"),
    comments: Optional[str] = typer.Option(None, "--comments", help="Additional feedback"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Answer the post-booking concierge survey for one completed leg (POST /booking-survey/create)."""
    payload = _build_booking_survey_body(
        leg_id=leg_id,
        booking_experience=_enum(booking_experience, "--booking-experience", _SURVEY_AGREEMENT),
        response_time=_enum(response_time, "--response-time", _SURVEY_AGREEMENT),
        concierge_interest=_enum(concierge_interest, "--concierge-interest", _SURVEY_CONCIERGE_INTEREST),
        concierge_help=_enum(concierge_help, "--concierge-help", _SURVEY_CONCIERGE_HELP),
        flight_itinerary_time=_enum(itinerary_on_time, "--itinerary-on-time", _SURVEY_YES_NO),
        additional_feedback=(comments or "").strip(),
    )
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": "/booking-survey/create", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/booking-survey/create", payload), fmt, compact)


# ---------------------------------------------------------------------------
# trips — manifest & recent
# ---------------------------------------------------------------------------


def _build_manifest_send_body(recipients: list[str], trip_id: str) -> dict[str, Any]:
    """Android POST /trip/manifest/send body: {"recipients": [emails], "tripId"}."""
    return {"recipients": recipients, "tripId": trip_id}


@trips_app.command("manifest-send")
def trips_manifest_send(
    trip_id: str = typer.Option(..., "--trip-id", help="Trip UUID"),
    to: str = typer.Option(..., "--to", help="Comma-separated recipient email addresses"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm", help="Required before the email is sent"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Email the trip manifest to people you name (POST /trip/manifest/send)."""
    payload = _build_manifest_send_body(_email_list(to, "--to"), trip_id)
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
    """List recent leg searches (GET /leg/recent/list)."""
    token = get_api_token(username, password)
    response = api_get(token, "/leg/recent/list")
    _out(_response_items(response, limit), fmt, compact)


@trips_app.command("flight-get")
def trips_flight_get(
    flight_id: str = typer.Option(..., "--id", help="Booked-flight UUID"),
    probe: bool = typer.Option(False, "--probe/--no-probe", help="Override recent-booking-write cooldown"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Read one booked flight once (GET /my-flight/{id}); never polls."""
    _guard_booking_probe(probe)
    token = get_api_token(username, password)
    _out(api_get(token, f"/my-flight/{flight_id}"), fmt, compact)


@trips_app.command("leg-get")
def trips_leg_get(
    leg_id: str = typer.Option(..., "--id", help="Booked-leg UUID"),
    probe: bool = typer.Option(False, "--probe/--no-probe", help="Override recent-booking-write cooldown"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Read one booked leg once (GET /my-leg/{id}); never polls."""
    _guard_booking_probe(probe)
    token = get_api_token(username, password)
    _out(api_get(token, f"/my-leg/{leg_id}"), fmt, compact)


# ---------------------------------------------------------------------------
# quote — airport-nearest, saved-airports
# ---------------------------------------------------------------------------


@quote_app.command("airport-nearest")
def quote_airport_nearest(
    lat: float = typer.Option(..., "--lat", min=-90, max=90, help="Latitude"),
    lng: float = typer.Option(..., "--lng", min=-180, max=180, help="Longitude"),
    limit: int = typer.Option(5, "--limit", min=1, max=100),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Find airports nearest a coordinate (POST /airport/nearest)."""
    token = get_api_token(username, password)
    response = api_post(token, "/airport/nearest", {
        "latitude": lat,
        "longitude": lng,
    })
    _out(_response_items(response, limit), fmt, compact)


@quote_app.command("saved-airports")
def quote_saved_airports(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List saved/favourite airports using the app's saved airport filter."""
    token = get_api_token(username, password)
    response = api_post(token, "/airport", {
        "sort": [],
        "page": {"limit": 100, "offset": 0},
        "filter": {"saved": True},
    })
    _out(_response_items(response), fmt, compact)


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
    city: Optional[str] = typer.Option(None, "--city"),
    state_or_province: Optional[str] = typer.Option(None, "--state", "--state-or-province"),
    country_code: Optional[str] = typer.Option(None, "--country-code", help="Two-letter code such as ca or us"),
    session_token: Optional[str] = typer.Option(None, "--session-token"),
    limit: int = typer.Option(10, "--limit", min=1, max=100),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Address autocomplete (POST /address/autocomplete)."""
    input_value = query.strip()
    if not input_value:
        _die("--query cannot be empty.", EXIT_VALIDATION)
    payload: dict[str, Any] = {"input": input_value, "language": "en"}
    if city:
        payload["city"] = city.strip()
    if state_or_province:
        payload["stateOrProvince"] = state_or_province.strip()
    if session_token:
        payload["sessionToken"] = session_token.strip()
    if country_code:
        normalized_country = country_code.strip().lower()
        if not re.fullmatch(r"[a-z]{2}", normalized_country):
            _die("--country-code must be a two-letter code such as ca or us.", EXIT_VALIDATION)
        payload["countryCode"] = normalized_country
    token = get_api_token(username, password)
    response = api_post(token, "/address/autocomplete", payload)
    _out(_response_items(response, limit), fmt, compact)



def _build_address_create_body(
    *,
    passenger_id: str,
    label: str,
    street: str,
    street_2: str | None,
    country: str,
    city: str,
    state: str,
    zip_code: str,
) -> dict[str, Any]:
    """Android createAddressOwn(), keys in source order; addressLine2 only when given."""
    body: dict[str, Any] = {
        "passengerId": _required_text(passenger_id, "--passenger-id"),
        "label": _required_text(label, "--label"),
        "addressLine1": _required_text(street, "--street"),
    }
    if street_2 and street_2.strip():
        body["addressLine2"] = street_2.strip()
    body.update({
        "country": _required_text(country, "--country"),
        "city": _required_text(city, "--city"),
        "stateOrProvince": _required_text(state, "--state"),
        "zipOrPostal": _required_text(zip_code, "--zip"),
    })
    return body


@address_app.command("create")
def address_create(
    passenger_id: str = typer.Option(..., "--passenger-id", help="Saved passenger UUID the address belongs to"),
    street: str = typer.Option(..., "--street", help="Street address line 1"),
    street_2: Optional[str] = typer.Option(None, "--street-2", help="Apartment, suite, etc."),
    city: str = typer.Option(..., "--city"),
    state: str = typer.Option(..., "--state", help="State or province"),
    zip_code: str = typer.Option(..., "--zip", help="ZIP or postal code"),
    country: str = typer.Option(..., "--country"),
    label: str = typer.Option("Profile Address", "--label", help="Address label (app default: Profile Address)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Save an address on a passenger profile (POST /my-address/create)."""
    payload = _build_address_create_body(
        passenger_id=passenger_id, label=label, street=street, street_2=street_2,
        country=country, city=city, state=state, zip_code=zip_code,
    )
    if dry_run:
        _out({"dry_run": True, "endpoint": "/my-address/create", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_post(token, "/my-address/create", payload), fmt, compact)


# ---------------------------------------------------------------------------
# hours — exchange marketplace (estimate already at quote.hours-exchange)
# ---------------------------------------------------------------------------


@hours_app.command("estimate")
def hours_estimate(
    hours: Optional[float] = typer.Option(None, "--hours", min=0.01),
    action: Optional[str] = typer.Option(None, "--type", help="BUY or SELL"),
    account_aircraft_id: Optional[str] = typer.Option(None, "--account-aircraft-id", help="Defaults automatically when the account has one aircraft"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Estimate Hours Exchange value (GET /hour-exchange/estimate)."""
    token = get_api_token(username, password)
    query = _hours_estimate_query(token, account_aircraft_id, hours, action)
    resp = api_get(token, "/hour-exchange/estimate", query)
    _out(resp.get("data", resp), fmt, compact)


@hours_app.command("power")
def hours_power(
    account_aircraft_id: Optional[str] = typer.Option(None, "--account-aircraft-id", help="Defaults automatically when the account has one aircraft"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get Hours Exchange buying/selling power (GET /hour-exchange/power)."""
    token = get_api_token(username, password)
    query = {"accountAircraftId": _get_account_aircraft_id(token, account_aircraft_id)}
    resp = api_get(token, "/hour-exchange/power", query)
    _out(resp.get("data", resp), fmt, compact)


_HOURS_LISTING_ACTIONS = ("BUY", "SELL")


def _build_hours_listing_body(account_aircraft_id: str, action: str, hours: float) -> dict[str, Any]:
    """Android POST /hours-exchange-listing/create body: {"accountAircraftId", "action": BUY|SELL, "hours": double}."""
    return {"accountAircraftId": account_aircraft_id, "action": action, "hours": float(hours)}


@hours_app.command("listing-create")
def hours_listing_create(
    action: str = typer.Option(..., "--action", help="buy | sell"),
    hours: float = typer.Option(..., "--hours", min=0.01, help="Hours to list"),
    account_aircraft_id: Optional[str] = typer.Option(None, "--account-aircraft-id", help="Defaults automatically when the account has one aircraft"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List hours to buy or sell on the Hours Exchange (POST /hours-exchange-listing/create)."""
    listing_action = _enum(action, "--action", _HOURS_LISTING_ACTIONS)
    token = get_api_token(username, password)
    payload = _build_hours_listing_body(_get_account_aircraft_id(token, account_aircraft_id), listing_action, hours)
    if dry_run:
        _out({"dry_run": True, "method": "POST", "path": "/hours-exchange-listing/create", "payload": payload}, fmt, compact)
        return
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


@files_app.command("resolve")
def files_resolve(
    application_url: str = typer.Option(..., "--application-url", help="AirSprint application URL or path"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Resolve an application file URL using Android's bounded fallback logic."""
    raw_url = application_url.strip()
    parsed = urlparse(raw_url)
    application_path = (parsed.path or raw_url).lstrip("/")
    if not application_path:
        _die("--application-url must contain a non-empty path.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    direct = api_post(token, "/my-file", {
        "filter": {"path": application_path},
        "page": {"limit": 1, "offset": 0},
    })
    direct_items = _response_items(direct, 1)
    if direct_items and isinstance(direct_items[0], dict):
        resolved = direct_items[0].get("url") or direct_items[0].get("applicationUrl")
        if isinstance(resolved, str) and resolved:
            _out({"url": resolved, "source": "path", "path": application_path}, fmt, compact)
            return

    current_user = _response_data(api_get(token, "/me"))
    user_id = current_user.get("id") if isinstance(current_user, dict) else None
    if not isinstance(user_id, str) or not user_id:
        _die("Could not resolve the current AirSprint user ID.", EXIT_NOT_FOUND)
    image_types = ["image/jpeg", "image/png", "image/webp", "image/gif"]
    for role in ("user_picture", "default_file"):
        fallback = api_post(token, "/my-file", {
            "filter": {
                "userId": user_id,
                "role": role,
                "contentType": image_types,
            },
            "sort": [{"createdAt": "DESC"}],
            "page": {"limit": 1, "offset": 0},
        })
        fallback_items = _response_items(fallback, 1)
        if not fallback_items or not isinstance(fallback_items[0], dict):
            continue
        resolved = fallback_items[0].get("url") or fallback_items[0].get("applicationUrl")
        if isinstance(resolved, str) and resolved:
            _out({"url": resolved, "source": role, "path": application_path}, fmt, compact)
            return
    _die(f"No file URL resolved for {application_url}.", EXIT_NOT_FOUND)


@files_app.command("get")
def files_get(
    file_id: str = typer.Option(..., "--id", help="Private file UUID"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Get a private file record (GET /my-file/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/my-file/{file_id}"), fmt, compact)


@files_app.command("public-get")
def files_public_get(
    file_id: str = typer.Option(..., "--id", help="Public file UUID"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Get a public-file record (GET /file-public/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/file-public/{file_id}"), fmt, compact)


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


@content_app.command("faq-get")
def content_faq_get(
    faq_id: str = typer.Option(..., "--id", help="FAQ UUID"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Get one FAQ entry (GET /faq/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/faq/{faq_id}"), fmt, compact)


@content_app.command("faq-categories")
def content_faq_categories(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List FAQ categories (POST /faq-category)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/faq-category", {
        "sort": [],
        "page": {"limit": 100, "offset": 0},
    })
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


@content_app.command("policy-get")
def content_policy_get(
    policy_id: str = typer.Option(..., "--id", help="Policy UUID"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Get one policy (GET /policy/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/policy/{policy_id}"), fmt, compact)


@content_app.command("policy-categories")
def content_policy_categories(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """List policy categories (POST /policy-category)."""
    token = get_api_token(username, password)
    resp = api_post(token, "/policy-category", {
        "sort": [],
        "page": {"limit": 100, "offset": 0},
    })
    _out(resp.get("data", {}).get("items", resp.get("data", resp)), fmt, compact)


@content_app.command("system-notice")
def content_system_notice(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get current system notice (POST /system-notice)."""
    token = get_api_token(username, password)
    response = api_post(token, "/system-notice", {
        "sort": [],
        "page": {"limit": 90, "offset": 0},
    })
    _out(_response_items(response), fmt, compact)


@content_app.command("concierge")
def content_concierge(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get concierge contact info (POST /concierge)."""
    token = get_api_token(username, password)
    response = api_post(token, "/concierge", {
        "sort": [],
        "page": {"limit": 100, "offset": 0},
    })
    _out(_response_items(response), fmt, compact)


@content_app.command("get")
def content_get(
    content_id: str = typer.Option(..., "--id", help="Content UUID"),
    username: Optional[str] = Username,
    password: Optional[str] = Password,
    fmt: str = Format,
    compact: bool = Compact,
):
    """Get one content record (GET /content/{id})."""
    token = get_api_token(username, password)
    _out(api_get(token, f"/content/{content_id}"), fmt, compact)


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
    payload_value: str = typer.Option(..., "--invite", help="Invitation code from the AirSprint connection link"),
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
# user — password and avatar (additional)
# ---------------------------------------------------------------------------



def _build_change_password_body(current_password: str, new_password: str) -> dict[str, Any]:
    """Android changeMyPassword(): {"currentPassword", "newPassword"}."""
    if not current_password:
        _die("--current-password is required.", EXIT_VALIDATION)
    if current_password == new_password:
        _die("--new-password must differ from --current-password.", EXIT_VALIDATION)
    return {"currentPassword": current_password, "newPassword": _new_password(new_password)}


@user_app.command("change-password")
def user_change_password(
    current_password: str = typer.Option(..., "--current-password", help="Password used today"),
    new_password: str = typer.Option(..., "--new-password", help="New password (at least 8 characters)"),
    confirm: bool = typer.Option(False, "--confirm", help="Required — this changes the login password"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Change the login password. Requires --confirm."""
    payload = _build_change_password_body(current_password, new_password)
    if not confirm:
        _die("--confirm required to actually change the password.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_post(token, "/my-user/change-password", payload), fmt, compact)


@user_app.command("avatar")
def user_avatar(
    user_id: str = typer.Option(..., "--id", help="User ID"),
    output: str = typer.Option("-", "--output", "-o", help="Output file path or - for metadata only"),
    username: Optional[str] = Username, password: Optional[str] = Password,
):
    """Get Android's avatar URL and optionally download the presigned file."""
    token = get_api_token(username, password)
    envelope = api_get(token, f"/my-user/avatar/{user_id}")
    avatar_url = _manifest_url(envelope)
    if not avatar_url:
        _die(f"No avatar URL returned for user {user_id}.", EXIT_NOT_FOUND)
    if output == "-":
        _out({"url": avatar_url, "message": "Use --output FILE to download the avatar."})
        return
    content = _download_bytes(avatar_url)
    Path(output).write_bytes(content)
    _out({"message": f"Saved to {output}", "size_bytes": len(content)})


# ---------------------------------------------------------------------------
# messages — notification settings
# ---------------------------------------------------------------------------


@messages_app.command("settings")
def messages_settings(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Get notification settings (GET /my-notification-settings)."""
    token = get_api_token(username, password)
    _out(api_get(token, "/my-notification-settings"), fmt, compact)


@messages_app.command("settings-update")
def messages_settings_update(
    on: Optional[str] = NotificationsOn,
    off: Optional[str] = NotificationsOff,
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Turn notification toggles on/off, e.g. --on weeklyDigest --off airsprintPromotions."""
    _notification_settings_update(on, off, dry_run, username, password, fmt, compact)


@messages_app.command("update")
def messages_update(
    ids: str = typer.Option(..., "--ids", help="Comma-separated notification IDs from `messages list`"),
    read: str = typer.Option("yes", "--read", help='"yes" marks them read, "no" marks them unread'),
    dry_run: bool = typer.Option(False, "--dry-run"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Mark in-app messages read or unread."""
    payload = {"ids": _csv(ids, "--ids", required=True), "isRead": _yes_no(read, "--read")}
    if dry_run:
        _out({"dry_run": True, "method": "PATCH", "path": "/my-notifications/update", "payload": payload}, fmt, compact)
        return
    token = get_api_token(username, password)
    _out(api_patch(token, "/my-notifications/update", payload), fmt, compact)


# ---------------------------------------------------------------------------
# auth — 2FA & password reset
# ---------------------------------------------------------------------------


@auth_app.command("2fa-setup")
def auth_2fa_setup(
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Begin 2FA setup with Android's empty JSON payload."""
    token = get_api_token(username, password)
    _out(api_post(token, "/user/2fa/setup", {}), fmt, compact)



_TWO_FA_CODE_RE = re.compile(r"^\d{6}$")


def _two_fa_code(value: str) -> str:
    code = value.strip().replace(" ", "")
    if not _TWO_FA_CODE_RE.match(code):
        _die("--code must be the 6-digit code shown by the authenticator app.", EXIT_VALIDATION)
    return code


def _build_2fa_verify_body(code: str) -> dict[str, Any]:
    """Android User2faVerifyRequest.toJson(): {"token"}."""
    return {"token": _two_fa_code(code)}


def _build_2fa_sign_in_body(user_id: str, code: str) -> dict[str, Any]:
    """Android User2faSignInRequest.toJson(): {"userId", "token"} in that order."""
    return {"userId": _required_text(user_id, "--user-id"), "token": _two_fa_code(code)}


def _build_reset_confirm_body(token: str, new_password: str) -> dict[str, Any]:
    """Android setNewPassword(): {"token", "newPassword"}."""
    return {
        "token": _required_text(token, "--token"),
        "newPassword": _new_password(new_password),
    }


def _new_password(value: str) -> str:
    if len(value) < 8:
        _die("--new-password must be at least 8 characters.", EXIT_VALIDATION)
    return value


@auth_app.command("2fa-verify")
def auth_2fa_verify(
    code: str = typer.Option(..., "--code", help="6-digit code from the authenticator app"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Confirm the authenticator code that completes 2FA setup."""
    payload = _build_2fa_verify_body(code)
    token = get_api_token(username, password)
    _out(api_post(token, "/user/2fa/verify", payload), fmt, compact)



@auth_app.command("2fa-sign-in")
def auth_2fa_sign_in(
    user_id: str = typer.Option(..., "--user-id", help="User UUID returned by the first sign-in step"),
    code: str = typer.Option(..., "--code", help="6-digit code from the authenticator app"),
    username: Optional[str] = Username,
    fmt: str = Format, compact: bool = Compact,
):
    """Finish 2FA sign-in and cache the session. Use the email from the first step."""
    payload = _build_2fa_sign_in_body(user_id, code)
    response = _http("POST", f"{API_BASE_URL}/user/2fa/sign-in",
                     headers={"Content-Type": "application/json", "Accept": "application/json"},
                     data=json.dumps(payload).encode("utf-8"))
    data = _response_data(response)
    token = data.get("authToken") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip():
        _die("No authToken in 2FA response; cached session was not replaced.", EXIT_AUTH)
    email = username or os.environ.get("AIRSPRINT_USERNAME", "")
    _save_api_token(token, email)
    _out({"authenticated": True, "email": email, "token_cached": True}, fmt, compact)


@auth_app.command("2fa-disable")
def auth_2fa_disable(
    confirm: bool = typer.Option(False, "--confirm", help="Required — disabling 2FA reduces account security"),
    username: Optional[str] = Username, password: Optional[str] = Password,
    fmt: str = Format, compact: bool = Compact,
):
    """Disable 2FA (POST /user/2fa/disable). Requires --confirm."""
    if not confirm:
        _die("--confirm required to disable 2FA.", EXIT_VALIDATION)
    token = get_api_token(username, password)
    _out(api_post(token, "/user/2fa/disable", {}), fmt, compact)


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
    token: str = typer.Option(..., "--token", help="Reset token from the password-reset email link"),
    new_password: str = typer.Option(..., "--new-password", help="New password (at least 8 characters)"),
    fmt: str = Format, compact: bool = Compact,
):
    """Set a new password with the token from the reset email. No auth required."""
    payload = _build_reset_confirm_body(token, new_password)
    _out(_http("POST", f"{API_BASE_URL}/user/reset-password",
               headers={"Content-Type": "application/json", "Accept": "application/json"},
               data=json.dumps(payload).encode("utf-8")), fmt, compact)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


@agent_app.command("commands")
def agent_commands(group: Optional[str] = typer.Option(None, "--group", help="Limit discovery to one command group"),
                   query: Optional[str] = typer.Option(None, "--query", help="Search command names and descriptions")):
    """List public commands and their effects without credentials or network calls."""
    from airsprint_agent import CommandRegistry
    print(json.dumps({"status": "ok", "data": CommandRegistry(sys.modules[__name__]).catalog(group, query)}, separators=(",", ":")))


@agent_app.command("describe")
def agent_describe(command: str = typer.Option(..., "--command", help="Exact command, e.g. 'customs prepare'")):
    """Return one command's typed form, required fields, effects and workflow guidance."""
    from airsprint_agent import CommandRegistry
    print(json.dumps({"status": "ok", "data": CommandRegistry(sys.modules[__name__]).describe(command)}, separators=(",", ":")))


@agent_app.command("serve")
def agent_serve():
    """Run typed JSON-line requests in one process. One request and result per line."""
    from airsprint_agent import serve_jsonl
    serve_jsonl(sys.modules[__name__])


def _event_store():
    from airsprint_events import EventStore, default_db_path
    owner = os.getenv("AIRSPRINT_USERNAME", "").strip().lower() or "local"
    return EventStore(default_db_path(), owner)


@agent_app.command("operation")
def agent_operation(operation_key: str = typer.Option(..., "--operation-key", help="Stable key used for the original write")):
    """Inspect a write receipt without another AirSprint request."""
    print(json.dumps({"status": "ok", "data": _event_store().operation(operation_key)}))


@events_app.command("list")
def events_list(after: int = typer.Option(0, "--after", min=0), limit: int = typer.Option(50, "--limit", min=1, max=100)):
    """Read observed events after a local sequence number."""
    print(json.dumps({"status": "ok", "data": _event_store().list_events(after, limit)}))


@events_app.command("get")
def events_get(source: str = typer.Option(..., "--source", help="notifications or trips"), resource_id: str = typer.Option(..., "--id")):
    """Read an observed record without refreshing AirSprint."""
    if source not in {"notifications", "trips"}:
        _die("Source must be notifications or trips.")
    print(json.dumps({"status": "ok", "data": _event_store().record(source, resource_id)}))


@events_app.command("status")
def events_status():
    """Show collection timestamps, active subscriptions and queued deliveries."""
    print(json.dumps({"status": "ok", "data": _event_store().status()}))


@events_app.command("collect")
def events_collect():
    """Collect one complete snapshot from safe lists; the first snapshot is silent."""
    from airsprint_events import collect
    print(json.dumps({"status": "ok", "data": collect(sys.modules[__name__], _event_store())}))


@mcp_app.command("serve")
def mcp_serve(
    transport: str = typer.Option("stdio", "--transport", help="stdio or http (MCP 2026-07-28)"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    interval: int = typer.Option(60, "--interval", min=60, help="Seconds between safe event collection cycles"),
):
    """Serve typed tools and durable event subscriptions for one authenticated owner."""
    from airsprint_mcp import serve
    serve(sys.modules[__name__], transport=transport, host=host, port=port, interval=interval)


if __name__ == "__main__":
    from airsprint_agent import cli_main
    sys.exit(cli_main(sys.modules[__name__]))
