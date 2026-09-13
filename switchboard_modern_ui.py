"""Modern production UI for Codex Switchboard.

The window is a presentation/controller layer over :mod:`switchboard`, which
remains the only owner of Provider publication, DPAPI secrets, task forks,
archive semantics, locks, retries, and rollback.  No business state is copied
into this module.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QObject,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import switchboard
import portable_migration
import handoff_jobs
import task_health
from task_health_ui import TaskHealthDialog
import projection_recovery
import recovery_jobs
from recovery_ui import RecoveryPreviewDialog, RecoveryRecordsDialog


UI_VERSION = "1.4.0"
DEFAULT_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
WINDOW_TITLE = "Codex Switchboard"


def _source_stamps() -> tuple:
    """An in-memory loaded-code baseline; not a task/config revision."""
    if getattr(sys, "frozen", False):
        return ()
    folder = Path(__file__).resolve().parent
    try:
        return tuple((folder / name).stat().st_mtime_ns for name in
                     ("switchboard_modern_ui.py", "switchboard.py", "appserver_client.py", "history_chain.py",
                      "handoff_jobs.py", "handoff_client.py", "handoff_bundle.py",
                      "task_health.py", "task_health_ui.py", "projection_recovery.py",
                      "recovery_jobs.py", "recovery_ui.py", "independent_worker.py"))
    except OSError:
        return (-1,)


_LOADED_SOURCE_STAMPS = _source_stamps()


def require_current_source() -> None:
    if _source_stamps() != _LOADED_SOURCE_STAMPS:
        raise RuntimeError("切换器代码已更新。请完全关闭并重新打开切换器后再转换；不用重启 Codex。")


APP_STYLE = r"""
* {
    font-family: "Microsoft YaHei UI", "Segoe UI Variable Text", "Segoe UI";
    color: #1D1D1F;
}
QMainWindow, QWidget#appRoot, QWidget#pageSurface {
    background: #F5F5F7;
}
QFrame#sidebar {
    background: #FBFBFD;
    border-right: 1px solid #E6E6EB;
}
QLabel[role="brand"] {
    color: #111114;
    font-size: 17px;
    font-weight: 700;
}
QLabel[role="eyebrow"] {
    color: #86868B;
    font-size: 11px;
    font-weight: 600;
}
QLabel[role="pageTitle"] {
    color: #111114;
    font-size: 30px;
    font-weight: 700;
}
QLabel[role="pageSubtitle"] {
    color: #6E6E73;
    font-size: 13px;
}
QLabel[role="heroTitle"] {
    color: #111114;
    font-size: 24px;
    font-weight: 700;
}
QLabel[role="cardTitle"] {
    color: #1D1D1F;
    font-size: 15px;
    font-weight: 650;
}
QLabel[role="metric"] {
    color: #111114;
    font-size: 19px;
    font-weight: 700;
}
QLabel[role="body"] {
    color: #49494F;
    font-size: 13px;
}
QLabel[role="secondary"] {
    color: #86868B;
    font-size: 12px;
}
QLabel[role="tiny"] {
    color: #9A9AA1;
    font-size: 10px;
}
QFrame[card="true"] {
    background: #FFFFFF;
    border: 1px solid #E8E8ED;
    border-radius: 18px;
}
QFrame[hero="true"] {
    background: #FFFFFF;
    border: 1px solid #E5E5EA;
    border-radius: 22px;
}
QFrame[providerCard="true"] {
    background: #FFFFFF;
    border: 1px solid #E5E5EA;
    border-radius: 18px;
}
QFrame[providerCard="true"][active="true"] {
    background: #F8FBFF;
    border: 2px solid #0071E3;
}
QFrame[taskRow="true"] {
    background: #FFFFFF;
    border: 1px solid #EBEBEF;
    border-radius: 14px;
}
QFrame[taskRow="true"][selected="true"] {
    background: #F3F8FF;
    border: 1px solid #87BFFF;
}
QLabel[badge="true"] {
    border-radius: 9px;
    padding: 3px 8px;
    font-size: 10px;
    font-weight: 650;
}
QLabel[badge="true"][tone="blue"] {
    color: #0066CC;
    background: #EAF3FF;
}
QLabel[badge="true"][tone="green"] {
    color: #1C7B3A;
    background: #EAF7EE;
}
QLabel[badge="true"][tone="orange"] {
    color: #9A5A00;
    background: #FFF4DF;
}
QLabel[badge="true"][tone="gray"] {
    color: #68686E;
    background: #F0F0F3;
}
QPushButton {
    min-height: 36px;
    border-radius: 10px;
    padding: 0 14px;
    border: 1px solid #D9D9DF;
    background: #FFFFFF;
    color: #343439;
    font-size: 12px;
    font-weight: 600;
}
QPushButton:hover {
    background: #F7F7F9;
    border-color: #C9C9D0;
}
QPushButton:pressed {
    background: #EEEEF2;
}
QPushButton:disabled {
    color: #A6A6AC;
    background: #F4F4F6;
    border-color: #E5E5E9;
}
QPushButton[variant="primary"] {
    color: #FFFFFF;
    background: #0071E3;
    border: 1px solid #0071E3;
}
QPushButton[variant="primary"]:hover {
    background: #0077ED;
    border-color: #0077ED;
}
QPushButton[variant="ghost"] {
    background: transparent;
    border-color: transparent;
    color: #0066CC;
}
QPushButton[nav="true"] {
    min-height: 44px;
    border: none;
    border-radius: 12px;
    padding: 0 14px;
    text-align: left;
    background: transparent;
    color: #5E5E64;
    font-size: 13px;
    font-weight: 600;
}
QPushButton[nav="true"]:hover {
    background: #F0F0F4;
}
QPushButton[nav="true"]:checked {
    color: #0066CC;
    background: #EAF3FF;
}
QPushButton[segment="true"] {
    min-height: 30px;
    border-radius: 8px;
    padding: 0 11px;
    border: none;
    background: transparent;
    color: #74747A;
    font-size: 11px;
}
QPushButton[segment="true"]:checked {
    color: #1D1D1F;
    background: #FFFFFF;
}
QFrame#segmentShell {
    background: #EBEBEF;
    border-radius: 10px;
    padding: 2px;
}
QLineEdit, QComboBox {
    min-height: 40px;
    border-radius: 11px;
    border: 1px solid #DFDFE5;
    background: #FFFFFF;
    padding: 0 13px;
    color: #242428;
    selection-background-color: #B8D9FF;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #6AAEFF;
}
QComboBox::drop-down {
    width: 28px;
    border: none;
}
QComboBox QAbstractItemView {
    background: #FFFFFF;
    border: 1px solid #DFDFE5;
    selection-background-color: #EAF3FF;
    selection-color: #1D1D1F;
    padding: 5px;
}
QPlainTextEdit {
    border-radius: 11px;
    border: 1px solid #DFDFE5;
    background: #FFFFFF;
    padding: 10px;
    color: #343439;
}
QDialog, QMessageBox, QProgressDialog {
    background: #F5F5F7;
}
QCheckBox {
    color: #68686E;
    spacing: 7px;
    font-size: 11px;
}
QListWidget {
    background: transparent;
    border: none;
    outline: none;
}
QListWidget::item {
    background: transparent;
    border: none;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    width: 8px;
    background: transparent;
    margin: 3px 0;
}
QScrollBar::handle:vertical {
    min-height: 30px;
    border-radius: 4px;
    background: #C9C9CF;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QLabel#toast {
    color: #FFFFFF;
    background: #252529;
    border-radius: 12px;
    padding: 10px 16px;
    font-size: 12px;
    font-weight: 600;
}
"""


def modern_instance_mutex_name(home: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(home))).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"Local\\CodexSwitchboardModern-{digest}"


class ModernInstanceGuard:
    """Keep one interactive preview per CODEX_HOME; screenshots are exempt."""

    def __init__(self, home: Path) -> None:
        self.name = modern_instance_mutex_name(home)
        self._handle: int | None = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError()
        if int(kernel32.GetLastError()) == 183:
            kernel32.CloseHandle(handle)
            return False
        self._handle = int(handle)
        return True

    def close(self) -> None:
        if self._handle is not None and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


def focus_existing_preview() -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    window = user32.FindWindowW(None, WINDOW_TITLE)
    if not window:
        return False
    user32.ShowWindow(window, 9)
    user32.SetForegroundWindow(window)
    return True


def ui_settings_path() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    root = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return root / "Codex-Switchboard" / "settings.json"


def _looks_like_codex_home(path: Path) -> bool:
    return path.is_dir() and any(
        (path / marker).exists()
        for marker in ("config.toml", "state_5.sqlite", "sessions", "auth.json")
    )


def discover_codex_home(explicit: Path | None = None) -> Path | None:
    """Resolve a target-PC state root without assuming that an E: drive exists."""

    if explicit is not None:
        return explicit.expanduser().resolve()
    candidates: list[Path] = []
    configured = os.environ.get("CODEX_HOME")
    if configured:
        candidates.append(Path(configured))
    settings = ui_settings_path()
    try:
        document = json.loads(settings.read_text(encoding="utf-8"))
        saved = document.get("codex_home") if isinstance(document, dict) else None
        if isinstance(saved, str) and saved.strip():
            candidates.append(Path(saved))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        pass
    candidates.extend((Path.home() / ".codex", Path(r"E:\Codex-Home")))
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(os.fspath(resolved))
        if key in seen:
            continue
        seen.add(key)
        if _looks_like_codex_home(resolved):
            return resolved
    return None


def save_codex_home_preference(home: Path) -> None:
    target = ui_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps({"codex_home": os.fspath(home)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def activate_codex_home(home: Path) -> None:
    """Publish a user-selected data root for future Switchboard/Codex starts."""

    resolved = home.expanduser().resolve()
    if not _looks_like_codex_home(resolved):
        raise ValueError("导入目录还不是有效的 Codex 数据目录")
    if os.name == "nt":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            winreg.SetValueEx(key, "CODEX_HOME", 0, winreg.REG_SZ, os.fspath(resolved))
        # Tell Explorer that future processes should inherit the new user value.
        sender = ctypes.windll.user32.SendMessageTimeoutW
        sender.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        sender.restype = ctypes.c_void_p
        result = ctypes.c_ulong()
        sender(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, ctypes.byref(result))
    save_codex_home_preference(resolved)


def choose_codex_home(parent: QWidget | None = None) -> Path | None:
    QMessageBox.information(
        parent,
        "选择 Codex 数据目录",
        "没有自动找到 CODEX_HOME。\n\n"
        "已有数据时请选择包含 config.toml、state_5.sqlite 或 sessions 的目录；"
        "如果要接收迁移包，可取消目录选择后直接进入迁移页。",
    )
    selected = QFileDialog.getExistingDirectory(
        parent,
        "选择 CODEX_HOME",
        os.fspath(Path.home()),
        QFileDialog.Option.ShowDirsOnly,
    )
    if not selected:
        return None
    home = Path(selected).resolve()
    if not _looks_like_codex_home(home):
        QMessageBox.critical(parent, "目录不正确", "所选目录不像有效的 Codex 数据目录。")
        return None
    save_codex_home_preference(home)
    return home


def friendly_error(error: BaseException) -> str:
    message = str(error or "").strip()
    lowered = message.casefold()
    if isinstance(error, switchboard.PartialThreadConversionError):
        return f"已生成新任务 {error.thread_id}，但尚未完成核验。请勿重复创建。\n详情：{error.detail[:400]}"
    if isinstance(error, switchboard.ConversionOutcomeUnknownError):
        return message[:700]
    if "timed out waiting for app-server" in lowered:
        return f"等待本地 Codex 响应超时，不能据此判断是否已创建副本。请先核验，不要重复转换。\n{message[:200]}"
    if (
        "active_processes=" in lowered
        or "requires the codex desktop" in lowered
        or "writer_locks=" in lowered
    ):
        return "请关闭 Codex 主窗口后重试；切换器本身保持打开。"
    if "thread history projection is unreadable" in lowered:
        if getattr(error, "reason", "") in {"rollout_duplicate_ordinal", "rollout_ordinal_gap"}:
            segment = getattr(error, "segment_id", None)
            return ("检测到聊天原文件或继承历史存在重复/缺失序号，已阻止转换；原记录未修改。"
                    + (f"\n异常历史分段：{segment}" if segment else ""))
        return "这个任务的聊天原文件仍在，但桌面历史投影不可读；未执行 Provider 转换。"
    if "cancelled before publication" in lowered or "wait was cancelled" in lowered:
        return "操作已取消，没有创建任务副本或发布 Provider。"
    if "official chatgpt account is not available" in lowered or "尚未登录 chatgpt" in lowered:
        return "尚未登录官方 ChatGPT 账号，请先使用“登录或切换账号”。"
    if "relay key is not configured" in lowered or "key is not configured" in lowered:
        return "中转站 Key 尚未配置，请先在账号页加密保存 Key。"
    if "model is not configured" in lowered:
        return "所选模型不在该中转站已保存的模型列表中。"
    if "winerror 5" in lowered or "access is denied" in lowered:
        return "Windows 拒绝启动本地 Codex 辅助进程，请完全退出 Codex 后重试。"
    if "target" in lowered and ("empty" in lowered or "not empty" in lowered):
        return "目标目录已有内容；请选择新的空目录，现有资料不会被覆盖。"
    if "unsupported" in lowered and ("schema" in lowered or "version" in lowered):
        return "迁移包由不兼容的版本创建，请先更新 Switchboard。"
    if "integrity" in lowered or "hash mismatch" in lowered or "bad zip" in lowered:
        return "迁移包不完整或已损坏，未向本机写入任何内容。"
    if "unsafe archive" in lowered or "path traversal" in lowered:
        return "迁移包包含不安全路径，已拒绝读取且未写入本机。"
    return message[:500] or type(error).__name__


class ActionBus(QObject):
    completed = Signal(str, object)
    failed = Signal(str, object)
    progress = Signal(str, str)

    def submit(
        self,
        label: str,
        operation: Callable[[Callable[[str], None], threading.Event], Any],
        cancel_event: threading.Event,
    ) -> None:
        def worker() -> None:
            try:
                result = operation(
                    lambda message: self.progress.emit(label, str(message)),
                    cancel_event,
                )
            except Exception as exc:  # noqa: BLE001 - marshalled to the UI thread
                self.failed.emit(label, exc)
            else:
                self.completed.emit(label, result)

        threading.Thread(
            target=worker,
            name=f"switchboard-modern-{label}",
            daemon=True,
        ).start()


class SwitchboardOperations:
    """Thin controller over the existing core; it owns no business state."""

    def __init__(self, paths: switchboard.Paths) -> None:
        self.paths = paths
        self.router_process: subprocess.Popen[str] | None = None
        self._router_lock = threading.Lock()

    def ensure_router(self) -> dict[str, Any]:
        with self._router_lock:
            status = _local_router_status(self.paths)
            if status["healthy"]:
                return status
            log_path = self.paths.switchboard / "router-modern.stdout.log"
            err_path = self.paths.switchboard / "router-modern.stderr.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.paths.runtime_tmp.mkdir(parents=True, exist_ok=True)
            stdout = log_path.open("a", encoding="utf-8")
            stderr = err_path.open("a", encoding="utf-8")
            try:
                command = (
                    [
                        sys.executable,
                        "--router-process",
                        "--home",
                        os.fspath(self.paths.codex_home),
                    ]
                    if getattr(sys, "frozen", False)
                    else [
                        sys.executable,
                        "-u",
                        os.fspath(Path(__file__).with_name("switchboard.py")),
                        "--home",
                        os.fspath(self.paths.codex_home),
                        "router",
                    ]
                )
                self.router_process = subprocess.Popen(
                    command,
                    cwd=os.fspath(self.paths.codex_home),
                    env={
                        **os.environ,
                        "CODEX_HOME": os.fspath(self.paths.codex_home),
                        "TEMP": os.fspath(self.paths.runtime_tmp),
                        "TMP": os.fspath(self.paths.runtime_tmp),
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    text=True,
                )
            finally:
                stdout.close()
                stderr.close()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                status = _local_router_status(self.paths)
                if status["healthy"]:
                    return status
                time.sleep(0.2)
            raise RuntimeError("本地路由器启动失败，请查看 switchboard 路由日志。")

    def switch_profile(self, profile_id: str) -> dict[str, Any]:
        document = switchboard.load_profiles(self.paths)
        profile = switchboard.profile_by_id(document, profile_id)
        active = switchboard.load_active(self.paths)
        same_version = False
        if active.get("profile_id") == profile_id:
            try:
                version = switchboard.profile_for_provider_alias(
                    self.paths,
                    str(active.get("provider_alias") or ""),
                )
                same_version = profile.get("kind") == "official" or all(
                    str(version.get(field) or "") == str(profile.get(field) or "")
                    for field in ("base_url", "model", "key_ref")
                )
            except (RuntimeError, ValueError):
                same_version = False
        if same_version:
            if profile.get("kind") == "relay":
                self.ensure_router()
            return {"operation": "profile_repair", **switchboard.repair_config(self.paths)}
        if profile.get("kind") == "relay":
            key_ref = str(profile.get("key_ref") or "")
            ready = bool(
                profile.get("enabled")
                and profile.get("base_url")
                and profile.get("model")
                and key_ref
                and switchboard.key_path(self.paths, key_ref).is_file()
            )
            if not ready:
                raise RuntimeError("中转站尚未配置完成：需要地址、模型、启用状态和加密 Key。")
            self.ensure_router()
        elif _official_login_status(self.paths).get("logged_in") is not True:
            raise RuntimeError("尚未登录 ChatGPT 官方账号")
        return {
            "operation": "profile_switch",
            **switchboard.set_active_profile(
                self.paths,
                profile_id,
                allow_official=profile.get("kind") == "official",
            ),
        }

    def login_official(
        self,
        *,
        switch_account: bool,
        progress: Callable[[str], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        probe = switchboard.appserver_blocking_processes
        if probe(self.paths):
            progress("等待 Codex 退出；请关闭 Codex 主窗口…")

            def report(info: dict[str, Any]) -> None:
                blockers = int(info.get("blockers") or 0)
                stable = int(info.get("stable_empty_checks") or 0)
                required = int(info.get("required_empty_checks") or 2)
                progress(
                    f"仍有 {blockers} 个 Codex 进程，请关闭主窗口…"
                    if blockers
                    else f"正在确认 Codex 已稳定退出 {stable}/{required}…"
                )

            switchboard.wait_for_appserver_exit(
                self.paths,
                process_probe=probe,
                cancel_probe=cancel_event.is_set,
                progress_fn=report,
                poll_seconds=1.0,
                timeout_seconds=30 * 60,
            )
        progress("正在打开 OpenAI 官方登录页…")
        account = switchboard.login_official_account(
            self.paths,
            switch_account=switch_account,
            blocker_probe=probe,
        )
        active = switchboard.set_active_profile(
            self.paths,
            "official",
            allow_official=True,
        )
        if os.name == "nt":
            os.startfile("codex://")  # type: ignore[attr-defined]
        return {"operation": "official_login", "account": account, **active}

    def convert_task(
        self,
        task: dict[str, Any],
        target_profile: str,
        target_model: str | None,
        *,
        progress: Callable[[str], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        require_current_source()
        thread_id = str(task.get("id") or "")
        if not thread_id:
            raise ValueError("任务 ID 为空")
        current = switchboard.thread_provider_binding(self.paths, thread_id)
        if (
            switchboard.thread_binding_matches_profile(
                self.paths,
                current,
                target_profile,
                model=target_model,
            )
            and not bool(int(current.get("archived") or 0))
        ):
            result = switchboard.fork_thread_provider(
                self.paths,
                thread_id,
                target_profile,
                expected_cwd=str(task.get("cwd") or "") or None,
                blocker_probe=lambda _paths: [],
                archive_source=False,
                target_model=target_model,
                progress_callback=progress,
            )
            result["operation"] = "task_conversion"
            return result
        probe = switchboard.appserver_blocking_processes
        if probe(self.paths):
            progress("等待 Codex 退出；请关闭 Codex 主窗口…")

            def report(info: dict[str, Any]) -> None:
                blockers = int(info.get("blockers") or 0)
                stable = int(info.get("stable_empty_checks") or 0)
                required = int(info.get("required_empty_checks") or 2)
                progress(
                    f"仍有 {blockers} 个 Codex 进程，请关闭主窗口…"
                    if blockers
                    else f"正在确认 Codex 已稳定退出 {stable}/{required}…"
                )

            switchboard.wait_for_appserver_exit(
                self.paths,
                process_probe=probe,
                cancel_probe=cancel_event.is_set,
                progress_fn=report,
                poll_seconds=1.0,
                timeout_seconds=30 * 60,
            )
        profile = switchboard.profile_by_id(
            switchboard.load_profiles(self.paths),
            target_profile,
        )
        if profile.get("kind") == "relay":
            self.ensure_router()
        require_current_source()
        if cancel_event.is_set():
            raise RuntimeError("wait was cancelled")
        progress("正在验证聊天历史并创建无损副本…")
        result = switchboard.fork_thread_provider(
            self.paths,
            thread_id,
            target_profile,
            expected_cwd=str(task.get("cwd") or "") or None,
            blocker_probe=probe,
            archive_source=False,
            target_model=target_model,
            progress_callback=progress,
        )
        head = result.get("thread")
        head_id = str(head.get("id") or "") if isinstance(head, dict) else ""
        if (result.get("completion") or {}).get("core_complete") is False:
            raise switchboard.PartialThreadConversionError(head_id, "Provider 已绑定，但置顶或核验尚未全部完成")
        if head_id and os.name == "nt":
            os.startfile(f"codex://threads/{head_id}")  # type: ignore[attr-defined]
            result["navigation_launched"] = True
        result["operation"] = "task_conversion"
        return result

    def cleanup_family(
        self,
        task: dict[str, Any],
        *,
        progress: Callable[[str], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        thread_id = str(task.get("id") or "")
        probe = switchboard.appserver_blocking_processes
        if probe(self.paths):
            progress("等待 Codex 退出；请关闭 Codex 主窗口…")
            switchboard.wait_for_appserver_exit(
                self.paths,
                process_probe=probe,
                cancel_probe=cancel_event.is_set,
                poll_seconds=1.0,
                timeout_seconds=30 * 60,
            )
        progress("正在归档旧成员并保留当前 head…")
        result = switchboard.archive_thread_family(
            self.paths,
            thread_id,
            blocker_probe=probe,
        )
        head_id = str(result.get("head_thread_id") or "")
        if head_id and os.name == "nt":
            os.startfile(f"codex://threads/{head_id}")  # type: ignore[attr-defined]
        return result

    def configure_relay(self, values: dict[str, Any]) -> dict[str, Any]:
        return switchboard.configure_relay_profile(
            self.paths,
            str(values["profile_id"]),
            base_url=str(values["base_url"]),
            model=str(values["model"]),
            models=str(values["models"]),
            enabled=bool(values["enabled"]),
        )

    def save_relay_key(self, profile_id: str, secret: str) -> str:
        profile = switchboard.profile_by_id(
            switchboard.load_profiles(self.paths),
            profile_id,
        )
        key_ref = str(profile.get("key_ref") or "")
        if not key_ref:
            raise RuntimeError("这个中转站没有配置 Key 引用")
        switchboard.store_key(self.paths, key_ref, secret)
        return "Key 已通过 Windows DPAPI 加密保存"

    def import_models(self) -> dict[str, Any]:
        return switchboard.bundled_model_choices(self.paths)

    def generate_catalog(self, values: dict[str, Any]) -> dict[str, Any]:
        return switchboard.prepare_model_catalog(
            self.paths,
            str(values["profile_id"]),
            model=str(values["model"]),
            models=str(values["models"]),
        )

    def probe_models(self, profile_id: str) -> dict[str, Any]:
        return switchboard.probe_relay_models(self.paths, profile_id)

    def repair_config(self) -> dict[str, Any]:
        return switchboard.repair_config(self.paths)

    def backup_plan(self) -> dict[str, Any]:
        return switchboard.backup_retention_plan(self.paths)

    def create_migration_pack(
        self,
        destination: Path,
        projects: list[Path],
        *,
        include_projection: bool,
        progress: Callable[[str], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        probe = switchboard.appserver_blocking_processes
        if probe(self.paths):
            progress("等待 Codex 退出；请关闭 Codex 主窗口…")
            switchboard.wait_for_appserver_exit(
                self.paths,
                process_probe=probe,
                cancel_probe=cancel_event.is_set,
                progress_fn=lambda info: progress(
                    f"仍有 {int(info.get('blockers') or 0)} 个 Codex 进程，请关闭主窗口…"
                    if int(info.get("blockers") or 0)
                    else "正在确认 Codex 已稳定退出…"
                ),
                poll_seconds=1.0,
                timeout_seconds=30 * 60,
            )
        locks = switchboard.writer_locks(self.paths)
        if locks:
            raise RuntimeError(
                f"migration export requires all task writers to stop; writer_locks={len(locks)}"
            )
        if cancel_event.is_set():
            raise RuntimeError("wait was cancelled before migration package creation")
        return portable_migration.create_pack(
            self.paths.codex_home,
            destination,
            projects=projects,
            include_projection=include_projection,
            progress=lambda event: progress(_migration_progress_text(event)),
        )

    def inspect_migration_pack(self, package: Path) -> dict[str, Any]:
        return portable_migration.inspect_pack(package, verify=True)

    def import_migration_pack(
        self,
        package: Path,
        target_home: Path,
        project_root: Path | None,
        *,
        progress: Callable[[str], None],
    ) -> dict[str, Any]:
        return portable_migration.import_pack(
            package,
            target_home,
            project_root=project_root,
            progress=lambda event: progress(_migration_progress_text(event)),
        )


def _role(widget: QWidget, name: str) -> QWidget:
    widget.setProperty("role", name)
    return widget


def _human_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB"} else f"{value:.2f} {unit}"
        value /= 1024
    return "0 B"


def _migration_progress_text(event: Any) -> str:
    if not isinstance(event, dict):
        return str(event)
    phase = str(event.get("phase") or "")
    labels = {
        "planning": "正在盘点任务和资料…",
        "integrity": "正在检查聊天历史完整性…",
        "snapshot": "正在创建数据库一致性副本…",
        "archive": "正在写入迁移包…",
        "verify": "正在验证迁移包…",
        "inspect": "正在解析并验证迁移包…",
        "extract": "正在恢复任务和资料…",
        "database_check": "正在检查数据库并映射路径…",
        "publish": "正在安全发布新资料库…",
        "complete": "正在完成最后检查…",
    }
    label = labels.get(phase, "正在处理迁移数据…")
    current = int(event.get("current") or 0)
    total = int(event.get("total") or 0)
    return f"{label} {current}/{total}" if total else label


def _compact_windows_path(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.startswith("\\\\?\\UNC\\"):
        normalized = "\\\\" + normalized[8:]
    elif normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    if not normalized:
        return "工作区未知"
    parts = PureWindowsPath(normalized).parts
    if len(parts) <= 3:
        return normalized
    return "…\\" + "\\".join(parts[-3:])


def _local_router_status(paths: switchboard.Paths) -> dict[str, Any]:
    document = switchboard.load_profiles(paths)
    router = document.get("router", {})
    host = str(router.get("host", switchboard.DEFAULT_ROUTER_HOST))
    port = int(router.get("port", switchboard.DEFAULT_ROUTER_PORT))
    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection(host, port, timeout=0.35)
        connection.request("GET", "/health", headers={"Cache-Control": "no-cache"})
        response = connection.getresponse()
        body = response.read(4096).decode("utf-8", errors="replace")
        payload = json.loads(body) if response.status == 200 else {}
        healthy = payload.get("status") == "ok"
    except (OSError, ValueError, json.JSONDecodeError):
        healthy = False
    finally:
        if connection is not None:
            connection.close()
    return {
        "healthy": healthy,
        "label": "运行正常" if healthy else "等待自动恢复",
        "detail": f"{host}:{port}",
    }


def _official_login_status(paths: switchboard.Paths) -> dict[str, Any]:
    """Return a bounded login label without preserving CLI account output."""

    try:
        executable = switchboard.resolve_appserver_executable(paths=paths)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = os.fspath(paths.codex_home)
        runtime_tmp = paths.runtime_tmp if paths.runtime_tmp.is_dir() else paths.codex_home
        environment["TEMP"] = os.fspath(runtime_tmp)
        environment["TMP"] = os.fspath(runtime_tmp)
        completed = subprocess.run(
            [executable, "login", "status"],
            cwd=os.fspath(paths.codex_home),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        normalized = (completed.stdout or "").casefold()
        logged_in = completed.returncode == 0 and (
            "logged in" in normalized or "chatgpt" in normalized
        )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return {"logged_in": None, "label": "状态暂不可读", "detail": "由 Codex 管理"}
    return {
        "logged_in": logged_in,
        "label": "已登录 ChatGPT" if logged_in else "需要登录",
        "detail": "官方额度" if logged_in else "打开官方登录页",
    }


def _task_projection(
    binding: dict[str, Any],
    history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_alias = str(binding.get("provider_alias") or "")
    provider = "官方" if provider_alias == "openai" else str(
        binding.get("profile_label") or provider_alias or "未知"
    ).split("（", 1)[0]
    role = str(binding.get("family_role") or "")
    archived = bool(int(binding.get("archived") or 0))
    pinned = bool(binding.get("codex_pinned_index")) or bool(
        int(binding.get("is_pinned") or 0)
    )
    state = "已归档" if archived else "当前 head" if role == "head" else "历史分支"
    full_title = str(binding.get("display_name") or "未命名任务")
    display_title = full_title if len(full_title) <= 42 else full_title[:39].rstrip() + "…"
    history_health = str((history or {}).get("health") or "unknown")
    history_labels = {
        "healthy": "投影正常",
        "unchecked": "待深度检查",
        "pending": "等待同步",
        "stalled": "历史异常",
        "unreadable": "历史不可读",
        "unknown": "无法确认",
    }
    return {
        "id": str(binding.get("thread_id") or ""),
        "id_suffix": str(binding.get("thread_id") or "")[-8:],
        "title": display_title,
        "title_full": full_title,
        "provider": provider,
        "provider_alias": provider_alias,
        "profile_id": str(binding.get("profile_id") or ""),
        "model": str(binding.get("model") or "由账号决定"),
        "state": state,
        "archived": archived,
        "pinned": pinned,
        "role": role,
        "family_id": str(binding.get("family_id") or ""),
        "family_size": int(binding.get("family_size") or 1),
        "family_head_id": str(binding.get("family_head_id") or ""),
        "recency": int(
            binding.get("recency_at_ms")
            or binding.get("updated_at_ms")
            or binding.get("created_at_ms")
            or 0
        ),
        "cwd": str(binding.get("cwd") or ""),
        "history_health": history_health,
        "history_label": history_labels.get(history_health, "无法确认"),
        "history_safe": bool((history or {}).get("safe_to_fork")),
        "history_checkable": history_health == "unchecked",
        "history_reason": str((history or {}).get("reason") or "not_checked"),
    }


def build_ui_snapshot(paths: switchboard.Paths) -> dict[str, Any]:
    """Build one secret-free, read-only snapshot for the visual prototype."""

    active = switchboard.load_active(paths)
    profiles_document = switchboard.load_profiles(paths)
    active_profile = switchboard.profile_by_id(
        profiles_document,
        str(active.get("profile_id") or "official"),
    )
    router = _local_router_status(paths)
    official = _official_login_status(paths)
    inventory = switchboard.recent_thread_bindings(
        paths,
        limit=switchboard.MAX_THREAD_INVENTORY_ROWS,
        include_archived=True,
    )
    family_rows = switchboard.thread_family_bindings(
        paths,
        limit=500,
        include_archived=True,
        include_subagents=False,
        sort_mode=switchboard.THREAD_SORT_CODEX,
        bindings=inventory,
    )
    # Reuse only the filename lookup during this one read-only UI snapshot.
    history_path_index: dict = {}
    tasks = [
        _task_projection(
            binding,
            switchboard.thread_history_projection_status(
                paths,
                str(binding.get("thread_id") or ""),
                include_candidates=False,
                verify_rollout=False,
                history_path_index=history_path_index,
            ),
        )
        for binding in family_rows
    ]
    profile_cards: list[dict[str, Any]] = []
    for profile in profiles_document.get("profiles", []):
        if not isinstance(profile, dict):
            continue
        profile_id = str(profile.get("id") or "")
        kind = str(profile.get("kind") or "")
        is_active = profile_id == active.get("profile_id")
        if kind == "official":
            profile_cards.append(
                {
                    "id": profile_id,
                    "title": "官方账号",
                    "kind": "OpenAI",
                    "active": is_active,
                    "ready": official.get("logged_in") is True,
                    "status": official["label"],
                    "detail": "模型和额度跟随当前 ChatGPT 登录",
                    "models": [],
                    "model": "",
                    "base_url": "",
                    "enabled": True,
                    "key_ref": "",
                    "key_configured": False,
                }
            )
            continue
        models = switchboard.relay_model_ids(profile)
        key_ref = str(profile.get("key_ref") or "")
        credential_ready = False
        if key_ref:
            try:
                credential_ready = switchboard.key_path(paths, key_ref).is_file()
            except ValueError:
                credential_ready = False
        profile_cards.append(
            {
                "id": profile_id,
                "title": str(profile.get("label") or profile_id).split("（", 1)[0].strip(),
                "kind": "中转站",
                "active": is_active,
                "ready": bool(profile.get("enabled")) and credential_ready,
                "status": "已就绪" if bool(profile.get("enabled")) and credential_ready else "待配置",
                "detail": str(profile.get("base_url") or "尚未填写 API 地址"),
                "models": models,
                "model": str(profile.get("model") or ""),
                "base_url": str(profile.get("base_url") or ""),
                "enabled": bool(profile.get("enabled")),
                "key_ref": key_ref,
                "key_configured": credential_ready,
            }
        )
    profile_cards.sort(key=lambda profile: (not bool(profile.get("active")), profile.get("id")))
    config_status = switchboard.config_projection_status(paths)
    files = {
        "任务索引": paths.state_database,
        "历史投影": paths.thread_history_database,
        "运行日志": paths.codex_home / "logs_2.sqlite",
    }
    footprint = [
        {
            "label": label,
            "size": _human_size(path.stat().st_size) if path.is_file() else "未生成",
        }
        for label, path in files.items()
    ]
    return {
        "version": UI_VERSION,
        "home": os.fspath(paths.codex_home),
        "active": {
            "id": str(active.get("profile_id") or ""),
            "label": str(active_profile.get("label") or active.get("profile_id") or "未知").split("（", 1)[0],
            "provider_alias": str(active.get("provider_alias") or ""),
            "revision": int(active.get("revision") or 0),
        },
        "official": official,
        "router": router,
        "profiles": profile_cards,
        "tasks": tasks,
        "counts": {
            "inventory": len(inventory),
            "user_tasks": len(tasks),
            "archived": sum(task["archived"] for task in tasks),
            "subagents_hidden": sum(bool(item.get("is_subagent")) for item in inventory),
        },
        "config": {
            "ready": bool(config_status.get("ready")),
            "label": "配置同步" if config_status.get("ready") else "配置需要修复",
        },
        "footprint": footprint,
    }


def demo_snapshot() -> dict[str, Any]:
    tasks = [
        {
            "id": f"01a0-demo-{index:04d}",
            "id_suffix": f"d{index:07d}"[-8:],
            "title": title,
            "title_full": title,
            "provider": provider,
            "provider_alias": "openai" if provider == "官方" else "custom",
            "profile_id": "official" if provider == "官方" else "maylily",
            "model": model,
            "state": "当前 head",
            "history_health": "healthy",
            "history_label": "历史正常",
            "history_safe": True,
            "history_checkable": False,
            "history_reason": "projection_at_eof",
            "archived": False,
            "pinned": index < 4,
            "role": "head",
            "family_id": f"family-{index}",
            "family_size": 1,
            "family_head_id": f"01a0-demo-{index:04d}",
            "recency": 10000 - index,
            "cwd": rf"E:\Projects\{title}",
        }
        for index, (title, provider, model) in enumerate(
            [
                ("示例任务 A", "官方", "gpt-5.6-sol"),
                ("网站项目", "官方", "gpt-5.6-sol"),
                ("数据工具", "官方", "gpt-5.6-sol"),
                ("自动化任务", "Maylily", "gpt-5.4"),
                ("设计系统", "官方", "gpt-5.6-sol"),
                ("文档整理", "官方", "gpt-5.4"),
            ]
        )
    ]
    return {
        "version": UI_VERSION,
        "home": r"E:\Codex-Home",
        "active": {"id": "official", "label": "官方账号", "provider_alias": "openai", "revision": 12},
        "official": {"logged_in": True, "label": "已登录 ChatGPT", "detail": "官方额度"},
        "router": {"healthy": True, "label": "运行正常", "detail": "127.0.0.1:8765"},
        "profiles": [
            {"id": "official", "title": "官方账号", "kind": "OpenAI", "active": True, "ready": True, "status": "已登录 ChatGPT", "detail": "模型和额度跟随当前 ChatGPT 登录", "models": [], "model": "", "base_url": "", "enabled": True, "key_ref": "", "key_configured": False},
            {"id": "maylily", "title": "Maylily", "kind": "中转站", "active": False, "ready": True, "status": "已就绪", "detail": "https://maylily.xyz", "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4"], "model": "gpt-5.6-sol", "base_url": "https://maylily.xyz", "enabled": True, "key_ref": "maylily-main", "key_configured": True},
            {"id": "relay-b", "title": "另一个中转站", "kind": "中转站", "active": False, "ready": False, "status": "待配置", "detail": "尚未填写 API 地址", "models": [], "model": "", "base_url": "", "enabled": False, "key_ref": "relay-b-main", "key_configured": False},
        ],
        "tasks": tasks,
        "counts": {"inventory": 946, "user_tasks": 141, "archived": 36, "subagents_hidden": 805},
        "config": {"ready": True, "label": "配置同步"},
        "footprint": [
            {"label": "任务索引", "size": "4.59 MB"},
            {"label": "历史投影", "size": "0.52 GB"},
            {"label": "运行日志", "size": "3.87 GB"},
        ],
    }


def bootstrap_snapshot(home: Path) -> dict[str, Any]:
    """Secret-free shell that lets a new computer inspect/import a pack first."""

    return {
        "version": UI_VERSION,
        "home": os.fspath(home),
        "active": {"id": "official", "label": "尚未激活资料库", "provider_alias": "openai", "revision": 0},
        "official": {"logged_in": None, "label": "导入后重新登录", "detail": "由 Codex 管理"},
        "router": {"healthy": False, "label": "尚未启动", "detail": "导入后配置"},
        "profiles": [
            {
                "id": "official",
                "title": "官方账号",
                "kind": "OpenAI",
                "active": True,
                "ready": False,
                "status": "导入后重新登录",
                "detail": "新电脑不会继承旧账号令牌",
                "models": [],
                "model": "",
                "base_url": "",
                "enabled": True,
                "key_ref": "",
                "key_configured": False,
            }
        ],
        "tasks": [],
        "counts": {"inventory": 0, "user_tasks": 0, "archived": 0, "subagents_hidden": 0},
        "config": {"ready": False, "label": "等待接收资料库"},
        "footprint": [],
    }


def make_app_icon(size: int = 64) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#0071E3"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(2, 2, size - 4, size - 4), size * 0.24, size * 0.24)
    painter.setPen(QColor("#FFFFFF"))
    font = QFont("Segoe UI Variable Display", max(12, int(size * 0.36)), QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "S")
    painter.end()
    return QIcon(pixmap)


def add_shadow(widget: QWidget, *, blur: int = 28, y: int = 7, alpha: int = 24) -> None:
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, y)
    shadow.setColor(QColor(20, 20, 24, alpha))
    widget.setGraphicsEffect(shadow)


class Badge(QLabel):
    def __init__(self, text: str, tone: str = "gray", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("badge", True)
        self.setProperty("tone", tone)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None, *, hero: bool = False) -> None:
        super().__init__(parent)
        self.setProperty("hero" if hero else "card", True)


class NavButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setProperty("nav", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class StatusCard(Card):
    def __init__(
        self,
        label: str,
        value: str,
        detail: str,
        *,
        tone: str = "blue",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumHeight(138)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(7)
        top = QHBoxLayout()
        top.setSpacing(8)
        caption = _role(QLabel(label), "secondary")
        top.addWidget(caption)
        top.addStretch(1)
        dot = QLabel("●")
        dot.setStyleSheet(
            "color: #34C759; font-size: 12px;" if tone == "green" else "color: #0071E3; font-size: 12px;"
        )
        top.addWidget(dot)
        layout.addLayout(top)
        metric = _role(QLabel(value), "metric")
        metric.setWordWrap(True)
        layout.addWidget(metric)
        description = _role(QLabel(detail), "secondary")
        description.setWordWrap(True)
        layout.addWidget(description)
        layout.addStretch(1)


class TaskRow(QFrame):
    clicked = Signal(dict)

    def __init__(self, task: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task = task
        self.setProperty("taskRow", True)
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(70)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 11, 14, 11)
        layout.setSpacing(12)
        marker = QFrame()
        marker.setFixedSize(4, 34)
        marker.setStyleSheet(
            "background:#0071E3;border-radius:2px;"
            if task.get("provider_alias") == "openai"
            else "background:#34C759;border-radius:2px;"
        )
        layout.addWidget(marker)
        text_column = QVBoxLayout()
        text_column.setSpacing(3)
        title = _role(QLabel(str(task.get("title") or "未命名任务")), "cardTitle")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        text_column.addWidget(title)
        meta = _role(
            QLabel(f"{task.get('provider')}  ·  {task.get('model')}"),
            "secondary",
        )
        text_column.addWidget(meta)
        layout.addLayout(text_column, 1)
        if task.get("pinned"):
            layout.addWidget(Badge("置顶", "blue"))
        layout.addWidget(
            Badge(
                str(task.get("history_label") or "无法确认"),
                "green"
                if task.get("history_safe")
                else "blue"
                if task.get("history_checkable")
                else "orange",
            )
        )
        if task.get("archived"):
            layout.addWidget(Badge("归档", "gray"))
        else:
            layout.addWidget(Badge(str(task.get("state") or "活跃"), "green"))
        suffix = _role(QLabel(str(task.get("id_suffix") or "")), "tiny")
        suffix.setMinimumWidth(60)
        suffix.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(suffix)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.task)
        super().mousePressEvent(event)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        column = QVBoxLayout()
        column.setSpacing(5)
        column.addWidget(_role(QLabel(title), "pageTitle"))
        sub = _role(QLabel(subtitle), "pageSubtitle")
        sub.setWordWrap(True)
        column.addWidget(sub)
        layout.addLayout(column, 1)
        layout.addWidget(Badge("已连接", "green"), 0, Qt.AlignmentFlag.AlignTop)


class ConversionDialog(QDialog):
    def __init__(
        self,
        task: dict[str, Any],
        profiles: list[dict[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.task = task
        self.profiles = profiles
        self.setWindowTitle("无损转换任务")
        self.setModal(True)
        self.setMinimumWidth(470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        title = _role(QLabel(str(task.get("title_full") or task.get("title"))), "heroTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        description = _role(
            QLabel("复制完整历史到目标 Provider；旧任务保持原状，不会发送消息或触发模型推理。"),
            "secondary",
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        form.setSpacing(12)
        self.target = QComboBox()
        for profile in profiles:
            suffix = "" if profile.get("ready") else "（待配置）"
            self.target.addItem(f"{profile.get('title')}{suffix}", profile.get("id"))
        self.target.currentIndexChanged.connect(self._refresh_models)
        form.addRow("目标 Provider", self.target)
        self.model = QComboBox()
        form.addRow("目标模型", self.model)
        layout.addLayout(form)
        self.warning = _role(QLabel("转换时如 Codex 正在运行，会提示你关闭主窗口。"), "secondary")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("开始无损转换")
        ok.setProperty("variant", "primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        preferred = str(task.get("profile_id") or "")
        for index, profile in enumerate(profiles):
            if str(profile.get("id") or "") != preferred:
                self.target.setCurrentIndex(index)
                break
        self._refresh_models()

    def _refresh_models(self) -> None:
        profile_id = str(self.target.currentData() or "")
        profile = next(
            (item for item in self.profiles if str(item.get("id") or "") == profile_id),
            {},
        )
        self.model.clear()
        models = list(profile.get("models") or [])
        if profile.get("kind") == "OpenAI":
            self.model.addItem("由官方账号决定", None)
            self.model.setEnabled(False)
        else:
            self.model.setEnabled(True)
            for model in models:
                self.model.addItem(str(model), str(model))
            default_model = str(profile.get("model") or "")
            index = self.model.findData(default_model)
            if index >= 0:
                self.model.setCurrentIndex(index)

    def selection(self) -> tuple[str, str | None]:
        return str(self.target.currentData() or ""), (
            str(self.model.currentData()) if self.model.currentData() else None
        )


class HandoffDialog(QDialog):
    """Explicit consent and frozen source/model/files, not a second config UI."""

    def __init__(self, task: dict, models: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("一键接手 · 新会话")
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 22)
        layout.setSpacing(14)
        title = _role(QLabel("把当前项目交给一个全新会话"), "heroTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        description = _role(QLabel(
            "老 AI 整理交接 → 本地检查并保存资料 → 新会话只读核验。\n"
            "不复制整段旧历史，不归档原聊天，不继续业务实现。"), "secondary")
        description.setWordWrap(True)
        layout.addWidget(description)
        source = QLabel(f"原会话：{task.get('title_full') or task.get('title')}\n"
                        f"沿用原 Provider：{task.get('provider')} · {task.get('model')}")
        source.setTextFormat(Qt.TextFormat.PlainText)
        source.setWordWrap(True)
        layout.addWidget(source)
        form = QFormLayout()
        self.workspace = QLineEdit(str(task.get("cwd") or ""))
        row = QHBoxLayout()
        row.addWidget(self.workspace, 1)
        browse = QPushButton("选择项目…")
        browse.clicked.connect(self.choose_workspace)
        row.addWidget(browse)
        form.addRow("项目 Git 根目录", row)
        self.model = QComboBox()
        self.model.addItems(models)
        if task.get("model") in models:
            self.model.setCurrentText(task["model"])
        form.addRow("新官方会话模型", self.model)
        layout.addLayout(form)
        self.documents = QListWidget()
        self.documents.setStyleSheet("QListWidget { background:white; border:1px solid #E5E5EA; border-radius:10px; padding:6px; } QListWidget::item:selected { background:#EAF3FF; color:#1D1D1F; }")
        self.documents.setMaximumHeight(112)
        self.documents.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(_role(QLabel("补充必读资料和图片（老 AI 也会整理项目内的资料清单）"), "secondary"))
        layout.addWidget(self.documents)
        buttons_row = QHBoxLayout()
        add = QPushButton("添加资料…")
        add.clicked.connect(self.choose_documents)
        remove = QPushButton("移出清单")
        remove.clicked.connect(self.remove_documents)
        buttons_row.addWidget(add)
        buttons_row.addWidget(remove)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("可选：提醒老 AI 本次真正要接手的目标，或特别不能遗漏的事情。")
        self.note.setMaximumHeight(76)
        layout.addWidget(self.note)
        warning = _role(QLabel(
            "将向老、新会话各提交一个模型回合并产生用量；长历史可能先压缩，实际模型请求数不固定。"
            "资料会由对应 Provider 处理，请不要选入秘密。结果不确定时切换器不会重发；官方内部重连由 Codex 管理。\n"
            "开始后请自行完全退出 Codex。切换器可以关闭，进度会保存；接手时不要改项目或重开 Codex。"
            "只读及自动动作限制仅用于接手后台；不改全局配置、不永久禁用新会话工具。"), "secondary")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.consent = QCheckBox("我确认本次资料范围，并授权整理和只读核验的模型用量")
        layout.addWidget(self.consent)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("开始一键接手")
        ok.setProperty("variant", "primary")
        ok.setStyleSheet("QPushButton:disabled { background:#E5E5EA; color:#8E8E93; border-color:#E5E5EA; }")
        ok.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.consent.toggled.connect(self._validate)
        self.workspace.textChanged.connect(self._validate)
        self.model.currentTextChanged.connect(self._validate)
        layout.addWidget(self.buttons)

    def _validate(self, *_args) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.consent.isChecked() and bool(self.workspace.text().strip()) and bool(self.model.currentText()))

    def choose_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择实际项目的 Git 根目录", self.workspace.text())
        if selected:
            self.workspace.setText(selected)

    def choose_documents(self) -> None:
        selected, _ = QFileDialog.getOpenFileNames(self, "补充必读资料与图片", self.workspace.text(),
            "文本、代码和图片 (*.md *.txt *.json *.py *.js *.ts *.tsx *.mjs *.yaml *.yml *.png *.jpg *.jpeg *.webp);;所有文件 (*)")
        present = {self.documents.item(index).text() for index in range(self.documents.count())}
        for path in selected:
            if path not in present:
                self.documents.addItem(path)
                present.add(path)

    def remove_documents(self) -> None:
        for item in self.documents.selectedItems():
            self.documents.takeItem(self.documents.row(item))

    def selection(self) -> dict:
        return {"workspace": Path(self.workspace.text().strip()), "target_model": self.model.currentText(),
                "documents": [Path(self.documents.item(index).text()) for index in range(self.documents.count())],
                "consent": self.consent.isChecked(), "user_note": self.note.toPlainText().strip()}


class HandoffPage(QWidget):
    refresh_requested = Signal()
    resume_requested = Signal(str)
    cancel_requested = Signal(str)
    open_requested = Signal(str)
    files_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.jobs: list[dict] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 30)
        outer.setSpacing(18)
        outer.addWidget(PageHeader("接手记录", "进度独立保存；关闭窗口后可回来继续查看。"))
        info = _role(QLabel("从“任务”选择老会话，再点击“一键接手”。通过表示交接检查通过，不代表业务完成或理解毫无遗漏。"), "secondary")
        info.setWordWrap(True)
        outer.addWidget(info)
        self.records = QListWidget()
        self.records.setMaximumHeight(180)
        self.records.setStyleSheet("QListWidget::item { color:#1D1D1F; background:white; border:1px solid #E5E5EA; border-radius:12px; padding:12px; } QListWidget::item:selected { background:#EAF3FF; color:#1D1D1F; border-color:#B8D9FF; }")
        self.records.currentRowChanged.connect(self.select_record)
        outer.addWidget(self.records, 1)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(210)
        outer.addWidget(self.detail, 3)
        actions = QHBoxLayout()
        self.open_button = QPushButton("打开新会话")
        self.open_button.setProperty("variant", "primary")
        self.files_button = QPushButton("查看交接资料")
        self.resume_button = QPushButton("继续 / 核验原操作")
        self.cancel_button = QPushButton("停止接手")
        refresh = QPushButton("刷新")
        for button in (self.open_button, self.files_button, self.resume_button, self.cancel_button, refresh):
            actions.addWidget(button)
        self.open_button.clicked.connect(lambda: self.emit_selected(self.open_requested))
        self.files_button.clicked.connect(lambda: self.emit_selected(self.files_requested))
        self.resume_button.clicked.connect(lambda: self.emit_selected(self.resume_requested))
        self.cancel_button.clicked.connect(lambda: self.emit_selected(self.cancel_requested))
        refresh.clicked.connect(self.refresh_requested.emit)
        outer.addLayout(actions)
        self.apply_jobs([])

    def emit_selected(self, signal) -> None:
        row = self.records.currentRow()
        if 0 <= row < len(self.jobs):
            signal.emit(self.jobs[row]["id"])

    def apply_jobs(self, jobs: list[dict]) -> None:
        row = self.records.currentRow()
        current = self.jobs[row]["id"] if 0 <= row < len(self.jobs) else None
        self.jobs = jobs
        self.records.blockSignals(True)
        self.records.clear()
        selected = 0
        for index, job in enumerate(jobs):
            label = handoff_jobs.STATUS_LABELS.get(job.get("status"), "待核验")
            if job.get("status") in {"running", "waiting"} and not job.get("worker_alive"):
                label = "后台已停止 · 待核验"
            item = QListWidgetItem(f"{job.get('title') or '接手操作'}\n{label}  ·  {job.get('message') or ''}")
            item.setSizeHint(QSize(100, 76))
            self.records.addItem(item)
            if job["id"] == current:
                selected = index
        self.records.blockSignals(False)
        self.records.setCurrentRow(selected if jobs else -1)
        self.select_record(selected if jobs else -1)

    def select_record(self, row: int) -> None:
        job = self.jobs[row] if 0 <= row < len(self.jobs) else {}
        if not job:
            self.detail.setPlainText("还没有接手记录。原聊天和项目文件不会因打开此页面而变化。")
        else:
            request = job.get("request") or {}
            text = [str(job.get("message") or ""), "", f"项目：{request.get('workspace', '')}",
                    f"原会话：{request.get('source_thread_id', '')}",
                    f"新会话：{(job.get('target_thread') or {}).get('thread_id') or '尚未创建'}",
                    f"阶段：{job.get('stage', '')}", "", "原操作记录："]
            text.extend(str(item.get("message") or "") for item in job.get("events", [])[-10:])
            if job.get("gaps"):
                text += ["", "需要补充：", *[str(gap) for gap in job["gaps"]]]
            if job.get("acceptance_text"):
                text = [str(job["acceptance_text"]), "", "—— 原操作与进度 ——", "", *text]
            self.detail.setPlainText("\n".join(text))
        terminal = job.get("status") in handoff_jobs.FINAL_STATUSES
        self.open_button.setEnabled(bool((job.get("target_thread") or {}).get("thread_id")) and not job.get("worker_alive"))
        self.files_button.setEnabled(bool(job.get("bundle")))
        self.resume_button.setEnabled(bool(job) and not terminal and not job.get("worker_alive") and not job.get("cancel_requested"))
        self.resume_button.setToolTip("已取消但结果未知时，请先在原会话人工核验；不会自动恢复模型。" if job.get("cancel_requested") else "")
        self.cancel_button.setEnabled(bool(job) and not terminal)


class RelaySettingsDialog(QDialog):
    save_metadata = Signal(dict)
    save_key = Signal(str, str)
    import_models = Signal(str)
    generate_catalog = Signal(dict)
    probe_models = Signal(str)

    def __init__(self, profile: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle(f"配置 {profile.get('title')}")
        self.setModal(True)
        self.setMinimumSize(600, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 22)
        layout.setSpacing(15)
        layout.addWidget(_role(QLabel(str(profile.get("title") or "中转站")), "heroTitle"))
        note = _role(
            QLabel("地址和模型保存为非秘密元数据；Key 单独通过 Windows DPAPI 加密。"),
            "secondary",
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        form.setSpacing(12)
        self.base_url = QLineEdit(str(profile.get("base_url") or ""))
        self.base_url.setPlaceholderText("https://example.com/v1")
        form.addRow("API 地址", self.base_url)
        self.default_model = QComboBox()
        self.default_model.setEditable(True)
        for model in profile.get("models") or []:
            self.default_model.addItem(str(model))
        self.default_model.setCurrentText(str(profile.get("model") or ""))
        form.addRow("默认模型", self.default_model)
        self.enabled = QCheckBox("启用这个中转站")
        self.enabled.setChecked(bool(profile.get("enabled")))
        form.addRow("状态", self.enabled)
        layout.addLayout(form)
        layout.addWidget(_role(QLabel("可用模型（逗号或换行分隔）"), "cardTitle"))
        self.models = QPlainTextEdit()
        self.models.setPlainText("\n".join(str(item) for item in profile.get("models") or []))
        self.models.setMinimumHeight(120)
        layout.addWidget(self.models)
        key_row = QHBoxLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText(
            "已配置；留空不改" if profile.get("key_configured") else "粘贴 Key"
        )
        key_row.addWidget(self.key, 1)
        save_key = QPushButton("加密保存 Key")
        save_key.clicked.connect(self._emit_key)
        key_row.addWidget(save_key)
        layout.addLayout(key_row)
        actions = QHBoxLayout()
        save = QPushButton("保存地址和模型")
        save.setProperty("variant", "primary")
        save.clicked.connect(lambda: self.save_metadata.emit(self.values()))
        actions.addWidget(save)
        local_models = QPushButton("导入本机模型")
        local_models.clicked.connect(
            lambda: self.import_models.emit(str(self.profile.get("id") or ""))
        )
        actions.addWidget(local_models)
        catalog = QPushButton("生成模型目录")
        catalog.clicked.connect(lambda: self.generate_catalog.emit(self.values()))
        actions.addWidget(catalog)
        layout.addLayout(actions)
        bottom = QHBoxLayout()
        probe = QPushButton("检测远端模型")
        probe.clicked.connect(
            lambda: self.probe_models.emit(str(self.profile.get("id") or ""))
        )
        bottom.addWidget(probe)
        bottom.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.status = _role(QLabel(""), "secondary")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def values(self) -> dict[str, Any]:
        return {
            "profile_id": str(self.profile.get("id") or ""),
            "base_url": self.base_url.text().strip(),
            "model": self.default_model.currentText().strip(),
            "models": self.models.toPlainText(),
            "enabled": self.enabled.isChecked(),
        }

    def _emit_key(self) -> None:
        secret = self.key.text()
        if not secret.strip():
            self.status.setText("Key 为空；没有执行保存。")
            return
        self.save_key.emit(str(self.profile.get("id") or ""), secret)
        self.key.clear()

    def apply_imported_models(self, models: list[str]) -> None:
        self.models.setPlainText("\n".join(models))
        self.default_model.clear()
        self.default_model.addItems(models)
        if models:
            self.default_model.setCurrentText(models[0])
        self.status.setText(f"已读取 {len(models)} 个本机安全模型；请保存后再生成目录。")


class BackupPlanDialog(QDialog):
    def __init__(self, plan: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("备份保留计划")
        self.setMinimumSize(620, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_role(QLabel("只读备份预览"), "heroTitle"))
        summary = _role(
            QLabel(
                f"共 {plan.get('entry_count', 0)} 项 / {plan.get('total_size', '0 B')}；"
                f"候选 {plan.get('candidate_count', 0)} 项 / {plan.get('candidate_size', '0 B')}。"
            ),
            "body",
        )
        layout.addWidget(summary)
        note = _role(QLabel("不会自动删除、移动或压缩任何备份。"), "secondary")
        layout.addWidget(note)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        candidates = list(plan.get("candidates") or [])
        details.setPlainText(
            "\n".join(
                f"{item.get('age_days', 0)} 天 · {_human_size(int(item.get('bytes') or 0))} · {item.get('name')}"
                for item in candidates[:100]
            )
            or "当前没有符合策略的候选。"
        )
        layout.addWidget(details, 1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


class OverviewPage(QWidget):
    request_tasks = Signal()
    refresh_requested = Signal()

    def __init__(self, snapshot: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 30)
        outer.setSpacing(22)
        outer.addWidget(PageHeader("概览", "账号、路由和任务状态，一眼就够。"))

        hero = Card(hero=True)
        add_shadow(hero, blur=36, y=9, alpha=20)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(26, 24, 26, 24)
        hero_layout.setSpacing(18)
        text = QVBoxLayout()
        text.setSpacing(7)
        all_ready = bool(snapshot["router"]["healthy"] and snapshot["config"]["ready"])
        title = "一切就绪" if all_ready else "有一项需要留意"
        text.addWidget(_role(QLabel(title), "heroTitle"))
        detail = (
            f"新任务默认使用 {snapshot['active']['label']}。"
            f"已隐藏 {snapshot['counts']['subagents_hidden']} 个内部子任务。"
        )
        body = _role(QLabel(detail), "body")
        body.setWordWrap(True)
        text.addWidget(body)
        hero_layout.addLayout(text, 1)
        task_button = QPushButton("查看任务")
        task_button.setProperty("variant", "primary")
        task_button.setCursor(Qt.CursorShape.PointingHandCursor)
        task_button.clicked.connect(self.request_tasks.emit)
        hero_layout.addWidget(task_button, 0, Qt.AlignmentFlag.AlignVCenter)
        refresh_button = QPushButton("刷新状态")
        refresh_button.clicked.connect(self.refresh_requested.emit)
        hero_layout.addWidget(refresh_button, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(hero)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        official_tone = "green" if snapshot["official"].get("logged_in") else "blue"
        cards.addWidget(
            StatusCard(
                "官方登录",
                snapshot["official"]["label"],
                snapshot["official"]["detail"],
                tone=official_tone,
            ),
            1,
        )
        cards.addWidget(
            StatusCard(
                "新任务默认",
                snapshot["active"]["label"],
                f"Provider · {snapshot['active']['provider_alias'] or '原生'}",
            ),
            1,
        )
        cards.addWidget(
            StatusCard(
                "本地路由",
                snapshot["router"]["label"],
                snapshot["router"]["detail"],
                tone="green" if snapshot["router"]["healthy"] else "blue",
            ),
            1,
        )
        outer.addLayout(cards)

        recent = Card()
        recent_layout = QVBoxLayout(recent)
        recent_layout.setContentsMargins(20, 18, 20, 18)
        recent_layout.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(_role(QLabel("最近任务"), "cardTitle"))
        header.addStretch(1)
        count = snapshot["counts"]["user_tasks"]
        header.addWidget(_role(QLabel(f"{count} 个用户任务"), "secondary"))
        recent_layout.addLayout(header)
        for task in snapshot["tasks"][:5]:
            row = TaskRow(task)
            row.clicked.connect(lambda _task: self.request_tasks.emit())
            recent_layout.addWidget(row)
        outer.addWidget(recent, 1)


class TasksPage(QWidget):
    diagnose_requested = Signal(dict)
    recovery_requested = Signal(dict)
    convert_requested = Signal(dict)
    handoff_requested = Signal(dict)
    open_requested = Signal(dict)
    cleanup_requested = Signal(dict)

    def __init__(self, snapshot: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tasks = list(snapshot["tasks"])
        self.row_widgets: list[TaskRow] = []
        self.selected_task: dict[str, Any] | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 30)
        outer.setSpacing(18)
        outer.addWidget(PageHeader("任务", "只显示用户任务；内部子智能体默认保持隐藏。"))

        toolbar = Card()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(14, 12, 14, 12)
        toolbar_layout.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索任务、Provider、模型或 ID")
        self.search.textChanged.connect(self.apply_filters)
        toolbar_layout.addWidget(self.search, 1)
        segment = QFrame()
        segment.setObjectName("segmentShell")
        segment_layout = QHBoxLayout(segment)
        segment_layout.setContentsMargins(2, 2, 2, 2)
        segment_layout.setSpacing(2)
        self.sort_group = QButtonGroup(self)
        self.sort_group.setExclusive(True)
        self.codex_sort = QPushButton("Codex 顺序")
        self.recent_sort = QPushButton("最近使用")
        for button in (self.codex_sort, self.recent_sort):
            button.setCheckable(True)
            button.setProperty("segment", True)
            segment_layout.addWidget(button)
            self.sort_group.addButton(button)
            button.clicked.connect(self.apply_filters)
        self.codex_sort.setChecked(True)
        toolbar_layout.addWidget(segment)
        self.archived_filter = QCheckBox("包含归档")
        self.archived_filter.setChecked(True)
        self.archived_filter.toggled.connect(self.apply_filters)
        toolbar_layout.addWidget(self.archived_filter)
        outer.addWidget(toolbar)

        body = QHBoxLayout()
        body.setSpacing(16)
        list_card = Card()
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(12, 12, 12, 12)
        list_layout.setSpacing(8)
        list_header = QHBoxLayout()
        list_header.addWidget(_role(QLabel("全部任务"), "cardTitle"))
        list_header.addStretch(1)
        self.count_label = _role(QLabel(""), "secondary")
        list_header.addWidget(self.count_label)
        list_layout.addLayout(list_header)
        self.task_list = QListWidget()
        self.task_list.setSpacing(7)
        self.task_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        list_layout.addWidget(self.task_list, 1)
        body.addWidget(list_card, 5)

        self.detail = Card()
        self.detail.setMinimumWidth(270)
        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_layout.setContentsMargins(22, 20, 22, 20)
        self.detail_layout.setSpacing(12)
        self.detail_eyebrow = _role(QLabel("所选任务"), "eyebrow")
        self.detail_layout.addWidget(self.detail_eyebrow)
        self.detail_title = _role(QLabel("请选择一个任务"), "heroTitle")
        self.detail_title.setWordWrap(True)
        self.detail_layout.addWidget(self.detail_title)
        self.detail_meta = _role(QLabel("查看实际 Provider 和模型配置"), "secondary")
        self.detail_meta.setWordWrap(True)
        self.detail_layout.addWidget(self.detail_meta)
        self.detail_badges = QHBoxLayout()
        self.detail_layout.addLayout(self.detail_badges)
        self.detail_path = _role(QLabel(""), "tiny")
        self.detail_path.setWordWrap(True)
        self.detail_layout.addWidget(self.detail_path)
        self.detail_layout.addStretch(1)
        self.diagnose_button = QPushButton("一键体检 · 只读")
        self.diagnose_button.setEnabled(False)
        self.diagnose_button.setToolTip("核对历史同步、最近回合及工具错误；不启动任务、不调用模型、不修复数据。")
        self.diagnose_button.clicked.connect(self._emit_diagnose)
        self.detail_layout.addWidget(self.diagnose_button)
        self.recovery_button = QPushButton("预览历史恢复")
        self.recovery_button.setEnabled(False)
        self.recovery_button.setToolTip("只读检查是否属于已知的元数据重复；符合条件后仍需单独确认。")
        self.recovery_button.clicked.connect(self._emit_recovery)
        self.detail_layout.addWidget(self.recovery_button)
        self.convert_button = QPushButton("无损转换")
        self.convert_button.setProperty("variant", "primary")
        self.convert_button.clicked.connect(self._emit_convert)
        self.detail_layout.addWidget(self.convert_button)
        self.handoff_button = QPushButton("一键接手 · 新会话")
        self.handoff_button.setToolTip("老 AI 更新交接资料，再由不继承旧历史的新会话只读核验；需要模型用量授权。")
        self.handoff_button.clicked.connect(self._emit_handoff)
        self.detail_layout.addWidget(self.handoff_button)
        cleanup = QPushButton("整理任务家族")
        cleanup.clicked.connect(self._emit_cleanup)
        self.detail_layout.addWidget(cleanup)
        open_button = QPushButton("在 Codex 中打开")
        open_button.clicked.connect(self._emit_open)
        self.detail_layout.addWidget(open_button)
        body.addWidget(self.detail, 2)
        outer.addLayout(body, 1)
        self.apply_filters()

    def apply_filters(self) -> None:
        query = self.search.text().strip().casefold()
        tasks = [task for task in self.tasks if self.archived_filter.isChecked() or not task["archived"]]
        if query:
            tasks = [
                task
                for task in tasks
                if query
                in "\n".join(
                    str(task.get(field) or "")
                    for field in ("title", "provider", "model", "id", "cwd")
                ).casefold()
            ]
        if self.recent_sort.isChecked():
            tasks = sorted(tasks, key=lambda task: int(task.get("recency") or 0), reverse=True)
        self.task_list.clear()
        self.row_widgets.clear()
        for task in tasks:
            item = QListWidgetItem()
            item.setSizeHint(QSize(100, 72))
            row = TaskRow(task)
            row.clicked.connect(self.select_task)
            self.task_list.addItem(item)
            self.task_list.setItemWidget(item, row)
            self.row_widgets.append(row)
        self.count_label.setText(f"{len(tasks)} 项")
        if tasks and self.selected_task is None:
            self.select_task(tasks[0])

    def select_task(self, task: dict[str, Any]) -> None:
        self.selected_task = task
        self.diagnose_button.setEnabled(bool(task.get("id")))
        self.recovery_button.setEnabled(bool(task.get("id")))
        for row in self.row_widgets:
            row.set_selected(row.task.get("id") == task.get("id"))
        self.detail_title.setText(str(task.get("title_full") or task.get("title") or "未命名任务"))
        self.detail_meta.setText(f"{task.get('provider')}  ·  {task.get('model')}")
        while self.detail_badges.count():
            item = self.detail_badges.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.detail_badges.addWidget(Badge(str(task.get("state") or "活跃"), "green"))
        self.detail_badges.addWidget(
            Badge(
                str(task.get("history_label") or "无法确认"),
                "green"
                if task.get("history_safe")
                else "blue"
                if task.get("history_checkable")
                else "orange",
            )
        )
        if task.get("pinned"):
            self.detail_badges.addWidget(Badge("置顶", "blue"))
        self.detail_badges.addStretch(1)
        self.detail_path.setText(
            f"任务 ID · …{task.get('id_suffix')}\n工作区 · {_compact_windows_path(str(task.get('cwd') or ''))}"
        )
        history_safe = bool(task.get("history_safe"))
        history_checkable = bool(task.get("history_checkable"))
        self.convert_button.setEnabled(history_safe or history_checkable)
        self.handoff_button.setEnabled((history_safe or history_checkable) and not task.get("archived"))
        self.convert_button.setText(
            "无损转换"
            if history_safe
            else "检查并转换"
            if history_checkable
            else "历史未通过检查"
        )
        self.convert_button.setToolTip(
            "转换前会完整扫描当前 rollout。"
            if history_checkable
            else ""
            if history_safe
            else "历史投影未完整同步或存在异常；刷新后仍异常则需要先恢复聊天。"
        )

    def _emit_diagnose(self) -> None:
        if self.selected_task is not None:
            self.diagnose_requested.emit(dict(self.selected_task))

    def _emit_recovery(self) -> None:
        if self.selected_task is not None:
            self.recovery_requested.emit(dict(self.selected_task))

    def _emit_convert(self) -> None:
        if self.selected_task is not None:
            self.convert_requested.emit(self.selected_task)

    def _emit_handoff(self) -> None:
        if self.selected_task is not None:
            self.handoff_requested.emit(self.selected_task)

    def _emit_open(self) -> None:
        if self.selected_task is not None:
            self.open_requested.emit(self.selected_task)

    def _emit_cleanup(self) -> None:
        if self.selected_task is not None:
            self.cleanup_requested.emit(self.selected_task)


class ProviderCard(Card):
    activate_requested = Signal(dict)
    configure_requested = Signal(dict)

    def __init__(self, profile: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setProperty("providerCard", True)
        self.setProperty("active", bool(profile.get("active")))
        self.setMinimumHeight(224)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(Badge(str(profile.get("kind") or "Provider"), "blue" if profile.get("active") else "gray"))
        top.addStretch(1)
        if profile.get("active"):
            top.addWidget(Badge("当前默认", "green"))
        layout.addLayout(top)
        title = _role(QLabel(str(profile.get("title") or "Provider")), "heroTitle")
        layout.addWidget(title)
        status_tone = "green" if profile.get("ready") else "orange"
        layout.addWidget(Badge(str(profile.get("status") or "未知"), status_tone))
        detail = _role(QLabel(str(profile.get("detail") or "")), "secondary")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        models = list(profile.get("models") or [])
        if models:
            model_text = " · ".join(models[:3]) + (f"  +{len(models) - 3}" if len(models) > 3 else "")
            model_label = _role(QLabel(model_text), "tiny")
            model_label.setWordWrap(True)
            layout.addWidget(model_label)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        if profile.get("kind") == "中转站":
            configure = QPushButton("配置")
            configure.clicked.connect(lambda: self.configure_requested.emit(self.profile))
            buttons.addWidget(configure)
        action = QPushButton("设为默认" if not profile.get("active") else "当前正在使用")
        if not profile.get("active"):
            action.setProperty("variant", "primary")
        action.setEnabled(not bool(profile.get("active")))
        action.clicked.connect(lambda: self.activate_requested.emit(self.profile))
        buttons.addWidget(action, 1)
        layout.addLayout(buttons)


class AccountsPage(QWidget):
    activate_requested = Signal(dict)
    configure_requested = Signal(dict)
    login_requested = Signal()

    def __init__(self, snapshot: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 30)
        outer.setSpacing(20)
        outer.addWidget(PageHeader("账号与 Provider", "选择入口，不再面对配置表格。"))
        cards = QHBoxLayout()
        cards.setSpacing(14)
        for profile in snapshot["profiles"]:
            card = ProviderCard(profile)
            card.activate_requested.connect(self.activate_requested.emit)
            card.configure_requested.connect(self.configure_requested.emit)
            cards.addWidget(card, 1)
        outer.addLayout(cards)

        account = Card()
        layout = QHBoxLayout(account)
        layout.setContentsMargins(22, 20, 22, 20)
        text = QVBoxLayout()
        text.setSpacing(5)
        text.addWidget(_role(QLabel("官方登录"), "cardTitle"))
        text.addWidget(_role(QLabel(snapshot["official"]["label"]), "metric"))
        text.addWidget(
            _role(
                QLabel("更换官方账号会让全部 openai 任务统一使用新账号额度，聊天和任务 ID 不变。"),
                "secondary",
            )
        )
        layout.addLayout(text, 1)
        switch_button = QPushButton("登录或切换账号")
        switch_button.setProperty("variant", "primary")
        switch_button.clicked.connect(self.login_requested.emit)
        layout.addWidget(switch_button)
        outer.addWidget(account)
        outer.addStretch(1)


class MigrationPage(QWidget):
    export_requested = Signal()
    inspect_requested = Signal()
    import_requested = Signal()

    def __init__(self, snapshot: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 30)
        outer.setSpacing(20)
        outer.addWidget(PageHeader("迁移", "同一个迁移包，在旧电脑创建，在新电脑查看并接收。"))

        hero = Card(hero=True)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 22, 24, 22)
        hero_layout.setSpacing(8)
        hero_layout.addWidget(_role(QLabel("把 Codex 带到新电脑"), "heroTitle"))
        hero_layout.addWidget(
            _role(
                QLabel("打开迁移包只会读取内容；选择目录并点击“开始导入”后才会写入。"),
                "secondary",
            )
        )
        home = _role(QLabel(f"当前资料库  {snapshot.get('home') or '尚未选择'}"), "body")
        home.setWordWrap(True)
        hero_layout.addWidget(home)
        outer.addWidget(hero)

        actions = QHBoxLayout()
        actions.setSpacing(14)
        cards = (
            (
                "迁出此电脑",
                "选择项目和保存位置，创建可验证的 .codexpack。",
                "创建迁移包",
                self.export_requested,
                "primary",
            ),
            (
                "接收迁移包",
                "先预览任务、资料和风险，再选择新的保存目录。",
                "选择迁移包",
                self.import_requested,
                "primary",
            ),
            (
                "只读查看",
                "解析包的版本、数量和完整性，不向本机写入。",
                "查看迁移包",
                self.inspect_requested,
                "secondary",
            ),
        )
        for title, detail, button_text, signal, variant in cards:
            card = Card()
            layout = QVBoxLayout(card)
            layout.setContentsMargins(20, 20, 20, 18)
            layout.setSpacing(8)
            layout.addWidget(_role(QLabel(title), "cardTitle"))
            description = _role(QLabel(detail), "secondary")
            description.setWordWrap(True)
            layout.addWidget(description)
            layout.addStretch(1)
            button = QPushButton(button_text)
            button.setProperty("variant", variant)
            button.clicked.connect(signal.emit)
            layout.addWidget(button)
            actions.addWidget(card, 1)
        outer.addLayout(actions)

        note = Card()
        note_layout = QVBoxLayout(note)
        note_layout.setContentsMargins(22, 18, 22, 18)
        note_layout.addWidget(_role(QLabel("不会进入迁移包"), "cardTitle"))
        note_layout.addWidget(
            _role(
                QLabel("Switchboard 保存的官方登录、API Key、DPAPI 凭据，以及运行进程、日志、缓存和重复备份。"),
                "secondary",
            )
        )
        outer.addWidget(note)
        outer.addStretch(1)


class ExportPackDialog(QDialog):
    def __init__(self, home: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.home = home
        self.setWindowTitle("创建迁移包")
        self.resize(690, 530)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)
        outer.addWidget(_role(QLabel("选择要带走的内容"), "heroTitle"))
        source = _role(QLabel(f"Codex 数据：{home}"), "secondary")
        source.setWordWrap(True)
        outer.addWidget(source)

        output_row = QHBoxLayout()
        self.output = QLineEdit(
            os.fspath(Path.home() / "Documents" / f"Codex-迁移-{time.strftime('%Y%m%d-%H%M')}.codexpack")
        )
        self.output.setPlaceholderText("迁移包保存位置")
        browse_output = QPushButton("选择位置")
        browse_output.clicked.connect(self._choose_output)
        output_row.addWidget(self.output, 1)
        output_row.addWidget(browse_output)
        outer.addLayout(output_row)

        outer.addWidget(_role(QLabel("项目目录（可选）"), "cardTitle"))
        outer.addWidget(
            _role(QLabel("任务和聊天始终包含；只有添加的项目源码会一并带走。"), "secondary")
        )
        self.projects = QListWidget()
        self.projects.setMinimumHeight(180)
        outer.addWidget(self.projects, 1)
        project_actions = QHBoxLayout()
        add_project = QPushButton("添加项目目录")
        remove_project = QPushButton("移除所选")
        add_project.clicked.connect(self._add_project)
        remove_project.clicked.connect(self._remove_project)
        project_actions.addWidget(add_project)
        project_actions.addWidget(remove_project)
        project_actions.addStretch(1)
        outer.addLayout(project_actions)

        self.include_projection = QCheckBox("包含桌面历史投影（可重建，通常无需携带）")
        self.include_projection.setChecked(False)
        outer.addWidget(self.include_projection)
        warning = _role(
            QLabel("迁移包包含私人聊天和项目源码。Switchboard 凭据不会打包，但源码中手写的密码或 Token 无法保证识别，请保存到可信磁盘。"),
            "secondary",
        )
        warning.setWordWrap(True)
        outer.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        create = buttons.addButton("创建迁移包", QDialogButtonBox.ButtonRole.AcceptRole)
        create.setProperty("variant", "primary")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _choose_output(self) -> None:
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "保存迁移包",
            self.output.text(),
            "Codex 迁移包 (*.codexpack)",
        )
        if selected:
            self.output.setText(selected if selected.casefold().endswith(".codexpack") else selected + ".codexpack")

    def _add_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择项目目录",
            os.fspath(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not selected:
            return
        normalized = os.path.normcase(os.path.abspath(selected))
        if all(os.path.normcase(os.path.abspath(self.projects.item(i).text())) != normalized for i in range(self.projects.count())):
            self.projects.addItem(selected)

    def _remove_project(self) -> None:
        row = self.projects.currentRow()
        if row >= 0:
            self.projects.takeItem(row)

    def _accept_if_valid(self) -> None:
        output = self.output.text().strip()
        if not output:
            QMessageBox.warning(self, "缺少保存位置", "请选择迁移包保存位置。")
            return
        self.accept()

    def selection(self) -> tuple[Path, list[Path], bool]:
        return (
            Path(self.output.text().strip()).expanduser(),
            [Path(self.projects.item(i).text()) for i in range(self.projects.count())],
            self.include_projection.isChecked(),
        )


def _pack_summary_text(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else report
    manifest = report.get("manifest") if isinstance(report.get("manifest"), dict) else {}
    task_rows = summary.get("tasks") if isinstance(summary.get("tasks"), list) else []
    project_rows = summary.get("projects") if isinstance(summary.get("projects"), list) else []
    tasks = int(summary.get("task_count") or manifest.get("task_count") or len(task_rows))
    projects = int(
        summary.get("project_count")
        or len(project_rows)
        or len(manifest.get("projects") or [])
    )
    files = int(summary.get("file_count") or manifest.get("file_count") or 0)
    size = int(
        report.get("package_bytes")
        or report.get("pack_size")
        or summary.get("bytes")
        or manifest.get("payload_bytes")
        or 0
    )
    verified = bool(report.get("verified", True))
    return "\n".join(
        (
            f"任务：{tasks}",
            f"项目：{projects}",
            f"文件：{files}",
            f"迁移包：{_human_size(size)}",
            f"完整性：{'已验证' if verified else '未验证'}",
        )
    )


class PackPreviewDialog(QDialog):
    def __init__(
        self,
        report: dict[str, Any],
        *,
        allow_import: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("迁移包内容")
        self.resize(660, 610)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)
        outer.addWidget(_role(QLabel("仅查看 · 尚未写入本机"), "eyebrow"))
        outer.addWidget(_role(QLabel("迁移包可以接收"), "heroTitle"))
        summary = QPlainTextEdit(_pack_summary_text(report))
        summary.setReadOnly(True)
        summary.setMinimumHeight(135)
        outer.addWidget(summary)
        tasks = report.get("tasks") if isinstance(report.get("tasks"), list) else []
        if tasks:
            outer.addWidget(_role(QLabel("任务预览"), "cardTitle"))
            task_list = QListWidget()
            task_list.setMinimumHeight(120)
            for task in tasks[:50]:
                if not isinstance(task, dict):
                    continue
                title = str(task.get("name") or task.get("title") or "未命名任务")
                provider = str(task.get("model_provider") or "未知 Provider")
                suffix = str(task.get("id") or "")[-8:]
                task_list.addItem(f"{title}  ·  {provider}  ·  …{suffix}")
            if len(tasks) > 50:
                task_list.addItem(f"另有 {len(tasks) - 50} 个任务，导入时会完整保留")
            outer.addWidget(task_list, 1)
        projects = report.get("projects") if isinstance(report.get("projects"), list) else []
        if projects:
            outer.addWidget(_role(QLabel("项目预览"), "cardTitle"))
            project_list = QListWidget()
            project_list.setMinimumHeight(90)
            for project in projects[:20]:
                if not isinstance(project, dict):
                    continue
                name = str(project.get("name") or project.get("directory_name") or "未命名项目")
                size = _human_size(int(project.get("size") or 0))
                project_list.addItem(f"{name}  ·  {size}")
            outer.addWidget(project_list, 1)
        outer.addWidget(
            _role(
                QLabel("Switchboard 管理的账号和 API Key 不在包内；导入完成后需要在新电脑重新登录或输入。"),
                "secondary",
            )
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if allow_import:
            receive = buttons.addButton("接收此迁移包", QDialogButtonBox.ButtonRole.AcceptRole)
            receive.setProperty("variant", "primary")
            buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)


class ImportPackDialog(QDialog):
    def __init__(
        self,
        package: Path,
        report: dict[str, Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.package = package
        self.projects = report.get("projects") if isinstance(report.get("projects"), list) else []
        manifest = report.get("manifest") if isinstance(report.get("manifest"), dict) else {}
        pack_id = str(manifest.get("pack_id") or package.stem)[-12:]
        base = Path("E:/") if Path("E:/").is_dir() else Path.home() / "Documents"
        self.setWindowTitle("选择接收位置")
        self.resize(720, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)
        outer.addWidget(_role(QLabel("选择新电脑上的保存位置"), "heroTitle"))
        outer.addWidget(
            _role(QLabel("目标 Codex 目录必须为空；现有资料库不会合并或覆盖。"), "secondary")
        )

        form = QFormLayout()
        home_row = QHBoxLayout()
        self.target_home = QLineEdit(os.fspath(base / f"Codex-Home-{pack_id}"))
        choose_home = QPushButton("选择目录")
        choose_home.clicked.connect(self._choose_home)
        home_row.addWidget(self.target_home, 1)
        home_row.addWidget(choose_home)
        form.addRow("Codex 数据目录", home_row)

        project_row = QHBoxLayout()
        self.project_root = QLineEdit(os.fspath(base / "Codex-Projects"))
        self.project_root.textChanged.connect(self._update_mapping)
        choose_projects = QPushButton("选择目录")
        choose_projects.clicked.connect(self._choose_projects)
        project_row.addWidget(self.project_root, 1)
        project_row.addWidget(choose_projects)
        form.addRow("项目统一目录", project_row)
        outer.addLayout(form)

        if self.projects:
            outer.addWidget(_role(QLabel("路径映射预览"), "cardTitle"))
            self.mapping = QPlainTextEdit()
            self.mapping.setReadOnly(True)
            self.mapping.setMinimumHeight(105)
            outer.addWidget(self.mapping)
            self._update_mapping()
        else:
            self.mapping = None

        self.activate_after = QCheckBox("导入成功后设为当前 Codex 数据目录")
        self.activate_after.setChecked(True)
        outer.addWidget(self.activate_after)
        note = _role(
            QLabel("激活只会改变以后启动的 Codex；当前窗口、正在运行的任务和原目录不会被覆盖。"),
            "secondary",
        )
        note.setWordWrap(True)
        outer.addWidget(note)
        outer.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        start = buttons.addButton("开始导入", QDialogButtonBox.ButtonRole.AcceptRole)
        start.setProperty("variant", "primary")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _choose_home(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择 Codex 数据目录的父目录",
            os.fspath(Path(self.target_home.text()).parent),
            QFileDialog.Option.ShowDirsOnly,
        )
        if selected:
            self.target_home.setText(os.fspath(Path(selected) / Path(self.target_home.text()).name))

    def _choose_projects(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择项目统一目录",
            self.project_root.text(),
            QFileDialog.Option.ShowDirsOnly,
        )
        if selected:
            self.project_root.setText(selected)

    def _update_mapping(self, *_args: Any) -> None:
        if not hasattr(self, "mapping") or self.mapping is None:
            return
        root = Path(self.project_root.text().strip() or ".")
        lines = []
        for project in self.projects[:30]:
            if not isinstance(project, dict):
                continue
            source = str(project.get("source_path") or "原路径未知")
            directory = str(project.get("directory_name") or project.get("name") or "project")
            lines.append(f"{source}\n→ {root / directory}")
        self.mapping.setPlainText("\n\n".join(lines))

    def _accept_if_valid(self) -> None:
        home = Path(self.target_home.text().strip()).expanduser()
        if not self.target_home.text().strip():
            QMessageBox.warning(self, "缺少目录", "请选择 Codex 数据目录。")
            return
        if home.exists() and any(home.iterdir()):
            QMessageBox.warning(self, "目录已有内容", "Codex 数据目录必须是新目录或空目录。")
            return
        self.accept()

    def selection(self) -> tuple[Path, Path | None, bool]:
        project = self.project_root.text().strip()
        return (
            Path(self.target_home.text().strip()).expanduser(),
            Path(project).expanduser() if project else None,
            self.activate_after.isChecked(),
        )


class MaintenancePage(QWidget):
    repair_requested = Signal()
    backup_requested = Signal()
    recoveries_requested = Signal()

    def __init__(self, snapshot: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 30)
        outer.setSpacing(20)
        outer.addWidget(PageHeader("安全维护", "只展示必要状态，危险操作永远需要确认。"))

        grid_top = QHBoxLayout()
        grid_top.setSpacing(14)
        config_card = StatusCard(
                "配置投影",
                snapshot["config"]["label"],
                f"revision {snapshot['active']['revision']}",
                tone="green" if snapshot["config"]["ready"] else "blue",
            )
        config_layout = config_card.layout()
        repair = QPushButton("修复配置")
        repair.setEnabled(not bool(snapshot["config"]["ready"]))
        repair.clicked.connect(self.repair_requested.emit)
        config_layout.addWidget(repair)
        grid_top.addWidget(config_card, 1)
        grid_top.addWidget(
            StatusCard(
                "任务索引",
                f"{snapshot['counts']['user_tasks']} 个用户任务",
                f"隐藏 {snapshot['counts']['subagents_hidden']} 个内部子任务",
                tone="green",
            ),
            1,
        )
        outer.addLayout(grid_top)

        recovery_records = QPushButton("历史恢复记录 · 查看退出后的结果")
        recovery_records.clicked.connect(self.recoveries_requested.emit)
        outer.addWidget(recovery_records)

        footprint = Card()
        footprint_layout = QVBoxLayout(footprint)
        footprint_layout.setContentsMargins(22, 20, 22, 20)
        footprint_layout.setSpacing(12)
        footprint_layout.addWidget(_role(QLabel("本地数据占用"), "cardTitle"))
        footprint_layout.addWidget(
            _role(QLabel("只读统计，不自动删除或压缩 Codex 数据库。"), "secondary")
        )
        for item in snapshot["footprint"]:
            row = QHBoxLayout()
            row.addWidget(_role(QLabel(str(item["label"])), "body"))
            row.addStretch(1)
            row.addWidget(_role(QLabel(str(item["size"])), "metric"))
            footprint_layout.addLayout(row)
        actions = QHBoxLayout()
        actions.addStretch(1)
        preview = QPushButton("预览备份保留计划")
        preview.clicked.connect(self.backup_requested.emit)
        actions.addWidget(preview)
        footprint_layout.addLayout(actions)
        outer.addWidget(footprint)
        outer.addStretch(1)


class SwitchboardModernWindow(QMainWindow):
    PAGE_NAMES = ("概览", "任务", "账号", "迁移", "安全维护", "接手记录")

    def __init__(
        self,
        snapshot: dict[str, Any],
        paths: switchboard.Paths | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.snapshot = snapshot
        self.paths = paths or switchboard.Paths(Path(str(snapshot.get("home") or DEFAULT_HOME)))
        self.operations = SwitchboardOperations(self.paths)
        self.action_bus = ActionBus(self)
        self.action_bus.completed.connect(self._action_completed)
        self.action_bus.failed.connect(self._action_failed)
        self.action_bus.progress.connect(self._action_progress)
        self._busy = False
        self._current_label = ""
        self._current_callback: Callable[[Any], None] | None = None
        self._refresh_after_action = False
        self._silent_action = False
        self._cancel_event = threading.Event()
        self._progress_dialog: QProgressDialog | None = None
        self.relay_dialog: RelaySettingsDialog | None = None
        self.setWindowTitle(WINDOW_TITLE)
        self.setWindowIcon(make_app_icon())
        self.resize(1180, 820)
        self.setMinimumSize(980, 700)

        root = QWidget()
        root.setObjectName("appRoot")
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.setCentralWidget(root)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(192)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 22, 18, 20)
        sidebar_layout.setSpacing(8)
        brand_row = QHBoxLayout()
        logo = QLabel("S")
        logo.setFixedSize(34, 34)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            "background:#0071E3;color:white;border-radius:10px;font-size:16px;font-weight:700;"
        )
        brand_row.addWidget(logo)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(_role(QLabel("Switchboard"), "brand"))
        brand_text.addWidget(_role(QLabel("Provider 控制台"), "tiny"))
        brand_row.addLayout(brand_text, 1)
        sidebar_layout.addLayout(brand_row)
        sidebar_layout.addSpacing(22)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[NavButton] = []
        nav_labels = ("概览", "任务", "账号与 Provider", "迁移", "安全维护", "接手记录")
        for index, label in enumerate(nav_labels):
            button = NavButton(label)
            button.clicked.connect(lambda _checked=False, page=index: self.switch_page(page))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            sidebar_layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        sidebar_layout.addStretch(1)

        version = Badge(f"UI {snapshot['version']}", "blue")
        sidebar_layout.addWidget(version, 0, Qt.AlignmentFlag.AlignLeft)
        home = _role(QLabel(snapshot["home"]), "tiny")
        home.setWordWrap(True)
        sidebar_layout.addWidget(home)
        shell.addWidget(sidebar)

        page_surface = QWidget()
        page_surface.setObjectName("pageSurface")
        page_layout = QVBoxLayout(page_surface)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.pages: list[QWidget] = []
        self._install_pages(snapshot)
        page_layout.addWidget(self.stack)
        shell.addWidget(page_surface, 1)

        self.toast = QLabel("", root)
        self.toast.setObjectName("toast")
        self.toast.hide()
        self.toast_timer = QTimer(self)
        self.toast_timer.setSingleShot(True)
        self.toast_timer.timeout.connect(self.toast.hide)
        self.router_timer = QTimer(self)
        self.router_timer.timeout.connect(self._router_tick)
        self.router_timer.start(5000)
        self.handoff_timer = QTimer(self)
        self.handoff_timer.timeout.connect(self._handoff_tick)
        self.handoff_timer.start(2000)

    def _install_pages(self, snapshot: dict[str, Any]) -> None:
        current = self.stack.currentIndex()
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
            widget.deleteLater()
        self.pages = [
            OverviewPage(snapshot),
            TasksPage(snapshot),
            AccountsPage(snapshot),
            MigrationPage(snapshot),
            MaintenancePage(snapshot),
            HandoffPage(),
        ]
        for page in self.pages:
            self.stack.addWidget(page)
        overview = self.pages[0]
        tasks = self.pages[1]
        accounts = self.pages[2]
        migration = self.pages[3]
        maintenance = self.pages[4]
        assert isinstance(overview, OverviewPage)
        assert isinstance(tasks, TasksPage)
        assert isinstance(accounts, AccountsPage)
        assert isinstance(migration, MigrationPage)
        assert isinstance(maintenance, MaintenancePage)
        overview.request_tasks.connect(lambda: self.switch_page(1))
        overview.refresh_requested.connect(self.refresh_snapshot)
        tasks.diagnose_requested.connect(self.diagnose_task)
        tasks.recovery_requested.connect(self.preview_recovery)
        tasks.convert_requested.connect(self.convert_task)
        tasks.handoff_requested.connect(self.start_handoff)
        tasks.open_requested.connect(self.open_task)
        tasks.cleanup_requested.connect(self.cleanup_family)
        accounts.activate_requested.connect(self.activate_profile)
        accounts.configure_requested.connect(self.open_relay_settings)
        accounts.login_requested.connect(self.login_official)
        migration.export_requested.connect(self.export_migration_pack)
        migration.inspect_requested.connect(self.inspect_migration_pack)
        migration.import_requested.connect(self.receive_migration_pack)
        maintenance.repair_requested.connect(self.repair_config)
        maintenance.backup_requested.connect(self.preview_backups)
        maintenance.recoveries_requested.connect(self.show_recoveries)
        handoffs = self.pages[5]
        handoffs.refresh_requested.connect(self.refresh_handoffs)
        handoffs.resume_requested.connect(self.resume_handoff)
        handoffs.cancel_requested.connect(self.cancel_handoff)
        handoffs.open_requested.connect(self.open_handoff)
        handoffs.files_requested.connect(self.open_handoff_files)
        self.stack.setCurrentIndex(max(0, current))

    def run_action(
        self,
        label: str,
        operation: Callable[[Callable[[str], None], threading.Event], Any],
        *,
        callback: Callable[[Any], None] | None = None,
        refresh: bool = False,
        cancellable: bool = False,
        silent: bool = False,
    ) -> bool:
        if self._busy:
            self.show_toast("另一个操作正在进行，请稍候。")
            return False
        self._busy = True
        self._current_label = label
        self._current_callback = callback
        self._refresh_after_action = refresh
        self._silent_action = silent
        self._cancel_event = threading.Event()
        if not silent:
            dialog = QProgressDialog(label, "取消" if cancellable else "", 0, 0, self)
            dialog.setWindowTitle("Codex Switchboard")
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.setMinimumDuration(0)
            dialog.setAutoClose(False)
            dialog.setAutoReset(False)
            if cancellable:
                dialog.canceled.connect(self._cancel_event.set)
            else:
                dialog.setCancelButton(None)
            dialog.show()
            self._progress_dialog = dialog
        self.action_bus.submit(label, operation, self._cancel_event)
        return True

    def _action_progress(self, label: str, message: str) -> None:
        if label == self._current_label and self._progress_dialog is not None:
            self._progress_dialog.setLabelText(message)
            if label == "无损转换任务" and message.startswith("1/5"):
                self._progress_dialog.setCancelButton(None)
            if label == "创建迁移包" and message.startswith("正在盘点"):
                # Cancellation is safe while waiting for Codex to exit. Once
                # the consistent snapshot starts, finish the atomic package.
                self._progress_dialog.setCancelButton(None)

    def _finish_action_state(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
            self._progress_dialog.deleteLater()
            self._progress_dialog = None
        self._busy = False

    def _action_completed(self, label: str, result: Any) -> None:
        if label != self._current_label:
            return
        callback = self._current_callback
        refresh = self._refresh_after_action
        silent = self._silent_action
        self._finish_action_state()
        try:
            if callback is not None:
                callback(result)
        except Exception as exc:  # noqa: BLE001 - UI callback protection
            QMessageBox.critical(self, "结果处理失败", friendly_error(exc))
            return
        if not silent and callback is None:
            self.show_toast(self._result_message(label, result))
        if refresh:
            self.refresh_snapshot(silent=True)

    def _action_failed(self, label: str, error: BaseException) -> None:
        if label != self._current_label:
            return
        silent = self._silent_action
        self._finish_action_state()
        message = friendly_error(error)
        if silent:
            self.show_toast(message)
        else:
            QMessageBox.critical(self, f"{label}失败", message)

    def _result_message(self, label: str, result: Any) -> str:
        if isinstance(result, str):
            return result
        if not isinstance(result, dict):
            return f"{label}已完成"
        operation = str(result.get("operation") or "")
        if operation in {"profile_switch", "profile_repair"}:
            profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
            return f"新任务默认已设为 {profile.get('label') or '所选 Provider'}"
        if operation == "official_login":
            return "官方账号登录完成，已设为新任务默认入口"
        if operation == "task_conversion":
            if result.get("same_task"):
                return "这个任务已经使用所选 Provider，没有创建副本"
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
            suffix = str(thread.get("id") or "")[-8:]
            verified = bool((result.get("completion") or {}).get("restart_verified"))
            if (result.get("completion") or {}).get("core_complete") is False:
                return f"新任务 …{suffix} 已创建，但置顶或核验尚未全部完成；请勿重复创建"
            detail = " · 已二次读取验证" if verified else ""
            return "任务已无损转换，旧任务保持原状" + (f" · …{suffix}" if suffix else "") + detail
        if operation == "family_cleanup":
            return f"已归档 {len(result.get('archived_thread_ids') or [])} 个旧成员"
        return f"{label}已完成"

    def diagnose_task(self, task: dict[str, Any]) -> None:
        """Inspect one frozen selection; never refresh or start an App Server."""
        try:
            require_current_source()
        except RuntimeError as exc:
            QMessageBox.information(self, "请重新打开切换器", str(exc))
            return
        task_id = str(task.get("id") or "")
        task_title = str(task.get("title_full") or task.get("title") or "")[:400]
        if not task_id:
            self.show_toast("请先选择一个任务。")
            return

        def show_report(report: dict[str, Any]) -> None:
            if task_title:
                report = {**report, "title": task_title}
            dialog = TaskHealthDialog(report, self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

            def recheck() -> None:
                dialog.close()
                self.diagnose_task({"id": task_id, "title": task_title})

            dialog.recheck_requested.connect(recheck)
            dialog.open()

        self.run_action(
            "一键只读体检",
            lambda progress, cancel: task_health.diagnose_task(
                self.paths, task_id, progress=progress, cancel_event=cancel),
            callback=show_report,
            cancellable=True,
        )

    def preview_recovery(self, task: dict[str, Any]) -> None:
        try:
            require_current_source()
        except RuntimeError as exc:
            QMessageBox.information(self, "请重新打开切换器", str(exc))
            return
        task_id = str(task.get("id") or "")
        if not task_id:
            return

        def show_preview(preview: dict) -> None:
            dialog = RecoveryPreviewDialog(preview, self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

            def confirmed() -> None:
                if dialog.confirmed:
                    self.enqueue_recovery(dialog.preview["plan"])

            dialog.accepted.connect(confirmed)
            dialog.open()

        self.run_action("只读预览历史恢复",
                        lambda _progress, cancel: projection_recovery.preview_recovery(
                            self.paths, task_id, cancel_event=cancel),
                        callback=show_preview, cancellable=True)

    def enqueue_recovery(self, plan: dict) -> None:
        # Recheck loaded code after consent, before publishing its immutable plan.
        try:
            require_current_source()
        except RuntimeError as exc:
            QMessageBox.information(self, "请重新打开切换器", str(exc))
            return
        frozen = json.loads(json.dumps(plan, ensure_ascii=False))
        self.run_action("确认历史恢复",
                        lambda _progress, _cancel: recovery_jobs.enqueue_recovery(self.paths, frozen, consent=True),
                        callback=lambda _job: self.show_recoveries())

    def show_recoveries(self) -> None:
        def show(jobs: list[dict]) -> None:
            existing = self.findChild(RecoveryRecordsDialog)
            if existing is not None and existing.isVisible():
                existing.update_jobs(jobs)
                existing.raise_()
                return
            dialog = RecoveryRecordsDialog(jobs, self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.refresh_requested.connect(self.show_recoveries)
            dialog.cancel_requested.connect(self.cancel_recovery)
            dialog.reconcile_requested.connect(self.reconcile_recovery)
            dialog.open()

        self.run_action("读取恢复记录", lambda _progress, _cancel: recovery_jobs.list_jobs(self.paths), callback=show)

    def cancel_recovery(self, operation_id: str) -> None:
        self.run_action("请求取消恢复", lambda _progress, _cancel: recovery_jobs.request_cancel(self.paths, operation_id),
                        callback=lambda _job: self.show_recoveries())

    def reconcile_recovery(self, operation_id: str) -> None:
        self.run_action("只读核验原生历史", lambda _progress, _cancel: recovery_jobs.reconcile_job(self.paths, operation_id),
                        callback=lambda _job: self.show_recoveries())

    def refresh_snapshot(self, *, silent: bool = False) -> None:
        self.run_action(
            "刷新状态",
            lambda _progress, _cancel: build_ui_snapshot(self.paths),
            callback=self._apply_snapshot,
            silent=silent,
        )

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        self._install_pages(snapshot)
        self.show_toast("状态已刷新")

    def activate_profile(self, profile: dict[str, Any]) -> None:
        if profile.get("kind") == "中转站" and not profile.get("ready"):
            self.open_relay_settings(profile)
            self.show_toast("请先完成中转站配置。")
            return
        answer = QMessageBox.question(
            self,
            "设置新任务默认入口",
            f"将新任务默认入口设为“{profile.get('title')}”？\n\n"
            "已有任务的 Provider 和聊天记录不会改变。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        profile_id = str(profile.get("id") or "")
        self.run_action(
            "设置默认 Provider",
            lambda _progress, _cancel: self.operations.switch_profile(profile_id),
            refresh=True,
        )

    def login_official(self) -> None:
        blockers = switchboard.appserver_blocking_processes(self.paths)
        message = (
            "这会退出当前官方账号并打开 OpenAI 官方登录页。\n\n"
            "全部 openai 任务随后使用新账号额度；任务 ID 和聊天不变。"
        )
        if blockers:
            message += "\n\n点击确定后请关闭 Codex 主窗口，保持此切换器打开。"
        if QMessageBox.question(self, "登录或切换官方账号", message) != QMessageBox.StandardButton.Yes:
            return
        self.run_action(
            "登录或切换官方账号",
            lambda progress, cancel: self.operations.login_official(
                switch_account=True,
                progress=progress,
                cancel_event=cancel,
            ),
            refresh=True,
            cancellable=True,
        )

    def open_task(self, task: dict[str, Any]) -> None:
        thread_id = str(task.get("id") or "")
        if not thread_id:
            return
        if os.name != "nt":
            QMessageBox.information(self, "任务 ID", thread_id)
            return
        try:
            os.startfile(f"codex://threads/{thread_id}")  # type: ignore[attr-defined]
        except OSError as exc:
            QMessageBox.critical(self, "打开任务失败", friendly_error(exc))

    def start_handoff(self, task: dict[str, Any]) -> None:
        try:
            require_current_source()
            models = switchboard.bundled_model_choices(paths=self.paths)["models"]
        except Exception as exc:
            QMessageBox.information(self, "暂不能开始接手", friendly_error(exc))
            return
        dialog = HandoffDialog(task, models, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selection = dialog.selection()

        def enqueue(_progress, _cancel):
            job = handoff_jobs.enqueue_handoff(self.paths, str(task["id"]), **selection)
            if not job.get("reused"):
                handoff_jobs.launch_worker(self.paths, job["id"])
            return job

        def saved(job):
            self.switch_page(5)
            self.show_toast("已打开原接手记录；不会重复提交。" if job.get("reused") else "请求已保存。请自行完全退出 Codex，再在这里查看进度。")

        self.run_action("保存接手请求", enqueue, callback=saved)

    def refresh_handoffs(self) -> None:
        if self._busy:
            return
        self.run_action("读取接手进度", lambda _p, _c: handoff_jobs.list_jobs(self.paths),
                        callback=lambda jobs: self.pages[5].apply_jobs(jobs), silent=True)

    def _handoff_tick(self) -> None:
        if self.stack.currentIndex() == 5:
            self.refresh_handoffs()

    def resume_handoff(self, ident: str) -> None:
        try:
            require_current_source()
            handoff_jobs.launch_worker(self.paths, ident)
            self.show_toast("继续核验原操作；已提交的回合不会重发。")
        except Exception as exc:
            QMessageBox.warning(self, "无法继续接手", friendly_error(exc))
        self.refresh_handoffs()

    def cancel_handoff(self, ident: str) -> None:
        if QMessageBox.question(self, "停止接手", "停止后不会发送下一步请求。已产生的模型用量不会撤销，结果未知的回合仍需人工核验。") != QMessageBox.StandardButton.Yes:
            return
        try:
            handoff_jobs.request_cancel(self.paths, ident)
            handoff_jobs.launch_worker(self.paths, ident)
        except Exception as exc:
            QMessageBox.warning(self, "无法停止接手", friendly_error(exc))
        self.refresh_handoffs()

    def open_handoff(self, ident: str) -> None:
        try:
            job = handoff_jobs.load_job(self.paths, ident)
            if handoff_jobs.pid_alive(job.get("worker_pid")):
                raise RuntimeError("接手后台尚未退出，请稍候再打开 Codex。")
            self.open_task({"id": job["target_thread"]["thread_id"]})
        except Exception as exc:
            QMessageBox.warning(self, "无法打开新会话", friendly_error(exc))

    def open_handoff_files(self, ident: str) -> None:
        try:
            folder = handoff_jobs.operation_folder(self.paths, ident)
            if not folder.is_dir():
                raise RuntimeError("接手资料目录不存在")
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                QMessageBox.information(self, "接手资料目录", str(folder))
        except Exception as exc:
            QMessageBox.warning(self, "无法打开资料", friendly_error(exc))

    def convert_task(self, task: dict[str, Any]) -> None:
        try:
            require_current_source()
        except RuntimeError as exc:
            QMessageBox.information(self, "请重新打开切换器", str(exc))
            return
        dialog = ConversionDialog(task, list(self.snapshot.get("profiles") or []), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target, model = dialog.selection()
        target_profile = next(
            (item for item in self.snapshot.get("profiles", []) if item.get("id") == target),
            {},
        )
        if target_profile.get("kind") == "中转站" and not target_profile.get("ready"):
            self.open_relay_settings(target_profile)
            return
        blockers = switchboard.appserver_blocking_processes(self.paths)
        message = (
            f"把“{task.get('title_full') or task.get('title')}”无损转换到 "
            f"{target_profile.get('title') or target}？\n\n"
            "完整历史会复制到新任务并二次读取验证；旧任务保持原状。"
        )
        if model:
            message += f"\n目标模型：{model}"
        if blockers:
            message += "\n\n点击确定后请关闭 Codex 主窗口，保持此切换器打开。"
        if QMessageBox.question(self, "确认无损转换", message) != QMessageBox.StandardButton.Yes:
            return
        self.run_action(
            "无损转换任务",
            lambda progress, cancel: self.operations.convert_task(
                task,
                target,
                model,
                progress=progress,
                cancel_event=cancel,
            ),
            refresh=True,
            cancellable=True,
        )

    def cleanup_family(self, task: dict[str, Any]) -> None:
        if task.get("role") != "head" or task.get("archived"):
            QMessageBox.information(self, "请选择当前 head", "只有活跃的当前 head 可以整理家族。")
            return
        try:
            plan = switchboard.thread_family_cleanup_plan(self.paths, str(task.get("id") or ""))
        except Exception as exc:
            QMessageBox.critical(self, "无法生成整理计划", friendly_error(exc))
            return
        count = int(plan.get("candidate_count") or 0)
        if count == 0:
            self.show_toast("这个家族已经无需整理。")
            return
        message = f"归档这个家族的 {count} 个活跃旧成员，并保留/置顶当前 head？\n\n不会永久删除聊天。"
        if switchboard.appserver_blocking_processes(self.paths):
            message += "\n\n点击确定后请关闭 Codex 主窗口。"
        if QMessageBox.question(self, "整理任务家族", message) != QMessageBox.StandardButton.Yes:
            return
        self.run_action(
            "整理任务家族",
            lambda progress, cancel: self.operations.cleanup_family(
                task,
                progress=progress,
                cancel_event=cancel,
            ),
            refresh=True,
            cancellable=True,
        )

    def open_relay_settings(self, profile: dict[str, Any]) -> None:
        dialog = RelaySettingsDialog(profile, self)
        self.relay_dialog = dialog
        dialog.save_metadata.connect(self._save_relay_metadata)
        dialog.save_key.connect(self._save_relay_key)
        dialog.import_models.connect(self._import_models)
        dialog.generate_catalog.connect(self._generate_catalog)
        dialog.probe_models.connect(self._probe_models)
        dialog.finished.connect(lambda _result: setattr(self, "relay_dialog", None))
        dialog.show()

    def _save_relay_metadata(self, values: dict[str, Any]) -> None:
        self.run_action(
            "保存中转站配置",
            lambda _progress, _cancel: self.operations.configure_relay(values),
            callback=lambda _result: self._relay_status("地址和模型已保存。"),
            refresh=True,
        )

    def _save_relay_key(self, profile_id: str, secret: str) -> None:
        self.run_action(
            "加密保存 Key",
            lambda _progress, _cancel: self.operations.save_relay_key(profile_id, secret),
            callback=lambda result: self._relay_status(str(result)),
            refresh=True,
        )

    def _import_models(self, _profile_id: str) -> None:
        self.run_action(
            "读取本机模型",
            lambda _progress, _cancel: self.operations.import_models(),
            callback=self._models_imported,
        )

    def _models_imported(self, result: dict[str, Any]) -> None:
        models = [str(item) for item in result.get("models") or []]
        if self.relay_dialog is not None:
            self.relay_dialog.apply_imported_models(models)

    def _generate_catalog(self, values: dict[str, Any]) -> None:
        self.run_action(
            "生成模型目录",
            lambda _progress, _cancel: self.operations.generate_catalog(values),
            callback=lambda result: self._relay_status(
                f"模型目录已生成：{result.get('model_count', 0)} 个模型。"
            ),
            refresh=True,
        )

    def _probe_models(self, profile_id: str) -> None:
        answer = QMessageBox.question(
            self,
            "检测远端模型",
            "将发送一次 GET /v1/models。不会发送聊天或推理请求，不自动重试，也不修改本地模型列表。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.run_action(
            "检测远端模型",
            lambda _progress, _cancel: self.operations.probe_models(profile_id),
            callback=self._models_probed,
        )

    def _models_probed(self, result: dict[str, Any]) -> None:
        supported = list(result.get("supported_configured") or [])
        missing = list(result.get("missing_remote") or [])
        message = f"远端返回 {result.get('remote_count', 0)} 个模型；已配置可用 {len(supported)} 个。"
        if missing:
            message += "\n疑似不支持：" + "、".join(str(item) for item in missing[:8])
        self._relay_status(message)

    def _relay_status(self, message: str) -> None:
        if self.relay_dialog is not None:
            self.relay_dialog.status.setText(message)
        self.show_toast(message)

    def export_migration_pack(self) -> None:
        if not _looks_like_codex_home(self.paths.codex_home):
            QMessageBox.warning(self, "没有可迁出的资料库", "请先选择一个有效的 Codex 数据目录。")
            return
        dialog = ExportPackDialog(self.paths.codex_home, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        destination, projects, include_projection = dialog.selection()
        if destination.exists():
            QMessageBox.warning(self, "文件已经存在", "请选择一个新的迁移包文件名；现有文件不会被覆盖。")
            return
        message = (
            f"将在以下位置创建迁移包：\n{destination}\n\n"
            f"包含 {len(projects)} 个手动选择的项目目录。Switchboard 账号凭据、Key、日志和缓存不会包含；源码中手写的秘密需自行确认。\n\n"
            "开始后请关闭 Codex；项目也应停止写入，切换器会在确认 Codex 完全退出后再创建快照。"
        )
        if QMessageBox.question(self, "确认创建迁移包", message) != QMessageBox.StandardButton.Yes:
            return
        self.run_action(
            "创建迁移包",
            lambda progress, cancel: self.operations.create_migration_pack(
                destination,
                projects,
                include_projection=include_projection,
                progress=progress,
                cancel_event=cancel,
            ),
            callback=self._migration_pack_created,
            cancellable=True,
        )

    def _migration_pack_created(self, result: dict[str, Any]) -> None:
        package = str(result.get("package") or result.get("path") or "").strip()
        message = "迁移包已创建并验证。"
        if package:
            message += f"\n\n{package}"
        QMessageBox.information(self, "迁移包已完成", message)

    def _choose_migration_pack(self) -> Path | None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "选择 Codex 迁移包",
            os.fspath(Path.home()),
            "Codex 迁移包 (*.codexpack)",
        )
        return Path(selected) if selected else None

    def inspect_migration_pack(self) -> None:
        package = self._choose_migration_pack()
        if package is None:
            return
        self.run_action(
            "验证迁移包",
            lambda _progress, _cancel: self.operations.inspect_migration_pack(package),
            callback=lambda result: PackPreviewDialog(result, allow_import=False, parent=self).exec(),
        )

    def receive_migration_pack(self) -> None:
        package = self._choose_migration_pack()
        if package is None:
            return
        self.run_action(
            "验证迁移包",
            lambda _progress, _cancel: self.operations.inspect_migration_pack(package),
            callback=lambda result: self._migration_pack_ready(package, result),
        )

    def _migration_pack_ready(self, package: Path, report: dict[str, Any]) -> None:
        preview = PackPreviewDialog(report, allow_import=True, parent=self)
        if preview.exec() != QDialog.DialogCode.Accepted:
            return
        dialog = ImportPackDialog(package, report, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target_home, project_root, activate_after = dialog.selection()
        message = (
            "下一步才会开始写入。现有 Codex 资料库不会被合并或覆盖。\n\n"
            f"Codex 数据目录：{target_home}\n"
            f"项目目录：{project_root or '迁移包不包含项目'}"
        )
        if QMessageBox.question(self, "确认开始导入", message) != QMessageBox.StandardButton.Yes:
            return
        self.run_action(
            "导入迁移包",
            lambda progress, _cancel: self.operations.import_migration_pack(
                package,
                target_home,
                project_root,
                progress=progress,
            ),
            callback=lambda result: self._migration_imported(result, target_home, activate_after),
        )

    def _migration_imported(
        self,
        result: dict[str, Any],
        target_home: Path,
        activate_after: bool,
    ) -> None:
        activated = False
        activation_error = ""
        if activate_after:
            try:
                activate_codex_home(target_home)
                activated = True
            except (OSError, ValueError) as exc:
                activation_error = friendly_error(exc)
        summary = _pack_summary_text(result)
        message = f"迁移包已接收。\n\n{summary}"
        if activated:
            message += "\n\n新的数据目录已保存。请完全退出并重新打开 Codex。"
        elif activation_error:
            message += f"\n\n数据已安全导入，但自动激活失败：{activation_error}"
        else:
            message += f"\n\n数据保存在：{target_home}"
        QMessageBox.information(self, "迁移完成", message)

    def repair_config(self) -> None:
        status = switchboard.config_projection_status(self.paths)
        if status.get("ready"):
            self.show_toast("配置已经同步。")
            return
        if not status.get("repairable"):
            QMessageBox.warning(self, "暂不能修复", "；".join(status.get("reasons") or []))
            return
        if QMessageBox.question(
            self,
            "修复配置",
            "按 active.json 和不可变 Provider 版本重新发布 config.toml？\n\n不会修改任务、聊天或 active revision。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.run_action(
            "修复配置",
            lambda _progress, _cancel: self.operations.repair_config(),
            refresh=True,
        )

    def preview_backups(self) -> None:
        self.run_action(
            "读取备份保留计划",
            lambda _progress, _cancel: self.operations.backup_plan(),
            callback=lambda result: BackupPlanDialog(result, self).exec(),
        )

    def _router_tick(self) -> None:
        if self._busy or not _looks_like_codex_home(self.paths.codex_home):
            return
        if not switchboard.router_required(self.paths):
            return
        if not _local_router_status(self.paths)["healthy"]:
            self.run_action(
                "恢复本地路由器",
                lambda _progress, _cancel: self.operations.ensure_router(),
                callback=lambda _result: self.refresh_snapshot(silent=True),
                silent=True,
            )

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self.toast.isVisible():
            self._position_toast()

    def _position_toast(self) -> None:
        self.toast.adjustSize()
        x = self.width() - self.toast.width() - 28
        y = self.height() - self.toast.height() - 24
        self.toast.move(max(12, x), max(12, y))

    def show_toast(self, message: str) -> None:
        self.toast.setText(message)
        self._position_toast()
        self.toast.show()
        self.toast.raise_()
        self.toast_timer.start(2200)

    def switch_page(self, index: int) -> None:
        if not 0 <= index < self.stack.count():
            return
        self.nav_buttons[index].setChecked(True)
        self.stack.setCurrentIndex(index)
        if index == 5:
            self.refresh_handoffs()
        page = self.stack.currentWidget()
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", page)
        animation.setDuration(170)
        animation.setStartValue(0.35)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(lambda: page.setGraphicsEffect(None))
        page._fade_animation = animation  # type: ignore[attr-defined]
        animation.start()

    def select_page(self, page: str) -> None:
        normalized = page.strip().casefold()
        aliases = {
            "overview": 0,
            "概览": 0,
            "tasks": 1,
            "task": 1,
            "任务": 1,
            "accounts": 2,
            "account": 2,
            "账号": 2,
            "migration": 3,
            "迁移": 3,
            "maintenance": 4,
            "维护": 4,
            "handoff": 5,
            "接手": 5,
            "接手记录": 5,
        }
        self.switch_page(aliases.get(normalized, 0))

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if self._busy:
            QMessageBox.information(
                self,
                "操作正在进行",
                "请等待操作完成；如果正在等待关闭 Codex，可以在进度窗口点击取消。",
            )
            event.ignore()
            return
        event.accept()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Switchboard modern UI")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--demo", action="store_true", help="use synthetic secret-free preview data")
    parser.add_argument("--page", default="overview", help="initial page")
    parser.add_argument("--screenshot", type=Path, help="save the rendered window and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Codex Switchboard")
    app.setWindowIcon(make_app_icon())
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLE)
    home = (
        args.home.expanduser().resolve()
        if args.home is not None
        else DEFAULT_HOME
        if args.demo
        else discover_codex_home()
    )
    bootstrap = False
    if home is None:
        home = choose_codex_home()
        if home is None:
            home = (Path.home() / ".codex").resolve()
            bootstrap = True
    guard: ModernInstanceGuard | None = None
    if args.screenshot is None:
        guard = ModernInstanceGuard(home)
        if not guard.acquire():
            focus_existing_preview()
            return 0
    try:
        snapshot = (
            demo_snapshot()
            if args.demo
            else bootstrap_snapshot(home)
            if bootstrap or not _looks_like_codex_home(home)
            else build_ui_snapshot(switchboard.Paths(home))
        )
        window = SwitchboardModernWindow(snapshot, switchboard.Paths(home))
        window.select_page("migration" if bootstrap and args.page == "overview" else args.page)
        window.show()
        if args.screenshot is not None:
            target = args.screenshot.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)

            def capture() -> None:
                app.processEvents()
                if not window.grab().save(os.fspath(target)):
                    raise RuntimeError(f"failed to save screenshot: {target}")
                app.quit()

            QTimer.singleShot(600, capture)
        return app.exec()
    finally:
        if guard is not None:
            guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
