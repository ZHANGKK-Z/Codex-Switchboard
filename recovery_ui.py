"""Recovery preview and receipts: presentation and intent signals only.

No backend imports, file access, process management, scheduling or Provider calls.
The caller owns fresh validation, persistence and all recovery actions.
"""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from task_health_ui import _label


STATUS_LABELS = {
    "queued": "已排队",
    "waiting_exit": "等待你退出 Codex",
    "revalidating": "正在重新核对预览",
    "backing_up": "正在备份",
    "advancing_cursor": "正在调整读取位置",
    "pending_native_replay": "等待原生历史重放",
    "verified": "原生历史核验通过",
    "cancelled": "已取消",
    "invalidated": "预览已失效",
    "needs_reconciliation": "结果待核对 · 不得重试",
    "failed": "恢复未完成",
    "launch_unknown": "后台启动结果未知 · 不得重试",
}

STATUS_GUIDANCE = {
    "queued": "尚未开始写入；此时可以取消。",
    "waiting_exit": "请先保存工作，再自行退出 Codex；工具不会强行关闭它。此时可以取消。",
    "revalidating": "正在重新检查来源与读取位置；出现变化会使本次预览失效。",
    "backing_up": "只有备份完成并通过检查，才允许调整读取位置。",
    "advancing_cursor": "仅调整读取位置，不修改聊天原文；请等待回执更新，不要重复执行。",
    "pending_native_replay": "请自行重开 Codex，等待原生历史重放，再点击「只读核验」。尚未核验通过。",
    "verified": "仅表示原生历史核验通过，不代表任务中的后台程序或业务操作成功。",
    "cancelled": "本次恢复已取消。此处只保留记录，不会重新执行。",
    "invalidated": "原文件或投影等状态发生变化，本次预览不再有效；此处不提供再次执行。",
    "needs_reconciliation": "写入结果尚未确认，不得重试。请先只读核验原操作，必要时人工检查回执与备份。",
    "failed": "请核对失败原因与原操作记录；此处不提供再次执行。",
    "launch_unknown": "无法确认后台是否已启动，不得重试。请先只读核验原操作，避免重复执行。",
}


