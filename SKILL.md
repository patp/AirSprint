---
name: airsprint-cli
description: Use the local AirSprint owner CLI for trips, booking, passengers, passports, Canadian customs, My Network, hours exchange, quotes, and current Android-app-backed operations. Every write is a typed form (options), never JSON. Apply the live-booking no-probe rules before any trip or leg access.
---

# AirSprint CLI agent guide

Use this CLI for AirSprint owner operations against `https://api.airsprint.com/api`.
It follows the current AirSprint Android 6.1.12 API. Retired prod2, follower, and
known-broken passport PATCH commands are intentionally absent.
The source audit covers all 105 unique Android method/route contracts; see
`ANDROID_CONTRACT.md` for the checksum and complete mapping.

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

JSON is the default output; there is no `--json` flag. Use `--format human`
for human output and `--compact` for token-efficient JSON.

Cached sessions are scoped to the requested login email. If `auth login`
reports a 2FA challenge, use its `userId` with `auth 2fa-sign-in --user-id ID
--code CODE --username EMAIL`. That command saves the authenticated session;
it never prints the full token.

## Forms, not JSON

Every command that writes to AirSprint is a form: you answer questions with
options (`--leg`, `--passengers`, `--catering yes`, ...) and the CLI builds the
exact Android 6.1.12 request body itself. No public command accepts an API
request body or a `--body` flag. You never need to know the wire format.

- IDs are the only internals you handle: passenger, passport, pet, flight,
  trip, leg, airport, and aircraft UUIDs. Get them from list/info commands.
- Yes/no questions take `yes` or `no`. Choice questions take lowercase words
  with hyphens (`strongly-agree`, `suv-and-driver`); `--help` lists them.
- Repeatable answers are written as `KEY=VALUE` and repeated
  (`--baggage "Golf bag=2" --baggage Suitcase=1`, `--passport PAX_ID=PASSPORT_ID`).
- Addresses are either separate options (`--destination-street/-city/-state/-zip`)
  or one string `"STREET; CITY; STATE; ZIP[; UNIT]"` for ground-transport stops.
- `--dry-run` prints `payload` — the exact request — so you can show the owner
  what will be sent. Read it, never edit it. `--compact` trims API responses
  but never a dry-run preview.
- If a form has no option for what the owner wants, say so; do not look for a
  raw or JSON way around it.

## Efficient agent usage

- Start with `agent commands --group GROUP` or `--query WORD`, then
  `agent describe --command 'GROUP COMMAND'`. With no filter, discovery lists
  groups only. Use the returned required fields, types and effect metadata.
- For repeated work, use `agent serve`: one typed JSON request and reply per
  line, reusing the command tree and caches. Form keys use underscores;
  unknown keys and wrong types fail before execution. See `AGENT_RUNTIME.md`.
- In the agent runtime or MCP, writes/local edits require a stable
  `operation_key`; AirSprint writes also require `confirm: true` after explicit
  authorization. Reuse the original key after interruption to recover its
  receipt. Never use another key to retry an uncertain write.
- Inspect failures using `code`, `requestMayHaveSucceeded` and `nextAction`.
  Use `agent operation --operation-key KEY` for the local receipt.
- `trips list` and `messages list` return `page.hasMore` and `page.nextOffset`.
  A partial list does not prove a passenger, flight or notification is absent.
- MCP clients use `airsprint_commands`, `airsprint_describe`, then the indicated
  `airsprint_read` or `airsprint_run` tool. Keep the original customs review,
  individual certification and final submission steps.
- For monitoring, use MCP events. The service polls safe collections at most
  once per configured cycle, establishes a silent baseline and delivers signed
  webhooks. Do not build a polling loop around booked-trip/leg detail reads.
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
- `trips get`, `trips show`, `trips tripsheet`, `trips flight-get`, `trips
  leg-get`, `leg update-passengers`, `leg update-required-info`, and high-level
  `customs prepare`, `customs submit` and `leg audit-travel-info` default to
  no probe during that cooldown.
