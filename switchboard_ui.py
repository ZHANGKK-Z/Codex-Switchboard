"""Windows desktop UI for the Codex Switchboard.

The UI is deliberately a thin presentation layer over :mod:`switchboard`.
It never stores or prints a relay key in plaintext, never starts a turn, and
never exposes the full account/App Server response in the log.  Provider
network traffic remains inside the core provider layer; the UI only invokes
explicit bounded operations and never handles a returned key.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import http.client
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk

import switchboard


DEFAULT_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
UI_VERSION = "0.8.1"
WINDOW_TITLE = "Codex Switchboard — Provider / 账号切换"
THREAD_SORT_OPTIONS = {
    "Codex 列表顺序": switchboard.THREAD_SORT_CODEX,
    "最近使用时间": switchboard.THREAD_SORT_RECENT,
}


def single_instance_mutex_name(home: Path) -> str:
    """Derive a stable Windows mutex name without exposing the home path."""

    normalized = os.path.normcase(os.path.abspath(os.fspath(home))).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"Local\\CodexSwitchboardUI-{digest}"


class SingleInstanceGuard:
    """Keep one Switchboard UI per authoritative CODEX_HOME."""

    def __init__(self, home: Path) -> None:
        self.name = single_instance_mutex_name(home)
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
        if int(kernel32.GetLastError()) == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def close(self) -> None:
        if self._handle is not None and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


def focus_existing_window() -> bool:
    """Best-effort focus for the already running Windows UI."""

    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    window = user32.FindWindowW(None, WINDOW_TITLE)
    if not window:
        return False
    user32.ShowWindow(window, 9)  # SW_RESTORE
    user32.SetForegroundWindow(window)
    return True


def router_health(paths: switchboard.Paths, timeout: float = 1.5) -> tuple[bool, str]:
    """Check only the local switchboard health endpoint."""

    document = switchboard.load_profiles(paths)
    router = document.get("router", {})
    host = str(router.get("host", switchboard.DEFAULT_ROUTER_HOST))
    port = int(router.get("port", switchboard.DEFAULT_ROUTER_PORT))
    connection: http.client.HTTPConnection | None = None
    try:
        # Use a direct socket instead of urllib's environment proxy handling.
        # Some Windows environments expose a malformed NO_PROXY variable;
        # a localhost health check must never be sent through that proxy.
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.request("GET", "/health", headers={"Cache-Control": "no-cache"})
        response = connection.getresponse()
        body = response.read(4096).decode("utf-8", errors="replace")
        if response.status != 200:
            return False, f"HTTP {response.status}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        if payload.get("status") != "ok":
            return False, "响应不是 ok"
        return True, f"{host}:{port} 正常"
    except (OSError, ValueError) as exc:
        return False, str(exc)
    finally:
        if connection is not None:
            connection.close()


def profile_rows(paths: switchboard.Paths) -> list[dict[str, Any]]:
    """Return secret-free profile rows for the tree view and smoke tests."""

    document = switchboard.load_profiles(paths)
    active = switchboard.load_active(paths)
    active_alias = str(active.get("provider_alias") or "")
    try:
        active_version = switchboard.profile_for_provider_alias(paths, active_alias)
    except (RuntimeError, ValueError):
        active_version = {}
    rows: list[dict[str, Any]] = []
    for profile in document.get("profiles", []):
        profile_id = str(profile.get("id") or "")
        key_ref = str(profile.get("key_ref") or "")
        kind = str(profile.get("kind") or "")
        if kind == "official":
            configured_models: list[str] = []
            ready = bool(profile.get("enabled", False))
            key_state = "官方登录"
            catalog_state = "官方目录"
            catalog_ready = True
        else:
            configured_models = switchboard.relay_model_ids(profile)
            key_state = "已配置" if key_ref and switchboard.key_path(paths, key_ref).exists() else "未配置"
            catalog = switchboard.model_catalog_status(
                paths,
                profile_id,
            )
            catalog_state = {
                "ready": "目录就绪",
                "missing": "目录待生成",
                "stale": "目录需刷新",
                "invalid": "目录损坏",
            }.get(str(catalog.get("state")), "目录待生成")
            if catalog.get("ready"):
                catalog_state += f"（{len(configured_models)} 个模型）"
            catalog_ready = bool(catalog.get("ready"))
            ready = bool(
                profile.get("enabled", False)
                and str(profile.get("base_url") or "").strip()
                and str(profile.get("model") or "").strip()
                and bool(configured_models)
                and key_state == "已配置"
            )
        rows.append(
            {
                "id": profile_id,
                "label": str(profile.get("label") or profile_id),
                "kind": kind,
                "base_url": str(profile.get("base_url") or ""),
                "model": str(profile.get("model") or ""),
                "models": configured_models,
                "model_count": len(configured_models),
                "key_ref": key_ref,
                "key_state": key_state,
                "catalog_state": catalog_state,
                "catalog_ready": catalog_ready,
                "catalog_path": str(switchboard.model_catalog_path(paths, profile_id)) if kind == "relay" else "",
                "enabled": bool(profile.get("enabled", False)),
                "ready": ready,
                "active": profile_id == active.get("profile_id"),
                "provider_alias": active_alias if profile_id == active.get("profile_id") else "",
                "active_version_current": bool(
                    profile_id == active.get("profile_id")
                    and (
                        kind == "official"
                        or (
                            active_version.get("profile_id") == profile_id
                            and all(
                                str(active_version.get(field) or "") == str(profile.get(field) or "")
                                for field in ("base_url", "model", "key_ref")
                            )
                        )
                    )
                ),
            }
        )
    return rows


def task_overview_rows(
    paths: switchboard.Paths,
    *,
    limit: int = 500,
    include_archived: bool = True,
    include_subagents: bool = False,
    sort_mode: str = switchboard.THREAD_SORT_CODEX,
    query: str = "",
    bindings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return a bounded, family-aware, secret-free task projection."""

    rows: list[dict[str, Any]] = []
    for binding in switchboard.thread_family_bindings(
        paths,
        limit=limit,
        include_archived=include_archived,
        include_subagents=include_subagents,
        sort_mode=sort_mode,
        query=query,
        bindings=bindings,
    ):
        archived = bool(int(binding.get("archived") or 0))
        pinned = bool(binding.get("codex_pinned_index")) or bool(
            int(binding.get("is_pinned") or 0)
        )
        is_subagent = bool(binding.get("is_subagent"))
        state = "已归档" if archived else "活跃"
        if pinned:
            state += " · 置顶"
        role = str(binding.get("family_role") or "")
        state += {
            "head": " · 当前 head",
            "ancestor": " · 历史祖先",
            "branch": " · 并行分支",
        }.get(role, "")
        if is_subagent:
            state += " · 内部子任务"
        thread_id = str(binding.get("thread_id") or "")
        family_id = str(binding.get("family_id") or thread_id)
        rows.append(
            {
                "thread_id": thread_id,
                "id_suffix": thread_id[-8:],
                "display_name": str(binding.get("display_name") or thread_id or "未命名任务"),
                "profile_label": str(binding.get("profile_label") or "未知 Provider"),
                "provider_alias": str(binding.get("provider_alias") or ""),
                "model": str(binding.get("model") or "由任务/账号决定"),
                "state": state,
                "family_id": family_id,
                "family_suffix": family_id[-8:],
                "family_size": int(binding.get("family_size") or 1),
                "family_role": role,
                "archived": archived,
                "is_pinned": pinned,
                "is_subagent": is_subagent,
                "cwd": str(binding.get("cwd") or ""),
            }
        )
    return rows


def config_projection_text(status: dict[str, Any]) -> str:
    if status.get("ready"):
        return "配置投影：正常（与当前默认入口一致）"
    reasons = [str(reason) for reason in status.get("reasons", []) if str(reason)]
    detail = "；".join(reasons[:2]) or "状态无法校验"
    return f"配置投影：需要修复（{detail}）"


