# Android 6.1.4 contract audit

This is the source-backed API contract for AirSprint CLI. The audit was made
offline from the Android 6.1.4 APK (version code 127), not by probing a booked
trip. The audited `base.apk` SHA-256 is:

```text
1d029d572731f9507864956e2f77155b641500a0319f368fbac4dab90328a477
```

The decompiled repository layer contains 111 calls to `APIClient.request`.
Seven are duplicate uses of the same method and normalized route, leaving 104
unique HTTP contracts:

| Method | Unique contracts |
|---|---:|
| POST | 65 |
| GET | 20 |
| PATCH | 11 |
| DELETE | 8 |
| **Total** | **104** |

Every one of these 104 contracts is reachable through a typed CLI command or,
for deliberately generic payload models, its guarded typed body command. The
comparison includes methods, bodyless requests, request envelopes, field names,
ID types, and high-risk no-probe behavior—not only matching URL text.

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
existing routes. The CLI now includes `files resolve` for Android's application
URL/avatar fallback and complete init → presigned multipart POST → attach
workflows in `passport upload-document` and `pet upload-document`.

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

## Complete route coverage

Dynamic IDs are normalized as `{id}`. Multiple CLI commands are shown where a
single Android route supports several app functions.

### GET (20)

| Contract | CLI coverage |
|---|---|
| `/aircraft/{id}` | `quote aircraft-get` |
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
| `/my-leg/{id}` | `trips leg-get` |
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
| `/booking-survey/create` | `trips flight-feedback`, `booking survey` |
| `/canadian-customs-declaration-link/create` | `customs link-create` |
| `/canadianCustomsDeclaration/create` | `customs create` |
| `/cancel-own` | `booking cancel` |
| `/concierge` | `content concierge` |
| `/empty-leg/book` | `booking empty-leg` |
| `/faq` | `content faq` |
| `/faq-category` | `content faq-categories` |
| `/feedback/create` | `feedback submit` |
| `/file-public/create` | `files public-create` |
| `/flight-quote` | `quote flight`, `quote roundtrip` |
| `/flight/lock` | `booking lock` |
| `/hours-exchange-listing/create` | `hours listing-create` |
| `/leg/recent/save` | `trips recent-save` |
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
| `/my-passport/document/attach` | `passport attach`, `passport upload-document` |
| `/my-passport/document/upload-init` | `passport upload-init`, `passport upload-document` |
| `/my-pet` | `pet list` |
| `/my-pet/create` | `pet create` |
| `/my-pet/document/attach` | `pet attach`, `pet upload-document` |
| `/my-pet/document/upload-init` | `pet upload-init`, `pet upload-document` |
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

These commands are useful but are not claims about Android 6.1.4 source:

- `customs update-date` uses the separately verified declaration PATCH.
- `passport make-primary` reorders `passportIds` because
  `selectedPassportId` does not persist in the live owner API.
- `trips show` enriches the trip object with its manifest and uses AnyDoc first,
  with Poppler as the fallback PDF converter.
- `raw` remains a guarded escape hatch and is not counted as Android coverage.