- Only use `--probe` to override the cooldown when the user explicitly wants an
  immediate read and understands that it can notify the app.
- The first safe API read may retry once only for `WRONG_VERSION_NUMBER`.
  Booking GETs, PATCHes, and all writes never retry.
- Always use `--dry-run` before a mutation and preserve the dry-run output.
- Use `--confirm` where required. Do not add a follow-up GET after success.

## Trips

```bash
python3 scripts/airsprint_cli.py trips list --upcoming --compact
python3 scripts/airsprint_cli.py trips list --past --limit 20

# One trip GET. During the post-write cooldown, this exits without probing.
python3 scripts/airsprint_cli.py trips get --id BOOKING_OR_TRIP_UUID

# One trip GET plus one manifest GET; no retries or polling. Extracts tail,
# crew, FBO, passenger lines, and full manifest text.
python3 scripts/airsprint_cli.py trips show --id TRIP_UUID --compact

# Download or obtain the manifest PDF URL.
python3 scripts/airsprint_cli.py trips tripsheet --id TRIP_UUID --output trip.pdf

# Email the manifest (one POST; --confirm required).
python3 scripts/airsprint_cli.py trips manifest-send \
  --trip-id TRIP_UUID --to "owner@example.com,assistant@example.com" --confirm
```

Prefer a trip UUID for `get`, `show`, or `tripsheet`. A booking code requires a
bounded `/my-leg` lookup first. `trips show` is the operations view; `trips get`
returns only the API trip object.

## Booking a trip

`booking create` is the app's booking form. Run `booking info` first for the
aircraft, saved-passenger, pet, and airport IDs; `booking baggage-types` lists
the baggage names. Airports may be ICAO codes or airport UUIDs. Leg times are
the local wall-clock time at the departure airport, `YYYY-MM-DDTHH:MM`, exactly
as typed in the app (no timezone suffix).

```bash
python3 scripts/airsprint_cli.py booking create \
  --leg "CYUL>KTEB@2026-09-01T09:00" \
  --leg "KTEB>CYUL@2026-09-03T17:30" \
  --passengers SAVED_PAX_UUID_1,SAVED_PAX_UUID_2 \
  --pets PET_UUID \
  --baggage "Golf bag=2" --baggage Suitcase=2 \
  --catering yes --catering-request "Light lunch for two" \
  --ground-transportation yes --ground-transportation-when arrival \
  --ground-transportation-method sedan-and-driver \
  --ground-dropoff-address "1 Main St; New York; NY; 10001" \
  --destination-street "1 Main St" --destination-city "New York" \
  --destination-state NY --destination-zip 10001 \
  --note "Owner prefers early boarding" \
  --dry-run
```

Form rules the CLI enforces before the booking request is sent (the passport
and destination checks first read airports and passengers; nothing is written):

- **Baggage must be answered.** Repeat `--baggage NAME=QUANTITY`, or say
  `--baggage none` when travelling without bags. A forgotten answer is refused.
- **Border crossings need passports with photos.** When any leg crosses a
  border, every passenger must have a passport on file (`passport list`), and
  that passport must have its photo/scan uploaded — AirSprint requires the
  passport image before departure, so a passport with no scan is refused. The
  CLI uses each passenger's first saved passport, as the app does; pick another
  with `--passport PAX_UUID=PASSPORT_UUID`. Create missing ones with
  `passport create` (which requires the photo) or attach a scan to an existing
  passport with `passport upload-document --id PASSPORT_UUID --file ...`.
- **US-touching and international trips need a destination address** (hotel,
  residence, ...): `--destination-street/-city/-state/-zip`, plus
  `--destination-street2` for a unit. It is copied to every passenger on every
  leg, including the return to Canada.
- Airport countries come from the local mirror. Run `cache refresh` when they
  are missing, or answer `--us-touching/--not-us-touching` and
  `--international/--domestic` explicitly.
