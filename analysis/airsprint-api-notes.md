# AirSprint Android API Notes

Static analysis target:

- Package: `com.droid.airsprint`
- Version: `5.1.13` (`versionCode 29`)
- Artifact: `artifacts/com.droid.airsprint.xapk`
- SHA-256: `b756a3ba0d6a42515ece4962ac8a99726e97cdd4017b927259c09be09e7d2357`
- Decompiled output: `decompiled/`

## High-confidence findings

- Base API URL: `https://prod2.airsprint.com/`
- Login endpoint: `POST /oauth/token`
- Booking prep endpoint: `GET /user/getBookingInfo`
- New booking endpoint: `POST /user/bookTrip`
- Existing booking update endpoint: `PUT /user/updateTrip`

The app does not add app-level certificate pinning in `NetworkService`, and the manifest sets `android:usesCleartextTraffic="true"`. Static analysis suggests future proxy-based validation should be possible if we want to confirm exact live payloads.

## Live validation

Live-verified on `2026-03-13`:

- `POST /oauth/token`
- `GET /user/getInitialUserInfo`
- `GET /user/getMyTrips?arrivalTimeZone=America/Toronto`

Reusable client:

- `scripts/airsprint_cli.py`

Operational note:

- Python `urllib` needed a `certifi`-backed SSL context in this environment even though `curl` validated the server certificate chain successfully.

## Auth flow

Login uses form data, not JSON.

Request:

- Method: `POST`
- URL: `https://prod2.airsprint.com/oauth/token`
- Content-Type: `application/x-www-form-urlencoded; charset=UTF-8`
- Authorization: `Basic VVNFUl9DTElFTlRfQVBQOnBhc3N3b3Jk`
- Decoded basic credentials: `USER_CLIENT_APP:password`

Form fields used by the app:

- `grant_type=password`
- `username=<email>`
- `password=<password>`
- `deviceToken=<firebase token or null/empty>`
- `deviceType=Android`

Expected token response fields:

- `access_token`
- `token_type`
- `refresh_token`
- `expires_in`
- `scope`
- `email`
- `jti`

After login, all JSON endpoints use:

- `Authorization: Bearer <access_token>`
- `Content-Type: application/json; charset=utf-8` for `POST` and `PUT`

Important detail: on `401`, the app replays the password grant using the stored email and password. It does not appear to use the refresh token for renewal.

## Booking flow

The app books in this order:

1. `POST /oauth/token`
2. `GET /user/getInitialUserInfo`
3. `GET /user/getBookingInfo`
4. `POST /user/bookTrip`

`/user/getBookingInfo` is the key discovery endpoint for scripting because it returns the values the app needs before booking:

- `accountId`
- `authorizer`
- `locations[]`
- `aircrafts[]`
- `passengers[]`
- `contacts[]`

Use `accountId`, `authorizer`, and the returned reference data when building the booking request.

## Booking payload shape

The booking body is JSON with a top-level `bookingReq` object.

```jsonc
{
  "bookingReq": {
    "accountId": "001...",
    "bookingSource": "App",
    "authorizer": "003...",
    "aircraftRequested": "Citation CJ3",
    "isReturnTrip": false,
    "isEmptyLegBooking": false,
    "bookingLegs": [
      {
        "aircraftRequested": "Citation CJ3",
        "departureAirport": "Calgary International Airport",
        "destinationAirport": "Vancouver International Airport",
        "departureTimeEpoch": 1773555600000,
        "arrivalTimeEpoch": 1773562800000,
        "timeZone": "America/Toronto",
        "specialRequest": "",
        "cateringRequested": false,
        "transportationBookingRequested": false,
        "legPassengers": [
          {
            "id": "003...",
            "email": "owner@example.com",
            "firstName": "Jane",
            "lastName": "Doe",
            "gender": "Female",
            "birthYear": 1985,
            "isFrequentTraveller": true
          }
        ]
      }
    ]
  }
}
```

Notes from the UI code:

- The app displays airports as `IATA | Airport Name`, but strips the `IATA | ` prefix before sending the request.
- The request sends airport names, not the visible IATA display string.
- Gson is used without `serializeNulls()`, so null fields are normally omitted from the JSON.
- The app sets `bookingSource` to `App`.
- The app uses the selected/default passenger list as `legPassengers`.

## Booking request fields discovered

Top-level `bookingReq` fields:

- `accountId`
- `aircraftRequested`
- `authorizer`
- `bookingId`
- `bookingLegs`
- `bookingSource`
- `cancellationAuthorizer`
- `isCancelBookingRequest`
- `isEmptyLegBooking`
- `isReturnTrip`
- `revisionSource`

Per-leg fields:

- `aircraftRequested`
- `arrivalTimeEpoch`
- `bookingLegId`
- `bookingLegStatus`
- `cateringRequested`
- `departureAirport`
- `departureTimeEpoch`
- `destinationAirport`
- `flightAttendantRequest`
- `includeStandardRequests`
- `isCancelBookingLegRequest`
- `isFavourite`
- `isNewBookingRequired`
- `legPassengers`
- `specialRequest`
- `timeZone`
- `transportationBookingRequested`

Per-passenger fields:

- `id`
- `email`
- `firstName`
- `lastName`
- `gender`
- `birthYear`
- `isFrequentTraveller`
- `legPassengerId`
- `passengerId`
- `status`

## Client-side booking rules

The app enforces these rules before calling the API:

- `flyFrom` must be present.
- `flyTo` must be present.
- Departure and destination cannot be the same.
- Airport names must match the client regex below:

