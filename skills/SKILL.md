# AirSprint CLI — Agent Guide

## Overview

CLI for AirSprint fractional jet ownership platform.
Uses two APIs: prod2.airsprint.com (booking, trips, messages) and api.airsprint.com (quotes, pricing, airport/aircraft lookups).
Covers: auth, user profile, trip management, booking, quotes & pricing, explore flights, messages, feedback.

Token-efficient design (compact mode, local mirror, compound commands) follows the [Printing Press](https://printingpress.dev/) principles for agent-friendly CLIs.

## Setup

```bash
# Credentials via env vars (recommended)
export AIRSPRINT_USERNAME="email@example.com"
export AIRSPRINT_PASSWORD="yourpassword"
export AIRSPRINT_TIMEZONE="America/Montreal"  # required for local time in --date

# Or load from .env
source /Users/mb/src/AirSprint/.env

# Run
python3 /Users/mb/src/AirSprint/scripts/airsprint_cli.py <group> <command> [options]
```

## Output

- Default: JSON with `{"status": "ok", "data": ...}` wrapper
- Human-readable: `--format human` or `-f human`
- Token-efficient: `--compact` (or `AIRSPRINT_COMPACT=1`) — strips null/empty/audit fields and emits minified JSON
- Errors: `{"status": "error", "message": "..."}` on stderr

`--compact` is wired into `summary`, `trips list`, `explore flights`, `quote airports`, `quote aircraft`. Use it when feeding output to another agent.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Validation / input error |
| 3 | Not found |
| 4 | Auth failure |

## Token Caching

Tokens are cached at `~/.airsprint_token.json` (auto-expires). First call logs in automatically if env vars are set. Use `auth login` to force refresh.

## Local Data Mirror

Static reference data (airports, fleet aircraft, owned aircraft) is mirrored to `~/.airsprint_cache.json` with a 7-day TTL. This lets `quote airports`, `quote aircraft`, and ICAO resolution in `quote flight`/`quote roundtrip` work without API roundtrips.

| Command | Description |
|---------|-------------|
| `cache refresh` | Pull airports, aircraft, my-aircraft into the local mirror (~943 airports as of last sync) |
| `cache status` | Show what's cached and how stale |
| `cache clear` | Delete the cache file |

The mirror is consulted automatically. Bypass it with `--no-cache` on `quote airports` / `quote aircraft`. Cache misses on `quote flight` ICAO lookup transparently fall back to the live API and extend the mirror.

## Compound Commands

These bundle multiple endpoints into a single call — the agent-friendly default for context gathering and round-trip pricing.

| Command | Replaces |
|---------|----------|
| `summary` | `user accounts` + `trips list` + `explore flights` + `explore counts` |
| `quote roundtrip` | Two `quote flight` calls + duplicated airport/aircraft resolution |

```bash
# One call: accounts, upcoming trips, empty legs, unread messages
python3 airsprint_cli.py summary --compact

# One call: outbound + return quote
python3 airsprint_cli.py quote roundtrip --from CYQB --to KTEB \
  --out 2026-04-15T10:00 --return 2026-04-18T17:00 --tz America/Montreal
```

## Commands

### auth — Authentication

| Command | Description |
|---------|-------------|
| `auth login` | Authenticate and cache token |
| `auth logout` | Clear cached token |
| `auth status` | Check if token is valid |

### user — User & Account

| Command | Description |
|---------|-------------|
| `user profile` | Get user profile (name, email, role) |
| `user accounts` | Get account info & access levels |
| `user preferences` | Get user preferences |
| `user set-preferences --body JSON` | Update preferences |
| `user update --body JSON` | Update account info |

### trips — Trip Management

| Command | Description |
|---------|-------------|
| `trips list` | List all trips |
| `trips get --id BOOKING_ID` | Get specific trip |
| `trips tripsheet --id BOOKING_ID [-o FILE]` | Download trip sheet PDF |
| `trips invoice --id TRIP_ID` | Get trip invoice |
| `trips invoices` | List all invoices |
| `trips preflight` | Get preflight info |
| `trips flight-feedback --id TRIP_ID --body JSON` | Submit flight feedback |

### booking — Book Flights

**Always run `booking info` first** to get valid `accountId`, `authorizer`, locations, aircraft, and passengers.

| Command | Description |
|---------|-------------|
| `booking info` | Get booking prep data (REQUIRED before create) |
| `booking create --body JSON [--dry-run]` | Book a new trip |
| `booking update --body JSON [--dry-run]` | Update existing trip |
| `booking cancel --id ID --authorizer ID [--leg-id ID] [--dry-run]` | Cancel trip or leg |

### explore — Available Flights

| Command | Description |
|---------|-------------|
| `explore flights` | List empty legs & shared flights |
| `explore counts` | Get dashboard counts |

### messages — In-App Messages

| Command | Description |
|---------|-------------|
| `messages list` | List messages |
| `messages read --id MSG_ID` | Mark message as read |
| `messages read-all` | Mark all as read |
| `messages delete --id MSG_ID` | Delete message |

### feedback — Feedback

| Command | Description |
|---------|-------------|
| `feedback subjects` | List feedback subjects |
| `feedback submit --body JSON` | Submit feedback |

### account — Account-User Management

| Command | Description |
|---------|-------------|
| `account users` | List users on the account |
| `account user-get --id ID` | Get account-user by ID |
| `account invite --body JSON` | Invite a user to the account |
| `account user-update --body JSON` | Update an account-user |
| `account roles` | List account-user roles |

### passenger — Saved Passengers

| Command | Description |
|---------|-------------|
| `passenger list` | List saved passengers |
| `passenger get --id ID` | Get a saved passenger |
| `passenger create --body JSON` | Create a saved passenger |

### passport — Passports & Documents

| Command | Description |
|---------|-------------|
| `passport list` | List saved passports |
| `passport get --id ID` | Get a saved passport |
| `passport create --body JSON` | Create a saved passport |
| `passport upload-init --body JSON` | Begin a passport doc upload (returns presigned URL) |
| `passport attach --body JSON` | Attach uploaded document to passport |

### pet — Pets & Documents

Same shape as `passport`: `list`, `get`, `create`, `upload-init`, `attach`.

### customs — Canadian Customs Declarations

| Command | Description |
|---------|-------------|
| `customs list` | List my customs declarations |
| `customs declaration --body JSON` | Get declaration form/template |
| `customs create --body JSON` | Create a declaration |
| `customs create-public --body JSON` | Public link-based declaration (no auth) |
| `customs link-create --body JSON` | Create a link to share with a passenger |

### address — Addresses

| Command | Description |
|---------|-------------|
| `address autocomplete --body JSON` | Address autocomplete |
| `address create --body JSON` | Save an address |

### hours — Hours-Exchange Marketplace

| Command | Description |
|---------|-------------|
| `hours estimate --body JSON` | Estimate hours-exchange value |
| `hours power --body JSON` | Hours-exchange power calculation |
| `hours listing-create --body JSON` | List hours for sale |
| `hours my-listings` | List my hours-exchange listings |

### files — Files

| Command | Description |
|---------|-------------|
| `files list` | List my files |
| `files public-create --body JSON` | Create a public-file record |

### content — Static Content

| Command | Description |
|---------|-------------|
| `content faq` | List FAQ entries |
| `content faq-categories` | List FAQ categories |
| `content policies` | List policies |
| `content policy-categories` | List policy categories |
| `content system-notice` | Current system notice |
| `content required-info` | Required-info prompts |
| `content concierge` | Concierge contact info |

### social — Follow Graph

| Command | Description |
|---------|-------------|
| `social followers` | List my followers |
| `social following` | List who I follow |
| `social requests` | Pending follower requests |
| `social follow --body JSON` | Follow a user |
| `social accept --body JSON` | Accept a follower request |
| `social decline --body JSON` | Decline a follower request |

### raw — Escape Hatches

For any endpoint not yet typed. Path is relative to the API host.

| Command | Description |
|---------|-------------|
| `raw api-get PATH` | GET against api.airsprint.com |
| `raw api-post PATH [--body JSON]` | POST against api.airsprint.com |
| `raw api-patch PATH [--body JSON]` | PATCH against api.airsprint.com |
| `raw prod2-get PATH` | GET against prod2.airsprint.com (decommissioned) |
| `raw prod2-post PATH [--body JSON]` | POST against prod2.airsprint.com (decommissioned) |
| `raw prod2-put PATH [--body JSON]` | PUT against prod2.airsprint.com (decommissioned) |

### quote — Pricing & Estimates

These commands call `api.airsprint.com` for **real server-side pricing**. Unlike `--dry-run`, these actually query AirSprint.

| Command | Description |
|---------|-------------|
| `quote flight --from ICAO --to ICAO --date DATETIME [--tz TZ]` | Get flight quote (simple mode, auto-resolves ICAO to UUID) |
| `quote flight --body JSON` | Get flight quote (advanced mode, pass UUIDs directly) |
| `quote roundtrip --from ICAO --to ICAO --out DATETIME --return DATETIME [--tz TZ]` | Quote outbound + return in one call |
| `quote cost --body JSON` | Misc cost estimate (catering, transport, surcharges) |
| `quote hours-exchange --body JSON` | Hours exchange value estimate |
| `quote airports [-q QUERY] [--saved] [--limit N] [--no-cache]` | Search airports (cache-served when fresh) |
| `quote aircraft [--no-cache]` | List all AirSprint fleet types with UUIDs (cache-served when fresh) |

## Booking Flow (Step by Step)

1. **Get reference data**: `booking info` → extract `accountId`, `authorizer`, aircraft names, airport names, passenger list
2. **Build payload**: Construct `bookingReq` JSON (see schema below)
3. **Dry run**: `booking create --body '...' --dry-run` → verify payload
4. **Submit**: `booking create --body '...'` → book the trip

### bookingReq Schema

```json
{
  "bookingReq": {
    "accountId": "001...",
    "bookingSource": "App",
    "authorizer": "003...",
    "aircraftRequested": "Citation CJ3",
    "isReturnTrip": false,
    "isEmptyLegBooking": false,
    "bookingLegs": [{
      "aircraftRequested": "Citation CJ3",
      "departureAirport": "Calgary International Airport",
      "destinationAirport": "Vancouver International Airport",
      "departureTimeEpoch": 1773555600000,
      "timeZone": "America/Toronto",
      "specialRequest": "",
      "cateringRequested": false,
      "transportationBookingRequested": false,
      "legPassengers": [{
        "id": "003...",
        "email": "owner@example.com",
        "firstName": "Jane",
        "lastName": "Doe",
        "gender": "Female",
        "birthYear": 1985,
        "isFrequentTraveller": true
      }]
    }]
  }
}
```

### Booking Rules

- Departure and destination cannot match
- Booking must be >= 8 hours in the future
- Max 10 passengers per leg
- At least one of `departureTimeEpoch` or `arrivalTimeEpoch` required
- Airport names must match values from `booking info` (use full names, not IATA codes)
- Epoch times are in **milliseconds**

### Cancellation

```bash
# Cancel entire trip
airsprint booking cancel --id BOOKING_ID --authorizer CONTACT_ID

# Cancel one leg
airsprint booking cancel --id BOOKING_ID --leg-id LEG_ID --authorizer CONTACT_ID

# Preview first
airsprint booking cancel --id BOOKING_ID --authorizer CONTACT_ID --dry-run
```

## Quote Flow (Get Pricing Without Booking)

**Simple mode** — just use ICAO codes:
```bash
# Local time (requires --tz or AIRSPRINT_TIMEZONE)
python3 airsprint_cli.py quote flight --from CYQB --to KTEB --date "2026-04-15T10:00" --tz America/Montreal

# UTC (no --tz needed)
python3 airsprint_cli.py quote flight --from CYQB --to KTEB --date "2026-04-15T14:00:00Z"

# Returns: price, flight time, distance, departure/arrival times
```

**Advanced mode** — use UUIDs for full control:
```bash
# 1. Find airport UUIDs
python3 airsprint_cli.py quote airports --name "Quebec"
python3 airsprint_cli.py quote airports --icao KTEB

# 2. Find aircraft UUID
python3 airsprint_cli.py quote aircraft

# 3. Get quote
python3 airsprint_cli.py quote flight --body '{"legs":[{"aircraftId":"UUID","departureAirportId":"UUID","arrivalAirportId":"UUID","departureDateUTC":"2026-04-15T14:00:00Z"}]}'
```

### Quote leg fields (for --body)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `aircraftId` | UUID | yes | Aircraft type UUID from `quote aircraft` |
| `departureAirportId` | UUID | yes | From `quote airports` |
| `arrivalAirportId` | UUID | yes | From `quote airports` |
| `departureDateUTC` | ISO 8601 | yes | e.g. `2026-04-15T14:00:00Z` |

**Notes:**
- All prices are in **CAD** (Canadian dollars). The API does not include a currency field.
- `distance` is in **nautical miles (NM)**. `flightTime` is in **minutes**.

## Common Patterns

```bash
# Check auth status
python3 airsprint_cli.py auth status

# Get profile as JSON
python3 airsprint_cli.py user profile

# List upcoming trips
python3 airsprint_cli.py trips list

# Get a specific trip
python3 airsprint_cli.py trips get --id "a16OF000000vNCoYAM"

# See available empty legs
python3 airsprint_cli.py explore flights

# Get a price quote (UTC)
python3 airsprint_cli.py quote flight --from CYUL --to CYYC --date "2026-05-01T16:00:00Z"

# Get a price quote (local time)
python3 airsprint_cli.py quote flight --from CYUL --to CYYC --date "2026-05-01T12:00" --tz America/Montreal

# Read all messages
python3 airsprint_cli.py messages read-all
```

## Timezone

No default timezone. For local times in `--date`:
- Pass `--tz America/Montreal` on the command line, or
- Set `AIRSPRINT_TIMEZONE=America/Montreal` in environment
- UTC times (ending in `Z`) work without `--tz`
- Local time without `--tz` exits with code 2

## Constraints

- All write operations (booking, cancel, update) support `--dry-run`
- The API may return 401 if the token expired — re-run `auth login` to refresh
- Airport names in bookings must match the `locations[]` values from `booking info` exactly
- The CLI never prompts for input — all values must come via flags or env vars
- Quote commands use api.airsprint.com with its own token cache (~/.airsprint_api_token.json)
- `quote flight --from/--to` auto-resolves ICAO codes via the local mirror (`cache refresh` to populate); cache misses fall back to a single-airport API lookup and extend the mirror