- Catering, ground transportation, and `--note` apply to every leg. Adjust a
  single leg afterwards with `leg update-required-info`.
- Seats default to the number of passengers. `--aircraft-id` defaults to the
  account's aircraft.
- Sharing requires the selected aircraft to belong to the active account,
  or CJ2+ with Embraer Infinity access (Android 6.1.12). The CLI checks the
  active account rather than combining rights from different accounts.
- Sharing is off by default. `--open-to-share yes` with `--share-network
  my-network|airsprint-network`, `--share-seats`, `--share-groups`,
  `--share-pets-allowed`, `--share-children-allowed`, and
  `--share-cost-percentage` (30–80, app default 50) mirrors the app's share
  settings. `--special-requests` is the free-text field shown with the trip.
- `--dog-form-submitted yes` only when the CDC dog-import form is already
  submitted (dogs entering the US).

The account is implicit in the auth token; nothing account-related is sent.
The dry run's `routeCheck` shows the detected countries, `usTouching`, and
`international` so you can confirm them with the owner.

Booking an existing flight from `explore flights` uses the same vocabulary but
a shorter form (no note or passports in the request). Android 6.1.10 also
supports arrival transport with `--arrival-ground-method`,
`--arrival-pickup-address`, and `--arrival-dropoff-address`:

```bash
python3 scripts/airsprint_cli.py booking empty-leg \
  --flight-id FLIGHT_UUID --passengers SAVED_PAX_UUID --baggage none --dry-run
python3 scripts/airsprint_cli.py booking shared-flight \
  --flight-id FLIGHT_UUID --passengers SAVED_PAX_UUID_1,SAVED_PAX_UUID_2 \
  --baggage Suitcase=1 \
  --destination-street "1 Main St" --destination-city "New York" \
  --destination-state NY --destination-zip 10001
python3 scripts/airsprint_cli.py booking lock --flight-id FLIGHT_UUID            # hold
python3 scripts/airsprint_cli.py booking lock --flight-id FLIGHT_UUID --release  # release
```

An existing-flight booking never carries a customs declaration: the app's
`BookSharedPassenger` sends only `id` and `destinationAddress`. Prepare Canadian
customs on the actual return leg with `customs prepare`,
then review, certify each traveller and submit separately.

Cancellation is one confirmed request with no read-back:

```bash
python3 scripts/airsprint_cli.py booking cancel \
  --leg-id LEG_UUID --reason "Plans changed" --dry-run
python3 scripts/airsprint_cli.py booking cancel \
  --leg-id LEG_UUID --reason "Plans changed" --confirm
```

Android 6.1.10 sends only `legId` and `reason`; do not add a booking code,
`tripId`, or a list of legs.

## Updating passengers on a booked leg

`PATCH /leg/{id}` replaces the passenger list completely. The CLI always sends
the full current list, keyed by the actual passenger-profile UUID (including
unsaved guests), never `legPassenger.id`.
It reads that list once through the owner's `GET /my-leg/{id}` route.

```bash
# Makes one GET and prints the complete kept/added/dropped plan; no PATCH.
python3 scripts/airsprint_cli.py leg update-passengers \
  --leg-id LEG_UUID --add SAVED_PAX_UUID --dry-run

# Makes one GET, one PATCH, then stops with no read-back.
python3 scripts/airsprint_cli.py leg update-passengers \
  --leg-id LEG_UUID --add SAVED_PAX_UUID --remove OTHER_SAVED_PAX_UUID --confirm
```

The command refuses to PATCH if any existing leg passenger cannot be mapped to
a passenger-profile UUID. This prevents accidental passenger loss.
A missing or malformed passenger-list field is also refused; it is never
treated as an empty list.

## Completing a booked leg's required information

