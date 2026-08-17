# AirSprint CLI

Agent-safe command-line access to the current `api.airsprint.com` owner API,
audited against AirSprint Android 6.1.4 (version code 127).

The offline source audit found 111 Android repository calls and 104 unique
method/route contracts. All 104 have CLI coverage; the complete matrix and APK
checksum are in [ANDROID_CONTRACT.md](ANDROID_CONTRACT.md).

The canonical agent instructions are in [SKILL.md](SKILL.md) and can also be
printed directly:

```bash
python3 scripts/airsprint_cli.py --skill
```

## Setup

Python dependencies are only `typer` and `truststore`.

```bash
python3 -m pip install typer truststore
export AIRSPRINT_USERNAME="owner@example.com"
export AIRSPRINT_PASSWORD="..."
export AIRSPRINT_TIMEZONE="America/Toronto"
python3 scripts/airsprint_cli.py --help
```

Tokens are stored with mode 0600 at `~/.airsprint_api_token.json`. Airport and
aircraft reference data is cached at `~/.airsprint_cache.json` for seven days;
owner accounts are cached for 15 minutes and invalidated when the login changes.
`trips show` converts manifests with AnyDoc first, preserving useful Markdown
structure. If AnyDoc is unavailable or cannot convert a PDF, it automatically
falls back to Poppler's `pdftotext` (`brew install poppler`).

JSON is the default output. Use `--format human` for readable text or
`--compact` for token-efficient JSON. `trips list --json` is accepted as an
explicit alias even though JSON is already the default.

## Live-booking safety

Booked-trip and booked-leg GET/PATCH requests can notify the owner's iPhone.
The CLI therefore:

- never retries a trip/leg GET or any PATCH;
- never polls or automatically reads back after a write;
- records live writes and blocks accidental probes for eight seconds;
- requires `--confirm` for the high-risk mutation paths;
- prints full mutation plans through `--dry-run`;
- retries only the first safe API read, once, and only for
  `WRONG_VERSION_NUMBER`.

Use `--probe` only to intentionally override the short post-write guard.

## Performance and cache

TLS setup is lazy, so offline commands such as `--help`, `--skill`, `auth
status`, and `cache status` do not load the network trust stack. Parsed cache
data and airport-ID indexes are reused within a process. State files are
written atomically with private permissions.

`summary`, `booking info`, and `explore counts` run only their independent
catalog/list reads concurrently. Booked-trip and booked-leg GET/PATCH calls are
never sent through that concurrent path.

```bash
python3 scripts/airsprint_cli.py cache status --compact
python3 scripts/airsprint_cli.py cache refresh
python3 scripts/airsprint_cli.py summary --compact
```

`cache refresh` updates accounts, airports, general aircraft, and the owner's
aircraft, then persists the complete mirror once.

## Critical workflows

### Trip operations

```bash
python3 scripts/airsprint_cli.py trips list --upcoming --compact
python3 scripts/airsprint_cli.py trips get --id TRIP_UUID
python3 scripts/airsprint_cli.py trips show --id TRIP_UUID --compact
python3 scripts/airsprint_cli.py trips tripsheet --id TRIP_UUID -o trip.pdf
```

`trips get` performs one trip GET. `trips show` performs one trip GET and one
manifest GET, then merges tail numbers, crew lines, FBO lines, passenger lines,
and full manifest text. Neither command polls or retries live booking calls.

### Safe full-list passenger merge

`PATCH /leg/{id}` replaces the entire passenger list. This command first reads
the current list, preserves everyone not explicitly removed, and sends saved
passenger UUIDs rather than `legPassenger.id`:

```bash
python3 scripts/airsprint_cli.py leg update-passengers \
  --leg-id LEG_UUID --add SAVED_PAX_UUID --dry-run
python3 scripts/airsprint_cli.py leg update-passengers \
  --leg-id LEG_UUID --add SAVED_PAX_UUID --confirm
```

The dry run shows `kept`, `added`, and `dropped`. If any current passenger
cannot be safely mapped, the CLI refuses the PATCH.

Android's broader required-information PATCH is also available. When
`options.passengers` is supplied, it uses the same full-list merge protection:

```bash
python3 scripts/airsprint_cli.py leg update-required-info \
  --leg-id LEG_UUID --body "$REQUIRED_INFO_JSON" --dry-run
python3 scripts/airsprint_cli.py leg update-required-info \
  --leg-id LEG_UUID --body "$REQUIRED_INFO_JSON" --confirm
```

It accepts only fields emitted by Android 6.1.4 and performs at most one
guarded leg GET followed by one PATCH, with no read-back.

### US bookings

The account is implicit in the token; top-level `accountId` is rejected. A
US-touching booking must include a complete destination address. The CLI copies
the address to every passenger on every leg, including a return to Canada.

