# AirSprint CLI

Agent-friendly CLI for [AirSprint](https://www.airsprint.com/) fractional jet ownership.

- **api.airsprint.com** — owner portal API: bookings, trips, messages, quotes, pricing, lookups (the only live host as of April 2026)
- **prod2.airsprint.com** — old mobile-app API, decommissioned April 2026; `raw prod2-*` escape hatches are kept only for reference

## Features

- **Non-interactive** — all inputs via flags or env vars, no prompts
- **JSON by default** — structured `{"status": "ok", "data": ...}` output, parseable by agents
- **`--compact` mode** — strips null/empty/audit fields and emits minified JSON for token-efficient agent piping
- **Local data mirror** — airports + fleet cached at `~/.airsprint_cache.json` (7-day TTL); offline ICAO resolution and search
- **Compound commands** — `summary` (one call replaces 4) and `quote roundtrip` (one call, both legs)
- **OAuth2 auth** with automatic token caching (`~/.airsprint_token.json`)
- **Server-side pricing** — get real flight quotes without submitting a booking
- **Local time support** — `--date 2026-04-15T10:00 --tz America/Montreal`
- **Dry-run** on all write operations (booking, cancel, update)
- **Semantic exit codes** — 0 success, 1 error, 2 validation, 3 not found, 4 auth
- **Single file** — `scripts/airsprint_cli.py`, no package structure needed

The agent-friendly design (compact mode, local mirror, compound commands) follows the [Printing Press](https://printingpress.dev/) principles for token-efficient CLIs.

## Setup

```bash
pip install typer truststore
```

Set credentials and timezone:

```bash
export AIRSPRINT_USERNAME="your@email.com"
export AIRSPRINT_PASSWORD="yourpassword"
export AIRSPRINT_TIMEZONE="America/Montreal"  # optional, for local time in --date
```

Or create a `.env` file:

```
AIRSPRINT_USERNAME=your@email.com
AIRSPRINT_PASSWORD=yourpassword
AIRSPRINT_TIMEZONE=America/Montreal
```

## Usage

```bash
python3 scripts/airsprint_cli.py <group> <command> [options]
```

### Command Groups

| Group | Description |
|-------|-------------|
| `summary` | One-call dashboard (accounts + trips + empty legs + unread) |
| `auth` | Login, logout, token status, 2FA setup/verify/disable |
| `user` | Profile, account info, preferences, avatar, change password |
| `trips` | List trips, get details, invoices, preflight, manifest, recent legs |
| `booking` | Get booking prep data, create/update/cancel, empty-leg & shared-flight booking, flight lock, reserve-day |
| `quote` | Flight quotes (one-way & roundtrip), cost estimates, airport/aircraft lookups, nearest airport, saved airports |
| `explore` | Browse empty legs and shared flights |
| `messages` | List, read, delete in-app messages |
| `feedback` | List subjects, submit feedback |
| `cache` | Manage the local airport/aircraft mirror |
| `raw` | Raw API escape hatches (api GET/POST/PATCH, prod2 GET/POST/PUT) |
| `account` | Account-user management (list, invite, update, roles) |
| `passenger` | Saved passengers (list, get, create) |
| `passport` | Saved passports + document upload/attach |
| `pet` | Saved pets + document upload/attach |
| `customs` | Canadian customs declarations (list, create, public link) |
| `address` | Address autocomplete + saved addresses |
| `hours` | Hours-exchange marketplace (estimate, power, listings) |
| `files` | File listing & public-file records |
| `content` | FAQ, policies, system notice, concierge, required info |
| `social` | Follow / followers / following / requests (accept/decline) |

### Quick Start

```bash
# Login (token cached automatically)
python3 scripts/airsprint_cli.py auth login

# Populate the local airport/aircraft mirror (one-time, 7-day TTL)
python3 scripts/airsprint_cli.py cache refresh

# Single-call dashboard for an agent
python3 scripts/airsprint_cli.py summary --compact

# View profile and hours balance
python3 scripts/airsprint_cli.py user profile

# List your trips
python3 scripts/airsprint_cli.py trips list

# Get a flight quote (local time)
python3 scripts/airsprint_cli.py quote flight --from CYQB --to KTEB --date "2026-04-15T10:00" --tz America/Montreal

# Get a flight quote (UTC)
python3 scripts/airsprint_cli.py quote flight --from CYUL --to CYYC --date "2026-05-01T16:00:00Z"

# Browse today's empty legs
python3 scripts/airsprint_cli.py explore flights

# Human-readable output
python3 scripts/airsprint_cli.py user profile --format human
```

### Quote Flow (Get Pricing Without Booking)

```bash
# Simple — use ICAO codes, auto-resolved (cache-served when fresh)
python3 scripts/airsprint_cli.py quote flight --from CYQB --to KTEB --date "2026-04-15T10:00" --tz America/Montreal

# Round-trip in one call
python3 scripts/airsprint_cli.py quote roundtrip --from CYQB --to KTEB \
  --out 2026-04-15T10:00 --return 2026-04-18T17:00 --tz America/Montreal

# List aircraft types (cache-served)
python3 scripts/airsprint_cli.py quote aircraft

# Search airports (cache-served when fresh; query matches ICAO/IATA/name/city/country)
python3 scripts/airsprint_cli.py quote airports -q montreal
python3 scripts/airsprint_cli.py quote airports -q KTEB

# Misc cost estimate (catering, transport, surcharges)
python3 scripts/airsprint_cli.py quote cost --body '{"legs":[...]}'

# Hours exchange estimate
python3 scripts/airsprint_cli.py quote hours-exchange --body '{"...": "..."}'
```

### Booking Flow

1. `booking info` — get `accountId`, `authorizer`, valid airports, aircraft, passengers
2. Build a `bookingReq` JSON payload (see [SKILL.md](skills/SKILL.md) for schema)
3. `booking create --body '...' --dry-run` — validate without submitting
4. `booking create --body '...'` — submit the booking

### Cancellation

```bash
# Cancel entire trip
python3 scripts/airsprint_cli.py booking cancel --id BOOKING_ID --authorizer CONTACT_ID

# Cancel one leg
python3 scripts/airsprint_cli.py booking cancel --id BOOKING_ID --leg-id LEG_ID --authorizer CONTACT_ID
```

### Timezone

No default timezone is assumed. For local times:

- Pass `--tz America/Montreal` on the command line, or
- Set `AIRSPRINT_TIMEZONE=America/Montreal` in your environment

UTC times (ending in `Z`) work without `--tz`.

## Agent Guide

See [`skills/SKILL.md`](skills/SKILL.md) for the full agent-oriented reference: every command, payload schemas, booking rules, and common patterns.

## API Coverage

Built from static analysis of the AirSprint Android app (`com.droid.airsprint` v5.1.13) and live API validation.

### Endpoints Covered

| Category | API | Endpoints |
|----------|-----|-----------|
| Auth | api | `/oauth/token`, `user/2fa/setup`, `user/2fa/verify`, `user/2fa/disable`, `user/request-reset-password`, `user/reset-password` |
| User | api | `getInitialUserInfo`, `getAccountInfo`, `updateAccountInfo`, `preferences`, `my-user`, `my-user/avatar`, `my-user/change-password`, `my-notifications`, `my-notification-settings` |
| Trips | api | `getMyTrips`, `getBookingById`, `downloadTripSheet`, `invoices`, `preflight-info`, `trip/manifest`, `trip/manifest/send`, `leg/recent/list`, `leg/recent/save` |
| Booking | api | `getBookingInfo`, `bookTrip`, `updateTrip` (update + cancel), `empty-leg/book`, `shared-flight/book`, `flight/lock`, `reserve-day`, `cancel-own`, `booking-survey/create` |
| Explore | api | `getEmptyLegDetails`, `getAllCounts` |
| Messages | api | `getUserMessages`, `readUserMessage`, `readAllUserMessages`, `deleteUserMessage` |
| Feedback | api | `feedback/subject`, `feedback`, `feedback/create` |
| Quotes | api | `flight-quote`, `trip/misc-cost-estimate`, `hour-exchange/estimate` |
| Lookups | api | `airport`, `airport/nearest`, `my-saved-airports`, `aircraft`, `my-aircraft`, `baggage-type` |
| Account | api | `my-account-users`, `my-account-user/{id}`, `account-user/invite`, `account-user/update`, `account-user-role` |
| Passenger | api | `my-passenger`, `my-passenger/{id}`, `my-passenger/create` |
| Passport | api | `my-passport`, `my-passport/create`, `my-passport/upload-init`, `my-passport/attach` |
| Pet | api | `my-pet`, `my-pet/create`, `my-pet/upload-init`, `my-pet/attach` |
| Customs | api | `myCanadianCustomsDeclaration`, `canadianCustomsDeclaration/create`, `canadianCustomsDeclaration/create-public`, `canadianCustomsDeclaration/link-create` |
| Address | api | `address/autocomplete`, `my-address/create` |
| Hours | api | `hour-exchange/estimate`, `hour-exchange/power`, `hours-exchange-listing/create`, `my-hours-exchange-listing` |
| Files | api | `my-file`, `file-public/create` |
| Content | api | `faq`, `faq-category`, `policy`, `policy-category`, `system-notice`, `required-info`, `concierge` |
| Social | api | `my-user/followers`, `my-user/following`, `my-user/follower-requests`, `user/follow`, `user/follower/accept`, `user/follower/decline` |
| Raw | both | Generic GET/POST/PUT escape hatches for any endpoint not yet typed |

## Requirements

- Python 3.10+
- `typer` (CLI framework)
- `truststore` (uses OS native certificate store for SSL)