`leg update-required-info` is the app's "required information" form for one
leg: passports, customs declarations, destination address, seats, pets,
baggage, catering, ground transportation, note, and the CDC dog form. Only the
sections you answer are sent. Passenger answers (`--passport`, `--customs`,
`--destination-*`) trigger one guarded `GET /my-leg/{id}` so the complete
passenger list is preserved; the dry run prints `kept`/`updated`/`dropped`.
The write remains `PATCH /leg/{id}/required-info`. Do not use `GET /leg/{id}`
for the pre-read: owner tokens can receive 401 on that route. A failed read
stops the command without fallback requests or a PATCH.

```bash
python3 scripts/airsprint_cli.py leg update-required-info \
  --leg-id LEG_UUID \
  --passport SAVED_PAX_UUID=PASSPORT_UUID \
  --customs SAVED_PAX_UUID=DECLARATION_UUID \
  --destination-street "1 Main St" --destination-city "New York" \
  --destination-state NY --destination-zip 10001 \
  --dry-run
python3 scripts/airsprint_cli.py leg update-required-info \
  --leg-id LEG_UUID --pets PET_UUID --baggage Suitcase=2 \
  --ground-transportation yes --ground-transportation-when departure \
  --ground-transportation-method taxi --confirm
```

Passengers cannot be added or dropped here; use `leg update-passengers`.
Airports, aircraft, date, and share settings are not editable through this form.

## Saved passengers and passports

Use `leg audit-travel-info --leg-id ID` to inspect the **actual trip profiles**.
The operation's passenger UUID can differ from a same-named Saved Passenger;
reconciliation by AirSprint/FL3XX can leave an old selection pointing to a
passport on a different profile. Do not create replacements from names alone.
`passport list --passenger-id ACTUAL_ID` includes an unsaved guest that the
saved-passenger list omits. Check number, dates, nationality, authority and
scan separately, and preserve exact trip IDs. PDF, JPEG and PNG are supported.
The leg audit also shows customs submission status per traveller.

`passport list` performs one bounded `POST /my-passenger` read and flattens the
exact embedded `passports` records. Do not call `POST /my-passport`; the
collection route returns 404. Preserve the API's exact `nationality` and
`issuingAuthority` fields. Never infer, translate, or relabel `nationality` as
`country`; the passport record has no `country` field.

For passport-data audits, the physical passport scan is the source of truth;
saved AirSprint values can be stale or wrong. Read the printed `Issuing
Country/Pays émetteur` and `Authority/Autorité` separately. Do not compare the
printed three-letter issuing-country code (for example, `CAN`) as though it
were the API's two-letter `nationality` value (for example, `CA`). Visually
verify the scan before proposing any saved-passport replacement.

Creating a passenger is the app's "Add New Person" form. Answer the form's
questions with options; `--save-profile yes|no` is required, with no default:

- `--save-profile yes` — add the person to Saved Passengers (web default).
- `--save-profile no` — this booking only; the person is created (a leg needs
  the UUID) but hidden from the saved list.

```bash
python3 scripts/airsprint_cli.py passenger create \
  --first-name Jean --last-name Tremblay --gender male --category adult \
  --save-profile no --dry-run
python3 scripts/airsprint_cli.py passenger create \
  --first-name Jean --last-name Tremblay --gender male --category adult \
  --save-profile no
python3 scripts/airsprint_cli.py leg update-passengers --leg-id LEG_UUID --add NEW_PAX_UUID --confirm
```

First name, last name, and `--gender` are mandatory, exactly as in the app
(`male`, `female`, or `x` — the app asks for gender to estimate aircraft weight
and balance; `x` is "prefer not to specify"). `--category` is `adult` (12+),
`child` (2–11), or `infant` (<2; default adult); `--middle-name` and `--email`
are optional (the form has no flight-preferences field; the app sends it
empty). `passenger update --id UUID` edits the profile the way the app's form
does: pass only what changes, and the CLI reads the current profile and sends
the complete form back (`--dry-run` still does that read). The saved-passenger
choice is made once, at creation; `passenger list` reports it as `isActive` —
read it as "saved profile", the app never changes it afterwards.

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

