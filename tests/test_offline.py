#!/usr/bin/env python3
"""Offline regression only: synthetic inputs, temporary state, no live IO."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request
from urllib.parse import unquote, urlsplit


PROJECT = Path(__file__).resolve().parents[1]
REAL_CONNECT = sqlite3.connect


def load_source(name: str):
    spec = importlib.util.spec_from_file_location(
        f"signal_gate_offline_{name}", PROJECT / "app" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OfflineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="signal-gate-offline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db = self.root / "data" / "signals.db"
        # Install process-wide guards before importing application modules so
        # future import-time IO cannot bypass the offline boundary.
        self.urlopen = self.patched(
            urllib.request, "urlopen", side_effect=AssertionError("network forbidden")
        )
        self.socket = self.patched(
            socket, "socket", side_effect=AssertionError("network forbidden")
        )
        self.patched(sqlite3, "connect", side_effect=self.temporary_connect)
        with patch.dict(os.environ, {
            "SIGNAL_GATE_HOME": str(self.root), "HOME": str(self.root),
        }, clear=True):
            self.server = load_source("server")
            self.review = load_source("review_bot")
            self.digest = load_source("digest")

        self.server_urlopen = self.patched(
            self.server, "urlopen", side_effect=AssertionError("network forbidden")
        )
        for module in (self.server, self.review, self.digest):
            # Simulated delivery messages must not look like live operations.
            self.patched(module, "log")
        for module, name in (
            (self.server, "load_secrets"), (self.review, "load_env_file"),
            (self.digest, "load_secrets"),
        ):
            self.patched(module, name, side_effect=AssertionError("credentials forbidden"))
        self.notify = self.patched(self.server, "enqueue_review_notify")
        self.tg = self.patched(self.review, "tg_api", return_value={"ok": True})
        self.quant = self.patched(
            self.review, "post_to_quant", return_value=(200, '{"ok":true,"offline":true}')
        )
        # A confirmed Telegram part requires a positive message ID, not truthiness.
        self.digest_send = self.patched(self.digest, "send_telegram", return_value=101)
        self.server.init_db(self.db)
        self.secret = "offline-hmac"
        self.actor = "101"
        self.quant_cfg = {
            "APPROVED_SIGNALS_URL": "https://quant.example.invalid/approved",
            "HMAC_SECRET_DEDIROCK_TO_QUANT": "offline-synthetic-quant-secret",
        }

    def patched(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def tearDown(self):
        # Assert even swallowed connection errors cannot hide an attempted call.
        self.urlopen.assert_not_called()
        self.server_urlopen.assert_not_called()
        self.socket.assert_not_called()

    def temporary_connect(self, database, *args, **kwargs):
        value = os.fspath(database)
        if kwargs.get("uri"):
            parsed = urlsplit(value)
            if parsed.scheme != "file" or parsed.netloc:
                raise AssertionError("non-local database URI forbidden")
            path = Path(unquote(parsed.path)).resolve()
        else:
            path = Path(value).resolve()
        if not path.is_relative_to(self.root):
            raise AssertionError("non-temporary database forbidden")
        return REAL_CONNECT(database, *args, **kwargs)

    def body(self, key="offline-1", expires="2099-01-01T00:00:00Z"):
        return {
            "schema_version": "1", "idempotency_key": key,
            "signal_type": "offline_fixture", "symbol": "000001.SZ",
            "confidence": 0.5, "expires_at": expires,
            "provenance": {"fixture": True}, "review_status": "pending",
        }

    def pending(self, body=None, *, bad_signature=False, timestamp=None):
        raw = json.dumps(self.body() if body is None else body).encode()
        stamp = str(int(time.time()) if timestamp is None else timestamp)
        signature = "sha256=" + hmac.new(
            self.secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        handler = object.__new__(self.server.Handler)
        handler.path = "/hooks/v1/pending-signals"
        handler.headers = {
            "Content-Type": "application/json", "Content-Length": str(len(raw)),
            "X-Source": "jd", "X-Timestamp": stamp,
            "X-Signature": "sha256=" + "0" * 64 if bad_signature else signature,
        }
        handler.rfile = io.BytesIO(raw)
        handler.secrets = {"HMAC_SECRET_JD": self.secret}
        handler.db_path = self.db
        handler._send = Mock()
        handler.do_POST()
        handler._send.assert_called_once()
        return handler._send.call_args.args

    def callback(self, row_id, action="ok", actor="101"):
        return {
            "id": "offline-callback", "data": f"sg:{action}:{row_id}",
            "from": {"id": int(actor), "username": "offline-reviewer"},
            "message": {"message_id": 1, "chat": {"id": int(actor)},
                        "text": "Synthetic offline review"},
        }

    def review_callback(self, callback):
        return self.review.handle_callback(
            "offline-token", callback, self.actor, set(), self.quant_cfg
        )

    def test_hmac_binds_timestamp_and_body(self):
        raw, stamp = b'{"fixture":true}', "1000"
        signature = self.review.sign_body(self.secret, stamp, raw)
        self.assertTrue(self.server.verify_hmac(self.secret, stamp, raw, signature))
        self.assertFalse(self.server.verify_hmac(self.secret, "1001", raw, signature))
        self.assertFalse(self.server.verify_hmac(self.secret, stamp, raw + b" ", signature))

    def test_pending_insert_and_duplicate_enqueue_only_once(self):
        status, result = self.pending()
        self.assertEqual(status, 200)
        self.assertIsInstance(result["id"], int)
        self.assertEqual(self.pending()[0], 409)
        self.notify.assert_called_once()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_signals").fetchone()[0], 1)
        self.assertTrue((self.root / "inbox" / "jd" / "offline-1.json").is_file())

    def test_invalid_signature_stale_timestamp_and_missing_fields_do_not_insert(self):
        self.assertEqual(self.pending(bad_signature=True)[0], 401)
        self.assertEqual(self.pending(timestamp=1)[0], 401)
        self.assertEqual(self.pending({"idempotency_key": "missing-fields"})[0], 422)
        self.notify.assert_not_called()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_signals").fetchone()[0], 0)

    def test_auth_failure_logs_reason_without_secret(self):
        self.assertEqual(self.pending(bad_signature=True)[0], 401)
        self.server.log.warning.assert_called()
        rendered = " ".join(str(part) for part in self.server.log.warning.call_args.args)
        self.assertIn("bad_signature", rendered)
        self.assertIn("jd", rendered)
        self.assertNotIn(self.secret, rendered)

    def test_review_actor_denied_before_business_state_changes(self):
        _, inserted = self.pending()
        result = self.review_callback(self.callback(inserted["id"], actor="202"))
        self.assertEqual(result.get("error"), "not_authorized")
        self.quant.assert_not_called()
        with sqlite3.connect(self.db) as conn:
            self.assertIsNone(self.review.fetch_row(conn, inserted["id"])["decision"])

    def test_approval_posts_only_once_and_duplicate_is_idempotent(self):
        _, inserted = self.pending()
        callback = self.callback(inserted["id"])
        first = self.review_callback(callback)
        self.assertTrue(first["ok"])
        self.assertEqual(first["quant_status"], "ok")
        self.assertTrue(self.review_callback(callback)["already"])
        self.quant.assert_called_once()
        self.assertEqual(self.quant.call_args.args[3]["decision"], "approve")
        self.assertTrue(self.tg.called)

    def test_rejected_and_expired_signals_are_not_posted_to_quant(self):
        _, rejected = self.pending(self.body("offline-reject"))
        result = self.review_callback(self.callback(rejected["id"], action="no"))
        self.assertEqual(result["decision"], "reject")
        _, expired = self.pending(self.body("offline-expired", "2000-01-01T00:00:00Z"))
        result = self.review_callback(self.callback(expired["id"]))
        self.assertEqual(result["quant_status"], "skipped_expired")
        self.quant.assert_not_called()

    def test_forward_secret_required_before_callback_handler(self):
        handler = object.__new__(self.review.ForwardHandler)
        handler.path = "/hooks/v1/telegram-callback"
        handler.headers = {"X-Review-Forward-Secret": "wrong"}
        handler._send = Mock()
        with patch.object(self.review._State, "forward_secret", "offline-forward-secret"), \
             patch.object(self.review, "handle_callback") as callback:
            handler.do_POST()
        callback.assert_not_called()
        self.assertEqual(handler._send.call_args.args[0], 401)

    def test_digest_uses_only_temporary_artifacts_and_mocked_telegram(self):
        self.pending()
        with patch.object(self.digest, "load_secrets", return_value={
            "TELEGRAM_DIGEST_BOT_TOKEN": "1:" + "x" * 24,
            "TELEGRAM_DIGEST_CHAT_ID": self.actor,
        }), redirect_stdout(io.StringIO()):
            self.assertEqual(self.digest.main(), 0)
        reports = list((self.root / "out").glob("daily-*.md"))
        self.assertEqual(len(reports), 1)
        # The readable digest deliberately hides internal idempotency keys.
        self.assertIn("000001.SZ", reports[0].read_text())
        self.assertNotIn("offline-1", reports[0].read_text())
        receipts = list((self.root / "out" / "delivery").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["status"], "confirmed")
        self.assertTrue(receipt["parts"])
        self.assertTrue(all(part["message_id"] == 101 for part in receipt["parts"]))
        self.digest_send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
