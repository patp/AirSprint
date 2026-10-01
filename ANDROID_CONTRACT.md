# Android 6.1.12 contract audit

This is the source-backed API contract for AirSprint CLI. The audit was made
offline from the Android 6.1.12 APK (version code 135), installed from Google
Play on 2026-10-01, without probing a booked trip. The audited `base.apk`
SHA-256 is:

```text
f9f689cf22a6c74558800d468765bdbb1c293713f1fe87057ab0ca774d2284e5
```

The decompiled repository layer contains 111 calls to `APIClient.request`.
Six are duplicate uses of the same method and normalized route, leaving 105
unique HTTP contracts:

| Method | Unique contracts |
|---|---:|
| POST | 65 |
| GET | 21 |
| PATCH | 11 |
| DELETE | 8 |
| **Total** | **105** |

102 of these 105 contracts are reachable through a typed CLI command; the three
remaining routes (`/file-public/create`, `/leg/recent/save`, `/app-update`) are deliberately
not exposed (see "Deliberate omissions"). No public command accepts JSON or
API field names: every write is a form of typed options and the CLI builds the
request body from the decompiled Android payload models. The comparison
includes methods, bodyless requests, request envelopes, field names, field
order, ID types, and high-risk no-probe behavior—not only matching URL text.

The 6.1.12 inventory with source locations is in `scripts/android_contracts.json`.
The regression suite checks the actual CLI call sites against that independent
inventory, including pre-reads and upload routes. The old customs-date
extension is retired. This would have caught the invalid `GET /leg/{id}`.

The original 6.1.4 analysis remains under `tmp/android-verify/analysis/`.
Addresses in the historical findings below refer to that baseline unless
explicitly marked 6.1.10 or 6.1.12. The fresh analysis is under
`tmp/audit-2026-10-01/analysis/`; see `AUDIT_2026-10-01.md` for changes and limits.

## October 1 workflow corrections

- Android 6.1.12 `RemoteCustomDeclarationRepository.submitDeclaration`
  (0x88ec9c, POST at 0x88f280) does not serialize `agreedToDeclaration`.
  The local form model does contain it; the checkbox text is at 0x8859c0
  and callback `setAgreedToDeclaration` at 0x8905a0. There is no separate
  signature/approve route. CLI certification is local and person-by-person.
- `CustomsDeclarationLinkModel` decodes each leg passenger's
  `customsDeclarationAlreadySubmitted` (0x7807ac). That flag, for the exact
  Canadian-arrival leg and leg-passenger ID, is the duplicate-submission guard.
  One link's status never establishes another leg's status.
- `customs prepare` saves private local drafts, including incomplete answers.
  Individual certifications bind to the exact itinerary, passenger/passport
  fields, address and answers. `submit` checks those again, requires all
  certifications, checks live submitted flags, and journals every attempted
  form before its POST. Unknown outcomes are blocked; no retries or read-back.
- The app permits four people residing at one address on one form
  (0x883324). CLI policy is one form each unless family at the same address
  is explicitly confirmed. Each family member still needs certification.
- Passport form upload extensions are PDF/JPG/PNG (0x8eb3b0). No forced JPEG
  conversion was added. Actual trip profiles and selected passport IDs must
  be audited independently of same-named saved profiles.
- Existing destination addresses are normalized through the five-field
  Android request model; nullable `street2` from reads is omitted on writes.
  `selectedPassportId` is preserved as `passport: {id}` when changing another
  field, and is checked against attached records in the travel-info audit.
- New `ShareEligibility` permits owned aircraft, or CJ2 with Embraer Infinity
  access. Booking sharing reads the active account and enforces this rule.
- Airport fallback uses `filter.name` and an exact ICAO match. The HTTP layer
  checks embedded `httpStatusCode`, including failures returned over HTTP 200.

## Method