def model_probe_text(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "远端模型：未检测（仅手动请求 /models）"
    remote_count = int(result.get("remote_count") or 0)
    supported = len(result.get("supported_configured") or [])
    missing = list(result.get("missing_remote") or [])
    text = f"远端模型：返回 {remote_count} 个；当前配置可用 {supported} 个"
    if missing:
        text += "；疑似不支持：" + "、".join(str(item) for item in missing[:5])
    return text


def backup_retention_text(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "尚未统计；策略只生成候选，不会自动删除。"
    policy = result.get("policy") if isinstance(result.get("policy"), dict) else {}
    return (
        f"共 {result.get('entry_count', 0)} 项 / {result.get('total_size', '0 B')}；"
        f"策略候选 {result.get('candidate_count', 0)} 项 / {result.get('candidate_size', '0 B')}。"
        f"每类保留最近 {policy.get('keep_per_series', '?')} 份，"
        f"且仅标记超过 {policy.get('min_age_days', '?')} 天的旧副本；不会自动删除。"
    )


def summarize_result(label: str, result: Any) -> str:
    """Create a bounded UI message without serializing secrets or auth data."""

    if not isinstance(result, dict):
        return f"{label}完成"
    active = result.get("active")
    profile = result.get("profile")
    source_thread = result.get("source_thread")
    thread = result.get("thread")
    binding = result.get("binding")
    completion = result.get("completion")
    strict_complete = completion.get("complete") if isinstance(completion, dict) else None
    parts = [f"{label}{'完成' if strict_complete is not False else '部分完成'}"]
    if result.get("operation") == "model_probe":
        parts.append(
            f"远端返回 {result.get('remote_count', 0)} 个模型，"
            f"当前配置匹配 {len(result.get('supported_configured') or [])} 个"
        )
        missing = result.get("missing_remote") or []
        if missing:
            parts.append("疑似不支持：" + "、".join(str(item) for item in missing[:6]))
        parts.append("未发送推理请求")
        return "；".join(parts)
    if result.get("operation") == "backup_retention":
        parts.append(
            f"备份 {result.get('entry_count', 0)} 项 / {result.get('total_size', '0 B')}，"
            f"策略候选 {result.get('candidate_count', 0)} 项 / {result.get('candidate_size', '0 B')}"
        )
        parts.append("未删除任何文件")
        return "；".join(parts)
    if result.get("operation") == "family_cleanup":
        parts[0] = f"{label}{'完成' if result.get('complete') else '部分完成'}"
        archived = result.get("archived_thread_ids") or []
        parts.append(f"已归档 {len(archived)} 个旧成员（可恢复）")
        parts.append(f"保留 head：{result.get('head_thread_id', '')}")
        if result.get("warning"):
            parts.append(str(result["warning"]))
        return "；".join(parts)
    if isinstance(profile, dict):
        parts.append(str(profile.get("label") or profile.get("id") or ""))
    if isinstance(active, dict):
        parts.append(f"revision {active.get('revision', '?')}")
    if isinstance(source_thread, dict) and isinstance(thread, dict):
        if result.get("same_task"):
            parts.append("所选任务已经使用目标 Provider；未创建副本")
        else:
            cleanup = result.get("cleanup")
            if isinstance(cleanup, dict) and cleanup.get("source_archived"):
                parts.append(f"旧任务已归档（可恢复）：{source_thread.get('id', '')}")
            else:
                parts.append(f"旧任务仍保留：{source_thread.get('id', '')}")
            action = "复用任务" if result.get("reused") else "新任务"
            parts.append(
                f"{action}：{thread.get('id', '')} / Provider："
                f"{result.get('provider_alias') or thread.get('modelProvider', '')}"
            )
            if result.get("model"):
                parts.append(f"模型：{result['model']}")
            if isinstance(cleanup, dict) and cleanup.get("head_pinned"):
                parts.append("新任务已进入置顶区")
            if isinstance(cleanup, dict) and cleanup.get("warning"):
                parts.append(str(cleanup["warning"]))
            if result.get("recovered_post_commit"):
                parts.append("已自动接管并完成上次的持久化结果")
            if isinstance(completion, dict):
                if completion.get("navigation_launched"):
                    parts.append("已打开新任务")
                else:
                    parts.append("未能自动打开；可用已复制的任务 ID 手动打开")
    elif isinstance(binding, dict):
        parts.append(
            f"任务 {binding.get('thread_id', '')} / "
            f"{binding.get('profile_label') or binding.get('provider_alias', '')} "
            f"({binding.get('provider_alias', '')})"
        )
    elif isinstance(thread, dict):
        parts.append(f"任务 {thread.get('id', '')} / {thread.get('modelProvider', '')}")
    config = result.get("config")
    if isinstance(config, dict) and config.get("changed") is True:
        parts.append("配置已更新")
    catalog = result.get("model_catalog")
    if isinstance(catalog, dict) and catalog.get("ready"):
        parts.append(
            f"模型目录已就绪：{catalog.get('model_count') or 1} 个模型"
        )
    return "；".join(part for part in parts if part)


def friendly_error_message(error: BaseException) -> str:
    """Translate expected switchboard failures without exposing internals."""

    if isinstance(error, switchboard.UnreadableThreadHistoryError):
        candidates = "、".join(error.candidate_thread_ids)
        suggestion = f" 可读候选任务 ID：{candidates}。" if candidates else ""
        return (
            "这个任务的聊天原文件仍在，但桌面端无法显示它的历史；继续切换会再次白屏。"
            "本次未发布 Provider 配置。"
            f"{suggestion}请改用可读候选或先创建无损恢复副本。"
        )
    if isinstance(error, switchboard.PartialThreadConversionError):
        return (
            f"新任务 {error.thread_id} 已经创建，但本地安全验收未完成。"
            "请不要重复转换；任务 ID 已保留，可刷新配置总览后继续处理。"
        )
    message = str(error)
    if "another Switchboard task conversion is already in progress" in message:
        return "已有一个任务转换正在执行，请等待它完成后再试；本次没有创建新任务。"
    if "family cleanup requires the Codex desktop/App Server to be closed" in message:
        return "Codex 桌面端或 App Server 仍在运行。请重新点击家族整理，然后按提示关闭 Codex。"
    if "requires the Codex desktop/App Server to be closed" in message:
        return "Codex 桌面端或 App Server 仍在运行。请重新点击复制按钮，然后关闭 Codex。"
    if "timed out waiting for the Codex desktop/App Server to close" in message:
        return "等待 Codex 退出超时，未执行任务切换。可以重新点击后再关闭 Codex。"
    if "thread switch wait was cancelled" in message or "thread copy wait was cancelled" in message:
        return "已取消等待；任务和 Provider 均未改变。"
    if "thread/fork" in message and "modelProvider mismatch" in message:
        return (
            "App Server 没有把目标 Provider 持久化到新任务。"
            "原任务未改写；请确认 Codex 已完全退出后重试。"
        )
    if "WinError 5" in message or "access denied" in message.lower() or "PermissionError" in message:
        return (
            "Windows 拒绝启动 App Server。通常是直接启动 WindowsApps 版 codex.exe 的权限限制；"
            "切换器会优先使用桌面端发布到用户目录的 Codex CLI。请重新点击一次；若仍失败，请更新 Codex 桌面端。"
        )
    if "failed to export bundled Codex model catalog" in message:
        return "无法读取当前桌面 Codex 的内置模型目录。请更新 Codex 桌面端后，再点击“生成/刷新模型目录”。"
    if "does not contain" in message and "bundled Codex model catalog" in message:
        return "当前桌面 Codex 的内置目录没有目标模型；请更新桌面端后，再刷新模型目录。"
    if "bundled Codex model catalog entry" in message and "incomplete" in message:
        return "当前桌面 Codex 的目标模型定义不完整；请更新桌面端后，再刷新模型目录。"
    if "base_instructions" in message and "model" in message.lower():
        return "旧模型目录不完整。请点击“生成/刷新模型目录”，然后重新执行任务复制。"
    if "relay model detection returned HTTP 401" in message or "relay model detection returned HTTP 403" in message:
        return "中转站拒绝了模型目录请求，请检查 Key 权限；没有发送推理请求。"
    if "relay model detection" in message:
        return "无法读取中转站模型目录；没有发送推理请求，也没有自动重试。"
    if "profile is disabled" in message:
        return "目标入口尚未启用，请先完成中转站配置。"
    return message[:1200]


def child_environment(paths: switchboard.Paths) -> dict[str, str]:
    """Build the environment for a router/app-server child under E:."""

    runtime_tmp = paths.runtime_tmp
    runtime_tmp.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["CODEX_HOME"] = os.fspath(paths.codex_home)
    environment["TEMP"] = os.fspath(runtime_tmp)
    environment["TMP"] = os.fspath(runtime_tmp)
    return environment


def official_login_summary(paths: switchboard.Paths) -> str:
    """Read the CLI's bounded login status without exposing account tokens."""

    try:
        executable = switchboard.resolve_appserver_executable(paths=paths)
        completed = subprocess.run(
            [executable, "login", "status"],
            cwd=os.fspath(paths.codex_home),
            env=child_environment(paths),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        output = completed.stdout.strip().lower()
        if completed.returncode == 0 and "chatgpt" in output and "logged in" in output:
            return "官方登录：已登录 ChatGPT（官方额度）"
        if "api key" in output and "logged in" in output:
            return "官方登录：当前是 API Key，不是 ChatGPT 订阅额度"
        return "官方登录：未登录 ChatGPT"
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return "官方登录：状态暂不可读"


def open_codex_thread(thread_id: str) -> None:
    """Launch Codex and navigate through its registered official deep link."""

    normalized = thread_id.strip()
    if not normalized:
        raise ValueError("thread id is required")
    if os.name != "nt":
        raise RuntimeError("自动打开任务目前只支持 Windows")
    os.startfile(f"codex://threads/{normalized}")  # type: ignore[attr-defined]


class SwitchboardUI:
    """Small, keyboard-friendly Tk UI for everyday profile switching."""

    def __init__(self, root: tk.Tk, paths: switchboard.Paths) -> None:
        self.root = root
        self.paths = paths
        self.jobs: queue.Queue[tuple[str, str, Any]] = queue.Queue()
        self.busy = False
        self.closed = False
        self.waiting_for_exit = False
        self.cancel_wait_event = threading.Event()
        self.router_process: subprocess.Popen[str] | None = None
        self._router_start_lock = threading.Lock()
        self._router_recovery_running = False
        self._router_recovery_failures = 0
        self._router_next_recovery_at = 0.0
        # One UI-owned, in-memory inventory snapshot feeds both task views.
        # A refresh or successful task mutation replaces it atomically; it is
        # never persisted and never becomes an identity or Provider authority.
        self._task_inventory: list[dict[str, Any]] | None = None
        self._task_inventory_loading = False
        self._task_inventory_refresh_pending = False
        self._profile_rows: dict[str, dict[str, Any]] = {}
        self._thread_choices: dict[str, dict[str, Any]] = {}
        self._overview_rows: dict[str, dict[str, Any]] = {}
        self._model_probe_results: dict[str, dict[str, Any]] = {}
        self._backup_retention_result: dict[str, Any] | None = None
        self._config_projection: dict[str, Any] = {}
        self._last_config_projection_signature: tuple[Any, ...] | None = None

        self.active_var = tk.StringVar(value="默认 Provider：读取中")
        self.account_var = tk.StringVar(value="官方登录：读取中")
        self.home_var = tk.StringVar(value=f"CODEX_HOME：{paths.codex_home}")
        self.router_var = tk.StringVar(value="本地路由器：检查中")
        self.config_drift_var = tk.StringVar(value="配置投影：检查中")
        self.revision_var = tk.StringVar(value="revision：-")
        self.config_profile_var = tk.StringVar(value="maylily")
        self.base_url_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.models_var = tk.StringVar()
        self.catalog_var = tk.StringVar(value="模型目录：未生成")
        self.remote_models_var = tk.StringVar(value=model_probe_text(None))
        self.enabled_var = tk.BooleanVar(value=True)
        self.key_var = tk.StringVar()
        self.thread_id_var = tk.StringVar()
        self.thread_choice_var = tk.StringVar()
        self.thread_target_var = tk.StringVar(value="maylily")
        self.thread_model_var = tk.StringVar()
        self.thread_cwd_var = tk.StringVar()
        self.thread_binding_var = tk.StringVar(value="当前绑定：尚未查询")
        self.overview_query_var = tk.StringVar()
        self.overview_include_archived_var = tk.BooleanVar(value=True)
        self.overview_show_subagents_var = tk.BooleanVar(value=False)
        self.overview_sort_var = tk.StringVar(value="Codex 列表顺序")
        self.backup_policy_var = tk.StringVar(value=backup_retention_text(None))
        self.status_var = tk.StringVar(value="就绪")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_jobs)
        self.root.after(2000, self._poll_config_drift)
        self.root.after(250, self._poll_router_health)
        self.refresh()

    def _build_ui(self) -> None:
        self.root.title(WINDOW_TITLE)
        self.root.geometry("1080x920")
        self.root.minsize(940, 730)
        try:
            self.root.iconname("Codex Switchboard")
        except tk.TclError:
            pass

        style = ttk.Style(self.root)
        for theme in ("vista", "xpnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Muted.TLabel", foreground="#606770")
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=30)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        # Accounts are the primary content.  Keep at least the header and all
        # three built-in profiles visible even when Windows DPI scaling grows
        # the fixed-size status controls.  Secondary forms live in tabs below
        # instead of competing for vertical space as always-visible rows.
        outer.rowconfigure(2, weight=3, minsize=225)
        outer.rowconfigure(3, weight=2, minsize=225)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Codex Switchboard", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="新任务使用默认 Provider；已有任务按创建时绑定，跨 Provider 通过无损副本迁移",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(header, text=f"UI {UI_VERSION}", style="Muted.TLabel").grid(
            row=0, column=1, rowspan=2, sticky="e"
        )

        status = ttk.LabelFrame(outer, text="当前状态", padding=9)
        status.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        status.columnconfigure(1, weight=1)
        ttk.Label(status, textvariable=self.active_var, style="Status.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 20)
        )
        ttk.Label(status, textvariable=self.revision_var).grid(row=0, column=1, sticky="w")
        ttk.Label(status, textvariable=self.account_var, style="Status.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(status, textvariable=self.home_var, style="Muted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(status, textvariable=self.router_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(status, textvariable=self.config_drift_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Button(status, text="刷新状态", command=self.refresh).grid(row=0, column=2, sticky="e")
        ttk.Button(status, text="启动/检查路由器", command=self.start_router).grid(
            row=1, column=2, sticky="e", pady=(4, 0)
        )
        ttk.Button(status, text="一键修复配置", command=self.repair_config_projection).grid(
            row=2, column=2, sticky="e", pady=(4, 0)
        )

        self.profiles_frame = ttk.LabelFrame(outer, text="Provider / 账号（双击设置默认入口）", padding=8)
        self.profiles_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        self.profiles_frame.columnconfigure(0, weight=1)
        self.profiles_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            self.profiles_frame,
            columns=("label", "kind", "endpoint", "model", "key", "state"),
            show="headings",
            selectmode="browse",
            height=5,
        )
        headings = {
            "label": "账号 / Provider",
            "kind": "类型",
            "endpoint": "API 地址",
            "model": "模型",
            "key": "凭据",
            "state": "状态",
        }
        widths = {"label": 220, "kind": 80, "endpoint": 250, "model": 150, "key": 90, "state": 100}
        for column, heading in headings.items():
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=widths[column], anchor="w", stretch=column in {"label", "endpoint"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self.profiles_frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("active", background="#e7f4e8", foreground="#183b24")
        self.tree.tag_configure("pending", foreground="#8a5b12")
        self.tree.tag_configure("disabled", foreground="#727272")
        self.tree.bind("<<TreeviewSelect>>", self._on_profile_select)
        self.tree.bind("<Double-1>", lambda _event: self.switch_selected_profile())

        quick = ttk.Frame(self.profiles_frame)
        quick.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(quick, text="设为默认：所选入口", command=self.switch_selected_profile).pack(side="left")
        ttk.Button(quick, text="设为默认：Maylily", command=lambda: self.switch_profile("maylily")).pack(side="left", padx=(8, 0))
        ttk.Button(quick, text="设为默认：另一个中转站", command=lambda: self.switch_profile("relay-b")).pack(side="left", padx=(8, 0))
        ttk.Button(quick, text="设为默认：官方账号", command=lambda: self.switch_profile("official")).pack(side="left", padx=(8, 0))
        ttk.Button(quick, text="登录/切换官方账号", command=self.change_official_account).pack(side="left", padx=(8, 0))
        ttk.Label(quick, text="默认入口不改已有任务", style="Muted.TLabel").pack(side="right", padx=(8, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=3, column=0, sticky="nsew", pady=(0, 8))

        self.config_tab = ttk.Frame(self.notebook, padding=10)
        self.overview_tab = ttk.Frame(self.notebook, padding=10)
        self.thread_tab = ttk.Frame(self.notebook, padding=10)
        self.maintenance_tab = ttk.Frame(self.notebook, padding=10)
        self.log_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.config_tab, text="中转站配置")
        self.notebook.add(self.overview_tab, text="任务配置总览")
        self.notebook.add(self.thread_tab, text="当前任务 / 无损转换")
        self.notebook.add(self.maintenance_tab, text="安全维护")
        self.notebook.add(self.log_tab, text="操作记录")

        config = self.config_tab
        config.columnconfigure(1, weight=1)
        config.columnconfigure(3, weight=1)
        ttk.Label(
            config,
            text="Key 只通过 Windows DPAPI 加密保存；不会显示在列表、日志或命令行中。",
            style="Muted.TLabel",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(config, text="入口").grid(row=1, column=0, sticky="w", padx=(0, 6))
        self.config_combo = ttk.Combobox(config, textvariable=self.config_profile_var, state="readonly", width=22)
        self.config_combo.grid(row=1, column=1, sticky="ew", padx=(0, 14))
        self.config_combo.bind("<<ComboboxSelected>>", lambda _event: self._load_config_profile())
        ttk.Label(config, text="启用").grid(row=1, column=2, sticky="w", padx=(0, 6))
        ttk.Checkbutton(config, variable=self.enabled_var).grid(row=1, column=3, sticky="w")
        ttk.Label(config, text="基础地址").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(config, textvariable=self.base_url_var).grid(row=2, column=1, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(config, text="默认模型").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.default_model_combo = ttk.Combobox(config, textvariable=self.model_var)
        self.default_model_combo.grid(row=3, column=1, sticky="ew", pady=(8, 0), padx=(0, 14))
        ttk.Label(config, text="Key").grid(row=3, column=2, sticky="w", pady=(8, 0), padx=(0, 6))
        ttk.Entry(config, textvariable=self.key_var, show="•").grid(row=3, column=3, sticky="ew", pady=(8, 0))
        ttk.Label(config, text="可用模型").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(config, textvariable=self.models_var).grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=(8, 0), padx=(0, 14)
        )
        ttk.Button(config, text="导入本机安全模型", command=self.import_bundled_models).grid(
            row=4, column=3, sticky="e", pady=(8, 0)
        )
        ttk.Button(config, text="保存地址/模型", command=self.save_relay_metadata).grid(row=5, column=1, sticky="w", pady=(10, 0))
        ttk.Button(config, text="加密保存 Key", command=self.save_key).grid(row=5, column=3, sticky="e", pady=(10, 0))
        ttk.Label(config, textvariable=self.catalog_var, style="Muted.TLabel").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        ttk.Button(config, text="生成/刷新模型目录", command=self.refresh_model_catalog).grid(
            row=6, column=3, sticky="e", pady=(10, 0)
        )
        ttk.Label(config, textvariable=self.remote_models_var, style="Muted.TLabel").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        ttk.Button(config, text="手动检测中转站模型", command=self.detect_remote_models).grid(
            row=7, column=3, sticky="e", pady=(10, 0)
        )

        overview = self.overview_tab
        overview.columnconfigure(0, weight=1)
        overview.rowconfigure(1, weight=1)
        overview_header = ttk.Frame(overview)
        overview_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        overview_header.columnconfigure(0, weight=1)
        ttk.Label(
            overview_header,
            text="标记 fork 任务家族；默认隐藏内部子任务，不显示 Key。",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            overview_header,
            text="刷新",
            command=self._schedule_task_inventory_refresh,
        ).grid(
            row=0, column=1, sticky="e"
        )
        overview_filters = ttk.Frame(overview_header)
        overview_filters.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        ttk.Label(overview_filters, text="排序").pack(side="left")
        self.overview_sort_combo = ttk.Combobox(
            overview_filters,
            textvariable=self.overview_sort_var,
            values=tuple(THREAD_SORT_OPTIONS),
            state="readonly",
            width=16,
        )
        self.overview_sort_combo.pack(side="left", padx=(6, 12))
        self.overview_sort_combo.bind("<<ComboboxSelected>>", self._on_task_view_options_change)
        ttk.Checkbutton(
            overview_filters,
            text="显示内部子任务",
            variable=self.overview_show_subagents_var,
            command=self._refresh_task_views,
        ).pack(side="left")
        ttk.Checkbutton(
            overview_filters,
            text="包含归档",
            variable=self.overview_include_archived_var,
            command=self._refresh_thread_overview,
        ).pack(side="right", padx=(0, 8))
        overview_search = ttk.Entry(overview_filters, textvariable=self.overview_query_var, width=24)
        overview_search.pack(side="right", padx=(8, 8))
        overview_search.bind("<Return>", lambda _event: self._refresh_thread_overview())
        ttk.Label(overview_filters, text="搜索").pack(side="right")
        self.overview_tree = ttk.Treeview(
            overview,
            columns=("name", "family", "profile", "alias", "model", "state", "id"),
            show="headings",
            selectmode="browse",
            height=7,
        )
        overview_headings = {
            "name": "任务",
            "family": "家族",
            "profile": "配置",
            "alias": "Provider 别名",
            "model": "模型",
            "state": "状态",
            "id": "ID 后 8 位",
        }
        overview_widths = {
            "name": 260,
            "family": 90,
            "profile": 130,
            "alias": 160,
            "model": 150,
            "state": 100,
            "id": 90,
        }
        for column, heading in overview_headings.items():
            self.overview_tree.heading(column, text=heading)
            self.overview_tree.column(
                column,
                width=overview_widths[column],
                anchor="w",
                stretch=column in {"name", "alias", "model"},
            )
        self.overview_tree.grid(row=1, column=0, sticky="nsew")
        overview_scroll = ttk.Scrollbar(overview, orient="vertical", command=self.overview_tree.yview)
        overview_scroll.grid(row=1, column=1, sticky="ns")
        self.overview_tree.configure(yscrollcommand=overview_scroll.set)
        self.overview_tree.tag_configure("archived", foreground="#727272")
        self.overview_tree.tag_configure("pinned", background="#e7f4e8", foreground="#183b24")
        self.overview_tree.tag_configure("head", background="#e8f1fb", foreground="#17324d")
        self.overview_tree.tag_configure("subagent", foreground="#7a5b19")
        self.overview_tree.bind("<Double-1>", self._use_overview_thread)
        self.overview_tree.bind("<Return>", self._use_overview_thread)
        overview_actions = ttk.Frame(overview)
        overview_actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(
            overview_actions,
            text="归档所选家族旧成员",
            command=self.archive_selected_family,
        ).pack(side="left")
        ttk.Button(
            overview_actions,
            text="用于无损转换",
            command=self._use_overview_thread,
        ).pack(side="right")

        thread = self.thread_tab
        thread.columnconfigure(1, weight=1)
        thread.columnconfigure(3, weight=1)
        ttk.Label(
            thread,
            text="跨 Provider 使用官方 fork 保留完整历史；新任务保持同名、自动打开，旧链路归档但可恢复。"
            "重复切换且来源未变化时复用已有结果，不发消息，也不触发模型推理。",
            style="Muted.TLabel",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        ttk.Label(thread, text="最近任务").grid(row=1, column=0, sticky="w", padx=(0, 6))
        self.thread_choice_combo = ttk.Combobox(
            thread,
            textvariable=self.thread_choice_var,
            state="readonly",
        )
        self.thread_choice_combo.grid(row=1, column=1, columnspan=3, sticky="ew")
        self.thread_choice_combo.bind("<<ComboboxSelected>>", self._on_thread_choice)
        ttk.Label(thread, text="任务 ID").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(8, 0))
        self.thread_id_entry = ttk.Entry(thread, textvariable=self.thread_id_var)
        self.thread_id_entry.grid(row=2, column=1, sticky="ew", padx=(0, 14), pady=(8, 0))
        ttk.Label(thread, text="目标").grid(row=2, column=2, sticky="w", padx=(0, 6), pady=(8, 0))
        self.thread_target_combo = ttk.Combobox(thread, textvariable=self.thread_target_var, state="readonly", width=18)
        self.thread_target_combo.grid(row=2, column=3, sticky="ew", pady=(8, 0))
        self.thread_target_combo.bind("<<ComboboxSelected>>", self._on_thread_target_change)
        ttk.Label(thread, text="目标模型").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.thread_model_combo = ttk.Combobox(
            thread,
            textvariable=self.thread_model_var,
            state="readonly",
        )
        self.thread_model_combo.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(thread, textvariable=self.thread_binding_var, style="Status.TLabel").grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(10, 0)
        )
        ttk.Label(thread, text="期望工作目录（可留空）").grid(row=5, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(thread, textvariable=self.thread_cwd_var).grid(row=5, column=1, columnspan=3, sticky="ew", pady=(10, 0))
        thread_actions = ttk.Frame(thread)
        thread_actions.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        ttk.Button(
            thread_actions,
            text="查看当前绑定",
            command=self.inspect_thread_binding,
        ).pack(side="left")
        self.cancel_wait_button = ttk.Button(
            thread_actions,
            text="取消等待",
            command=self.cancel_wait,
            state="disabled",
        )
        self.cancel_wait_button.pack(side="right")
        ttk.Button(
            thread_actions,
            text="无损切换并整理旧任务",
            command=self.switch_thread,
        ).pack(side="right", padx=(0, 8))

        maintenance = self.maintenance_tab
        maintenance.columnconfigure(0, weight=1)
        ttk.Label(
            maintenance,
            text="备份策略是只读预览：不会自动永久删除。候选项仍需人工确认后再处理。",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))
        backup_frame = ttk.LabelFrame(maintenance, text="备份保留策略", padding=10)
        backup_frame.grid(row=1, column=0, sticky="ew")
        backup_frame.columnconfigure(0, weight=1)
        ttk.Label(
            backup_frame,
            textvariable=self.backup_policy_var,
            wraplength=850,
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Button(
            backup_frame,
            text="刷新备份统计",
            command=self.refresh_backup_policy,
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Button(
            backup_frame,
            text="打开 Switchboard 备份",
            command=lambda: self.open_backup_root(self.paths.backups),
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        ttk.Button(
            backup_frame,
            text="打开历史备份",
            command=lambda: self.open_backup_root(self.paths.codex_home / "backups"),
        ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(10, 0))
        ttk.Label(
            maintenance,
            text="任务整理请到“任务配置总览”选择任意家族成员，再点击“归档所选家族旧成员”。"
            "只归档旧成员并保留/置顶当前 head，不删除聊天。",
            style="Muted.TLabel",
            wraplength=900,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(14, 0))

        log_frame = self.log_tab
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=8, wrap="word", state="disabled", background="#f7f7f7")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)
        ttk.Label(outer, textvariable=self.status_var, style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(2, 0))

    def _append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{timestamp}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _ensure_state(self) -> None:
        switchboard.ensure_state(self.paths)

    def refresh(self) -> None:
        try:
            self._ensure_state()
            rows = profile_rows(self.paths)
            account_summary = official_login_summary(self.paths)
            for row in rows:
                if row.get("kind") == "official":
                    row["key_state"] = "已登录" if "已登录 ChatGPT" in account_summary else "未登录"
            self._profile_rows = {row["id"]: row for row in rows}
            active = switchboard.load_active(self.paths)
            active_profile = switchboard.profile_by_id(switchboard.load_profiles(self.paths), active.get("profile_id", "maylily"))
            self.active_var.set(
                f"新任务默认：{active_profile.get('label', active.get('profile_id'))} "
                f"[{active.get('provider_alias', 'legacy')}]"
            )
            self.account_var.set(account_summary)
            self.revision_var.set(f"revision：{active.get('revision', 0)}")
            self.home_var.set(f"CODEX_HOME：{self.paths.codex_home}")
            self._refresh_config_drift()
            self._refresh_tree(rows, active.get("profile_id"))
            relay_ids = [row["id"] for row in rows if row["kind"] == "relay"]
            all_ids = [row["id"] for row in rows]
            self.config_combo["values"] = relay_ids
            self.thread_target_combo["values"] = all_ids
            if self.config_profile_var.get() not in relay_ids and relay_ids:
                self.config_profile_var.set(relay_ids[0])
            if self.thread_target_var.get() not in all_ids and all_ids:
                self.thread_target_var.set(all_ids[0])
            self._refresh_thread_target_models()
            self._schedule_task_inventory_refresh()
            self._load_config_profile()
            healthy, detail = router_health(self.paths)
            self.router_var.set(f"本地路由器：{'运行中' if healthy else '未连接'}（{detail}）")
            self.status_var.set("就绪")
        except Exception as exc:
            self.status_var.set("状态读取失败")
            self._append_log(f"状态读取失败：{exc}")

    def _refresh_config_drift(self) -> dict[str, Any]:
        status = switchboard.config_projection_status(self.paths)
        self._config_projection = status
        self.config_drift_var.set(config_projection_text(status))
        return status

    def _poll_config_drift(self) -> None:
        if self.closed:
            return
        try:
            status = self._refresh_config_drift()
            signature = (
                status.get("state"),
                status.get("profile_id"),
                tuple(status.get("reasons") or []),
            )
            previous = self._last_config_projection_signature
            if previous is not None and signature != previous and not status.get("ready"):
                self._append_log("检测到配置投影漂移；可点击“一键修复配置”恢复当前默认入口")
            self._last_config_projection_signature = signature
        except Exception as exc:
            self.config_drift_var.set("配置投影：状态暂不可读")
            signature = ("error", type(exc).__name__)
            self._last_config_projection_signature = signature
        self.root.after(3000, self._poll_config_drift)

    def _refresh_thread_overview(self) -> None:
        if self._task_inventory is None:
            self._schedule_task_inventory_refresh()
            return
        previous = self.overview_tree.selection()
        previous_id = previous[0] if previous else self.thread_id_var.get().strip()
        for item in self.overview_tree.get_children():
            self.overview_tree.delete(item)
        self._overview_rows = {}
        for row in task_overview_rows(
            self.paths,
            include_archived=bool(self.overview_include_archived_var.get()),
            include_subagents=bool(self.overview_show_subagents_var.get()),
            sort_mode=self._thread_sort_mode(),
            query=self.overview_query_var.get(),
            bindings=self._task_inventory,
        ):
            thread_id = row["thread_id"]
            if not thread_id:
                continue
            self._overview_rows[thread_id] = row
            tags: tuple[str, ...] = ()
            if row["archived"]:
                tags += ("archived",)
            if row["is_pinned"]:
                tags += ("pinned",)
            if row["family_role"] == "head":
                tags += ("head",)
            if row["is_subagent"]:
                tags += ("subagent",)
            name = row["display_name"]
            if len(name) > 70:
                name = name[:67].rstrip() + "…"
            self.overview_tree.insert(
                "",
                "end",
                iid=thread_id,
                values=(
                    name,
                    f"{row['family_suffix']} · {row['family_size']}",
                    row["profile_label"],
                    row["provider_alias"] or "—",
                    row["model"],
                    row["state"],
                    row["id_suffix"],
                ),
                tags=tags,
            )
        if previous_id and self.overview_tree.exists(previous_id):
            self.overview_tree.selection_set(previous_id)
            self.overview_tree.focus(previous_id)

    def _thread_sort_mode(self) -> str:
        return THREAD_SORT_OPTIONS.get(
            self.overview_sort_var.get(),
            switchboard.THREAD_SORT_CODEX,
        )

    def _on_task_view_options_change(self, _event: Any = None) -> None:
        self._refresh_task_views()

    def _refresh_task_views(self) -> None:
        if self._task_inventory is None:
            self._schedule_task_inventory_refresh()
            return
        self._refresh_recent_threads()
        self._refresh_thread_overview()

    def _schedule_task_inventory_refresh(self) -> None:
        """Replace the UI-owned task snapshot without blocking Tk's event loop."""

        if self._task_inventory_loading:
            self._task_inventory_refresh_pending = True
            return
        self._task_inventory_loading = True
        self._task_inventory_refresh_pending = False
        if self._task_inventory is None:
            self.status_var.set("正在读取完整任务列表…")

        def worker() -> None:
            try:
                bindings = switchboard.recent_thread_bindings(
                    self.paths,
                    limit=switchboard.MAX_THREAD_INVENTORY_ROWS,
                    include_archived=True,
                )
            except Exception as exc:  # noqa: BLE001 - surface bounded UI status
                self.jobs.put(("task-inventory", "任务列表", {"error": exc}))
            else:
                self.jobs.put(("task-inventory", "任务列表", {"bindings": bindings}))

        threading.Thread(
            target=worker,
            name="switchboard-task-inventory",
            daemon=True,
        ).start()

    def _use_overview_thread(self, _event: Any = None) -> None:
        selected = self.overview_tree.selection()
        if not selected:
            return
        row = self._overview_rows.get(selected[0])
        if not row:
            return
        if row.get("is_subagent"):
            messagebox.showinfo(
                "内部子任务不能转换",
                "这是主任务临时派出的内部子智能体记录，不是独立用户任务。请改选它所属家族的当前 head。",
                parent=self.root,
            )
            return
        self._apply_thread_choice(row)
        for label, choice in self._thread_choices.items():
            if choice.get("thread_id") == row["thread_id"]:
                self.thread_choice_var.set(label)
                break
        self.notebook.select(self.thread_tab)

    def archive_selected_family(self) -> None:
        selected = self.overview_tree.selection()
        if not selected:
            messagebox.showinfo("请选择任务家族", "请先选择任意一个任务家族成员。", parent=self.root)
            return
        thread_id = selected[0]
        selected_row = self._overview_rows.get(thread_id)
        if (
            not selected_row
            or selected_row.get("family_role") != "head"
            or selected_row.get("archived")
        ):
            messagebox.showinfo(
                "请选择当前 head",
                "为避免误归档并行分支，请选择状态中标有“当前 head”的活跃任务后再整理。",
                parent=self.root,
            )
            return
        try:
            plan = switchboard.thread_family_cleanup_plan(self.paths, thread_id)
        except Exception as exc:
            messagebox.showerror("无法生成整理计划", friendly_error_message(exc), parent=self.root)
            return
        candidate_count = int(plan.get("candidate_count") or 0)
        if candidate_count == 0:
            messagebox.showinfo(
                "无需整理",
                "这个家族已经只保留一个活跃 head；没有任务会被改变。",
                parent=self.root,
            )
            return
        blockers = switchboard.appserver_blocking_processes(self.paths)
        needs_wait = bool(blockers)
        prompt = (
            f"将归档这个家族的 {candidate_count} 个活跃旧成员，并保留/置顶当前 head：\n"
            f"{plan.get('head_thread_id')}\n\n"
            "归档可随时恢复，不会永久删除任务、聊天或附件。"
        )
        if needs_wait:
            prompt += "\n\n点击“确定”后请关闭 Codex 主窗口；保持此切换器窗口打开。"
        if not messagebox.askokcancel("确认整理任务家族", prompt, parent=self.root):
            return
        self.waiting_for_exit = needs_wait
        job_label = "等待 Codex 退出后整理任务家族" if needs_wait else "整理任务家族"

        def operation() -> dict[str, Any]:
            probe = switchboard.appserver_blocking_processes
            if needs_wait:
                def progress(info: dict[str, Any]) -> None:
                    blockers_count = int(info.get("blockers", 0))
                    stable = int(info.get("stable_empty_checks", 0))
                    required = int(info.get("required_empty_checks", 2))
                    message = (
                        f"等待 Codex 退出：仍有 {blockers_count} 个相关进程；请关闭 Codex 桌面端"
                        if blockers_count
                        else f"已检测到 Codex 退出，正在做稳定性确认 {stable}/{required}…"
                    )
                    self.jobs.put(
                        ("progress", job_label, {"phase": "waiting", "message": message})
                    )

                switchboard.wait_for_appserver_exit(
                    self.paths,
                    process_probe=probe,
                    cancel_probe=self.cancel_wait_event.is_set,
                    progress_fn=progress,
                    poll_seconds=1.0,
                    timeout_seconds=30 * 60,
                )
                if self.cancel_wait_event.is_set():
                    raise RuntimeError("thread copy wait was cancelled before publication")
            result = switchboard.archive_thread_family(
                self.paths,
                thread_id,
                blocker_probe=probe,
            )
            head_id = str(result.get("head_thread_id") or "")
            if head_id and result.get("head_active"):
                try:
                    open_codex_thread(head_id)
                except Exception as exc:
                    result["navigation_warning"] = str(exc)[:240]
                else:
                    result["navigation_launched"] = True
            return result

        self._run_job(job_label, operation)

    def _refresh_recent_threads(self) -> None:
        if self._task_inventory is None:
            self._schedule_task_inventory_refresh()
            return
        rows = switchboard.task_list_bindings(
            self.paths,
            limit=40,
            include_subagents=bool(self.overview_show_subagents_var.get()),
            sort_mode=self._thread_sort_mode(),
            bindings=self._task_inventory,
        )
        previous_id = self.thread_id_var.get().strip()
        choices: dict[str, dict[str, Any]] = {}
        selected_label: str | None = None
        for row in rows:
            name = str(row.get("display_name") or row.get("thread_id") or "未命名任务")
            if len(name) > 54:
                name = name[:51].rstrip() + "…"
            provider = str(row.get("profile_label") or row.get("provider_alias") or "未知")
            thread_id = str(row.get("thread_id") or "")
            label = f"{name}  |  {provider}  |  {thread_id[-8:]}"
            # Duplicate titles remain distinguishable by their ID suffix.
            choices[label] = row
            if thread_id == previous_id:
                selected_label = label
        self._thread_choices = choices
        self.thread_choice_combo["values"] = list(choices)
        if selected_label is None and choices:
            selected_label = next(iter(choices))
        if selected_label is not None:
            self.thread_choice_var.set(selected_label)
            self._apply_thread_choice(choices[selected_label])

    def _on_thread_choice(self, _event: Any = None) -> None:
        row = self._thread_choices.get(self.thread_choice_var.get())
        if row:
            self._apply_thread_choice(row)

    def _apply_thread_choice(self, row: dict[str, Any]) -> None:
        thread_id = str(row.get("thread_id") or "")
        self.thread_id_var.set(thread_id)
        self.thread_cwd_var.set(str(row.get("cwd") or ""))
        self.thread_binding_var.set(
            f"所选任务：{row.get('display_name') or thread_id} ｜ "
            f"{row.get('profile_label') or '未知'} [{row.get('provider_alias') or '-'}] ｜ "
            f"模型 {row.get('model') or '由任务/账号决定'}"
        )

    def _refresh_tree(self, rows: list[dict[str, Any]], active_id: str | None) -> None:
        previous_selection = self.tree.selection()
        selected_id = previous_selection[0] if previous_selection else None
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in rows:
            state = (
                "默认"
                if row["active"] and row.get("active_version_current")
                else "默认（旧版本）"
                if row["active"]
                else "可用"
                if row["ready"]
                else "待配置"
            )
            if not row["enabled"]:
                state = "已停用"
            tags: tuple[str, ...]
            if row["active"]:
                tags = ("active",)
            elif not row["enabled"]:
                tags = ("disabled",)
            elif not row["ready"]:
                tags = ("pending",)
            else:
                tags = ()
            self.tree.insert(
                "",
                "end",
                iid=row["id"],
                values=(
                    row["label"],
                    "官方" if row["kind"] == "official" else "中转站",
                    row["base_url"] or "—",
                    (
                        f"{row['model']}（{row.get('model_count', 1)} 个）"
                        if row.get("model") and row.get("kind") == "relay"
                        else row["model"] or "—"
                    ),
                    row["key_state"],
                    state,
                ),
                tags=tags,
            )
        selection = selected_id if selected_id and self.tree.exists(selected_id) else active_id
        if selection and self.tree.exists(selection):
            self.tree.selection_set(selection)
            self.tree.focus(selection)

    def _on_profile_select(self, _event: Any = None) -> None:
        selected = self.tree.selection()
        if selected and selected[0] in self._profile_rows:
            row = self._profile_rows[selected[0]]
            if row["kind"] == "relay":
                self.config_profile_var.set(row["id"])
                self._load_config_profile()

    def _on_thread_target_change(self, _event: Any = None) -> None:
        self._refresh_thread_target_models()

    def _refresh_thread_target_models(self) -> None:
        row = self._profile_rows.get(self.thread_target_var.get())
        if not row or row.get("kind") == "official":
            self.thread_model_combo["values"] = ("由官方账号决定",)
            self.thread_model_var.set("由官方账号决定")
            return
        models = tuple(str(model) for model in row.get("models", []) if str(model))
        self.thread_model_combo["values"] = models
        if self.thread_model_var.get() not in models:
            self.thread_model_var.set(str(row.get("model") or (models[0] if models else "")))

    def _load_config_profile(self) -> None:
        row = self._profile_rows.get(self.config_profile_var.get())
        if not row:
            return
        self.base_url_var.set(row["base_url"])
        self.model_var.set(row["model"])
        models = list(row.get("models") or ([row["model"]] if row.get("model") else []))
        self.models_var.set(", ".join(models))
        self.default_model_combo["values"] = models
        self.enabled_var.set(bool(row["enabled"]))
        self.catalog_var.set(f"模型目录：{row.get('catalog_state', '未生成')}")
        self.remote_models_var.set(model_probe_text(self._model_probe_results.get(row["id"])))
        self.key_var.set("")

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self.busy = busy
        self.status_var.set(label if busy else "就绪")
        state = "disabled" if busy else "normal"
        for child in self.root.winfo_children():
            self._set_widget_state(child, state)
        self.cancel_wait_button.configure(
            state="normal" if busy and self.waiting_for_exit else "disabled"
        )

    def _set_widget_state(self, widget: tk.Misc, state: str) -> None:
        try:
            if isinstance(widget, (ttk.Button, ttk.Checkbutton, ttk.Combobox, ttk.Entry)):
                if isinstance(widget, ttk.Combobox) and state == "normal":
                    # Keep profile selectors read-only after the job ends.
                    widget.configure(
                        state="readonly"
                        if widget in {
                            self.config_combo,
                            self.thread_target_combo,
                            self.thread_choice_combo,
                            self.thread_model_combo,
                            self.overview_sort_combo,
                        }
                        else state
                    )
                else:
                    widget.configure(state=state)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._set_widget_state(child, state)

    def _run_job(self, label: str, function: Callable[[], Any]) -> None:
        if self.busy:
            return
        self.cancel_wait_event.clear()
        self._set_busy(True, f"正在执行：{label}")
        self._append_log(f"开始：{label}")

        def worker() -> None:
            try:
                result = function()
            except Exception as exc:  # noqa: BLE001 - surface bounded message in UI
                self.jobs.put(("error", label, exc))
            else:
                self.jobs.put(("success", label, result))

        threading.Thread(target=worker, name="switchboard-ui-job", daemon=True).start()

    def _drain_jobs(self) -> None:
        if self.closed:
            return
        try:
            while True:
                kind, label, payload = self.jobs.get_nowait()
                if kind == "progress":
                    progress = payload if isinstance(payload, dict) else {"message": str(payload)}
                    phase = str(progress.get("phase") or "waiting")
                    self.waiting_for_exit = phase == "waiting"
                    self.cancel_wait_button.configure(
                        state="normal" if self.waiting_for_exit else "disabled"
                    )
                    progress_message = str(progress.get("message") or label)
                    if phase == "binding":
                        self.thread_binding_var.set(progress_message)
                    self.status_var.set(progress_message)
                    self._append_log(progress_message)
                    continue
                if kind == "task-inventory":
                    self._task_inventory_loading = False
                    result = payload if isinstance(payload, dict) else {}
                    error = result.get("error")
                    bindings = result.get("bindings")
                    if error is not None:
                        self.status_var.set("任务列表读取失败")
                        self._append_log(
                            f"任务列表读取失败：{friendly_error_message(error)}"
                        )
                    elif isinstance(bindings, list):
                        self._task_inventory = bindings
                        self._refresh_task_views()
                        visible_count = sum(
                            not bool(row.get("is_subagent")) for row in bindings
                        )
                        self.status_var.set(f"就绪（{visible_count} 个用户任务）")
                    if self._task_inventory_refresh_pending:
                        self._task_inventory_refresh_pending = False
                        self.root.after(0, self._schedule_task_inventory_refresh)
                    continue
                if kind == "router-recovery":
                    self._router_recovery_running = False
                    result = payload if isinstance(payload, dict) else {}
                    if result.get("ok"):
                        self._router_recovery_failures = 0
                        self._router_next_recovery_at = 0.0
                        detail = str(result.get("message") or "127.0.0.1:8765 正常")
                        self.router_var.set(f"本地路由器：运行中（{detail}）")
                        self._append_log("本地路由器已自动恢复")
                    else:
                        self._router_recovery_failures += 1
                        delay = min(60, 2 ** min(self._router_recovery_failures, 6))
                        self._router_next_recovery_at = time.monotonic() + delay
                        self.router_var.set(
                            f"本地路由器：自动恢复失败（{delay} 秒后重试）"
                        )
                        error = result.get("error")
                        self._append_log(
                            "本地路由器自动恢复失败："
                            + friendly_error_message(error or RuntimeError("unknown router error"))
                        )
                    continue
                self.waiting_for_exit = False
                self._set_busy(False)
                if kind == "error":
                    friendly = friendly_error_message(payload)
                    self._append_log(f"失败：{label}：{friendly}")
                    messagebox.showerror("Switchboard 操作失败", friendly, parent=self.root)
                else:
                    operation_name = payload.get("operation") if isinstance(payload, dict) else None
                    if operation_name == "model_probe":
                        profile_id = str(payload.get("profile_id") or "")
                        if profile_id:
                            self._model_probe_results[profile_id] = payload
                    elif operation_name == "backup_retention":
                        self._backup_retention_result = payload
                        self.backup_policy_var.set(backup_retention_text(payload))
                    message = summarize_result(label, payload)
                    self._append_log(message)
                    self.refresh()
                    if isinstance(payload, dict) and isinstance(payload.get("thread"), dict):
                        task_id = str(payload["thread"].get("id") or "")
                        if task_id:
                            try:
                                self.root.clipboard_clear()
                                self.root.clipboard_append(task_id)
                            except tk.TclError:
                                pass
                        completion = payload.get("completion")
                        complete = bool(
                            isinstance(completion, dict) and completion.get("complete")
                        )
                        dialog = messagebox.showinfo if complete else messagebox.showwarning
                        dialog(
                            "任务切换完成" if complete else "任务切换部分完成",
                            f"{message}\n\n任务 ID 已复制。",
                            parent=self.root,
                        )
                    elif operation_name == "model_probe":
                        dialog = messagebox.showwarning if payload.get("missing_remote") else messagebox.showinfo
                        dialog("中转站模型检测", message, parent=self.root)
                    elif operation_name == "family_cleanup":
                        dialog = messagebox.showinfo if payload.get("complete") else messagebox.showwarning
                        dialog(
                            "任务家族整理完成" if payload.get("complete") else "任务家族整理部分完成",
                            message,
                            parent=self.root,
                        )
        except queue.Empty:
            pass
        self.root.after(100, self._drain_jobs)

    def _poll_router_health(self) -> None:
        """Keep published local relay routes available without Provider traffic."""

        if self.closed:
            return
        try:
            required = switchboard.router_required(self.paths)
            if not required:
                self._router_recovery_failures = 0
                self._router_next_recovery_at = 0.0
                self.router_var.set("本地路由器：无需启动（没有可用中转路由）")
            else:
                healthy, detail = router_health(self.paths, timeout=0.35)
                if healthy:
                    self._router_recovery_failures = 0
                    self._router_next_recovery_at = 0.0
                    self.router_var.set(f"本地路由器：运行中（{detail}）")
                elif self._router_recovery_running:
                    self.router_var.set("本地路由器：未连接，正在自动恢复…")
                elif time.monotonic() >= self._router_next_recovery_at:
                    self._schedule_router_recovery()
                else:
                    remaining = max(
                        1,
                        int(self._router_next_recovery_at - time.monotonic() + 0.999),
                    )
                    self.router_var.set(
                        f"本地路由器：未连接（{remaining} 秒后自动重试）"
                    )
        except Exception as exc:  # noqa: BLE001 - bounded status only
            self.router_var.set("本地路由器：状态暂不可读")
            self._append_log(f"本地路由器状态读取失败：{friendly_error_message(exc)}")
        self.root.after(5000, self._poll_router_health)

    def _schedule_router_recovery(self) -> None:
        if self.closed or self._router_recovery_running:
            return
        if not switchboard.router_required(self.paths):
            return
        self._router_recovery_running = True
        self.router_var.set("本地路由器：未连接，正在自动恢复…")

        def worker() -> None:
            try:
                self.ensure_router()
                healthy, detail = router_health(self.paths)
                if not healthy:
                    raise RuntimeError("router health check failed after local start")
            except Exception as exc:  # noqa: BLE001 - handled by UI backoff
                self.jobs.put(("router-recovery", "本地路由器", {"ok": False, "error": exc}))
            else:
                self.jobs.put(
                    ("router-recovery", "本地路由器", {"ok": True, "message": detail})
                )

        threading.Thread(
            target=worker,
            name="switchboard-router-recovery",
            daemon=True,
        ).start()

    def _router_command(self) -> list[str]:
        # The router is the core CLI process, not another UI instance.  Keep
        # this explicit so a pythonw-launched window does not recursively try
        # to parse the ``router`` subcommand as a GUI argument.
        switchboard_script = Path(__file__).with_name("switchboard.py").resolve()
        return [sys.executable, "-u", str(switchboard_script), "--home", str(self.paths.codex_home), "router"]

    def ensure_router(self) -> str:
        with self._router_start_lock:
            healthy, detail = router_health(self.paths)
            if healthy:
                return f"路由器已运行（{detail}）"
            log_path = self.paths.switchboard / "router-ui.stdout.log"
            err_path = self.paths.switchboard / "router-ui.stderr.log"
            log_handle = log_path.open("a", encoding="utf-8")
            err_handle = err_path.open("a", encoding="utf-8")
            try:
                self.router_process = subprocess.Popen(
                    self._router_command(),
                    cwd=os.fspath(self.paths.codex_home),
                    env=child_environment(self.paths),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=err_handle,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    text=True,
                )
            finally:
                log_handle.close()
                err_handle.close()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                healthy, detail = router_health(self.paths)
                if healthy:
                    return f"路由器已启动（{detail}）"
                time.sleep(0.2)
            raise RuntimeError(
                "路由器启动失败，请查看 E:\\Codex-Home\\switchboard\\router-ui.stderr.log"
            )

    def start_router(self) -> None:
        self._run_job("启动本地路由器", self.ensure_router)

    def repair_config_projection(self) -> None:
        try:
            status = self._refresh_config_drift()
        except Exception as exc:
            messagebox.showerror(
                "配置状态不可读",
                friendly_error_message(exc),
                parent=self.root,
            )
            return
        if status.get("ready"):
            messagebox.showinfo("无需修复", "当前配置已经与默认入口一致。", parent=self.root)
            return
        if not status.get("repairable"):
            messagebox.showwarning(
                "暂不能自动修复",
                config_projection_text(status),
                parent=self.root,
            )
            return
        if not messagebox.askokcancel(
            "一键修复配置",
            config_projection_text(status)
            + "\n\n将按 active.json 和不可变 Provider 版本重新发布 config.toml。"
            "不会修改任何任务、聊天正文或 active revision。",
            parent=self.root,
        ):
            return
        self._run_job("修复配置投影", lambda: switchboard.repair_config(self.paths))

    def refresh_backup_policy(self) -> None:
        self._run_job(
            "刷新备份保留策略",
            lambda: switchboard.backup_retention_plan(self.paths),
        )

    def open_backup_root(self, root: Path) -> None:
        if not root.is_dir():
            messagebox.showinfo("备份目录不存在", f"当前没有目录：{root}", parent=self.root)
            return
        if os.name != "nt":
            messagebox.showinfo("仅支持 Windows", str(root), parent=self.root)
            return
        os.startfile(str(root))  # type: ignore[attr-defined]

    def switch_selected_profile(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("请选择入口", "请先在账号列表中选择一个入口。", parent=self.root)
            return
        self.switch_profile(selected[0])

    def switch_profile(self, profile_id: str) -> None:
        row = self._profile_rows.get(profile_id)
        if not row:
            messagebox.showwarning("入口不存在", f"找不到入口：{profile_id}", parent=self.root)
            return
        active = switchboard.load_active(self.paths)
        current = active.get("profile_id")
        same_version = False
        if current == profile_id:
            try:
                version = switchboard.profile_for_provider_alias(
                    self.paths,
                    str(active.get("provider_alias") or ""),
                )
                same_version = (
                    str(version.get("profile_id") or "") == profile_id
                    and all(
                        str(version.get(field) or "") == str(row.get(field) or "")
                        for field in ("base_url", "model", "key_ref")
                    )
                )
            except (RuntimeError, ValueError):
                same_version = False
        if same_version:
            def repair() -> dict[str, Any]:
                self.ensure_router()
                return switchboard.repair_config(self.paths)

            self._run_job(f"刷新默认入口：{row['label']}", repair)
            return
        if not row["ready"]:
            self.config_profile_var.set(profile_id)
            self._load_config_profile()
            self.notebook.select(self.config_tab)
            messagebox.showinfo(
                "中转站尚未配置完成",
                "请先填写基础地址和模型、勾选启用，并加密保存 Key。",
                parent=self.root,
            )
            return

        def operation() -> dict[str, Any]:
            if row["kind"] == "relay":
                self.ensure_router()
            return switchboard.set_active_profile(
                self.paths,
                profile_id,
                allow_official=row["kind"] == "official",
            )

        self._run_job(f"设为默认入口：{row['label']}", operation)

    def switch_official(self) -> None:
        """Compatibility action: convert the selected task, not global login."""

        self.thread_target_var.set("official")
        self._refresh_thread_target_models()
        self.notebook.select(self.thread_tab)
        self._run_thread_copy("official")

    def change_official_account(self) -> None:
        blockers = switchboard.appserver_blocking_processes(self.paths)
        needs_wait = bool(blockers)
        if not messagebox.askokcancel(
            "登录/切换官方账号",
            "这会显式退出当前官方登录并打开 OpenAI 官方登录页。\n\n"
            + (
                "点击“确定”后请关闭 Codex 主窗口；保持切换器窗口打开。\n"
                if needs_wait
                else "Codex 已关闭，可以直接继续。\n"
            )
            + "中转站 Key、任务历史和任务 Provider 绑定都不会改变。",
            parent=self.root,
        ):
            return

        self.waiting_for_exit = needs_wait
        label = "登录/切换官方账号"

        def operation() -> dict[str, Any]:
            probe = switchboard.appserver_blocking_processes
            if needs_wait:
                switchboard.wait_for_appserver_exit(
                    self.paths,
                    process_probe=probe,
                    cancel_probe=self.cancel_wait_event.is_set,
                    poll_seconds=1.0,
                    timeout_seconds=30 * 60,
                )
            account = switchboard.login_official_account(
                self.paths,
                switch_account=True,
                blocker_probe=probe,
            )
            active = switchboard.set_active_profile(self.paths, "official", allow_official=True)
            if os.name == "nt":
                os.startfile("codex://")  # type: ignore[attr-defined]
            return {"account": account, **active, "codex_reopened": True}

        self._run_job(label, operation)

    def save_relay_metadata(self) -> None:
        profile_id = self.config_profile_var.get()
        base_url = self.base_url_var.get().strip()
        model = self.model_var.get().strip()
        enabled = bool(self.enabled_var.get())
        models = self.models_var.get()

        def operation() -> dict[str, Any]:
            return switchboard.configure_relay_profile(
                self.paths,
                profile_id,
                base_url=base_url,
                model=model,
                models=models,
                enabled=enabled,
            )

        self._run_job(f"保存 {profile_id} 地址/模型", operation)

    def import_bundled_models(self) -> None:
        """Populate the editor from the local bundled catalog only."""

        try:
            result = switchboard.bundled_model_choices(self.paths)
            models = list(result.get("models") or [])
            if not models:
                raise RuntimeError("本机 Codex 没有可生成独立目录的模型")
            self.models_var.set(", ".join(models))
            self.default_model_combo["values"] = models
            if self.model_var.get().strip() not in models:
                self.model_var.set(models[0])
            self.catalog_var.set(
                f"已从 {result.get('client_version') or '本机 Codex'} 读取 {len(models)} 个安全模型；保存后再生成目录"
            )
        except Exception as exc:
            messagebox.showerror(
                "读取模型失败",
                friendly_error_message(exc),
                parent=self.root,
            )

    def detect_remote_models(self) -> None:
        profile_id = self.config_profile_var.get()
        row = self._profile_rows.get(profile_id)
        if not row or row.get("kind") != "relay":
            messagebox.showwarning("不能检测", "请选择一个中转站入口。", parent=self.root)
            return
        if (
            not row.get("enabled")
            or not str(row.get("base_url") or "").strip()
            or row.get("key_state") != "已配置"
        ):
            messagebox.showinfo(
                "请先保存配置",
                "请先保存中转站地址并加密保存 Key；检测只读取已经持久化的配置。",
                parent=self.root,
            )
            return
        if not messagebox.askokcancel(
            "手动检测中转站模型",
            f"将向 {row.get('label') or profile_id} 发送一次 GET /v1/models。\n\n"
            "不会发送聊天内容或推理请求，不会自动重试，也不会修改可用模型列表。",
            parent=self.root,
        ):
            return
        self._run_job(
            f"检测 {profile_id} 远端模型",
            lambda: switchboard.probe_relay_models(self.paths, profile_id),
        )

    def save_key(self) -> None:
        profile_id = self.config_profile_var.get()
        row = self._profile_rows.get(profile_id)
        secret = self.key_var.get()
        if not row or row["kind"] != "relay":
            messagebox.showwarning("不能保存", "只有中转站入口可以保存 Key。", parent=self.root)
            return
        if not secret.strip():
            messagebox.showwarning("Key 为空", "请输入 Key；Key 不会显示在操作记录中。", parent=self.root)
            return
        key_ref = row["key_ref"]

        def operation() -> str:
            switchboard.store_key(self.paths, key_ref, secret)
            return "Key 已通过 Windows DPAPI 加密保存"

        self._run_job(f"保存 {profile_id} Key", operation)
        self.key_var.set("")

    def refresh_model_catalog(self) -> None:
        profile_id = self.config_profile_var.get()
        row = self._profile_rows.get(profile_id)
        if not row or row["kind"] != "relay":
            messagebox.showwarning("不能生成", "请选择一个中转站入口。", parent=self.root)
            return

        def operation() -> dict[str, Any]:
            return switchboard.prepare_model_catalog(
                self.paths,
                profile_id,
                model=self.model_var.get().strip(),
                models=self.models_var.get(),
            )

        self._run_job(f"生成 {profile_id} 模型目录", operation)

    def inspect_thread_binding(self) -> None:
        thread_id = self.thread_id_var.get().strip()
        if not thread_id:
            messagebox.showwarning("缺少任务 ID", "请填写已有任务 ID。", parent=self.root)
            return

        def operation() -> dict[str, Any]:
            binding = switchboard.thread_provider_binding(self.paths, thread_id)
            self.jobs.put(
                (
                    "progress",
                    "查看任务绑定",
                    {
                        "phase": "binding",
                        "message": (
                            f"当前绑定：{binding.get('profile_label') or '未知'} "
                            f"[{binding.get('provider_alias') or '-'}]"
                        ),
                    },
                )
            )
            return {"binding": binding}

        self._run_job("查看任务绑定", operation)

    def _run_thread_copy(self, target: str | None = None) -> None:
        thread_id = self.thread_id_var.get().strip()
        target_id = target or self.thread_target_var.get().strip()
        cwd = self.thread_cwd_var.get().strip() or None
        if not thread_id:
            messagebox.showwarning("缺少任务 ID", "请填写已有任务 ID。", parent=self.root)
            return
        if not target_id:
            messagebox.showwarning("缺少目标", "请选择目标 Provider。", parent=self.root)
            return
        selected_task = next(
            (
                choice
                for choice in self._thread_choices.values()
                if str(choice.get("thread_id") or "") == thread_id
            ),
            None,
        )
        if isinstance(selected_task, dict) and selected_task.get("is_subagent"):
            messagebox.showinfo(
                "内部子任务不能转换",
                "这是主任务临时派出的内部子智能体记录，请改选它所属家族的当前 head。",
                parent=self.root,
            )
            return

        row = self._profile_rows.get(target_id)
        if not row:
            messagebox.showwarning("入口不存在", f"找不到入口：{target_id}", parent=self.root)
            return
        selected_model = None
        if row["kind"] == "relay":
            selected_model = self.thread_model_var.get().strip()
            if selected_model not in row.get("models", []):
                messagebox.showwarning(
                    "模型未配置",
                    "请选择这个中转站已保存的目标模型。",
                    parent=self.root,
                )
                return
        if row["kind"] == "relay" and not row["ready"]:
            self.config_profile_var.set(target_id)
            self._load_config_profile()
            self.notebook.select(self.config_tab)
            messagebox.showinfo(
                "中转站尚未配置完成",
                "请先填写基础地址和模型、勾选启用，并加密保存 Key。",
                parent=self.root,
            )
            return

        try:
            current_binding = switchboard.thread_provider_binding(self.paths, thread_id)
        except (OSError, RuntimeError, ValueError):
            current_binding = {}
        if (
            current_binding
            and switchboard.thread_binding_matches_profile(
                self.paths,
                current_binding,
                target_id,
                model=selected_model,
            )
            and not int(current_binding.get("archived") or 0)
        ):
            self.thread_binding_var.set(
                f"所选任务已经使用 {row['label']} [{current_binding.get('provider_alias') or '-'}]"
            )
            messagebox.showinfo("无需转换", "这个任务已经使用所选 Provider，不会创建新任务。", parent=self.root)
            return

        # The desktop/App Server owns the live thread database.  Detect it
        # before starting the worker so the user gets one clear instruction,
        # then let the worker wait while this small switchboard window stays
        # open.  The router/UI/project processes are deliberately not part of
        # this probe; they do not own the desktop thread state.
        blockers = switchboard.appserver_blocking_processes(self.paths)
        needs_wait = bool(blockers)
        if needs_wait:
            confirmed = messagebox.askokcancel(
                "请关闭 Codex 后继续",
                "检测到 Codex 桌面端或 App Server 正在运行。\n\n"
                "点击“确定”后，请关闭 Codex 主窗口；保持此切换器窗口不关闭。\n"
                "切换器会等待退出，然后无损转换完整历史。\n"
                "新任务保持同名并自动打开；旧任务归档但可恢复，不会发送消息。",
                parent=self.root,
            )
            if not confirmed:
                return
        elif not messagebox.askokcancel(
            "确认无损切换任务",
            f"将任务 {thread_id} 的完整历史切换到 {row['label']}。\n\n"
            + (f"目标模型：{selected_model}\n" if selected_model else "")
            + "必要时生成新任务 ID；来源未变化则复用已有结果。旧任务仅归档，可随时恢复。",
            parent=self.root,
        ):
            return

        self.waiting_for_exit = needs_wait
        job_label = (
            f"等待 Codex 退出后切换任务到 {target_id}"
            if needs_wait
            else f"切换任务到 {target_id}"
        )

        def operation() -> dict[str, Any]:
            probe = switchboard.appserver_blocking_processes
            if needs_wait:
                def progress(info: dict[str, Any]) -> None:
                    blockers_count = int(info.get("blockers", 0))
                    stable = int(info.get("stable_empty_checks", 0))
                    required = int(info.get("required_empty_checks", 2))
                    if blockers_count:
                        message = (
                            f"等待 Codex 退出：仍有 {blockers_count} 个相关进程；"
                            "请关闭 Codex 桌面端（保留此窗口）"
                        )
                    else:
                        message = f"已检测到 Codex 退出，正在做稳定性确认 {stable}/{required}…"
                    self.jobs.put(
                        (
                            "progress",
                            job_label,
                            {"phase": "waiting", "message": message},
                        )
                    )

                switchboard.wait_for_appserver_exit(
                    self.paths,
                    process_probe=probe,
                    cancel_probe=self.cancel_wait_event.is_set,
                    progress_fn=progress,
                    poll_seconds=1.0,
                    timeout_seconds=30 * 60,
                )
                if self.cancel_wait_event.is_set():
                    raise RuntimeError("thread copy wait was cancelled before publication")
                self.jobs.put(
                    (
                        "progress",
                        job_label,
                        {
                            "phase": "switching",
                            "message": "Codex 已退出，正在验证原任务并创建 Provider 副本…",
                        },
                    )
                )
            if row["kind"] == "relay":
                self.ensure_router()
            result = switchboard.fork_thread_provider(
                self.paths,
                thread_id,
                target_id,
                expected_cwd=cwd,
                blocker_probe=probe,
                target_model=selected_model,
            )
            head = result.get("thread")
            head_id = str(head.get("id") or "") if isinstance(head, dict) else ""
            navigation_launched = False
            if head_id:
                try:
                    open_codex_thread(head_id)
                except Exception as exc:
                    result["navigation_warning"] = f"自动打开失败：{str(exc)[:240]}"
                else:
                    navigation_launched = True
                    result["codex_reopened"] = True
            completion = result.get("completion")
            if not isinstance(completion, dict):
                completion = {"core_complete": False}
                result["completion"] = completion
            completion["navigation_launched"] = navigation_launched
            completion["complete"] = bool(
                completion.get("core_complete") and navigation_launched
            )
            return result

        self._run_job(job_label, operation)

    def switch_thread(self) -> None:
        self._run_thread_copy()

    def cancel_wait(self) -> None:
        """Cancel only the local wait; no provider or task state is changed."""

        if not self.busy or not self.waiting_for_exit:
            return
        self.cancel_wait_event.set()
        self.cancel_wait_button.configure(state="disabled")
        self.status_var.set("正在取消等待…")
        self._append_log("已请求取消等待；不会创建任务副本")

    def _on_close(self) -> None:
        if self.busy:
            messagebox.showinfo(
                "操作进行中",
                "请等待操作完成后再关闭切换器。\n"
                "如果正在等待 Codex 退出，请关闭 Codex 主窗口，但不要关闭此切换器。",
                parent=self.root,
            )
            return
        self.closed = True
        # Do not terminate a router that may be serving the Codex desktop.
        self.root.destroy()


def smoke_snapshot(home: Path) -> dict[str, Any]:
    """Headless validation used by CI and the launcher troubleshooting path."""

    paths = switchboard.Paths(home.resolve())
    rows = profile_rows(paths)
    active = switchboard.load_active(paths)
    config_status = switchboard.config_projection_status(paths)
    healthy, detail = router_health(paths, timeout=0.5)
    return {
        "ui_version": UI_VERSION,
        "codex_home": str(paths.codex_home),
        "active_profile": active.get("profile_id"),
        "profiles": [
            {
                "id": row["id"],
                "ready": row["ready"],
                "active": row["active"],
                "key_state": row["key_state"],
                "catalog_state": row.get("catalog_state"),
            }
            for row in rows
        ],
        "config_projection": {
            "ready": config_status.get("ready"),
            "state": config_status.get("state"),
            "reason_count": len(config_status.get("reasons") or []),
        },
        "router": {"healthy": healthy, "detail": detail},
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Switchboard Windows UI")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--smoke-test", action="store_true", help="run a headless status check and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.smoke_test:
        print(json.dumps(smoke_snapshot(args.home), ensure_ascii=False, indent=2))
        return 0
    paths = switchboard.Paths(args.home.resolve())
    guard = SingleInstanceGuard(paths.codex_home)
    if not guard.acquire():
        focus_existing_window()
        return 0
    try:
        root = tk.Tk()
        SwitchboardUI(root, paths)
        root.mainloop()
        return 0
    finally:
        guard.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
