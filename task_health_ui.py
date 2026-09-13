"""Read-only health report presentation; no filesystem, processes or network."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)


SECTIONS = (
    ("history", "历史完整性"),
    ("latest_turn", "最近回合记录"),
    ("tools", "工具执行记录"),
    ("runtime", "实时运行状态"),
    ("business", "业务结果"),
)


def _label(text: object, role: str = "body") -> QLabel:
    label = QLabel(str(text or ""))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setProperty("role", role)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _checked_time(value: object) -> str:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(value or "未知")


def _record_times(report: dict) -> str:
    section = report.get("latest_turn") or {}
    stamps = []
    for field, caption in (("started_at", "开始"), ("completed_at", "结束")):
        value = section.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                instant = datetime.fromtimestamp(value / 1000 if value > 10**11 else value, timezone.utc)
                stamps.append(f"{caption} {_checked_time(instant.isoformat())}")
            except (OverflowError, OSError, ValueError):
                pass
    last = ((report.get("evidence") or {}).get("raw_tail") or {}).get("last_record_at")
    if last:
        stamps.append(f"原文件最后记录 {_checked_time(last)}")
    return "\n".join(stamps)


def _history_evidence(report: dict) -> str:
    history = report.get("history") or {}
    offset, size = history.get("projection_offset"), history.get("rollout_size")
    parts = []
    if all(isinstance(value, int) and not isinstance(value, bool) for value in (offset, size)):
        parts.append(f"投影位置 {offset:,} / {size:,} 字节")
    count = (report.get("evidence") or {}).get("segment_count")
    if isinstance(count, int) and not isinstance(count, bool):
        parts.append(f"{count} 个历史分段")
    return " · ".join(parts)


def report_text(report: dict) -> str:
    """Copy only the safe report contract, never raw commands or tool output."""
    lines = ["Codex Switchboard · 只读体检", str(report.get("title") or "未命名任务"),
             f"任务 ID：{report.get('thread_id', '')}",
             f"检查时间：{_checked_time(report.get('checked_at'))}",
             str(report.get("summary") or "检查结果未确认"), ""]
    for key, heading in SECTIONS:
        section = report.get(key) or {}
        lines += [f"{heading}：{section.get('label') or '未核验'}", str(section.get("detail") or ""), ""]
        if key == "latest_turn" and _record_times(report):
            lines.append(_record_times(report))
        if key == "history" and _history_evidence(report):
            lines.append(_history_evidence(report))
    for failure in (report.get("tools") or {}).get("failures", []):
        # Intentionally exclude output, arguments and command even if supplied.
        lines.append(f"失败记录：{failure.get('type', 'tool')} / {failure.get('id', '')} / "
                     f"exit={failure.get('exit_code')} / ordinal={failure.get('ordinal')}")
    lines += ["处理建议：", *[f"- {item}" for item in report.get("recommendations", [])]]
    lines += [f"注意：{item}" for item in report.get("warnings", [])]
    lines.append("本次未修复、未重启、未调用 Provider；回合结束不等于业务成功。")
    return "\n".join(lines)


class TaskHealthDialog(QDialog):
    recheck_requested = Signal()

    def __init__(self, report: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Result is an observation owned by this dialog, never an authorization
        # or a cache used by conversion / repair / handoff operations.
        self.report = json.loads(json.dumps(report, ensure_ascii=False))
        self.setWindowTitle("任务体检 · 只读")
        self.setModal(True)
        self.resize(740, 740)
        self.setMinimumSize(520, 420)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(740, available.width() - 60), min(740, available.height() - 60))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)
        layout.addWidget(_label("TASK CHECK  /  只读快照", "eyebrow"))
        layout.addWidget(_label(report.get("title") or "未命名任务", "heroTitle"))
        summary = _label(report.get("summary") or "检查结果未确认", "cardTitle")
        summary.setStyleSheet("color:#A65A00;" if report.get("tone") == "orange" else "color:#0071E3;")
        layout.addWidget(summary)
        checked = _checked_time(report.get("checked_at"))
        elapsed = report.get("elapsed_ms")
        duration = f" · {elapsed / 1000:.2f} 秒" if isinstance(elapsed, (int, float)) else ""
        layout.addWidget(_label(f"{checked}{duration} · 仅代表本次检查时的记录", "secondary"))

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(10)
        for key, heading in SECTIONS:
            section = report.get(key) or {}
            card = QFrame()
            card.setProperty("card", True)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 16, 12)
            card_layout.setSpacing(5)
            row = QHBoxLayout()
            row.addWidget(_label(heading, "secondary"))
            row.addStretch(1)
            row.addWidget(_label(section.get("label") or "未核验", "cardTitle"), 1)
            card_layout.addLayout(row)
            card_layout.addWidget(_label(section.get("detail")))
            if key == "latest_turn" and _record_times(report):
                card_layout.addWidget(_label(_record_times(report), "secondary"))
            if key == "history" and _history_evidence(report):
                card_layout.addWidget(_label(_history_evidence(report), "secondary"))
            if key == "tools":
                for failure in section.get("failures", [])[:5]:
                    # Locate in Codex without copying potentially secret argv.
                    evidence = (f"记录 …{str(failure.get('id') or '')[-16:]}"
                                f" · 退出码 {failure.get('exit_code')}"
                                f" · 序号 {failure.get('ordinal')}")
                    card_layout.addWidget(_label(evidence, "secondary"))
            content_layout.addWidget(card)
        content_layout.addWidget(_label("建议下一步", "cardTitle"))
        for index, advice in enumerate(report.get("recommendations", []), 1):
            content_layout.addWidget(_label(f"{index}. {advice}"))
        for warning in report.get("warnings", []):
            content_layout.addWidget(_label(f"注意：{warning}", "secondary"))
        content_layout.addWidget(_label(f"任务 ID · {report.get('thread_id', '')}", "tiny"))
        content_layout.addStretch(1)
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(_label("不修复数据 · 不启动任务 · 不调用模型\n回合结束不等于业务成功；实时状态未知不代表任务异常。", "secondary"))
        actions = QHBoxLayout()
        self.copy_button = QPushButton("复制报告")
        self.copy_button.clicked.connect(self.copy_report)
        actions.addWidget(self.copy_button)
        actions.addStretch(1)
        self.recheck_button = QPushButton("重新体检")
        self.recheck_button.clicked.connect(self.recheck_requested.emit)
        actions.addWidget(self.recheck_button)
        close_button = QPushButton("关闭")
        close_button.setProperty("variant", "primary")
        close_button.clicked.connect(self.accept)
        actions.addWidget(close_button)
        layout.addLayout(actions)

    def copy_report(self) -> None:
        QApplication.clipboard().setText(report_text(self.report))
        self.copy_button.setText("已复制")
