import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path


import switchboard
import switchboard_exe
from tests.test_switchboard import create_binding_fixture

PYSIDE6_AVAILABLE = importlib.util.find_spec("PySide6") is not None
if PYSIDE6_AVAILABLE:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import switchboard_modern_ui as modern
    from PySide6.QtWidgets import QApplication
else:
    modern = None
    QApplication = None


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 preview runtime is not installed")
class ModernUISnapshotTests(unittest.TestCase):
    def test_updated_source_requires_reopening_only_switchboard(self):
        with mock.patch.object(modern, "_source_stamps", return_value=("changed",)):
            with self.assertRaisesRegex(RuntimeError, "不用重启 Codex"):
                modern.require_current_source()

    def test_partial_and_unknown_conversion_messages_do_not_offer_blind_retry(self):
        partial = modern.friendly_error(switchboard.PartialThreadConversionError("child-id", "pin failed"))
        self.assertIn("child-id", partial)
        self.assertIn("请勿重复", partial)
        unknown = modern.friendly_error(switchboard.ConversionOutcomeUnknownError("source", ["candidate"]))
        self.assertIn("candidate", unknown)
        self.assertIn("暂停重复", unknown)

    def test_modern_mutex_name_is_stable_and_path_free(self):
        first = Path(r"E:\Codex-Home")
        second = Path(r"E:\Other-Codex-Home")
        name = modern.modern_instance_mutex_name(first)

        self.assertEqual(name, modern.modern_instance_mutex_name(first))
        self.assertNotEqual(name, modern.modern_instance_mutex_name(second))
        self.assertNotIn("Codex-Home", name)

    def test_discover_codex_home_prefers_explicit_and_saved_valid_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            explicit = root / "explicit"
            explicit.mkdir()
            self.assertEqual(modern.discover_codex_home(explicit), explicit.resolve())

            saved = root / "saved"
            saved.mkdir()
            (saved / "config.toml").write_text("", encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(
                json.dumps({"codex_home": str(saved)}),
                encoding="utf-8",
            )
            original_settings = modern.ui_settings_path
            original_home = os.environ.pop("CODEX_HOME", None)
            try:
                modern.ui_settings_path = lambda: settings
                self.assertEqual(modern.discover_codex_home(), saved.resolve())
            finally:
                modern.ui_settings_path = original_settings
                if original_home is not None:
                    os.environ["CODEX_HOME"] = original_home

    def test_save_codex_home_preference_is_atomic_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "ui" / "settings.json"
            original_settings = modern.ui_settings_path
            try:
                modern.ui_settings_path = lambda: settings
                modern.save_codex_home_preference(root / "home")
            finally:
                modern.ui_settings_path = original_settings

            document = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(document, {"codex_home": str(root / "home")})
            self.assertEqual(list(settings.parent.glob("*.tmp-*")), [])

    def test_frozen_entry_routes_router_mode_without_loading_ui_main(self):
        with tempfile.TemporaryDirectory() as directory:
            called = []
            original = switchboard.run_router
            try:
                switchboard.run_router = lambda paths: called.append(paths.codex_home)
                result = switchboard_exe._router_main(
                    ["--router-process", "--home", directory]
                )
            finally:
                switchboard.run_router = original

            self.assertEqual(result, 0)
            self.assertEqual(called, [Path(directory).resolve()])

    def test_frozen_entry_routes_handoff_worker_without_starting_ui(self):
        with mock.patch.object(switchboard_exe.sys, "argv", ["app.exe", "--handoff-worker", "--home", "E:/fixture", "--operation", "test-id"]), \
             mock.patch("handoff_jobs.main", return_value=2) as worker:
            self.assertEqual(switchboard_exe.main(), 2)
            worker.assert_called_once_with(["--home", "E:/fixture", "--operation", "test-id"])

    def test_demo_snapshot_is_secret_free(self):
        rendered = json.dumps(modern.demo_snapshot(), ensure_ascii=False).casefold()

        self.assertNotIn("access_token", rendered)
        self.assertNotIn("bearer_token", rendered)
        self.assertNotIn("sk-", rendered)
        self.assertNotIn("cookie", rendered)

    def test_bootstrap_snapshot_allows_migration_without_existing_home(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = modern.bootstrap_snapshot(Path(directory) / "future-home")

        self.assertEqual(snapshot["tasks"], [])
        self.assertEqual(snapshot["counts"]["user_tasks"], 0)
        self.assertIn("重新登录", snapshot["official"]["label"])

    def test_compact_windows_path_removes_extended_prefix(self):
        display = modern._compact_windows_path(
            r"\\?\C:\Users\developer\Documents\Codex\2026-08-20\n-h-2"
        )

        self.assertEqual(display, r"…\Codex\2026-08-20\n-h-2")
        self.assertFalse(display.startswith("\\\\" + "?\\"))

    def test_migration_progress_events_have_user_facing_labels(self):
        text = modern._migration_progress_text(
            {"phase": "extract", "current": 3, "total": 9}
        )

        self.assertEqual(text, "正在恢复任务和资料… 3/9")

    def test_real_snapshot_adapter_does_not_rewrite_state(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            create_binding_fixture(
                paths,
                [
                    {
                        "id": "user-task",
                        "name": "用户任务",
                        "model_provider": "openai",
                    },
                    {
                        "id": "internal-task",
                        "name": None,
                        "parent_thread_id": "user-task",
                        "source": {
                            "subagent": {
                                "thread_spawn": {"agent_path": "/root/internal"}
                            }
                        },
                    },
                ],
            )
            tracked = [paths.profiles, paths.active, paths.provider_versions, paths.state_database]
            before = {path: path.read_bytes() for path in tracked if path.is_file()}
            original_login = modern._official_login_status
            original_router = modern._local_router_status
            try:
                modern._official_login_status = lambda _paths: {
                    "logged_in": True,
                    "label": "已登录 ChatGPT",
                    "detail": "官方额度",
                }
                modern._local_router_status = lambda _paths: {
                    "healthy": True,
                    "label": "运行正常",
                    "detail": "127.0.0.1:8765",
                }
                snapshot = modern.build_ui_snapshot(paths)
            finally:
                modern._official_login_status = original_login
                modern._local_router_status = original_router

            after = {path: path.read_bytes() for path in before}
            self.assertEqual(before, after)
            self.assertEqual(snapshot["counts"]["user_tasks"], 1)
            self.assertEqual(snapshot["counts"]["subagents_hidden"], 1)
            self.assertEqual(snapshot["tasks"][0]["title"], "用户任务")

    def test_operations_switch_official_in_isolated_home(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            operations = modern.SwitchboardOperations(paths)
            original_login = modern._official_login_status
            try:
                modern._official_login_status = lambda _paths: {
                    "logged_in": True,
                    "label": "已登录 ChatGPT",
                    "detail": "官方额度",
                }
                result = operations.switch_profile("official")
            finally:
                modern._official_login_status = original_login

            self.assertEqual(result["operation"], "profile_switch")
            self.assertEqual(switchboard.load_active(paths)["profile_id"], "official")

    def test_operations_same_provider_conversion_is_a_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            create_binding_fixture(
                paths,
                [{"id": "official-task", "name": "官方任务", "model_provider": "openai"}],
            )
            binding = switchboard.thread_provider_binding(paths, "official-task")
            task = modern._task_projection(
                {
                    **binding,
                    "family_id": "official-task",
                    "family_size": 1,
                    "family_head_id": "official-task",
                    "family_role": "head",
                    "display_name": "官方任务",
                }
            )

            result = modern.SwitchboardOperations(paths).convert_task(
                task,
                "official",
                None,
                progress=lambda _message: None,
                cancel_event=__import__("threading").Event(),
            )

            self.assertTrue(result["same_task"])
            self.assertEqual(result["thread"]["id"], "official-task")


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 preview runtime is not installed")
class ModernUIWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(modern.APP_STYLE)

    def test_window_builds_six_pages_and_task_search_filters(self):
        window = modern.SwitchboardModernWindow(modern.demo_snapshot())
        try:
            self.assertEqual(window.stack.count(), 6)
            window.select_page("tasks")
            self.assertEqual(window.stack.currentIndex(), 1)
            tasks_page = window.pages[1]
            self.assertIsInstance(tasks_page, modern.TasksPage)
            tasks_page.search.setText("示例任务 A")
            self.app.processEvents()
            self.assertEqual(tasks_page.count_label.text(), "1 项")
        finally:
            window.close()

    def test_handoff_dialog_requires_explicit_usage_consent_and_model(self):
        dialog = modern.HandoffDialog(modern.demo_snapshot()["tasks"][0], ["fixture-model"])
        try:
            ok = dialog.buttons.button(modern.QDialogButtonBox.StandardButton.Ok)
            self.assertFalse(ok.isEnabled())
            dialog.consent.setChecked(True)
            self.assertTrue(ok.isEnabled())
            selection = dialog.selection()
            self.assertTrue(selection["consent"])
            self.assertEqual(selection["target_model"], "fixture-model")
            dialog.model.clear()
            self.assertFalse(ok.isEnabled())
        finally:
            dialog.close()

    def test_handoff_page_shows_gaps_and_report_without_claiming_completion(self):
        page = modern.HandoffPage()
        try:
            page.apply_jobs([{"id": "fixture", "title": "Fixture task", "status": "needs_review", "worker_alive": False,
                "request": {}, "message": "Missing original image", "gaps": ["reference.png is missing"],
                "target_thread": {"thread_id": "target"}, "acceptance_text": "# Read-only report\nNeed original image before implementation."}])
            self.assertIn("reference.png is missing", page.detail.toPlainText())
            self.assertIn("# Read-only report", page.detail.toPlainText())
            self.assertTrue(page.open_button.isEnabled())
            self.assertTrue(page.resume_button.isEnabled())
            page.apply_jobs([{**page.jobs[0], "worker_alive": True}])
            self.assertFalse(page.open_button.isEnabled())
            self.assertFalse(page.resume_button.isEnabled())
        finally:
            page.close()

    def test_task_handoff_action_has_separate_signal_from_conversion(self):
        page = modern.TasksPage(modern.demo_snapshot())
        received = []
        page.handoff_requested.connect(lambda task: received.append(task["id"]))
        try:
            page.handoff_button.click()
            self.assertEqual(received, [page.selected_task["id"]])
        finally:
            page.close()

    def test_migration_page_has_export_receive_and_read_only_actions(self):
        page = modern.MigrationPage(modern.demo_snapshot())
        emitted = []
        page.export_requested.connect(lambda: emitted.append("export"))
        page.import_requested.connect(lambda: emitted.append("import"))
        page.inspect_requested.connect(lambda: emitted.append("inspect"))

        buttons = {button.text(): button for button in page.findChildren(modern.QPushButton)}
        buttons["创建迁移包"].click()
        buttons["选择迁移包"].click()
        buttons["查看迁移包"].click()
        self.app.processEvents()

        self.assertEqual(emitted, ["export", "import", "inspect"])

    def test_export_pack_excludes_rebuildable_projection_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog = modern.ExportPackDialog(Path(directory))
            try:
                self.assertFalse(dialog.include_projection.isChecked())
                self.assertIn("通常无需携带", dialog.include_projection.text())
            finally:
                dialog.close()

    def test_unhealthy_task_disables_conversion_button(self):
        snapshot = modern.demo_snapshot()
        snapshot["tasks"][0].update(
            {
                "history_health": "stalled",
                "history_label": "历史异常",
                "history_safe": False,
                "history_reason": "projection_duplicate_or_rewind",
            }
        )
        page = modern.TasksPage(snapshot)
        try:
            page.select_task(snapshot["tasks"][0])
            self.assertFalse(page.convert_button.isEnabled())
            self.assertEqual(page.convert_button.text(), "历史未通过检查")
        finally:
            page.close()

    def test_quick_projection_check_requires_deep_check_before_conversion(self):
        task = modern._task_projection(
            {
                "thread_id": "task-1",
                "display_name": "待检查任务",
                "provider_alias": "openai",
                "profile_label": "官方",
                "model": "gpt-test",
                "family_role": "head",
            },
            {
                "health": "unchecked",
                "reason": "rollout_integrity_not_checked",
                "safe_to_fork": False,
            },
        )
        page = modern.TasksPage({"tasks": [task]})
        try:
            page.select_task(task)
            self.assertEqual(task["history_label"], "待深度检查")
            self.assertTrue(page.convert_button.isEnabled())
            self.assertEqual(page.convert_button.text(), "检查并转换")
        finally:
            page.close()

    def test_pack_preview_renders_portable_task_index(self):
        dialog = modern.PackPreviewDialog(
            {
                "verified": True,
                "task_count": 1,
                "project_count": 0,
                "file_count": 2,
                "pack_size": 128,
                "tasks": [
                    {
                        "id": "01a0-preview-task",
                        "name": "迁移任务",
                        "model_provider": "openai",
                    }
                ],
            },
            allow_import=False,
        )
        try:
            rendered = "\n".join(
                item.text()
                for widget in dialog.findChildren(modern.QListWidget)
                for item in (widget.item(index) for index in range(widget.count()))
            )
            self.assertIn("迁移任务", rendered)
            self.assertIn("openai", rendered)
        finally:
            dialog.close()

    def test_import_dialog_previews_old_to_new_project_mapping(self):
        dialog = modern.ImportPackDialog(
            Path("demo.codexpack"),
            {
                "manifest": {"pack_id": "demo"},
                "projects": [
                    {
                        "name": "示例项目",
                        "directory_name": "demo-project",
                        "source_path": r"D:\Old\demo-project",
                    }
                ],
            },
        )
        try:
            self.assertIsNotNone(dialog.mapping)
            rendered = dialog.mapping.toPlainText()
            self.assertIn(r"D:\Old\demo-project", rendered)
            self.assertIn("demo-project", rendered)
        finally:
            dialog.close()

    def test_provider_card_emits_activation_and_configuration(self):
        profile = modern.demo_snapshot()["profiles"][1]
        card = modern.ProviderCard(profile)
        activations = []
        configurations = []
        card.activate_requested.connect(activations.append)
        card.configure_requested.connect(configurations.append)
        buttons = card.findChildren(modern.QPushButton)
        next(button for button in buttons if button.text() == "配置").click()
        next(button for button in buttons if button.text() == "设为默认").click()
        self.app.processEvents()

        self.assertEqual(configurations[0]["id"], "maylily")
        self.assertEqual(activations[0]["id"], "maylily")


if __name__ == "__main__":
    unittest.main()