The APK was fingerprinted with the
[android-reverse-engineering-skill](https://github.com/SimoneAvogadro/android-reverse-engineering-skill)
workflow: `libapp.so` identifies the app as Flutter, so Dart code was recovered
with `blutter` (assembly plus object-pool and class-layout dumps under
`tmp/android-verify/analysis/`, gitignored) rather than with a Java decompiler.
Request bodies were read from the payload models' `toJson` methods
(`booking_api_models.dart`, `book_shared_request_model.dart`,
`flight_lock_request_model.dart`, the survey/feedback/manifest/hours/misc-cost
request models, and `LegUpdateOptions`), the enum wire values from the enum
classes, and validation rules from the form controllers. Evidence dumps for
each payload are kept next to the analysis; the CLI's exact-body tests are
built from them.

## Functions added from Android

The audit found 16 route contracts with no prior CLI equivalent. They now map
as follows:

| Android function | CLI command |
|---|---|
| Authenticate a cached token | `auth verify` |
| Register/delete a notification token | `device register-token`, `device delete-token` |
| Get a private/public file | `files get`, `files public-get` |
| Get a user by UUID | `user get` |
| Patch one account user | `account user-patch` |
| Get a customs declaration link | `customs link-get` |
| Get one owner flight or leg | `trips flight-get`, `trips leg-get` |
| Get one aircraft | `quote aircraft-get` |
| List baggage types | `booking baggage-types` |
| Get content, FAQ, or policy by UUID | `content get`, `content faq-get`, `content policy-get` |
| Patch required leg information | `leg update-required-info` |

The Android client also contains bounded client-side workflows that reuse
existing routes. The CLI includes `files resolve` for Android's application
URL/avatar fallback and complete init → presigned multipart POST → attach
workflows in `passport create`, `passport upload-document`, and
`pet upload-document`. As a stricter CLI policy, `passport create` requires a
validated JPEG, PNG, or PDF scan and always attempts to attach it; the Android
API itself does not make the document mandatory.

## Corrected source mismatches

- Android cancellation sends exactly `{"legId": ..., "reason": ...}`. The CLI
  no longer sends a booking code, trip ID, or a list of legs.
- Booking surveys use `legId`, not `tripId`.
- Quote legs include `pax`.
- Airport search uses `filter.name`, not `filter.query`.
- Pet PATCH uses an `options` envelope.
- Account role batch update uses `{"ids": [...], "options": {"roleNames": [...]}}`.
- `/my-accounts`, `/my-aircraft`, `/my-pet`, and `/baggage-type` are bodyless
  POSTs. The CLI no longer turns these into an empty JSON object.
- Avatar GET returns JSON containing `url`; it is not the image bytes.
- 2FA setup and disable send an empty JSON object and accept no invented body.
- Required-info passenger updates preserve the complete current passenger list
  before one PATCH and refuse unresolved saved-passenger IDs.
- Android's passport date conversion uses device-local midnight. It is not
  fixed to AirSprint headquarters time.
- The passenger form's "add to Saved Passengers?" toggle (`addToSavedList` in
  `AddNewPersonState`) is sent as `isActive` in `NewPassengersRequestModel`
  (`POST /my-passenger/create`, `toJson` 0x6f4a90) after `firstName`,
  `middleName` (`""` when blank), `lastName`, `email` (`null` when blank),
  `gender`, `age`, `flightPreferences` (hard-coded `""`, 0x6f4ba4), `picture`
  (`""` when none), `passports` only when non-empty (never from this form,
  0x6f4be8) and `addresses: []`. `passenger create` builds that exact body
  from typed options, maps `--save-profile yes|no` to `isActive`, and has no
  flight-preferences option.
- The saved-passenger profile form (`PassengerPersonProfilePage._onSave`,
  0x881fcc) never patches one field: after `isValid` (0x87de8c: first and
  last name non-blank, gender set) it sends the complete map `firstName`,
  `middleName`, `lastName`, `email` (`null` when blank), `gender`
  (`MALE|FEMALE|NONE`), `age`, `flightPreferences` (0x88237c–0x8825e8), plus
  `picture` only after a new avatar upload, wrapped as `{"options"}` in
  `PATCH /my-passenger/{id}` (`updatePassengerOwn` 0x86769c). `isActive` is
  never sent from this form. `passenger update` reads the current profile
  (`GET /my-passenger/{id}`, also under `--dry-run`), merges the options you
  pass and sends that full form; it offers no `--save-profile`.
- The passenger form requires first name, last name, and gender
  (`PassengerPersonProfileController::isValid`: both names non-blank after
  trim and `gender != null`). The UI enum `PassengerGenderEnum` MALE/FEMALE/X
  is converted to the wire enum `GenderEnum` MALE/FEMALE/NONE in
  `AddNewPersonController::submit`. `--gender` is therefore required and
  `x` is sent as `NONE`.