def _value(value: object) -> str:
    """Never stringify arbitrary nested payloads into the UI."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    return "未提供"


def _offset(plan: dict, name: str) -> str:
    cursor = plan.get("cursor")
    value = cursor.get(name) if isinstance(cursor, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return f"{value:,} 字节"
    return _value(value)


def _setup_dialog(dialog: QDialog, title: str, width: int, height: int) -> QVBoxLayout:
    dialog.setWindowTitle(title)
    dialog.setModal(True)
    dialog.setMinimumSize(480, 360)
    screen = dialog.screen()
    if screen is not None:
        available = screen.availableGeometry()
        width = min(width, max(480, available.width() - 60))
        height = min(height, max(360, available.height() - 60))
    dialog.resize(width, height)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 22, 24, 18)
    layout.setSpacing(12)
    return layout


def _scroll_content() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 8, 0)
    layout.setSpacing(10)
    scroll.setWidget(content)
    return scroll, content, layout


def _field(layout: QVBoxLayout, caption: str, value: str) -> None:
    label = _label(f"{caption}：{value}", "secondary")
    label.setMinimumWidth(0)
    layout.addWidget(label)


class RecoveryPreviewDialog(QDialog):
    """A frozen preview. Accepted means consent intent, never execution."""

    def __init__(self, preview: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.preview = copy.deepcopy(preview)
        self.supported = self.preview.get("supported") is True
        self.consent: QCheckBox | None = None
        self.confirm_button: QPushButton | None = None
        self.confirmed = False
        layout = _setup_dialog(self, "历史恢复 · 只读预览", 740, 700)
        layout.addWidget(_label("HISTORY RECOVERY  /  只读预览", "eyebrow"))
        layout.addWidget(_label("恢复历史读取位置", "heroTitle"))
        summary = _label("可以请求安全恢复" if self.supported else "当前不支持恢复", "cardTitle")
        summary.setStyleSheet("color:#0071E3;" if self.supported else "color:#A65A00;")
        layout.addWidget(summary)
        self.scroll, _, content = _scroll_content()
        content.addWidget(_label(_value(self.preview.get("message"))))
        if self.preview.get("reason"):
            _field(content, "检查结果", _value(self.preview["reason"]))
        plan = self.preview.get("plan")
        if isinstance(plan, dict):
            card = QFrame()
            card.setProperty("card", True)
            fields = QVBoxLayout(card)
            fields.setContentsMargins(16, 14, 16, 14)
            fields.setSpacing(8)
            fields.addWidget(_label("本次目标", "cardTitle"))
            _field(fields, "根任务 ID", _value(plan.get("root_task_id")))
            _field(fields, "历史分段", _value(plan.get("segment_id")))
            _field(fields, "原文件", _value(plan.get("rollout_path", plan.get("path"))))
            _field(fields, "当前读取位置", _offset(plan, "old_offset"))
            _field(fields, "计划读取位置", _offset(plan, "new_offset"))
            content.addWidget(card)
        content.addWidget(_label("操作边界", "cardTitle"))
        content.addWidget(_label(
            "仅在备份并重新核对后调整历史读取位置；不修改聊天原文，不调用 Provider。\n"
            "你必须自行退出并重开 Codex，工具不会替你关闭或重启它。\n"
            "原文件、投影或其他受检状态发生变化时，本次预览失效，不沿用旧授权。\n"
            "调整读取位置后仍需原生历史重放与只读核验；这不代表业务成功。"
        ))
        content.addStretch(1)
        layout.addWidget(self.scroll, 1)
        if self.supported:
            self.consent = QCheckBox("我同意上述范围：先备份，再调整读取位置")
            self.consent.setChecked(False)
            self.consent.setObjectName("recoveryConsent")
            self.consent.setToolTip("不修改聊天原文，不调用 Provider；自行退出并重开 Codex；受检状态变化则本次预览失效。")
            layout.addWidget(self.consent)
            layout.addWidget(_label(
                "不修改聊天原文、不调用 Provider。请自行退出并重开 Codex；受检状态变化则本次预览失效。",
                "secondary",
            ))
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.close_button = QPushButton("取消" if self.supported else "关闭")
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button)
        if self.supported:
            self.confirm_button = QPushButton("确认并等待退出")
            self.confirm_button.setObjectName("recoveryConfirm")
            self.confirm_button.setProperty("variant", "primary")
            self.confirm_button.setStyleSheet(
                "QPushButton:disabled { background:#E5E5EA; color:#96969C; border:1px solid #E5E5EA; }"
            )
            self.confirm_button.setEnabled(False)
            self.confirm_button.setAutoDefault(False)
            self.confirm_button.clicked.connect(self.accept)
            self.consent.toggled.connect(self.confirm_button.setEnabled)
            actions.addWidget(self.confirm_button)
        layout.addLayout(actions)

    def accept(self) -> None:
        # Also guard direct calls / keyboard acceptance, not just button clicks.
        if not self.supported or self.consent is None or not self.consent.isChecked():
            return
        self.confirmed = True
        super().accept()

    def reject(self) -> None:
        self.confirmed = False
        super().reject()


class RecoveryRecordsDialog(QDialog):
    """Displays at most 50 receipts supplied newest-first by the caller."""

    refresh_requested = Signal()
    cancel_requested = Signal(str)
    reconcile_requested = Signal(str)

    def __init__(self, jobs: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.jobs: list[dict] = []
        layout = _setup_dialog(self, "历史恢复 · 操作记录", 780, 740)
        layout.addWidget(_label("HISTORY RECOVERY  /  操作记录", "eyebrow"))
        layout.addWidget(_label("恢复记录", "heroTitle"))
        self.count_label = _label("", "secondary")
        layout.addWidget(self.count_label)
        self.scroll, _, self.records_layout = _scroll_content()
        layout.addWidget(self.scroll, 1)
        layout.addWidget(_label(
            "只读核验不会重新执行恢复。结果未知时不得重试。\n"
            "「原生历史核验通过」只说明历史一致，不代表业务成功。", "secondary"
        ))
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新记录")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.setProperty("variant", "primary")
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self.update_jobs(jobs)

    def update_jobs(self, jobs: list[dict]) -> None:
        """Replace presentation only; never query, reconcile, cancel or enqueue."""
        self.jobs = copy.deepcopy([job for job in jobs if isinstance(job, dict)][:50])
        while self.records_layout.count():
            item = self.records_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.count_label.setText(f"最近 {len(self.jobs)} 条 · 最多显示 50 条 · 以本次刷新为准")
        if not self.jobs:
            self.records_layout.addWidget(_label("暂无恢复记录。这里不会自动创建操作。"))
        for job in self.jobs:
            self._add_record(job)
        self.records_layout.addStretch(1)

    def _add_record(self, job: dict) -> None:
        status = job.get("status") if isinstance(job.get("status"), str) else ""
        job_id = job.get("id") if isinstance(job.get("id"), str) else ""
        card = QFrame()
        card.setProperty("card", True)
        card.setObjectName("recoveryRecord")
        card.setProperty("receiptId", job_id)
        content = QVBoxLayout(card)
        content.setContentsMargins(16, 14, 16, 14)
        content.setSpacing(7)
        content.addWidget(_label(STATUS_LABELS.get(status, "状态未知 · 不得重试"), "cardTitle"))
        _field(content, "状态", status or "未知")
        _field(content, "操作 ID", job_id or "未提供")
        content.addWidget(_label(_value(job.get("message"))))
        plan = job.get("plan")
        if isinstance(plan, dict):
            _field(content, "根任务 ID", _value(plan.get("root_task_id")))
            _field(content, "历史分段", _value(plan.get("segment_id")))
        content.addWidget(_label(STATUS_GUIDANCE.get(
            status, "无法确认原操作结果，不得重试；请先只读核验原操作。"
        ), "secondary"))
        actions = QHBoxLayout()
        actions.addStretch(1)
        if status in {"queued", "waiting_exit"}:
            cancel_button = QPushButton("取消等待")
            cancel_button.setObjectName("recoveryCancel")
            cancel_button.setProperty("receiptId", job_id)
            cancel_button.setEnabled(bool(job_id))
            cancel_button.clicked.connect(
                lambda _checked=False, value=job_id: self.cancel_requested.emit(value)
            )
            actions.addWidget(cancel_button)
        check_button = QPushButton("只读核验")
        check_button.setObjectName("recoveryReconcile")
        check_button.setProperty("receiptId", job_id)
        check_button.setEnabled(bool(job_id))
        check_button.clicked.connect(
            lambda _checked=False, value=job_id: self.reconcile_requested.emit(value)
        )
        actions.addWidget(check_button)
        content.addLayout(actions)
        self.records_layout.addWidget(card)
