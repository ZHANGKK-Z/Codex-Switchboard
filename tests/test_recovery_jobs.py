import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import recovery_jobs as jobs
from tests import test_projection_recovery as fixtures


IDENTITY = {"pid": 321, "creationTime": "456", "alive": True, "inJob": False}


class RecoveryJobTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RecoveryFixture()
        self.f.setUp()
        self.paths = self.f.paths
        self.real_launch = jobs.independent_worker.launch_independent
        self.launch = mock.patch.object(jobs.independent_worker, "launch_independent", return_value={"pid": 123, "childPid": 321, "creationTime": "456", "independent": True})
        self.launcher = self.launch.start()
        self.addCleanup(self.launch.stop)
        self.addCleanup(self.f.tearDown)

    def queue(self):
        return jobs.enqueue_recovery(self.paths, self.f._plan(), consent=True)

    def worker(self, job, **kwargs):
        with mock.patch.object(jobs.independent_worker, "require_current_independent", return_value=IDENTITY), mock.patch.object(jobs.independent_worker, "process_identity", return_value=IDENTITY):
            return jobs.run_worker(self.paths, job["id"], sleep_fn=lambda _: None,
                                   process_probe=kwargs.pop("process_probe", lambda _: []), **kwargs)

    def test_explicit_consent_before_any_disk_or_dispatch(self):
        with self.assertRaises(jobs.RecoveryJobError):
            jobs.enqueue_recovery(self.paths, self.f._plan(), consent=False)
        self.launcher.assert_not_called()
        self.assertFalse((self.paths.switchboard / "recoveries").exists())

    def test_plan_is_frozen_and_duplicate_dispatch_reuses_operation(self):
        plan = self.f._plan()
        first = jobs.enqueue_recovery(self.paths, plan, consent=True)
        plan["cursor"]["new_offset"] = 0
        second = self.queue()
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["reused"])
        self.assertNotEqual(jobs.load_job(self.paths, first["id"])["plan"]["cursor"]["new_offset"], 0)
        self.launcher.assert_called_once()

    def test_changed_preview_refuses_without_launch(self):
        plan = self.f._plan()
        self.f.raw.write_bytes(self.f.raw.read_bytes() + b"\n")
        with self.assertRaises(jobs.RecoveryJobError):
            jobs.enqueue_recovery(self.paths, plan, consent=True)
        self.launcher.assert_not_called()

    def test_launch_unknown_has_intent_and_never_automatic_retry(self):
        self.launcher.side_effect = RuntimeError("SECRET backend exception")
        job = self.queue()
        self.assertEqual(job["status"], "launch_unknown")
        self.assertTrue(job["dispatch_intent"])
        self.assertNotIn("SECRET", json.dumps(job))
        self.queue()
        self.launcher.assert_called_once()

    def test_cancel_before_worker_never_executes_engine(self):
        job = self.queue()
        jobs.request_cancel(self.paths, job["id"])
        with mock.patch.object(jobs.projection_recovery, "execute_plan") as execute:
            result = self.worker(job)
        self.assertEqual(result["status"], "cancelled")
        execute.assert_not_called()
        self.assertEqual(self.f._cursor(), (self.f.old, 2))

    def test_wait_requires_three_consecutive_empty_checks(self):
        job = self.queue()
        seen = []
        answers = iter([[], [], [{"Name": "codex.exe"}], [], [], []])
        def probe(_):
            seen.append(True)
            return next(answers, [])
        result = self.worker(job, process_probe=probe)
        self.assertGreaterEqual(len(seen), 6)
        self.assertEqual(result["status"], "pending_native_replay")
        self.assertTrue(result["cas_intent"])
        self.assertEqual(len(result["backups"]), 2)
        self.assertEqual(self.f._cursor(), (self.f.new, 2))

    def test_probe_error_stops_before_cas_without_secret_output(self):
        job = self.queue()
        result = self.worker(job, process_probe=mock.Mock(side_effect=RuntimeError("SECRET")))
        self.assertEqual(result["status"], "invalidated")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(self.f._cursor(), (self.f.old, 2))

    def test_independence_failure_precedes_claim_or_db_access(self):
        job = self.queue()
        with mock.patch.object(jobs.independent_worker, "require_current_independent", side_effect=RuntimeError("still in job")):
            with self.assertRaises(RuntimeError):
                jobs.run_worker(self.paths, job["id"])
        self.assertEqual(jobs.load_job(self.paths, job["id"])["status"], "queued")

    def test_backup_intent_write_failure_blocks_cas(self):
        job = self.queue()
        original = jobs._save
        def save(paths, value):
            if value.get("cas_intent"):
                raise OSError("receipt disk full")
            return original(paths, value)
        with mock.patch.object(jobs, "_save", side_effect=save):
            result = self.worker(job)
        self.assertEqual(self.f._cursor(), (self.f.old, 2))
        self.assertNotEqual(result["status"], "verified")

    def test_worker_claim_cannot_be_reused_even_if_process_dead(self):
        job = self.queue()
        self.worker(job)
        with self.assertRaises(jobs.RecoveryJobError):
            self.worker(job)

    def test_cas_is_pending_until_native_replay_and_all_messages_verified(self):
        job = self.queue()
        result = self.worker(job)
        self.assertEqual(result["status"], "pending_native_replay")
        pending = jobs.reconcile_job(self.paths, job["id"])
        self.assertEqual(pending["status"], "pending_native_replay")
        self.f._materialize(job["plan"])
        final = jobs.reconcile_job(self.paths, job["id"])
        self.assertEqual(final["status"], "verified")
        self.assertIn("不代表", final["message"])

    def test_waiting_worker_cannot_be_overwritten_by_reconcile(self):
        job = self.queue()
        job.update(status="waiting_exit", worker=IDENTITY)
        with jobs.switchboard.conversion_operation_lock(self.paths):
            jobs._save(self.paths, job)
        with mock.patch.object(jobs.independent_worker, "process_identity", return_value=IDENTITY):
            with self.assertRaises(jobs.RecoveryJobError):
                jobs.reconcile_job(self.paths, job["id"])
        self.assertEqual(jobs.load_job(self.paths, job["id"])["status"], "waiting_exit")

    def test_corrupt_existing_receipt_blocks_fresh_dispatch(self):
        job = self.queue()
        file = jobs.operation_folder(self.paths, job["id"]) / "operation.json"
        file.write_text("[]", encoding="utf-8")
        with self.assertRaises(jobs.RecoveryJobError):
            self.queue()
        self.launcher.assert_called_once()

    def test_identity_mismatch_receipt_cannot_be_reconciled(self):
        job = self.queue()
        job["plan_sha256"] = "wrong-plan"
        with jobs.switchboard.conversion_operation_lock(self.paths):
            jobs._save(self.paths, job)
        with self.assertRaises(jobs.RecoveryJobError):
            jobs.reconcile_job(self.paths, job["id"])

    def test_record_read_does_not_launch_or_reconcile_or_start_appserver(self):
        self.queue()
        with mock.patch.object(jobs.projection_recovery, "reconcile_plan") as reconcile:
            results = jobs.list_jobs(self.paths)
        self.assertEqual(len(results), 1)
        reconcile.assert_not_called()
        self.launcher.assert_called_once()

    def test_unclaimed_reconciliation_revokes_late_worker(self):
        job = self.queue()
        result = jobs.reconcile_job(self.paths, job["id"])
        self.assertEqual(result["status"], "invalidated")
        with self.assertRaises(jobs.RecoveryJobError):
            self.worker(job)

    def test_delayed_launch_failure_cannot_revive_revoked_authorization(self):
        def delayed(*_args, **_kwargs):
            current = jobs.list_jobs(self.paths)[0]
            jobs.reconcile_job(self.paths, current["id"])
            raise RuntimeError("launcher ack lost")
        self.launcher.side_effect = delayed
        job = self.queue()
        self.assertEqual(job["status"], "invalidated")
        with self.assertRaises(jobs.RecoveryJobError):
            self.worker(job)

    def test_explicitly_absent_pid_is_dead_but_permission_error_unknown(self):
        absent = OSError("absent")
        absent.winerror = 87
        denied = OSError("denied")
        denied.winerror = 5
        with mock.patch.object(jobs.independent_worker, "process_identity", side_effect=absent):
            self.assertIs(jobs._worker_alive(IDENTITY), False)
        with mock.patch.object(jobs.independent_worker, "process_identity", side_effect=denied):
            self.assertIsNone(jobs._worker_alive(IDENTITY))

    @unittest.skipUnless(os.name == "nt", "Real independent Windows synthetic recovery")
    def test_real_independent_worker_persists_cas_then_readonly_reconcile(self):
        job = self.queue()
        marker = self.paths.codex_home / "SYNTHETIC_RECOVERY_FIXTURE.json"
        marker.write_text(json.dumps({"synthetic": True, "operation": job["id"]}), encoding="utf-8")
        receipt = self.real_launch([sys.executable, str(Path(__file__).with_name("recovery_worker_fixture.py").resolve()),
                                    str(self.paths.codex_home), job["id"]],
                                   jobs.operation_folder(self.paths, job["id"]))
        self.assertTrue(receipt["independent"])
        deadline = time.monotonic() + 15
        current = job
        while time.monotonic() < deadline:
            current = jobs.load_job(self.paths, job["id"])
            if current["status"] in {"pending_native_replay", "invalidated", "needs_reconciliation"}:
                break
            time.sleep(0.1)
        self.assertEqual(current["status"], "pending_native_replay", current["message"])
        self.assertFalse(current["worker"]["inJob"])
        self.assertTrue(current["cas_intent"])
        self.assertEqual(self.f._cursor(), (self.f.new, 2))
        self.f._materialize(job["plan"])
        final = jobs.reconcile_job(self.paths, job["id"])
        self.assertEqual(final["status"], "verified")


if __name__ == "__main__":
    unittest.main()
