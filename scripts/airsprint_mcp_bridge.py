#!/usr/bin/env python3
"""Private stdio bridge for MCP 1.x clients of the AirSprint MCP 2.0 service.

Only transport/protocol adaptation happens here. All AirSprint operations,
credentials, journals and validation stay on the remote service. No retries.
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

PROTOCOL = "2026-07-28"
LEGACY = "2025-06-18"
MAX_LINE = 256 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Bridge:
    def __init__(self, url: str, token: str, *, connect_ip=None, opener=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("The bridge requires a private HTTPS endpoint without embedded credentials.")
        if not isinstance(token, str) or len(token) < 32 or not token.isascii():
            raise ValueError("A private bearer token of at least 32 ASCII characters is required.")
        self.url, self.token = url, token
        handlers = [NoRedirect(), ProxyHandler({})]
        if connect_ip is not None:
            address = str(ipaddress.ip_address(connect_ip))

            class PinnedConnection(http.client.HTTPSConnection):
                def connect(self):
                    self._create_connection = lambda pair, timeout, source: socket.create_connection((address, pair[1]), timeout, source)
                    super().connect()  # TLS still validates the URL hostname and uses it for SNI.

            class PinnedHandler(HTTPSHandler):
                def https_open(self, req):
                    return self.do_open(PinnedConnection, req)

            handlers.append(PinnedHandler())
        self.opener = opener or build_opener(*handlers)
        self.initialized = False

    def remote(self, request):
        params = dict(request.get("params", {}))
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": PROTOCOL,
                           "io.modelcontextprotocol/clientCapabilities": {}}
        body = {**request, "params": params}
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL,
                   "Mcp-Method": request["method"]}
        if request["method"] == "tools/call":
            headers["Mcp-Name"] = params.get("name", "")
        req = Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            response = self.opener.open(req, timeout=120)
        except HTTPError as exc:
            response = exc  # Preserve a valid protocol error; never repeat the request.
        with response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Oversized server response")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != request["id"] or not (isinstance(result.get("result"), dict) or isinstance(result.get("error"), dict)):
            raise ValueError("Invalid server response")
        return result

    def handle(self, request):
        request_id = request.get("id") if isinstance(request, dict) else None

        def error(code, message):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return error(-32600, "Invalid JSON-RPC request.")
        if "id" not in request:
            return None  # Notifications have no response; they never trigger remote calls.
        if type(request_id) not in (str, int) or not isinstance(request.get("params", {}), dict):
            return error(-32600, "Invalid request id or parameters.")
        method = request["method"]
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        legacy = method == "initialize" or self.initialized
        if method == "initialize":
            params = request.get("params", {})
            if not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                return error(-32602, "Initialize requires protocolVersion, capabilities and clientInfo.")
            outbound = {"jsonrpc": "2.0", "id": request_id, "method": "server/discover", "params": {}}
        elif method in {"server/discover", "tools/list", "tools/call", "events/list", "events/subscribe", "events/unsubscribe"}:
            metadata = request.get("params", {}).get("_meta", {})
            if not isinstance(metadata, dict):
                return error(-32602, "Request metadata must be an object.")
            if not self.initialized and method != "server/discover" and metadata.get("io.modelcontextprotocol/protocolVersion") != PROTOCOL:
                return error(-32002, "Initialize this MCP 1.x connection first.")
            outbound = request
        else:
            return error(-32601, "Method not found.")
        try:
            response = self.remote(outbound)
        except (OSError, URLError, ValueError, TypeError):
            return error(-32603, "AirSprint MCP transport failed. A write may have succeeded. Inspect airsprint_operation with the same operation_key; never retry under a new key.")
        if method == "initialize" and "result" in response:
            self.initialized = True
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": LEGACY, "capabilities": {"tools": {}},
                "serverInfo": {"name": "AirSprint Mac Studio", "version": "1.2.0"},
                "instructions": response["result"].get("instructions", "")}}
        if legacy and "result" in response:
            response["result"] = {k: v for k, v in response["result"].items() if k not in {"resultType", "cacheScope", "ttlMs"}}
        return response


def serve(bridge, source=None, sink=None):
    source, sink = source or sys.stdin, sink or sys.stdout
    while line := source.readline(MAX_LINE + 1):
        try:
            if len(line) > MAX_LINE:
                while not line.endswith("\n"):
                    line = source.readline(MAX_LINE + 1)
                    if not line:
                        break
                raise ValueError("Oversized line")
            response = bridge.handle(json.loads(line))
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Expected one JSON request per line, at most 256 KiB."}}
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sink.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Private JSON file with url and token; mode 0600.")
    args = parser.parse_args()
    if args.config.stat().st_mode & 0o077:
        parser.error("Bridge config must be private (chmod 600).")
    config = json.loads(args.config.read_text())
    serve(Bridge(config["url"], config["token"], connect_ip=config.get("connect_ip")))


if __name__ == "__main__":
    main()
