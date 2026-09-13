"""Offline recovery presentation contracts; no backend or production state."""
import importlib.util
import os
import unittest


PYSIDE6_AVAILABLE = importlib.util.find_spec("PySide6") is not None
if PYSIDE6_AVAILABLE:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QFrame, QLabel, QPushButton
    from recovery_ui import RecoveryPreviewDialog, RecoveryRecordsDialog, STATUS_LABELS


def preview_fixture(supported=True):
    return {
        "supported": supported,
        "reason": "projection_cursor_stalled" if supported else "source_changed",
        "message": "聊天原文仍在；可以预览历史读取位置。" if supported else "来源已变化，请重新检查。",
        "plan": {
            "root_task_id": "fixture-root-task",
            "segment_id": "fixture-history-segment",
            "rollout_path": r"E:\示例数据\sessions\示例历史.jsonl",
            "cursor": {"old_offset": 123456, "new_offset": 124789, "next_ordinal": 400},
            "raw_payload": "SECRET-RAW-PAYLOAD",
        },
        "payload": "SECRET-TOP-LEVEL-PAYLOAD",
    }


def job_fixture(status, index=1):
    return {"id": f"fixture-recovery-{index}", "status": status,
            "message": "原操作记录，尚不代表业务成功。", "plan": preview_fixture()["plan"],
            "output": "SECRET-RAW-OUTPUT", "command": "SECRET-COMMAND"}


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is not installed")
class RecoveryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()

    def dialog(self, value):
        self.dialogs.append(value)
        return value

    @staticmethod
    def rendered(dialog):
        return "\n".join(label.text() for label in dialog.findChildren(QLabel))

    def test_preview_requires_explicit_unchecked_consent_even_for_direct_accept(self):
        dialog = self.dialog(RecoveryPreviewDialog(preview_fixture()))
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        self.assertFalse(dialog.consent.isChecked())
        self.assertFalse(dialog.confirm_button.isEnabled())
        dialog.confirm_button.click()
        dialog.accept()
        self.assertFalse(dialog.confirmed)
        self.assertEqual(accepted, [])
        dialog.consent.setChecked(True)
        self.assertTrue(dialog.confirm_button.isEnabled())
        dialog.confirm_button.click()
        self.assertEqual(accepted, [True])
        self.assertTrue(dialog.confirmed)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_unchecking_consent_disables_confirmation_again(self):
        dialog = self.dialog(RecoveryPreviewDialog(preview_fixture()))
        dialog.consent.setChecked(True)
        dialog.consent.setChecked(False)
        self.assertFalse(dialog.confirm_button.isEnabled())
        dialog.accept()
        self.assertFalse(dialog.confirmed)

    def test_unsupported_and_truthy_non_boolean_never_offer_confirmation(self):
        for supported in (False, "true", 1, None):
            with self.subTest(supported=supported):
                dialog = self.dialog(RecoveryPreviewDialog(preview_fixture(supported)))
                self.assertIsNone(dialog.consent)
                self.assertIsNone(dialog.confirm_button)
                self.assertEqual(dialog.findChildren(QCheckBox), [])
                dialog.accept()
                self.assertFalse(dialog.confirmed)
                self.assertIn("当前不支持恢复", self.rendered(dialog))

    def test_preview_shows_only_whitelisted_fields_and_boundaries(self):
        dialog = self.dialog(RecoveryPreviewDialog(preview_fixture()))
        text = self.rendered(dialog)
        for expected in ("fixture-root-task", "fixture-history-segment", "示例历史.jsonl",
                         "123,456 字节", "124,789 字节", "不修改聊天原文", "不调用 Provider",
                         "自行退出并重开 Codex", "预览失效", "不代表业务成功"):
            self.assertIn(expected, text)
        self.assertNotIn("SECRET", text)

    def test_preview_copies_input_and_ignores_nested_display_payloads(self):
        preview = preview_fixture()
        preview["plan"]["rollout_path"] = {"output": "SECRET-NESTED"}
        dialog = self.dialog(RecoveryPreviewDialog(preview))
        preview["plan"]["root_task_id"] = "changed-id"
        self.assertEqual(dialog.preview["plan"]["root_task_id"], "fixture-root-task")
        self.assertNotIn("SECRET", self.rendered(dialog))

    def test_all_untrusted_labels_render_plain_text_not_html(self):
        preview = preview_fixture()
        markup = "<img src='https://example.invalid/private'><b>data</b>"
        preview.update(reason=markup, message=markup)
        preview["plan"].update(root_task_id=markup, segment_id=markup, rollout_path=markup)
        dialogs = [self.dialog(RecoveryPreviewDialog(preview)),
                   self.dialog(RecoveryRecordsDialog([{"id": markup, "status": markup,
                       "message": markup, "plan": preview["plan"]}]))]
        for dialog in dialogs:
            self.assertIn(markup, self.rendered(dialog))
            for label in dialog.findChildren(QLabel):
                self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
                self.assertFalse(label.openExternalLinks())

    def test_record_statuses_cancel_only_before_execution_and_never_retry(self):
        jobs = [job_fixture(status, index) for index, status in enumerate(STATUS_LABELS)]
        dialog = self.dialog(RecoveryRecordsDialog(jobs))
        cancelled, reconciled, refreshed = [], [], []
        dialog.cancel_requested.connect(cancelled.append)
        dialog.reconcile_requested.connect(reconciled.append)
        dialog.refresh_requested.connect(lambda: refreshed.append(True))
        self.assertEqual(cancelled + reconciled + refreshed, [])
        cancel_buttons = dialog.findChildren(QPushButton, "recoveryCancel")
        self.assertEqual({button.property("receiptId") for button in cancel_buttons},
                         {jobs[0]["id"], jobs[1]["id"]})
        for button in cancel_buttons:
            button.click()
        for button in dialog.findChildren(QPushButton, "recoveryReconcile"):
            button.click()
        dialog.refresh_button.click()
        self.assertEqual(cancelled, [jobs[0]["id"], jobs[1]["id"]])
        self.assertEqual(reconciled, [job["id"] for job in jobs])
        self.assertEqual(refreshed, [True])
        self.assertFalse(any(any(word in button.text() for word in ("重试", "再次执行", "开始恢复"))
                             for button in dialog.findChildren(QPushButton)))

    def test_records_keep_native_verification_separate_from_business(self):
        dialog = self.dialog(RecoveryRecordsDialog([
            job_fixture("verified"), job_fixture("pending_native_replay", 2),
            job_fixture("needs_reconciliation", 3), job_fixture("launch_unknown", 4)]))
        text = self.rendered(dialog)
        for expected in ("原生历史核验通过", "不代表业务成功", "自行重开 Codex", "只读核验",
                         "尚未核验通过", "不得重试", "后台启动结果未知"):
            self.assertIn(expected, text)
        self.assertNotIn("SECRET", text)
        self.assertEqual(dialog.findChildren(QPushButton, "recoveryCancel"), [])

    def test_records_update_replaces_cards_without_emitting_requests_and_limits_50(self):
        jobs = [job_fixture("waiting_exit", index) for index in range(60)]
        dialog = self.dialog(RecoveryRecordsDialog(jobs))
        self.assertEqual(len(dialog.jobs), 50)
        self.assertEqual(len(dialog.findChildren(QFrame, "recoveryRecord")), 50)
        requests = []
        dialog.cancel_requested.connect(requests.append)
        dialog.reconcile_requested.connect(requests.append)
        dialog.refresh_requested.connect(lambda: requests.append("refresh"))
        dialog.update_jobs([job_fixture("verified", 99)])
        self.assertEqual(requests, [])
        self.assertEqual(len(dialog.findChildren(QFrame, "recoveryRecord")), 1)
        self.assertEqual(dialog.findChildren(QPushButton, "recoveryCancel"), [])
        self.assertIn("fixture-recovery-99", self.rendered(dialog))
        self.assertNotIn("fixture-recovery-0", self.rendered(dialog))
        self.assertIn("最近 1 条", dialog.count_label.text())
        dialog.update_jobs([])
        self.assertIn("暂无恢复记录", self.rendered(dialog))

    def test_missing_receipt_id_disables_all_record_action_signals(self):
        dialog = self.dialog(RecoveryRecordsDialog([{"status": "queued"}]))
        for name in ("recoveryCancel", "recoveryReconcile"):
            self.assertFalse(dialog.findChild(QPushButton, name).isEnabled())


if __name__ == "__main__":
    unittest.main()