- Trip booking (`TripBookRequestPayload`) sends `legs`, `dogFormSubmitted`,
  `baggage`, and a complete `shareSettings` object (nine keys, app defaults
  when sharing is off). Each `TripLegPayload` sends `departureAirportId`,
  `arrivalAirportId`, `aircraftId`, `date`, `numberOfSeats`, `passengers`,
  `petIds`, and `requestSettings`; `date` is the wall-clock departure time
  `YYYY-MM-DDTHH:MM:00.000` with no zone suffix. `PassengerPayload` drops null
  keys (`id`, `customsDeclarationId`, `destinationAddress`, `passport: {id}`).
  `AddressPayload` sends `street`, `city`, `state`, `zip`, and `street2` only
  when set; there is no `country` key. `RequestSettingsPayload` always sends
  `cateringRequired` and `groundTransportationRequired`, then only the
  conditional keys the form filled. `booking create` builds exactly this.
- Existing-flight bookings (`BookSharedRequestModel` for `/empty-leg/book` and
  `/shared-flight/book`) send `{"flightId", "options": {passengers,
  requestSettings, [petIds], [baggage]}}`; `BookSharedRequestOptions.toJson`
  adds `petIds` and `baggage` only when the lists are non-empty (unlike the
  trip form, which always sends them). Passenger entries are `{id,
  [destinationAddress]}`: `BookSharedPassenger.toJson` (0x70760c) gates
  `customsDeclarationId` on a constant `""` (0x707658), so it is never sent,
  and there is no `passport` key; the request settings have no note.
  Since Android 6.1.10, they also support arrival-side transportation.
  `booking empty-leg` and `booking shared-flight` expose
  exactly that (no `--customs`; attach declarations with
  `leg update-required-info --customs`).
- `/flight/lock` sends `{"id", "lock"}` (`booking lock`, `--release` = false).
- Survey answers are Likert/yes-no enums; feedback answers use the app's
  satisfaction enums with the wire spelling `DISSASTIFIED` (sic), the
  feedback `contact` boolean is sent only when the yes/no question was
  answered (`FeedbackSurveyCreateRequestModel` skips it when null), and the
  feedback `score` is derived as the app does: sum of the five answers
  (5 ... 1) divided by 5.0. `booking survey` and `feedback submit` map
  lowercase words to those wire values.
- Miscellaneous cost estimates (`toTripMiscCostEstimateRequest`) always send
  `aircraft`, `quotePrice` and `serviceArea` (possibly `""`), add
  `serviceLocation`, `actualFlightMinutes` and `groundTransportation`
  (`{"method", "applyServiceCharge": true}`) only when set, and add
  `interchange {"ownedAircraft", "flownAircraft"}` only when the owned
  aircraft type is known and the flown type is recognised. `quote cost`
  sends `interchange` only when `--owned-aircraft` is answered and
  `actualFlightMinutes` only when `--flight-minutes` is given (the model
  null-gates it at 0x8d8064).
- Hours-exchange listings (`{"accountAircraftId", "action": BUY|SELL,
  "hours"}`) and manifest emails (`{"recipients", "tripId"}`) use the app's
  exact request models (`hours listing-create`, `trips manifest-send`).
  `/hour-exchange/estimate` and `/hour-exchange/power` are true GETs with
  query parameters.
- `/account-user/invite` wraps the new user in `{"newUser": {...}}`, with a
  sibling `passengerId` when inviting from a saved passenger; notification
  reads always go through the batch `PATCH /my-notifications/update`
  `{"ids", "isRead"}`.
- `--dry-run` previews are printed exactly as the body would be sent;
  `--compact` (which strips empty values from API responses) never edits a
  preview, so always-present empty keys such as `baggage: []` stay visible.
  Customs drafts without a link use a placeholder until submission creates
  the link. The final body carries the returned UUID.
- `LegUpdateOptions` (`PATCH /leg/{id}/required-info`) carries only the
  sections the form filled; `leg update-required-info` sends only answered
  sections and uses the model's key order.
