# signal-gate

Minimal localhost HMAC receiver for pending A-share signals on dedirock.

- Listen: `127.0.0.1:8095`
- Endpoint: `POST /hooks/v1/pending-signals`
- Secrets: `/opt/signal-gate/secrets.env` (mode 600)
- Data: `/opt/signal-gate/data/signals.db`
- Digest out: `/opt/signal-gate/out/daily-YYYY-MM-DD.md`
- Units: `signal-gate.service`, `signal-gate-digest.timer` (08:00 Asia/Shanghai)

The existing HTTPS entry was explicitly approved on 2026-09-08. Its private
routes remain outside this repository; the relay and included snippet are
monitored by separate approved hashes in `/opt/dedirock-infra`. Do not copy
private paths into documentation. See `docs/NGINX_PROXY.md` for the older template.

- Review bot: `signal-gate-review.service` (Telegram Approve/Reject → Quant). See `docs/REVIEW.md`.

## Version control boundary

This repository tracks the reviewed application scripts, documentation, systemd
templates and offline tests through an explicit `.gitignore` allowlist. Stage
reviewed paths explicitly. Credentials, private endpoint files, SQLite databases,
inbox artifacts and generated reports stay outside Git, including their backups.
Do not use `git add -f` to bypass that boundary.

## Safe offline verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_offline.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_digest_content.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_digest_delivery.py' -v
```

This suite uses temporary databases and artifacts, synthetic configuration, and
mocked Telegram/Quant calls. Network access and production database access are
blocked. It does not read production credentials, bind a listener, or send a
message.

`app/selftest.py`, `app/review_selftest.py` and `app/e2e_forward_check.py` are
**manual integration drills with live side effects**, not offline regression
tests. Their defaults load production credentials and submit pending signals;
they can write the live database and trigger Telegram notifications. The review
drill mocks only Quant, while the end-to-end drill also forwards a real review
callback. Do not run them in automated checks or routine deployment validation.
They require separate authorization for the exact live actions. See
`docs/REVIEW.md` for details.

## Digest schedule

The timer specifies `OnCalendar=*-*-* 08:00:00 Asia/Shanghai`, so its calendar
does not depend on the server's local timezone. `RandomizedDelaySec=30s`,
`AccuracySec=1m` and `Persistent=true` are retained; execution may be delayed from
08:00, and starting an overdue persistent timer can catch up a missed run.
`Timezone=` is not a valid timer directive.

```bash
systemd-analyze calendar '*-*-* 08:00:00 Asia/Shanghai'
```

This command only validates the calendar. Applying a changed unit requires a
systemd daemon reload and inspection of the next scheduled trigger. Do not
manually start the digest service as a schedule check: it writes a report and
may send Telegram messages.

## Digest quality and delivery (2026-09-08)

The digest summarizes new signals and review/execution progress; it does not
replace the 08:30 overseas market brief and requires neither portfolio entry nor
additional model calls. `app/digest_content.py` reads the existing inbox artifacts
and SQLite database read-only, with bounded inputs and per-source degradation.
The text distinguishes source dates from generation time, missing/invalid/stale
data from confirmed zero, current unexpired pending items from history, and
upstream acceptance from actual fills. Simulation filtering is shared by item
counts and execution totals. A previous-day `as_of` is normal for a morning
report; sources older than 72 hours, undated or future-dated are flagged.

`app/digest.py` is the delivery entry point. Its normal invocation sends; for
inspection use only:

```bash
python3 -B app/digest.py --dry-run --json
```

The JSON dry-run prints fixed metadata and issue codes, never the report body,
credentials or recipients. It does not read notification secrets, write reports
or delivery state, or send messages. A degraded source is visible in this output;
it must not be silently treated as fresh data.

Normal delivery uses a daily lock and atomic private receipt at
`out/delivery/YYYY-MM-DD.json` (0600; directory 0700). Receipts contain report date,
content/source-version hashes, timestamps, fixed issue codes and confirmed part
message IDs, not tokens, recipients or body text. HTTP status alone is not a
delivery confirmation: JSON success and a valid message ID are required.
Messages are split into complete bounded parts, not silently truncated.

An already-confirmed day is not resent, even if the current inputs changed.
Known rejected requests can be retried within limits; ambiguous acceptance or a
crash around sending is retained as unknown and is not blindly resent. Partially
confirmed delivery retains its part IDs; changed content blocks automatic resume
instead of mixing two versions. Telegram provides no idempotency key, so this
conservative mechanism does not claim absolute exactly-once delivery or that a
human read the message. Manual resend/reconciliation requires explicit approval.

The separate infra watchdog starts monitoring receipt-based delivery on
2026-09-09, after the 08:10 deadline, with bounded morning rechecks. It alerts only
for missing/failed/unknown delivery or degraded data, deduplicates an incident and
records recovery silently. Normal days produce no extra Telegram message. The
rollout does not fabricate a receipt for the legacy 09-08 message or resend it.
Receipt checks are operational evidence, not tamper-proof verification against a
compromised service account. Generated state and report files stay out of Git.
