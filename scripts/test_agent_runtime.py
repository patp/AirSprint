import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

import airsprint_cli as cli
from airsprint_agent import AgentRuntime, CommandRegistry, error_result, serve_jsonl
from airsprint_events import EventStore


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = EventStore(Path(self.temp.name) / "state.sqlite3", "test-owner")
        self.runtime = AgentRuntime(cli, store=self.store, principal="test-owner")
        self.addCleanup(patch.stopall)
        patch("socket.create_connection", side_effect=AssertionError("No network in runtime tests")).start()

    def test_discovery_preserves_empty_schema_and_hides_secrets(self):
        runner = CliRunner()
        result = runner.invoke(cli.app, ["agent", "describe", "--command", "auth status"])
        data = json.loads(result.stdout)["data"]
        self.assertEqual(data["inputSchema"]["properties"], {})
        for name in self.runtime.registry.commands:
            spec = self.runtime.registry.describe(name)
            self.assertNotIn("username", spec["inputSchema"]["properties"])
            self.assertNotIn("password", spec["inputSchema"]["properties"])
        self.assertNotIn("raw", self.runtime.registry.commands)
        catalog = self.runtime.registry.catalog()
        self.assertIn("groups", catalog)
        self.assertNotIn("commands", catalog)
        self.assertLess(len(json.dumps(catalog)), 4000)

    def test_type_validation_rejects_coercion_and_unknown_fields_before_auth(self):
        with patch.object(cli, "get_api_token") as auth:
            for arguments in [{"limit": "25"}, {"limit": True}, {"limit": 0}, {"payload": {}}, {"upcoming": "false"}]:
                result = self.runtime.execute("trips list", arguments)
                self.assertEqual(result["code"], "validation", result)
            auth.assert_not_called()
        self.assertEqual(self.runtime.execute("auth status", [])["code"], "validation")

    def test_boolean_false_maps_to_past_and_pagination_is_explicit(self):
        with patch.object(cli, "get_api_token", return_value="test"), patch.object(cli, "_get_account_ids", return_value=["a"]), patch.object(cli, "api_post", return_value={"data": {"items": [{"id": "leg"}], "total": 30}}) as post:
            result = self.runtime.execute("trips list", {"upcoming": False, "limit": 1, "offset": 2})
        self.assertEqual(post.call_args.args[2]["page"], {"limit": 1, "offset": 2})
        self.assertIn("max", post.call_args.args[2]["filter"]["departureTime"])
        self.assertEqual(result["page"]["nextOffset"], 3)
        self.assertFalse(result["page"]["complete"])

    def test_write_requires_confirmation_and_stable_key_before_network(self):
        with patch.object(cli, "get_api_token") as auth:
            result = self.runtime.execute("messages read", {"id": "message"})
            self.assertEqual(result["code"], "validation")
            result = self.runtime.execute("messages read", {"id": "message", "confirm": True})
            self.assertEqual(result["code"], "validation")
            auth.assert_not_called()

    def test_successful_write_replays_saved_receipt_without_another_api_call(self):
        with patch.object(cli, "get_api_token", return_value="test"), patch.object(cli, "api_patch", return_value={"data": {"id": "message"}}) as api:
            first = self.runtime.execute("messages read", {"id": "message", "confirm": True}, operation_key="read-message-0001")
            again = self.runtime.execute("messages read", {"id": "message", "confirm": True}, operation_key="read-message-0001")
        self.assertEqual(first["status"], "ok", first)
        self.assertTrue(again["meta"]["replayed"])
        api.assert_called_once()
        self.assertEqual(self.store.operation("read-message-0001")["state"], "completed")

    def test_uncertain_write_is_blocked_with_same_or_different_key(self):
        def timeout(*_args):
            cli._REQUEST_TRACE.get().append({"effect": "write", "outcome": "uncertain"})
            raise RuntimeError("connection closed before a response")
        with patch.object(cli, "get_api_token", return_value="test"), patch.object(cli, "api_patch", side_effect=timeout) as api:
            first = self.runtime.execute("messages read", {"id": "message", "confirm": True}, operation_key="read-message-0001")
            same = self.runtime.execute("messages read", {"id": "message", "confirm": True}, operation_key="read-message-0001")
            different = self.runtime.execute("messages read", {"id": "message", "confirm": True}, operation_key="read-message-0002")
        self.assertTrue(first["requestMayHaveSucceeded"])
        self.assertEqual(same["code"], "operation_uncertain")
        self.assertTrue(same["requestMayHaveSucceeded"])
        self.assertEqual(different["code"], "operation_uncertain")
        api.assert_called_once()

    def test_partial_success_is_also_uncertain(self):
        def partial(*_args):
            cli._REQUEST_TRACE.get().append({"effect": "write", "outcome": "accepted"})
            raise ValueError("Could not save the returned receipt")
        with patch.object(cli, "get_api_token", return_value="test"), patch.object(cli, "api_patch", side_effect=partial):
            result = self.runtime.execute("messages read", {"id": "message", "confirm": True}, operation_key="partial-result-01")
        self.assertTrue(result["requestMayHaveSucceeded"])
        self.assertEqual(self.store.operation("partial-result-01")["state"], "uncertain")

    def test_operation_conflict_and_pending_survive_restart(self):
        self.store.begin_operation("pending-write-01", "messages read", "fingerprint")
        reopened = EventStore(self.store.path, "test-owner")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            reopened.begin_operation("pending-write-01", "messages read", "fingerprint")
        with self.assertRaisesRegex(ValueError, "different arguments"):
            reopened.begin_operation("pending-write-01", "messages read", "different")

    def test_concurrent_write_uses_one_request(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def api(*_args):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"data": {"id": "message"}}
        def run():
            results.append(self.runtime.execute("messages read", {"id": "message", "confirm": True}, operation_key="concurrent-write-01"))
        with patch.object(cli, "get_api_token", return_value="test"), patch.object(cli, "api_patch", side_effect=api) as request:
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(5))
            duplicate = self.runtime.execute("messages read", {"id": "message", "confirm": True}, operation_key="concurrent-write-01")
            release.set()
            thread.join(5)
        self.assertEqual(duplicate["code"], "operation_uncertain")
        request.assert_called_once()
        self.assertEqual(results[0]["status"], "ok")

    def test_dry_run_does_not_need_operation_key_or_confirmation(self):
        with patch.object(cli, "get_api_token") as auth:
            result = self.runtime.execute("messages update", {"ids": "message", "read": "yes", "dry_run": True})
        self.assertEqual(result["status"], "ok", result)
        auth.assert_not_called()

    def test_jsonl_recovers_after_malformed_and_oversize_requests(self):
        input_data = "{bad}\n" + "x" * 270000 + "\n" + json.dumps({"id": 3, "command": "auth status", "arguments": {}}) + "\n"
        sink = io.StringIO()
        with patch.object(cli, "_load_api_token", return_value=None):
            serve_jsonl(cli, io.StringIO(input_data), sink)
        rows = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["id"], 3)
        self.assertEqual(rows[-1]["status"], "ok")

    def test_unknown_cli_option_is_json_without_ansi_traceback(self):
        result = subprocess.run([sys.executable, str(Path(cli.__file__)), "trips", "list", "--bogus"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stderr)
        self.assertEqual(error["code"], "validation")
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("\x1b", result.stderr)

    def test_no_cross_command_timezone_state(self):
        with patch.object(cli, "get_api_token", return_value="test"), patch.object(cli, "_get_account_ids", return_value=["a"]), patch.object(cli, "api_post", return_value={"data": {"items": [], "total": 0}}):
            self.runtime.execute("trips list", {"timezone": "America/Toronto"})
            self.runtime.execute("trips list", {})
        self.assertIsNone(cli._OUTPUT_TIMEZONE)

    def test_booking_code_lookup_rejects_ambiguity_and_incomplete_page(self):
        for response in [
            {"data": {"items": [{"bookingId": "ABC", "tripId": "a"}, {"bookingId": "ABC", "tripId": "b"}], "total": 2}},
            {"data": {"items": [{"bookingId": "ABC", "tripId": "a"}], "total": 201}},
        ]:
            with patch.object(cli, "_get_account_ids", return_value=["a"]), patch.object(cli, "api_post", return_value=response), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(cli.typer.Exit):
                    cli._resolve_trip_uuid("test", "abc")

    def test_booking_code_casefold_and_same_trip_multiple_legs(self):
        response = {"data": {"items": [{"bookingId": "ABC", "tripId": "a"}, {"bookingId": "ABC", "tripId": "a"}], "total": 2}}
        with patch.object(cli, "_get_account_ids", return_value=["a"]), patch.object(cli, "api_post", return_value=response):
            self.assertEqual(cli._resolve_trip_uuid("test", "abc"), "a")

    def test_error_codes_and_request_effects(self):
        self.assertEqual(error_result(RuntimeError('{"http_code":401,"message":"unauthorized"}'))["code"], "authentication")
        self.assertEqual(error_result(RuntimeError('{"http_code":429,"message":"limit"}'))["code"], "rate_limited")
        registry = CommandRegistry(cli)
        self.assertTrue(registry.describe("trips get")["mayNotifyOwner"])
        self.assertFalse(registry.describe("trips list")["mayNotifyOwner"])

    def test_server_failure_envelope_does_not_claim_write_was_rejected(self):
        class Response:
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def read(self):
                return b'{"httpStatusCode":500,"failureMessage":"backend failed"}'
        trace = []
        token = cli._REQUEST_TRACE.set(trace)
        try:
            with patch.object(cli, "_ssl_ctx", return_value=None), patch.object(cli, "urlopen", return_value=Response()):
                with self.assertRaises(RuntimeError):
                    cli._http("POST", cli.API_BASE_URL + "/passenger/create", api_request=True)
        finally:
            cli._REQUEST_TRACE.reset(token)
        self.assertEqual(trace[0]["outcome"], "uncertain")


if __name__ == "__main__":
    unittest.main()
