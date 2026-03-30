# AirSprint CLI

Agent-friendly CLI for [AirSprint](https://www.airsprint.com/) fractional jet ownership. Built against `prod2.airsprint.com` (the mobile app's API).

## Features

- **Non-interactive** — all inputs via flags or env vars, no prompts
- **JSON by default** — structured `{"status": "ok", "data": ...}` output, parseable by agents
- **OAuth2 auth** with automatic token caching (`~/.airsprint_token.json`)
- **Dry-run** on all write operations (booking, cancel, update)
- **Semantic exit codes** — 0 success, 1 error, 2 validation, 3 not found, 4 auth
- **Single file** — `scripts/airsprint_cli.py`, no package structure needed

## Setup

```bash
pip install typer certifi
```

Set credentials:

```bash
export AIRSPRINT_USERNAME="your@email.com"
export AIRSPRINT_PASSWORD="yourpassword"
```

Or create a `.env` file:

```
AIRSPRINT_USERNAME=your@email.com
AIRSPRINT_PASSWORD=yourpassword
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

# Browse today's empty legs
python3 scripts/airsprint_cli.py explore flights

# Get booking prep data (airports, aircraft, passengers)
python3 scripts/airsprint_cli.py booking info

# Book a flight (dry-run first)
python3 scripts/airsprint_cli.py booking create --body '{"bookingReq": {...}}' --dry-run

# Human-readable output
python3 scripts/airsprint_cli.py user profile --format human
```

### Booking Flow

1. `booking info` — get `accountId`, `authorizer`, valid airports, aircraft, passengers
2. Build a `bookingReq` JSON payload (see [SKILL.md](scripts/SKILL.md) for schema)
3. `booking create --body '...' --dry-run` — validate without submitting
4. `booking create --body '...'` — submit the booking

### Cancellation

```bash
# Cancel entire trip
python3 scripts/airsprint_cli.py booking cancel --id BOOKING_ID --authorizer CONTACT_ID

# Cancel one leg
python3 scripts/airsprint_cli.py booking cancel --id BOOKING_ID --leg-id LEG_ID --authorizer CONTACT_ID
```

## Agent Guide

See [`scripts/SKILL.md`](scripts/SKILL.md) for the full agent-oriented reference: every command, payload schemas, booking rules, and common patterns.

## API Coverage

Built from static analysis of the AirSprint Android app (`com.droid.airsprint` v5.1.13) and live API validation. See [`analysis/`](analysis/) for the full API catalog and notes.

### Endpoints Covered

| Category | Endpoints |
|----------|-----------|
| Auth | `/oauth/token` |
| User | `getInitialUserInfo`, `getAccountInfo`, `updateAccountInfo`, `preferences` |
| Trips | `getMyTrips`, `getBookingById`, `downloadTripSheet`, `invoices`, `preflight-info` |
| Booking | `getBookingInfo`, `bookTrip`, `updateTrip` (update + cancel) |
| Explore | `getEmptyLegDetails`, `getAllCounts` |
| Messages | `getUserMessages`, `readUserMessage`, `readAllUserMessages`, `deleteUserMessage` |
| Feedback | `feedback/subject`, `feedback` |

## Requirements

- Python 3.10+
- `typer` (CLI framework)
- `certifi` (optional, for SSL on some systems)