- `/canadianCustomsDeclaration/create` is built by hand in
  `RemoteCustomDeclarationRepository.submitDeclaration()`
  (`app/features/custom_declaration/data/remote_custom_declaration_repository.dart`,
  0x8a7920), not by `CustomsDeclarationRequestModel.toJson()`. It always sends
  `canadianCustomsDeclationLinkId` (the app's misspelling, value from the link
  created by `POST /canadian-customs-declaration-link/create {"legId"}` when
  the form opens), `legPassengerIds`, `date`, `purposeOfTravel`,
  `travelDescription`, `hasPet`, `hasAlcoholOrTobacco`, `hasImportedGoods`,
  `importedGoodsFromUS` and `hasHighValueCurrency`; `alcoholOrTobaccoType`,
  `alcoholOrTobaccoVolume`, `importedGoodItems`, `souvenirItems` and
  `highValueCurrencyDescription` only when non-empty after `trim()`
  (0x8a7aa4–0x8a7cb0), `importedGoodsCurrency` only when non-empty without
  trimming (0x8a7bd4–0x8a7bf8), `alcoholOrTobaccoValueCAD` as
  `double.parse()` when set (0x8a7b3c).
  `date` is the form's Traveller-Declaration "Date", not the departure:
  `DateFormat("yyyy-MM-dd").tryParse(date).toUtc().toIso8601String()`. The
  validator (`CustomDeclarationController.passengerFormIsValid`, 0x8a6c54)
  requires `travelDescription` only for `BUSINESS`, and the details behind
  each "yes". `customs submit` builds this body from a completed local draft
  after individual certification. `customs create` / `prepare` never submit.
- `/empty-leg/book` and `/shared-flight/book` passengers: the CLI used to
  accept `--customs PAX=DECLARATION` and emit `customsDeclarationId`, a key
  `BookSharedPassenger.toJson` (0x70760c) can never produce; the option is
  gone.
- `POST /my-passenger/create`: the CLI used to send `null` for a blank middle
  name, picture and flight preferences, a caller-chosen `flightPreferences`
  and an unconditional `passports: []`; it now sends the `""`/omitted values
  of `NewPassengersRequestModel.toJson` (0x6f4a90).
- `PATCH /my-passenger/{id}`: the CLI used to send only the changed keys and
  could send `isActive`; it now sends the profile form's complete seven-key
  map (0x881fcc) and never `isActive`.
- `PATCH /leg/{id}/required-info`: the CLI used to copy a fetched
  `passportIds` list into passenger entries; `LegPassengerUpdate.toJson`
  (0x801778) has no such key, so only `customsDeclarationId`,
  `destinationAddress` and `passport` are carried over.
- Booked-leg pre-reads in `leg update-required-info`, `leg update-passengers`
  and `customs create --leg-id` use `GET /my-leg/{id}`, matching
  `RemoteFlightRepository.myAccountLeg` (0x9bce44; route at 0x9bce94,
  request at 0x9bcec8). `LegResponseModel.fromJson` reads `legPassengers`
  (0x6ec2f4); `LegPassengerModel.fromJson` distinguishes the leg-passenger
  `id` (0x6f16d0) from the saved `passengerId` (0x6f1700). The old
  `GET /leg/{id}` pre-read could return 401 for an owner token and abort
  before writing. PATCH routes remain `/leg/{id}` and
  `/leg/{id}/required-info`. No fallback, retry or read-back was added.

## Guard rails beyond the app

These CLI refusals are stricter than Android and happen before the
write is sent. Unanswered customs questions remain in local drafts and block
certification/submission. Passport and destination checks need read-only
lookups (airport mirror, `GET /my-passenger/{id}` for passports):

- Baggage must be answered on every booking form (`--baggage NAME=QTY` or
  `--baggage none`); the app allows silently sending an empty list.
- A trip whose legs cross a border refuses to book a passenger without a
  passport on file (the app only warns). Each passenger's first passport is
  used, as the app does; `--passport PAX=PASSPORT` overrides. When the
  `GET /my-passenger/{id}` read returns the full passport records, a selected
  passport with no photo/scan uploaded (`image` empty) is refused too. A thin
  response that carries only `passportIds`, or omits the selected passport,
  is refused because the required scan cannot be verified.
- US-touching and international trips require a destination address on every
  passenger of every leg.
- Airport countries come from the local airport mirror (ICAO prefix as a
  fallback); when neither can decide, `--us-touching/--not-us-touching` and
  `--international/--domestic` must be answered explicitly.
- Existing-flight bookings cannot enforce the passport rule: the Android model
  has no passport key and the flight route is not known without an extra
  booked-flight read, which the no-probe policy forbids.

