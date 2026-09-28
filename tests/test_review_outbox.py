"""No live IO: real SQLite transactions, competing workers and crash points."""
import concurrent.futures
import json
import multiprocessing
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

import test_offline as offline


def approve_process(database):
    """Spawned process; its only side effect is this test's SQLite fixture."""
    review = offline.load_source("review_bot")
    def fake_post(*args, **kwargs):
        with sqlite3.connect(database) as conn:
            conn.execute("INSERT INTO fixture_posts DEFAULT VALUES")
        return 200, "synthetic accepted"
    review.post_to_quant = fake_post
    with sqlite3.connect(database) as conn:
        return review.apply_decision(conn, 1, "approve", "fixture",
            {"APPROVED_SIGNALS_URL": "https://never-called.invalid", "HMAC_SECRET_DEDIROCK_TO_QUANT": "synthetic"})


class ReviewOutboxTests(unittest.TestCase):
    setUp = offline.OfflineTests.setUp
    tearDown = offline.OfflineTests.tearDown
    patched = offline.OfflineTests.patched
    temporary_connect = offline.OfflineTests.temporary_connect
    body = offline.OfflineTests.body
    pending = offline.OfflineTests.pending

    def job(self, kind="quant", row_id=1):
        with sqlite3.connect(self.db) as conn:
            return self.review.review_outbox.get(conn, kind, row_id)

    def approve(self, row_id=1, cfg=None):
        with sqlite3.connect(self.db) as conn:
            return self.review.apply_decision(conn, row_id, "approve", "fixture", self.quant_cfg if cfg is None else cfg)

    def test_invalid_expiry_rejected_without_intake_or_notify(self):
        for expiry in (None, "", "not-a-date", "2099-01-01", "2099-01-01T00:00:00", 12, True):
            with self.subTest(expiry=expiry):
                self.assertEqual(self.pending(self.body(expires=expiry))[0], 422)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_signals").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_outbox").fetchone()[0], 0)
        self.notify.assert_not_called()

    def test_legacy_unknown_expiry_fails_closed_at_approval(self):
        self.pending()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE pending_signals SET expires_at=NULL WHERE id=1")
        result = self.approve()
        self.assertEqual(result["quant_status"], "skipped_expired")
        self.quant.assert_not_called()
        self.assertIsNone(self.job())

    def test_intake_and_notification_are_one_transaction(self):
        with patch.object(self.server.review_outbox, "enqueue", side_effect=sqlite3.OperationalError("fake crash before commit")):
            self.assertEqual(self.pending()[0], 500)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_signals").fetchone()[0], 0)

    def test_migration_does_not_backfill_historical_rows(self):
        self.pending()
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM review_outbox")  # synthetic pre-migration DB
            conn.execute("UPDATE pending_signals SET decision='approve',review_status='reviewed',quant_status=NULL")
        self.server.init_db(self.db)
        self.review.ensure_db(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_signals").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_outbox").fetchone()[0], 0)

    def test_decision_rolls_back_if_intent_cannot_be_saved(self):
        self.pending()
        with patch.object(self.review.review_outbox, "enqueue", side_effect=sqlite3.OperationalError("fake crash")):
            with self.assertRaises(sqlite3.OperationalError):
                self.approve()
        with sqlite3.connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT decision FROM pending_signals").fetchone()[0])
        self.quant.assert_not_called()

    def test_crash_after_approval_commit_before_claim_is_recoverable(self):
        self.pending()
        with patch.object(self.review, "deliver_quant", side_effect=SystemExit("crash before claim")):
            with self.assertRaises(SystemExit):
                self.approve()
        self.assertEqual(self.job()["status"], "pending")
        self.quant.assert_not_called()
        with sqlite3.connect(self.db) as conn:
            self.review.deliver_quant(conn, 1, self.quant_cfg)
        self.assertEqual(self.job()["status"], "confirmed")
        self.quant.assert_called_once()

    def test_missing_quant_config_preserves_durable_intent(self):
        self.pending()
        self.assertEqual(self.approve(cfg={})["quant_status"], "config_missing")
        self.assertEqual(self.job()["status"], "pending")
        self.quant.assert_not_called()

    def test_crash_after_claim_before_or_after_post_never_replays(self):
        self.pending()
        self.quant.side_effect = SystemExit("lost process/ack")
        with self.assertRaises(SystemExit):
            self.approve()
        self.assertEqual(self.job()["status"], "sending")
        with sqlite3.connect(self.db) as conn:
            self.review.review_outbox.recover_sending(conn, "quant")
            self.assertIsNone(self.review.deliver_quant(conn, 1, self.quant_cfg))
        self.assertEqual(self.job()["status"], "unknown")
        self.quant.assert_called_once()
        self.assertTrue(self.approve()["already"])
        self.quant.assert_called_once()

    def test_transport_timeout_and_http_error_are_unknown_not_replayed(self):
        for index, response in enumerate(((0, "timeout"), (500, "server failed"), (409, "conflict")), 1):
            self.pending(self.body(key="failure-" + str(index)))
            self.quant.return_value = response
            self.assertEqual(self.approve(index)["quant_status"], "unknown")
            self.assertEqual(self.job(row_id=index)["status"], "unknown")
            before = self.quant.call_count
            with sqlite3.connect(self.db) as conn:
                self.assertIsNone(self.review.deliver_quant(conn, index, self.quant_cfg))
            self.assertEqual(self.quant.call_count, before)

    def test_two_approval_workers_post_once_after_durable_sending(self):
        self.pending()
        def observed(*args, **kwargs):
            self.assertEqual(self.job()["status"], "sending")
            with sqlite3.connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT decision FROM pending_signals").fetchone()[0], "approve")
            return 200, "accepted"
        self.quant.side_effect = observed
        barrier = threading.Barrier(2)
        def worker():
            barrier.wait()
            return self.approve()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(not r["already"] for r in results), 1)
        self.quant.assert_called_once()
        self.assertEqual(self.job()["status"], "confirmed")

    def test_pending_quant_rechecks_expiry_before_network(self):
        self.pending()
        self.approve(cfg={})
        with sqlite3.connect(self.db) as conn:
            job = self.review.review_outbox.get(conn, "quant", 1)
            data = json.loads(job["payload_json"]); data["expires_at"] = None
            conn.execute("UPDATE review_outbox SET payload_json=? WHERE kind='quant'", (json.dumps(data),))
        with sqlite3.connect(self.db) as conn:
            self.review.deliver_quant(conn, 1, self.quant_cfg)
        self.assertEqual(self.job()["status"], "expired")
        self.quant.assert_not_called()

    def test_two_real_processes_approve_and_post_exactly_once(self):
        self.pending()
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE fixture_posts(id INTEGER PRIMARY KEY)")
        with concurrent.futures.ProcessPoolExecutor(max_workers=2,
                mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(approve_process, [str(self.db)] * 2))
        self.assertEqual(sum(not r["already"] for r in results), 1)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fixture_posts").fetchone()[0], 1)
        self.assertEqual(self.job()["status"], "confirmed")

    def test_ack_received_but_result_commit_crashes_is_unknown_on_restart(self):
        self.pending()
        with patch.object(self.review.review_outbox, "finish", side_effect=SystemExit("crash after HTTP ack")):
            with self.assertRaises(SystemExit):
                self.approve()
        self.quant.assert_called_once()
        self.assertEqual(self.job()["status"], "sending")
        with sqlite3.connect(self.db) as conn:
            self.review.review_outbox.recover_sending(conn, "quant")
            self.assertIsNone(self.review.deliver_quant(conn, 1, self.quant_cfg))
        self.quant.assert_called_once()
        self.assertEqual(self.job()["status"], "unknown")

    def test_notification_recovers_pending_not_lost_with_memory_queue(self):
        self.pending()
        self.assertEqual(self.job("notify")["status"], "pending")
        sender = Mock(return_value={"status": "confirmed", "message_id": 99})
        with patch.object(self.server, "send_review_notify", sender), sqlite3.connect(self.db) as conn:
            self.server.review_outbox.recover_sending(conn, "notify")
            self.server.deliver_review_notify(conn, "synthetic", "101", 1)
        self.assertEqual(self.job("notify")["message_id"], "99")
        self.assertEqual(self.pending()[0], 409)
        with patch.object(self.server, "send_review_notify", sender), sqlite3.connect(self.db) as conn:
            self.server.deliver_review_notify(conn, "synthetic", "101", 1)
        sender.assert_called_once()

    def test_notification_false_or_timeout_is_retained_unknown(self):
        self.pending()
        sender = Mock(return_value=False)
        with patch.object(self.server, "send_review_notify", sender), sqlite3.connect(self.db) as conn:
            self.server.deliver_review_notify(conn, "synthetic", "101", 1)
            self.server.review_outbox.recover_sending(conn, "notify")
            self.server.deliver_review_notify(conn, "synthetic", "101", 1)
        self.assertEqual(self.job("notify")["status"], "unknown")
        sender.assert_called_once()

    def test_notify_missing_config_and_crash_states_are_not_discarded(self):
        self.pending()
        with sqlite3.connect(self.db) as conn:
            self.server.deliver_review_notify(conn, "", "", 1)
        self.assertEqual(self.job("notify")["status"], "pending")
        sender = Mock(side_effect=SystemExit("crash after posting"))
        with patch.object(self.server, "send_review_notify", sender), sqlite3.connect(self.db) as conn:
            with self.assertRaises(SystemExit):
                self.server.deliver_review_notify(conn, "synthetic", "101", 1)
        with sqlite3.connect(self.db) as conn:
            self.server.review_outbox.recover_sending(conn, "notify")
        self.assertEqual(self.job("notify")["status"], "unknown")

    def test_second_worker_start_does_not_resend_active_claim(self):
        self.pending()
        def reply(*args, **kwargs):
            with sqlite3.connect(self.db) as other:
                self.review.review_outbox.recover_sending(other, "quant")
                self.assertIsNone(self.review.deliver_quant(other, 1, self.quant_cfg))
            return 200, "accepted"
        self.quant.side_effect = reply
        self.approve()
        self.assertEqual(self.job()["status"], "confirmed")
        self.quant.assert_called_once()

    def test_two_notify_workers_claim_once(self):
        self.pending()
        barrier = threading.Barrier(2)
        def observed(*args, **kwargs):
            self.assertEqual(self.job("notify")["status"], "sending")
            return {"status": "confirmed", "message_id": 88}
        sender = Mock(side_effect=observed)
        def worker():
            barrier.wait()
            with sqlite3.connect(self.db) as conn:
                self.server.deliver_review_notify(conn, "synthetic", "101", 1)
        with patch.object(self.server, "send_review_notify", sender):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: worker(), range(2)))
        sender.assert_called_once()
        self.assertEqual(self.job("notify")["status"], "confirmed")

    def test_real_notify_sender_requires_valid_message_and_recipient_ack(self):
        row = {"id": 1}
        cases = [[], {"ok": True}, {"ok": True, "result": {"message_id": 8, "chat": {"id": 202}}},
                 {"ok": True, "result": {"message_id": True, "chat": {"id": 101}}},
                 {"ok": False, "result": {"message_id": 8, "chat": {"id": 101}}},
                 {"ok": True, "result": {"message_id": 8, "chat": {"id": 101}}}]
        for index, response in enumerate(cases):
            fake = Mock(status=200)
            fake.read.return_value = json.dumps(response).encode()
            manager = Mock(); manager.__enter__ = Mock(return_value=fake); manager.__exit__ = Mock(return_value=False)
            with patch.object(self.server, "urlopen", return_value=manager):
                result = self.server.send_review_notify("synthetic", "101", row)
            self.assertEqual(result["status"], "confirmed" if index == len(cases) - 1 else "unknown")


if __name__ == "__main__":
    unittest.main()
