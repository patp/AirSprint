---
name: airsprint-cli
description: Use the local AirSprint owner CLI for trips, booking, passengers, passports, Canadian customs, My Network, hours exchange, quotes, and current Android-app-backed operations. Apply the live-booking no-probe rules before any trip or leg access.
---

# AirSprint CLI agent guide

Use this CLI for AirSprint owner operations against `https://api.airsprint.com/api`.
It follows the current AirSprint Android 6.1.4 API. Retired prod2, follower, and
known-broken passport PATCH commands are intentionally absent.

## Invoke

```bash
python3 /Users/mb/src/AirSprintCLI/scripts/airsprint_cli.py --help
python3 /Users/mb/src/AirSprintCLI/scripts/airsprint_cli.py --skill
python3 /Users/mb/src/AirSprintCLI/scripts/airsprint_cli.py GROUP COMMAND [OPTIONS]
```

Python dependencies are only `typer` and `truststore`. For manifest conversion,
`trips show` prefers the `anydoc` executable and falls back automatically to
Poppler's system `pdftotext`; these are not Python dependencies.

Credentials come from `AIRSPRINT_USERNAME` and `AIRSPRINT_PASSWORD`, or the
`--username` and `--password` options. The token is stored with mode 0600 in
`~/.airsprint_api_token.json`. Set `AIRSPRINT_TIMEZONE` for local date input.

JSON is the default output. Do not add `--json` except on `trips list`, where it
is accepted as an explicit alias. Use `--format human` for human output and
`--compact` for token-efficient JSON.

## Efficient agent usage

- Prefer `summary --compact` for dashboard context instead of making several
  separate commands.
- Use `cache status --compact` before refreshing reference data. `cache refresh`
  updates accounts, airports, aircraft, and owner aircraft in one pass.
- Airport and aircraft reference data has a seven-day TTL. Accounts have a
  15-minute TTL and owner-specific sections are invalidated when login changes.
- `summary`, `booking info`, and `explore counts` may parallelize independent
  catalog/list reads only. This optimization never applies to a booked-trip or
  booked-leg GET/PATCH.
- Offline commands initialize no TLS state. `--skill` uses a dedicated fast
  path, so it is safe and inexpensive to inspect before an operation.

## Non-negotiable live-booking safety

- A booked-trip or booked-leg GET/PATCH can notify the owner's iPhone.
- Never retry, poll, loop, or automatically verify a live booking request.
- A live booking PATCH is sent exactly once and the command stops.
- Never perform a read-back after a write. Wait at least 8 seconds first.
- The CLI records live booking writes in `~/.airsprint_last_booking_write.json`.
- `trips get`, `trips show`, `trips tripsheet`, `leg update-passengers`, and
  high-level `customs create` default to no probe during that cooldown.
- Only use `--probe` to override the cooldown when the user explicitly wants an
  immediate read and understands that it can notify the app.
- The first safe API read may retry once only for `WRONG_VERSION_NUMBER`.
  Booking GETs, PATCHes, and all writes never retry.
- Always use `--dry-run` before a mutation and preserve the dry-run output.
- Use `--confirm` where required. Do not add a follow-up GET after success.

## Trips

```bash
# JSON is already the default; --json is an accepted explicit alias here.
python3 scripts/airsprint_cli.py trips list --upcoming --compact
python3 scripts/airsprint_cli.py trips list --past --limit 20 --json

# One trip GET. During the post-write cooldown, this exits without probing.
python3 scripts/airsprint_cli.py trips get --id BOOKING_OR_TRIP_UUID

# One trip GET plus one manifest GET; no retries or polling. Extracts tail,
# crew, FBO, passenger lines, and full manifest text.
python3 scripts/airsprint_cli.py trips show --id TRIP_UUID --compact

# Download or obtain the manifest PDF URL.
python3 scripts/airsprint_cli.py trips tripsheet --id TRIP_UUID --output trip.pdf
```

Prefer a trip UUID for `get`, `show`, or `tripsheet`. A booking code requires a
bounded `/my-leg` lookup first. `trips show` is the operations view; `trips get`
returns only the API trip object.

## Updating passengers on a booked leg

`PATCH /leg/{id}` replaces `options.passengers` completely. Never construct a
one-passenger payload. The identifier sent as each passenger `id` must be the
saved passenger UUID, never `legPassenger.id`.

```bash
# Makes one GET and prints the complete kept/added/dropped plan; no PATCH.
python3 scripts/airsprint_cli.py leg update-passengers \
  --leg-id LEG_UUID --add SAVED_PAX_UUID --dry-run

# Makes one GET, one PATCH, then stops with no read-back.
python3 scripts/airsprint_cli.py leg update-passengers \
  --leg-id LEG_UUID --add SAVED_PAX_UUID --remove OTHER_SAVED_PAX_UUID --confirm
```

The command refuses to PATCH if any existing leg passenger cannot be mapped to
a saved passenger UUID. This prevents accidental passenger loss.

## Booking creation and US destination addresses

The account is implicit in the auth token. Never send top-level `accountId`.
Every leg requires `departureAirportId`, `arrivalAirportId`, `aircraftId`,
`date`, `numberOfSeats`, `passengers`, `petIds`, and `requestSettings` with both
`cateringRequired` and `groundTransportationRequired`.

