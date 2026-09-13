import json
import http.client
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import switchboard
import switchboard_ui


class SwitchboardUITests(unittest.TestCase):
    def test_profile_rows_are_secret_free_and_report_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"ciphertext")

            rows = switchboard_ui.profile_rows(paths)

            maylily = next(row for row in rows if row["id"] == "maylily")
            relay_b = next(row for row in rows if row["id"] == "relay-b")
            self.assertTrue(maylily["ready"])
            self.assertEqual(maylily["key_state"], "已配置")
            self.assertEqual(maylily["models"], ["gpt-5.6-sol"])
            self.assertEqual(maylily["model_count"], 1)
            self.assertFalse(relay_b["ready"])
            self.assertEqual(relay_b["key_state"], "未配置")
            self.assertTrue(all("secret" not in row and "token" not in row for row in rows))

    def test_summarize_result_does_not_render_account_payload(self):
        result = {
            "profile": {"id": "official", "label": "官方账号"},
            "active": {"revision": 7},
            "account": {"email": "person@example.test", "accessToken": "do-not-render"},
            "config": {"changed": True},
        }

        message = switchboard_ui.summarize_result("切换", result)

        self.assertIn("官方账号", message)
        self.assertIn("revision 7", message)
        self.assertNotIn("person@example.test", message)
        self.assertNotIn("do-not-render", message)

    def test_summarize_result_reports_source_and_forked_binding(self):
        message = switchboard_ui.summarize_result(
            "复制",
            {
                "profile": {"id": "maylily", "label": "Maylily"},
                "source_thread": {"id": "thread-1", "modelProvider": "openai"},
                "thread": {"id": "thread-2", "modelProvider": "custom"},
                "provider_alias": "custom",
            },
        )

        self.assertIn("旧任务仍保留：thread-1", message)
        self.assertIn("新任务：thread-2", message)
        self.assertIn("Provider：custom", message)

    def test_summarize_result_does_not_claim_completion_without_navigation(self):
        message = switchboard_ui.summarize_result(
            "切换",
            {
                "source_thread": {"id": "source"},
                "thread": {"id": "head", "modelProvider": "openai"},
                "provider_alias": "openai",
                "cleanup": {"source_archived": True, "head_pinned": True},
                "completion": {
                    "core_complete": True,
                    "navigation_launched": False,
                    "complete": False,
                },
            },
        )

        self.assertIn("部分完成", message)
        self.assertIn("置顶区", message)
        self.assertIn("未能自动打开", message)

    def test_maintenance_summaries_are_bounded_and_explicit(self):
        drift = switchboard_ui.config_projection_text(
            {"ready": False, "reasons": ["model_provider 与 active 版本不一致"]}
        )
        probe = switchboard_ui.model_probe_text(
            {
                "remote_count": 12,
                "supported_configured": ["gpt-a"],
                "missing_remote": ["gpt-b"],
            }
        )
        backups = switchboard_ui.backup_retention_text(
            {
                "entry_count": 8,
                "total_size": "16.00 GiB",
                "candidate_count": 2,
                "candidate_size": "8.00 GiB",
                "policy": {"keep_per_series": 2, "min_age_days": 3},
            }
        )

        self.assertIn("需要修复", drift)
        self.assertIn("gpt-b", probe)
        self.assertIn("不会自动删除", backups)

    def test_task_overview_rows_are_secret_free_and_show_frozen_config(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory) / "home")
            original = switchboard.thread_family_bindings
            received = {}
            try:
                def fake_family_bindings(*_args, **kwargs):
                    received.update(kwargs)
                    return [
                        {
                            "thread_id": "01abcdef-thread",
                            "display_name": "任务 A",
                            "profile_label": "Maylily",
                            "provider_alias": "custom-v2",
                            "model": "gpt-5.6-sol",
                            "archived": 0,
                            "is_pinned": 0,
                            "codex_pinned_index": 2,
                            "is_subagent": False,
                            "family_id": "family-root",
                            "family_size": 2,
                            "family_role": "head",
                            "cwd": str(paths.codex_home / "project"),
                            "secret": "must-not-copy",
                        }
                    ]

                switchboard.thread_family_bindings = fake_family_bindings
                rows = switchboard_ui.task_overview_rows(paths)
            finally:
                switchboard.thread_family_bindings = original

            rendered = json.dumps(rows, ensure_ascii=False)
            self.assertEqual(rows[0]["provider_alias"], "custom-v2")
            self.assertEqual(rows[0]["model"], "gpt-5.6-sol")
            self.assertIn("置顶", rows[0]["state"])
            self.assertIn("当前 head", rows[0]["state"])
            self.assertNotIn("must-not-copy", rendered)
            self.assertNotIn("secret", rendered)
            self.assertFalse(received["include_subagents"])
            self.assertEqual(received["sort_mode"], switchboard.THREAD_SORT_CODEX)

    def test_single_instance_mutex_name_is_stable_and_path_free(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "home-a"
            second = Path(directory) / "home-b"
            name = switchboard_ui.single_instance_mutex_name(first)

            self.assertEqual(name, switchboard_ui.single_instance_mutex_name(first))
            self.assertNotEqual(name, switchboard_ui.single_instance_mutex_name(second))
            self.assertNotIn(str(first), name)

    def test_friendly_error_explains_provider_mismatch(self):
        message = switchboard_ui.friendly_error_message(
            RuntimeError("thread/fork persisted modelProvider mismatch: expected custom, got openai")
        )

        self.assertIn("没有把目标 Provider 持久化", message)
        self.assertIn("原任务未改写", message)

    def test_friendly_error_explains_unreadable_history_and_candidates(self):
        message = switchboard_ui.friendly_error_message(
            switchboard.UnreadableThreadHistoryError(
                "broken-thread",
                ["readable-thread-1", "readable-thread-2"],
            )
        )

        self.assertIn("聊天原文件仍在", message)
        self.assertIn("再次白屏", message)
        self.assertIn("未发布 Provider", message)
        self.assertIn("readable-thread-1", message)
        self.assertIn("readable-thread-2", message)
        self.assertNotIn("broken-thread", message)

    def test_friendly_error_explains_wait_mode_without_exposing_details(self):
        message = switchboard_ui.friendly_error_message(
            RuntimeError(
                "switch-thread requires the Codex desktop/App Server to be closed; "
                "active_processes=1"
            )
        )
        self.assertIn("重新点击复制按钮", message)
        self.assertNotIn("active_processes", message)
        self.assertIn("Codex", message)

        cancelled = switchboard_ui.friendly_error_message(
            RuntimeError("thread switch wait was cancelled before publication")
        )
        self.assertIn("已取消等待", cancelled)

        permission = switchboard_ui.friendly_error_message(
            RuntimeError("failed to start app-server: [WinError 5] 拒绝访问。")
        )
        self.assertIn("Windows 拒绝启动 App Server", permission)
        self.assertNotIn("WinError 5", permission)

    def test_child_environment_pins_codex_home_and_runtime_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory) / "home")
            environment = switchboard_ui.child_environment(paths)

            self.assertEqual(Path(environment["CODEX_HOME"]), paths.codex_home)
            self.assertEqual(Path(environment["TEMP"]), paths.runtime_tmp)
            self.assertEqual(Path(environment["TMP"]), paths.runtime_tmp)
            self.assertTrue(paths.runtime_tmp.is_dir())

    def test_smoke_snapshot_is_json_safe_without_router(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            paths = switchboard.Paths(home)
            switchboard.initialize(paths)
            profiles = switchboard.load_profiles(paths)
            profiles["router"]["port"] = 1
            switchboard.atomic_write_json(paths.profiles, profiles)
            snapshot = switchboard_ui.smoke_snapshot(home)

            json.dumps(snapshot, ensure_ascii=False)
            self.assertEqual(snapshot["active_profile"], "maylily")
            self.assertEqual(len(snapshot["profiles"]), 3)
            self.assertIn("config_projection", snapshot)
            self.assertFalse(snapshot["router"]["healthy"])

    def test_router_command_targets_core_cli_not_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory) / "home")
            # Avoid creating a Tk root: inspect the command through an
            # uninitialised object because this is a pure path contract.
            ui = object.__new__(switchboard_ui.SwitchboardUI)
            ui.paths = paths
            command = ui._router_command()

            self.assertEqual(Path(command[2]).name, "switchboard.py")
            self.assertNotEqual(Path(command[2]).name, "switchboard_ui.py")
            self.assertEqual(command[-1], "router")

    def test_minimum_window_keeps_all_account_rows_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory) / "home")
            paths.switchboard.mkdir(parents=True)
            profiles = switchboard.default_profiles()
            profiles["router"]["port"] = 1
            switchboard.atomic_write_json(paths.profiles, profiles)
            switchboard.atomic_write_json(
                paths.active,
                {"profile_id": "maylily", "revision": 0, "changed_at": switchboard.utc_now()},
            )
            try:
                root = switchboard_ui.tk.Tk()
            except switchboard_ui.tk.TclError as exc:
                self.skipTest(f"Tk display is unavailable: {exc}")
            try:
                root.attributes("-alpha", 0.0)
                ui = switchboard_ui.SwitchboardUI(root, paths)
                root.geometry("900x690")
                root.update()
                row_boxes = [ui.tree.bbox(item) for item in ui.tree.get_children()]

                self.assertEqual(len(row_boxes), 3)
                self.assertTrue(all(box and box[3] >= 28 for box in row_boxes))
                self.assertGreaterEqual(ui.tree.winfo_height(), 140)
                self.assertGreaterEqual(ui.notebook.winfo_height(), 210)
            finally:
                if "ui" in locals():
                    ui.closed = True
                root.destroy()

    def test_router_health_is_local_and_does_not_require_a_key(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory) / "home")
            switchboard.initialize(paths)
            server = switchboard.ThreadingHTTPServer(("127.0.0.1", 0), switchboard.RouterHandler)
            server.paths = paths
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("GET", "/health?check=ui")
                response = connection.getresponse()
                body = json.loads(response.read().decode("utf-8"))
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(body["status"], "ok")

                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("HEAD", "/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"")
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_router_poll_schedules_local_recovery_when_a_route_is_published(self):
        class FakeVar:
            value = ""

            def set(self, value):
                self.value = value

        class FakeRoot:
            scheduled = []

            def after(self, delay, callback):
                self.scheduled.append((delay, callback))

        ui = object.__new__(switchboard_ui.SwitchboardUI)
        ui.closed = False
        ui.paths = switchboard.Paths(Path("C:/fake-codex-home"))
        ui.router_var = FakeVar()
        ui.root = FakeRoot()
        ui._router_recovery_running = False
        ui._router_recovery_failures = 0
        ui._router_next_recovery_at = 0.0
        ui._append_log = lambda _message: None
        scheduled = []
        ui._schedule_router_recovery = lambda: scheduled.append(True)
        original_required = switchboard.router_required
        original_health = switchboard_ui.router_health
        try:
            switchboard.router_required = lambda _paths: True
            switchboard_ui.router_health = lambda _paths, timeout=1.5: (
                False,
                f"connection refused after {timeout}",
            )

            ui._poll_router_health()
        finally:
            switchboard.router_required = original_required
            switchboard_ui.router_health = original_health

        self.assertEqual(scheduled, [True])
        self.assertEqual(ui.root.scheduled[0][0], 5000)


if __name__ == "__main__":
    unittest.main()
