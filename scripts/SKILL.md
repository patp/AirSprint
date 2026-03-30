# AirSprint CLI — Agent Guide

## Overview

CLI for AirSprint fractional jet ownership platform.
Uses two APIs: prod2.airsprint.com (booking, trips, messages) and api.airsprint.com (quotes, pricing, airport/aircraft lookups).
Covers: auth, user profile, trip management, booking, quotes & pricing, explore flights, messages, feedback.

## Setup

```bash
# Credentials via env vars (recommended)
export AIRSPRINT_USERNAME="email@example.com"
export AIRSPRINT_PASSWORD="yourpassword"

# Or load from .env
source /Users/mb/src/AirSprint/.env

# Run
python3 /Users/mb/src/AirSprint/scripts/airsprint_cli.py <group> <command> [options]
```

## Output

- Default: JSON with `{"status": "ok", "data": ...}` wrapper
- Human-readable: `--format human` or `-f human`
- Errors: `{"status": "error", "message": "..."}` on stderr

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

### quote — Pricing & Estimates (legacy API)

These commands call `api.airsprint.com` for **real server-side pricing**. Unlike `--dry-run`, these actually query AirSprint.

| Command | Description |
|---------|-------------|
| `quote flight --from ICAO --to ICAO --date UTC` | Get flight quote (simple mode, auto-resolves ICAO to UUID) |
| `quote flight --body JSON` | Get flight quote (advanced mode, pass UUIDs directly) |
| `quote cost --body JSON` | Misc cost estimate (catering, transport, surcharges) |
| `quote hours-exchange --body JSON` | Hours exchange value estimate |
| `quote airports [--icao CODE] [--name TEXT] [--limit N]` | Search airports (returns UUIDs for --body mode) |
| `quote aircraft` | List all AirSprint fleet types with UUIDs |

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

# Get a price quote
python3 airsprint_cli.py quote flight --from CYUL --to CYYC --date "2026-05-01T12:00:00Z"

# Read all messages
python3 airsprint_cli.py messages read-all
```

## Constraints

- All write operations (booking, cancel, update) support `--dry-run`
- The API may return 401 if the token expired — re-run `auth login` to refresh
- Airport names in bookings must match the `locations[]` values from `booking info` exactly
- The CLI never prompts for input — all values must come via flags or env vars
- Quote commands use a separate legacy API (api.airsprint.com) with its own token cache (~/.airsprint_legacy_token.json)
- `quote flight --from/--to` auto-resolves ICAO codes but fetches the full airport list (~958 airports) on first call