```text
^[^?%&#@$+=*^\[\]`~<>]{2,}$
```

- A date is required.
- At least one of departure time or arrival time is required.
- Aircraft is required.
- Max passengers per leg: `10`.
- Booking must be at least `480` minutes (`8` hours) in the future.
- For a second/return leg, the later leg must be after the first leg.

The 8-hour rule is enforced client-side by `Utilities.checkBookTripAvailable(...)`.

## Currency

All prices returned by the API (flight quotes, empty leg costs, misc cost estimates) are in **CAD** (Canadian dollars). The API does not include a currency field in responses.

## Flight Tracking Feasibility

Based on static analysis plus live `getMyTrips` responses on `2026-03-13`, the app API currently gives enough data to describe the trip, but not enough to reliably identify the exact aircraft on FlightAware.

Fields we do get from trip data:

- `bookingId`
- `bookingStatus`
- `aircraftRequested` (aircraft type or cabin class, for example `Citation CJ3+/CJ2+`)
- `departureAirportIcaoId`
- `departureAirportIataId`
- `destinationAirportIcaoId`
- `destinationAirportIataId`
- `departureTimeEpoch`
- `arrivalTimeEpoch`

Fields we have not found in app models or live trip JSON:

- tail number / registration
- call sign / flight ident
- ICAO 24-bit hex code
- operator flight number
- FlightAware-specific tracking ID

Practical implication:

- Route plus times plus aircraft type may be enough to guess candidate flights in FlightAware.
- It is not robust enough to automate exact tracking with high confidence.
- For reliable FlightAware matching, we would want a tail number or call sign.

Best next source to check:

- If a trip ever returns `isTripsheetAvailable=true`, fetch `GET /user/downloadTripSheet?bookingId=...` and inspect the PDF. That is the most likely app-accessible place where a real aircraft registration or operator detail could appear.

Live trip-sheet check on `2026-03-13`:

- Every booking currently returned `isTripsheetAvailable=false` in `GET /user/getBookingById`.
- Direct probes to `GET /user/downloadTripSheet?bookingId=...` for the full visible booking history returned `200 OK` with `Content-Type: text/plain` and `Content-Length: 0`.
- The Android app's `MyTripsService.getTripDocument(...)` treats an empty body as `no_file_to_download`, so there is currently no trip-sheet PDF available to mine for tail number data.

## Update and cancellation semantics

The same `PUT /user/updateTrip` endpoint is reused for several actions.

Request changes:

- `isCancelBookingRequest=false`
- `bookingId=<existing booking>`
- `revisionSource="App"`
- includes updated `bookingLegs`

Add/confirm a return leg:

- `bookingId=<existing booking>`
- `isReturnTrip=true`
- includes `bookingLegs`

Cancel whole trip:

- `isCancelBookingRequest=true`
- `bookingLegs=null`
- `cancellationAuthorizer=<current user contactId>`

Cancel one leg:

- `isCancelBookingRequest=false` unless there is only one leg left
- `bookingLegs=[{"bookingLegId":"...","isCancelBookingLegRequest":true}]`
- `cancellationAuthorizer=<current user contactId>`

## Other endpoints discovered

Auth and account:

- `POST /oauth/token`
- `GET /user/getInitialUserInfo`
- `POST /user/changePassword`
- `POST /user/updateDeviceToken`
- `POST /user/logout`
- `POST /user/requestInfo`
- `POST /user/password-reset/start`
- `POST /user/password-reset/complete`
- `GET /user/getAccountInfo`
- `POST /user/updateAccountInfo`
- `GET /user/preferences`
- `POST /user/preferences`
- `POST /user/ownerHours/topUp`

Trips and booking:

- `GET /user/getBookingInfo`
- `POST /user/bookTrip`
- `PUT /user/updateTrip`
- `GET /user/getMyTrips?arrivalTimeZone=...`
- `GET /user/getBookingById?bookingId=...&arrivalTimeZone=...`
- `GET /user/downloadTripSheet?bookingId=...`
- `GET /user/trip/{id}/invoice`
- `GET /user/invoices?arrivalTimeZone=...`
- `GET /user/preflight-info?arrivalTimeZone=...`
- `POST /user/preflight-info/covid/{id}`
- `POST /user/flight-feedback/{id}`

Messages and feedback:

- `GET /user/getUserMessages`
- `POST /user/readUserMessage`
- `POST /user/deleteUserMessage`
- `POST /user/readAllUserMessages`
- `GET /user/feedback/subject`
- `POST /user/feedback`

Misc:

- `GET /user/getAllCounts?currentEpoch=...&arrivalTimeZone=...`
- `GET /user/getStaticPageDetails?type=...`
- `GET /user/getEmptyLegDetails`
- `GET /user/verifyAppVersion?platform=android&versionName=...`

## Practical scripting plan

Minimum viable script flow:

1. Login with password grant at `/oauth/token`.
2. Fetch `/user/getBookingInfo`.
3. Pick `accountId`, `authorizer`, an aircraft name, and valid airport names from the returned reference data.
4. Build `bookingReq`.
5. Submit `POST /user/bookTrip` with bearer auth.
6. If you get `401`, repeat the login and retry once.

If we decide to build the script, the first implementation should target:

- one-way booking only
- existing passenger/contact IDs from `getBookingInfo`
- explicit departure time
- no new passenger creation
- no empty-leg or return-trip logic

That will keep the first script small and close to the app's happy path.

Use your own account credentials and keep request volume low enough to stay inside the service's expected usage and terms.
