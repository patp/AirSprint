import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import airsprint_cli as cli
from airsprint_events import EventStore, https_post
from airsprint_mcp import CAPABILITIES_KEY, MCPService, PROTOCOL, VERSION_KEY, make_http_server, serve, serve_stdio


SECRET = "whsec_" + base64.b64encode(b"integration-test-signing-key-32!!").decode()
BEARER = "test-only-mcp-bearer-credential-32bytes"
_NATIVE_SSL_CONTEXT = ssl.SSLContext  # Before CLI truststore injection in other tests.


def request(method, **params):
    return {"jsonrpc": "2.0", "id": 1, "method": method,
            "params": {"_meta": {VERSION_KEY: PROTOCOL, CAPABILITIES_KEY: {}}, **params}}


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = EventStore(Path(self.temp.name) / "state.sqlite3", "owner")
        self.service = MCPService(cli, self.store)
        self.addCleanup(patch.stopall)
        patch("socket.create_connection", side_effect=AssertionError("Protocol tests must remain offline")).start()

    def test_discovery_and_compact_tool_catalog(self):
        code, response = self.service.handle(request("server/discover"))
        self.assertEqual(code, 200)
        result = response["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("events", result["capabilities"])
        self.assertEqual(result["supportedVersions"], [PROTOCOL])
        tools = self.service.handle(request("tools/list"))[1]["result"]
        self.assertEqual(len(tools["tools"]), 8)
        self.assertLess(len(json.dumps(tools)), 8000)

    def test_metadata_is_per_request_no_sticky_capabilities(self):
        self.service.handle(request("server/discover"))
        bad = request("tools/list")
        del bad["params"]["_meta"][CAPABILITIES_KEY]
        code, response = self.service.handle(bad)
        self.assertEqual(code, 400)
        self.assertEqual(response["error"]["code"], -32602)

    def test_header_mismatch_unsupported_version_and_unknown_method(self):
        req = request("tools/call", name="airsprint_event_status", arguments={})
        headers = {"MCP-Protocol-Version": PROTOCOL, "Mcp-Method": "tools/call", "Mcp-Name": "wrong"}
        self.assertEqual(self.service.handle(req, headers)[1]["error"]["code"], -32020)
        req["params"]["_meta"][VERSION_KEY] = "2025-11-25"
        self.assertEqual(self.service.handle(req)[1]["error"]["code"], -32022)
        self.assertEqual(self.service.handle(request("does/not-exist"))[0], 404)

    def test_tool_validation_errors_are_visible_to_model(self):
        code, response = self.service.handle(request("tools/call", name="airsprint_run", arguments={"command": "trips list", "arguments": {"limit": "50"}}))
        self.assertEqual(code, 200)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["code"], "validation")

    def test_read_tool_refuses_writes_before_network(self):
        result = self.service.handle(request("tools/call", name="airsprint_read", arguments={"command": "messages read", "arguments": {"id": "n"}}))[1]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("changes state", result["structuredContent"]["message"])

    def test_tools_hide_auth_secrets_raw_and_recursive_service_commands(self):
        for command in ["auth login", "user change-password", "raw post", "mcp serve", "agent serve", "events collect"]:
            response = self.service.handle(request("tools/call", name="airsprint_run", arguments={"command": command, "arguments": {}}))[1]
            self.assertTrue(response["result"]["isError"], command)

    def test_command_search_and_describe_match_runtime(self):
        result = self.service.handle(request("tools/call", name="airsprint_commands", arguments={"group": "customs", "query": "submit"}))[1]["result"]["structuredContent"]
        self.assertIn("customs submit", [row["command"] for row in result["commands"]])
        self.assertTrue(all(row["command"].startswith("customs ") for row in result["commands"]))
        schema = self.service.handle(request("tools/call", name="airsprint_describe", arguments={"command": "trips list"}))[1]["result"]["structuredContent"]["inputSchema"]
        self.assertEqual(schema["properties"]["limit"]["type"], "integer")
        self.assertFalse(schema["additionalProperties"])

    def test_event_catalog_has_filters_and_payload_schemas(self):
        result = self.service.handle(request("events/list"))[1]["result"]
        self.assertEqual(len(result["events"]), 7)
        for definition in result["events"]:
            self.assertEqual(definition["delivery"], ["webhook"])
            self.assertIn("resourceId", definition["payloadSchema"]["required"])
        self.assertEqual(self.service.handle(request("events/list", cursor="unsupported"))[0], 400)

    def test_stdio_multiple_requests_and_parse_error(self):
        source = io.StringIO("bad json\n" + json.dumps(request("server/discover")) + "\n" + json.dumps(request("events/list")) + "\n")
        sink = io.StringIO()
        serve_stdio(self.service, source, sink)
        responses = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertIn("events", responses[2]["result"])

    def test_idle_service_starts_without_contacting_airsprint(self):
        source = io.StringIO(json.dumps(request("server/discover")) + "\n")
        sink = io.StringIO()
        with patch.dict(os.environ, {"AIRSPRINT_USERNAME": "test@example.invalid"}), patch("sys.stdin", source), patch("sys.stdout", sink), patch.object(cli, "get_api_token", side_effect=AssertionError("No subscription, no API call")) as token:
            serve(cli, state=Path(self.temp.name) / "idle.sqlite3")
        token.assert_not_called()
        self.assertIn("result", json.loads(sink.getvalue()))

    def test_responses_validate_against_optional_official_schema(self):
        path = os.getenv("AIRSPRINT_MCP_SCHEMA")
        if not path:
            self.skipTest("Set AIRSPRINT_MCP_SCHEMA to the official 2026-07-28 schema.json")
        from jsonschema import Draft202012Validator
        schema = json.loads(Path(path).read_text())
        for method, definition, params in [
            ("server/discover", "DiscoverResultResponse", {}),
            ("tools/list", "ListToolsResultResponse", {}),
            ("tools/call", "CallToolResultResponse", {"name": "airsprint_event_status", "arguments": {}}),
        ]:
            validator = Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/" + definition})
            validator.validate(self.service.handle(request(method, **params))[1])


