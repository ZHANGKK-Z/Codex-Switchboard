import json
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

import sys as _sys

ROOT = Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(ROOT))

from appserver_client import (
    AppServerClient,
    AppServerPostCommitValidationError,
    AppServerTimeoutError,
    AppServerValidationError,
)


class AppServerClientTests(unittest.TestCase):
    def test_fork_timeout_retains_only_matching_notification_candidate(self):
        client = AppServerClient(codex_home=Path.cwd(), fork_timeout=0.01)
        notification = {"method": "thread/started", "params": {"thread": {
            "id": "child", "forkedFromId": "source"}}}
        with mock.patch.object(client, "_ensure_initialized"), \
             mock.patch.object(client, "_write_message") as write, \
             mock.patch.object(client, "_next_message", side_effect=[notification, AppServerTimeoutError("late")]):
            with self.assertRaises(AppServerTimeoutError) as caught:
                client.fork_thread("source", model_provider="openai")
        self.assertEqual(caught.exception.method, "thread/fork")
        self.assertEqual(caught.exception.request_id, 1)
        self.assertEqual(caught.exception.created_thread_id, "child")
        self.assertTrue(write.call_args.args[0]["params"]["excludeTurns"])
        self.assertTrue(write.call_args.args[0]["params"]["deferGoalContinuation"])

    def test_notifications_cannot_extend_absolute_deadline(self):
        client = AppServerClient(codex_home=Path.cwd())
        with mock.patch.object(client, "_ensure_initialized"), \
             mock.patch.object(client, "_write_message"), \
             mock.patch("appserver_client.time.monotonic", side_effect=[1.0, 1.01, 2.0]), \
             mock.patch.object(client, "_next_message", return_value={"method": "notice"}) as receive:
            with self.assertRaises(AppServerTimeoutError) as caught:
                client._request("thread/read", {"threadId": "source"}, timeout=0.1)
        self.assertEqual(receive.call_count, 1)
        self.assertEqual(caught.exception.method, "thread/read")
        self.assertIsNone(caught.exception.created_thread_id)

    def test_fork_has_separate_bounded_deadline_and_metadata_only_payload(self):
        client = AppServerClient(codex_home=Path.cwd(), request_timeout=2, fork_timeout=12)
        reply = {"thread": {"id": "child", "forkedFromId": "source", "cwd": str(Path.cwd()),
                            "modelProvider": "openai"}, "cwd": str(Path.cwd()), "modelProvider": "openai"}
        with mock.patch.object(client, "_request", return_value=reply) as request:
            client.fork_thread("source", model_provider="openai")
        self.assertEqual(request.call_args.kwargs["timeout"], 12)
        self.assertTrue(request.call_args.args[1]["excludeTurns"])
        self.assertNotIn("lastTurnId", request.call_args.args[1])
        self.assertEqual(client.request_timeout, 2)

    @unittest.skipUnless(sys.platform == "win32", "Windows path spelling regression")
    def test_thread_validation_accepts_extended_windows_cwd_spelling(self):
        conventional = r"E:\Codex-Projects\Codex\2026-08-16\v6-clean-handoff"
        AppServerClient._validate_thread(
            {
                "id": "thread-1",
                "cwd": conventional,
                "modelProvider": "openai",
            },
            "thread-1",
            rf"\\?\{conventional}",
            "openai",
        )
        AppServerClient._validate_thread(
            {
                "id": "thread-2",
                "cwd": r"\\server\share\workspace",
                "modelProvider": "custom",
            },
            "thread-2",
            r"\\?\UNC\server\share\workspace",
            "custom",
        )

    def test_initialize_snapshot_fork_and_config_write(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            cwd = Path(directory) / "workspace"
            home.mkdir()
            cwd.mkdir()
            script = Path(directory) / "fake_app_server.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json, os, sys
                    threads = {
                        "thread-1": {"id":"thread-1","name":"demo","cwd":os.environ["TEST_CWD"],"modelProvider":"custom"}
                    }
                    for line in sys.stdin:
                        msg = json.loads(line)
                        method = msg.get("method")
                        ident = msg.get("id")
                        if ident is None:
                            continue
                        if method == "initialize":
                            result = {"codexHome": os.environ["CODEX_HOME"], "platformFamily":"windows", "platformOs":"windows", "userAgent":"test"}
                        elif method == "thread/read":
                            result = {"thread": threads[msg["params"]["threadId"]]}
                        elif method == "thread/fork":
                            params = msg["params"]
                            thread = {
                                "id":"thread-2",
                                "name":"demo",
                                "cwd":params.get("cwd") or os.environ["TEST_CWD"],
                                "modelProvider":params["modelProvider"],
                                "forkedFromId":"thread-1",
                            }
                            threads["thread-2"] = thread
                            result = {
                                "thread":thread,
                                "cwd":thread["cwd"],
                                "modelProvider":thread["modelProvider"],
                                "deferred":params.get("deferGoalContinuation"),
                                "threadSource":params.get("threadSource"),
                            }
                        elif method == "thread/name/set":
                            params = msg["params"]
                            threads[params["threadId"]] = dict(
                                threads[params["threadId"]],
                                name=params["name"],
                            )
                            result = {}
                        elif method == "thread/metadata/update":
                            params = msg["params"]
                            threads[params["threadId"]] = dict(
                                threads[params["threadId"]],
                                isPinned=params["isPinned"],
                            )
                            result = {"thread": threads[params["threadId"]]}
                        elif method == "thread/list":
                            result = {"data": list(threads.values()), "nextCursor": None}
                        elif method == "thread/archive":
                            result = {}
                        elif method == "thread/unarchive":
                            result = {"thread": threads[msg["params"]["threadId"]]}
                        elif method == "config/batchWrite":
                            result = {"filePath": os.path.join(os.environ["CODEX_HOME"], "config.toml"), "status":"ok", "version":"2"}
                        else:
                            result = {}
                        print(json.dumps({"id": ident, "result": result}), flush=True)
                    """
                ),
                encoding="utf-8",
            )
            env = {"TEST_CWD": str(cwd)}
            with AppServerClient(
                executable=sys.executable,
                command_args=(str(script),),
                codex_home=home,
                env=env,
                request_timeout=5,
            ) as client:
                self.assertEqual(client.snapshot_thread("thread-1")["name"], "demo")
                result = client.fork_thread(
                    "thread-1",
                    model_provider="openai",
                    expected_cwd=cwd,
                )
                self.assertEqual(result["thread"]["id"], "thread-2")
                self.assertEqual(result["thread"]["modelProvider"], "openai")
                self.assertTrue(result["deferred"])
                self.assertEqual(result["threadSource"], "user")
                client.set_thread_name("thread-2", "copied task")
                self.assertEqual(client.snapshot_thread("thread-2")["name"], "copied task")
                pinned = client.set_thread_pinned("thread-2", True)
                self.assertTrue(pinned["thread"]["isPinned"])
                self.assertEqual(len(client.list_threads()["data"]), 2)
                client.archive_thread("thread-1")
                self.assertEqual(client.unarchive_thread("thread-2")["thread"]["id"], "thread-2")
                config = client.write_config_batch(
                    [{"keyPath":"model_provider", "mergeStrategy":"replace", "value":"openai"}]
                )
                self.assertEqual(config["status"], "ok")

    def test_fork_rejects_non_persisted_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            cwd = Path(directory) / "workspace"
            home.mkdir()
            cwd.mkdir()
            script = Path(directory) / "fake_app_server.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json, os, sys
                    source = {"id":"thread-1","name":"legacy","cwd":os.environ["TEST_CWD"],"modelProvider":"openai"}
                    for line in sys.stdin:
                        msg = json.loads(line)
                        method = msg.get("method")
                        ident = msg.get("id")
                        if ident is None:
                            continue
                        if method == "initialize":
                            result = {"codexHome": os.environ["CODEX_HOME"], "platformFamily":"windows", "platformOs":"windows", "userAgent":"test"}
                        elif method == "thread/fork":
                            thread = dict(source, id="thread-2", forkedFromId="thread-1")
                            result = {"thread": thread, "cwd": thread["cwd"], "modelProvider":"custom"}
                        elif method == "thread/read":
                            result = {"thread": source}
                        else:
                            result = {}
                        print(json.dumps({"id": ident, "result": result}), flush=True)
                    """
                ),
                encoding="utf-8",
            )
            with AppServerClient(
                executable=sys.executable,
                command_args=(str(script),),
                codex_home=home,
                env={"TEST_CWD": str(cwd)},
                request_timeout=5,
            ) as client:
                with self.assertRaises(AppServerPostCommitValidationError) as raised:
                    client.fork_thread(
                        "thread-1",
                        model_provider="custom",
                        expected_cwd=cwd,
                    )
                self.assertIn("modelProvider mismatch", str(raised.exception))
                self.assertEqual(raised.exception.created_thread_id, "thread-2")

    def test_resume_is_outside_safe_client_scope(self):
        self.assertNotIn("thread/resume", AppServerClient._ALLOWED_REQUESTS)
        self.assertNotIn("thread/delete", AppServerClient._ALLOWED_REQUESTS)


if __name__ == "__main__":
    unittest.main()
