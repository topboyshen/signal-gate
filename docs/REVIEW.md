# Signal Gate — human review (Telegram)

Closed-loop review for pending signals on dedirock. **No live trading changes** happen here; Approve only HMAC-POSTs the envelope to Quant’s approved-signals intake.

## Components

| Piece | Path / unit |
|-------|-------------|
| Receiver + notify enqueue | `/opt/signal-gate/app/server.py` → `signal-gate.service` |
| Review handler (HTTP) | `/opt/signal-gate/app/review_bot.py` → `signal-gate-review.service` on `127.0.0.1:8096` |
| Telegram getUpdates owner | **mini-hermes** (same bot token as digest). Forwards `sg:ok:` / `sg:no:` callbacks |
| Hermes forwarder | `/opt/mini-hermes/app/mini_hermes/gateway/telegram/signal_gate_review_forward.py` |
| Secrets | `/opt/signal-gate/secrets.env` — **same** `TELEGRAM_DIGEST_BOT_TOKEN` + `TELEGRAM_DIGEST_CHAT_ID` as digest / mini-hermes |
| Forward auth | `SIGNAL_GATE_REVIEW_FORWARD_SECRET` (also in mini-hermes `.env`) |
| Optional reviewer whitelist | `TELEGRAM_REVIEW_USER_IDS=123,456`. If unset, only the digest chat id is allowed |
| Quant egress | `/opt/signal-gate/quant_approved.env` |

REVIEW uses the **same Telegram bot** as mini-hermes / daily digest. Only one process may `getUpdates`; therefore review runs in **http** mode and mini-hermes forwards matching callbacks.

## Flow

1. JD / nnG8 / Quant POST `POST /hooks/v1/pending-signals` (HMAC + `X-Source`).
2. Row inserted into `pending_signals` (`review_status=pending`).
3. Server enqueues Telegram notify to digest chat: short summary + **Approve** / **Reject** (`callback_data` `sg:ok:<id>` / `sg:no:<id>`).
4. User taps a button → mini-hermes receives update → POSTs callback to `http://127.0.0.1:8096/hooks/v1/telegram-callback`.
5. **Approve**: `decision=approve`, `review_status=reviewed`, `reviewed_at`, `reviewer`; if `expires_at` not past, HMAC POST full envelope + review fields to Quant; store `quant_status` / `quant_response`; edit Telegram message.
6. **Reject**: `decision=reject`, no Quant POST; edit message.
7. Second click is idempotent (“already reviewed”).

## Ops

```bash
systemctl status signal-gate signal-gate-review mini-hermes --no-pager
journalctl -u signal-gate -u signal-gate-review -u mini-hermes -n 80 --no-pager
# look for: notify_sent / reviewed id=… / quant_post / signal_gate_review_forward
curl -sS http://127.0.0.1:8096/healthz
```

Restart cleanly after code changes:

```bash
systemctl restart signal-gate.service
systemctl restart signal-gate-review.service
systemctl restart mini-hermes.service   # picks up sg: forwarder
```

Poll mode (`SIGNAL_GATE_REVIEW_MODE=poll`) exists for a **dedicated** bot token only — do not enable while mini-hermes owns this token (409 Conflict).

## Manual JD integration drill (live side effects)

The following commands are operator-only examples, not routine verification.
Do not run them without separate authorization for production inserts, review
decisions and Telegram notifications. They must not be included in preflight,
CI, timer checks or the offline test suite.

| Script | Actual side effects with its defaults |
|---|---|
| `app/selftest.py` | Reads production credentials; POSTs to port 8095; inserts a real pending row and can trigger Telegram. |
| `app/review_selftest.py` | Does the same, then directly updates the production database for approve/reject. Only the Quant endpoint is mocked; Telegram and the database are not isolated. |
| `app/e2e_forward_check.py` | Uses hardcoded production credential/database paths; POSTs to 8095 and 8096, rejects a real inserted row and can send/edit Telegram messages. |

For ordinary verification, use `tests/test_offline.py` as documented in the
README. That suite blocks network and uses only temporary business state.

Public URL: `/opt/signal-gate/pending_public_url.txt`
Or SSH tunnel to `http://127.0.0.1:8095/hooks/v1/pending-signals`.

Headers: `Content-Type: application/json`, `X-Source: jd`, `X-Timestamp`, `X-Signature: sha256=HMAC("{ts}."+body, HMAC_SECRET_JD)`.

Body must include required fields, `review_status: "pending"`, fresh `idempotency_key`, and a far-future `expires_at` so Approve can reach Quant.

On-box:

```bash
sudo -u signal-gate SIGNAL_GATE_HOME=/opt/signal-gate \
  /usr/bin/python3 /opt/signal-gate/app/selftest.py
# journalctl -u signal-gate -n 30 | grep notify_sent

sudo -u signal-gate SIGNAL_GATE_HOME=/opt/signal-gate \
  /usr/bin/python3 /opt/signal-gate/app/review_selftest.py
```

Then tap **Approve** / **Reject** on the Telegram message in the digest chat.

## DB columns (migration-safe)

Added if missing: `decision`, `reviewed_at`, `reviewer`, `quant_status`, `quant_response`.

## Durable side effects (2026-09-28)

`app/review_outbox.py` adds `review_outbox` idempotently; it never rewrites or
deletes historical review rows, and **never backfills old pending prompts or
old approvals**. Only new intake creates a `notify` job, in the same transaction
as the signal. Only a new approval creates a `quant` job, in the same transaction
as the decision. An invalid/missing/naive expiry is rejected at intake; legacy
rows with an unknown lifetime also fail closed during approval. Quant rechecks
the expiry immediately before network delivery.

Both workers claim `pending -> sending` with a SQLite compare-and-set and commit
before calling a sender. The queue is a wakeup hint, not the record of work.
After restart, committed `pending` jobs remain eligible; `sending` becomes
`unknown`, never eligible for automatic replay. A still-running worker may close
its own claim with the matching claim ID, but another worker cannot claim it.
Two approval workers cannot create or dispatch two jobs for one signal.

HTTP 2xx means Quant intake acceptance, not a fill. Telegram confirmation needs
JSON `ok:true`, the expected recipient and a positive `message_id`. Timeout,
lost acknowledgement, malformed acknowledgement and platform errors remain
`unknown` and do not trigger automatic resend. Missing configuration leaves a
durable `pending` job, not an erased notification. Explicit operator reconciliation
is required for unknown jobs; there is intentionally no generic retry/reset
command, and records must not be deleted to make queue counts zero.

Read-only operational inspection (no body/recipient/secret export):

```sql
SELECT kind, status, count(*) FROM review_outbox GROUP BY kind, status;
SELECT id, kind, signal_id, status, attempts, started_at, completed_at
FROM review_outbox WHERE status IN ('pending', 'sending', 'unknown') ORDER BY id;
```

Ordinary restarts can process **new, already-authorized committed pending jobs**;
review this count before a deployment. Migration itself makes no external calls.
Old `decision=approve,quant_status IS NULL` rows without an outbox are evidence
gaps, not permission to resend. Preserve them for read-only reconciliation.