AirSprint's concierge says a passport photo is not mandatory but is highly
recommended to avoid customs-clearance delays. The CLI intentionally makes it
mandatory for `passport create`: agents must supply `--file` and the command
must create the record, initialize one upload, perform one presigned multipart
POST, and attach the returned storage path. Accept only a real JPEG, PNG, or
PDF scan up to 20 MiB. Validate its signature before authentication or any API
write. Require `--confirm` for a real run. Never retry a create, upload, attach,
or automatically read back. If creation succeeds but upload fails, report the
new passport UUID and direct the agent to `passport upload-document`; never
rerun `passport create`.

```bash
python3 scripts/airsprint_cli.py passport create \
  --passenger-id SAVED_PAX_UUID \
  --passport-number AB123456 \
  --date-of-birth 1980-01-02 --expiration-date 2031-03-04 \
  --nationality CA --issuing-authority "QUÉBEC" \
  --file passport.pdf --timezone America/Toronto \
  --dry-run
python3 scripts/airsprint_cli.py passport create \
  --passenger-id SAVED_PAX_UUID \
  --passport-number AB123456 \
  --date-of-birth 1980-01-02 --expiration-date 2031-03-04 \
  --nationality CA --issuing-authority "QUÉBEC" \
  --file passport.pdf --timezone America/Toronto \
  --confirm
```

For an already-created passport, use the same complete Android document
workflow. The storage write is never retried:

```bash
python3 scripts/airsprint_cli.py passport upload-document \
  --id PASSPORT_UUID --file passport.pdf --dry-run
python3 scripts/airsprint_cli.py passport upload-document \
  --id PASSPORT_UUID --file passport.pdf --confirm

python3 scripts/airsprint_cli.py pet upload-document \
  --id PET_UUID --file vaccination.pdf \
  --document-type vaccinationDocument --confirm
```

Android 6.1.10 parses `yyyy-MM-dd` passport dates at device-local midnight. For
timezone-less ISO input, always pass `--timezone` (or set `AIRSPRINT_TIMEZONE`)
so the CLI preserves the same calendar date in the Android UI; the CLI refuses
ambiguous input without it. This is the Android device timezone, not a fixed
AirSprint HQ/Calgary timezone. Use `America/Edmonton` only when that is the
device's configured timezone.

Android 6.1.10 supports an authority-only update with one
`PATCH /my-passport/{id}`. Copy the printed `Authority/Autorité` exactly and use
`passport update-authority`; do not extend this to passport numbers or dates,
which have not persisted reliably:

```bash
python3 scripts/airsprint_cli.py passport update-authority \
  --id PASSPORT_UUID --authority 'OTTAWA' --dry-run
python3 scripts/airsprint_cli.py passport update-authority \
  --id PASSPORT_UUID --authority 'OTTAWA' --confirm
```

On a saved passenger profile, the app displays the first passport and uses
it when booking; a saved-profile `selectedPassportId` update does not persist.
An existing leg has its own selection: preserve it and inspect the actual trip
profile. To change the saved profile's default, reorder with one passenger GET
and one PATCH:

```bash
python3 scripts/airsprint_cli.py passport make-primary \
  --passenger-id SAVED_PAX_UUID --passport-id PASSPORT_UUID --dry-run
python3 scripts/airsprint_cli.py passport make-primary \
  --passenger-id SAVED_PAX_UUID --passport-id PASSPORT_UUID --confirm
```

`make-primary` only reorders a passport already attached to that passenger.

## Canadian customs: prepare, review, certify each person, submit

Canadian declarations belong to the **specific active leg arriving in Canada**.
The outbound and return have different leg-passenger IDs and different links.
A submitted outbound form does not complete the return. `--booking` selects
exactly one active Canadian arrival; ambiguous itineraries need `--leg-id`.