```bash
python3 scripts/airsprint_cli.py booking create \
  --body "$BOOKING_JSON" \
  --destination-address '{"street":"1 Main St","city":"New York","state":"NY","zip":"10001","country":"United States"}' \
  --dry-run
```

Airport-country detection comes from the local cache. Run `cache refresh` when
needed. `--us-touching` and `--not-us-touching` are explicit overrides.

### Passports

`passport list` reads the exact embedded passport records returned by
`POST /my-passenger`, because the API's `POST /my-passport` collection route
returns 404. Output preserves AirSprint's `nationality` and
`issuingAuthority` fields; it does not infer or rename `nationality` as a
passport `country` field.

Passport creation normalizes `dateOfBirth` and `expirationDate` to epoch
milliseconds. For timezone-less dates, pass `--tz` (or set
`AIRSPRINT_TIMEZONE`): Android converts `yyyy-MM-dd` from device-local midnight,
and the CLI deliberately matches that behavior. Passport dates are **not**
fixed to AirSprint HQ/Calgary (`America/Edmonton`): use the timezone configured
on the Android device at the time of entry, such as `America/Toronto` or
`America/Edmonton`. The API may return stored values in seconds. Passport PATCH
for number/date fields is not advertised because those updates do not persist.
The Android-supported authority-only update is available as a single PATCH:

```bash
python3 scripts/airsprint_cli.py passport update-authority \
  --id PASSPORT_UUID --authority 'QUÉBEC' --dry-run
python3 scripts/airsprint_cli.py passport update-authority \
  --id PASSPORT_UUID --authority 'QUÉBEC' --confirm
```

The app displays the first entry in `passportIds`; `selectedPassportId` does
not persist:

```bash
python3 scripts/airsprint_cli.py passport create \
  --passenger-id SAVED_PAX_UUID --body "$PASSPORT_JSON" \
  --tz America/Toronto --dry-run
python3 scripts/airsprint_cli.py passport make-primary \
  --passenger-id SAVED_PAX_UUID --passport-id PASSPORT_UUID --confirm
```

Working deletions use HTTP DELETE:

```bash
python3 scripts/airsprint_cli.py passenger delete --id SAVED_PAX_UUID --confirm
python3 scripts/airsprint_cli.py passport delete --id PASSPORT_UUID --confirm
```

The old `POST .../{id}/delete` routes are not used.

Complete document uploads use Android's three steps: API initialization, one
presigned multipart storage POST, then API attachment. Files are capped at the
same 20 MiB limit:

```bash
python3 scripts/airsprint_cli.py passport upload-document \
  --id PASSPORT_UUID --file passport.pdf --dry-run
python3 scripts/airsprint_cli.py passport upload-document \
  --id PASSPORT_UUID --file passport.pdf --confirm
python3 scripts/airsprint_cli.py pet upload-document \
  --id PET_UUID --file vaccination.pdf \
  --document-type vaccinationDocument --confirm
```

### Canadian customs

```bash
python3 scripts/airsprint_cli.py customs create \
  --booking BOOKING_CODE \
  --passengers "Jane Doe,John Doe" \
  --purpose PLEASURE \
  --description "Family visit" \
  --dry-run
```

The command resolves passenger names to leg-passenger UUIDs, defaults the one
customs `date` to the outbound departure leaving Canada, and creates one form
per ID in a single request. `customs list` sends only `page` and `filter`; it
never sends the invalid `sort`. Fix dates with:

```bash
python3 scripts/airsprint_cli.py customs update-date \
  --id DECLARATION_UUID --date 2026-09-01T14:00:00Z --confirm
```

Certification/signature is not in the API and must still be completed in the
AirSprint app.

## Other current groups

| Group | Purpose |
|---|---|
| `auth` | Login, local status, one-shot live verification, logout, 2FA, reset |
| `device` | Android-compatible notification-token registration and deletion |
| `user`, `account` | Profiles, preferences, owner accounts and access users |
| `booking` | Prep, create, cancel, empty/shared flights, holds |
| `explore` | Empty and shared flights; use `flights --compact` |
| `network` | Current connections and sharing groups |
| `passenger`, `passport`, `pet` | Saved traveler data and documents |
| `customs` | Canadian declarations and date correction |
| `quote`, `hours` | Quotes, airports, aircraft, Hours Exchange |
| `messages`, `feedback` | Notifications and feedback |
| `files`, `content` | File resolution, FAQ, policy, system and concierge content |
| `raw` | Current API escape hatches with PATCH/DELETE safeguards |

Retired prod2, follower/social, duplicate booking, unsupported invoice,
preflight, message-delete, and passport-update commands are intentionally not
present.

## Verification

```bash
python3 -m unittest discover -s scripts -p 'test_*.py' -v
python3 -m py_compile scripts/airsprint_cli.py scripts/test_airsprint_cli.py
ruff check scripts/airsprint_cli.py scripts/test_airsprint_cli.py
git diff --check
```
