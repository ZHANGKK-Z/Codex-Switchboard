import os
import tempfile
import json
import sys
import subprocess
import base64
import time
from contextlib import contextmanager
import unittest
from pathlib import Path
from unittest import mock

from appserver_client import AppServerClient, AppServerTimeoutError
from handoff_client import HandoffClient, DISABLED_FEATURES, READ_ONLY
from handoff_bundle import HandoffValidationError
from tests.handoff_native_fixture import LoopbackResponses


@contextmanager
def native_fixture_directory(root):
    temporary = tempfile.TemporaryDirectory(dir=root, prefix="native-read-fixture-")
    try:
        yield temporary.name
    finally:
        # Windows can retain a just-executed sandbox image briefly after the
        # process tree exits. Retry cleanup, never suppress its final failure.
        deadline = time.monotonic() + 15
        while True:
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)


class HandoffClientTests(unittest.TestCase):
    def test_metadata_client_still_cannot_send_paid_turns(self):
        client = AppServerClient(codex_home=Path.cwd())
        self.assertNotIn("turn/start", client._ALLOWED_REQUESTS)
        self.assertNotIn("thread/start", client._ALLOWED_REQUESTS)
        guarded = HandoffClient(codex_home=Path.cwd())
        with self.assertRaises(HandoffValidationError):
            guarded._request("turn/start", {})

    def test_unverified_goal_scheduler_is_rejected_before_resume(self):
        for enabled in (True, None, "false", 0):
            with self.subTest(enabled=enabled):
                client = HandoffClient(codex_home=Path.cwd(), allow_inference=True)
                client.safe_config = {"features.goals": enabled}
                with mock.patch.object(client, "_request") as request:
                    with self.assertRaises(HandoffValidationError):
                        client.resume_source("old", cwd=str(Path.cwd()), provider="openai", model="test-model")
                    request.assert_not_called()

    def test_receipt_failure_prevents_submission(self):
        client = HandoffClient(codex_home=Path.cwd(), allow_inference=True)
        client._sessions["old"] = {"cwd": str(Path.cwd()), "provider": "openai", "model": "test-model"}
        def fail():
            raise OSError("disk full")
        with mock.patch.object(client, "read_thread", return_value={"thread": {"status": {"type": "idle"}}}), \
             mock.patch.object(client, "_request") as request:
            with self.assertRaises(OSError):
                client.send_turn("old", "handoff", {}, before_send=fail, accepted=lambda _: None, client_message_id="once")
            request.assert_not_called()

    def test_turn_start_is_sent_once_and_explicitly_readonly(self):
        client = HandoffClient(codex_home=Path.cwd(), allow_inference=True)
        client._sessions["old"] = {"cwd": str(Path.cwd()), "provider": "openai", "model": "test-model"}
        events = []
        def timeout(*args, **kwargs):
            events.append("sent")
            raise AppServerTimeoutError("lost response")
        with mock.patch.object(client, "read_thread", return_value={"thread": {"status": {"type": "idle"}}}), \
             mock.patch.object(client, "_request", side_effect=timeout) as request:
            with self.assertRaises(AppServerTimeoutError):
                client.send_turn("old", "handoff", {}, before_send=lambda: events.append("receipt"),
                                 accepted=lambda _: events.append("accepted"), client_message_id="once")
        self.assertEqual(events, ["receipt", "sent"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1]["sandboxPolicy"], READ_ONLY)
        self.assertFalse(client._inference_gate)

    def test_restored_dynamic_tools_and_approvals_are_rejected(self):
        client = HandoffClient(codex_home=Path.cwd())
        incoming = [{"id": "external", "method": "item/tool/call"},
                    {"id": "approval", "method": "item/commandExecution/requestApproval"},
                    {"method": "notice"}]
        with mock.patch.object(AppServerClient, "_next_message", side_effect=incoming), \
             mock.patch.object(client, "_write_message") as send:
            self.assertEqual(client._next_message(1), {"method": "notice"})
        self.assertEqual(send.call_count, 2)
        self.assertTrue(all("error" in call.args[0] for call in send.call_args_list))

    def test_text_acknowledgement_is_not_a_report(self):
        with self.assertRaises(HandoffValidationError):
            HandoffClient.report_from_items([{"type": "agentMessage", "text": "已完全理解并接手"}])

    @unittest.skipUnless(os.environ.get("CODEX_HANDOFF_PROTOCOL_SMOKE"), "opt-in isolated native protocol check")
    def test_installed_protocol_without_provider_or_production_home(self):
        binary = os.environ["CODEX_HANDOFF_PROTOCOL_SMOKE"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, work = root / "home", root / "work"
            home.mkdir()
            work.mkdir()
            (home / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
            client = HandoffClient(executable=binary, codex_home=home, cwd=work,
                                   request_timeout=30, allow_inference=False)
            try:
                client.start()
                client.initialize(capabilities={"experimentalApi": True})
                safety = client.prepare(str(work), ["openai"])
                self.assertEqual(safety["disabled_features"], list(DISABLED_FEATURES))
                committed = []
                result = client.create_clean(cwd=str(work), model="gpt-5.6-sol", before_send=lambda: None,
                                             created=committed.append)
                self.assertEqual(committed, [result["thread"]["id"]])
                self.assertFalse(result["thread"]["turns"])
                # Isolated fixture only: materialize a historical message with
                # the no-inference inject API. Production handoff never uses it.
                with mock.patch.object(client, "_ALLOWED_REQUESTS", client._ALLOWED_REQUESTS | {"thread/inject_items"}):
                    client._request("thread/inject_items", {"threadId": committed[0], "items": [
                        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Offline handoff fixture; do not run."}]}]})
                resumed = client.resume_source(committed[0], cwd=str(work), provider="openai", model="gpt-5.6-sol")
                self.assertEqual(resumed["thread"]["id"], committed[0])
            finally:
                client.close()

    @unittest.skipUnless(os.environ.get("CODEX_HANDOFF_PROTOCOL_SMOKE"), "opt-in loopback-only native protocol check")
    def test_reopened_thread_uses_normal_tool_and_permission_defaults(self):
        binary = os.environ["CODEX_HANDOFF_PROTOCOL_SMOKE"]
        with tempfile.TemporaryDirectory() as directory, LoopbackResponses([{"answer": "offline fixture"}]) as upstream:
            root = Path(directory)
            home, work = root / "home", root / "work"
            home.mkdir()
            work.mkdir()
            # Normal defaults differ from the handoff worker's temporary ones.
            config = ('model = "gpt-5.6-sol"\nmodel_provider = "fixture"\n'
                      'sandbox_mode = "danger-full-access"\napproval_policy = "on-request"\n'
                      '[features]\ngoals = false\nmemories = false\nplugins = true\n'
                      '[model_providers.fixture]\nname = "Offline fixture"\n'
                      f'base_url = "{upstream.base_url}"\nwire_api = "responses"\n'
                      'request_max_retries = 0\nstream_max_retries = 0\n')
            marker = root / "mcp-connected.txt"
            config += ('[mcp_servers.fixture_reader]\nenabled = true\n'
                       f'command = {json.dumps(sys.executable)}\n'
                       f'args = {json.dumps([str(Path(__file__).with_name("handoff_native_fixture.py")), "--mcp-server", str(marker)])}\n')
            (home / "config.toml").write_text(config, encoding="utf-8")
            client = HandoffClient(executable=binary, codex_home=home, cwd=work,
                                   request_timeout=30, allow_inference=True,
                                   temporary_overrides={"mcp_servers.fixture_reader.enabled": False,
                                                        "model_providers.fixture.request_max_retries": 0,
                                                        "model_providers.fixture.stream_max_retries": 0})
            try:
                client.start()
                client.initialize(capabilities={"experimentalApi": True})
                client.prepare(str(work), ["fixture"])
                response = client._request("thread/start", {"cwd": str(work), "modelProvider": "fixture", "model": "gpt-5.6-sol",
                    "sandbox": "read-only",
                    "ephemeral": False})
                ident = client._validate_session(response, thread_id=None, cwd=str(work), provider="fixture", model="gpt-5.6-sol", fresh=True)
                turn_id = client.send_turn(ident, "Return the synthetic fixture report only.",
                    {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False},
                    before_send=lambda: None, accepted=lambda _: None, client_message_id="offline-only")
                self.assertEqual(client.wait_turn(ident, turn_id, timeout=20)["report"], {"answer": "offline fixture"})
                self.assertFalse(marker.exists(), "temporary handoff must not connect MCP")
            finally:
                client.close()
            ordinary = AppServerClient(executable=binary, codex_home=home, cwd=work, request_timeout=30)
            try:
                ordinary.start()
                ordinary.initialize(capabilities={"experimentalApi": True})
                with mock.patch.object(ordinary, "_ALLOWED_REQUESTS", ordinary._ALLOWED_REQUESTS | {"thread/resume", "config/read", "mcpServerStatus/list"}):
                    normal = ordinary._request("config/read", {"cwd": str(work)})
                    self.assertIs(normal["config"]["features"]["plugins"], True)
                    restored = ordinary._request("thread/resume", {"threadId": ident, "excludeTurns": True})
                    mcp = ordinary._request("mcpServerStatus/list", {"threadId": ident})
                    self.assertTrue(any(item.get("name") == "fixture_reader" and item.get("tools") for item in mcp["data"]), mcp)
                self.assertTrue(marker.exists(), "ordinary reopened session must regain its configured MCP tools")
                self.assertEqual(restored["sandbox"]["type"], "dangerFullAccess")
                self.assertEqual(restored["approvalPolicy"], "on-request")
            finally:
                ordinary.close()
            reopened = AppServerClient(executable=binary, codex_home=home, cwd=work, request_timeout=30)
            try:
                reopened.start()
                reopened.initialize(capabilities={"experimentalApi": True})
                with mock.patch.object(reopened, "_ALLOWED_REQUESTS", reopened._ALLOWED_REQUESTS | {"thread/resume"}):
                    persisted = reopened._request("thread/resume", {"threadId": ident, "excludeTurns": True})
                self.assertEqual(persisted["sandbox"]["type"], "dangerFullAccess")
                self.assertEqual(persisted["approvalPolicy"], "on-request")
            finally:
                reopened.close()
            self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), config)
            self.assertEqual(len(upstream.requests), 1)

    @unittest.skipUnless(os.environ.get("CODEX_HANDOFF_PROTOCOL_SMOKE"), "opt-in native read-tool check")
    def test_native_readonly_turn_can_read_text_and_image_but_not_write(self):
        binary = os.environ["CODEX_HANDOFF_PROTOCOL_SMOKE"]
        fixture_root = Path(__file__).resolve().parents[1] / "artifacts"
        fixture_root.mkdir(exist_ok=True)
        with native_fixture_directory(fixture_root) as directory:
            root = Path(directory)
            if os.name == "nt":
                # Python's secure temporary directories are owner-only; that
                # is not representative of an inherited-ACL project folder.
                self.assertTrue(root.resolve().is_relative_to(fixture_root.resolve()))
                subprocess.run(["icacls", str(root), "/reset"], check=True, capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            home, work = root / "home", root / "work"
            home.mkdir()
            work.mkdir()
            marker = "HANDOFF-READ-ONLY-FIXTURE-VERIFIED"
            (work / "README.md").write_text(marker, encoding="utf-8")
            picture = work / "reference.png"
            picture.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
            forbidden_write = work / "must-not-be-written.txt"

            def command_call(payload, command):
                choices = []
                for tool in payload.get("tools", []):
                    if tool.get("type") == "namespace":
                        choices.extend((tool["name"], item) for item in tool.get("tools", []))
                    else:
                        choices.append((None, tool))
                matching = [(ns, item) for ns, item in choices if item.get("name") in {"exec_command", "shell_command", "shell"}]
                if not matching:
                    return {"__fixture_tool_call__": {"type": "custom_tool_call", "name": "exec", "namespace": "functions",
                        "input": "text(await tools.exec_command(" + json.dumps({"cmd": command, "workdir": str(work), "login": False, "max_output_tokens": 1000}) + "));"}}
                namespace, selected = matching[0]
                name = selected["name"]
                args = ({"cmd": command, "workdir": str(work), "max_output_tokens": 1000}
                        if name == "exec_command" else {"command": command, "workdir": str(work)})
                if name == "shell":
                    args["command"] = ["powershell.exe", "-NoProfile", "-Command", command] if os.name == "nt" else ["sh", "-c", command]
                call = {"name": name, "arguments": json.dumps(args)}
                if namespace:
                    call["namespace"] = namespace
                return {"__fixture_tool_call__": call}

            read_command = f"Get-Content -LiteralPath '{work / 'README.md'}'" if os.name == "nt" else "head -n 1 README.md"
            write_command = f"Set-Content -LiteralPath '{forbidden_write}' -Value 'must-not-exist'" if os.name == "nt" else "printf forbidden > must-not-be-written.txt"
            picture_call = {"__fixture_tool_call__": {"type": "custom_tool_call", "name": "exec", "namespace": "functions",
                "input": "const result = await tools.view_image(" + json.dumps({"path": str(picture)}) + "); image(result.image_url);"}}
            with LoopbackResponses([lambda p: command_call(p, read_command), lambda p: command_call(p, write_command),
                                    picture_call, {"answer": "read tool fixture finished"}]) as upstream:
                config = ('model = "gpt-5.6-sol"\nmodel_provider = "fixture"\napproval_policy = "on-request"\n'
                          '[model_providers.fixture]\nname = "Offline fixture"\n'
                          f'base_url = "{upstream.base_url}"\nwire_api = "responses"\n'
                          'request_max_retries = 0\nstream_max_retries = 0\n')
                (home / "config.toml").write_text(config, encoding="utf-8")
                client = HandoffClient(executable=binary, codex_home=home, cwd=work, request_timeout=30, allow_inference=True,
                                       temporary_overrides={"windows.sandbox": "unelevated"})
                try:
                    client.start()
                    client.initialize(capabilities={"experimentalApi": True})
                    client.prepare(str(work), ["fixture"])
                    native_request = client._request
                    def observed_request(method, *args, **kwargs):
                        result = native_request(method, *args, **kwargs)
                        if method == "command/exec":
                            self.assertEqual(result.get("exitCode"), 0, result)
                        return result
                    with mock.patch.object(client, "_request", side_effect=observed_request):
                        client.probe_read_access(str(work), [work / "README.md", picture])
                    response = client._request("thread/start", {"cwd": str(work), "modelProvider": "fixture", "model": "gpt-5.6-sol",
                        "sandbox": "read-only", "ephemeral": False})
                    ident = client._validate_session(response, thread_id=None, cwd=str(work), provider="fixture", model="gpt-5.6-sol", fresh=True)
                    turn = client.send_turn(ident, "Read README.md using the available local read tool.", {},
                        before_send=lambda: None, accepted=lambda _: None, client_message_id="read-only-native")
                    result = client.wait_turn(ident, turn, timeout=30)
                    outputs = [item for payload in upstream.payloads[1:] for item in payload.get("input", [])
                               if item.get("type") in {"function_call_output", "custom_tool_call_output"}]
                    self.assertTrue(any(marker in json.dumps(item) for item in outputs), outputs)
                    self.assertFalse(forbidden_write.exists(), "read-only model tools must not write project files")
                    self.assertIn(str(picture.resolve()), result["viewed_images"])
                finally:
                    client.close()


if __name__ == "__main__":
    unittest.main()
