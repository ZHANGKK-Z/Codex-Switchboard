import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PYSIDE6_AVAILABLE = importlib.util.find_spec("PySide6") is not None
if PYSIDE6_AVAILABLE:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel
    import switchboard
    import switchboard_modern_ui as ui
    from task_health_ui import TaskHealthDialog, report_text


def report_fixture(kind="tool_failure"):
    report = {
        "schema_version": 1, "thread_id": "fixture-thread", "title": "检查示例集群状态",
        "checked_at": "2026-09-12T01:20:00+00:00", "elapsed_ms": 285,
        "summary": "历史已同步；最近回合有工具失败记录", "tone": "orange",
        "history": {"status": "healthy", "label": "历史已完整同步", "detail": "原始记录与桌面投影一致，继承历史边界检查通过。"},
        "latest_turn": {"status": "completed", "label": "最近回合已结束", "detail": "结束记录已保存；不代表后台程序或业务操作成功。"},
        "runtime": {"status": "unknown", "label": "实时运行态未核验", "detail": "未连接桌面实例的实时状态通道，不能从历史记录断定当前空闲或运行中。"},
        "business": {"status": "unverified", "label": "业务结果未核验", "detail": "本次只检查 Codex 任务记录，没有连接业务系统或调用 Provider。"},
        "tools": {"status": "failures", "label": "发现 1 条失败记录", "detail": "最近回合内的工具返回退出码 1。已有失败可能被后续步骤处理，请核对原命令结果。",
                  "failed_count": 1, "failures": [{"id": "exec-offline-fixture", "type": "commandExecution", "status": "failed", "exit_code": 1, "ordinal": 5798}]},
        "recommendations": ["在 Codex 中核对失败工具的具体结果，确认是否已由后续步骤恢复。", "没有发现需要修复历史的证据，不要重复执行可能收费的请求。"],
        "evidence": {}, "warnings": [],
    }
    if kind == "lagging":
        report.update(summary="历史尚未同步；最近回合记录可能不是最新")
        report["history"] = {"status": "stalled", "label": "历史投影落后", "detail": "原始记录仍在，桌面投影未到达当前文件末尾。一次落后不等于历史损坏。"}
        report["latest_turn"] = {"status": "unknown", "label": "最新回合未确认", "detail": "当前展示仅为已投影部分，不能据此确认最新回合是否结束。"}
        report["tools"] = {"status": "unknown", "label": "工具记录不完整", "detail": "历史投影落后，缺失部分尚不能核验。", "failures": []}
        report["recommendations"] = ["先在 Codex 中打开任务并刷新体检。", "若持续落后，保留原文件后再评估安全恢复；本次不会自动修复。"]
    elif kind == "changing":
        report.update(summary="检查期间有新活动，请重新体检", tone="blue")
        report["history"] = {"status": "changed", "label": "快照已变化", "detail": "读取前后历史文件或投影位置发生变化，本次不作损坏判断。"}
        report["recommendations"] = ["等待本轮活动稳定后重新体检，无需重启。"]
    return report


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is not installed")
class TaskHealthUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_health_action_available_even_when_conversion_blocked(self):
        snapshot = ui.demo_snapshot()
        snapshot["tasks"][0].update(history_safe=False, history_checkable=False, archived=True)
        page = ui.TasksPage(snapshot)
        try:
            page.select_task(snapshot["tasks"][0])
            captured = []
            page.diagnose_requested.connect(captured.append)
            self.assertTrue(page.diagnose_button.isEnabled())
            self.assertFalse(page.convert_button.isEnabled())
            page.diagnose_button.click()
            self.assertEqual(captured[0]["id"], snapshot["tasks"][0]["id"])
            self.assertIsNot(captured[0], page.selected_task)
        finally:
            page.close()

    def test_empty_selection_is_disabled(self):
        page = ui.TasksPage({"tasks": []})
        try:
            self.assertFalse(page.diagnose_button.isEnabled())
        finally:
            page.close()

    def test_report_keeps_runtime_business_and_tool_failure_separate(self):
        dialog = TaskHealthDialog(report_fixture())
        try:
            rendered = "\n".join(label.text() for label in dialog.findChildren(QLabel))
            for text in ("历史已完整同步", "发现 1 条失败记录", "实时运行态未核验", "业务结果未核验"):
                self.assertIn(text, rendered)
            self.assertFalse(any(button.text().startswith("修复") for button in dialog.findChildren(ui.QPushButton)))
        finally:
            dialog.close()

    def test_untrusted_title_is_plain_text_and_raw_output_is_not_copied(self):
        report = report_fixture()
        report["title"] = "<img src='https://example.invalid/secret'>"
        report["tools"]["failures"][0].update(command="SECRET-COMMAND", output="SECRET-OUTPUT")
        dialog = TaskHealthDialog(report)
        try:
            labels = dialog.findChildren(QLabel)
            self.assertTrue(all(label.textFormat() == Qt.TextFormat.PlainText for label in labels))
            copied = report_text(report)
            self.assertNotIn("SECRET-COMMAND", copied)
            self.assertNotIn("SECRET-OUTPUT", copied)
        finally:
            dialog.close()

    def test_copy_is_explicit_and_contains_observation_time(self):
        with mock.patch("task_health_ui.QApplication.clipboard") as clipboard:
            dialog = TaskHealthDialog(report_fixture())
            try:
                clipboard.assert_not_called()
                dialog.copy_button.click()
                text = clipboard.return_value.setText.call_args.args[0]
                self.assertIn("2026-09-12", text)
                self.assertIn("fixture-thread", text)
            finally:
                dialog.close()

    def test_recheck_only_emits_request(self):
        dialog = TaskHealthDialog(report_fixture("changing"))
        try:
            captured = []
            dialog.recheck_requested.connect(lambda: captured.append(True))
            dialog.recheck_button.click()
            self.assertEqual(captured, [True])
        finally:
            dialog.close()

    def test_record_times_render_without_claiming_live_state(self):
        report = report_fixture()
        report["latest_turn"].update(started_at=1789141819, completed_at=1789144765)
        report["evidence"] = {"raw_tail": {"last_record_at": "2026-09-11T16:39:25Z"}}
        text = report_text(report)
        self.assertIn("开始 ", text)
        self.assertIn("结束 ", text)
        self.assertIn("原文件最后记录", text)
        self.assertIn("实时运行态未核验", text)

    def test_diagnosis_is_background_read_only_action_with_frozen_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            window = ui.SwitchboardModernWindow(ui.demo_snapshot(), switchboard.Paths(Path(folder)))
            window.router_timer.stop()
            window.handoff_timer.stop()
            try:
                selected = {"id": "original-id", "title": "original title"}
                with mock.patch.object(window, "run_action") as run, mock.patch.object(ui.task_health, "diagnose_task", return_value=report_fixture()) as diagnose:
                    window.diagnose_task(selected)
                    selected["id"] = "changed-selection"
                    operation = run.call_args.args[1]
                    event = mock.Mock()
                    operation(mock.Mock(), event)
                    self.assertEqual(diagnose.call_args.args[1], "original-id")
                    self.assertTrue(run.call_args.kwargs["cancellable"])
                    self.assertNotIn("refresh", run.call_args.kwargs)
            finally:
                window.close()

    def test_button_runs_action_bus_and_opens_report_without_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            window = ui.SwitchboardModernWindow(ui.demo_snapshot(), switchboard.Paths(Path(folder)))
            window.router_timer.stop()
            window.handoff_timer.stop()
            try:
                with mock.patch.object(ui.task_health, "diagnose_task", return_value=report_fixture()) as diagnose, mock.patch.object(window, "refresh_snapshot") as refresh:
                    window.pages[1].diagnose_button.click()
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and window.findChild(TaskHealthDialog) is None:
                        self.app.processEvents()
                        time.sleep(0.01)
                    dialog = window.findChild(TaskHealthDialog)
                    self.assertIsNotNone(dialog)
                    self.assertTrue(dialog.isVisible())
                    self.assertFalse(window._busy)
                    diagnose.assert_called_once()
                    refresh.assert_not_called()
                    dialog.close()
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