class MCPWireTests(unittest.TestCase):
    """Real loopback HTTP + TLS callback. No AirSprint requests or account data."""
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        folder = Path(cls.temp.name)
        cls.cert, cls.key = folder / "cert.pem", folder / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost", "-keyout", str(cls.key), "-out", str(cls.cert)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(patch.stopall)
        patch("ssl.SSLContext", _NATIVE_SSL_CONTEXT).start()
        self.store = EventStore(Path(self.folder.name) / "state.sqlite3", "wire-owner")
        self.store.observe("notifications", [], created="notification.created")
        self.store.observe("trips", [], created="trip.created")
        self.received = []
        received = self.received

        class Callback(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((body, dict(self.headers)))
                event = json.loads(body)
                data = json.dumps({"challenge": event["challenge"]} if event.get("type") == "verification" else {"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.callback = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self.cert, self.key)
        self.callback.socket = tls.wrap_socket(self.callback.socket, server_side=True)
        self.callback_thread = threading.Thread(target=self.callback.serve_forever, daemon=True)
        self.callback_thread.start()
        self.service = MCPService(cli, self.store, callback_sender=https_post)
        self.server = make_http_server(self.service, "127.0.0.1", 0, BEARER)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.stop_servers)
        # Deliberately allow this test's loopback callback only. Separate tests
        # exercise the production SSRF checks with private/mixed/public DNS.
        self.url = f"https://localhost:{self.callback.server_port}/events"
        def destination(url):
            self.assertEqual(url, self.url)
            return urlsplit(url), (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", self.callback.server_port))
        trust = ssl.create_default_context(cafile=str(self.cert))
        self.addCleanup(patch.stopall)
        patch("airsprint_events.callback_destination", side_effect=destination).start()
        patch("airsprint_events.ssl.create_default_context", return_value=trust).start()

    def stop_servers(self):
        self.server.shutdown()
        self.callback.shutdown()
        self.server.server_close()
        self.callback.server_close()
        self.server_thread.join(2)
        self.callback_thread.join(2)

    def post(self, req, **header_changes):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + BEARER,
                   "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": req["method"],
                   "Accept": "application/json, text/event-stream"}
        if req["method"] == "tools/call":
            headers["Mcp-Name"] = req["params"]["name"]
        headers.update(header_changes)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("POST", "/mcp", json.dumps(req), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_actual_http_subscription_tls_verification_delivery_restart_unsubscribe(self):
        self.assertEqual(self.post(request("server/discover"))[0], 200)
        self.assertEqual(self.post(request("events/list"))[0], 200)
        delivery = {"mode": "webhook", "url": self.url, "secret": SECRET}
        code, subscribed = self.post(request("events/subscribe", name="notification.created", arguments={"resourceId": "n"}, delivery=delivery))
        self.assertEqual(code, 200, subscribed)
        self.assertEqual(json.loads(self.received[0][0])["type"], "verification")
        self.assertIn("webhook-signature", {k.lower(): v for k, v in self.received[0][1].items()})
        self.store.observe("notifications", [], created="notification.created")
        self.store.observe("notifications", [{"id": "n", "text": "Test event"}, {"id": "filtered"}], created="notification.created")
        reopened = EventStore(self.store.path, "wire-owner")
        self.assertEqual(reopened.dispatch(sender=https_post)["delivered"], 1)
        delivered = json.loads(self.received[-1][0])
        self.assertEqual(delivered["data"]["resourceId"], "n")
        self.assertNotIn("Test event", self.received[-1][0].decode())
        self.assertEqual(len(self.received), 2)
        stop_delivery = {"mode": "webhook", "url": self.url}
        self.assertEqual(self.post(request("events/unsubscribe", name="notification.created", arguments={"resourceId": "n"}, delivery=stop_delivery))[0], 200)
        self.assertEqual(self.store.status()["activeSubscriptions"], 0)

    def test_auth_origin_host_and_header_enforcement_over_http(self):
        self.assertEqual(self.post(request("tools/list"), Authorization="Bearer wrong")[0], 401)
        self.assertEqual(self.post(request("tools/list"), Origin="https://untrusted.example")[0], 403)
        self.assertEqual(self.post(request("tools/list"), Host="untrusted.example")[0], 403)
        code, response = self.post(request("tools/list"), **{"Mcp-Method": "tools/call"})
        self.assertEqual(code, 400)
        self.assertEqual(response["error"]["code"], -32020)


if __name__ == "__main__":
    unittest.main()