For any US-touching trip, provide one destination address containing exactly
the required fields. The CLI copies it to every passenger object on every leg,
including the Canadian return leg, and refuses to publish if it is missing.

```bash
ADDRESS='{"street":"1 Main St","city":"New York","state":"NY","zip":"10001","country":"United States"}'

python3 scripts/airsprint_cli.py booking create \
  --body "$BOOKING_JSON" --destination-address "$ADDRESS" --dry-run

python3 scripts/airsprint_cli.py booking create \
  --body "$BOOKING_JSON" --destination-address "$ADDRESS"
```

Airport-country detection uses the local mirror. Run `cache refresh` when IDs
are missing. Use `--us-touching` or `--not-us-touching` only when explicitly
overriding unresolved cache data.

`shareSettings` is required. `networkType` is `MY_NETWORK` or
`AIRSPRINT_NETWORK`. `joinerVariableCostPercentage`, when present, must be
between 30 and 80. If `specificGroupsOnly` is true, `groupIds` is required.

Cancellation is one confirmed request with no read-back:

```bash
python3 scripts/airsprint_cli.py booking cancel \
  --id BOOKING_CODE --reason "Plans changed" --dry-run
python3 scripts/airsprint_cli.py booking cancel \
  --id BOOKING_CODE --reason "Plans changed" --confirm
```

## Saved passengers and passports

Working deletion routes use HTTP DELETE:

```bash
python3 scripts/airsprint_cli.py passenger delete --id SAVED_PAX_UUID --dry-run
python3 scripts/airsprint_cli.py passenger delete --id SAVED_PAX_UUID --confirm
python3 scripts/airsprint_cli.py passport delete --id PASSPORT_UUID --dry-run
python3 scripts/airsprint_cli.py passport delete --id PASSPORT_UUID --confirm
```

Do not use `POST /my-passenger/{id}/delete` or
`POST /my-passport/{id}/delete`; those return 404. Passport number/date PATCH is
not supported and is not advertised.

`POST /my-passport/create` expects `dateOfBirth` and `expirationDate` in epoch
milliseconds, although later API responses may store seconds. `passport create`
accepts ISO dates, epoch seconds, or epoch milliseconds and always sends ms.
Android 6.1.4 parses `yyyy-MM-dd` at device-local midnight before taking
`millisecondsSinceEpoch`. For timezone-less ISO input, always pass `--tz` (or
set `AIRSPRINT_TIMEZONE`) so the CLI preserves the same calendar date in the
Android UI; the CLI refuses ambiguous input without it.

The app displays the first UUID in a passenger's `passportIds` array;
`selectedPassportId` does not persist. Use:

```bash
# Create and then place the returned passport first for this passenger.
python3 scripts/airsprint_cli.py passport create \
  --passenger-id SAVED_PAX_UUID \
  --body '{"dateOfBirth":"1980-01-02","expirationDate":"2031-03-04"}' \
  --tz America/Toronto \
  --dry-run

# Reorder an existing passport to first. One passenger GET and one PATCH.
python3 scripts/airsprint_cli.py passport make-primary \
  --passenger-id SAVED_PAX_UUID --passport-id PASSPORT_UUID --dry-run
python3 scripts/airsprint_cli.py passport make-primary \
  --passenger-id SAVED_PAX_UUID --passport-id PASSPORT_UUID --confirm
```

## Canadian customs

Use `POST /canadianCustomsDeclaration/create`. The required IDs are
`legPassenger.id` values from the booked leg, not saved passenger IDs.
`purposeOfTravel` is `BUSINESS` or `PLEASURE`. There is one `date` field: the
outbound departure leaving Canada. It is not the return or signature date.

```bash
python3 scripts/airsprint_cli.py customs create \
  --booking BOOKING_CODE \
  --passengers "Jane Doe,John Doe" \
  --purpose PLEASURE \
  --description "Family visit" \
  --dry-run
```

This resolves names to legPassenger UUIDs and defaults `date` from the outbound
leg. One request containing several IDs creates one declaration per person.
Optional flags cover pets, alcohol/tobacco, imported goods, US-imported goods,
high-value currency, and souvenir items.

```bash
python3 scripts/airsprint_cli.py customs update-date \
  --id DECLARATION_UUID --date 2026-09-01T14:00:00Z --dry-run
python3 scripts/airsprint_cli.py customs update-date \
  --id DECLARATION_UUID --date 2026-09-01T14:00:00Z --confirm
```

`customs list` sends only `page` and `filter`; never add `sort` because the API
returns 400. Signature/certification is not exposed by the API. Always tell the
owner to complete the signature in the app.

## Empty legs, network, hours, and raw access

- Use `explore flights --compact` for empty legs. Snapshot/diff belongs to the
  caller; the CLI does not maintain snapshots.
- Use `network connections` and `network groups`; follower/social commands are
  retired and intentionally absent.
- `hours estimate` and `hours power` are GET calls with query parameters.
- `raw api-patch` and `raw api-delete` require `--confirm` or `--dry-run`.
- A raw trip/leg GET observes the same post-write no-probe cooldown.
- Raw commands are escape hatches, not permission to bypass typed safeguards.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 1 | General/API error |
| 2 | Validation or safety refusal |
| 3 | Not found |
| 4 | Authentication failure |
