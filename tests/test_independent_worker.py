import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import independent_worker as worker


class LaunchContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.command = [str(Path(sys.executable).resolve()), "--fixture-arg"]
        self.identity = {"pid": 999, "creationTime": "12345", "alive": True, "inJob": False}
        self.child = {"pid": 1000, "creationTime": "67890", "alive": True, "inJob": False}

    def launch_patches(self, *, ready=None, started=None, inspect=None):
        def wait(folder, name, nonce, deadline):
            digest = worker._request_digest(worker._read(folder, "request.json"))
            if name == "ready.json":
                return {"schema": 1, "nonce": nonce, "request_sha256": digest,
                        **(self.identity if ready is None else ready)}
            return {"schema": 1, "nonce": nonce, "creationTime": "12345", "childPid": 1000,
                    "childCreationTime": "67890", "request_sha256": digest, **(started or {})}
        patches = [mock.patch.object(worker, "_spawn_wmi", return_value=999),
                   mock.patch.object(worker, "_wait_message", side_effect=wait),
                   mock.patch.object(worker, "process_identity", side_effect=inspect or [self.identity, self.identity, self.child])]
        for patch in patches:
            self.addCleanup(patch.stop)
            patch.start()

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_verified_result_is_sanitized_and_owner_is_wrapper(self):
        self.launch_patches()
        result = worker.launch_independent(self.command + ["private-marker"], self.root)
        self.assertEqual(result["pid"], 999)
        self.assertEqual(result["childPid"], 1000)
        self.assertTrue(result["independent"])
        self.assertNotIn("private-marker", json.dumps(result))
        self.assertNotIn("command", result)
        folders = list(self.root.glob(".independent-launch-*"))
        self.assertEqual(len(folders), 1)
        self.assertTrue((folders[0] / "accepted.json").is_file())

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_job_membership_rejects_before_permit(self):
        self.launch_patches(inspect=[{**self.identity, "inJob": True}])
        with self.assertRaises(worker.IndependentLaunchError) as raised:
            worker.launch_independent(self.command, self.root)
        self.assertFalse(raised.exception.dispatched)
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_pid_reuse_rejects_before_permit(self):
        self.launch_patches(ready={**self.identity, "creationTime": "different-instance"})
        with self.assertRaises(worker.IndependentLaunchError) as raised:
            worker.launch_independent(self.command, self.root)
        self.assertFalse(raised.exception.dispatched)
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_wrong_wmi_pid_rejects_before_permit(self):
        self.launch_patches(ready={**self.identity, "pid": 123})
        with self.assertRaises(worker.IndependentLaunchError) as raised:
            worker.launch_independent(self.command, self.root)
        self.assertFalse(raised.exception.dispatched)

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_ack_identity_failure_is_dispatched_unknown(self):
        self.launch_patches(started={"childCreationTime": "reused-pid"})
        with self.assertRaises(worker.IndependentLaunchError) as raised:
            worker.launch_independent(self.command, self.root)
        self.assertTrue(raised.exception.dispatched)
        self.assertEqual(raised.exception.pid, 999)
        self.assertEqual(len(list(self.root.glob(".independent-launch-*/permit.json"))), 1)

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_ack_timeout_never_claims_not_dispatched(self):
        self.launch_patches()
        def wait(folder, name, nonce, deadline):
            if name == "ready.json":
                return {**self.identity, "request_sha256": worker._request_digest(worker._read(folder, "request.json"))}
            raise worker.IndependentLaunchError("INDEPENDENT_HANDSHAKE_TIMEOUT")
        with mock.patch.object(worker, "_wait_message", side_effect=wait):
            with self.assertRaises(worker.IndependentLaunchError) as raised:
                worker.launch_independent(self.command, self.root)
        self.assertTrue(raised.exception.dispatched)

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_wmi_failure_has_no_fallback_or_command_disclosure(self):
        with mock.patch.object(worker, "_spawn_wmi", side_effect=OSError("private-marker")) as spawn:
            with self.assertRaises(worker.IndependentLaunchError) as raised:
                worker.launch_independent(self.command + ["private-marker"], self.root)
        self.assertEqual(spawn.call_count, 1)
        self.assertFalse(raised.exception.dispatched)
        self.assertNotIn("private-marker", str(raised.exception))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_wmi_parameters_are_data_not_powershell_source(self):
        arguments = [r"C:\a b\python.exe", 'double"quote', "'; throw 'injected", "$env:PATH", "中文"]
        response = subprocess.CompletedProcess([], 0, '{"returnValue":0,"pid":123}', "")
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(subprocess, "run", return_value=response) as run:
            self.assertEqual(worker._spawn_wmi(arguments, self.root, 1), 123)
        call = run.call_args
        encoded = call.args[0][-1]
        script = base64.b64decode(encoded).decode("utf-16le")
        self.assertEqual(script, worker._WMI_SCRIPT)
        for argument in arguments:
            self.assertNotIn(argument, script)
        payload = json.loads(base64.b64decode(call.kwargs["input"]))
        self.assertEqual(payload["commandLine"], subprocess.list2cmdline(arguments))
        self.assertEqual(call.kwargs["creationflags"], 0x08000000)
        self.assertNotIn("shell", call.kwargs)

    def test_frozen_dispatch_contract(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(worker._wrapper_command(self.root), [sys.executable, "--independent-worker", str(self.root)])

    def test_protocol_messages_cannot_be_overwritten(self):
        worker._write(self.root, "ready.json", {"first": True})
        with self.assertRaises(FileExistsError):
            worker._write(self.root, "ready.json", {"second": True})
        self.assertEqual(worker._read(self.root, "ready.json"), {"first": True})

    def test_missing_permit_has_a_bounded_timeout(self):
        begin = time.monotonic()
        with self.assertRaisesRegex(worker.IndependentLaunchError, "TIMEOUT"):
            worker._wait_message(self.root, "permit.json", "n", begin + 0.08)
        self.assertLess(time.monotonic() - begin, 0.5)

    def test_nonce_mismatch_rejects(self):
        worker._write(self.root, "permit.json", {"schema": 1, "nonce": "wrong"})
        with self.assertRaisesRegex(worker.IndependentLaunchError, "MISMATCH"):
            worker._wait_message(self.root, "permit.json", "right", time.monotonic() + 0.1)

    def test_cleanup_never_deletes_unknown_files(self):
        (self.root / "business.txt").write_text("keep", encoding="utf-8")
        worker._write(self.root, "ready.json", {})
        worker._cleanup(self.root)
        self.assertEqual((self.root / "business.txt").read_text(), "keep")
        self.assertFalse((self.root / "ready.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_invalid_timeouts_and_relative_executables_reject(self):
        for timeout in (0, -1, 61, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(worker.IndependentLaunchError):
                worker.launch_independent(self.command, self.root, timeout=timeout)
        with self.assertRaises(worker.IndependentLaunchError):
            worker.launch_independent(["python.exe", "fixture"], self.root)

    def test_worker_refuses_job_before_any_child(self):
        folder = self.root / ".independent-launch-test0000"
        folder.mkdir()
        worker._write(folder, "request.json", {"schema": 1, "nonce": "a" * 64, "command": self.command,
                                               "cwd": str(self.root), "expiresAt": time.time() + 1})
        with mock.patch.object(worker, "require_current_independent", side_effect=worker.IndependentLaunchError("IN_JOB")), \
                mock.patch.object(worker, "_start_verified_child") as child:
            self.assertEqual(worker.worker_main(folder), 1)
        child.assert_not_called()

    def test_worker_never_cleans_arbitrary_directory(self):
        sentinel = self.root / "ready.json"
        sentinel.write_text("user-owned-file", encoding="utf-8")
        self.assertEqual(worker.worker_main(self.root), 1)
        self.assertEqual(sentinel.read_text(), "user-owned-file")
        self.assertFalse((self.root / "error.json").exists())

    def test_worker_missing_permit_never_starts_business(self):
        folder = self.root / ".independent-launch-timeout0"
        folder.mkdir()
        worker._write(folder, "request.json", {"schema": 1, "nonce": "a" * 64, "command": self.command,
                                               "cwd": str(self.root), "expiresAt": time.time() + 0.12})
        with mock.patch.object(worker, "require_current_independent", return_value=self.identity), \
                mock.patch.object(worker, "_start_verified_child") as child:
            self.assertEqual(worker.worker_main(folder), 1)
        child.assert_not_called()
        self.assertFalse(folder.exists())

    def test_worker_wrong_creation_time_in_permit_never_starts_business(self):
        folder = self.root / ".independent-launch-badgrant"
        folder.mkdir()
        worker._write(folder, "request.json", {"schema": 1, "nonce": "a" * 64, "command": self.command,
                                               "cwd": str(self.root), "expiresAt": time.time() + 1})
        worker._write(folder, "permit.json", {"schema": 1, "nonce": "a" * 64, "pid": self.identity["pid"],
                                              "creationTime": "reused-pid"})
        with mock.patch.object(worker, "require_current_independent", return_value=self.identity), \
                mock.patch.object(worker, "_start_verified_child") as child:
            self.assertEqual(worker.worker_main(folder), 1)
        child.assert_not_called()

    def test_base_python_wrapper_avoids_venv_redirector(self):
        with mock.patch.object(sys, "_base_executable", r"C:\base\python.exe", create=True), \
                mock.patch.object(Path, "is_file", return_value=False):
            self.assertEqual(worker._wrapper_command(self.root)[0], r"C:\base\python.exe")

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_command_cwd_environment_tampering_before_ready_is_not_authorized(self):
        for field, changed in (("command", self.command + ["unexpected-command"]),
                               ("cwd", str(self.root.parent)),
                               ("environment", {"__PYVENV_LAUNCHER__": str(Path(sys._base_executable))})):
            with self.subTest(field=field):
                def spawn(command, cwd, timeout):
                    folder = Path(command[-1])
                    request = worker._read(folder, "request.json")
                    request[field] = changed
                    (folder / "request.json").write_text(json.dumps(request), encoding="utf-8")
                    return 999
                def ready(folder, name, nonce, deadline):
                    self.assertEqual(name, "ready.json", "changed command must never reach grant")
                    return {"schema": 1, "nonce": nonce, **self.identity,
                            "request_sha256": worker._request_digest(worker._read(folder, "request.json"))}
                with mock.patch.object(worker, "_spawn_wmi", side_effect=spawn), \
                        mock.patch.object(worker, "_wait_message", side_effect=ready), \
                        mock.patch.object(worker, "process_identity", return_value=self.identity):
                    with self.assertRaisesRegex(worker.IndependentLaunchError, "DIGEST_MISMATCH") as raised:
                        worker.launch_independent(self.command, self.root)
                self.assertFalse(raised.exception.dispatched)
                self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_ready_digest_tampering_is_rejected_before_grant(self):
        self.launch_patches(ready={**self.identity, "request_sha256": "0" * 64})
        with self.assertRaisesRegex(worker.IndependentLaunchError, "DIGEST_MISMATCH") as raised:
            worker.launch_independent(self.command, self.root)
        self.assertFalse(raised.exception.dispatched)
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows launch contract")
    def test_started_digest_tampering_is_dispatched_unknown(self):
        self.launch_patches(started={"request_sha256": "0" * 64})
        with self.assertRaises(worker.IndependentLaunchError) as raised:
            worker.launch_independent(self.command, self.root)
        self.assertTrue(raised.exception.dispatched)

    def worker_request(self, folder):
        request = {"schema": 1, "nonce": "a" * 64, "command": self.command, "cwd": str(self.root),
                   "environment": {}, "expiresAt": time.time() + 1}
        worker._write(folder, "request.json", request)
        return request

    def test_permit_digest_tampering_never_starts_business(self):
        folder = self.root / ".independent-launch-digest00"
        folder.mkdir()
        request = self.worker_request(folder)
        permit = {"schema": 1, "nonce": request["nonce"], **self.identity, "request_sha256": "0" * 64}
        worker._write(folder, "permit.json", permit)
        with mock.patch.object(worker, "require_current_independent", return_value=self.identity), \
                mock.patch.object(worker, "_start_verified_child") as child:
            self.assertEqual(worker.worker_main(folder), 1)
        child.assert_not_called()

    def test_worker_rereads_exact_request_after_permit_before_start(self):
        for field, changed in (("command", self.command + ["unexpected-command"]),
                               ("cwd", str(self.root.parent)),
                               ("environment", {"__PYVENV_LAUNCHER__": self.command[0]})):
            with self.subTest(field=field):
                folder = self.root / ".independent-launch-reread00"
                folder.mkdir()
                request = self.worker_request(folder)
                digest = worker._request_digest(request)
                def permit(directory, name, nonce, deadline):
                    self.assertEqual(name, "permit.json")
                    changed_request = dict(request)
                    changed_request[field] = changed
                    (directory / "request.json").write_text(json.dumps(changed_request), encoding="utf-8")
                    return {"schema": 1, "nonce": nonce, **self.identity, "request_sha256": digest}
                with mock.patch.object(worker, "require_current_independent", return_value=self.identity), \
                        mock.patch.object(worker, "_wait_message", side_effect=permit), \
                        mock.patch.object(worker, "_start_verified_child") as child:
                    self.assertEqual(worker.worker_main(folder), 1)
                child.assert_not_called()
                self.assertFalse(folder.exists())

    def test_environment_nul_cannot_inject_another_variable(self):
        for environment in ({"__PYVENV_LAUNCHER__": self.command[0] + "\0UNEXPECTED=injected"},
                            {"__PYVENV_LAUNCHER__": "relative.exe"},
                            {"__PYVENV_LAUNCHER__": self.command[0], "UNEXPECTED": "injected"}, []):
            with self.subTest(environment=environment), self.assertRaises(worker.IndependentLaunchError):
                worker._validate_environment(environment)

    def test_duplicate_json_keys_cannot_have_ambiguous_digest(self):
        (self.root / "request.json").write_text('{"command":[],"command":["changed"]}', encoding="utf-8")
        with self.assertRaises(worker.IndependentLaunchError):
            worker._read(self.root, "request.json")

    @unittest.skipUnless(os.name == "nt", "Windows process inspection")
    def test_open_process_reports_missing_pid_distinct_from_access_denied(self):
        for error in (87, 5):
            with self.subTest(winerror=error):
                kernel = mock.Mock()
                kernel.OpenProcess.return_value = None
                with mock.patch.object(worker, "_kernel32", return_value=kernel), \
                        mock.patch.object(worker.ctypes, "get_last_error", return_value=error):
                    with self.assertRaises(worker.IndependentLaunchError) as raised:
                        worker.process_identity(12345)
                self.assertEqual(raised.exception.code, "INDEPENDENT_PROCESS_OPEN_FAILED")
                self.assertEqual(raised.exception.winerror, error)
                self.assertEqual(raised.exception.pid, 12345)
                kernel.CloseHandle.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Real Windows independence fixture")
class RealWindowsLaunchTests(unittest.TestCase):
    def test_rejected_suspended_child_never_executes_fixture(self):
        with tempfile.TemporaryDirectory(prefix="switchboard-suspended-test-") as temporary:
            root = Path(temporary).resolve()
            fixture = Path(__file__).with_name("independent_worker_fixture.py").resolve()
            inspected = []
            original = worker._handle_identity
            def reject(handle, pid):
                identity = original(handle, pid)
                inspected.append(identity)
                return {**identity, "inJob": True}
            with mock.patch.object(worker, "_handle_identity", side_effect=reject):
                with self.assertRaisesRegex(worker.IndependentLaunchError, "STILL_IN_JOB"):
                    worker._start_verified_child([sys.executable, str(fixture), "child", str(root)], root)
            self.assertEqual(len(inspected), 1)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    if not worker.process_identity(inspected[0]["pid"])["alive"]:
                        break
                except worker.IndependentLaunchError:
                    break
                time.sleep(0.04)
            else:
                self.fail("rejected never-executed child did not exit")
            self.assertEqual(list(root.iterdir()), [])

    def test_child_survives_launcher_exit_and_finishes_naturally(self):
        with tempfile.TemporaryDirectory(prefix="switchboard-independent-test-") as temporary:
            root = Path(temporary).resolve()
            fixture = Path(__file__).with_name("independent_worker_fixture.py").resolve()
            arguments = ["spaces here", 'quote"and\\', "中文", "$env:PATH", ";not-a-command"]
            launcher = subprocess.run([sys.executable, str(fixture), "launch", str(root), *arguments],
                                      capture_output=True, text=True, timeout=22, creationflags=0x08000000)
            self.assertEqual(launcher.returncode, 0, launcher.stderr)
            result = json.loads((root / "launch.json").read_text(encoding="utf-8"))
            owner = worker.process_identity(result["pid"])
            self.assertTrue(owner["alive"])
            self.assertFalse(owner["inJob"])
            self.assertEqual(owner["creationTime"], result["creationTime"])
            deadline = time.monotonic() + 6
            while not (root / "child.json").exists() and time.monotonic() < deadline:
                time.sleep(0.04)
            child = json.loads((root / "child.json").read_text(encoding="utf-8"))
            self.assertFalse(child["inJob"])
            self.assertEqual(child["args"], arguments)
            self.assertEqual(Path(child["prefix"]), Path(sys.prefix))
            self.assertEqual(Path(child["executable"]), Path(sys.executable))
            while not (root / "completed.json").exists() and time.monotonic() < deadline:
                time.sleep(0.04)
            self.assertTrue((root / "completed.json").is_file(), "bounded fixture must finish naturally")
            while time.monotonic() < deadline:
                try:
                    if not worker.process_identity(result["pid"])["alive"]:
                        break
                except worker.IndependentLaunchError:
                    break
                time.sleep(0.04)
            else:
                self.fail("independent wrapper did not exit after its child")
            self.assertEqual(list(root.glob(".independent-launch-*")), [])


if __name__ == "__main__":
    unittest.main()
