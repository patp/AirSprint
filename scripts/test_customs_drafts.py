import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner
import airsprint_cli as cli


class CustomsDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "RETURN-jane-doe--john-roe.json"
        self.runner = CliRunner()
        self.patches = [
            patch.object(cli, "CUSTOMS_DRAFT_DIR", self.root),
            patch.object(cli, "get_api_token", return_value="token"),
            patch.object(cli, "_guard_booking_probe"),
            patch(
                "socket.create_connection", side_effect=AssertionError("offline test")
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.leg = {
            "id": "return-leg",
            "bookingId": "RETURN",
            "status": "CONFIRMED",
            "customsDeclarationId": "link-1",
            "flight": {
                "departureTime": "2026-10-07T19:00:00Z",
                "arrivalTime": "2026-10-07T20:25:00Z",
                "departureAirportCode": "KTEB",
                "arrivalAirportCode": "CYQB",
                "departureAirportCountry": "United States",
                "arrivalAirportCountry": "Canada",
                "departureAirportTimezone": "America/New_York",
                "arrivalAirportTimezone": "America/Toronto",
            },
            "legPassengers": [],
        }
        for i, name in enumerate(["Jane Doe", "John Roe"]):
            passport = {
                "id": f"passport-{i}",
                "passportNumber": f"TEST000{i}",
                "nationality": "CA",
                "issuingAuthority": "OTTAWA",
                "dateOfBirth": "1980-01-01T05:00:00Z",
                "expirationDate": "2030-01-01T05:00:00Z",
                "image": f"https://example.test/scan-{i}.pdf?signature=volatile",
            }
            self.leg["legPassengers"].append(
                {
                    "id": f"lp-{i}",
                    "passengerId": f"trip-person-{i}",
                    "name": name,
                    "selectedPassportId": passport["id"],
                    "passports": [passport],
                    "destinationAddress": {
                        "street": "1 Ann St",
                        "street2": None,
                        "city": "New York",
                        "state": "NY",
                        "zip": "10038",
                    },
                }
            )
        self.link = {
            "id": "link-1",
            "leg": {
                "id": "return-leg",
                "passengers": [
                    {
                        "id": f"lp-{i}",
                        "name": p["name"],
                        "customsDeclarationAlreadySubmitted": False,
                    }
                    for i, p in enumerate(self.leg["legPassengers"])
                ],
            },
        }
        self.answers = [
            "--purpose",
            "pleasure",
            "--date",
            "2026-10-07",
            "--timezone",
            "America/Toronto",
            "--has-pet",
            "no",
            "--has-alcohol-or-tobacco",
            "no",
            "--has-imported-goods",
            "no",
            "--has-high-value-currency",
            "no",
        ]

    def read(self, token, path):
        if path == "/my-leg/return-leg":
            return {"data": copy.deepcopy(self.leg)}
        if path == "/canadian-customs-declaration-link/link-1":
            return {"data": copy.deepcopy(self.link)}
        raise AssertionError(path)

    def prepare(self, *extra, complete=True):
        with (
            patch.object(cli, "api_get", side_effect=self.read),
            patch.object(cli, "api_post") as write,
        ):
            result = self.runner.invoke(
                cli.app,
                [
                    "customs",
                    "prepare",
                    "--leg-id",
                    "return-leg",
                    "--passengers",
                    "Jane Doe,John Roe",
                    "--draft",
                    str(self.path),
                    *(self.answers if complete else []),
                    *extra,
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        write.assert_not_called()
        return json.loads(result.output)["data"]

    def certify(self, person):
        result = self.runner.invoke(
            cli.app,
            [
                "customs",
                "certify",
                "--draft",
                str(self.path),
                "--passenger",
                person,
                "--approve",
                "yes",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        return json.loads(result.output)["data"]

    def certified(self, *extra):
        self.prepare(*extra)
        self.certify("Jane Doe")
        self.certify("lp-1")

    def submit(self, response=None):
        if response is None:

            def response(_token, path, body):
                self.assertEqual(path, "/canadianCustomsDeclaration/create")
                self.assertNotIn("agreedToDeclaration", body)
                return {
                    "data": [
                        {"id": "decl-" + p, "passengerId": p}
                        for p in body["legPassengerIds"]
                    ]
                }

        with (
            patch.object(cli, "api_get", side_effect=self.read) as read,
            patch.object(cli, "api_post", side_effect=response) as write,
        ):
            result = self.runner.invoke(
                cli.app, ["customs", "submit", "--draft", str(self.path), "--confirm"]
            )
        return result, read, write

    def test_incomplete_draft_is_private_editable_and_never_posts(self):
        draft = self.prepare(complete=False)
        self.assertFalse(draft["readyForApproval"])
        self.assertIn("--has-alcohol-or-tobacco", draft["blockers"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            draft["submissionStatus"]["passengers"][0]["submissionStatus"],
            "not-submitted",
        )
        with (
            patch.object(cli, "api_get") as read,
            patch.object(cli, "api_post") as write,
        ):
            result = self.runner.invoke(
                cli.app,
                ["customs", "prepare", "--draft", str(self.path), *self.answers],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(json.loads(result.output)["data"]["readyForApproval"])
        read.assert_not_called()
        write.assert_not_called()

    def test_default_filename_uses_full_name_and_never_overwrites(self):
        self.leg["legPassengers"][0]["name"] = "Élodie Durand"
        expected = self.root / "RETURN-elodie-durand.json"
        args = [
            "customs",
            "prepare",
            "--leg-id",
            "return-leg",
            "--passengers",
            "Élodie Durand",
        ]
        with (
            patch.object(cli, "api_get", side_effect=self.read),
            patch.object(cli, "api_post") as write,
        ):
            first = self.runner.invoke(cli.app, args)
            self.assertEqual(first.exit_code, 0, first.output)
            self.assertEqual(Path(json.loads(first.output)["data"]["draft"]), expected)
            original = expected.read_bytes()
            second = self.runner.invoke(cli.app, args)
        self.assertEqual(second.exit_code, cli.EXIT_VALIDATION, second.output)
        self.assertIn("already exists", second.output)
        self.assertEqual(expected.read_bytes(), original)
        write.assert_not_called()

    def test_first_name_alias_is_rejected_for_passenger_selection(self):
        with (
            patch.object(cli, "api_get", side_effect=self.read),
            patch.object(cli, "api_post") as write,
        ):
            result = self.runner.invoke(
                cli.app,
                [
                    "customs",
                    "prepare",
                    "--leg-id",
                    "return-leg",
                    "--passengers",
                    "Jane",
                ],
            )
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("full first and last name", result.output)
        self.assertFalse(list(self.root.glob("*.json")))
        write.assert_not_called()

    def test_first_name_filename_cannot_be_created_or_used(self):
        alias = self.root / "RETURN-jane.json"
        with (
            patch.object(cli, "api_get", side_effect=self.read),
            patch.object(cli, "api_post") as write,
        ):
            result = self.runner.invoke(
                cli.app,
                [
                    "customs",
                    "prepare",
                    "--leg-id",
                    "return-leg",
                    "--passengers",
                    "Jane Doe",
                    "--draft",
                    str(alias),
                ],
            )
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("RETURN-jane-doe.json", result.output)
        self.assertFalse(alias.exists())
        write.assert_not_called()
        self.prepare()
        self.path.rename(alias)
        with (
            patch.object(cli, "api_get") as read,
            patch.object(cli, "api_post") as write,
        ):
            for command in (
                ["review"],
                ["certify", "--passenger", "Jane Doe", "--approve", "yes"],
                ["submit", "--confirm"],
            ):
                result = self.runner.invoke(
                    cli.app, ["customs", *command, "--draft", str(alias)]
                )
                self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
                self.assertIn("full-name draft filename", result.output)
        read.assert_not_called()
        write.assert_not_called()

    def test_individual_certifications_are_required_and_edit_invalidates_them(self):
        self.prepare()
        self.certify("Jane Doe")
        result, read, write = self.submit()
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("John Roe", result.output)
        read.assert_not_called()
        write.assert_not_called()
        self.certify("John Roe")
        result = self.runner.invoke(
            cli.app,
            [
                "customs",
                "prepare",
                "--draft",
                str(self.path),
                "--souvenir-items",
                "Books",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            len(json.loads(result.output)["data"]["pendingCertifications"]), 2
        )

    def test_default_submits_one_form_per_person_and_blocks_second_run(self):
        self.certified()
        result, read, write = self.submit()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            [c.args[2]["legPassengerIds"] for c in write.call_args_list],
            [["lp-0"], ["lp-1"]],
        )
        self.assertEqual(read.call_count, 2)  # leg + link, no post-write read-back
        self.assertEqual(cli._load_customs_draft(self.path)["state"], "submitted")
        result, read, write = self.submit()
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        read.assert_not_called()
        write.assert_not_called()

    def test_family_one_form_still_requires_each_person_certification(self):
        self.certified("--family", "yes")
        result, read, write = self.submit()
        self.assertEqual(result.exit_code, 0, result.output)
        write.assert_called_once()
        self.assertEqual(write.call_args.args[2]["legPassengerIds"], ["lp-0", "lp-1"])

    def test_already_submitted_after_preparation_stops_all_forms(self):
        self.certified()
        self.link["leg"]["passengers"][1]["customsDeclarationAlreadySubmitted"] = True
        result, read, write = self.submit()
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("Already submitted", result.output)
        write.assert_not_called()
        self.assertTrue(
            cli._load_customs_draft(self.path)["submissionStatus"]["passengers"][1][
                "customsDeclarationAlreadySubmitted"
            ]
        )

    def test_unknown_or_foreign_link_status_never_submits(self):
        for mutation in [
            "missing",
            "string",
            "wrong-leg",
            "missing-person",
            "duplicate-person",
        ]:
            with self.subTest(mutation=mutation):
                self.path.unlink(missing_ok=True)
                self.certified()
                original = copy.deepcopy(self.link)
                if mutation == "missing":
                    self.link["leg"]["passengers"][0].pop(
                        "customsDeclarationAlreadySubmitted"
                    )
                if mutation == "string":
                    self.link["leg"]["passengers"][0][
                        "customsDeclarationAlreadySubmitted"
                    ] = "false"
                if mutation == "wrong-leg":
                    self.link["leg"]["id"] = "outbound-leg"
                if mutation == "missing-person":
                    self.link["leg"]["passengers"].pop()
                if mutation == "duplicate-person":
                    self.link["leg"]["passengers"].append(
                        self.link["leg"]["passengers"][0]
                    )
                result, read, write = self.submit()
                self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
                write.assert_not_called()
                self.link = original

    def test_changed_passport_address_or_flight_needs_new_review(self):
        for field in ["passport", "address", "flight"]:
            with self.subTest(field=field):
                self.path.unlink(missing_ok=True)
                self.certified()
                original = copy.deepcopy(self.leg)
                if field == "passport":
                    self.leg["legPassengers"][0]["passports"][0]["passportNumber"] = (
                        "OTHER123"
                    )
                if field == "address":
                    self.leg["legPassengers"][0]["destinationAddress"]["street"] = (
                        "2 Main"
                    )
                if field == "flight":
                    self.leg["flight"]["arrivalTime"] = "2026-10-08T20:25:00Z"
                result, read, write = self.submit()
                self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
                self.assertIn("changed since preparation", result.output)
                write.assert_not_called()
                self.leg = original

    def test_changed_presigned_url_does_not_invalidate_review(self):
        self.certified()
        self.leg["legPassengers"][0]["passports"][0]["image"] = (
            "https://example.test/new-signature"
        )
        result, read, write = self.submit()
        self.assertEqual(result.exit_code, 0, result.output)

    def test_timeout_keeps_partial_receipts_and_blocks_replay(self):
        self.certified()
        result, read, write = self.submit(
            [{"data": {"id": "decl-0"}}, RuntimeError("timeout")]
        )
        self.assertNotEqual(result.exit_code, 0)
        draft = cli._load_customs_draft(self.path)
        self.assertEqual(draft["state"], "uncertain")
        self.assertEqual(len(draft["results"]), 1)
        self.assertEqual(draft["pendingLegPassengerIds"], ["lp-1"])
        result, read, write = self.submit()
        read.assert_not_called()
        write.assert_not_called()

    def test_new_draft_cannot_bypass_local_submission_journal(self):
        self.certified()
        result, _, _ = self.submit([RuntimeError("timeout")])
        self.assertNotEqual(result.exit_code, 0)
        self.path = self.root / "another" / "RETURN-jane-doe--john-roe.json"
        self.certified()
        result, read, write = self.submit()
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        self.assertIn("already attempted", result.output)
        read.assert_not_called()
        write.assert_not_called()

    def test_socket_timeout_is_uncertain_and_never_retried(self):
        self.certified()
        result, _read, write = self.submit(TimeoutError("socket timed out"))
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("status is uncertain", str(result.exception))
        self.assertEqual(cli._load_customs_draft(self.path)["state"], "uncertain")
        write.assert_called_once()
        result, read, write = self.submit()
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION)
        read.assert_not_called()
        write.assert_not_called()

    def test_post_submission_date_edit_is_retired(self):
        with patch.object(cli, "api_patch") as write:
            result = self.runner.invoke(
                cli.app,
                [
                    "customs",
                    "update-date",
                    "--id",
                    "declaration",
                    "--date",
                    "2026-10-07",
                    "--confirm",
                ],
            )
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        write.assert_not_called()

    def test_empty_success_response_remains_uncertain(self):
        self.certified()
        result, read, write = self.submit([{"data": []}])
        self.assertNotEqual(result.exit_code, 0)
        write.assert_called_once()
        self.assertEqual(cli._load_customs_draft(self.path)["state"], "uncertain")

    def test_draft_cannot_be_certified_for_outbound_or_missing_documents(self):
        self.leg["flight"]["arrivalAirportCountry"] = "United States"
        self.leg["legPassengers"][0]["selectedPassportId"] = "other-profile-passport"
        preview = self.prepare()
        self.assertIn("CanadianCustomsRequiresLegArrivingInCanada", preview["blockers"])
        self.assertTrue(
            any("selectedPassportNotOnTripProfile" in x for x in preview["blockers"])
        )
        result = self.runner.invoke(
            cli.app,
            [
                "customs",
                "certify",
                "--draft",
                str(self.path),
                "--passenger",
                "Jane Doe",
                "--approve",
                "yes",
            ],
        )
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)

    def test_status_keeps_false_in_compact_output_and_is_read_only(self):
        with (
            patch.object(cli, "api_get", side_effect=self.read) as read,
            patch.object(cli, "api_post") as write,
        ):
            result = self.runner.invoke(
                cli.app, ["customs", "status", "--link-id", "link-1", "--compact"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        status = json.loads(result.output)["data"]["submissionStatus"]["passengers"][0]
        self.assertIs(status["customsDeclarationAlreadySubmitted"], False)
        self.assertEqual(status["submissionStatus"], "not-submitted")
        read.assert_called_once()
        write.assert_not_called()

    def test_booking_selects_return_and_ignores_cancelled_duplicate(self):
        outbound = copy.deepcopy(self.leg)
        outbound.update(id="outbound", status="CONFIRMED")
        outbound["flight"]["arrivalAirportCountry"] = "United States"
        cancelled = copy.deepcopy(self.leg)
        cancelled.update(id="cancelled", status="CANCELLED")
        with (
            patch.object(cli, "_resolve_trip_uuid", return_value="trip-id"),
            patch.object(
                cli,
                "api_get",
                side_effect=[
                    {"data": {"legs": [outbound, cancelled, self.leg]}},
                    {"data": self.link},
                ],
            ) as read,
            patch.object(cli, "api_post") as write,
        ):
            result = self.runner.invoke(
                cli.app,
                [
                    "customs",
                    "prepare",
                    "--booking",
                    "RETURN",
                    "--passengers",
                    "Jane Doe",
                    "--dry-run",
                    *self.answers,
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["data"]["legId"], "return-leg")
        self.assertEqual(read.call_count, 2)
        write.assert_not_called()

    def test_hand_edited_draft_is_rejected_offline(self):
        self.prepare()
        draft = json.loads(self.path.read_text())
        draft["answers"]["has_pet"] = "yes"
        self.path.write_text(json.dumps(draft))
        with patch.object(cli, "api_get") as read:
            result = self.runner.invoke(
                cli.app, ["customs", "review", "--draft", str(self.path)]
            )
        self.assertEqual(result.exit_code, cli.EXIT_VALIDATION, result.output)
        read.assert_not_called()

    def test_preserves_selection_and_removes_null_street2_in_leg_patch(self):
        payload = cli._leg_passenger_payload(self.leg["legPassengers"][0])
        self.assertEqual(payload["passport"], {"id": "passport-0"})
        self.assertNotIn("street2", payload["destinationAddress"])

    def test_hidden_guest_passports_are_read_by_actual_profile_id(self):
        with (
            patch.object(
                cli,
                "api_get",
                return_value={
                    "data": {
                        "id": "guest",
                        "passports": [{"id": "p", "passportNumber": "TEST"}],
                    }
                },
            ) as read,
            patch.object(cli, "api_post") as write,
        ):
            result = self.runner.invoke(
                cli.app, ["passport", "list", "--passenger-id", "guest"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        read.assert_called_once_with("token", "/my-passenger/guest")
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
