# AirSprint CLI

Agent-safe command-line access to the current `api.airsprint.com` owner API,
audited against AirSprint Android 6.1.12 (version code 135).

The offline source audit found 111 Android repository calls and 105 unique
method/route contracts; the complete matrix and APK checksum are in
[ANDROID_CONTRACT.md](ANDROID_CONTRACT.md).

The [2026-10-01 audit report](AUDIT_2026-10-01.md) records the source evidence,
corrections, offline validation and remaining limits. Route regression tests
compare the implementation with the extracted Android inventory.

Every write is a **form**: agents answer questions with typed options and the
CLI builds the exact Android 6.1.12 request. No public command accepts an API
request body — only IDs (passenger, passport, pet, flight, trip, leg,
airport, aircraft) cross the boundary. This keeps agents away from the wire
format and lets the CLI add guard rails without changing how it is driven.

The canonical agent instructions are in [SKILL.md](SKILL.md) and can also be
printed directly:

```bash
python3 scripts/airsprint_cli.py --skill
```

## Agents and MCP

Start with `agent commands --group GROUP` and `agent describe --command
'GROUP COMMAND'` to load only the form you need. `agent serve` keeps one
process running for typed JSON-line requests and durable write receipts.
CLI failures now use structured JSON, including unknown-option errors.

`mcp serve` exposes the same validated forms through MCP 2.0 and adds signed
webhook events for notifications, trip changes, customs submissions and agent
operations. Event collection reads safe lists and never polls booked-trip
detail endpoints. See [the runtime and MCP guide](AGENT_RUNTIME.md) for setup,
operation keys, event filters, limits and verification.

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
`--compact` for token-efficient JSON.

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
the current list with `GET /my-leg/{id}`, preserves everyone not explicitly
removed, and sends saved passenger UUIDs rather than `legPassenger.id`:

```bash
python3 scripts/airsprint_cli.py leg update-passengers \
  --leg-id LEG_UUID --add SAVED_PAX_UUID --dry-run
python3 scripts/airsprint_cli.py leg update-passengers \
  --leg-id LEG_UUID --add SAVED_PAX_UUID --confirm
```

The dry run shows `kept`, `added`, and `dropped`. If any current passenger
cannot be safely mapped, the CLI refuses the PATCH.

Android's required-information form for a booked leg is also available:
passports, customs declarations, destination address, seats, pets, baggage,
catering, ground transportation, and note. Passenger answers reuse the same
full-list merge protection (one guarded `GET /my-leg/{id}`, one
`PATCH /leg/{id}/required-info`, no read-back):

```bash
python3 scripts/airsprint_cli.py leg update-required-info \
  --leg-id LEG_UUID --passport SAVED_PAX_UUID=PASSPORT_UUID \
  --destination-street "1 Main St" --destination-city "New York" \
  --destination-state NY --destination-zip 10001 --dry-run
python3 scripts/airsprint_cli.py leg update-required-info \
  --leg-id LEG_UUID --pets PET_UUID --baggage Suitcase=2 --confirm
```

### Booking a trip

`booking create` is the app's booking form. Legs are `FROM>TO@YYYY-MM-DDTHH:MM`
(ICAO or airport UUID; local time at the departure airport, as typed in the
app). The account is implicit in the token; nothing account-related is sent.

```bash
python3 scripts/airsprint_cli.py booking create \
  --leg "CYUL>KTEB@2026-09-01T09:00" --leg "KTEB>CYUL@2026-09-03T17:30" \
  --passengers SAVED_PAX_UUID_1,SAVED_PAX_UUID_2 \
  --baggage "Golf bag=2" --baggage Suitcase=2 \
  --catering yes --catering-request "Light lunch" \
  --destination-street "1 Main St" --destination-city "New York" \
  --destination-state NY --destination-zip 10001 \
  --dry-run
```

Guard rails, all enforced before the booking request is sent (the passport
and destination checks first read airports and passengers; nothing is written):

- baggage must be answered (`--baggage NAME=QTY` repeated, or `--baggage none`);
- a trip that crosses a border needs a passport on file for every passenger
  (the app's default is each passenger's first passport; override with
  `--passport PAX_UUID=PASSPORT_UUID`);
- US-touching and international trips need a destination address (hotel,
  residence, ...), copied to every passenger on every leg, including a return
  to Canada;
- airport countries come from the local cache (`cache refresh`);
  `--us-touching/--not-us-touching` and `--international/--domestic` are the
  explicit overrides when the cache cannot answer.

`booking empty-leg`, `booking shared-flight`, and `booking lock` use the same
vocabulary for flights found with `explore flights`.

### Passports

`passport list` reads the exact embedded passport records returned by
`POST /my-passenger`, because the API's `POST /my-passport` collection route
returns 404. Output preserves AirSprint's `nationality` and
`issuingAuthority` fields; it does not infer or rename `nationality` as a
passport `country` field.

Passport creation requires `--file` and completes the Android create → upload
initialization → one-shot storage POST → attachment workflow. This is a CLI
policy: AirSprint says the passport photo is not mandatory, but highly
recommends it to avoid customs-clearance delays. JPEG, PNG, and PDF scans up to
20 MiB are accepted and their file signatures are checked before any API
write. For real creation, `--confirm` is also required.

