import base64
import hashlib
import hmac
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import airsprint_cli as cli
from airsprint_events import EventStore, callback_destination, collect, collection, decode_secret, signed_headers


SECRET = "whsec_" + base64.b64encode(b"test-signing-secret-32-bytes-long!").decode()
DELIVERY = {"mode": "webhook", "url": "https://callback.example/events", "secret": SECRET}
PUBLIC_ADDRESS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]


def verify_callback(_url, body, _headers):
    return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode()


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = EventStore(Path(self.temp.name) / "private" / "state.sqlite3", "owner-a")
        self.addCleanup(patch.stopall)
        patch("socket.create_connection", side_effect=AssertionError("No external network in event tests")).start()
        patch("socket.getaddrinfo", return_value=PUBLIC_ADDRESS).start()

    def subscribe(self, name="notification.created", arguments=None, store=None, **kwargs):
        return (store or self.store).subscribe(name, arguments or {}, DELIVERY, sender=verify_callback, **kwargs)

    def test_initial_baseline_silent_changes_deduplicate_across_restart(self):
        self.subscribe()
        first = [{"id": "n1", "text": "first"}]
        self.assertEqual(self.store.observe("notifications", first, created="notification.created", updated="notification.updated"), 0)
        updated = first + [{"id": "n2", "text": "second"}]
        self.assertEqual(self.store.observe("notifications", updated, created="notification.created", updated="notification.updated"), 1)
        reopened = EventStore(self.store.path, "owner-a")
        self.assertEqual(reopened.observe("notifications", updated, created="notification.created", updated="notification.updated"), 0)
        self.assertEqual(len(reopened.list_events()["items"]), 1)
        self.assertEqual(reopened.status()["pendingDeliveries"], 1)

    def test_repeated_state_changes_are_distinct_and_absence_is_not_cancellation(self):
        self.store.observe("notifications", [{"id": "n", "isRead": False}], created="notification.created", updated="notification.updated")
        for value in (True, False, True):
            self.assertEqual(self.store.observe("notifications", [{"id": "n", "isRead": value}], created="notification.created", updated="notification.updated"), 1)
        self.store.observe("notifications", [], created="notification.created", updated="notification.updated")
        events = self.store.list_events()["items"]
        self.assertEqual(len(events), 3)
        self.assertEqual(len({event["eventId"] for event in events}), 3)

    def test_filters_owner_isolation_and_private_file_modes(self):
        self.subscribe(arguments={"resourceId": "wanted"})
        other = EventStore(self.store.path, "owner-b")
        self.subscribe(store=other)
        self.store.observe("notifications", [], created="notification.created")
        self.store.observe("notifications", [{"id": "wanted"}, {"id": "unwanted"}], created="notification.created")
        self.assertEqual(self.store.status()["pendingDeliveries"], 1)
        self.assertEqual(other.status()["pendingDeliveries"], 0)
        self.assertEqual(other.list_events()["items"], [])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.parent.stat().st_mode & 0o777, 0o700)

    def test_subscribe_is_idempotent_refreshes_expiry_and_caches_verification(self):
        calls = []
        def sender(*args):
            calls.append(args)
            return verify_callback(*args)
        first = self.store.subscribe("trip.updated", {"legId": "leg", "bookingId": "BOOK"}, DELIVERY, 10000, sender=sender)
        again = self.store.subscribe("trip.updated", {"bookingId": "BOOK", "legId": "leg"}, DELIVERY, 20000, sender=sender)
        self.assertEqual(first["id"], again["id"])
        self.assertLess(first["refreshBefore"], again["refreshBefore"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.status()["activeSubscriptions"], 1)

    def test_signature_and_rotation_use_exact_body_and_both_keys(self):
        sub = self.subscribe()
        new_secret = "whsec_" + base64.b64encode(b"a-different-test-key-32-bytes-long").decode()
        self.store.subscribe("notification.created", {}, {**DELIVERY, "secret": new_secret}, sender=verify_callback)
        self.store.observe("notifications", [], created="notification.created")
        self.store.observe("notifications", [{"id": "n"}], created="notification.created")
        received = []
        self.store.dispatch(sender=lambda *args: (received.append(args) or (200, b"")))
        _, body, headers = received[0]
        event = json.loads(body)
        self.assertEqual(headers["X-MCP-Subscription-Id"], sub["id"])
        self.assertEqual(headers["webhook-id"], event["eventId"])
        signed = headers["webhook-id"].encode() + b"." + headers["webhook-timestamp"].encode() + b"." + body
        for secret in (SECRET, new_secret):
            sig = "v1," + base64.b64encode(hmac.new(decode_secret(secret), signed, hashlib.sha256).digest()).decode()
            self.assertIn(sig, headers["webhook-signature"].split())

    def test_failed_challenge_creates_no_subscription_or_delivery(self):
        with self.assertRaisesRegex(ValueError, "verification failed"):
            self.store.subscribe("notification.created", {}, DELIVERY, sender=lambda *_: (200, b'{"challenge":"wrong"}'))
        self.assertEqual(self.store.status()["activeSubscriptions"], 0)

    def test_transient_delivery_keeps_event_id_retries_then_stops(self):
        self.subscribe()
        self.store.observe("notifications", [], created="notification.created")
        self.store.observe("notifications", [{"id": "n"}], created="notification.created")
        ids = []
        def sender(_url, body, headers):
            ids.append((json.loads(body)["eventId"], headers["webhook-id"]))
            return 503, b""
        for _ in range(8):
            self.store.dispatch(sender=sender)
            with self.store.db() as db:
                db.execute("UPDATE deliveries SET next_at=0")
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.store.dispatch(sender=sender)["deferred"], 0)

    def test_no_retry_for_410_or_413(self):
        for status in (410, 413):
            with self.subTest(status=status):
                store = EventStore(Path(self.temp.name) / f"{status}.sqlite3", "owner")
                self.subscribe(store=store)
                store.observe("notifications", [], created="notification.created")
                store.observe("notifications", [{"id": "n"}], created="notification.created")
                self.assertEqual(store.dispatch(sender=lambda *_: (status, b""))["stopped"], 1)
                self.assertEqual(store.dispatch(sender=lambda *_: self.fail("Must not retry"))["stopped"], 0)

    def test_unsubscribe_and_expiration_stop_pending_delivery(self):
        self.subscribe()
        self.store.observe("notifications", [], created="notification.created")
        self.store.observe("notifications", [{"id": "n"}], created="notification.created")
        self.store.unsubscribe("notification.created", {}, {"mode": "webhook", "url": DELIVERY["url"]})
        self.store.unsubscribe("notification.created", {}, {"mode": "webhook", "url": DELIVERY["url"]})
        self.store.dispatch(sender=lambda *_: self.fail("Unsubscribed"))
        self.subscribe()
        self.store.observe("notifications", [{"id": "n2"}], created="notification.created")
        with self.store.db() as db:
            db.execute("UPDATE subscriptions SET expires=?", (time.time() - 1,))
        self.store.dispatch(sender=lambda *_: self.fail("Expired"))

    def test_revocation_stops_pending_data(self):
        self.subscribe()
        self.store.observe("notifications", [], created="notification.created")
        self.store.observe("notifications", [{"id": "n"}], created="notification.created")
        self.store.revoke()
        self.store.dispatch(sender=lambda *_: self.fail("Revoked"))
        self.assertEqual(self.store.status()["activeSubscriptions"], 0)

    def test_callback_rejects_private_mixed_dns_userinfo_and_wrong_scheme(self):
        for url in ["http://public.example/cb", "https://user:pass@public.example/cb", "https://public.example/#secret"]:
            with self.assertRaises(ValueError):
                callback_destination(url)
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "::1", "::ffff:127.0.0.1"]:
            with patch("socket.getaddrinfo", return_value=PUBLIC_ADDRESS + [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]):
                with self.assertRaises(ValueError):
                    callback_destination("https://public.example/cb")

    def test_invalid_filters_and_secrets(self):
        for secret in ["plain", "whsec_!", "whsec_" + base64.b64encode(b"short").decode()]:
            with self.assertRaises(ValueError):
                decode_secret(secret)
        for name, arguments in [("missing", {}), ("trip.updated", {"arbitrary": "x"}), ("customs.submitted", {}), ("trip.updated", {"legId": ""})]:
            with self.assertRaises(ValueError):
                self.subscribe(name, arguments)

    def test_known_owner_leg_required_for_customs_subscription(self):
        with self.assertRaisesRegex(ValueError, "connected owner's"):
            self.subscribe("customs.submitted", {"legId": "leg"})
        self.store.observe("trips", [{"id": "leg"}], created="trip.created")
        self.subscribe("customs.submitted", {"legId": "leg"})

    def test_sensitive_documents_and_cards_not_stored_or_delivered(self):
        self.store.observe("trips", [{"id": "leg", "passport": {"passportNumber": "SECRET"}, "cvc": "SECRET", "url": "SECRET", "flight": {"destination": "YQB"}}], created="trip.created")
        record = self.store.record("trips", "leg")
        self.assertNotIn("SECRET", json.dumps(record))
        self.assertTrue(record["untrustedContent"])

    def test_complete_collection_pagination_and_incomplete_fail_closed(self):
        page = [{"id": str(n)} for n in range(100)]
        with patch.object(cli, "api_post", side_effect=[{"data": {"items": page, "total": 101}}, {"data": {"items": [{"id": "100"}], "total": 101}}]) as post:
            self.assertEqual(len(collection(cli, "test", "/my-leg", {})), 101)
            self.assertEqual(post.call_args.args[2]["page"]["offset"], 100)
        with patch.object(cli, "api_post", return_value={"data": {"items": [{"id": "1"}], "total": 2}}):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                collection(cli, "test", "/my-leg", {})
        with patch.object(cli, "api_post", return_value={"data": {"items": page, "total": 201}}):
            with self.assertRaisesRegex(ValueError, "repeated"):
                collection(cli, "test", "/my-leg", {})

    def test_collector_never_reads_detail_routes_or_writes_airsprint(self):
        with patch.object(cli, "get_api_token", return_value="test"), patch.object(cli, "_get_accounts", return_value=[{"id": "a"}]), patch.object(cli, "api_post", return_value={"data": {"items": [], "total": 0}}) as post, patch.object(cli, "api_get", side_effect=AssertionError("No detail GET")):
            result = collect(cli, self.store)
        self.assertEqual(result["newEvents"], 0)
        self.assertEqual([call.args[1] for call in post.call_args_list], ["/my-notifications", "/my-leg"])

    def test_signed_verification_uses_standard_webhooks(self):
        body = b'{"type":"verification","challenge":"random"}'
        headers = signed_headers(body, "msg_test", "sub_test", SECRET)
        self.assertEqual(headers["webhook-id"], "msg_test")
        self.assertTrue(headers["webhook-signature"].startswith("v1,"))


if __name__ == "__main__":
    unittest.main()
