import unittest
from unittest.mock import patch
import airsprint_cli as cli


class Android6112Tests(unittest.TestCase):
    def test_envelope_failure_is_not_success_or_retried(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"httpStatusCode":409,"status":"error","httpStatusReason":"Conflict","failureMessage":"Already locked"}'

        with (
            patch.object(cli, "urlopen", return_value=Response()) as request,
            patch.object(cli, "_ssl_ctx"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Already locked"):
                cli._http(
                    "POST",
                    "https://api.airsprint.com/api/flight/lock",
                    api_request=True,
                )
        request.assert_called_once()

    def test_envelope_codes_are_strict(self):
        for code in [True, "200", 200.0, 400, 500]:
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                cli._check_api_response({"httpStatusCode": code})
        cli._check_api_response({"httpStatusCode": 201})
        cli._check_api_response({"data": []})

    def test_airport_fallback_uses_name_and_exact_icao(self):
        with (
            patch.object(cli, "_load_data_cache", return_value={}),
            patch.object(cli, "_save_data_cache"),
            patch.object(
                cli,
                "api_post",
                return_value={
                    "data": {
                        "items": [
                            {"id": "near", "codeICAO": "CYQA"},
                            {"id": "correct", "codeICAO": "CYQB"},
                        ]
                    }
                },
            ) as post,
        ):
            self.assertEqual(cli._resolve_airport("token", "CYQB"), "correct")
        self.assertEqual(post.call_args.args[2]["filter"], {"name": "CYQB"})
        self.assertEqual(post.call_args.args[2]["page"]["limit"], 100)

    def test_share_uses_only_active_account_entitlements(self):
        accounts = [
            {"id": "active", "accessLevels": []},
            {"id": "other", "accessLevels": [{"aircraftId": "cj2"}]},
        ]
        with (
            patch.object(
                cli, "api_get", return_value={"data": {"activeAccountId": "active"}}
            ),
            patch.object(cli, "_get_accounts", return_value=accounts),
        ):
            with self.assertRaises(cli.typer.Exit):
                cli._require_booking_share_eligibility("token", "cj2")
        accounts[0]["accessLevels"] = [{"aircraftId": "cj2"}]
        with (
            patch.object(
                cli, "api_get", return_value={"data": {"activeAccountId": "active"}}
            ) as read,
            patch.object(cli, "_get_accounts", return_value=accounts),
        ):
            self.assertEqual(
                cli._require_booking_share_eligibility("token", "cj2")["basis"],
                "owned-aircraft",
            )
        read.assert_called_once_with("token", "/me")

    def test_infinity_exception_is_cj2_only(self):
        for aircraft, allowed in [
            ("Cessna Citation CJ2+", True),
            ("Cessna Citation CJ3+", False),
        ]:
            with (
                self.subTest(aircraft=aircraft),
                patch.object(
                    cli,
                    "api_get",
                    side_effect=[
                        {"data": {"activeAccountId": "active"}},
                        {"data": {"id": "chosen", "name": aircraft}},
                    ],
                ),
                patch.object(
                    cli,
                    "_get_accounts",
                    return_value=[
                        {
                            "id": "active",
                            "accessLevels": [
                                {
                                    "aircraftName": "Embraer Praetor",
                                    "accessLevelName": "Infinity",
                                    "aircraftId": "owned",
                                }
                            ],
                        }
                    ],
                ),
            ):
                if allowed:
                    self.assertEqual(
                        cli._require_booking_share_eligibility("token", "chosen")[
                            "basis"
                        ],
                        "embraer-infinity-cj2",
                    )
                else:
                    with self.assertRaises(cli.typer.Exit):
                        cli._require_booking_share_eligibility("token", "chosen")


if __name__ == "__main__":
    unittest.main()
