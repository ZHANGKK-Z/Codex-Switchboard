import json
import os
import subprocess
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import switchboard
import handoff_jobs as jobs
import handoff_bundle as bundle
from handoff_client import InferenceOutcomeUnknown
from tests.test_switchboard import create_binding_fixture


def git(root, *args):
    subprocess.run(["git", "-c", "user.name=Offline fixture", "-c", "user.email=offline@example.invalid", *args],
                   cwd=root, check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def source_report(root):
    return {**{key: "Offline fixture: this section is known and explicitly scoped." for key in bundle.SECTIONS},
            "requirements": [{"id": "R1", "statement": "Continue the documented project task", "status": "pending",
                              "evidence": "README.md records the objective", "next_step": "Ask for implementation approval"}],
            "references": [{"path": str(root / "README.md"), "purpose": "Current project objective", "kind": "document", "required": True}],
            "missing_information": []}


def target_report(folder):
    manifest = bundle.read_json(folder / "manifest.json")
    checks = []
    for item in manifest["documents"]:
        evidence = {"path": str(folder / item["file"]), "line": 1,
                    "quote": (folder / item["file"]).read_text(encoding="utf-8").splitlines()[0]}
        checks.append({"id": item["id"], "summary": "Read and compared the actual objective with the frozen document.",
                       "status": "verified", "evidence": [evidence]})
    return {"objective": "Continue the documented task after user approval.", "scope": "Read-only acceptance, no business implementation.",
            "head": manifest["workspace"]["head"],
            "requirement_checks": [{"id": "R1", "interpretation": "This is an unfinished task and needs new implementation authority.",
                                    "status": "verified", "evidence": checks[0]["evidence"]}],
            "document_checks": checks,
            "repo_checks": [{"path": manifest["documents"][0]["source"], "line": 1,
                             "quote": Path(manifest["documents"][0]["source"]).read_text().splitlines()[0]}],
            "conflicts": [], "missing_information": [], "next_steps": "Ask the user to approve implementation before editing.",
            "authorization_needed": "Business implementation is not authorized by this handoff."}


class FakeClient:
    """Stateful fake for interruption/receipt tests; not model-quality evidence."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.process = None
        self.denied_requests = []
        self.discovered_overrides = {}

    def start(self):
        self.fixture.calls.append(("start",))

    def initialize(self, **kwargs):
        pass

    def close(self):
        self.fixture.calls.append(("close",))

    def prepare(self, cwd, providers, **kwargs):
        return {"sandbox": "read-only"}

    def read_account(self):
        return {"account": {"type": "chatgpt", "email": self.fixture.account}}

    def resume_source(self, ident, **kwargs):
        self.fixture.calls.append(("resume", ident, kwargs))

    def probe_read_access(self, cwd, files):
        self.fixture.calls.append(("read_probe", cwd, files))
        if self.fixture.fail_read_probe:
            raise bundle.HandoffValidationError("Read-only fixture probe failed")

    def create_clean(self, *, before_send, created, **kwargs):
        before_send()
        self.fixture.calls.append(("create", kwargs))
        if self.fixture.fail_create:
            raise InferenceOutcomeUnknown("unknown creation fixture")
        created("new-thread")

    def set_thread_name(self, *args):
        pass

    def read_thread(self, ident, **kwargs):
        return {"thread": {"id": ident, "model": "fixture-model"}}

    def send_turn(self, ident, prompt, schema, *, before_send, accepted, **kwargs):
        before_send()
        self.fixture.calls.append(("send", ident, prompt))
        if self.fixture.fail_without_id:
            raise InferenceOutcomeUnknown("unknown paid result fixture")
        accepted(ident + "-turn")
        return ident + "-turn"

    def wait_turn(self, ident, turn_id, **kwargs):
        if self.fixture.fail_wait:
            raise InferenceOutcomeUnknown("interrupted fixture")
        if ident == "old-thread":
            return {"report": self.fixture.report, "viewed_images": []}
        report = target_report(self.fixture.folder / "bundle")
        if self.fixture.incomplete_target:
            report["document_checks"] = []
        return {"report": report, "viewed_images": []}

    def recover_turn(self, ident, turn_id, **kwargs):
        self.fixture.calls.append(("recover", ident, turn_id))
        if self.fixture.fail_recover:
            raise InferenceOutcomeUnknown("not completed fixture")
        result = self.wait_turn(ident, turn_id)
        if self.fixture.corrupt_persistence and ident == "new-thread":
            result["report"]["objective"] = "Different persisted result"
        return result


class HandoffWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "project"
        self.work.mkdir()
        (self.work / "README.md").write_text("# Current project objective\nRead-only acceptance fixture.\n", encoding="utf-8")
        git(self.work, "init")
        git(self.work, "add", "README.md")
        git(self.work, "commit", "-m", "offline fixture")
        self.paths = switchboard.Paths(self.root / "home")
        # Original chat cwd deliberately differs from the actual project.
        self.chat_cwd = self.root / "original-chat-workspace"
        self.chat_cwd.mkdir()
        create_binding_fixture(self.paths, [{"id": "old-thread", "model_provider": "openai",
                                            "cwd": str(self.chat_cwd), "model": "fixture-model"}])
        self.calls = []
        self.report = source_report(self.work)
        self.account = "offline@example.invalid"
        self.fail_without_id = self.fail_wait = self.fail_recover = False
        self.incomplete_target = self.corrupt_persistence = self.fail_create = False
        self.fail_read_probe = False

    def enqueue(self, **kwargs):
        job = jobs.enqueue_handoff(self.paths, "old-thread", self.work, kwargs.pop("documents", []),
                                   "fixture-model", consent=kwargs.pop("consent", True), **kwargs)
        self.folder = jobs.operation_folder(self.paths, job["id"])
        return job

    def run_job(self, job, **kwargs):
        runner = jobs.HandoffRunner(self.paths, job["id"], client_factory=lambda: FakeClient(self),
            process_probe=kwargs.pop("process_probe", lambda _owned=None: []), history_check=lambda _: None,
            sleep=lambda _: None, **kwargs)
        return runner.run()

    def sends(self):
        return [call[1] for call in self.calls if call[0] == "send"]

    def test_full_flow_uses_original_chat_and_clean_target_then_reopens(self):
        job = self.enqueue()
        binding_before = switchboard.thread_provider_binding(self.paths, "old-thread")
        result = self.run_job(job)
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(self.sends(), ["old-thread", "new-thread"])
        resume = next(call for call in self.calls if call[0] == "resume")
        self.assertEqual(resume[2]["cwd"], str(self.chat_cwd))
        self.assertEqual(result["target_thread"]["thread_id"], "new-thread")
        self.assertEqual(switchboard.thread_provider_binding(self.paths, "old-thread"), binding_before)
        self.assertTrue((self.folder / "ACCEPTANCE.md").is_file())
        self.assertEqual(len([call for call in self.calls if call[0] == "start"]), 3)
        self.assertTrue(self.enqueue()["reused"])
        self.assertEqual(self.run_job(job)["status"], "ready")
        self.assertEqual(len(self.sends()), 2)

    def test_consent_required_before_creating_any_job(self):
        with self.assertRaises(bundle.HandoffValidationError):
            self.enqueue(consent=False)
        self.assertFalse((self.paths.switchboard / "handoffs").exists())

    def test_failed_read_preflight_prevents_model_spend(self):
        job = self.enqueue()
        self.fail_read_probe = True
        self.assertEqual(self.run_job(job)["status"], "failed")
        self.assertEqual(self.sends(), [])

    def test_repeated_click_reuses_same_operation_even_when_parameters_change(self):
        first = self.enqueue()
        second = self.enqueue(user_note="A later click does not overwrite the frozen request")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["request"]["user_note"], "")

    def test_changed_external_attachment_does_not_reuse_ready_acceptance(self):
        attachment = self.root / "chosen.txt"
        attachment.write_text("Original external reference", encoding="utf-8")
        job = self.enqueue(documents=[attachment])
        self.assertEqual(self.run_job(job)["status"], "ready")
        attachment.write_text("Updated external reference", encoding="utf-8")
        self.assertNotEqual(self.enqueue(documents=[attachment])["id"], job["id"])

    def test_unknown_paid_result_is_never_resent_or_hidden_by_new_id(self):
        job = self.enqueue()
        self.fail_without_id = True
        result = self.run_job(job)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["source_turn"]["status"], "sending")
        self.fail_without_id = False
        self.assertEqual(self.run_job(job)["status"], "needs_review")
        self.assertEqual(self.sends(), ["old-thread"])
        self.assertEqual(self.enqueue()["id"], job["id"])

    def test_known_turn_recovers_readonly_without_resubmitting(self):
        job = self.enqueue()
        self.fail_wait = True
        result = self.run_job(job)
        self.assertEqual(result["source_turn"]["status"], "submitted")
        self.fail_wait = False
        self.assertEqual(self.run_job(job)["status"], "ready")
        self.assertEqual(self.sends(), ["old-thread", "new-thread"])

    def test_unknown_thread_creation_never_creates_a_second_target(self):
        job = self.enqueue()
        self.fail_create = True
        self.assertEqual(self.run_job(job)["status"], "needs_review")
        self.fail_create = False
        self.assertEqual(self.run_job(job)["status"], "needs_review")
        self.assertEqual(len([call for call in self.calls if call[0] == "create"]), 1)

    def test_cancel_unknown_thread_creation_still_blocks_new_operation(self):
        job = self.enqueue()
        self.fail_create = True
        self.run_job(job)
        jobs.request_cancel(self.paths, job["id"])
        result = self.run_job(job)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(self.enqueue()["id"], job["id"])
        self.assertEqual(self.sends(), ["old-thread"])

    def test_uncertain_receipt_blocks_new_id_even_if_top_level_marked_cancelled(self):
        job = self.enqueue()
        self.fail_create = True
        result = self.run_job(job)
        result["status"] = "cancelled"
        bundle.atomic_json(self.folder / "operation.json", result)
        self.assertEqual(self.enqueue()["id"], job["id"])

    def test_source_gaps_save_bundle_but_do_not_charge_target(self):
        job = self.enqueue()
        self.report["missing_information"] = ["Original reference image is missing"]
        result = self.run_job(job)
        self.assertEqual(result["stage"], "source_gaps")
        self.assertTrue((self.folder / "bundle" / "HANDOFF.md").is_file())
        self.assertEqual(self.sends(), ["old-thread"])
        self.assertEqual(self.run_job(job)["status"], "needs_review")
        self.assertEqual(self.sends(), ["old-thread"])

    def test_incomplete_target_cannot_be_ready(self):
        job = self.enqueue()
        self.incomplete_target = True
        result = self.run_job(job)
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["gaps"])

    def test_mismatching_persisted_output_cannot_be_ready(self):
        job = self.enqueue()
        self.corrupt_persistence = True
        self.assertEqual(self.run_job(job)["status"], "needs_review")

    def test_project_change_before_execution_prevents_paid_requests(self):
        job = self.enqueue()
        (self.work / "README.md").write_text("changed contents", encoding="utf-8")
        self.assertEqual(self.run_job(job)["status"], "failed")
        self.assertEqual(self.sends(), [])

    def test_source_model_or_workspace_drift_is_not_silently_overridden(self):
        for field, value in (("cwd", str(self.work)), ("model", "different-model")):
            with self.subTest(field=field):
                job = self.enqueue()
                binding = switchboard.thread_provider_binding(self.paths, "old-thread")
                binding[field] = value
                self.assertEqual(self.run_job(job, binding_fn=lambda _: binding)["status"], "failed")
                self.assertEqual(self.sends(), [])

    def test_external_selected_file_change_invalidates_frozen_request(self):
        attachment = self.root / "chosen.txt"
        attachment.write_text("Selected original content", encoding="utf-8")
        job = self.enqueue(documents=[attachment])
        attachment.write_text("Different content", encoding="utf-8")
        self.assertEqual(self.run_job(job)["status"], "failed")
        self.assertEqual(self.sends(), [])

    def test_secret_selected_text_is_rejected_before_enqueue_or_model(self):
        attachment = self.root / "chosen.txt"
        attachment.write_text("token fixture: sk-" + "x" * 32, encoding="utf-8")
        with self.assertRaises(bundle.HandoffValidationError):
            self.enqueue(documents=[attachment])
        self.assertFalse((self.paths.switchboard / "handoffs").exists())

    def test_cancel_while_waiting_never_starts_a_model(self):
        job = self.enqueue()
        jobs.request_cancel(self.paths, job["id"])
        self.assertEqual(self.run_job(job, process_probe=lambda _: ["codex"])["status"], "cancelled")
        self.assertEqual(self.sends(), [])

    def test_real_detached_worker_routes_cancelled_fixture_without_provider(self):
        job = self.enqueue()
        jobs.request_cancel(self.paths, job["id"])
        worker_pid = jobs.launch_worker(self.paths, job["id"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = jobs.load_job(self.paths, job["id"])
            if result["status"] == "cancelled" and not jobs.pid_alive(worker_pid):
                break
            time.sleep(0.1)
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result["source_turn"]["status"], "not_sent")
        self.assertEqual(result["target_turn"]["status"], "not_sent")

    def test_cancel_unknown_remains_blocked(self):
        job = self.enqueue()
        self.fail_without_id = True
        self.run_job(job)
        jobs.request_cancel(self.paths, job["id"])
        self.assertEqual(self.run_job(job)["status"], "needs_review")
        self.assertEqual(self.enqueue()["id"], job["id"])

    def test_secret_output_is_not_written_to_private_job_or_bundle(self):
        job = self.enqueue()
        self.report["objective"] = "secret fixture sk-" + "x" * 32
        self.assertEqual(self.run_job(job)["status"], "needs_review")
        self.assertFalse((self.folder / "source_turn-output.json").exists())
        self.assertFalse((self.folder / "bundle").exists())

    def test_frozen_output_tamper_does_not_trigger_new_model_turn(self):
        job = self.enqueue()
        self.report["missing_information"] = ["Need original screenshot"]
        self.run_job(job)
        (self.folder / "bundle" / "HANDOFF.md").write_text("modified", encoding="utf-8")
        self.assertEqual(self.run_job(job)["status"], "needs_review")
        self.assertEqual(self.sends(), ["old-thread"])

    def test_account_switch_invalidates_existing_authority(self):
        job = self.enqueue()
        self.fail_wait = True
        self.run_job(job)
        self.fail_wait = False
        self.account = "different@example.invalid"
        self.assertEqual(self.run_job(job)["status"], "needs_review")
        self.assertEqual(self.sends(), ["old-thread"])

    def test_untracked_contents_are_part_of_snapshot_not_only_filename(self):
        file = self.work / "not-committed.txt"
        file.write_text("first", encoding="utf-8")
        first = bundle.workspace_snapshot(self.work)
        file.write_text("other", encoding="utf-8")
        self.assertNotEqual(first, bundle.workspace_snapshot(self.work))

    def test_bundle_rejects_unselected_external_documents_and_credentials(self):
        external = self.root / "not-selected.txt"
        external.write_text("outside project", encoding="utf-8")
        secret = self.work / ".env"
        secret.write_text("not real credentials", encoding="utf-8")
        for file in [external, secret]:
            with self.subTest(file=file), self.assertRaises(bundle.HandoffValidationError):
                bundle.safe_reference(self.work, str(file), explicit=[], codex_home=self.paths.codex_home)

    def test_forged_quote_or_omitted_requirement_fails_acceptance(self):
        job = self.enqueue()
        self.assertEqual(self.run_job(job)["status"], "ready")
        folder = self.folder / "bundle"
        report = target_report(folder)
        report["requirement_checks"][0]["evidence"][0]["quote"] = "invented evidence"
        self.assertTrue(bundle.validate_acceptance(report, folder))
        report = target_report(folder)
        report["requirement_checks"] = []
        self.assertTrue(bundle.validate_acceptance(report, folder))

    def test_process_probe_does_not_hide_sibling_codex(self):
        rows = [{"Name": "codex.exe", "ProcessId": 501, "ParentProcessId": os.getppid()},
                {"Name": "codex.exe", "ProcessId": 600, "ParentProcessId": os.getpid()},
                {"Name": "codex.exe", "ProcessId": 601, "ParentProcessId": 600}]
        with mock.patch.object(switchboard, "process_lines", return_value=[json.dumps(item) for item in rows]):
            found = switchboard.appserver_blocking_processes(strict=True, ignored_tree_root_pid=600)
        self.assertEqual(len(found), 1, found)


if __name__ == "__main__":
    unittest.main()
