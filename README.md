# signal-gate

Minimal localhost HMAC receiver for pending A-share signals on dedirock.

- Listen: `127.0.0.1:8095`
- Endpoint: `POST /hooks/v1/pending-signals`
- Secrets: `/opt/signal-gate/secrets.env` (mode 600)
- Data: `/opt/signal-gate/data/signals.db`
- Digest out: `/opt/signal-gate/out/daily-YYYY-MM-DD.md`
- Units: `signal-gate.service`, `signal-gate-digest.timer` (08:15 Asia/Shanghai)

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
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_sealed_research.py' -v
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

The timer specifies `OnCalendar=*-*-* 08:15:00 Asia/Shanghai`, so its calendar
does not depend on the server's local timezone. `RandomizedDelaySec=30s`,
`AccuracySec=1m` are retained; execution may be delayed from 08:15.
`Persistent=false` avoids catching up and sending an outdated morning report
after a late reboot or a timer migration. A missed morning delivery is reported
by the watchdog; it is not automatically resent in the afternoon.
`Timezone=` is not a valid timer directive.

```bash
systemd-analyze calendar '*-*-* 08:15:00 Asia/Shanghai'
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
report. News/execution sources older than 72 hours, undated or future-dated are
flagged. Research now uses the stricter same-day sealed contract below.

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
2026-09-09, after the 08:25 deadline, with bounded morning rechecks. It alerts only
for missing/failed/unknown delivery or degraded data, deduplicates an incident and
records recovery silently. Normal days produce no extra Telegram message. The
rollout does not fabricate a receipt for the legacy 09-08 message or resend it.
Receipt checks are operational evidence, not tamper-proof verification against a
compromised service account. Generated state and report files stay out of Git.

## Producer-shaped digest adapters (2026-09-09)

The renderer accepts editorial text from explicit top-level fields first, then
from the item's `provenance` object: `title/note/reason/summary`, `source`, public
`url/link/source_url`, and `published_at/timestamp/as_of/date`. Non-text values
do not mask valid nested text; malformed provenance and publication dates retain
quality warnings. A valid top-level publication date remains authoritative.
`event_window.start/end` are signal windows, not news publication timestamps.
For non-research sources, source-envelope date precedence and the 72-hour rule are unchanged;
a fresh `produced_at` does not refresh an old `as_of`.

All displayed nested fields use the existing bounded credential/link cleaning.
Explicit simulation/probe metadata in provenance is also filtered, including
known probe prefixes in `event_id`; ordinary headline text is not a probe flag.
No arbitrary nested objects, private event identifiers, or inferred sentiment
are rendered. Items retain upstream order and the existing three-item limit;
this adapter is not news ranking or cross-day deduplication.

For legacy nng8 text/markdown parser regression fixtures, report titles no longer consume a summary slot. Adjacent
market and turnover lines form one overview; a labelled watch list and its
bullet children form one block. The renderer selects at most three blocks with
labelled-section coverage, preserving source order, rather than discarding
later sector/watch content. Unlabelled text keeps its first-three-line behavior.
Source data notes and limitations are shown separately. An explicitly rejected,
failed or invalid `research-data` snapshot sets the existing `nng8:invalid`
quality code; its source warning must not disappear when shortening the report.
Rejection, risk and limitation notes take precedence over ordinary source notes.
Overflow and shortened long paragraphs/watch lists are explicitly labelled
incomplete and set the existing `truncated` code; clipping must not imply a
complete list or conclusion.

The content regression suite includes synthetic, anonymized examples matching
the actual producer field shapes. Raw production inputs, symbols, event IDs,
private source routes and credentials are not copied into test fixtures. Run
the same offline commands above. No approval, Quant, scheduler or delivery
behavior is changed by this adapter.

## Sealed research handoff (2026-09-09)

The nnG8 source job runs on weekdays at 08:00 Shanghai. Its existing draft and
notification chain stays intact. A successful draft/seal job additionally queues
the research relay, with independent checks at 08:05 and 08:10. The relay validates
today's completed occurrence, run gates, report bytes and seal hashes before
reading the **formal report**, not the XHS draft body or old EOD template. Posting
closes at 08:14, reserving time before the 08:15 merge; the relay unit is bounded
to 45 seconds plus a 5-second stop allowance. The 08:25
watchdog checks confirmed delivery and quality without extra healthy-day messages.

Production requires `research_contract` version 1, ready status, today's report
date, ordered timezone-aware completion time, passed publication/evidence gates,
and the report/run/context hash identifiers. These identifiers are verified
against artifacts by the authenticated relay; the receiver does not independently
read nnG8 files. The market `as_of` stays the report's previous-session date; a
Monday report may correctly reference Friday and is not rejected by a 72-hour
shortcut. A fresh envelope cannot make yesterday's report current.

`research_summary` retains the original fact/inference label, a complete selected
analysis block, support, counterevidence, risks, referenced sources and limitations.
Partial data and no human semantic review remain explicit. Missing evidence,
unresolved references, oversized summaries and unvalidated old input fail closed.
Missing/currently invalid research is shown as a gap, never silently replaced by
legacy text or Markdown. Historical parsers remain testable through an internal
opt-out used only by regression fixtures; no production CLI switch enables it.

Weekends have no scheduled source and explicitly omit old research. A weekday
non-trading-day skip is not yet transported as a separately validated skip
contract; it is conservatively reported as missing, not assumed to be a holiday.
No model call, approval, trading action or same-day resend is added by this change.

## News time provenance and point-in-time regression (2026-09-12)

Three clocks have different meanings:

- `items[].published_at` or `items[].provenance.published_at`: publication claimed
  by the producer. Use ISO timestamps with seconds and an explicit timezone.
  Date-only values remain date-only; missing publication time stays unknown.
  The receiver does not query publishers or manufacture missing timestamps.
- `as_of`/`date`: data dates, NOT proof of publication or arrival. Date-only
  envelopes are no longer displayed as an invented midnight timestamp.
- Local `received_at`: the receiver's UTC clock after successful HMAC and
  JSON-object validation, when serializing an observation. Sender-provided
  `received_at`, `produced_at`, signature timestamp and file mtime never supply
  this clock. This is reception evidence, not a Telegram delivery acknowledgement.

New authenticated JD digest drops retain the exact original UTF-8 request bytes
inside private append-only records at `inbox/jd/received/<receiver-id>.json`.
Each record includes a server timestamp and SHA-256 of those bytes, but no token
or HMAC signature. Complete records are published atomically without overwrite;
files are 0600 and the history directory is 0700. The latest JSON and dated JSON
aliases remain available. `digest-latest.receipt.json` points to a receiver-owned
record. Live rendering trusts its time only when the record and latest body
match. JSON news never borrows an unrelated old Markdown twin.

Receipt names are server-generated. Pending-signal keys are restricted to safe,
printable single filenames (240 UTF-8 bytes max); the `digest-` namespace is
reserved. Digest dates become safe calendar-day aliases. These checks prevent
another authenticated ingress endpoint from overwriting reception evidence.
HMAC construction/skew, sealed nnG8 gates, review/Quant behavior and schedules
are unchanged. Only the receiver needs a restart to begin recording new arrivals.

Strict **news-only** historical replay:

```bash
python3 -B app/news_history.py --base /opt/signal-gate \
  --cutoff '2026-09-14T08:15:00+08:00'
