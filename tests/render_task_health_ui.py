"""Render offline task-health states without reading the user's task data."""
import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication
import switchboard
import switchboard_modern_ui as ui
from task_health_ui import TaskHealthDialog
from tests.test_task_health_ui import report_fixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "segoeuib.ttf"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(ui.APP_STYLE)
    with tempfile.TemporaryDirectory() as folder:
        window = ui.SwitchboardModernWindow(ui.demo_snapshot(), switchboard.Paths(Path(folder)))
        window.router_timer.stop()
        window.handoff_timer.stop()
        window.switch_page(1)
        window.pages[1].setGraphicsEffect(None)
        window.show()
        app.processEvents()
        window.grab().save(str(args.output / "tasks.png"))
        dimensions = {}
        for kind in ("tool_failure", "lagging", "changing"):
            dialog = TaskHealthDialog(report_fixture(kind), window)
            dialog.show()
            app.processEvents()
            dialog.grab().save(str(args.output / f"{kind}.png"))
            dialog.scroll.verticalScrollBar().setValue(dialog.scroll.verticalScrollBar().maximum())
            app.processEvents()
            dialog.grab().save(str(args.output / f"{kind}-bottom.png"))
            dimensions[kind] = [dialog.width(), dialog.height()]
            dialog.close()
        window.close()
        print({"sizes": dimensions, "output": str(args.output.resolve())})


if __name__ == "__main__":
    main()