## Complete route coverage

Dynamic IDs are normalized as `{id}`. Multiple CLI commands are shown where a
single Android route supports several app functions.

### GET (21)

| Contract | CLI coverage |
|---|---|
| `/aircraft/{id}` | `quote aircraft-get` |
| `/app-update` | not exposed (native-app version check; deliberate omission) |
| `/canadian-customs-declaration-link/{id}` | `customs link-get` |
| `/content/{id}` | `content get` |
| `/faq/{id}` | `content faq-get` |
| `/file-public/{id}` | `files public-get` |
| `/hour-exchange/estimate` | `hours estimate`, `quote hours-exchange` |
| `/hour-exchange/power` | `hours power` |
| `/leg/recent/list` | `trips recent` |
| `/me` | `user profile` |
| `/my-file/{id}` | `files get` |
| `/my-flight/{id}` | `trips flight-get` |
| `/my-leg/{id}` | `trips leg-get`; pre-read for `leg update-passengers`, `leg update-required-info`, `customs create` (with --leg-id) |
| `/my-notification-settings` | `messages settings`, `user preferences` |
| `/my-passenger/{id}` | `passenger get` |
| `/my-pet/{id}` | `pet get` |
| `/my-user/avatar/{id}` | `user avatar` |
| `/policy/{id}` | `content policy-get` |
| `/trip/{id}` | `trips get`, `trips show` |
| `/trip/manifest/{id}` | `trips tripsheet`, `trips show` |
| `/user/{id}` | `user get` |

### PATCH (11)

| Contract | CLI coverage |
|---|---|
| `/account-user/update` | `account user-update` |
| `/leg/{id}` | `leg update-passengers` |
| `/leg/{id}/required-info` | `leg update-required-info` |
| `/my-account-user/{id}` | `account user-patch` |
| `/my-notification-settings/update` | `messages settings-update`, `user set-preferences` |
| `/my-notifications/update` | `messages read`, `messages read-all`, `messages update` |
| `/my-passenger/{id}` | `passenger update`, `passport make-primary` |
| `/my-passport/{id}` | `passport update-authority` |
| `/my-pet/{id}` | `pet update` |
| `/my-user` | `user update` |
| `/my-user/groups/{id}` | `network group-rename` |

### DELETE (8)

| Contract | CLI coverage |
|---|---|
| `/my-account-user/{id}` | `account user-delete` |
| `/my-passenger/{id}` | `passenger delete` |
| `/my-passport/{id}` | `passport delete` |
| `/my-pet/{id}` | `pet delete` |
| `/my-saved-airports/{id}` | `quote saved-airport-delete` |
| `/my-user/connections/{id}` | `network connection-remove` |
| `/my-user/groups/{id}` | `network group-delete` |
| `/my-user/groups/{id}/members/{memberId}` | `network group-member-remove` |

### POST (65)

