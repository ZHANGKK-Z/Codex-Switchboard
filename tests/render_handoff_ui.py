"""Render secret-free handoff states; never reads production Codex state."""
import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication
import switchboard
import switchboard_modern_ui as ui


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "segoeuib.ttf"):
        file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if file.is_file():
            QFontDatabase.addApplicationFont(str(file))
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(ui.APP_STYLE)
    with tempfile.TemporaryDirectory() as temporary:
        window = ui.SwitchboardModernWindow(ui.demo_snapshot(), switchboard.Paths(Path(temporary)))
        window.handoff_timer.stop()
        window.router_timer.stop()
        window.refresh_handoffs = lambda: None
        window.show()
        window.select_page("tasks")
        dialog = ui.HandoffDialog(ui.demo_snapshot()["tasks"][0], ["gpt-5.6-sol", "gpt-5.6-terra"], window)
        dialog.workspace.setText(r"D:\示例项目")
        dialog.documents.addItem(r"D:\示例项目\CODEX_HANDOFF.md")
        dialog.documents.addItem(r"D:\示例项目\references\参考效果.png")

        def capture():
            app.processEvents()
            window.grab().save(str(args.output / "tasks.png"))
            dialog.show()
            app.processEvents()
            dialog.grab().save(str(args.output / "consent.png"))
            dialog.hide()
            window.select_page("handoff")
            page = window.pages[5]
            report = ("# 新会话接手验收报告\n\n交接检查通过，未继续业务实现。\n\n"
                      "## 当前目标\n继续商品详情页优化，先核对真实素材和页面状态。\n\n"
                      "## 逐项核验\nR1 · verified · 已核对当前需求与源码，剩余实施需用户确认。\n\n"
                      "## 必读资料核验\nD001 · verified · 已阅读交接文档与验证记录。\n"
                      "D002 · verified · 已实际查看参考效果图。\n\n"
                      "## 下一步\n用户确认方案后，再实施范围内的修改。")
            base = {"id": "offline-fixture", "title": "商品详情页 · 独立新会话接手", "worker_alive": False,
                "stage": "finished", "request": {"workspace": r"D:\示例项目", "source_thread_id": "original-demo-thread"},
                "target_thread": {"thread_id": "clean-demo-thread"}, "bundle": "fixture-only",
                "events": [{"message": "老 AI 已整理最新目标与必读清单"}, {"message": "冻结交接资料已生成，原文档保持不变"}], "gaps": []}
            page.apply_jobs([{**base, "status": "needs_review", "message": "请求已提交但结果未知，禁止自动重发。", "gaps": ["请核对原回合结果；继续操作只读取原回执。"]}])
            app.processEvents()
            page.setGraphicsEffect(None)
            window.grab().save(str(args.output / "needs-review.png"))
            page.apply_jobs([{**base, "status": "ready", "message": "交接检查通过；请查看报告，再决定下一步。", "acceptance_text": report}])
            app.processEvents()
            window.grab().save(str(args.output / "ready.png"))
            print({"window": [window.width(), window.height()], "dialog": [dialog.width(), dialog.height()],
                   "output": str(args.output.resolve())})
            app.quit()

        QTimer.singleShot(600, capture)
        app.exec()
        dialog.close()
        window.close()


if __name__ == "__main__":
    main()
