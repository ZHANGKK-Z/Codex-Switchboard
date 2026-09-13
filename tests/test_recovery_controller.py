"""Offline real Qt action-bus integration: preview is never authorization."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
import switchboard
import switchboard_modern_ui as ui
from recovery_ui import RecoveryPreviewDialog, RecoveryRecordsDialog
from tests.test_recovery_ui import preview_fixture, job_fixture


class RecoveryControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = ui.SwitchboardModernWindow(ui.demo_snapshot(), switchboard.Paths(Path(self.temp.name)))
        self.window.router_timer.stop()
        self.window.handoff_timer.stop()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_dialog(self, kind):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.app.processEvents()
            dialog = self.window.findChild(kind)
            if dialog is not None and dialog.isVisible() and not self.window._busy:
                return dialog
            time.sleep(0.01)
        self.fail("action bus did not present expected dialog")

    def test_selection_is_frozen_and_preview_never_enqueues(self):
        with mock.patch.object(ui.projection_recovery, "preview_recovery", return_value=preview_fixture()) as preview, mock.patch.object(ui.recovery_jobs, "enqueue_recovery") as enqueue:
            self.window.pages[1].recovery_button.click()
            dialog = self.wait_dialog(RecoveryPreviewDialog)
            preview.assert_called_once()
            enqueue.assert_not_called()
            dialog.accept()
            enqueue.assert_not_called()
            dialog.close()

    def test_explicit_checkbox_runs_enqueue_then_records_no_snapshot_refresh(self):
        with mock.patch.object(ui.projection_recovery, "preview_recovery", return_value=preview_fixture()), mock.patch.object(ui.recovery_jobs, "enqueue_recovery", return_value=job_fixture("waiting_exit")) as enqueue, mock.patch.object(ui.recovery_jobs, "list_jobs", return_value=[job_fixture("waiting_exit")]), mock.patch.object(self.window, "refresh_snapshot") as refresh:
            self.window.pages[1].recovery_button.click()
            dialog = self.wait_dialog(RecoveryPreviewDialog)
            dialog.consent.setChecked(True)
            dialog.confirm_button.click()
            records = self.wait_dialog(RecoveryRecordsDialog)
            enqueue.assert_called_once()
            self.assertIs(enqueue.call_args.kwargs["consent"], True)
            refresh.assert_not_called()
            records.close()

    def test_records_open_after_restart_without_any_execution(self):
        with mock.patch.object(ui.recovery_jobs, "list_jobs", return_value=[job_fixture("pending_native_replay")]), mock.patch.object(ui.recovery_jobs, "enqueue_recovery") as enqueue, mock.patch.object(ui.recovery_jobs, "reconcile_job") as reconcile:
            self.window.show_recoveries()
            dialog = self.wait_dialog(RecoveryRecordsDialog)
            enqueue.assert_not_called()
            reconcile.assert_not_called()
            self.assertEqual(dialog.jobs[0]["status"], "pending_native_replay")
            dialog.close()


if __name__ == "__main__":
    unittest.main()
