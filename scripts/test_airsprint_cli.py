import json
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

    @staticmethod
    def booking_body(passengers=None) -> dict:
        return {
            "legs": [{
                "departureAirportId": "departure",
                "arrivalAirportId": "arrival",
                "aircraftId": "aircraft",
                "date": "2026-09-01T16:00:00Z",
                "numberOfSeats": 1,
                "passengers": passengers if passengers is not None else [],
                "petIds": [],
                "requestSettings": {
                    "cateringRequired": False,
                    "groundTransportationRequired": False,
                },
            }],
            "baggage": [],
            "shareSettings": {
                "specialRequests": "",
                "openToShare": False,
                "networkType": "MY_NETWORK",
                "seats": 0,
                "petsAllowed": False,
                "childrenAllowed": False,
            },
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
        request.assert_called_once_with("token", "/my-accounts", {})

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
                "raw", "api-patch", "--path", "/leg/leg-id", "--body", "{}",
            ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        request.assert_not_called()

    def test_raw_json_body_must_be_an_object(self) -> None:
        result = self.runner.invoke(cli.app, [
            "raw", "api-patch", "--path", "/my-user", "--body", "[]", "--dry-run",
        ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("must be an object", result.output)

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

    def test_booking_share_percentage_is_limited_to_app_range(self) -> None:
        body = self.booking_body()
        body["shareSettings"]["joinerVariableCostPercentage"] = 20
        result = self.runner.invoke(cli.app, [
            "booking", "create", "--body", json.dumps(body), "--dry-run",
        ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("between 30 and 80", result.output)

    def test_booking_rejects_top_level_account_id(self) -> None:
        body = self.booking_body()
        body["accountId"] = "must-not-be-sent"
        result = self.runner.invoke(cli.app, [
            "booking", "create", "--body", json.dumps(body), "--dry-run",
        ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("implicit in the token", result.output)

    def test_us_booking_requires_and_copies_destination_address(self) -> None:
        body = self.booking_body(["saved-1", {"id": "saved-2"}])
        body["legs"].append({**body["legs"][0], "passengers": [{"id": "saved-1"}]})
        address = {
            "street": "1 Main St",
            "city": "New York",
            "state": "NY",
            "zip": "10001",
            "country": "United States",
        }
        result = self.runner.invoke(cli.app, [
            "booking", "create",
            "--body", json.dumps(body),
            "--destination-address", json.dumps(address),
            "--us-touching",
            "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)["data"]["payload"]
        for leg in payload["legs"]:
            for passenger in leg["passengers"]:
                self.assertEqual(passenger["destinationAddress"], address)

    def test_us_booking_without_destination_address_is_refused(self) -> None:
        result = self.runner.invoke(cli.app, [
            "booking", "create",
            "--body", json.dumps(self.booking_body(["saved-1"])),
            "--us-touching",
            "--dry-run",
        ])

        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        self.assertIn("require --destination-address", result.output)

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
        read.assert_called_once_with("token", "/leg/leg-id")
        write.assert_called_once_with("token", "/leg/leg-id", {
            "options": {"passengers": [{"id": "saved-1"}, {"id": "saved-3"}]},
        })
        plan = json.loads(result.output)["data"]["plan"]
        self.assertEqual([item["id"] for item in plan["kept"]], ["saved-1"])
        self.assertEqual([item["id"] for item in plan["dropped"]], ["saved-2"])

    def test_passport_create_normalizes_dates_to_milliseconds(self) -> None:
        result = self.runner.invoke(cli.app, [
            "passport", "create",
            "--body", json.dumps({
                "dateOfBirth": "1980-01-02",
                "expirationDate": 1893456000,
            }),
            "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)["data"]["payload"]
        self.assertEqual(payload["dateOfBirth"], 315619200000)
        self.assertEqual(payload["expirationDate"], 1893456000000)

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

    def test_customs_body_validates_date_semantics(self) -> None:
        body = {
            "legPassengerIds": ["leg-passenger-1", "leg-passenger-2"],
            "purposeOfTravel": "PLEASURE",
            "travelDescription": "Vacation",
            "date": "2026-09-01T14:00:00Z",
        }
        result = self.runner.invoke(cli.app, [
            "customs", "create", "--body", json.dumps(body), "--dry-run",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)["data"]
        self.assertEqual(data["declarations"], 2)
        self.assertIn("signature", data["message"].lower())

    def test_customs_booking_mode_resolves_leg_passenger_and_outbound_date(self) -> None:
        trip = {"data": {"legs": [{
            "id": "leg-1",
            "departureDate": "2026-09-01T14:00:00Z",
            "departureAirport": {"address": {"country": "Canada"}},
            "passengers": [{
                "id": "leg-passenger-1",
                "passenger": {
                    "id": "saved-passenger-1",
                    "firstName": "Jane",
                    "lastName": "Doe",
                },
            }],
        }]}}
        with (
            patch.object(cli, "_guard_booking_probe"),
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_resolve_trip_uuid", return_value="trip-uuid"),
            patch.object(cli, "api_get", return_value=trip) as read,
        ):
            result = self.runner.invoke(cli.app, [
                "customs", "create",
                "--booking", "ABCDE",
                "--passengers", "Jane Doe",
                "--purpose", "pleasure",
                "--description", "Vacation",
                "--dry-run",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        read.assert_called_once_with("token", "/trip/trip-uuid")
        payload = json.loads(result.output)["data"]["payload"]
        self.assertEqual(payload["legPassengerIds"], ["leg-passenger-1"])
        self.assertEqual(payload["date"], "2026-09-01T14:00:00Z")

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

    def test_skill_flag_prints_live_booking_rules(self) -> None:
        result = self.runner.invoke(cli.app, ["--skill"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Non-negotiable live-booking safety", result.output)
        self.assertIn("leg update-passengers", result.output)


if __name__ == "__main__":
    unittest.main()