1. Start a **local draft** with `customs prepare` (`customs create` is now an
   alias that also only prepares). It reads the leg once and an existing
   declaration link once, and makes **no API writes**, including link creation.
   Missing answers stay visible as blockers. Draft files are private (0600).
   Filenames always include the booking code and full first/last name:
   `BOOKING-firstname-lastname.json`. Accents are normalized; a group includes
   every full name separated by `--`. Omit `--draft` to generate the name.
   Explicit paths must use that same filename; first-name-only aliases and
   UUID filenames are rejected. Existing drafts are never overwritten by a
   new preparation. Passenger selection also requires the full name.
2. Gather each traveller's answers. Never invent "no", reuse another trip's
   purpose, or infer certification from a booking approval. Compare the
   passport scan with the actual trip profile, including authority and dates.
   Show route, local dates/times, actual passenger and passport IDs, destination
   address, all customs answers and existing submission status.
3. Correct answers with `customs prepare --draft FILE` and typed options.
   This is offline. `--refresh` reads the same leg/link again after travel
   information was corrected. **Any edit or refresh clears all certifications.**
4. Present each person's complete information **one at a time**, quote the
   app's statement, and ask for that person's explicit certification. Only
   after that answer run `customs certify --passenger NAME --approve yes`.
   Never certify everyone from one general approval. `--approve no` removes
   a certification. This step only saves local state.
5. Once all individual certifications exist, show the final recap. On the
   owner's explicit final submission instruction run `customs submit --confirm`.
   The CLI rechecks the leg, passports, address and per-passenger submitted
   status before sending. Changed data requires another review/certification.

```bash
# Incomplete local draft: no answers are invented and no declaration is sent.
python3 scripts/airsprint_cli.py customs prepare \
  --leg-id RETURN_LEG_UUID --passengers "Jane Doe,John Roe" \
  --draft /private/path/RETURN-jane-doe--john-roe.json

# Fill answers explicitly after collecting them. These are examples, not defaults.
python3 scripts/airsprint_cli.py customs prepare --draft /private/path/RETURN-jane-doe--john-roe.json \
  --purpose PLEASURE --date 2026-10-07 --timezone America/Toronto \
  --has-pet no --has-alcohol-or-tobacco no \
  --has-imported-goods no --has-high-value-currency no
python3 scripts/airsprint_cli.py customs review --draft /private/path/RETURN-jane-doe--john-roe.json

# Run each only after that person's information has been shown and certified.
python3 scripts/airsprint_cli.py customs certify --draft /private/path/RETURN-jane-doe--john-roe.json \
  --passenger "Jane Doe" --approve yes
python3 scripts/airsprint_cli.py customs certify --draft /private/path/RETURN-jane-doe--john-roe.json \
  --passenger "John Roe" --approve yes
# Separate, final instruction to submit:
python3 scripts/airsprint_cli.py customs submit --draft /private/path/RETURN-jane-doe--john-roe.json --confirm
```

The statement is: **"I certify that the above declaration is true and complete.
By checking this box, I confirm my agreement."** Android 6.1.12 keeps this
checkbox locally; its repository submits the form without an
`agreedToDeclaration` field or separate signature endpoint. Do not tell the
owner to sign again after a confirmed submission. "Submitted" means accepted
by the AirSprint form API, not customs clearance.

**One form per person by default.** `--family yes` explicitly confirms all
named travellers are family residing at the same address; at most four can
share one form. A common hotel does not establish a shared home address.
Every person still needs an individual certification. When answers differ,
prepare separate drafts per person; never copy one person's answers to others.

Every yes/no question must be answered before certification. A "yes" requires
its details: alcohol/tobacco type, volume and CAD value; imported-goods
items, currency (`CAD|USD`) and U.S. origin; high-value-currency description.
`BUSINESS` requires `--description`. `--date` is the Traveller Declaration
Form's calendar date, converted from device-local midnight using `--timezone`;
it is not automatically the outbound departure date. Card details are omitted.

### Submitted status and duplicate protection

