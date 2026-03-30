# AirSprint CLI

Agent-friendly CLI for [AirSprint](https://www.airsprint.com/) fractional jet ownership. Uses two APIs:

- **prod2.airsprint.com** — booking, trips, messages (OAuth2, from the mobile app)
- **api.airsprint.com** — quotes, pricing, airport/aircraft lookups (legacy API)

## Features

- **Non-interactive** — all inputs via flags or env vars, no prompts
- **JSON by default** — structured `{"status": "ok", "data": ...}` output, parseable by agents
- **OAuth2 auth** with automatic token caching (`~/.airsprint_token.json`)
- **Server-side pricing** — get real flight quotes without submitting a booking
- **Local time support** — `--date 2026-04-15T10:00 --tz America/Montreal`
- **Dry-run** on all write operations (booking, cancel, update)
- **Semantic exit codes** — 0 success, 1 error, 2 validation, 3 not found, 4 auth
- **Single file** — `scripts/airsprint_cli.py`, no package structure needed

## Setup

```bash
pip install typer certifi
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
| `auth` | Login, logout, check token status |
| `user` | Profile, account info, preferences |
| `trips` | List trips, get details, invoices, preflight info |
| `booking` | Get booking prep data, create/update/cancel bookings |
| `quote` | Flight quotes, cost estimates, airport/aircraft lookups |
| `explore` | Browse empty legs and shared flights |
| `messages` | List, read, delete in-app messages |
| `feedback` | List subjects, submit feedback |

### Quick Start

```bash
# Login (token cached automatically)
python3 scripts/airsprint_cli.py auth login

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
# Simple — use ICAO codes, auto-resolved
python3 scripts/airsprint_cli.py quote flight --from CYQB --to KTEB --date "2026-04-15T10:00" --tz America/Montreal

# List aircraft types
python3 scripts/airsprint_cli.py quote aircraft

# Search airports
python3 scripts/airsprint_cli.py quote airports --name Montreal
python3 scripts/airsprint_cli.py quote airports --icao KTEB

# Misc cost estimate (catering, transport, surcharges)
python3 scripts/airsprint_cli.py quote cost --body '{"legs":[...]}'

# Hours exchange estimate
python3 scripts/airsprint_cli.py quote hours-exchange --body '{"...": "..."}'
```

### Booking Flow

1. `booking info` — get `accountId`, `authorizer`, valid airports, aircraft, passengers
2. Build a `bookingReq` JSON payload (see [SKILL.md](.agents/skills/airsprint/SKILL.md) for schema)
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

See [`.agents/skills/airsprint/SKILL.md`](.agents/skills/airsprint/SKILL.md) for the full agent-oriented reference: every command, payload schemas, booking rules, and common patterns.

## API Coverage

Built from static analysis of the AirSprint Android app (`com.droid.airsprint` v5.1.13) and live API validation. See [`analysis/`](analysis/) for the full API catalog and notes.

### Endpoints Covered

| Category | API | Endpoints |
|----------|-----|-----------|
| Auth | prod2 | `/oauth/token` |
| User | prod2 | `getInitialUserInfo`, `getAccountInfo`, `updateAccountInfo`, `preferences` |
| Trips | prod2 | `getMyTrips`, `getBookingById`, `downloadTripSheet`, `invoices`, `preflight-info` |
| Booking | prod2 | `getBookingInfo`, `bookTrip`, `updateTrip` (update + cancel) |
| Explore | prod2 | `getEmptyLegDetails`, `getAllCounts` |
| Messages | prod2 | `getUserMessages`, `readUserMessage`, `readAllUserMessages`, `deleteUserMessage` |
| Feedback | prod2 | `feedback/subject`, `feedback` |
| Quotes | legacy | `flight-quote`, `trip/misc-cost-estimate`, `hour-exchange/estimate` |
| Lookups | legacy | `airport`, `aircraft`, `my-aircraft` |

## Requirements

- Python 3.10+
- `typer` (CLI framework)
- `certifi` (optional, for SSL on some systems)
