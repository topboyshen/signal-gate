#!/usr/bin/env python3
"""Offline delivery fixtures: temporary private state and mocked Telegram only."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parents[1] / "app" / "digest.py"
NOW = datetime(2026, 9, 8, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TOKEN = "1:" + "x" * 24
CHAT_ID = "202"
PRIVATE_BODY = "合成日报 private-body-marker，仅用于离线验证。\n"


def response(status=200, value=None, raw=None):
    result = Mock()
    result.status = status
    result.read.return_value = raw if raw is not None else json.dumps(
        {"ok": True, "result": {"message_id": 11}} if value is None else value
    ).encode()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class DigestDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="digest-delivery-offline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.network = self.patched(urllib.request, "urlopen", side_effect=AssertionError("network forbidden"))
        self.socket = self.patched(socket, "socket", side_effect=AssertionError("network forbidden"))
        content = types.ModuleType("digest_content")
        content.build_report = Mock()
        spec = importlib.util.spec_from_file_location("digest_delivery_fixture", SOURCE)
        self.digest = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"digest_content": content}), patch.dict(os.environ, {
            "SIGNAL_GATE_HOME": str(self.root),
            "SIGNAL_GATE_DB": str(self.root / "synthetic.db"),
            "SIGNAL_GATE_SECRETS": str(self.root / "never-read.env"),
        }, clear=True):
            spec.loader.exec_module(self.digest)
        self.render = self.digest.build_report
        self.render.return_value = {"report_date": NOW.date().isoformat(), "text": PRIVATE_BODY,
                                    "issues": [], "degraded": False}
        self.secrets = self.patched(self.digest, "load_secrets", return_value={
            "TELEGRAM_DIGEST_BOT_TOKEN": TOKEN, "TELEGRAM_DIGEST_CHAT_ID": CHAT_ID,
        })
        self.patched(self.digest, "code_version", return_value="a" * 64)
        self.original_sender = self.digest.send_telegram
        self.sender = self.patched(self.digest, "send_telegram", return_value=11)
        self.sleep = self.patched(self.digest.time, "sleep")
        self.output = io.StringIO()
        self.addCleanup(self.output.close)

    def patched(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def tearDown(self):
        self.socket.assert_not_called()
        self.assertNotIn(TOKEN, self.output.getvalue())
        self.assertNotIn(PRIVATE_BODY, self.output.getvalue())

    def run_digest(self, **kwargs):
        with contextlib.redirect_stdout(self.output):
            return self.digest.run_digest(now=NOW, **kwargs)

    @property
    def receipt_path(self):
        return self.root / "out" / "delivery" / f"{NOW.date().isoformat()}.json"

    def receipt(self):
        return json.loads(self.receipt_path.read_text())

    def enable_transport(self):
        # Exercise the real sender only with its network entry point mocked.
        return self.original_sender

    def test_dry_run_json_does_not_read_secrets_send_or_write_any_state(self):
        self.secrets.side_effect = AssertionError("credentials forbidden")
        self.render.return_value.update(issues=["nng8:stale", "jd:unknown_date"], degraded=True)
        self.assertEqual(self.run_digest(dry_run=True, json_output=True), 0)
        self.secrets.assert_not_called()
        self.sender.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])
        result = json.loads(self.output.getvalue())
        self.assertEqual(result["issues"], ["jd:unknown_date", "nng8:stale"])
        self.assertTrue(result["degraded"])
        self.assertNotIn("text", result)

    def test_split_preserves_all_text_and_prefers_line_boundaries(self):
        text = "一" * 2200 + "\n" + "二" * 5001 + "\n结束\n"
        parts = self.digest.split_text(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < len(part) <= 3500 for part in parts))
        self.assertTrue(parts[0].endswith("\n"))

    def test_split_rejects_more_than_64_parts_instead_of_truncating(self):
        with self.assertRaises(self.digest.DeliveryError):
            self.digest.split_text("一" * 65, limit=1)

    def test_inconsistent_degraded_flag_does_not_discard_issue(self):
        self.render.return_value.update(issues=["nng8:stale"], degraded=False)
        self.assertEqual(self.run_digest(dry_run=True, json_output=True), 1)
        self.secrets.assert_not_called()
        self.sender.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_confirmed_receipt_is_private_and_same_day_never_resends(self):
        self.render.return_value.update(issues=["quant:missing", "review:schema_error"], degraded=True)
        self.assertEqual(self.run_digest(), 0)
        receipt = self.receipt()
        self.assertEqual(receipt["status"], "confirmed")
        self.assertEqual(receipt["issues"], ["quant:missing", "review:schema_error"])
        self.assertEqual(receipt["parts"], [{"index": 1, "status": "confirmed", "message_id": 11}])
        self.assertEqual(stat.S_IMODE(self.receipt_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.receipt_path.parent.stat().st_mode), 0o700)
        self.assertNotIn(TOKEN, self.receipt_path.read_text())
        self.assertNotIn(PRIVATE_BODY, self.receipt_path.read_text())
        self.render.side_effect = AssertionError("confirmed date must not rebuild")
        self.secrets.side_effect = AssertionError("confirmed date must not read credentials")
        self.assertEqual(self.run_digest(), 0)
        self.sender.assert_called_once()

    def test_partial_explicit_rejection_resumes_without_repeating_confirmed_part(self):
        self.render.return_value["text"] = "甲" * 3500 + "乙" * 100
        self.sender.side_effect = [11, self.digest.DeliveryError("request_rejected")]
        self.assertEqual(self.run_digest(), 1)
        self.assertEqual([part["status"] for part in self.receipt()["parts"]], ["confirmed", "failed"])
        self.sender.reset_mock(side_effect=True)
        self.sender.return_value = 12
        self.assertEqual(self.run_digest(), 0)
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[2], "乙" * 100)
        self.assertEqual([part["message_id"] for part in self.receipt()["parts"]], [11, 12])

    def test_changed_content_after_partial_delivery_blocks_mixed_reports(self):
        self.render.return_value["text"] = "甲" * 3500 + "乙"
        self.sender.side_effect = [11, self.digest.DeliveryError("request_rejected")]
        self.assertEqual(self.run_digest(), 1)
        self.render.return_value["text"] += "变化"
        self.sender.reset_mock(side_effect=True)
        self.assertEqual(self.run_digest(), 1)
        self.sender.assert_not_called()
        self.assertEqual(self.receipt()["status"], "blocked")
        self.assertEqual(self.receipt()["last_error_code"], "partial_content_changed")

    def test_unknown_acceptance_never_blindly_resends(self):
        self.sender.side_effect = self.digest.DeliveryError("network_unknown", "unknown")
        self.assertEqual(self.run_digest(), 1)
        self.assertEqual(self.receipt()["status"], "unknown")
        self.sender.reset_mock(side_effect=True)
        self.assertEqual(self.run_digest(), 1)
        self.sender.assert_not_called()

    def test_pending_unknown_crash_marker_blocks_resend(self):
        self.assertEqual(self.run_digest(), 0)
        receipt = self.receipt()
        receipt.pop("confirmed_at")
        receipt.update(status="pending")
        receipt["parts"][0].update(status="unknown", message_id=None)
        self.digest.write_receipt(self.receipt_path, receipt)
        self.sender.reset_mock()
        self.assertEqual(self.run_digest(), 1)
        self.sender.assert_not_called()
        self.assertEqual(self.receipt()["status"], "unknown")

    def test_missing_configuration_is_nonzero_and_not_confirmed(self):
        self.secrets.return_value = {}
        self.assertEqual(self.run_digest(), 1)
        self.sender.assert_not_called()
        self.assertEqual(self.receipt()["status"], "failed")
        self.assertEqual(self.receipt()["last_error_code"], "config_missing")

    def test_unsafe_state_directory_mode_and_owner_block_delivery(self):
        self.receipt_path.parent.mkdir(parents=True, mode=0o755)
        self.receipt_path.parent.chmod(0o755)
        self.assertEqual(self.run_digest(), 1)
        self.receipt_path.parent.chmod(0o700)
        with patch.object(self.digest.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertEqual(self.run_digest(), 1)
        self.sender.assert_not_called()

    def test_duplicate_message_ids_cannot_confirm_multiple_parts(self):
        self.render.return_value["text"] = "甲" * 3500 + "乙"
        self.sender.side_effect = [11, 11]
        self.assertEqual(self.run_digest(), 1)
        self.assertEqual(self.receipt()["status"], "unknown")
        self.assertEqual([p["status"] for p in self.receipt()["parts"]], ["confirmed", "unknown"])

    def test_renderer_exception_is_safe_failure(self):
        self.render.side_effect = RuntimeError("private-body-marker " + TOKEN)
        self.assertEqual(self.run_digest(), 1)
        self.secrets.assert_not_called()
        self.sender.assert_not_called()
        self.assertEqual(self.receipt()["last_error_code"], "render_failed")

    def test_bad_receipt_and_symlink_are_fail_closed(self):
        self.receipt_path.parent.mkdir(parents=True)
        self.receipt_path.write_text('{"private":"malformed-receipt"}')
        self.receipt_path.chmod(0o600)
        self.assertEqual(self.run_digest(), 1)
        self.sender.assert_not_called()
        self.receipt_path.unlink()
        self.receipt_path.symlink_to(self.root / "missing.json")
        self.assertEqual(self.run_digest(), 1)
        self.sender.assert_not_called()

    def test_concurrent_daily_lock_does_not_send(self):
        self.receipt_path.parent.mkdir(parents=True)
        lock = self.receipt_path.parent / f".{NOW.date().isoformat()}.lock"
        with lock.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_digest(), 1)
        self.sender.assert_not_called()
        self.render.assert_not_called()

    def test_write_failure_after_acceptance_leaves_unknown_disk_marker(self):
        original = self.digest.write_receipt
        count = 0
        def writer(path, receipt):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("private state error " + TOKEN)
            return original(path, receipt)
        self.patched(self.digest, "write_receipt", side_effect=writer)
        self.assertEqual(self.run_digest(), 1)
        self.assertEqual(self.receipt()["parts"][0]["status"], "unknown")
        self.sender.reset_mock()
        self.assertEqual(self.run_digest(), 1)
        self.sender.assert_not_called()

    def test_final_receipt_update_crash_recovers_without_rebuild_or_send(self):
        original = self.digest.write_receipt
        count = 0
        def writer(path, receipt):
            nonlocal count
            count += 1
            if count == 4:
                raise OSError("synthetic final-write failure")
            return original(path, receipt)
        self.patched(self.digest, "write_receipt", side_effect=writer)
        self.assertEqual(self.run_digest(), 1)
        self.assertEqual(self.receipt()["status"], "pending")
        self.assertEqual(self.receipt()["parts"][0]["status"], "confirmed")
        self.sender.reset_mock()
        self.render.side_effect = AssertionError("do not rebuild an acknowledged report")
        self.assertEqual(self.run_digest(), 0)
        self.sender.assert_not_called()
        self.assertEqual(self.receipt()["status"], "confirmed")

    def test_transport_requires_json_ok_and_positive_nonboolean_message_id(self):
        send = self.enable_transport()
        self.network.side_effect = None
        for ack in ({"ok": False}, {"ok": True}, {"ok": True, "result": {"message_id": True}},
                    {"ok": True, "result": {"message_id": 0}}, []):
            with self.subTest(ack=ack):
                self.network.return_value = response(value=ack)
                with self.assertRaises(self.digest.DeliveryError) as error:
                    send(TOKEN, CHAT_ID, PRIVATE_BODY)
                self.assertEqual(error.exception.status, "unknown")
        self.network.return_value = response()
        self.assertEqual(send(TOKEN, CHAT_ID, PRIVATE_BODY), 11)

    def test_transport_preserves_full_part_and_limits_timeout(self):
        send = self.enable_transport()
        self.network.side_effect = None
        self.network.return_value = response()
        self.assertEqual(send(TOKEN, CHAT_ID, PRIVATE_BODY), 11)
        request = self.network.call_args.args[0]
        self.assertEqual(json.loads(request.data)["text"], PRIVATE_BODY)
        self.assertLessEqual(self.network.call_args.kwargs["timeout"], 15)

    def test_transport_retries_only_explicit_429_at_most_twice(self):
        send = self.enable_transport()
        rejected = {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
        self.network.side_effect = [response(429, rejected), response(429, rejected), response()]
        self.assertEqual(send(TOKEN, CHAT_ID, PRIVATE_BODY), 11)
        self.assertEqual(self.network.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.network.reset_mock()
        self.network.side_effect = [response(429, rejected)] * 3
        with self.assertRaises(self.digest.DeliveryError) as error:
            send(TOKEN, CHAT_ID, PRIVATE_BODY)
        self.assertEqual((error.exception.code, error.exception.status), ("rate_limited", "failed"))
        self.assertEqual(self.network.call_count, 3)

    def test_http_error_429_is_explicitly_rejected_then_retried(self):
        send = self.enable_transport()
        body = json.dumps({"ok": False, "error_code": 429,
                           "parameters": {"retry_after": 0}}).encode()
        error = urllib.error.HTTPError("https://example.invalid", 429, "synthetic", {}, io.BytesIO(body))
        self.network.side_effect = [error, response()]
        self.assertEqual(send(TOKEN, CHAT_ID, PRIVATE_BODY), 11)
        self.assertEqual(self.network.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_large_retry_after_403_5xx_timeout_and_malformed_ack_do_not_retry(self):
        send = self.enable_transport()
        cases = [
            (response(429, {"ok": False, "parameters": {"retry_after": 99999}}), "failed"),
            (response(403, {"ok": False}), "failed"),
            (response(503, {"ok": False}), "unknown"),
            (response(raw=b"malformed private-body-marker"), "unknown"),
            (TimeoutError("private timeout " + TOKEN), "unknown"),
        ]
        for outcome, status in cases:
            with self.subTest(status=status):
                self.network.reset_mock()
                self.network.side_effect = [outcome]
                with self.assertRaises(self.digest.DeliveryError) as error:
                    send(TOKEN, CHAT_ID, PRIVATE_BODY)
                self.assertEqual(error.exception.status, status)
                self.assertNotIn(TOKEN, str(error.exception))
                self.assertEqual(self.network.call_count, 1)

    def test_delivery_budget_expired_does_not_attempt_network(self):
        send = self.enable_transport()
        with self.assertRaises(self.digest.DeliveryError) as error:
            send(TOKEN, CHAT_ID, PRIVATE_BODY, deadline=0)
        self.assertEqual(error.exception.code, "delivery_budget_exceeded")
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