| Contract | CLI coverage |
|---|---|
| `/account-notification-registration-token-delete` | `device delete-token` |
| `/account-notification-registration-token-register` | `device register-token` |
| `/account-user-role` | `account roles` |
| `/account-user/invite` | `account invite` |
| `/address/autocomplete` | `address autocomplete` |
| `/aircraft` | `quote aircraft`, `cache refresh` |
| `/airport` | `quote airports`, `quote saved-airports` |
| `/airport/nearest` | `quote airport-nearest` |
| `/baggage-type` | `booking baggage-types` |
| `/booking-survey/create` | `booking survey` |
| `/canadian-customs-declaration-link/create` | `customs link-create` |
| `/canadianCustomsDeclaration/create` | `customs submit` (certified local draft only) |
| `/cancel-own` | `booking cancel` |
| `/concierge` | `content concierge` |
| `/empty-leg/book` | `booking empty-leg` |
| `/faq` | `content faq` |
| `/faq-category` | `content faq-categories` |
| `/feedback/create` | `feedback submit` |
| `/file-public/create` | not exposed (deliberate omission) |
| `/flight-quote` | `quote flight`, `quote roundtrip` |
| `/flight/lock` | `booking lock` |
| `/hours-exchange-listing/create` | `hours listing-create` |
| `/leg/recent/save` | not exposed (deliberate omission) |
| `/my-account-users` | `account users` |
| `/my-accounts` | `user accounts` |
| `/my-address/create` | `address create` |
| `/my-aircraft` | `booking info`, `cache refresh` |
| `/my-file` | `files list`, `files resolve` |
| `/my-flights` | `explore flights`, `explore counts` |
| `/my-hours-exchange-listing` | `hours my-listings` |
| `/my-leg` | `trips list` |
| `/my-notifications` | `messages list`, `messages read-all`, `explore counts` |
| `/my-passenger` | `passenger list`, `passport list` |
| `/my-passenger/create` | `passenger create` |
| `/my-passport/create` | `passport create` |
| `/my-passport/document/attach` | `passport upload-document`, `passport create` |
| `/my-passport/document/upload-init` | `passport upload-document`, `passport create` |
| `/my-pet` | `pet list` |
| `/my-pet/create` | `pet create` |
| `/my-pet/document/attach` | `pet upload-document` |
| `/my-pet/document/upload-init` | `pet upload-document` |
| `/my-user/change-password` | `user change-password` |
| `/my-user/connections` | `network connections` |
| `/my-user/groups` | `network groups` |
| `/my-user/groups/create` | `network group-create` |
| `/my-user/groups/{id}/members` | `network group-members-add` |
| `/myCanadianCustomsDeclaration` | `customs list` |
| `/policy` | `content policies` |
| `/policy-category` | `content policy-categories` |
| `/reserve-day` | `booking reserved-days` |
| `/shared-flight/book` | `booking shared-flight` |
| `/system-notice` | `content system-notice` |
| `/trip/book` | `booking create` |
| `/trip/manifest/send` | `trips manifest-send` |
| `/trip/misc-cost-estimate` | `quote cost` |
| `/user/2fa/disable` | `auth 2fa-disable` |
| `/user/2fa/setup` | `auth 2fa-setup` |
| `/user/2fa/sign-in` | `auth 2fa-sign-in` |
| `/user/2fa/verify` | `auth 2fa-verify` |
| `/user/authenticate` | `auth verify` |
| `/user/connections/invite/claim` | `network claim` |
| `/user/connections/request` | `network connect` |
| `/user/request-reset-password` | `auth reset-request` |
| `/user/reset-password` | `auth reset-confirm` |
| `/user/sign-in-email` | `auth login` |

## Deliberate API extensions

These commands are useful but are not claims about the Android source:

- The former `customs update-date` extension is retired; only unsubmitted local drafts can be corrected.
- `passport make-primary` reorders `passportIds` because
  `selectedPassportId` does not persist in the live owner API.
- `trips show` enriches the trip object with its manifest and uses AnyDoc first,
  with Poppler as the fallback PDF converter.
- A hidden `raw` group (gated by `--allow-raw`) remains for maintainers and
  contract debugging; it is not part of the agent surface and is not counted
  as Android coverage.

## Deliberate omissions

- `/file-public/create` (`app/features/account/data/remote_account_repository.dart`)
  and `/leg/recent/save` (`app/shared/repositories/flight/remote_flight_repository.dart`)
  were only reachable as raw JSON pass-throughs. They have no owner-facing form
  in the app (public-file creation and the "recent legs" cache write), so they
  are not exposed rather than kept as a JSON escape hatch.
- Share settings, airports, aircraft, and the departure date are not part of
  the app's required-information form, so `leg update-required-info` does not
  offer them.
- The note is absent from `booking empty-leg` and `booking shared-flight`
  because the Android model does not send it for existing flights. Android
  6.1.10 adds arrival-side transport, now available through
  `--arrival-ground-method`, `--arrival-pickup-address`, and
  `--arrival-dropoff-address` (`BookSharedRequestSettings.toJson`,
  0x720a3c–0x720a94).
- `GET /app-update` is the native application's update prompt. It sends
  `version` and optional `buildNumber` without an owner token
  (`RemoteAppUpdateRepository.check`, 6.1.10: 0x541688). It does not apply
  to this Python CLI and is intentionally omitted.
- `trips flight-feedback` was removed; it duplicated `booking survey` with a
  hand-written body.
- The seven payment-authorization keys the customs form can add to
  `/canadianCustomsDeclaration/create` (`cardType`, `cardName`,
  `phoneNumber`, `billingAddress`, `cardNumber`, `expiry`, `cvc`, each sent
  only when non-empty) are not offered by `customs create`: card data should
  not transit an agent's command line or logs. The declaration is still
  created; any payment authorization stays in the app.
