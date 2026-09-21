"""Current-input snapshots are read-only and never impersonate a delivery receipt."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("input_status", ROOT / "app/input_status.py")
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class InputStatusTests(unittest.TestCase):
    def fixture(self, **changes):
        return {"schema_version": 1, "status": "dry_run", "report_date": "2026-09-21",
                "generated_at": NOW.isoformat(), "issues": [], "degraded": False, **changes}

    def test_only_quality_fields_leave_checker(self):
        original = self.fixture(text="PRIVATE_BODY", internal_path="PRIVATE_PATH", message_id=987)
        result = status.normalize(json.dumps(original).encode(), NOW)
        self.assertEqual(result["state"], "clear")
        self.assertEqual(set(result), {"schema_version", "report_date", "state", "checked_at", "quality_issues"})
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_degraded_is_issues_not_a_delivery_failure(self):
        result = status.normalize(json.dumps(self.fixture(issues=["nng8:missing"], degraded=True)).encode(), NOW)
        self.assertEqual(result["state"], "issues")
        self.assertEqual(result["quality_issues"], ["nng8:missing"])
        self.assertNotIn("delivery_status", result)

    def test_wrong_or_inconsistent_results_fail_closed(self):
        changes = [{"status": "confirmed"}, {"schema_version": True}, {"report_date": "2026-09-20"},
                   {"issues": ["arbitrary:PRIVATE"], "degraded": True}, {"issues": ["jd:stale"]},
                   {"degraded": True}, {"issues": {}}, {"degraded": 0}]
        for values in changes:
            with self.subTest(values=values), self.assertRaises(ValueError):
                status.normalize(json.dumps(self.fixture(**values)).encode(), NOW)

    def test_expired_future_naive_and_previous_local_day_rejected(self):
        for checked in (NOW - timedelta(minutes=16), NOW + timedelta(seconds=61),
                        NOW.replace(tzinfo=None), NOW - timedelta(days=1)):
            with self.subTest(checked=checked), self.assertRaises(ValueError):
                status.normalize(json.dumps(self.fixture(generated_at=checked.isoformat())).encode(), NOW)
        # Midnight UTC is not the report boundary: use Shanghai's calendar day.
        late = datetime(2026, 9, 21, 16, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            status.normalize(json.dumps(self.fixture(generated_at=late.isoformat())).encode(), late)

    def test_malformed_duplicate_and_oversized_output_rejected(self):
        raw = json.dumps(self.fixture())
        for value in (b"{", b"[]", b"x" * (status.MAX_BYTES + 1),
                      (raw[:-1] + ',"status":"dry_run"}').encode(),
                      (raw[:-1] + ',"extra":NaN}').encode()):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                status.normalize(value, NOW)

    def test_collector_fixed_dry_run_flags_and_minimal_environment(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(self.fixture()).encode()))
        self.assertEqual(status.collect(now=NOW, runner=runner)["state"], "clear")
        args, kwargs = runner.call_args
        self.assertEqual(args[0], ("/usr/bin/python3", "-B", "/opt/signal-gate/app/digest.py", "--dry-run", "--json"))
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 20)
        self.assertEqual(set(kwargs["env"]), {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "SIGNAL_GATE_HOME"})
        self.assertNotIn("shell", kwargs)

    def test_timeout_nonzero_and_invalid_output_become_unknown(self):
        for runner in (Mock(side_effect=subprocess.TimeoutExpired("PRIVATE_COMMAND", 20)),
                       Mock(side_effect=OSError("PRIVATE_PATH")),
                       Mock(return_value=SimpleNamespace(returncode=1, stdout=json.dumps(self.fixture()).encode())),
                       Mock(return_value=SimpleNamespace(returncode=0, stdout=b"broken"))):
            result = status.collect(now=NOW, runner=runner)
            self.assertEqual(result["state"], "unknown")
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_private_atomic_write_and_failure_preserves_previous(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            root.chmod(0o700)
            target = root / "status.json"
            status.write_snapshot(status.unknown(NOW), target)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            before = target.read_bytes()
            with patch.object(status.os, "replace", side_effect=OSError("synthetic")):
                with self.assertRaises(OSError):
                    status.write_snapshot({"state": "clear"}, target)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(root.glob(".inputs-*")), [])
            root.chmod(0o750)
            with self.assertRaises(ValueError):
                status.write_snapshot({}, target)

    def test_service_is_separate_identity_and_cannot_send_or_mutate_business_data(self):
        unit = (ROOT / "systemd/project-portal-inputs.service").read_text()
        for line in ("User=signal-gate", "Group=signal-gate", "PrivateNetwork=yes",
                     "ProtectSystem=strict", "CapabilityBoundingSet=\n", "StateDirectoryMode=0700",
                     "ReadWritePaths=/var/lib/project-portal-inputs", "RestrictAddressFamilies=AF_UNIX"):
            self.assertIn(line, unit)
        inaccessible = next(line for line in unit.splitlines() if line.startswith("InaccessiblePaths="))
        self.assertIn("/opt/signal-gate/secrets.env", inaccessible)
        self.assertIn("/opt/signal-gate/out", inaccessible)
        self.assertNotIn("EnvironmentFile", unit)
        self.assertNotIn("OnFailure", unit)


if __name__ == "__main__":
    unittest.main()