# Add --text for sanitized news; the default prints safe metadata only.
```

Replay selects the latest complete receiver record at/before the cutoff, never
`digest-latest`, a dated legacy alias, or today's review database. Later revisions
and late-arriving old articles are unavailable before their recorded reception.
Publication claims after the cutoff are omitted; malformed or timezone-ambiguous
publication claims are omitted in strict replay. Known receipt time can establish
availability when publication is unknown, but the output explicitly says unknown.
Publication counters describe inspected display candidates before duplicate
folding, not the raw incoming batch size. This does not prove that an actual
scheduled report had already read the input. It does not reconstruct research,
approval/decision changes or execution state.

Missing legacy receipts return `status=unavailable`, exit 2: **do not backfill
history using mtime, date filenames or producer clocks**. Corrupt, mismatched,
ambiguous selected receipts and scan limits fail closed instead of selecting an
older healthy-looking version. Reads are bounded to 10,000 directory entries
and roughly 2 MB per candidate; the renderer retains its 512 KiB body limit.
There is no automatic history deletion or import of old artifacts in this rollout.
The receiver filesystem/account and synchronized host clock are trusted; SHA-256
is a content identifier, not protection against a compromised local account.

Run `test_news_history.py` with the other offline suites. Tests use synthetic
publication/arrival clocks and block network and all SQLite access during replay.
They cover exact cutoff boundaries, late corrections, unknown time precision,
spoofed sender times, byte hashes, corrupt/symlink receipts and read-only behavior.
Receiver tests ensure failed authentication/storage cannot return a successful
reception acknowledgement. Reception records and aliases remain excluded from
Git; no production inputs or credentials belong in these fixtures.

## nnG8 byte-bound reception receipts (2026-09-20)

For newly received nnG8 digest drops, the canonical business input is a private,
append-only record at `inbox/nng8/research-receipts/<report-day>.json`. It retains
the original UTF-8 POST bytes in base64, their SHA-256 and byte count, the report
hash, both dates, an opaque receiver ID and a receiver UTC storage timestamp.
New directories/files are 0700/0600; existing parent permissions are unchanged.
No legacy latest/dated alias is copied, rewritten, imported or upgraded.

Actual producer fields are top-level `target_date` and `as_of`, plus
`research_contract.report_date`, `as_of`, `report_sha256`. The dates must be
valid calendar dates, agree across levels, and satisfy `as_of <= target_date`.
This storage check does not infer a trading calendar or replace the stricter
existing digest publication/semantic gate. The 512 KiB limit matches the reader.

Successful POST acknowledgement includes `stored=true`, `content_sha256`,
`record_id`, `stored_at`, `target_date`, `as_of` and `day` (the latter two both
mean the data date). The hash is recomputed from the saved original bytes, not
from reserialized JSON. A process-shared lock, no-overwrite atomic publication,
file/directory fsync and readback precede acknowledgement. Same key `(nng8,
target_date)` and same bytes returns the original acknowledgement; different
bytes returns 409 `content_conflict`, leaving the original record unchanged.
Errors never produce `stored=true`; a crash/fsync error can leave complete but
unacknowledged evidence requiring read-only investigation, not automatic resend.

`GET /records/nng8/YYYY-MM-DD` returns only the verified receipt, never the body
or internal path. It uses `X-Source: nng8`, a timestamp within the existing skew,
and `X-Signature: sha256=<HMAC(secret, timestamp + ".GET." + exact_path)>`.
Authentication is checked before opening records; queries do not create a
directory, take a writing lock or modify state. Missing records return 404;
corrupt/unreadable evidence returns 503, not a fabricated successful receipt.
The existing HTTPS listener exposes only this bounded GET route; no port,
certificate, firewall, private POST URL or credential is changed.

The digest reader consumes the same canonical record for its report date,
verifies its bytes and observed time, and then applies the unchanged sealed
research checks. A missing canonical record retains the existing legacy reading
path **without** inventing a receipt; a corrupt record cannot fall back to an
apparently healthy alias. Stored does not mean the downstream digest was sent.

Run the two additional offline suites with the original five:

```text
python3 -B -m unittest discover -s tests -p 'test_research_receipts.py' -v
python3 -B -m unittest discover -s tests -p 'test_research_receiver.py' -v
```

They cover real handler/authentication, durable storage, process concurrency,
restart/readback, conflicts, failures, consumer binding and no legacy import.
No production POST, database access, model call or Telegram send is involved.
Sender-side v2 receipt persistence and the next natural handoff remain separate
acceptance steps; this receiver deployment does not rewrite historic nnG8 state.
