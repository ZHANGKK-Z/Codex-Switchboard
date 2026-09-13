"""Render synthetic recovery dialogs only; never reads production state.

Run separately with QT_SCALE_FACTOR=1 and 1.5 for 100% / 150% images.
"""
import argparse
import ast
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication
from recovery_ui import RecoveryPreviewDialog, RecoveryRecordsDialog
from tests.test_recovery_ui import job_fixture, preview_fixture


def _app_style() -> str:
    # Read the established style without importing the main window or backend.
    source = Path(__file__).resolve().parents[1] / "switchboard_modern_ui.py"
    for node in ast.parse(source.read_text(encoding="utf-8-sig")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "APP_STYLE" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError("APP_STYLE was not found")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
        file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if file.is_file():
            QFontDatabase.addApplicationFont(str(file))
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(_app_style())
    captures = [
        ("preview-supported", RecoveryPreviewDialog(preview_fixture())),
        ("preview-unsupported", RecoveryPreviewDialog(preview_fixture(False))),
        ("records-waiting", RecoveryRecordsDialog([job_fixture("waiting_exit")])),
        ("records-reopen", RecoveryRecordsDialog([job_fixture("pending_native_replay")])),
        ("records-unknown", RecoveryRecordsDialog([job_fixture("needs_reconciliation"),
                                                 job_fixture("launch_unknown", 2)])),
        ("records-verified", RecoveryRecordsDialog([job_fixture("verified")])),
    ]
    sizes = {}
    for name, dialog in captures:
        dialog.show()
        app.processEvents()
        image = dialog.grab()
        if not image.save(str(args.output / f"{name}.png")):
            raise RuntimeError(f"Could not save {name}")
        sizes[name] = {"logical": [dialog.width(), dialog.height()],
                       "pixels": [image.width(), image.height()]}
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
    print({"scale": os.environ.get("QT_SCALE_FACTOR", "1"), "sizes": sizes,
           "output": str(args.output.resolve())})


if __name__ == "__main__":
    main()
