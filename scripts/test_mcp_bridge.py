import io
import json
import unittest
from urllib.error import URLError
from airsprint_mcp_bridge import Bridge, serve, PROTOCOL


class Opener:
    def __init__(self):
        self.requests = []
        self.failure = False

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.failure:
            raise URLError('private credential must never appear')
        body = json.loads(request.data)
        result = {'resultType': 'complete', 'cacheScope': 'private', 'ttlMs': 1}
        if body['method'] == 'server/discover':
            result['instructions'] = 'safe'
        elif body['method'] == 'tools/list':
            result['tools'] = [{'name': 'airsprint_commands'}]
        else:
            result.update(content=[{'type': 'text', 'text': '{}'}], structuredContent={}, isError=False)
        return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}).encode())


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.opener = Opener()
        self.bridge = Bridge('https://private.example/mcp', 'x'*48, opener=self.opener)

    def request(self, method, params=None):
        return self.bridge.handle({'jsonrpc': '2.0', 'id': 3, 'method': method, 'params': params or {}})

    def initialize(self):
        return self.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})

    def test_legacy_negotiation_and_wire_headers(self):
        self.assertEqual(self.initialize()['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(self.request('tools/list')['result'], {'tools': [{'name': 'airsprint_commands'}]})
        result = self.request('tools/call', {'name': 'airsprint_commands', 'arguments': {}})
        self.assertIn('structuredContent', result['result'])
        self.assertNotIn('resultType', result['result'])
        request, timeout = self.opener.requests[-1]
        body = json.loads(request.data)
        self.assertEqual(body['params']['_meta']['io.modelcontextprotocol/protocolVersion'], PROTOCOL)
        self.assertEqual(request.get_header('Mcp-name'), 'airsprint_commands')
        self.assertEqual(request.get_header('Mcp-method'), 'tools/call')
        self.assertEqual(timeout, 120)

    def test_transport_error_never_retries_or_leaks(self):
        self.initialize()
        self.opener.failure = True
        result = self.request('tools/call', {'name': 'airsprint_run', 'arguments': {'operation_key': 'keep'}})
        self.assertEqual(len(self.opener.requests), 2)
        self.assertIn('same operation_key', result['error']['message'])
        self.assertNotIn('credential', json.dumps(result))

    def test_notifications_do_not_reach_remote(self):
        self.assertIsNone(self.bridge.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        self.assertEqual(len(self.opener.requests), 0)
        self.assertEqual(self.request('tools/list')['error']['code'], -32002)

    def test_bad_input_resynchronizes_stdio(self):
        source = io.StringIO('x'*300000+'\n'+json.dumps({'jsonrpc': '2.0', 'id': 4, 'method': 'ping'})+'\n')
        sink = io.StringIO()
        serve(self.bridge, source, sink)
        lines = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]['error']['code'], -32700)
        self.assertEqual(lines[1]['id'], 4)

    def test_mcp2_discovery_remains_available(self):
        self.assertEqual(self.request('server/discover')['result']['resultType'], 'complete')

    def test_remote_requires_https(self):
        with self.assertRaises(ValueError):
            Bridge('http://private.example/mcp', 'x'*48)


if __name__ == '__main__':
    unittest.main()
