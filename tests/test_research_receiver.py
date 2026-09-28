"""Real handler and digest reader, synthetic bodies, no sockets/DB/secrets/send."""
from datetime import datetime, timezone
import hashlib
import hmac
import importlib.util
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ResearchReceiverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="receiver-v2-offline-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.guards = []
        for obj, attr in ((socket, "socket"), (urllib.request, "urlopen"), (sqlite3, "connect")):
            guard = patch.object(obj, attr, side_effect=AssertionError("live IO forbidden"))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        self.server = load("http_research_test", PROJECT / "app/server.py")
        self.content = load("content_research_test", PROJECT / "app/digest_content.py")
        sample = load("sample_research_test", PROJECT / "tests/test_sealed_research.py")
        self.now = datetime(2026, 9, 16, 8, 15, tzinfo=self.content.TZ)
        self.payload = sample.payload(self.now)
        self.raw = json.dumps(self.payload, ensure_ascii=False, indent=1).encode("utf-8")
        self.day = "2026-09-16"
        at = self.now.replace(hour=8, minute=3).astimezone(timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return at if tz is None else at.astimezone(tz)
        clock = patch.object(self.server.research_receipts, "datetime", Clock)
        clock.start(); self.addCleanup(clock.stop)
        # Public, reproducible test material; never read a production key. A
        # full-length derived value retains realistic HMAC signing behavior.
        self.secret = hashlib.sha256(b"research receiver offline HMAC fixture v1").hexdigest()
        for module, attr, value in ((self.server, "BASE", self.base),
                                    (self.server, "enqueue_review_notify", Mock()),
                                    (self.server, "load_secrets", Mock(side_effect=AssertionError("secrets forbidden")))):
            p = patch.object(module, attr, value); p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()
        self.server.enqueue_review_notify.assert_not_called()
        self.server.load_secrets.assert_not_called()

    def tree(self):
        return {str(p.relative_to(self.base)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.base.rglob("*") if p.is_file()}

    def request(self, method="POST", *, raw=None, path=None, headers=None,
                signing_method=None, signing_path=None, signing_secret=None):
        raw = self.raw if raw is None else raw
        path = path or ("/hooks/v1/digest-drop" if method == "POST" else f"/records/nng8/{self.day}")
        stamp = str(int(time.time()))
        hdr = {"X-Source": "nng8", "X-Timestamp": stamp,
               "Content-Type": "application/json", "Content-Length": str(len(raw))}
        signed = (stamp + "." + (signing_method or method) + "." + (signing_path or path)).encode()
        if method == "POST":
            signed = stamp.encode() + b"." + raw
        key = self.secret if signing_secret is None else signing_secret
        hdr["X-Signature"] = "sha256=" + hmac.new(key.encode(), signed, hashlib.sha256).hexdigest()
        hdr.update(headers or {})
        handler = object.__new__(self.server.Handler)
        handler.path, handler.headers, handler.rfile = path, hdr, io.BytesIO(raw)
        handler.secrets = {"HMAC_SECRET_NNG8": self.secret}
        handler._send = Mock()
        getattr(handler, "do_" + method)()
        handler._send.assert_called_once()
        return handler._send.call_args.args

    def receive(self):
        at = self.now.replace(hour=8, minute=3).astimezone(timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return at if tz is None else at.astimezone(tz)
        with patch.object(self.server.research_receipts, "datetime", Clock):
            code, ack = self.request()
        self.assertEqual(code, 200)
        return ack

    def test_http_store_query_and_real_consumer_share_exact_record(self):
        folder = self.base / "inbox/nng8"
        folder.mkdir(parents=True)
        legacy = folder / "digest-latest.json"
        legacy.write_text('{"text":"OLD_ALIAS_MUST_NOT_BE_USED"}')
        old = (legacy.read_bytes(), legacy.stat().st_mtime_ns)
        ack = self.receive()
        self.assertIs(ack["stored"], True)
        self.assertEqual(ack["day"], "2026-09-15")
        self.assertEqual(ack["target_date"], self.day)
        self.assertEqual(ack["content_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertNotIn("path", ack)
        before = self.tree()
        self.assertEqual(self.request("GET"), (200, ack))
        renderer = self.content.Renderer(self.base, self.base / "unused.db", self.now)
        text = "\n".join(renderer.highlights("nng8"))
        self.assertIn("合成研究结论", text)
        self.assertNotIn("OLD_ALIAS_MUST_NOT_BE_USED", text)
        self.assertEqual(renderer.issues, [])
        self.assertEqual(renderer.sources["nng8"]["content_sha256"], ack["content_sha256"])
        self.assertEqual(self.tree(), before)
        self.assertEqual(old, (legacy.read_bytes(), legacy.stat().st_mtime_ns))

    def test_duplicate_returns_original_and_conflict_does_not_overwrite(self):
        ack = self.receive()
        before = self.tree()
        self.assertEqual(self.request(), (200, ack))
        changed = self.raw + b"\n"
        self.assertEqual(self.request(raw=changed), (409, {"status": "rejected", "reason": "content_conflict"}))
        self.assertEqual(self.tree(), before)

    def test_get_authentication_precedes_any_record_read(self):
        self.receive()
        before = self.tree()
        cases = [{"X-Signature": ""}, {"X-Signature": "sha256=" + "0" * 64}, {"X-Signature": "坏签名"},
                 {"X-Source": "jd"}, {"X-Timestamp": ""},
                 {"X-Timestamp": str(int(time.time()) - 301)},
                 {"X-Timestamp": str(int(time.time()) + 301)}]
        for headers in cases:
            with self.subTest(headers=list(headers)), patch.object(self.server.research_receipts, "read_record") as read:
                self.assertEqual(self.request("GET", headers=headers)[0], 401)
                read.assert_not_called()
        for opts in ({"signing_method": "POST"}, {"signing_path": "/records/nng8/2026-09-15"}):
            with patch.object(self.server.research_receipts, "read_record") as read:
                self.assertEqual(self.request("GET", **opts)[0], 401)
                read.assert_not_called()
        self.assertEqual(before, self.tree())

    def test_authenticated_get_missing_is_read_only_and_cannot_import_legacy(self):
        folder = self.base / "inbox/nng8"
        folder.mkdir(parents=True)
        (folder / "digest-latest.json").write_bytes(self.raw)
        (folder / "digest-2026-09-15.json").write_bytes(self.raw)
        before = self.tree()
        self.assertEqual(self.request("GET"), (404, {"status": "not_found"}))
        self.assertEqual(before, self.tree())
        self.assertFalse((folder / "research-receipts").exists())

    def test_bad_post_auth_never_writes_or_notifies(self):
        for headers in ({"X-Signature": ""}, {"X-Timestamp": "1"}, {"X-Source": "unknown"}):
            self.assertEqual(self.request(headers=headers)[0], 401)
        self.assertEqual(self.tree(), {})

    def test_different_synthetic_hmac_key_is_refused_before_storage_access(self):
        other_key = hashlib.sha256(b"research receiver wrong offline HMAC fixture v1").hexdigest()
        self.assertEqual(len(self.secret), 64)
        self.assertNotEqual(other_key, self.secret)
        for method in ("POST", "GET"):
            with self.subTest(method=method), \
                 patch.object(self.server.research_receipts, "capture") as capture, \
                 patch.object(self.server.research_receipts, "read_record") as read:
                self.assertEqual(self.request(method, signing_secret=other_key)[0], 401)
                capture.assert_not_called()
                read.assert_not_called()
        self.assertEqual(self.tree(), {})

    def test_storage_error_never_returns_success_or_internal_detail(self):
        with patch.object(self.server.research_receipts, "capture", side_effect=OSError("SECRET internal path")):
            code, value = self.request()
        self.assertEqual((code, value), (500, {"error": "storage_unavailable"}))
        self.assertEqual(self.tree(), {})

    def test_contract_contradictions_are_rejected_before_store(self):
        for field, value in (("source", "jd"), ("target_date", "2026-09-17"), ("as_of", "2026-09-14")):
            data = json.loads(self.raw); data[field] = value
            with self.subTest(field=field):
                self.assertEqual(self.request(raw=json.dumps(data).encode())[0], 422)
        self.assertEqual(self.tree(), {})

    def test_corrupt_record_query_and_consumer_fail_closed(self):
        self.receive()
        record = self.base / "inbox/nng8/research-receipts" / (self.day + ".json")
        record.write_bytes(b"{broken")
        (self.base / "inbox/nng8/digest-latest.json").write_bytes(self.raw)
        before = self.tree()
        self.assertEqual(self.request("GET"), (503, {"error": "record_unavailable"}))
        renderer = self.content.Renderer(self.base, self.base / "unused.db", self.now)
        text = "\n".join(renderer.highlights("nng8"))
        self.assertNotIn("合成研究结论", text)
        self.assertIn("nng8:invalid", renderer.issues)
        self.assertEqual(before, self.tree())

    def test_future_reception_cannot_be_used_by_earlier_consumer(self):
        self.receive()
        renderer = self.content.Renderer(self.base, self.base / "unused.db", self.now.replace(hour=8, minute=0))
        text = "\n".join(renderer.highlights("nng8"))
        self.assertNotIn("合成研究结论", text)
        self.assertIn("nng8:invalid", renderer.issues)

    def test_query_noncanonical_path_is_refused_without_read(self):
        for path in (f"/records/nng8/{self.day}?x=1", f"/records/nng8/{self.day}/", "/records/jd/2026-09-16",
                     "/records/nng8/../2026-09-16", "/records/nng8/%32%30%32%36-09-16", "/records/nng8/2026-02-30"):
            with self.subTest(path=path), patch.object(self.server.research_receipts, "read_record") as read:
                self.assertEqual(self.request("GET", path=path)[0], 422)
                read.assert_not_called()

    def test_calendar_skip_uses_the_same_hmac_boundary(self):
        from test_research_contract import skip_payload, digest
        data = skip_payload()
        data["target_date"] = data["as_of"] = self.day
        data["produced_at"] = "2026-09-16T08:00:30+08:00"
        skip = data["research_skip"]
        skip["target_date"] = self.day; skip["validated_at"] = data["produced_at"]
        calendar = skip["calendar"]
        calendar.update(target_date=self.day, previous_trading_date="2026-09-15",
                        next_trading_date="2026-09-17", data_as_of="2026-09-16T07:50:00+08:00")
        skip["proof"]["calendar_sha256"] = digest(calendar)
        encoded = json.dumps(data).encode()
        self.assertEqual(self.request(raw=encoded, signing_secret="wrong-synthetic")[0], 401)
        self.assertEqual(self.tree(), {})
        code, ack = self.request(raw=encoded)
        self.assertEqual(code, 200)
        self.assertEqual(ack["payload_kind"], "skip")
        renderer = self.content.Renderer(self.base, self.base / "unused.db", self.now)
        self.assertIn("非交易日证明", "\n".join(renderer.highlights("nng8")))
        self.assertEqual(renderer.issues, [])


if __name__ == "__main__":
    unittest.main()
