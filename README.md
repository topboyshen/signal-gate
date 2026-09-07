# signal-gate

Minimal localhost HMAC receiver for pending A-share signals on dedirock.

- Listen: `127.0.0.1:8095`
- Endpoint: `POST /hooks/v1/pending-signals`
- Secrets: `/opt/signal-gate/secrets.env` (mode 600)
- Data: `/opt/signal-gate/data/signals.db`
- Digest out: `/opt/signal-gate/out/daily-YYYY-MM-DD.md`
- Units: `signal-gate.service`, `signal-gate-digest.timer` (08:00 Asia/Shanghai)

Public nginx proxy is **not** enabled. See `docs/NGINX_PROXY.md`.

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