```bash
python3 scripts/airsprint_cli.py customs status --link-id RETURN_LINK_UUID --compact
python3 scripts/airsprint_cli.py leg audit-travel-info --leg-id RETURN_LEG_UUID
python3 scripts/airsprint_cli.py passport list --passenger-id ACTUAL_TRIP_PASSENGER_UUID
```

`status` (also `link-get`) exposes `customsDeclarationAlreadySubmitted` and
`submissionStatus: submitted|not-submitted` for **each passenger on that leg**,
including false values in compact mode. The leg's `customsDeclarationId` names
this link; it is not one traveller's declaration record ID. Missing/ambiguous
status blocks submission. `customs list` alone is not a completeness check.

- Already submitted passengers block the whole attempted submission. Prepare
  only the remaining people, with their own answers and certifications.
- Before each form POST, the CLI saves a journal entry. Success records the
  returned declaration IDs. A timeout, interruption, or malformed response
  remains blocked as `uncertain`/`submitting`; **never rerun or make a new
  draft to retry**. Check the server status and resolve with AirSprint first.
- The journal also blocks another draft for the same leg-passenger IDs and
  serializes submissions on this machine. Never run submissions concurrently
  on different machines; the API has no verified idempotency-key contract.
- No automatic retry or post-write read-back occurs. Local submitted drafts
  cannot be edited or resubmitted. The old `customs update-date` write is retired.
- `AIRSPRINT_CUSTOMS_DRAFT_DIR` can set the private draft/journal directory;
  default `drafts/customs/` under the CLI installation. Keep the journal across deployments.

## Surveys and feedback

Both are the app's questionnaires for one completed leg (never a trip ID):

```bash
python3 scripts/airsprint_cli.py booking survey --leg-id LEG_UUID \
  --booking-experience strongly-agree --response-time agree \
  --concierge-interest agree --concierge-help yes --itinerary-on-time yes \
  --comments "Smooth as always" --dry-run

python3 scripts/airsprint_cli.py feedback submit --leg-id LEG_UUID \
  --snacks-and-amenities very-satisfied --aircraft-condition excellent \
  --crew exceptional --fbo satisfied --catering-and-transport good \
  --contact-me no --dry-run
```

`feedback submit` derives the app's overall score from the five answers.
`--contact-me` is optional; when unanswered the app sends no `contact` key.

## Empty legs, network, hours, and quotes

- Use `explore flights --compact` for empty legs. Snapshot/diff belongs to the
  caller; the CLI does not maintain snapshots.
- Use `network connections` and `network groups`; follower/social commands are
  retired and intentionally absent.
- `hours estimate` and `hours power` are GET calls with query parameters.
  `hours listing-create --action buy|sell --hours 10` lists hours on the
  exchange (one POST; the account aircraft is resolved automatically).
- `quote flight` / `quote roundtrip` price a route. `quote cost --aircraft
  citation-cj3-plus --quote-price 12345 --flight-minutes 95` estimates one
  leg's miscellaneous costs; run it once per leg. Answer `--owned-aircraft`
  (and optionally `--flown-aircraft`) only when pricing an interchange —
  flying a type other than the one owned.

## Other Android-backed functions

- `auth status` is local cache status only; use `auth verify` for exactly one
  live `POST /user/authenticate` validation.
- Use `device register-token` and `device delete-token` for Android-compatible
  notification registration payloads.
- Use `trips flight-get` and `trips leg-get` for one guarded detail read; they
  never retry or poll.
- Use `booking baggage-types`, `quote aircraft-get`, `customs link-get`, and
  `user get` for the matching Android detail functions.
- Use `files resolve --application-url URL` for Android's bounded path lookup
  plus avatar fallback. It is not an unbounded probe loop.
- Use `content get`, `content faq-get`, and `content policy-get` for exact
  source-backed detail routes.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 1 | General/API error |
| 2 | Validation or safety refusal |
| 3 | Not found |
| 4 | Authentication failure |
