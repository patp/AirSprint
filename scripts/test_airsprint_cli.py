import ast
import json
import re
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

import airsprint_cli as cli


class AirSprintCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.addCleanup(patch.stopall)
        patch("socket.create_connection", side_effect=AssertionError("Tests must stay offline")).start()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        patch.object(cli, "CUSTOMS_DRAFT_DIR", Path(self.temporary.name) / "drafts").start()
        self.passport_scan = Path(self.temporary.name) / "passport.jpg"
        self.passport_scan.write_bytes(b"\xff\xd8\xfftest-passport-scan")

    @staticmethod
    def passport_flags(date_of_birth: str = "1980-01-02", expiration: str = "2031-03-04") -> list:
        return [
            "--passenger-id", "passenger-1",
            "--passport-number", "ab123456",
            "--date-of-birth", date_of_birth,
            "--nationality", "ca",
            "--issuing-authority", "QUÉBEC",
            "--expiration-date", expiration,
        ]

    @staticmethod
    def passport_body(date_of_birth_ms: int = 315637200000, expiration_ms: int = 1930366800000) -> dict:
        return {
            "passengerId": "passenger-1",
            "image": "",
            "passportNumber": "AB123456",
            "dateOfBirth": date_of_birth_ms,
            "nationality": "CA",
            "issuingAuthority": "QUÉBEC",
            "expirationDate": expiration_ms,
        }

    def test_api_get_encodes_query_parameters(self) -> None:
        with patch.object(cli, "_http", return_value={}) as request:
            cli.api_get("token", "/hour-exchange/estimate", {
                "accountAircraftId": "aircraft id",
                "hours": 2,
                "type": "BUY",
            })

        _, url = request.call_args.args
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertIn("accountAircraftId=aircraft+id", url)
        self.assertIn("hours=2", url)
        self.assertIn("type=BUY", url)

    def test_live_trip_get_disables_ssl_retry(self) -> None:
        with patch.object(cli, "_http", return_value={}) as request:
            cli.api_get("token", "/trip/trip-id")

        self.assertFalse(request.call_args.kwargs["retry_first_ssl"])

    def test_first_safe_api_ssl_failure_retries_once(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        cli._API_REQUEST_COUNT = 0
        failure = cli.URLError(cli.ssl.SSLError("WRONG_VERSION_NUMBER"))
        with patch.object(cli, "urlopen", side_effect=[failure, Response()]) as request:
            result = cli._http(
                "GET",
                "https://api.airsprint.com/api/my-user",
                api_request=True,
                retry_first_ssl=True,
            )

        self.assertEqual(result, {})
        self.assertEqual(request.call_count, 2)

    def test_ssl_context_is_initialized_once_and_reused(self) -> None:
        context = object()
        with (
            patch.object(cli, "_SSL_CONTEXT", None),
            patch("truststore.inject_into_ssl") as inject,
            patch.object(cli.ssl, "create_default_context", return_value=context) as create,
        ):
            first = cli._ssl_ctx()
            second = cli._ssl_ctx()

        self.assertIs(first, context)
        self.assertIs(second, context)
        inject.assert_called_once_with()
        create.assert_called_once_with()

    def test_data_cache_is_memory_cached_and_airports_are_indexed(self) -> None:
        cache_data = {
            "airports": {
                "_cached_at": 1,
                "by_icao": {
                    "KTEB": {"id": "airport-id", "country": "United States"},
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            cli._atomic_write_json(cache_path, cache_data)
            with (
                patch.object(cli, "DATA_CACHE", cache_path),
                patch.object(cli, "_DATA_CACHE_MEMORY", None),
                patch.object(cli, "_DATA_CACHE_MEMORY_MTIME_NS", None),
                patch.object(cli, "_DATA_CACHE_MEMORY_PATH", None),
                patch.object(cli, "_AIRPORT_BY_ID", None),
            ):
                first = cli._load_data_cache()
                second = cli._load_data_cache()
                airport = cli._airport_country("airport-id")

        self.assertIs(first, second)
        self.assertEqual(airport, ("United States", "KTEB"))

    def test_private_json_write_is_atomic_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            cli._atomic_write_json(path, {"ok": True})
            payload = json.loads(path.read_text())
            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(mode, 0o600)

    def test_account_lookup_uses_short_lived_cache(self) -> None:
        response = {"data": {"items": [{"id": "account-id"}]}}
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            with (
                patch.object(cli, "DATA_CACHE", cache_path),
                patch.object(cli, "_DATA_CACHE_MEMORY", None),
                patch.object(cli, "_DATA_CACHE_MEMORY_MTIME_NS", None),
                patch.object(cli, "_DATA_CACHE_MEMORY_PATH", None),
                patch.object(cli, "_AIRPORT_BY_ID", None),
                patch.object(cli, "api_post", return_value=response) as request,
            ):
                first = cli._get_account_ids("token")
                second = cli._get_account_ids("token")

        self.assertEqual(first, ["account-id"])
        self.assertEqual(second, ["account-id"])
        request.assert_called_once_with("token", "/my-accounts")

    def test_personalized_cache_is_invalidated_for_a_different_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            with (
                patch.object(cli, "DATA_CACHE", cache_path),
                patch.object(cli, "_DATA_CACHE_MEMORY", None),
                patch.object(cli, "_DATA_CACHE_MEMORY_MTIME_NS", None),
                patch.object(cli, "_DATA_CACHE_MEMORY_PATH", None),
                patch.object(cli, "_AIRPORT_BY_ID", None),
                patch.object(cli, "api_post") as request,
            ):
                request.side_effect = [
                    {"data": {"items": [{"id": "account-a"}]}},
                    {"data": {"items": [{"id": "account-b"}]}},
                ]
                first = cli._get_account_ids("token-a")
                second = cli._get_account_ids("token-b")

        self.assertEqual(first, ["account-a"])
        self.assertEqual(second, ["account-b"])
        self.assertEqual(request.call_count, 2)

    def test_independent_read_calls_run_concurrently(self) -> None:
        barrier = threading.Barrier(3)

        def task(value):
            barrier.wait(timeout=1)
            return {"value": value}

        with patch.object(cli, "_ssl_ctx"):
            result = cli._parallel_read_calls({
                "one": lambda: task(1),
                "two": lambda: task(2),
                "three": lambda: task(3),
            })

        self.assertEqual(result, {
            "one": {"value": 1},
            "two": {"value": 2},
            "three": {"value": 3},
        })

    def test_explore_counts_batches_only_safe_list_reads(self) -> None:
        responses = {
            "notifications": {"data": {"total": 2}},
            "upcoming": {"data": {"total": 3}},
            "empty_legs": {"data": {"total": 4}},
        }
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_get_account_ids", return_value=["account-id"]),
            patch.object(
                cli, "_parallel_read_calls", return_value=responses
            ) as parallel,
        ):
            result = self.runner.invoke(cli.app, ["explore", "counts"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["data"], {
            "unreadMessages": 2,
            "upcomingTrips": 3,
            "emptyLegs": 4,
        })
        self.assertEqual(
            set(parallel.call_args.args[0]),
            {"notifications", "upcoming", "empty_legs"},
        )

    def test_trips_get_defaults_to_no_probe_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "write.json"
            with patch.object(cli, "BOOKING_WRITE_GUARD", marker):
                cli._record_booking_write("/leg/leg-id")
                with patch.object(cli, "get_api_token") as token:
                    result = self.runner.invoke(cli.app, [
                        "trips", "get", "--id", "trip-id",
                    ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("No booking probe sent", result.output)
        token.assert_not_called()

    def test_hours_estimate_uses_get_and_explicit_flags(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(
                cli, "api_get", return_value={"data": {"totalPrice": 123}}
            ) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "hours", "estimate",
                "--hours", "2",
                "--type", "buy",
                "--account-aircraft-id", "account-aircraft-id",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/hour-exchange/estimate", {
            "accountAircraftId": "account-aircraft-id",
            "hours": 2.0,
            "type": "BUY",
        })

    def test_notification_settings_match_android_get_and_patch_contract(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value={"data": {}}) as read,
            patch.object(cli, "api_patch", return_value={"data": {}}) as write,
        ):
            get_result = self.runner.invoke(cli.app, ["messages", "settings"])
            update_result = self.runner.invoke(cli.app, [
                "messages", "settings-update", "--on", "weeklyDigest",
            ])

        self.assertEqual(get_result.exit_code, 0, get_result.output)
        self.assertEqual(update_result.exit_code, 0, update_result.output)
        read.assert_called_once_with("token", "/my-notification-settings")
        write.assert_called_once_with(
            "token",
            "/my-notification-settings/update",
            {"options": {"weeklyDigest": True}},
        )

    def test_user_set_preferences_uses_same_android_patch_contract(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_patch", return_value={}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "user", "set-preferences", "--off", "airsprintPromotions",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with(
            "token",
            "/my-notification-settings/update",
            {"options": {"airsprintPromotions": False}},
        )

    def test_account_user_update_uses_patch(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_patch", return_value={}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "account", "user-update",
                "--ids", "user-id",
                "--roles", "OWNER",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/account-user/update", {
            "ids": ["user-id"],
            "options": {"roleNames": ["OWNER"]},
        })

    def test_reserved_days_is_read_only_android_list_request(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(
                cli,
                "api_post",
                return_value={"data": {"items": [{"id": "day-1"}, {"id": "day-2"}]}},
            ) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "booking", "reserved-days", "--limit", "1",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/reserve-day", {
            "sort": [],
            "page": {"limit": 1, "offset": 0},
        })
        self.assertEqual(json.loads(result.output)["data"], [{"id": "day-1"}])

    def test_recent_leg_searches_use_get_and_limit_locally(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(
                cli,
                "api_get",
                return_value={"data": {"items": [{"id": "one"}, {"id": "two"}]}},
            ) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "trips", "recent", "--limit", "1",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/leg/recent/list")
        self.assertEqual(json.loads(result.output)["data"], [{"id": "one"}])

    def test_airport_nearest_uses_flat_android_payload(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={"data": {"items": []}}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "quote", "airport-nearest", "--lat", "45.5", "--lng", "-73.6",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/airport/nearest", {
            "latitude": 45.5,
            "longitude": -73.6,
        })

    def test_saved_airports_use_airport_saved_filter(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={"data": {"items": []}}) as request,
        ):
            result = self.runner.invoke(cli.app, ["quote", "saved-airports"])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/airport", {
            "sort": [],
            "page": {"limit": 100, "offset": 0},
            "filter": {"saved": True},
        })

    def test_address_autocomplete_uses_android_field_names(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={"data": {"items": []}}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "address", "autocomplete",
                "--query", " 1 Main ",
                "--city", "Montreal",
                "--state", "QC",
                "--country-code", "CA",
                "--session-token", "session-id",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/address/autocomplete", {
            "input": "1 Main",
            "language": "en",
            "city": "Montreal",
            "stateOrProvince": "QC",
            "sessionToken": "session-id",
            "countryCode": "ca",
        })

    def test_customs_link_create_uses_exact_leg_id_payload(self) -> None:
        result = self.runner.invoke(cli.app, [
            "customs", "link-create", "--leg-id", "leg-id", "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertEqual(data["path"], "/canadian-customs-declaration-link/create")
        self.assertEqual(data["payload"], {"legId": "leg-id"})

    def test_removed_routes_are_not_advertised(self) -> None:
        removed = {
            "account": "user-get",
            "passport": "get",
            "customs": "create-public",
            "content": "required-info",
            "network": "group-get",
            "user": "me",
            "trips": "flight-feedback",
        }
        root_command = cli.typer.main.get_command(cli.app)
        for group, command in removed.items():
            with self.subTest(group=group, command=command):
                group_command = root_command.commands[group]
                self.assertNotIn(command, group_command.commands)

    def test_all_booking_post_writes_set_the_probe_guard(self) -> None:
        with (
            patch.object(cli, "_record_booking_write") as record,
            patch.object(cli, "_http", return_value={}),
        ):
            for path in sorted(cli._BOOKING_WRITE_POST_PATHS):
                cli.api_post("token", path, {})

        self.assertEqual(
            [call.args[0] for call in record.call_args_list],
            sorted(cli._BOOKING_WRITE_POST_PATHS),
        )

    def test_network_group_create_dry_run_has_current_payload(self) -> None:
        result = self.runner.invoke(cli.app, [
            "network", "group-create",
            "--name", "Family",
            "--members", "user-1,user-2",
            "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertEqual(data["path"], "/my-user/groups/create")
        self.assertEqual(data["payload"], {
            "name": "Family",
            "memberIds": ["user-1", "user-2"],
        })

    def test_delete_requires_confirmation(self) -> None:
        with patch.object(cli, "api_delete") as request:
            result = self.runner.invoke(cli.app, [
                "passenger", "delete", "--id", "passenger-id",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        request.assert_not_called()

    def test_raw_patch_requires_confirmation(self) -> None:
        with patch.object(cli, "api_patch") as request:
            result = self.runner.invoke(cli.app, [
                "raw", "api-patch", "--path", "/leg/leg-id", "--body", "{}", "--allow-raw",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        request.assert_not_called()

    def test_raw_json_body_must_be_an_object(self) -> None:
        result = self.runner.invoke(cli.app, [
            "raw", "api-patch", "--path", "/my-user", "--body", "[]", "--dry-run", "--allow-raw",
        ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("must be an object", result.output)

    def test_raw_group_is_hidden_and_gated_for_maintainers(self) -> None:
        root = cli.typer.main.get_command(cli.app)
        self.assertTrue(root.commands["raw"].hidden)
        self.assertNotIn("raw", self.runner.invoke(cli.app, ["--help"]).output)
        for name, extra in (
            ("api-get", []),
            ("api-post", ["--body", "{}"]),
            ("api-patch", ["--body", "{}", "--dry-run"]),
            ("api-delete", ["--dry-run"]),
        ):
            with self.subTest(command=name), patch.object(cli, "get_api_token") as token:
                result = self.runner.invoke(cli.app, ["raw", name, "--path", "/my-user", *extra])
                self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
                self.assertIn("reserved for maintainers", result.output)
                token.assert_not_called()

    def test_public_commands_do_not_accept_raw_json(self) -> None:
        """Agents only ever answer typed questions; raw JSON stays in the hidden raw group."""
        root = cli.typer.main.get_command(cli.app)
        offenders = []
        for group_name, group in sorted(root.commands.items()):
            if group_name == "raw":
                continue
            commands = getattr(group, "commands", {group_name: group})
            for command_name, command in commands.items():
                for param in command.params:
                    if any(opt in {"--body", "--json", "--payload", "--options"} for opt in param.opts):
                        offenders.append(f"{group_name} {command_name} {param.opts}")
        self.assertEqual(offenders, [])

    def test_cache_status_accepts_compact_output(self) -> None:
        with patch.object(cli, "_load_data_cache", return_value={}):
            result = self.runner.invoke(cli.app, ["cache", "status", "--compact"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["data"]["exists"], False)

    def test_cache_refresh_persists_all_sections_once(self) -> None:
        cache = {}
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_load_data_cache", return_value=cache),
            patch.object(cli, "_prepare_cache_for_token"),
            patch.object(cli, "_refresh_accounts", return_value=[]),
            patch.object(cli, "_refresh_airports"),
            patch.object(cli, "_refresh_aircraft"),
            patch.object(cli, "_refresh_my_aircraft"),
            patch.object(cli, "_save_data_cache") as save,
        ):
            result = self.runner.invoke(cli.app, ["cache", "refresh"])

        self.assertEqual(result.exit_code, 0, result.output)
        save.assert_called_once_with(cache)

    def test_epoch_formatter_recognizes_historical_milliseconds(self) -> None:
        self.assertEqual(
            cli._fmt_epoch(315619200000, fmt="%Y-%m-%d"),
            "1980-01-02",
        )

    # -- booking create: the typed form must produce Android 6.1.4's TripBookRequestPayload --

    def booking_create_patches(self, airport_country=("Canada", "CYUL")):
        return (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_resolve_airport", side_effect=lambda token, code: f"{code.lower()}-id"),
            patch.object(cli, "_get_default_aircraft", return_value="aircraft-id"),
            patch.object(cli, "_airport_country", return_value=airport_country),
            patch.object(cli, "api_get", return_value={"data": {
                "passportIds": ["passport-1", "passport-9"],
                "passports": [{"id": "passport-1", "image": "passport/scan.jpg"}],
            }}),
        )

    def test_booking_create_builds_exact_android_trip_book_body(self) -> None:
        patches = self.booking_create_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4] as read, patch.object(cli, "api_post") as post, patch.object(cli, "_require_booking_share_eligibility", return_value={"basis": "owned-aircraft"}):
            result = self.runner.invoke(cli.app, [
                "booking", "create",
                "--leg", "CYUL>CYYZ@2026-09-01T16:00",
                "--leg", "CYYZ>CYUL@2026-09-03T09:30",
                "--passengers", "saved-1,saved-2",
                "--passport", "saved-2=passport-2",
                "--pets", "pet-1",
                "--baggage", "Golf bag=2",
                "--baggage", "Suitcase=1",
                "--catering", "yes",
                "--catering-request", "Sushi for two",
                "--ground-transportation", "yes",
                "--ground-transportation-when", "both",
                "--ground-transportation-method", "suv-and-driver",
                "--ground-pickup-address", "1 Main St; Montreal; QC; H1A 1A1; Suite 4",
                "--ground-dropoff-address", "2 King St; Toronto; ON; M5H 1A1",
                "--arrival-ground-method", "taxi",
                "--note", "Early boarding please",
                "--dog-form-submitted", "no",
                "--special-requests", "Window seats",
                "--open-to-share", "yes",
                "--share-network", "airsprint-network",
                "--share-groups", "group-1",
                "--share-seats", "2",
                "--share-pets-allowed", "yes",
                "--share-children-allowed", "no",
                "--share-cost-percentage", "60",
                "--dry-run",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_not_called()
        # Only saved-1 needed a passport lookup; saved-2 was answered with --passport.
        read.assert_called_once_with("token", "/my-passenger/saved-1")
        data = json.loads(result.output)["data"]
        self.assertEqual(data["path"], "/trip/book")
        payload = data["payload"]
        self.assertEqual(list(payload), ["legs", "dogFormSubmitted", "baggage", "shareSettings"])
        request_settings = {
            "cateringRequired": True,
            "groundTransportationRequired": True,
            "cateringRequest": "Sushi for two",
            "groundTransportationType": "BOTH",
            "groundTransportationMethod": "SUV_AND_DRIVER",
            "groundTransportationPickUpAddress": {
                "street": "1 Main St", "city": "Montreal", "state": "QC", "zip": "H1A 1A1", "street2": "Suite 4",
            },
            "groundTransportationDropOffAddress": {
                "street": "2 King St", "city": "Toronto", "state": "ON", "zip": "M5H 1A1",
            },
            "arrivalGroundTransportationMethod": "TAXI",
            "note": "Early boarding please",
        }
        passengers = [
            {"id": "saved-1", "passport": {"id": "passport-1"}},
            {"id": "saved-2", "passport": {"id": "passport-2"}},
        ]
        self.assertEqual(payload, {
            "legs": [
                {
                    "departureAirportId": "cyul-id",
                    "arrivalAirportId": "cyyz-id",
                    "aircraftId": "aircraft-id",
                    "date": "2026-09-01T16:00:00.000",
                    "numberOfSeats": 2,
                    "passengers": passengers,
                    "petIds": ["pet-1"],
                    "requestSettings": request_settings,
                },
                {
                    "departureAirportId": "cyyz-id",
                    "arrivalAirportId": "cyul-id",
                    "aircraftId": "aircraft-id",
                    "date": "2026-09-03T09:30:00.000",
                    "numberOfSeats": 2,
                    "passengers": passengers,
                    "petIds": ["pet-1"],
                    "requestSettings": request_settings,
                },
            ],
            "dogFormSubmitted": False,
            "baggage": [{"name": "Golf bag", "quantity": 2}, {"name": "Suitcase", "quantity": 1}],
            "shareSettings": {
                "specialRequests": "Window seats",
                "openToShare": True,
                "networkType": "AIRSPRINT_NETWORK",
                "specificGroupsOnly": True,
                "groupIds": ["group-1"],
                "seats": 2,
                "petsAllowed": True,
                "childrenAllowed": False,
                "joinerVariableCostPercentage": 60,
            },
        })
        self.assertEqual(data["routeCheck"]["usTouching"], False)
        self.assertIn("exactly once", data["message"])

    def test_booking_create_minimal_form_uses_android_defaults(self) -> None:
        patches = self.booking_create_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none", "--dry-run",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)["data"]["payload"]
        self.assertEqual(payload["baggage"], [])
        self.assertIs(payload["dogFormSubmitted"], False)
        self.assertEqual(payload["shareSettings"], {
            "specialRequests": "",
            "openToShare": False,
            "networkType": "MY_NETWORK",
            "specificGroupsOnly": False,
            "groupIds": [],
            "seats": 0,
            "petsAllowed": False,
            "childrenAllowed": False,
            "joinerVariableCostPercentage": 50,
        })
        leg = payload["legs"][0]
        self.assertEqual(leg["numberOfSeats"], 1)
        self.assertEqual(leg["petIds"], [])
        self.assertEqual(leg["requestSettings"], {"cateringRequired": False, "groundTransportationRequired": False})
        self.assertEqual(leg["passengers"], [{"id": "saved-1", "passport": {"id": "passport-1"}}])

    def test_booking_create_accepts_airport_ids_without_lookup(self) -> None:
        departure = "11111111-2222-3333-4444-555555555555"
        arrival = "66666666-7777-8888-9999-000000000000"
        patches = self.booking_create_patches()
        with patches[0], patches[1] as resolve, patches[2], patches[3], patches[4]:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", f"{departure}>{arrival}@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none", "--dry-run",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        resolve.assert_not_called()
        leg = json.loads(result.output)["data"]["payload"]["legs"][0]
        self.assertEqual((leg["departureAirportId"], leg["arrivalAirportId"]), (departure, arrival))

    def test_booking_create_requires_an_explicit_baggage_answer(self) -> None:
        patches = self.booking_create_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(cli, "api_post") as post:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
                "--passengers", "saved-1", "--dry-run",
            ])

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("--baggage", result.output)
        post.assert_not_called()

    def test_booking_baggage_none_cannot_be_mixed_with_items(self) -> None:
        patches = self.booking_create_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none", "--baggage", "Suitcase=1", "--dry-run",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("cannot be combined", result.output)

    def test_booking_baggage_quantity_must_be_a_positive_integer(self) -> None:
        patches = self.booking_create_patches()
        for bad in ("Suitcase=0", "Suitcase=two", "Suitcase"):
            with self.subTest(baggage=bad), patches[0], patches[1], patches[2], patches[3], patches[4]:
                result = self.runner.invoke(cli.app, [
                    "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
                    "--passengers", "saved-1", "--baggage", bad, "--dry-run",
                ])
                self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)

    def test_booking_share_percentage_is_limited_to_app_range(self) -> None:
        patches = self.booking_create_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none",
                "--share-cost-percentage", "20", "--dry-run",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("between 30 and 80", result.output)

    def test_booking_create_never_sends_account_id(self) -> None:
        patches = self.booking_create_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none", "--dry-run",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("accountId", result.output)

    def test_booking_leg_time_is_departure_wall_clock_without_zone(self) -> None:
        for bad in ("2026-09-01", "2026-09-01T16:00:00Z", "2026-09-01T16:00-04:00", "tomorrow"):
            with self.subTest(when=bad):
                result = self.runner.invoke(cli.app, [
                    "booking", "create", "--leg", f"CYUL>CYYZ@{bad}",
                    "--passengers", "saved-1", "--baggage", "none", "--dry-run",
                ])
                self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
                self.assertIn("YYYY-MM-DDTHH:MM", result.output)

    def test_booking_leg_spec_must_have_route_and_time(self) -> None:
        for bad in ("CYUL-CYYZ@2026-09-01T16:00", "CYUL>CYYZ", ">CYYZ@2026-09-01T16:00"):
            with self.subTest(leg=bad):
                result = self.runner.invoke(cli.app, [
                    "booking", "create", "--leg", bad, "--passengers", "saved-1", "--baggage", "none", "--dry-run",
                ])
                self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
                self.assertIn("--leg must look like", result.output)

    def test_booking_ground_details_require_ground_transportation_yes(self) -> None:
        result = self.runner.invoke(cli.app, [
            "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
            "--passengers", "saved-1", "--baggage", "none",
            "--ground-transportation-method", "taxi", "--dry-run",
        ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("--ground-transportation yes", result.output)

        result = self.runner.invoke(cli.app, [
            "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
            "--passengers", "saved-1", "--baggage", "none",
            "--catering-request", "Sushi", "--dry-run",
        ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("--catering yes", result.output)

    def test_booking_passport_option_must_name_a_listed_passenger(self) -> None:
        result = self.runner.invoke(cli.app, [
            "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
            "--passengers", "saved-1", "--passport", "saved-9=passport-9", "--baggage", "none", "--dry-run",
        ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("saved-9", result.output)

    def test_us_booking_requires_and_copies_destination_address(self) -> None:
        patches = self.booking_create_patches(airport_country=("United States", "KTEB"))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = self.runner.invoke(cli.app, [
                "booking", "create",
                "--leg", "CYUL>KTEB@2026-09-01T16:00",
                "--leg", "KTEB>CYUL@2026-09-03T09:00",
                "--passengers", "saved-1,saved-2",
                "--baggage", "none",
                "--destination-street", "1 Main St",
                "--destination-street2", "Apt 5",
                "--destination-city", "New York",
                "--destination-state", "NY",
                "--destination-zip", "10001",
                "--dry-run",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertIs(data["routeCheck"]["usTouching"], True)
        expected = {"street": "1 Main St", "city": "New York", "state": "NY", "zip": "10001", "street2": "Apt 5"}
        for leg in data["payload"]["legs"]:
            self.assertEqual(len(leg["passengers"]), 2)
            for passenger in leg["passengers"]:
                self.assertEqual(passenger["destinationAddress"], expected)
                self.assertNotIn("country", passenger["destinationAddress"])

    def test_us_booking_without_destination_address_is_refused(self) -> None:
        patches = self.booking_create_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(cli, "api_post") as post:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>KTEB@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none", "--us-touching", "--dry-run",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("--destination-street", result.output)
        self.assertIn("No booking was sent", result.output)
        post.assert_not_called()

    def test_destination_address_is_all_or_nothing(self) -> None:
        result = self.runner.invoke(cli.app, [
            "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
            "--passengers", "saved-1", "--baggage", "none",
            "--destination-street", "1 Main St", "--dry-run",
        ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("--destination-city", result.output)
        self.assertIn("--destination-zip", result.output)

    def test_booking_create_posts_the_built_body_once(self) -> None:
        patches = self.booking_create_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(cli, "api_post", return_value={"data": {"id": "trip-1"}}) as post:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "Suitcase=1",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[:2], ("token", "/trip/book"))
        self.assertEqual(post.call_args.args[2]["baggage"], [{"name": "Suitcase", "quantity": 1}])

    # -- border-crossing guard rails --

    @staticmethod
    def airport_country_by_id(airport_id):
        return {
            "cyul-id": ("Canada", "CYUL"),
            "cyyz-id": ("Canada", "CYYZ"),
            "kteb-id": ("United States", "KTEB"),
        }.get(airport_id, (None, None))

    def test_international_booking_requires_a_passport_for_every_passenger(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_resolve_airport", side_effect=lambda token, code: f"{code.lower()}-id"),
            patch.object(cli, "_get_default_aircraft", return_value="aircraft-id"),
            patch.object(cli, "_airport_country", side_effect=self.airport_country_by_id),
            patch.object(cli, "api_get", side_effect=lambda token, path: {"data": {"passportIds": ["passport-1"] if path.endswith("saved-1") else []}}),
            patch.object(cli, "api_post") as post,
        ):
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>KTEB@2026-09-01T16:00",
                "--passengers", "saved-1,saved-2", "--baggage", "none",
                "--destination-street", "1 Main St", "--destination-city", "New York",
                "--destination-state", "NY", "--destination-zip", "10001",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("passengers without a passport on file: saved-2", result.output)
        self.assertIn("No booking was sent", result.output)
        post.assert_not_called()

    def international_document_patches(self, passenger):
        """Patches for a Canada->US booking whose passenger read is `passenger`."""
        return (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_resolve_airport", side_effect=lambda token, code: f"{code.lower()}-id"),
            patch.object(cli, "_get_default_aircraft", return_value="aircraft-id"),
            patch.object(cli, "_airport_country", side_effect=self.airport_country_by_id),
            patch.object(cli, "api_get", **passenger),
        )

    def test_international_booking_refuses_a_passport_without_a_photo_scan(self) -> None:
        passenger = {"data": {"passportIds": ["passport-1"], "passports": [{"id": "passport-1", "image": ""}]}}
        patches = self.international_document_patches({"return_value": passenger})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(cli, "api_post") as post:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>KTEB@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none",
                "--destination-street", "1 Main St", "--destination-city", "New York",
                "--destination-state", "NY", "--destination-zip", "10001",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("no photo/scan uploaded: saved-1", result.output)
        self.assertIn("passport upload-document", result.output)
        self.assertIn("No booking was sent", result.output)
        post.assert_not_called()

    def test_international_booking_accepts_a_passport_with_a_photo_scan(self) -> None:
        passenger = {"data": {"passportIds": ["passport-1"], "passports": [{"id": "passport-1", "image": "passport/scan.jpg"}]}}
        patches = self.international_document_patches({"return_value": passenger})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(cli, "api_post") as post:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>KTEB@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none",
                "--destination-street", "1 Main St", "--destination-city", "New York",
                "--destination-state", "NY", "--destination-zip", "10001", "--dry-run",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertIs(data["routeCheck"]["international"], True)
        post.assert_not_called()

    def test_international_booking_verifies_scan_for_a_passport_answered_passenger(self) -> None:
        # --passport answers the id, so the passenger read is needed only for the
        # scan check; passport-9 has no image, so the booking is still refused.
        passenger = {"data": {"passportIds": ["passport-9"], "passports": [{"id": "passport-9", "image": ""}]}}
        patches = self.international_document_patches({"return_value": passenger})
        with patches[0], patches[1], patches[2], patches[3], patches[4] as read, patch.object(cli, "api_post") as post:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>KTEB@2026-09-01T16:00",
                "--passengers", "saved-1", "--passport", "saved-1=passport-9", "--baggage", "none",
                "--destination-street", "1 Main St", "--destination-city", "New York",
                "--destination-state", "NY", "--destination-zip", "10001",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("no photo/scan uploaded: saved-1", result.output)
        read.assert_called_once_with("token", "/my-passenger/saved-1")
        post.assert_not_called()

    def test_international_booking_reports_missing_passport_and_missing_scan_together(self) -> None:
        def read(token, path):
            if path.endswith("saved-1"):
                return {"data": {"passportIds": ["passport-1"], "passports": [{"id": "passport-1", "image": ""}]}}
            return {"data": {"passportIds": [], "passports": []}}

        patches = self.international_document_patches({"side_effect": read})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(cli, "api_post") as post:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>KTEB@2026-09-01T16:00",
                "--passengers", "saved-1,saved-2", "--baggage", "none",
                "--destination-street", "1 Main St", "--destination-city", "New York",
                "--destination-state", "NY", "--destination-zip", "10001",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("without a passport on file: saved-2", result.output)
        self.assertIn("no photo/scan uploaded: saved-1", result.output)
        post.assert_not_called()

    def test_international_booking_requires_a_destination_even_outside_the_us(self) -> None:
        countries = {"cyul-id": ("Canada", "CYUL"), "mmmx-id": ("Mexico", "MMMX")}
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_resolve_airport", side_effect=lambda token, code: f"{code.lower()}-id"),
            patch.object(cli, "_get_default_aircraft", return_value="aircraft-id"),
            patch.object(cli, "_airport_country", side_effect=lambda airport_id: countries.get(airport_id, (None, None))),
            patch.object(cli, "api_get", return_value={"data": {
                "passportIds": ["passport-1"], "passports": [{"id": "passport-1", "image": "passport/scan.jpg"}],
            }}),
            patch.object(cli, "api_post") as post,
        ):
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>MMMX@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none", "--dry-run",
            ])
            self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
            self.assertIn("destination address (hotel", result.output)
            post.assert_not_called()

            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>MMMX@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none",
                "--destination-street", "Av. Reforma 1", "--destination-city", "Mexico City",
                "--destination-state", "CDMX", "--destination-zip", "06600", "--dry-run",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertEqual(data["routeCheck"]["usTouching"], False)
        self.assertEqual(data["routeCheck"]["international"], True)
        passenger = data["payload"]["legs"][0]["passengers"][0]
        self.assertEqual(passenger["passport"], {"id": "passport-1"})
        self.assertEqual(passenger["destinationAddress"]["city"], "Mexico City")

    def test_domestic_booking_needs_neither_passport_nor_destination(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_resolve_airport", side_effect=lambda token, code: f"{code.lower()}-id"),
            patch.object(cli, "_get_default_aircraft", return_value="aircraft-id"),
            patch.object(cli, "_airport_country", side_effect=self.airport_country_by_id),
            patch.object(cli, "api_get", return_value={"data": {"passportIds": []}}),
        ):
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none", "--dry-run",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertEqual(data["routeCheck"], {
            "usTouching": False,
            "international": False,
            "airports": [
                {"airportId": "cyul-id", "icao": "CYUL", "country": "Canada"},
                {"airportId": "cyyz-id", "icao": "CYYZ", "country": "Canada"},
            ],
        })
        self.assertEqual(data["payload"]["legs"][0]["passengers"], [{"id": "saved-1"}])

    def test_unknown_airport_countries_need_explicit_route_answers(self) -> None:
        departure = "11111111-2222-3333-4444-555555555555"
        arrival = "66666666-7777-8888-9999-000000000000"
        base = [
            "booking", "create", "--leg", f"{departure}>{arrival}@2026-09-01T16:00",
            "--passengers", "saved-1", "--baggage", "none", "--dry-run",
        ]
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_get_default_aircraft", return_value="aircraft-id"),
            patch.object(cli, "_airport_country", return_value=(None, None)),
            patch.object(cli, "api_get", return_value={"data": {"passportIds": []}}),
        ):
            result = self.runner.invoke(cli.app, base)
            self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
            self.assertIn("--us-touching/--not-us-touching", result.output)

            result = self.runner.invoke(cli.app, [*base, "--not-us-touching"])
            self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
            self.assertIn("--international/--domestic", result.output)

            result = self.runner.invoke(cli.app, [*base, "--not-us-touching", "--domestic"])
            self.assertEqual(result.exit_code, 0, result.output)

            result = self.runner.invoke(cli.app, [*base, "--not-us-touching", "--international"])
            self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
            self.assertIn("without a passport on file: saved-1", result.output)

    def test_icao_prefixes_detect_a_border_crossing_when_the_cache_is_silent(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_resolve_airport", side_effect=lambda token, code: f"{code.lower()}-id"),
            patch.object(cli, "_get_default_aircraft", return_value="aircraft-id"),
            patch.object(cli, "_airport_country", return_value=(None, None)),
            patch.object(cli, "api_get", return_value={"data": {"passportIds": []}}),
        ):
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>KTEB@2026-09-01T16:00",
                "--passengers", "saved-1", "--baggage", "none", "--dry-run",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("without a passport on file: saved-1", result.output)

    def test_passenger_gender_is_required_and_x_maps_to_android_none(self) -> None:
        result = self.runner.invoke(cli.app, [
            "passenger", "create", "--first-name", "Sam", "--last-name", "Roy", "--save-profile", "yes", "--dry-run",
        ])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("--gender", result.output)

        result = self.runner.invoke(cli.app, [
            "passenger", "create", "--first-name", "Sam", "--last-name", "Roy", "--gender", "x",
            "--save-profile", "yes", "--dry-run",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["data"]["payload"]["gender"], "NONE")

    # -- booking empty-leg / shared-flight: Android BookSharedRequest --

    def test_existing_flight_bookings_build_android_book_shared_request(self) -> None:
        for command, path in (("empty-leg", "/empty-leg/book"), ("shared-flight", "/shared-flight/book")):
            with self.subTest(command=command), \
                    patch.object(cli, "get_api_token", return_value="token"), \
                    patch.object(cli, "api_post", return_value={}) as post:
                result = self.runner.invoke(cli.app, [
                    "booking", command,
                    "--flight-id", "flight-1",
                    "--passengers", "saved-1,saved-2",
                    "--destination-street", "1 Main St",
                    "--destination-city", "New York",
                    "--destination-state", "NY",
                    "--destination-zip", "10001",
                    "--pets", "pet-1,pet-2",
                    "--baggage", "Suitcase=2",
                    "--catering", "yes",
                    "--catering-request", "Coffee",
                    "--ground-transportation", "yes",
                    "--ground-transportation-when", "arrival",
                    "--ground-transportation-method", "limo-and-driver",
                    "--ground-pickup-address", "1 Main St; New York; NY; 10001",
                ])
                self.assertEqual(result.exit_code, 0, result.output)
                address = {"street": "1 Main St", "city": "New York", "state": "NY", "zip": "10001"}
                post.assert_called_once_with("token", path, {
                    "flightId": "flight-1",
                    "options": {
                        "passengers": [
                            {"id": "saved-1", "destinationAddress": address},
                            {"id": "saved-2", "destinationAddress": address},
                        ],
                        "requestSettings": {
                            "cateringRequired": True,
                            "groundTransportationRequired": True,
                            "cateringRequest": "Coffee",
                            "groundTransportationType": "ARRIVAL",
                            "groundTransportationMethod": "LIMO_AND_DRIVER",
                            "groundTransportationPickUpAddress": address,
                        },
                        "petIds": ["pet-1", "pet-2"],
                        "baggage": [{"name": "Suitcase", "quantity": 2}],
                    },
                })

    def test_existing_flight_booking_minimal_form_and_baggage_rule(self) -> None:
        with patch.object(cli, "get_api_token", return_value="token"), patch.object(cli, "api_post") as post:
            result = self.runner.invoke(cli.app, [
                "booking", "shared-flight", "--flight-id", "flight-1", "--passengers", "saved-1", "--dry-run",
            ])
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertIn("--baggage", result.output)

            result = self.runner.invoke(cli.app, [
                "booking", "shared-flight", "--flight-id", "flight-1", "--passengers", "saved-1",
                "--baggage", "none", "--dry-run",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_not_called()
        data = json.loads(result.output)["data"]
        self.assertEqual(data["path"], "/shared-flight/book")
        self.assertEqual(data["payload"], {
            "flightId": "flight-1",
            "options": {
                "passengers": [{"id": "saved-1"}],
                "requestSettings": {"cateringRequired": False, "groundTransportationRequired": False},
            },
        })

    def test_existing_flight_booking_omits_empty_pets_and_baggage_but_compact_keeps_preview(self) -> None:
        """BookSharedRequestOptions.toJson adds petIds/baggage only for non-empty lists; --compact never edits a dry run."""
        result = self.runner.invoke(cli.app, [
            "booking", "empty-leg", "--flight-id", "flight-1", "--passengers", "saved-1",
            "--baggage", "none", "--dry-run", "--compact",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        options = json.loads(result.output)["data"]["payload"]["options"]
        self.assertEqual(list(options), ["passengers", "requestSettings"])
        self.assertEqual(options["requestSettings"], {"cateringRequired": False, "groundTransportationRequired": False})

        result = self.runner.invoke(cli.app, [
            "booking", "empty-leg", "--flight-id", "flight-1", "--passengers", "saved-1",
            "--baggage", "Suitcase=1", "--pets", "pet-1", "--dry-run", "--compact",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        options = json.loads(result.output)["data"]["payload"]["options"]
        self.assertEqual(list(options), ["passengers", "requestSettings", "petIds", "baggage"])

    def test_compact_dry_run_keeps_empty_values_the_app_sends(self) -> None:
        """TripBookRequestPayload always sends baggage, petIds and every share setting; --compact must not hide them."""
        patches = self.booking_create_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = self.runner.invoke(cli.app, [
                "booking", "create", "--leg", "CYUL>CYYZ@2026-09-01T09:00", "--passengers", "saved-1",
                "--baggage", "none", "--dry-run", "--compact",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)["data"]["payload"]
        self.assertEqual(payload["baggage"], [])
        self.assertEqual(payload["legs"][0]["petIds"], [])
        self.assertEqual(payload["shareSettings"]["specialRequests"], "")
        self.assertEqual(payload["shareSettings"]["groupIds"], [])

    def test_existing_flight_booking_offers_arrival_but_not_customs_or_note(self) -> None:
        """Android 6.1.10 adds arrival transport; shared passengers still omit customs and note."""
        root = cli.typer.main.get_command(cli.app)
        for command in ("empty-leg", "shared-flight"):
            options = {opt for param in root.commands["booking"].commands[command].params for opt in param.opts}
            self.assertNotIn("--customs", options)
            self.assertNotIn("--note", options)
            self.assertIn("--arrival-ground-method", options)
            self.assertIn("--ground-dropoff-address", options)

    def test_booking_lock_and_release_use_android_flight_lock_body(self) -> None:
        for extra, lock in (([], True), (["--release"], False)):
            with self.subTest(lock=lock), \
                    patch.object(cli, "get_api_token", return_value="token"), \
                    patch.object(cli, "api_post", return_value={}) as post:
                result = self.runner.invoke(cli.app, ["booking", "lock", "--flight-id", "flight-1", *extra])
                self.assertEqual(result.exit_code, 0, result.output)
                post.assert_called_once_with("token", "/flight/lock", {"id": "flight-1", "lock": lock})

    # -- surveys, feedback, hours exchange, cost estimate, manifest --

    def test_booking_survey_builds_android_body_from_form_answers(self) -> None:
        with patch.object(cli, "get_api_token", return_value="token"), patch.object(cli, "api_post", return_value={}) as post:
            result = self.runner.invoke(cli.app, [
                "booking", "survey", "--leg-id", "leg-id",
                "--booking-experience", "strongly-agree",
                "--response-time", "agree",
                "--concierge-interest", "agree-private",
                "--concierge-help", "not-applicable",
                "--itinerary-on-time", "yes",
                "--comments", " Great trip ",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_called_once_with("token", "/booking-survey/create", {
            "legId": "leg-id",
            "bookingExperience": "STRONGLY_AGREE",
            "responseTime": "AGREE",
            "conciergeInterest": "AGREE_PRIVATE",
            "conciergeHelp": "NOT_APPLICABLE",
            "flightItineraryTime": "YES",
            "additionalFeedback": "Great trip",
        })
        self.assertNotIn("tripId", json.dumps(post.call_args.args[2]))

    def test_booking_survey_rejects_unknown_answers(self) -> None:
        result = self.runner.invoke(cli.app, [
            "booking", "survey", "--leg-id", "leg-id",
            "--booking-experience", "5", "--response-time", "agree",
            "--concierge-interest", "agree", "--concierge-help", "yes", "--itinerary-on-time", "yes", "--dry-run",
        ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("--booking-experience", result.output)

    def test_feedback_submit_builds_android_body_with_derived_score(self) -> None:
        with patch.object(cli, "get_api_token", return_value="token"), patch.object(cli, "api_post", return_value={}) as post:
            result = self.runner.invoke(cli.app, [
                "feedback", "submit", "--leg-id", "leg-id",
                "--snacks-and-amenities", "very-satisfied",
                "--aircraft-condition", "excellent",
                "--crew", "exceptional",
                "--fbo", "very-satisfied",
                "--catering-and-transport", "excellent",
                "--contact-me", "yes",
                "--comments", "Perfect",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_called_once_with("token", "/feedback/create", {
            "legId": "leg-id",
            "quality": "VERY_SATISFIED",
            "cleanliness": "EXCELLENT",
            "professionalism": "EXCEPTIONAL",
            "fbo": "VERY_SATISFIED",
            "catering": "EXCELLENT",
            "contact": True,
            "additionalFeedback": "Perfect",
            "score": 5.0,
        })

    def test_feedback_submit_uses_android_wire_spelling_and_averages_score(self) -> None:
        result = self.runner.invoke(cli.app, [
            "feedback", "submit", "--leg-id", "leg-id",
            "--snacks-and-amenities", "dissatisfied",
            "--aircraft-condition", "fair",
            "--crew", "satisfactory",
            "--fbo", "neutral",
            "--catering-and-transport", "very-poor",
            "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)["data"]["payload"]
        self.assertEqual(payload["quality"], "DISSASTIFIED")
        self.assertNotIn("contact", payload, "unanswered contact question must be omitted, as in the app")
        self.assertEqual(list(payload), ["legId", "quality", "cleanliness", "professionalism", "fbo", "catering", "additionalFeedback", "score"])
        self.assertEqual(payload["additionalFeedback"], "")
        # (2 + 3 + 3 + 3 + 1) / 5
        self.assertEqual(payload["score"], 2.4)

    def test_feedback_submit_rejects_answers_outside_the_app_scale(self) -> None:
        result = self.runner.invoke(cli.app, [
            "feedback", "submit", "--leg-id", "leg-id",
            "--snacks-and-amenities", "excellent",
            "--aircraft-condition", "fair", "--crew", "satisfactory", "--fbo", "neutral",
            "--catering-and-transport", "good", "--dry-run",
        ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("--snacks-and-amenities must be one of", result.output)
        self.assertIn("dissatisfied", result.output)
        self.assertNotIn("DISSASTIFIED", result.output)

    def test_hours_listing_create_builds_android_body(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_get_account_aircraft_id", return_value="account-aircraft-1"),
            patch.object(cli, "api_post", return_value={}) as post,
        ):
            result = self.runner.invoke(cli.app, ["hours", "listing-create", "--action", "sell", "--hours", "12.5"])

        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_called_once_with("token", "/hours-exchange-listing/create", {
            "accountAircraftId": "account-aircraft-1", "action": "SELL", "hours": 12.5,
        })

    def test_quote_cost_builds_android_misc_cost_estimate_request(self) -> None:
        with patch.object(cli, "get_api_token", return_value="token"), patch.object(cli, "api_post", return_value={"data": {"total": 1}}) as post:
            result = self.runner.invoke(cli.app, [
                "quote", "cost", "--aircraft", "citation-cj3-plus", "--quote-price", "12345.5",
                "--flight-minutes", "95", "--service-area", "Northeast",
                "--ground-transportation-method", "sedan-and-driver",
                "--owned-aircraft", "citation-cj3-plus", "--flown-aircraft", "legacy-450",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_called_once_with("token", "/trip/misc-cost-estimate", {"legs": [{
            "aircraft": "CITATION_CJ3_PLUS",
            "quotePrice": 12345.5,
            "serviceArea": "Northeast",
            "actualFlightMinutes": 95,
            "groundTransportation": {"method": "SEDAN_AND_DRIVER", "applyServiceCharge": True},
            "interchange": {"ownedAircraft": "CITATION_CJ3_PLUS", "flownAircraft": "LEGACY_450"},
        }]})
        self.assertEqual(json.loads(result.output)["data"], {"total": 1})

    def test_quote_cost_omits_unset_sections_like_android(self) -> None:
        """toTripMiscCostEstimateRequest: serviceArea always (may be ""); serviceLocation,
        actualFlightMinutes, groundTransportation and interchange only when known."""
        result = self.runner.invoke(cli.app, [
            "quote", "cost", "--aircraft", "legacy-450", "--quote-price", "100", "--flight-minutes", "60", "--dry-run",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        leg = json.loads(result.output)["data"]["payload"]["legs"][0]
        self.assertEqual(leg, {"aircraft": "LEGACY_450", "quotePrice": 100.0, "serviceArea": "", "actualFlightMinutes": 60})

        # actualFlightMinutes is null-gated in toTripMiscCostEstimateRequest (0x8d8064).
        result = self.runner.invoke(cli.app, [
            "quote", "cost", "--aircraft", "legacy-450", "--quote-price", "100", "--dry-run",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        leg = json.loads(result.output)["data"]["payload"]["legs"][0]
        self.assertEqual(leg, {"aircraft": "LEGACY_450", "quotePrice": 100.0, "serviceArea": ""})

        result = self.runner.invoke(cli.app, [
            "quote", "cost", "--aircraft", "legacy-450", "--quote-price", "100", "--flight-minutes", "60",
            "--service-location", "FBO North", "--owned-aircraft", "citation-cj2-plus", "--dry-run",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        leg = json.loads(result.output)["data"]["payload"]["legs"][0]
        self.assertEqual(list(leg), ["aircraft", "quotePrice", "serviceArea", "serviceLocation", "actualFlightMinutes", "interchange"])
        self.assertEqual(leg["interchange"], {"ownedAircraft": "CITATION_CJ2_PLUS", "flownAircraft": "LEGACY_450"})

        result = self.runner.invoke(cli.app, [
            "quote", "cost", "--aircraft", "legacy-450", "--quote-price", "100", "--flight-minutes", "60",
            "--flown-aircraft", "citation-cj2-plus", "--dry-run",
        ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("--owned-aircraft", result.output)

    def test_manifest_send_validates_emails_and_requires_confirm(self) -> None:
        with patch.object(cli, "get_api_token", return_value="token"), patch.object(cli, "api_post", return_value={}) as post:
            result = self.runner.invoke(cli.app, ["trips", "manifest-send", "--trip-id", "trip-1", "--to", "not-an-email"])
            self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
            self.assertIn("invalid email", result.output)

            result = self.runner.invoke(cli.app, ["trips", "manifest-send", "--trip-id", "trip-1", "--to", "a@example.com"])
            self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
            self.assertIn("--confirm", result.output)
            post.assert_not_called()

            result = self.runner.invoke(cli.app, [
                "trips", "manifest-send", "--trip-id", "trip-1", "--to", "a@example.com, b@example.com", "--confirm",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_called_once_with("token", "/trip/manifest/send", {
            "recipients": ["a@example.com", "b@example.com"], "tripId": "trip-1",
        })

    def test_leg_passenger_update_sends_full_saved_id_list_once(self) -> None:
        leg = {"data": {"passengers": [
            {
                "id": "leg-passenger-1",
                "passenger": {"id": "saved-1", "firstName": "Jane", "lastName": "Doe"},
            },
            {
                "id": "leg-passenger-2",
                "passenger": {"id": "saved-2", "firstName": "John", "lastName": "Doe"},
            },
        ]}}
        with (
            patch.object(cli, "_guard_booking_probe"),
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value=leg) as read,
            patch.object(cli, "api_patch", return_value={"status": "ok"}) as write,
        ):
            result = self.runner.invoke(cli.app, [
                "leg", "update-passengers",
                "--leg-id", "leg-id",
                "--add", "saved-3",
                "--remove", "saved-2",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        read.assert_called_once_with("token", "/my-leg/leg-id")
        write.assert_called_once_with("token", "/leg/leg-id", {
            "options": {"passengers": [{"id": "saved-1"}, {"id": "saved-3"}]},
        })
        plan = json.loads(result.output)["data"]["plan"]
        self.assertEqual([item["id"] for item in plan["kept"]], ["saved-1"])
        self.assertEqual([item["id"] for item in plan["dropped"]], ["saved-2"])

    def test_passport_create_normalizes_dates_to_milliseconds(self) -> None:
        result = self.runner.invoke(cli.app, [
            "passport", "create",
            "--file", str(self.passport_scan),
            *self.passport_flags(expiration="1893456000"),
            "--tz", "America/Toronto",
            "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)["data"]["payload"]
        self.assertEqual(payload, self.passport_body(expiration_ms=1893456000000))

    def test_passport_create_requires_photo_or_scan(self) -> None:
        with (
            patch.object(cli, "get_api_token") as auth,
            patch.object(cli, "api_post") as request,
        ):
            result = self.runner.invoke(cli.app, [
                "passport", "create",
                *self.passport_flags(),
                "--tz", "America/Toronto",
                "--confirm",
            ])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--file", result.output)
        auth.assert_not_called()
        request.assert_not_called()

    def test_passport_create_rejects_fake_image_before_authentication(self) -> None:
        fake_image = Path(self.temporary.name) / "fake.jpg"
        fake_image.write_text("not an image")
        with patch.object(cli, "get_api_token") as auth:
            result = self.runner.invoke(cli.app, [
                "passport", "create",
                "--file", str(fake_image),
                *self.passport_flags(),
                "--tz", "America/Toronto",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("contents do not match", result.output)
        auth.assert_not_called()

    def test_passport_create_uploads_required_scan_before_reordering(self) -> None:
        init = {
            "data": {
                "presignedUpload": {
                    "url": "https://storage.invalid/upload",
                    "fields": {"key": "passport/key"},
                },
                "storagePath": "passport/key",
            }
        }
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(
                cli,
                "api_get",
                return_value={"data": {"passportIds": ["passport-old"]}},
            ) as read,
            patch.object(
                cli,
                "api_post",
                side_effect=[
                    {"data": {"id": "passport-new"}},
                    init,
                    {"data": {"attached": True}},
                ],
            ) as post,
            patch.object(cli, "_post_presigned_multipart", return_value=204) as upload,
            patch.object(cli, "api_patch", return_value={"data": True}) as update,
        ):
            result = self.runner.invoke(cli.app, [
                "passport", "create",
                "--file", str(self.passport_scan),
                *self.passport_flags(),
                "--tz", "America/Toronto",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        read.assert_called_once_with("token", "/my-passenger/passenger-1")
        self.assertEqual(post.call_args_list[0].args, (
            "token", "/my-passport/create", self.passport_body(),
        ))
        self.assertEqual(post.call_args_list[1].args, (
            "token", "/my-passport/document/upload-init", {
                "passportId": "passport-new",
                "fileName": "passport.jpg",
                "contentType": "image/jpeg",
                "maxFileSizeBytes": 20 * 1024 * 1024,
            },
        ))
        upload.assert_called_once()
        self.assertEqual(post.call_args_list[2].args, (
            "token", "/my-passport/document/attach", {
                "passportId": "passport-new",
                "fileName": "passport.jpg",
                "contentType": "image/jpeg",
                "storagePath": "passport/key",
            },
        ))
        update.assert_called_once_with(
            "token",
            "/my-passenger/passenger-1",
            {"options": {"passportIds": ["passport-new", "passport-old"]}},
        )
        output = json.loads(result.output)["data"]
        self.assertEqual(output["documentUpload"]["storageUploadStatus"], 204)
        self.assertIn("required photo/scan attached", output["message"])

    def test_passport_create_requires_confirm_before_network_calls(self) -> None:
        with (
            patch.object(cli, "get_api_token") as auth,
            patch.object(cli, "api_post") as request,
        ):
            result = self.runner.invoke(cli.app, [
                "passport", "create",
                "--file", str(self.passport_scan),
                *self.passport_flags(),
                "--tz", "America/Toronto",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("--confirm required", result.output)
        auth.assert_not_called()
        request.assert_not_called()

    def test_passport_create_upload_failure_reports_uuid_and_does_not_reorder(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(
                cli,
                "api_get",
                return_value={"data": {"passportIds": ["passport-old"]}},
            ),
            patch.object(
                cli,
                "api_post",
                side_effect=[
                    {"data": {"id": "passport-new"}},
                    {"data": {}},
                ],
            ),
            patch.object(cli, "api_patch") as update,
        ):
            result = self.runner.invoke(cli.app, [
                "passport", "create",
                "--file", str(self.passport_scan),
                *self.passport_flags(),
                "--tz", "America/Toronto",
                "--confirm",
            ])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIsInstance(result.exception, RuntimeError)
        failure = json.loads(str(result.exception))
        self.assertEqual(failure["passportId"], "passport-new")
        self.assertIn("Do not rerun create", failure["message"])
        update.assert_not_called()

    def test_passport_create_reorder_failure_does_not_repeat_creation(self) -> None:
        init = {
            "data": {
                "presignedUpload": {
                    "url": "https://storage.invalid/upload",
                    "fields": {"key": "passport/key"},
                },
                "storagePath": "passport/key",
            }
        }
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(
                cli,
                "api_get",
                return_value={"data": {"passportIds": ["passport-old"]}},
            ),
            patch.object(
                cli,
                "api_post",
                side_effect=[
                    {"data": {"id": "passport-new"}},
                    init,
                    {"data": {"attached": True}},
                ],
            ) as post,
            patch.object(cli, "_post_presigned_multipart", return_value=204),
            patch.object(cli, "api_patch", side_effect=RuntimeError("patch failed")),
        ):
            result = self.runner.invoke(cli.app, [
                "passport", "create",
                "--file", str(self.passport_scan),
                *self.passport_flags(),
                "--tz", "America/Toronto",
                "--confirm",
            ])

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(post.call_count, 3)
        failure = json.loads(str(result.exception))
        self.assertEqual(failure["passportId"], "passport-new")
        self.assertTrue(failure["documentAttached"])
        self.assertIn("passport make-primary", failure["message"])

    def test_passport_list_uses_exact_embedded_fields_without_country_inference(self) -> None:
        response = {"data": {"items": [{
            "id": "passenger-1",
            "firstName": "Jane",
            "lastName": "Doe",
            "passports": [{
                "id": "passport-1",
                "passengerId": "passenger-1",
                "nationality": "CA",
                "issuingAuthority": "Ottawa",
            }],
        }]}}
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value=response) as request,
        ):
            result = self.runner.invoke(cli.app, ["passport", "list", "--limit", "25"])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/my-passenger", {
            "sort": [],
            "page": {"limit": 25, "offset": 0},
            "filter": {},
        })
        records = json.loads(result.output)["data"]
        self.assertEqual(records, [{
            "id": "passport-1",
            "passengerId": "passenger-1",
            "nationality": "CA",
            "issuingAuthority": "Ottawa",
            "passenger": {
                "id": "passenger-1",
                "firstName": "Jane",
                "lastName": "Doe",
            },
        }])
        self.assertNotIn("country", records[0])

    def test_passport_update_authority_sends_one_narrow_patch(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_patch", return_value={"data": True}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "passport", "update-authority",
                "--id", "passport-1",
                "--authority", "  QUÉBEC  ",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/my-passport/passport-1", {
            "options": {"issuingAuthority": "QUÉBEC"},
        })

    def test_passport_update_authority_requires_confirm(self) -> None:
        with patch.object(cli, "api_patch") as request:
            result = self.runner.invoke(cli.app, [
                "passport", "update-authority",
                "--id", "passport-1",
                "--authority", "OTTAWA",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("--confirm required", result.output)
        request.assert_not_called()

    def test_passport_create_matches_android_local_midnight(self) -> None:
        result = self.runner.invoke(cli.app, [
            "passport", "create",
            "--file", str(self.passport_scan),
            *self.passport_flags("1970-05-15", "2034-02-01"),
            "--tz", "America/Toronto",
            "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)["data"]["payload"]
        self.assertEqual(payload["dateOfBirth"], 11592000000)
        self.assertEqual(payload["expirationDate"], 2022382800000)

    def test_passport_create_uses_device_zone_not_fixed_hq_zone(self) -> None:
        toronto = self.runner.invoke(cli.app, [
            "passport", "create", *self.passport_flags(),
            "--file", str(self.passport_scan),
            "--tz", "America/Toronto", "--dry-run",
        ])
        edmonton = self.runner.invoke(cli.app, [
            "passport", "create", *self.passport_flags(),
            "--file", str(self.passport_scan),
            "--tz", "America/Edmonton", "--dry-run",
        ])

        self.assertEqual(toronto.exit_code, 0, toronto.output)
        self.assertEqual(edmonton.exit_code, 0, edmonton.output)
        toronto_ms = json.loads(toronto.output)["data"]["payload"]["dateOfBirth"]
        edmonton_ms = json.loads(edmonton.output)["data"]["payload"]["dateOfBirth"]
        self.assertEqual(toronto_ms, 315637200000)
        self.assertEqual(edmonton_ms, 315644400000)
        self.assertNotEqual(toronto_ms, edmonton_ms)

    def test_passport_create_requires_timezone_for_naive_dates(self) -> None:
        result = self.runner.invoke(cli.app, [
            "passport", "create",
            "--file", str(self.passport_scan),
            *self.passport_flags(),
            "--dry-run",
        ], env={"AIRSPRINT_TIMEZONE": ""})

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("--timezone is required", result.output)

    def test_passport_make_primary_reorders_without_selected_id(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value={"data": {"passportIds": ["old", "new"]}}),
            patch.object(cli, "api_patch", return_value={}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "passport", "make-primary",
                "--passenger-id", "passenger",
                "--passport-id", "new",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/my-passenger/passenger", {
            "options": {"passportIds": ["new", "old"]},
        })

    def test_customs_list_omits_rejected_sort_field(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={"data": {"items": []}}) as request,
        ):
            result = self.runner.invoke(cli.app, ["customs", "list"])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/myCanadianCustomsDeclaration", {
            "page": {"limit": 100, "offset": 0},
            "filter": {},
        })

    CUSTOMS_LEG = {"data": {
        "id": "leg-1",
        "departureDate": "2026-09-01T14:00:00Z",
        "legPassengers": [
            {"id": "leg-passenger-1", "passengerId": "s-1", "firstName": "Jane", "lastName": "Doe"},
            {"id": "leg-passenger-2", "passengerId": "s-2", "firstName": "John", "lastName": "Roe"},
            {"id": "leg-passenger-3", "passengerId": "s-3", "firstName": "Kid", "lastName": "Roe"},
        ],
    }}
    CUSTOMS_ANSWERED_NO = [
        "--has-pet", "no", "--has-alcohol-or-tobacco", "no",
        "--has-imported-goods", "no", "--has-high-value-currency", "no",
    ]

    def customs_dry_run(self, *extra: str):
        with (
            patch.object(cli, "_guard_booking_probe"),
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", side_effect=lambda _t, p: self.CUSTOMS_LEG if p.startswith("/my-leg/") else {"data": {"id": "link-9", "leg": {"id": "leg-1", "passengers": [{**r, "customsDeclarationAlreadySubmitted": False} for r in self.CUSTOMS_LEG["data"]["legPassengers"]]}}}) as read,
        ):
            result = self.runner.invoke(cli.app, [
                "customs", "create", "--leg-id", "leg-1", "--passengers", "Jane Doe",
                "--purpose", "pleasure", "--date", "2026-09-02", "--timezone", "America/Montreal",
                *extra, "--dry-run",
            ])
        return result, read

    def test_customs_create_matches_android_submit_declaration_body(self) -> None:
        result, read = self.customs_dry_run(
            "--passengers", "Jane Doe, John Roe", "--family", "yes", "--purpose", "business", "--description", "Meetings",
            "--link-id", "link-9",
            "--has-pet", "yes",
            "--has-alcohol-or-tobacco", "yes", "--alcohol-type", "wine", "--alcohol-volume", "2 x 750 ml",
            "--alcohol-value-cad", "45.50",
            "--has-imported-goods", "yes", "--imported-goods", "Two watches", "--imported-goods-currency", "usd",
            "--imported-goods-from-us", "yes", "--souvenir-items", "Maple syrup",
            "--has-high-value-currency", "yes", "--high-value-currency-description", "CAD 12,000 in cash",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(read.call_args_list[0].args, ("token", "/my-leg/leg-1"))
        data = json.loads(result.output)["data"]
        self.assertEqual(data["path"], "/canadianCustomsDeclaration/create")
        self.assertEqual(data["declarations"], 2)
        self.assertEqual(data["submissionStatus"]["linkId"], "link-9")
        self.assertIn("Local draft", data["message"])
        self.assertEqual(data["payload"], {
            "canadianCustomsDeclationLinkId": "link-9",
            "legPassengerIds": ["leg-passenger-1", "leg-passenger-2"],
            "date": "2026-09-02T04:00:00.000Z",
            "purposeOfTravel": "BUSINESS",
            "travelDescription": "Meetings",
            "hasPet": True,
            "hasAlcoholOrTobacco": True,
            "alcoholOrTobaccoType": "wine",
            "alcoholOrTobaccoVolume": "2 x 750 ml",
            "alcoholOrTobaccoValueCAD": 45.5,
            "hasImportedGoods": True,
            "importedGoodItems": "Two watches",
            "importedGoodsCurrency": "USD",
            "importedGoodsFromUS": True,
            "souvenirItems": "Maple syrup",
            "hasHighValueCurrency": True,
            "highValueCurrencyDescription": "CAD 12,000 in cash",
        })
        self.assertEqual(list(data["payload"]), [
            "canadianCustomsDeclationLinkId", "legPassengerIds", "date", "purposeOfTravel", "travelDescription",
            "hasPet", "hasAlcoholOrTobacco", "alcoholOrTobaccoType", "alcoholOrTobaccoVolume",
            "alcoholOrTobaccoValueCAD", "hasImportedGoods", "importedGoodItems", "importedGoodsCurrency",
            "importedGoodsFromUS", "souvenirItems", "hasHighValueCurrency", "highValueCurrencyDescription",
        ])
        for card_key in ("cardType", "cardName", "phoneNumber", "billingAddress", "cardNumber", "expiry", "cvc"):
            self.assertNotIn(card_key, data["payload"])

    def test_customs_create_minimal_sends_only_the_keys_the_app_always_sends(self) -> None:
        result, _ = self.customs_dry_run(*self.CUSTOMS_ANSWERED_NO)

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertEqual(data["payload"], {
            "canadianCustomsDeclationLinkId": "<link created at submission>",
            "legPassengerIds": ["leg-passenger-1"],
            "date": "2026-09-02T04:00:00.000Z",
            "purposeOfTravel": "PLEASURE",
            "travelDescription": "",
            "hasPet": False,
            "hasAlcoholOrTobacco": False,
            "hasImportedGoods": False,
            "importedGoodsFromUS": False,
            "hasHighValueCurrency": False,
        })
        self.assertEqual(data["submissionStatus"], "unverified-until-link-created")

    def test_manifest_highlights_extract_ops_fields(self) -> None:
        text = """Trip Sheet
        Aircraft Tail C-GABC
        Crew: Captain Jane Pilot
        Departure FBO: Example Aviation
        Passengers
        Martin Bouchard
        Guest Person
        """

        highlights = cli._manifest_highlights(text)

        self.assertEqual(highlights["tailNumbers"], ["C-GABC"])
        self.assertIn("Crew: Captain Jane Pilot", highlights["crewLines"])
        self.assertIn("Departure FBO: Example Aviation", highlights["fboLines"])
        self.assertIn("Martin Bouchard", highlights["passengerLines"])

    def test_manifest_conversion_prefers_anydoc(self) -> None:
        converted = cli.subprocess.CompletedProcess(
            args=["anydoc"], returncode=0, stdout=b"# Trip Sheet\nC-GABC\n", stderr=b""
        )
        with patch.object(cli.subprocess, "run", return_value=converted) as run:
            text = cli._manifest_text(b"%PDF test")

        self.assertEqual(text, "# Trip Sheet\nC-GABC")
        run.assert_called_once_with(
            ["anydoc", "-", "--format", "pdf"],
            input=b"%PDF test",
            stdout=cli.subprocess.PIPE,
            stderr=cli.subprocess.PIPE,
            check=False,
            timeout=30,
        )

    def test_manifest_conversion_falls_back_to_poppler(self) -> None:
        anydoc_failure = cli.subprocess.CompletedProcess(
            args=["anydoc"], returncode=1, stdout=b"", stderr=b"unsupported PDF"
        )
        poppler_success = cli.subprocess.CompletedProcess(
            args=["pdftotext"], returncode=0, stdout=b"Trip Sheet\nC-GABC\n", stderr=b""
        )
        with patch.object(
            cli.subprocess,
            "run",
            side_effect=[anydoc_failure, poppler_success],
        ) as run:
            text = cli._manifest_text(b"%PDF test")

        self.assertEqual(text, "Trip Sheet\nC-GABC")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["pdftotext", "-layout", "-", "-"],
        )

    def test_skill_flag_prints_live_booking_rules(self) -> None:
        result = self.runner.invoke(cli.app, ["--skill"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Non-negotiable live-booking safety", result.output)
        self.assertIn("leg update-passengers", result.output)

    def test_android_authenticate_uses_exact_body_without_auth_header(self) -> None:
        with patch.object(cli, "_http", return_value={"data": {"active": True}}) as request:
            response = cli.api_authenticate("auth-token")

        self.assertEqual(response, {"data": {"active": True}})
        self.assertEqual(request.call_args.args, (
            "POST", f"{cli.API_BASE_URL}/user/authenticate",
        ))
        self.assertEqual(
            json.loads(request.call_args.kwargs["data"]),
            {"authToken": "auth-token"},
        )
        self.assertNotIn("x-airsprint-auth-token", request.call_args.kwargs["headers"])
        self.assertTrue(request.call_args.kwargs["retry_first_ssl"])

    def test_api_post_none_omits_body_like_android_baggage_request(self) -> None:
        with patch.object(cli, "_http", return_value={}) as request:
            cli.api_post("token", "/baggage-type")

        self.assertIsNone(request.call_args.kwargs["data"])

    def test_device_token_contracts_match_android(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={}) as request,
        ):
            registered = self.runner.invoke(cli.app, [
                "device", "register-token",
                "--registration-token", " push-token ",
                "--device-type", " android ",
                "--device-name", " Pixel ",
            ])
            deleted = self.runner.invoke(cli.app, [
                "device", "delete-token",
                "--registration-token", " push-token ",
                "--confirm",
            ])

        self.assertEqual(registered.exit_code, 0, registered.output)
        self.assertEqual(deleted.exit_code, 0, deleted.output)
        self.assertEqual(request.call_args_list[0].args, (
            "token", "/account-notification-registration-token-register", {
                "registrationToken": "push-token",
                "deviceType": "android",
                "deviceName": "Pixel",
            },
        ))
        self.assertEqual(request.call_args_list[1].args, (
            "token", "/account-notification-registration-token-delete", {
                "registrationToken": "push-token",
            },
        ))

    def test_account_user_patch_matches_android_options_contract(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_patch", return_value={}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "account", "user-patch",
                "--id", "account-user-id",
                "--first-name", "Jane",
                "--saved-airports", "airport-id",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with(
            "token", "/my-account-user/account-user-id", {
                "id": "account-user-id",
                "options": {
                    "firstName": "Jane",
                    "savedAirportIds": ["airport-id"],
                },
            },
        )

    def test_required_info_merges_onto_full_passenger_list(self) -> None:
        leg = {"data": {"passengers": [
            {
                "passengerId": "saved-1",
                "firstName": "Jane",
                "lastName": "Doe",
                "passportIds": ["passport-9"],  # not in LegPassengerUpdate.toJson (0x801778); never copied
                "customsDeclarationId": "customs-1",
                "destinationAddress": {"city": "Las Vegas"},
            },
            {
                "passenger": {"id": "saved-2", "firstName": "John", "lastName": "Doe"},
                "passport": {"id": "passport-2"},
            },
        ]}}
        with (
            patch.object(cli, "_guard_booking_probe"),
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value=leg) as read,
            patch.object(cli, "api_patch", return_value={}) as write,
        ):
            result = self.runner.invoke(cli.app, [
                "leg", "update-required-info",
                "--leg-id", "leg-id",
                "--passport", "saved-1=passport-1",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        read.assert_called_once_with("token", "/my-leg/leg-id")
        write.assert_called_once_with("token", "/leg/leg-id/required-info", {
            "options": {
                "passengers": [
                    {
                        "id": "saved-1",
                        "customsDeclarationId": "customs-1",
                        "destinationAddress": {"city": "Las Vegas"},
                        "passport": {"id": "passport-1"},
                    },
                    {"id": "saved-2", "passport": {"id": "passport-2"}},
                ],
            },
        })
        plan = json.loads(result.output)["data"]["plan"]
        self.assertEqual(plan["dropped"], [])
        self.assertEqual([item["id"] for item in plan["kept"]], ["saved-2"])
        self.assertEqual([item["id"] for item in plan["updated"]], ["saved-1"])

    def test_required_info_address_uses_owner_leg_read_then_original_patch_route(self) -> None:
        address = {"street": "1 Example Avenue", "city": "New York", "state": "NY", "zip": "10001"}
        leg = {"data": {"id": "leg-id", "legPassengers": [
            {"id": "leg-pax-1", "passengerId": "saved-1", "passport": {"id": "passport-1"}},
            {"id": "leg-pax-2", "passengerId": "saved-2", "customsDeclarationId": "customs-2"},
        ]}}
        expected = {"options": {"passengers": [
            {"id": "saved-1", "passport": {"id": "passport-1"}, "destinationAddress": address},
            {"id": "saved-2", "customsDeclarationId": "customs-2", "destinationAddress": address},
        ]}}

        def request(method, url, **kwargs):
            if method == "GET" and url == f"{cli.API_BASE_URL}/leg/leg-id":
                raise RuntimeError('{"http_code": 401, "message": "Unauthorized"}')
            if method == "GET" and url == f"{cli.API_BASE_URL}/my-leg/leg-id":
                return leg
            if method == "PATCH" and url == f"{cli.API_BASE_URL}/leg/leg-id/required-info":
                self.assertEqual(json.loads(kwargs["data"]), expected)
                return {"data": {"updated": True}}
            self.fail(f"Unexpected request: {method} {url}")

        for mode in ("--dry-run", "--confirm"):
            with (
                self.subTest(mode=mode),
                patch.object(cli, "BOOKING_WRITE_GUARD", Path(self.temporary.name) / "write.json"),
                patch.object(cli, "get_api_token", return_value="token"),
                patch.object(cli, "_http", side_effect=request) as http,
            ):
                result = self.runner.invoke(cli.app, [
                    "leg", "update-required-info", "--leg-id", "leg-id",
                    "--destination-street", address["street"], "--destination-city", address["city"],
                    "--destination-state", address["state"], "--destination-zip", address["zip"], mode,
                ])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(http.call_count, 1 if mode == "--dry-run" else 2)
                self.assertEqual(http.call_args_list[0].args, ("GET", f"{cli.API_BASE_URL}/my-leg/leg-id"))
                self.assertFalse(http.call_args_list[0].kwargs["retry_first_ssl"])
                data = json.loads(result.output)["data"]
                self.assertEqual(data["plan"]["dropped"], [])
                self.assertEqual([row["id"] for row in data["plan"]["updated"]], ["saved-1", "saved-2"])
                if mode == "--dry-run":
                    self.assertEqual(data["payload"], expected)
                else:
                    self.assertFalse(http.call_args_list[1].kwargs.get("retry_first_ssl", False))

    def test_required_info_failed_owner_read_never_falls_back_or_writes(self) -> None:
        with (
            patch.object(cli, "_guard_booking_probe"),
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_http", side_effect=RuntimeError('{"http_code": 401}')) as http,
            patch.object(cli, "api_patch") as write,
        ):
            result = self.runner.invoke(cli.app, [
                "leg", "update-required-info", "--leg-id", "leg-id",
                "--passport", "saved-1=passport-1", "--confirm",
            ])
        self.assertNotEqual(result.exit_code, 0)
        http.assert_called_once()
        self.assertEqual(http.call_args.args, ("GET", f"{cli.API_BASE_URL}/my-leg/leg-id"))
        self.assertFalse(http.call_args.kwargs["retry_first_ssl"])
        write.assert_not_called()

    def test_required_info_customs_and_destination_apply_to_passengers(self) -> None:
        leg = {"data": {"passengers": [
            {"passenger": {"id": "saved-1", "firstName": "Jane", "lastName": "Doe"}},
            {"passenger": {"id": "saved-2", "firstName": "John", "lastName": "Doe"}},
        ]}}
        with (
            patch.object(cli, "_guard_booking_probe"),
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value=leg),
            patch.object(cli, "api_patch", return_value={}) as write,
        ):
            result = self.runner.invoke(cli.app, [
                "leg", "update-required-info", "--leg-id", "leg-id",
                "--customs", "saved-2=customs-2",
                "--destination-street", "1 Main St", "--destination-street2", "Apt 5",
                "--destination-city", "New York", "--destination-state", "NY", "--destination-zip", "10001",
                "--seats", "2", "--dog-form-submitted", "yes",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        address = {"street": "1 Main St", "street2": "Apt 5", "city": "New York", "state": "NY", "zip": "10001"}
        write.assert_called_once_with("token", "/leg/leg-id/required-info", {"options": {
            "numberOfSeats": 2,
            "passengers": [
                {"id": "saved-1", "destinationAddress": address},
                {"id": "saved-2", "customsDeclarationId": "customs-2", "destinationAddress": address},
            ],
            "dogFormSubmitted": True,
        }})
        options = write.call_args.args[2]["options"]
        self.assertEqual(list(options), ["numberOfSeats", "passengers", "dogFormSubmitted"])
        self.assertEqual(list(options["passengers"][1]["destinationAddress"]), ["street", "street2", "city", "state", "zip"])

    def test_required_info_without_passenger_changes_skips_the_pre_read(self) -> None:
        with (
            patch.object(cli, "_guard_booking_probe") as guard,
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get") as read,
            patch.object(cli, "api_patch", return_value={}) as write,
        ):
            result = self.runner.invoke(cli.app, [
                "leg", "update-required-info", "--leg-id", "leg-id",
                "--pets", "pet-1", "--baggage", "Suitcase=2",
                "--catering", "yes", "--catering-request", "Tea",
                "--ground-transportation", "yes", "--ground-transportation-when", "departure",
                "--ground-transportation-method", "taxi",
                "--ground-dropoff-address", "2 King St; Toronto; ON; M5H 1A1",
                "--note", "Late arrival",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        read.assert_not_called()
        guard.assert_not_called()
        write.assert_called_once_with("token", "/leg/leg-id/required-info", {"options": {
            "petIds": ["pet-1"],
            "baggage": [{"name": "Suitcase", "quantity": 2}],
            "requestSettings": {
                "cateringRequired": True,
                "cateringRequest": "Tea",
                "groundTransportationRequired": True,
                "groundTransportationType": "DEPARTURE",
                "groundTransportationMethod": "TAXI",
                "groundTransportationDropOffAddress": {"street": "2 King St", "city": "Toronto", "state": "ON", "zip": "M5H 1A1"},
                "note": "Late arrival",
            },
        }})
        self.assertIsNone(json.loads(result.output)["data"]["plan"])

    def test_required_info_refuses_an_empty_form_and_unknown_passengers(self) -> None:
        with patch.object(cli, "api_patch") as write, patch.object(cli, "api_get") as read:
            result = self.runner.invoke(cli.app, ["leg", "update-required-info", "--leg-id", "leg-id", "--confirm"])
            self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
            self.assertIn("Nothing to update", result.output)
            read.assert_not_called()

        leg = {"data": {"passengers": [{"passenger": {"id": "saved-1", "firstName": "Jane", "lastName": "Doe"}}]}}
        with (
            patch.object(cli, "_guard_booking_probe"),
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value=leg),
            patch.object(cli, "api_patch") as write,
        ):
            result = self.runner.invoke(cli.app, [
                "leg", "update-required-info", "--leg-id", "leg-id", "--passport", "saved-9=passport-9", "--confirm",
            ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("saved-9", result.output)
        write.assert_not_called()

    def test_required_info_dry_run_sends_nothing(self) -> None:
        with patch.object(cli, "api_patch") as write:
            result = self.runner.invoke(cli.app, [
                "leg", "update-required-info", "--leg-id", "leg-id", "--baggage", "none", "--dry-run",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        write.assert_not_called()
        data = json.loads(result.output)["data"]
        self.assertEqual(data["payload"], {"options": {"baggage": []}})
        self.assertEqual(data["path"], "/leg/leg-id/required-info")

    def test_android_detail_commands_use_verified_get_routes(self) -> None:
        cases = [
            (["user", "get", "--id", "user-id"], "/user/user-id"),
            (["files", "get", "--id", "file-id"], "/my-file/file-id"),
            (["files", "public-get", "--id", "file-id"], "/file-public/file-id"),
            (["quote", "aircraft-get", "--id", "aircraft-id"], "/aircraft/aircraft-id"),
            (["content", "get", "--id", "content-id"], "/content/content-id"),
            (["content", "faq-get", "--id", "faq-id"], "/faq/faq-id"),
            (["content", "policy-get", "--id", "policy-id"], "/policy/policy-id"),
        ]
        for arguments, path in cases:
            with self.subTest(arguments=arguments):
                with (
                    patch.object(cli, "get_api_token", return_value="token"),
                    patch.object(cli, "api_get", return_value={}) as request,
                ):
                    result = self.runner.invoke(cli.app, arguments)
                    self.assertEqual(result.exit_code, 0, result.output)
                    request.assert_called_once_with("token", path)

    def test_booked_android_detail_commands_guard_and_read_once(self) -> None:
        cases = [
            (["trips", "flight-get", "--id", "flight-id"], "/my-flight/flight-id"),
            (["trips", "leg-get", "--id", "leg-id"], "/my-leg/leg-id"),
        ]
        for arguments, path in cases:
            with self.subTest(arguments=arguments):
                with (
                    patch.object(cli, "_guard_booking_probe") as guard,
                    patch.object(cli, "get_api_token", return_value="token"),
                    patch.object(cli, "api_get", return_value={}) as request,
                ):
                    result = self.runner.invoke(cli.app, arguments)
                    self.assertEqual(result.exit_code, 0, result.output)
                    guard.assert_called_once_with(False)
                    request.assert_called_once_with("token", path)

    def test_booked_android_detail_gets_never_retry(self) -> None:
        for path in ("/my-flight/flight-id", "/my-leg/leg-id"):
            with self.subTest(path=path), patch.object(cli, "_http", return_value={}) as request:
                cli.api_get("token", path)
                self.assertFalse(request.call_args.kwargs["retry_first_ssl"])

    def test_baggage_types_uses_bodyless_source_request(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={"data": {"items": []}}) as request,
        ):
            result = self.runner.invoke(cli.app, ["booking", "baggage-types"])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/baggage-type")

    def test_bodyless_android_collections_do_not_send_empty_json(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={"data": {"items": []}}) as request,
        ):
            accounts = self.runner.invoke(cli.app, ["user", "accounts"])
            pets = self.runner.invoke(cli.app, ["pet", "list", "--limit", "10"])

        self.assertEqual(accounts.exit_code, 0, accounts.output)
        self.assertEqual(pets.exit_code, 0, pets.output)
        self.assertEqual(request.call_args_list[0].args, ("token", "/my-accounts"))
        self.assertEqual(request.call_args_list[1].args, ("token", "/my-pet"))

    def test_airport_search_uses_android_name_filter(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={"data": {"items": []}}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "quote", "airports", "--query", "Toronto", "--no-cache",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/airport", {
            "sort": [],
            "page": {"limit": 20, "offset": 0},
            "filter": {"name": "Toronto"},
        })

    def test_pet_update_adds_android_options_envelope(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_patch", return_value={}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "pet", "update", "--id", "pet-id", "--name", " Milo ", "--weight", "medium",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/my-pet/pet-id", {
            "options": {"name": "Milo", "weight": "MEDIUM"},
        })

    def test_cancel_uses_one_android_leg_request(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "booking", "cancel",
                "--leg-id", "leg-id",
                "--reason", " Plans changed ",
                "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/cancel-own", {
            "legId": "leg-id", "reason": "Plans changed",
        })

    def test_booking_survey_uses_leg_id_not_trip_id(self) -> None:
        root = cli.typer.main.get_command(cli.app)
        options = {opt for param in root.commands["booking"].commands["survey"].params for opt in param.opts}
        self.assertIn("--leg-id", options)
        self.assertNotIn("--trip-id", options)
        self.assertNotIn("flight-feedback", root.commands["trips"].commands)

    def test_simple_quote_includes_android_pax_field(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_parse_local_dt", return_value="2026-09-01T14:00:00Z"),
            patch.object(cli, "_resolve_airport", side_effect=["dep-id", "arr-id"]),
            patch.object(cli, "_get_default_aircraft", return_value="aircraft-id"),
            patch.object(cli, "api_post", return_value={}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "quote", "flight",
                "--from", "CYYZ", "--to", "KLAS",
                "--date", "2026-09-01T10:00", "--pax", "3",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/flight-quote", {"legs": [{
            "aircraftId": "aircraft-id",
            "departureAirportId": "dep-id",
            "arrivalAirportId": "arr-id",
            "departureDateUTC": "2026-09-01T14:00:00Z",
            "pax": 3,
        }]})

    def test_avatar_reads_json_url_before_optional_download(self) -> None:
        response = {"data": {"url": "https://example.invalid/avatar.jpg"}}
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value=response) as request,
            patch.object(cli, "_download_bytes") as download,
        ):
            result = self.runner.invoke(cli.app, ["user", "avatar", "--id", "user-id"])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/my-user/avatar/user-id")
        download.assert_not_called()
        self.assertEqual(
            json.loads(result.output)["data"]["url"],
            "https://example.invalid/avatar.jpg",
        )

    def test_2fa_setup_and_disable_send_android_empty_json(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={}) as request,
        ):
            setup = self.runner.invoke(cli.app, ["auth", "2fa-setup"])
            disabled = self.runner.invoke(
                cli.app,
                ["auth", "2fa-disable", "--confirm"],
            )

        self.assertEqual(setup.exit_code, 0, setup.output)
        self.assertEqual(disabled.exit_code, 0, disabled.output)
        self.assertEqual(
            request.call_args_list[0].args,
            ("token", "/user/2fa/setup", {}),
        )
        self.assertEqual(
            request.call_args_list[1].args,
            ("token", "/user/2fa/disable", {}),
        )

    def test_passport_document_upload_runs_android_three_step_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "passport.pdf"
            document.write_bytes(b"%PDF-test")
            init = {
                "data": {
                    "presignedUpload": {
                        "url": "https://storage.invalid/upload",
                        "fields": {"key": "passport/key"},
                    },
                    "storagePath": "passport/key",
                }
            }
            attached = {"data": {"attached": True}}
            with (
                patch.object(cli, "get_api_token", return_value="token"),
                patch.object(cli, "api_post", side_effect=[init, attached]) as request,
                patch.object(cli, "_post_presigned_multipart", return_value=204) as upload,
            ):
                result = self.runner.invoke(cli.app, [
                    "passport", "upload-document",
                    "--id", "passport-id",
                    "--file", str(document),
                    "--confirm",
                ])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(request.call_args_list[0].args, (
            "token", "/my-passport/document/upload-init", {
                "passportId": "passport-id",
                "fileName": "passport.pdf",
                "contentType": "application/pdf",
                "maxFileSizeBytes": 20 * 1024 * 1024,
            },
        ))
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0], "https://storage.invalid/upload")
        self.assertEqual(upload.call_args.args[1], {"key": "passport/key"})
        self.assertEqual(request.call_args_list[1].args, (
            "token", "/my-passport/document/attach", {
                "passportId": "passport-id",
                "fileName": "passport.pdf",
                "contentType": "application/pdf",
                "storagePath": "passport/key",
            },
        ))

    def test_pet_document_upload_uses_android_document_type_and_mime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "receipt.png"
            document.write_bytes(b"PNG-test")
            init = {
                "data": {
                    "presignedUpload": {
                        "url": "https://storage.invalid/upload",
                        "fields": {"key": "pet/key"},
                    },
                    "storagePath": "pet/key",
                }
            }
            with (
                patch.object(cli, "get_api_token", return_value="token"),
                patch.object(cli, "api_post", side_effect=[init, {}]) as request,
                patch.object(cli, "_post_presigned_multipart", return_value=201),
            ):
                result = self.runner.invoke(cli.app, [
                    "pet", "upload-document",
                    "--id", "pet-id",
                    "--file", str(document),
                    "--document-type", "importFormReceipt",
                    "--confirm",
                ])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(request.call_args_list[0].args[2], {
            "petId": "pet-id",
            "documentType": "importFormReceipt",
            "fileName": "receipt.png",
            "contentType": "image/png",
            "maxFileSizeBytes": 20 * 1024 * 1024,
        })
        self.assertEqual(request.call_args_list[1].args[2], {
            "petId": "pet-id",
            "documentType": "importFormReceipt",
            "fileName": "receipt.png",
            "contentType": "image/png",
            "storagePath": "pet/key",
        })

    def test_files_resolve_uses_android_path_filter_first(self) -> None:
        response = {"data": {"items": [{"url": "https://files.invalid/item"}]}}
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value=response) as request,
            patch.object(cli, "api_get") as user_read,
        ):
            result = self.runner.invoke(cli.app, [
                "files", "resolve",
                "--application-url", "https://app.invalid/documents/passport.pdf",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/my-file", {
            "filter": {"path": "documents/passport.pdf"},
            "page": {"limit": 1, "offset": 0},
        })
        user_read.assert_not_called()

    def test_files_resolve_uses_bounded_android_avatar_fallback(self) -> None:
        responses = [
            {"data": {"items": []}},
            {"data": {"items": []}},
            {"data": {"items": [{"applicationUrl": "https://files.invalid/default"}]}},
        ]
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", side_effect=responses) as request,
            patch.object(cli, "api_get", return_value={"data": {"id": "user-id"}}) as user_read,
        ):
            result = self.runner.invoke(cli.app, [
                "files", "resolve", "--application-url", "/missing/path",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        user_read.assert_called_once_with("token", "/me")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[1].args[2]["filter"]["role"], "user_picture")
        self.assertEqual(request.call_args_list[2].args[2]["filter"]["role"], "default_file")

    def test_passport_create_skips_reorder_when_passenger_had_no_passport(self) -> None:
        init = {"data": {
            "presignedUpload": {"url": "https://storage.invalid/upload", "fields": {}},
            "storagePath": "passport/key",
        }}
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value={"data": {"passportIds": []}}),
            patch.object(cli, "api_post", side_effect=[
                {"data": {"id": "passport-new"}}, init, {"data": {"attached": True}},
            ]),
            patch.object(cli, "_post_presigned_multipart", return_value=204),
            patch.object(cli, "api_patch") as update,
        ):
            result = self.runner.invoke(cli.app, [
                "passport", "create", "--file", str(self.passport_scan),
                *self.passport_flags(), "--tz", "America/Toronto", "--confirm",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        update.assert_not_called()
        self.assertEqual(json.loads(result.output)["data"]["passportIds"], ["passport-new"])

    def test_passport_create_validates_form_fields_before_any_network_call(self) -> None:
        with patch.object(cli, "get_api_token") as auth:
            result = self.runner.invoke(cli.app, [
                "passport", "create", "--file", str(self.passport_scan),
                *self.passport_flags(), "--nationality", "Canada",
                "--tz", "America/Toronto", "--dry-run",
            ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("two-letter country code", result.output)
        auth.assert_not_called()

    def test_pet_create_builds_android_body_in_source_order(self) -> None:
        result = self.runner.invoke(cli.app, [
            "pet", "create", "--name", "Milo", "--species", "dog", "--gender", "male",
            "--weight", "small", "--save-profile", "yes", "--dry-run",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertEqual(data["endpoint"], "/my-pet/create")
        self.assertEqual(list(data["payload"].items()), [
            ("name", "Milo"), ("species", "DOG"), ("gender", "MALE"), ("weight", "SMALL"),
            ("vaccineDocuments", []), ("importFormReceipt", ""), ("isActive", True),
            ("picture", ""), ("note", ""),
        ])

        bad = self.runner.invoke(cli.app, [
            "pet", "create", "--name", "Milo", "--species", "hamster", "--gender", "male",
            "--weight", "small", "--save-profile", "yes", "--dry-run",
        ])
        self.assertEqual(bad.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("DOG, CAT, RABBIT, OTHER", bad.output)

        missing = self.runner.invoke(cli.app, [
            "pet", "create", "--name", "Milo", "--species", "dog", "--gender", "male",
            "--weight", "small", "--dry-run",
        ])
        self.assertEqual(missing.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("--save-profile", missing.output)

    def test_pet_update_requires_a_field_and_maps_save_profile(self) -> None:
        empty = self.runner.invoke(cli.app, ["pet", "update", "--id", "pet-id", "--dry-run"])
        self.assertEqual(empty.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("at least one field", empty.output)

        result = self.runner.invoke(cli.app, [
            "pet", "update", "--id", "pet-id", "--save-profile", "no", "--dry-run",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["data"]["payload"], {"options": {"isActive": False}})

    def test_address_create_builds_android_body_with_optional_line_two(self) -> None:
        result = self.runner.invoke(cli.app, [
            "address", "create", "--passenger-id", "pax-1", "--street", "1 Main St",
            "--city", "Québec", "--state", "QC", "--zip", "G1A 1A1", "--country", "Canada",
            "--dry-run",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(list(json.loads(result.output)["data"]["payload"].items()), [
            ("passengerId", "pax-1"), ("label", "Profile Address"), ("addressLine1", "1 Main St"),
            ("country", "Canada"), ("city", "Québec"), ("stateOrProvince", "QC"),
            ("zipOrPostal", "G1A 1A1"),
        ])

        with_suite = self.runner.invoke(cli.app, [
            "address", "create", "--passenger-id", "pax-1", "--street", "1 Main St",
            "--street-2", " Suite 4 ", "--city", "Québec", "--state", "QC", "--zip", "G1A 1A1",
            "--country", "Canada", "--dry-run",
        ])
        payload = json.loads(with_suite.output)["data"]["payload"]
        self.assertEqual(list(payload)[:4], ["passengerId", "label", "addressLine1", "addressLine2"])
        self.assertEqual(payload["addressLine2"], "Suite 4")

    def test_account_invite_resolves_role_name_and_account_like_android(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_get_account_ids", return_value=["account-1"]),
            patch.object(cli, "api_post", return_value={"data": {"items": [{"id": "role-1"}]}}) as post,
        ):
            result = self.runner.invoke(cli.app, [
                "account", "invite", "--first-name", "Jane", "--last-name", "Doe",
                "--email", "jane@x.com", "--role", "full-access", "--dry-run",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_called_once_with("token", "/account-user-role", {
            "filter": {"name": "FULL_ACCESS"}, "page": {"limit": 2, "offset": 0},
        })
        self.assertEqual(json.loads(result.output)["data"]["payload"], {"newUser": {
            "firstName": "Jane", "lastName": "Doe", "email": "jane@x.com",
            "accountUserRoleId": "role-1", "accountId": "account-1",
        }})

        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_get_account_ids", return_value=["account-1", "account-2"]),
            patch.object(cli, "api_post") as post,
        ):
            passenger = self.runner.invoke(cli.app, [
                "account", "invite", "--first-name", "Jane", "--last-name", "Doe",
                "--email", "jane@x.com", "--role-id", "role-9", "--account-id", "account-2",
                "--passenger-id", "pax-1", "--dry-run",
            ])
            several = self.runner.invoke(cli.app, [
                "account", "invite", "--first-name", "Jane", "--last-name", "Doe",
                "--email", "jane@x.com", "--role-id", "role-9", "--dry-run",
            ])

        post.assert_not_called()
        self.assertEqual(list(json.loads(passenger.output)["data"]["payload"]), ["passengerId", "newUser"])
        self.assertEqual(several.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("--account-id", several.output)

        both = self.runner.invoke(cli.app, [
            "account", "invite", "--first-name", "J", "--last-name", "D", "--email", "j@x.com",
            "--role", "FULL_ACCESS", "--role-id", "r", "--dry-run",
        ])
        self.assertEqual(both.exit_code, cli.EXIT_VALIDATION)
        bad_role = self.runner.invoke(cli.app, [
            "account", "invite", "--first-name", "J", "--last-name", "D", "--email", "j@x.com",
            "--role", "ADMIN", "--dry-run",
        ])
        self.assertEqual(bad_role.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("ACCOUNT_OWNER, FULL_ACCESS", bad_role.output)

    def test_user_update_sends_only_changed_fields_in_options(self) -> None:
        empty = self.runner.invoke(cli.app, ["user", "update", "--dry-run"])
        self.assertEqual(empty.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("at least one field", empty.output)

        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_patch", return_value={}) as patch_call,
        ):
            result = self.runner.invoke(cli.app, [
                "user", "update", "--phone", "5145551234", "--nationality", "ca",
                "--preferred-contact-method", "sms",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        patch_call.assert_called_once_with("token", "/my-user", {"options": {
            "phone": "5145551234", "nationality": "CA", "preferredContactMethod": "SMS",
        }})

    def test_password_and_two_factor_commands_build_android_bodies(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_post", return_value={}) as post,
        ):
            change = self.runner.invoke(cli.app, [
                "user", "change-password", "--current-password", "old-secret",
                "--new-password", "new-secret-1", "--confirm",
            ])
            verify = self.runner.invoke(cli.app, ["auth", "2fa-verify", "--code", "123 456"])
        self.assertEqual(change.exit_code, 0, change.output)
        self.assertEqual(verify.exit_code, 0, verify.output)
        self.assertEqual(post.call_args_list[0].args, ("token", "/my-user/change-password", {
            "currentPassword": "old-secret", "newPassword": "new-secret-1",
        }))
        self.assertEqual(post.call_args_list[1].args, ("token", "/user/2fa/verify", {"token": "123456"}))

        with (
            patch.object(cli, "_http", return_value={"data": {"authToken": "2fa-session"}}) as http,
            patch.object(cli, "_save_api_token") as save,
        ):
            sign_in = self.runner.invoke(cli.app, [
                "auth", "2fa-sign-in", "--user-id", "user-1", "--code", "654321", "--username", "jane@example.com",
            ])
            reset = self.runner.invoke(cli.app, [
                "auth", "reset-confirm", "--token", "reset-token", "--new-password", "new-secret-1",
            ])
        self.assertEqual(sign_in.exit_code, 0, sign_in.output)
        save.assert_called_once_with("2fa-session", "jane@example.com")
        self.assertEqual(reset.exit_code, 0, reset.output)
        self.assertTrue(http.call_args_list[0].args[1].endswith("/user/2fa/sign-in"))
        self.assertEqual(json.loads(http.call_args_list[0].kwargs["data"]), {"userId": "user-1", "token": "654321"})
        self.assertTrue(http.call_args_list[1].args[1].endswith("/user/reset-password"))
        self.assertEqual(json.loads(http.call_args_list[1].kwargs["data"]), {
            "token": "reset-token", "newPassword": "new-secret-1",
        })

        with patch.object(cli, "get_api_token") as auth:
            unconfirmed = self.runner.invoke(cli.app, [
                "user", "change-password", "--current-password", "old-secret", "--new-password", "new-secret-1",
            ])
            same = self.runner.invoke(cli.app, [
                "user", "change-password", "--current-password", "same-secret", "--new-password", "same-secret", "--confirm",
            ])
            bad_code = self.runner.invoke(cli.app, ["auth", "2fa-verify", "--code", "12ab"])
        auth.assert_not_called()
        self.assertEqual(unconfirmed.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("--confirm required", unconfirmed.output)
        self.assertEqual(same.exit_code, cli.EXIT_VALIDATION)
        self.assertEqual(bad_code.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("6-digit", bad_code.output)

    def test_android_contract_matrix_has_105_registered_command_mappings(self) -> None:
        contract = (Path(__file__).resolve().parents[1] / "ANDROID_CONTRACT.md").read_text()
        expected_counts = {"GET": 21, "PATCH": 11, "DELETE": 8, "POST": 65}
        root = cli.typer.main.get_command(cli.app)
        active_method = None
        counts = {method: 0 for method in expected_counts}
        for line in contract.splitlines():
            heading = re.fullmatch(r"### (GET|PATCH|DELETE|POST) \(\d+\)", line)
            if heading:
                active_method = heading.group(1)
                continue
            if line.startswith("## "):
                active_method = None
            if not active_method or not line.startswith("| `/"):
                continue
            counts[active_method] += 1
            columns = line.split("|")
            for command_path in re.findall(r"`([^`]+)`", columns[2]):
                parts = command_path.split()
                self.assertEqual(len(parts), 2, command_path)
                group = root.commands.get(parts[0])
                self.assertIsNotNone(group, command_path)
                self.assertIn(parts[1], group.commands, command_path)

        self.assertEqual(counts, expected_counts)
        self.assertEqual(sum(counts.values()), 105)


    def test_passenger_create_save_profile_yes_builds_android_payload(self) -> None:
        result = self.runner.invoke(cli.app, [
            "passenger", "create", "--first-name", " Jean ", "--last-name", "Tremblay",
            "--gender", "male", "--save-profile", "yes", "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertEqual(data["endpoint"], "/my-passenger/create")
        self.assertIs(data["saveProfile"], True)
        self.assertEqual(data["payload"], {
            "firstName": "Jean", "middleName": "", "lastName": "Tremblay",
            "email": None, "gender": "MALE", "age": "ADULT",
            "flightPreferences": "", "picture": "",
            "addresses": [], "isActive": True,
        })
        # NewPassengersRequestModel.toJson (0x6f4a90) key order; `passports` is
        # gated on a non-empty list and the form always passes an empty one.
        self.assertEqual(list(data["payload"]), [
            "firstName", "middleName", "lastName", "email", "gender", "age",
            "flightPreferences", "picture", "addresses", "isActive",
        ])

    def test_passenger_create_save_profile_no_hides_person(self) -> None:
        result = self.runner.invoke(cli.app, [
            "passenger", "create", "--first-name", "Jean", "--last-name", "Tremblay",
            "--gender", "FEMALE", "--category", "child", "--email", "j@x.com",
            "--save-profile", "no", "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertIs(data["saveProfile"], False)
        self.assertIs(data["payload"]["isActive"], False)
        self.assertEqual(data["payload"]["age"], "CHILD")
        self.assertEqual(data["payload"]["email"], "j@x.com")

    def test_passenger_create_requires_save_profile(self) -> None:
        result = self.runner.invoke(cli.app, [
            "passenger", "create", "--first-name", "Jean", "--last-name", "Tremblay",
            "--gender", "MALE", "--dry-run",
        ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("--save-profile yes|no is required", result.output)

    def test_passenger_create_rejects_bad_values(self) -> None:
        base = ["passenger", "create", "--first-name", "Jean", "--last-name", "Tremblay", "--dry-run"]
        cases = {
            "save": base + ["--gender", "MALE", "--save-profile", "maybe"],
            "gender": base + ["--gender", "OTHER", "--save-profile", "yes"],
            "category": base + ["--gender", "MALE", "--category", "SENIOR", "--save-profile", "yes"],
            "blank": ["passenger", "create", "--first-name", " ", "--last-name", "T", "--gender", "MALE", "--save-profile", "yes", "--dry-run"],
        }
        for name, args in cases.items():
            with self.subTest(case=name):
                result = self.runner.invoke(cli.app, args)
                self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)

    @staticmethod
    def _passenger_record() -> dict:
        return {"data": {
            "object": "passenger", "id": "pax-1", "firstName": "Jean", "middleName": None,
            "lastName": "Tremblay", "email": "old@x.com", "gender": "MALE", "age": "ADULT",
            "flightPreferences": "aisle", "picture": "", "isActive": True, "passportIds": ["pp-1"],
        }}

    def test_passenger_update_sends_the_whole_profile_form_like_the_app(self) -> None:
        """PassengerPersonProfilePage._onSave (0x881fcc) always PATCHes the seven profile
        keys in order, wrapped in {"options"}; never isActive, picture (no upload) or passportIds."""
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value=self._passenger_record()) as read,
            patch.object(cli, "api_patch", return_value={"data": {"id": "pax-1"}}) as write,
        ):
            result = self.runner.invoke(cli.app, [
                "passenger", "update", "--id", "pax-1", "--email", " new@x.com ", "--category", "infant",
                "--flight-preferences", "window seat",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        read.assert_called_once_with("token", "/my-passenger/pax-1")
        write.assert_called_once_with("token", "/my-passenger/pax-1", {"options": {
            "firstName": "Jean", "middleName": "", "lastName": "Tremblay", "email": "new@x.com",
            "gender": "MALE", "age": "INFANT", "flightPreferences": "window seat",
        }})
        self.assertEqual(
            list(write.call_args.args[2]["options"]),
            ["firstName", "middleName", "lastName", "email", "gender", "age", "flightPreferences"],
        )

    def test_passenger_update_dry_run_reads_the_profile_but_writes_nothing(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value=self._passenger_record()),
            patch.object(cli, "api_patch") as write,
        ):
            result = self.runner.invoke(cli.app, [
                "passenger", "update", "--id", "pax-1", "--email", "", "--gender", "x", "--dry-run",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        write.assert_not_called()
        data = json.loads(result.output)["data"]
        self.assertEqual(data["path"], "/my-passenger/pax-1")
        self.assertEqual(data["payload"], {"options": {
            "firstName": "Jean", "middleName": "", "lastName": "Tremblay", "email": None,
            "gender": "NONE", "age": "ADULT", "flightPreferences": "aisle",
        }})
        self.assertEqual(data["changed"], ["email", "gender"])

    def test_passenger_forms_offer_only_what_the_app_sends(self) -> None:
        root = cli.typer.main.get_command(cli.app)
        passenger = root.commands["passenger"].commands
        create_options = {opt for param in passenger["create"].params for opt in param.opts}
        update_options = {opt for param in passenger["update"].params for opt in param.opts}
        self.assertNotIn("--flight-preferences", create_options)  # toJson hard-codes "" (0x6f4ba4)
        self.assertNotIn("--save-profile", update_options)  # _onSave never sends isActive
        self.assertIn("--flight-preferences", update_options)

        result = self.runner.invoke(cli.app, ["passenger", "update", "--id", "pax-1", "--dry-run"])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)

        record = self._passenger_record()
        record["data"]["gender"] = None
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value=record),
            patch.object(cli, "api_patch") as write,
        ):
            result = self.runner.invoke(cli.app, ["passenger", "update", "--id", "pax-1", "--email", "a@b.c"])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("--gender", result.output)
        write.assert_not_called()

    def test_notification_toggle_names_are_validated(self) -> None:
        result = self.runner.invoke(cli.app, ["messages", "settings-update", "--on", "weeklyDigest,bogus", "--dry-run"])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("unknown notification toggle 'bogus'", result.output)

        result = self.runner.invoke(cli.app, ["messages", "settings-update", "--on", "weeklyDigest", "--off", "weeklyDigest", "--dry-run"])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)

        result = self.runner.invoke(cli.app, [
            "messages", "settings-update", "--on", "weeklyDigest, bookingConfirmed", "--off", "airsprintPromotions", "--dry-run",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["data"]["payload"], {
            "options": {"weeklyDigest": True, "bookingConfirmed": True, "airsprintPromotions": False},
        })

    def test_messages_update_builds_android_read_body(self) -> None:
        result = self.runner.invoke(cli.app, ["messages", "update", "--ids", "n1, n2,n1", "--read", "no", "--dry-run"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["data"]["payload"], {"ids": ["n1", "n2"], "isRead": False})

    def test_quote_flight_builds_android_legs_with_return(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_resolve_airport", side_effect=lambda _t, icao: f"id-{icao.upper()}"),
            patch.object(cli, "_get_default_aircraft", return_value="ac-default"),
            patch.object(cli, "api_post", return_value={"data": {"quote": 1}}) as request,
        ):
            result = self.runner.invoke(cli.app, [
                "quote", "flight", "--from", "cyqb", "--to", "KTEB", "--date", "2026-04-15T10:00",
                "--return-date", "2026-04-18T16:30", "--pax", "3", "--tz", "America/Montreal",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        request.assert_called_once_with("token", "/flight-quote", {"legs": [
            {"aircraftId": "ac-default", "departureAirportId": "id-CYQB", "arrivalAirportId": "id-KTEB",
             "departureDateUTC": "2026-04-15T14:00:00Z", "pax": 3},
            {"aircraftId": "ac-default", "departureAirportId": "id-KTEB", "arrivalAirportId": "id-CYQB",
             "departureDateUTC": "2026-04-18T20:30:00Z", "pax": 3},
        ]})

    def test_output_presentation_hides_backend_mechanics_but_keeps_ids(self) -> None:
        record = {
            "status": "success", "httpStatusCode": 200, "httpStatusReason": "OK",
            "data": {"items": [{
                "id": "pax-1", "object": "passenger", "firstName": "A", "age": "CHILD",
                "isActive": False, "accountUserIds": ["au-1"], "legPassengerIds": ["lp-1"],
                "passportIds": ["pp-1"], "dateOfLastFlight": 1793554200, "createdAt": 1767129344,
                "passports": [{
                    "id": "pp-1", "object": "passport", "fl3xxDocumentId": "x",
                    "image": "https://bucket/presigned", "dateOfBirthTimestamp": 94107600,
                    "expirationDateTimestamp": 1966564800, "passportNumber": "AR1",
                }],
                "duration": 239, "flightTime": "227", "time": 1788102960,
            }]},
        }
        with patch.dict(cli.os.environ, {"AIRSPRINT_TIMEZONE": "America/Montreal"}):
            shown = cli._present(record)
        self.assertEqual(shown, {"items": [{
            "id": "pax-1", "firstName": "A", "category": "CHILD", "savedProfile": False,
            "passportIds": ["pp-1"], "dateOfLastFlight": "2026-11-01T12:30:00-05:00",
            "createdAt": "2025-12-30T16:15:44-05:00",
            "passports": [{
                "id": "pp-1", "scanAttached": True, "dateOfBirth": "1972-12-25",
                "expirationDate": "2032-04-26", "passportNumber": "AR1",
            }],
            "duration": 239, "flightTime": "227", "time": "2026-08-30T11:16:00-04:00",
        }]})
        self.assertEqual(cli._present({"dry_run": True, "payload": {"isActive": True}}), {"dry_run": True, "payload": {"isActive": True}})

    def test_dry_run_previews_are_printed_verbatim_and_internal_flag_disables_presentation(self) -> None:
        result = self.runner.invoke(cli.app, [
            "passenger", "create", "--first-name", "A", "--last-name", "B", "--gender", "NONE",
            "--save-profile", "no", "--dry-run",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"isActive": false', result.output)

        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value={
                "status": "success", "httpStatusCode": 200, "httpStatusReason": "OK",
                "data": {"object": "passenger", "id": "p", "isActive": True},
            }),
        ):
            shown = self.runner.invoke(cli.app, ["passenger", "get", "--id", "p"])
            verbatim = self.runner.invoke(cli.app, ["--internal", "passenger", "get", "--id", "p"])
        self.assertEqual(json.loads(shown.output)["data"], {"id": "p", "savedProfile": True})
        self.assertEqual(json.loads(verbatim.output)["data"]["data"], {"object": "passenger", "id": "p", "isActive": True})


    def test_all_cli_api_routes_match_the_android_source_inventory(self) -> None:
        snapshot = json.loads(Path(__file__).with_name("android_contracts.json").read_text())
        expected = {(item["method"], item["path"]) for item in snapshot["contracts"]}
        self.assertEqual((snapshot["androidVersion"], snapshot["versionCode"]),
                         (cli.ANDROID_APP_VERSION, cli.ANDROID_APP_VERSION_CODE))
        extensions = {("PATCH", "/canadianCustomsDeclaration/{id}")}
        omitted = {("GET", "/app-update"), ("POST", "/file-public/create"), ("POST", "/leg/recent/save")}
        observed = set()
        tree = ast.parse(Path(cli.__file__).read_text())
        wrappers = {"api_get", "api_post", "api_patch", "api_delete", "_android_document_upload"}
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef) or function.name in wrappers or function.name.startswith("raw_"):
                continue
            bindings = {
                target.id: node.value
                for node in ast.walk(function) if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)
            }

            def paths(node):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return {node.value}
                if isinstance(node, ast.JoinedStr):
                    return {"".join(
                        part.value if isinstance(part, ast.Constant)
                        else cli.API_BASE_URL if isinstance(part.value, ast.Name) and part.value.id == "API_BASE_URL"
                        else "{id}" for part in node.values
                    )}
                if isinstance(node, ast.IfExp):
                    return paths(node.body) | paths(node.orelse)
                if isinstance(node, ast.Name) and node.id in bindings:
                    return paths(bindings[node.id])
                self.fail(f"Unverified dynamic API route in {function.name}: {ast.unparse(node)}")

            for node in ast.walk(function):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                name = node.func.id
                if name == "_android_document_upload":
                    calls = [("POST", kw.value) for kw in node.keywords if kw.arg in {"init_path", "attach_path"}]
                elif name in {"api_get", "api_post", "api_patch", "api_delete"}:
                    calls = [(name.removeprefix("api_").upper(), node.args[1])]
                elif name == "_http":
                    calls = [(node.args[0].value, node.args[1])]
                else:
                    continue
                for method, expression in calls:
                    for path in paths(expression):
                        route = (method, path.removeprefix(cli.API_BASE_URL))
                        self.assertIn(route, expected | extensions, f"{function.name}:{node.lineno}")
                        observed.add(route)
        self.assertEqual(expected - omitted, observed - extensions)

    def test_owner_leg_updates_reject_missing_or_unresolved_passengers(self) -> None:
        cases = [{}, {"legPassengers": None}, {"legPassengers": {}},
                 {"legPassengers": [{"id": "leg-passenger-only"}]}]
        commands = [
            ["leg", "update-passengers", "--leg-id", "leg-id", "--add", "saved-2", "--confirm"],
            ["leg", "update-required-info", "--leg-id", "leg-id", "--destination-street", "1 Main",
             "--destination-city", "New York", "--destination-state", "NY", "--destination-zip", "10001", "--confirm"],
        ]
        for leg in cases:
            for command in commands:
                with (
                    self.subTest(leg=leg, command=command),
                    patch.object(cli, "_guard_booking_probe"),
                    patch.object(cli, "get_api_token", return_value="token"),
                    patch.object(cli, "api_get", return_value={"data": leg}) as read,
                    patch.object(cli, "api_patch") as write,
                ):
                    result = self.runner.invoke(cli.app, command)
                    self.assertNotEqual(result.exit_code, 0)
                    read.assert_called_once_with("token", "/my-leg/leg-id")
                    write.assert_not_called()

    def test_shared_and_empty_flights_support_android_6110_arrival_transport(self) -> None:
        for command in ("shared-flight", "empty-leg"):
            with self.subTest(command=command), patch.object(cli, "api_post") as post:
                result = self.runner.invoke(cli.app, [
                    "booking", command, "--flight-id", "flight-id", "--passengers", "saved-1",
                    "--baggage", "none", "--ground-transportation", "yes", "--ground-transportation-when", "both",
                    "--ground-transportation-method", "taxi", "--arrival-ground-method", "sedan-and-driver",
                    "--arrival-pickup-address", "1 Main; New York; NY; 10001",
                    "--arrival-dropoff-address", "2 Main; New York; NY; 10002", "--dry-run",
                ])
                self.assertEqual(result.exit_code, 0, result.output)
                settings = json.loads(result.output)["data"]["payload"]["options"]["requestSettings"]
                self.assertEqual(settings["arrivalGroundTransportationMethod"], "SEDAN_AND_DRIVER")
                self.assertEqual(settings["arrivalGroundTransportationPickUpAddress"]["street"], "1 Main")
                self.assertEqual(settings["arrivalGroundTransportationDropOffAddress"]["zip"], "10002")
                self.assertNotIn("note", settings)
                post.assert_not_called()

    def test_token_cache_respects_the_requested_identity(self) -> None:
        with (
            patch.object(cli, "API_TOKEN_CACHE", Path(self.temporary.name) / "token.json"),
            patch.object(cli, "_api_login", return_value="new-session") as login,
        ):
            cli._save_api_token("old-session", "old@example.com")
            self.assertEqual(cli.get_api_token("OLD@example.com", "unused"), "old-session")
            login.assert_not_called()
            self.assertEqual(cli.get_api_token("new@example.com", "new-password"), "new-session")
            login.assert_called_once_with("new@example.com", "new-password")
            self.assertIsNone(cli._load_api_token("old@example.com"))
            self.assertEqual(cli._load_api_token("new@example.com"), "new-session")

    def test_login_exposes_android_two_factor_challenge_without_caching_it(self) -> None:
        for key in ("userId", "id"):
            with (
                self.subTest(key=key),
                patch.object(cli, "_http", return_value={"data": {key: "user-1"}}) as http,
                patch.object(cli, "_save_api_token") as save,
            ):
                result = self.runner.invoke(cli.app, [
                    "auth", "login", "--username", "jane@example.com", "--password", "test-password",
                ])
                self.assertEqual(result.exit_code, cli.EXIT_AUTH)
                self.assertIn("user-1", result.output)
                self.assertIn("2fa-sign-in", result.output)
                http.assert_called_once()
                save.assert_not_called()

    def test_failed_two_factor_sign_in_keeps_the_cached_session(self) -> None:
        with patch.object(cli, "_http", return_value={}), patch.object(cli, "_save_api_token") as save:
            result = self.runner.invoke(cli.app, ["auth", "2fa-sign-in", "--user-id", "user-1", "--code", "123456"])
        self.assertEqual(result.exit_code, cli.EXIT_AUTH)
        save.assert_not_called()

    def test_trips_timezone_is_used_and_does_not_leak_between_invocations(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_get_account_ids", return_value=["account-1"]),
            patch.object(cli, "api_post", return_value={"data": {"items": [{"departureTime": 1793554200}]}}),
        ):
            local = self.runner.invoke(cli.app, ["trips", "list", "--timezone", "America/Toronto"])
            utc = self.runner.invoke(cli.app, ["trips", "list"], env={"AIRSPRINT_TIMEZONE": ""})
        self.assertEqual(local.exit_code, 0, local.output)
        self.assertEqual(utc.exit_code, 0, utc.output)
        self.assertEqual(json.loads(local.output)["data"][0]["departureTime"], "2026-11-01T12:30:00-05:00")
        self.assertEqual(json.loads(utc.output)["data"][0]["departureTime"], "2026-11-01T17:30:00Z")

    def test_invalid_output_timezone_is_rejected_before_authentication(self) -> None:
        with patch.object(cli, "get_api_token") as auth:
            result = self.runner.invoke(cli.app, ["trips", "list", "--timezone", "not/a-timezone"])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        auth.assert_not_called()

    def test_passport_make_primary_does_not_attach_an_unrelated_passport(self) -> None:
        with (
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "api_get", return_value={"data": {"passportIds": ["owned"]}}),
            patch.object(cli, "api_patch") as write,
        ):
            result = self.runner.invoke(cli.app, [
                "passport", "make-primary", "--passenger-id", "saved-1", "--passport-id", "unrelated", "--confirm",
            ])
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        write.assert_not_called()

    def test_international_booking_requires_verifiable_passport_records(self) -> None:
        for passenger in ({"data": {"passportIds": ["passport-1"]}}, {"data": {"passports": []}}):
            patches = self.international_document_patches({"return_value": passenger})
            with self.subTest(passenger=passenger), patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(cli, "api_post") as post:
                result = self.runner.invoke(cli.app, [
                    "booking", "create", "--leg", "CYUL>KTEB@2026-09-01T16:00",
                    "--passengers", "saved-1", "--passport", "saved-1=passport-1", "--baggage", "none",
                    "--destination-street", "1 Main", "--destination-city", "New York",
                    "--destination-state", "NY", "--destination-zip", "10001",
                ])
                self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
                self.assertIn("could not be verified", result.output)
                post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