The command normalizes `dateOfBirth` and `expirationDate` to epoch
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
  --passenger-id SAVED_PAX_UUID --passport-number AB123456 \
  --date-of-birth 1980-01-02 --expiration-date 2031-03-04 \
  --nationality CA --issuing-authority "QUÉBEC" \
  --file passport.pdf --timezone America/Toronto --dry-run
python3 scripts/airsprint_cli.py passport create \
  --passenger-id SAVED_PAX_UUID --passport-number AB123456 \
  --date-of-birth 1980-01-02 --expiration-date 2031-03-04 \
  --nationality CA --issuing-authority "QUÉBEC" \
  --file passport.pdf --timezone America/Toronto --confirm
python3 scripts/airsprint_cli.py passport make-primary \
  --passenger-id SAVED_PAX_UUID --passport-id PASSPORT_UUID --confirm
```

`passenger create` takes the same fields as the app's "Add New Person" form and
requires the form's yes/no answer as `--save-profile`; the CLI builds the
Android-exact body. First name, last name, and gender (`male`, `female`, `x`)
are mandatory, as in the app:

```bash
python3 scripts/airsprint_cli.py passenger create \
  --first-name Jean --last-name Tremblay --gender male --category adult \
  --save-profile no --dry-run   # "no" = this booking only, hidden from Saved Passengers
python3 scripts/airsprint_cli.py passenger update --id SAVED_PAX_UUID --email jean@example.com --dry-run
```

Working deletions use HTTP DELETE:

```bash
python3 scripts/airsprint_cli.py passenger delete --id SAVED_PAX_UUID --confirm
python3 scripts/airsprint_cli.py passport delete --id PASSPORT_UUID --confirm
```

The old `POST .../{id}/delete` routes are not used.

For an already-created passport, complete document uploads use Android's three
steps: API initialization, one presigned multipart storage POST, then API
attachment. Files are capped at the same 20 MiB limit:

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

Prepare local drafts for the **actual return leg arriving in Canada**. No
form or link is submitted during preparation. Missing answers remain visible.
Draft names use `BOOKING-firstname-lastname.json`; group drafts include every
full name separated by `--`. Omit `--draft` on creation to generate this name.
First-name-only aliases and arbitrary filenames are rejected. The example
below assumes booking code `RETURN`.

```bash
python3 scripts/airsprint_cli.py customs prepare \
  --leg-id RETURN_LEG_UUID --passengers "Jane Doe,John Roe" --draft RETURN-jane-doe--john-roe.json
python3 scripts/airsprint_cli.py customs review --draft RETURN-jane-doe--john-roe.json
# Fill/correct answers using customs prepare --draft RETURN-jane-doe--john-roe.json [typed options].
# After showing and confirming each person's information individually:
python3 scripts/airsprint_cli.py customs certify --draft RETURN-jane-doe--john-roe.json --passenger "Jane Doe" --approve yes
python3 scripts/airsprint_cli.py customs certify --draft RETURN-jane-doe--john-roe.json --passenger "John Roe" --approve yes
# On the owner's final instruction:
python3 scripts/airsprint_cli.py customs submit --draft RETURN-jane-doe--john-roe.json --confirm
python3 scripts/airsprint_cli.py customs status --link-id RETURN_LINK_UUID
```

One form per person; `--family yes` groups at most four family members living
at the same address, with individual certifications still required. Edits
clear certifications. Submission checks the same itinerary, passport and
address data and reads `customsDeclarationAlreadySubmitted` for each person.
Already-submitted, unknown or uncertain status blocks another submission.
A private journal also prevents replay from a new draft after an interrupted
request. No retry or automatic read-back occurs.

`customs create` now only prepares a local draft. Post-submission date edits
are retired. See [the full workflow](SKILL.md#canadian-customs-prepare-review-certify-each-person-submit).

### Trip passport audit

`leg audit-travel-info --leg-id ID` shows the actual trip-profile IDs,
passport selections, attached scans, addresses and customs status. Use
`passport list --passenger-id ID` for unsaved guests. A same-named saved
profile does not establish that its passport is attached to the booked leg.
PDF, JPEG and PNG remain supported.

## Other current groups

| Group | Purpose |
|---|---|
| `auth` | Login, local status, one-shot live verification, logout, 2FA, reset |
| `device` | Android-compatible notification-token registration and deletion |
| `user`, `account` | Profiles, preferences, owner accounts and access users |
| `booking` | Prep, create, cancel, empty/shared flights, holds, post-booking survey |
| `explore` | Empty and shared flights; use `flights --compact` |
| `network` | Current connections and sharing groups |
| `passenger`, `passport`, `pet` | Saved traveler data and documents |
| `customs` | Canadian drafts, individual certification, submission and status |
| `quote`, `hours` | Quotes, airports, aircraft, Hours Exchange |
| `messages`, `feedback` | Notifications and the post-flight questionnaire |
| `files`, `content` | File resolution, FAQ, policy, system and concierge content |

Retired prod2, follower/social, duplicate booking, unsupported invoice,
preflight, message-delete, and passport-update commands are intentionally not
present. A hidden `raw` group exists for maintainers only (gated by
`--allow-raw`); it is not part of the agent surface and is never documented for
agents.

## Verification

```bash
python3 -m unittest discover -s scripts -p 'test_*.py' -v
python3 -m py_compile scripts/*.py
ruff check scripts
git diff --check
```

The test suite includes a guard that fails if any public command grows a
`--body`, `--json`, `--payload`, or `--options` option, and exact-body tests
that compare each form's request with the payload models decompiled from
Android 6.1.12.
