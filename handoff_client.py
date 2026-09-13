"""Explicit, metered handoff client; never used by metadata-only conversions.

Only this client can submit the two user-authorized model turns. It does not
perform provider HTTP calls itself; Codex's provider worker owns credentials
and transport. A submitted/uncertain turn is never automatically resubmitted.
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

from appserver_client import (AppServerClient, AppServerTimeoutError,
                              AppServerValidationError, _path_key, _UNSET)
from handoff_bundle import HandoffValidationError


DISABLED_FEATURES = ("hooks", "goals", "apps", "enable_mcp_apps", "plugins",
                     "remote_plugin", "multi_agent", "multi_agent_v2", "computer_use",
                     "browser_use", "browser_use_external", "in_app_browser",
                     "image_generation", "memories", "skill_mcp_dependency_install")
READ_ONLY = {"type": "readOnly", "networkAccess": False}
HOST_CONTEXT_ENV = {"CODEX_APP_TOOLS_PIPE_PATH", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_PERMISSION_PROFILE",
                    "CODEX_SAGE_BACKFILL_TRACKER_TAB_REUSE", "CODEX_CI"}


class InferenceOutcomeUnknown(HandoffValidationError):
    pass


class _ChildLifetime:
    """Kernel-owned kill-on-close job; a worker crash cannot leave its AI running."""

    def __init__(self, process) -> None:
        self.process = process
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(key, ctypes.c_ulonglong) for key in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway
        try:
            if (not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                    or not kernel.AssignProcessToJobObject(handle, int(process._handle))):
                raise HandoffValidationError("无法建立后台进程退出保护，尚未发送模型请求")
        except BaseException:
            if handle:
                kernel.CloseHandle(handle)
            raise
        self.handle, self.kernel = handle, kernel

    def close(self) -> None:
        if self.handle:
            from ctypes import wintypes

            class Accounting(ctypes.Structure):
                _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                            ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                            ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                            ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

            self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                            wintypes.DWORD, ctypes.c_void_p]
            # Termination is asynchronous. Keep the job until child processes
            # have exited, so sandbox binaries are not left running after QA.
            self.kernel.TerminateJobObject(self.handle, 1)
            deadline = time.monotonic() + 5
            try:
                while time.monotonic() < deadline:
                    state = Accounting()
                    if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(state), ctypes.sizeof(state), None):
                        break
                    if state.ActiveProcesses == 0:
                        break
                    time.sleep(0.05)
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def startup_overrides() -> list[str]:
    values = {f"features.{key}": "false" for key in DISABLED_FEATURES}
    values.update({"sandbox_mode": '"read-only"', "web_search": '"disabled"'})
    return [part for key, value in values.items() for part in ("-c", f"{key}={value}")]


class HandoffClient(AppServerClient):
    _ALLOWED_REQUESTS = frozenset({
        "initialize", "config/read", "configRequirements/read", "account/read",
        "thread/read", "thread/start", "thread/resume",
        "thread/name/set", "turn/start", "turn/interrupt", "mcpServerStatus/list",
        "command/exec",
    })

    def __init__(self, *args, allow_inference: bool = False,
                 temporary_overrides: dict | None = None, **kwargs) -> None:
        self.allow_inference = allow_inference
        self._inference_gate = False
        self._lifetime = None
        self.denied_requests: list[str] = []
        self.safe_config: dict = {}
        self._sessions: dict[str, dict] = {}
        command_args = kwargs.pop("command_args", ("app-server",))
        extras = [part for key, value in (temporary_overrides or {}).items()
                  for part in ("-c", f"{key}={json.dumps(value, ensure_ascii=False)}")]
        kwargs["command_args"] = (*command_args, *startup_overrides(), *extras)
        factory = kwargs.pop("popen_factory", subprocess.Popen)

        def isolated_process(*args, **options):
            # A launched worker must not inherit the initiating desktop turn's
            # IPC tool channel, thread identity or permission override.
            options["env"] = {key: value for key, value in options["env"].items()
                              if key not in HOST_CONTEXT_ENV}
            if os.name != "nt":
                options["start_new_session"] = True
            return factory(*args, **options)

        kwargs["popen_factory"] = isolated_process
        super().__init__(*args, **kwargs)

    def start(self) -> None:
        super().start()
        if self._lifetime is None:
            try:
                self._lifetime = _ChildLifetime(self.process)
            except BaseException:
                super().close()
                raise

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._lifetime is not None:
                self._lifetime.close()
                self._lifetime = None

    def _next_message(self, timeout=None) -> dict:
        deadline = time.monotonic() + (self.request_timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeoutError("handoff response deadline reached")
            message = super()._next_message(remaining)
            if "id" in message and "method" in message:
                # Restored dynamic tools, UI asks and escalation are NEVER
                # delegated to the desktop host or automatically approved.
                self.denied_requests.append(str(message["method"]))
                self._write_message({"id": message["id"], "error": {
                    "code": -32601, "message": "Handoff is read-only; external tools and approvals are unavailable"}})
                continue
            return message

    def _request(self, method, params=_UNSET, *, timeout=None) -> dict:
        if method == "turn/start" and not (self.allow_inference and self._inference_gate):
            raise HandoffValidationError("没有本次接手的模型用量授权")
        return super()._request(method, params, timeout=timeout)

    def prepare(self, cwd: str, providers: list[str], *, discover_only: bool = False) -> dict:
        """Read merged configuration, then narrow it for this session only."""
        loaded = self._request("config/read", {"cwd": cwd, "includeLayers": True})
        effective = loaded.get("config")
        if not isinstance(effective, dict):
            raise HandoffValidationError("无法读取有效配置，接手未启动")
        features = effective.get("features") or {}
        if any(features.get(key) is not False for key in DISABLED_FEATURES):
            raise HandoffValidationError("无法确认插件、钩子或自动继续已禁用，接手未启动")
        if effective.get("sandbox_mode") != "read-only" or effective.get("web_search") != "disabled":
            raise HandoffValidationError("有效配置未保持只读和离线工具权限")
        requirements = self._request("configRequirements/read").get("requirements") or {}
        # Managed hooks can be forced on independently of normal config layers.
        if requirements.get("hooks"):
            raise HandoffValidationError("此环境有管理员钩子，无法证明只读接手无额外副作用")
        overrides = {f"features.{key}": False for key in DISABLED_FEATURES}
        overrides.update({"web_search": "disabled", "sandbox_mode": "read-only"})
        servers = effective.get("mcp_servers") or {}
        if not isinstance(servers, dict):
            raise HandoffValidationError("无法枚举有效 MCP 配置")
        if not discover_only and any(not isinstance(value, dict) or value.get("enabled") is not False for value in servers.values()):
            raise HandoffValidationError("存在尚未临时隔离的 MCP；未启动会话或模型请求")
        for name in servers:
            overrides[f'mcp_servers.{json.dumps(name)}.enabled'] = False
        # Preserve immutable provider endpoints; only turn off automatic retries.
        for provider in providers:
            if not provider or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in provider):
                raise HandoffValidationError("Provider 标识格式不正确")
            # Native OpenAI is reserved in current Codex and cannot be
            # redefined. Never silently route official login through an alias
            # to work around this. Its transport policy remains Codex-owned.
            if provider != "openai":
                configured = (effective.get("model_providers") or {}).get(provider) or {}
                if not discover_only and (configured.get("request_max_retries") != 0 or configured.get("stream_max_retries") != 0):
                    raise HandoffValidationError("尚未确认自定义 Provider 的自动重试关闭")
                overrides[f"model_providers.{provider}.request_max_retries"] = 0
                overrides[f"model_providers.{provider}.stream_max_retries"] = 0
        self.safe_config = overrides if not discover_only else {}
        self.discovered_overrides = overrides
        return {"disabled_features": list(DISABLED_FEATURES), "mcp_servers_disabled": len(servers),
                "sandbox": "read-only", "tool_network": False,
                "switchboard_turn_resubmissions": 0,
                "custom_provider_automatic_retries": 0,
                "official_transport_retries": "codex_managed_not_overridable"}

    def _validate_session(self, result: dict, *, thread_id: str | None, cwd: str,
                          provider: str, model: str, fresh: bool) -> str:
        thread = result.get("thread")
        ident = (thread or {}).get("id")
        if not isinstance(ident, str) or not ident:
            raise HandoffValidationError("会话创建/恢复没有返回可核对的 ID")
        self._validate_thread(thread, thread_id or ident, cwd, provider)
        if (_path_key(result.get("cwd", "")) != _path_key(cwd)
                or result.get("modelProvider") != provider or result.get("model") != model
                or thread.get("model") not in {None, model}):
            raise HandoffValidationError("会话的有效工作区、Provider 或模型与冻结配置不符")
        sandbox = result.get("sandbox") or {}
        if (sandbox.get("type") != "readOnly" or sandbox.get("networkAccess", False) is not False
                or result.get("approvalPolicy") is None or result.get("approvalsReviewer") != "user"):
            raise HandoffValidationError("会话实际权限不是只读；尚未发送本轮模型请求")
        if fresh and (thread.get("forkedFromId") or thread.get("parentThreadId") or thread.get("turns")
                      or thread.get("sessionId") not in {None, ident} or thread.get("ephemeral") is not False):
            raise HandoffValidationError("目标不是独立、持久化的干净会话")
        status = thread.get("status") or {}
        if status.get("type") not in {"idle", "notLoaded"}:
            raise HandoffValidationError("会话并非空闲，禁止发送可能变成插话的请求")
        cursor = None
        for _ in range(100):
            page = self._request("mcpServerStatus/list", {"threadId": ident, "cursor": cursor, "limit": 100})
            if (not isinstance(page.get("data"), list)
                    or any(item.get("runtimeStatus") != "disabled" or item.get("tools")
                           or item.get("resources") or item.get("resourceTemplates") for item in page["data"])):
                raise HandoffValidationError("仍检测到外部 MCP 工具，接手已停止")
            cursor = page.get("nextCursor")
            if not cursor:
                break
        else:
            raise HandoffValidationError("无法完整核验外部工具状态")
        self._sessions[ident] = {"cwd": cwd, "provider": provider, "model": model}
        return ident

    def probe_read_access(self, cwd: str, files: list[Path]) -> None:
        """Fixed local read probe, before model spend; no thread is created.

        Paths are data, never interpolated into a shell program. The caller
        supplies only validated attachments/entrypoints, not model commands.
        """
        if not files:
            raise HandoffValidationError("没有可预检的项目入口文件")
        if os.name == "nt":
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop'; "
                "$handoffPaths=ConvertFrom-Json $env:SWITCHBOARD_HANDOFF_READ_PATHS; "
                "foreach($handoffFile in $handoffPaths){"
                "Get-Content -LiteralPath $handoffFile -Encoding Byte -TotalCount 1 | Out-Null}; "
                "Write-Output 'handoff-read-ok'"]
        else:
            command = ["sh", "-c", 'for path do head -c 1 -- "$path" >/dev/null || exit 1; done; printf handoff-read-ok',
                       "handoff-read", *[str(file) for file in files]]
        try:
            result = self._request("command/exec", {"command": command, "cwd": cwd,
                "sandboxPolicy": dict(READ_ONLY), "timeoutMs": 30000,
                "env": {"SWITCHBOARD_HANDOFF_READ_PATHS": json.dumps([str(file) for file in files], ensure_ascii=False)}})
        except Exception as exc:
            raise HandoffValidationError("只读文件预检无法运行；请先在 Codex 中确认沙盒已就绪。未发送下一步模型请求。") from exc
        if result.get("exitCode") != 0 or result.get("stdout", "").strip() != "handoff-read-ok":
            raise HandoffValidationError("只读后台不能读取项目或附件；请核对文件权限，不会自动提权或重发模型请求。")

    def resume_source(self, thread_id: str, *, cwd: str, provider: str, model: str) -> dict:
        if not self.safe_config or self.safe_config.get("features.goals") is not False:
            raise HandoffValidationError("必须先核验接手权限")
        # Current native Codex rejects goal/get while features.goals=false.
        # Do not turn goals back on just to inspect them. Startup + merged
        # config verification + session overrides disable the continuation
        # scheduler without editing the original persisted goal.
        current = self.read_thread(thread_id, expected_cwd=cwd,
                                   expected_model_provider=provider)["thread"]
        if (current.get("status") or {}).get("type") not in {"idle", "notLoaded"}:
            raise HandoffValidationError("老会话仍在运行，禁止恢复后插入交接请求")
        result = self._request("thread/resume", {"threadId": thread_id, "cwd": cwd,
            "modelProvider": provider, "model": model, "sandbox": "read-only",
            "excludeTurns": True})
        self._validate_session(result, thread_id=thread_id, cwd=cwd, provider=provider, model=model, fresh=False)
        return result

    def create_clean(self, *, cwd: str, model: str, before_send: Callable[[], None],
                     created: Callable[[str], None]) -> dict:
        if not self.safe_config:
            raise HandoffValidationError("必须先核验接手权限")
        before_send()  # Durable receipt BEFORE the unknown-result boundary.
        result = self._request("thread/start", {"cwd": cwd, "modelProvider": "openai", "model": model,
            "sandbox": "read-only", "ephemeral": False})
        ident = (result.get("thread") or {}).get("id")
        if isinstance(ident, str) and ident:
            created(ident)  # Keep committed ID even if validation below fails.
        self._validate_session(result, thread_id=None, cwd=cwd, provider="openai", model=model, fresh=True)
        return result

    def send_turn(self, thread_id: str, prompt: str, schema: dict, *,
                  before_send: Callable[[], None], accepted: Callable[[str], None],
                  client_message_id: str) -> str:
        if not self.allow_inference or thread_id not in self._sessions:
            raise HandoffValidationError("没有已核验会话或模型用量授权")
        session = self._sessions[thread_id]
        current = self.read_thread(thread_id, expected_cwd=session["cwd"],
                                   expected_model_provider=session["provider"])["thread"]
        if (current.get("status") or {}).get("type") != "idle":
            raise HandoffValidationError("会话正在运行，禁止把交接请求当作插话")
        before_send()
        self._inference_gate = True
        try:
            result = self._request("turn/start", {"threadId": thread_id,
                "clientUserMessageId": client_message_id,
                "input": [{"type": "text", "text": prompt}], "outputSchema": schema,
                "sandboxPolicy": dict(READ_ONLY),
                "cwd": session["cwd"], "model": session["model"]})
        finally:
            self._inference_gate = False
        turn = result.get("turn") or {}
        if not isinstance(turn.get("id"), str) or not turn["id"]:
            raise InferenceOutcomeUnknown("请求已提交但未返回回合 ID；不得自动重发")
        accepted(turn["id"])
        return turn["id"]

    def wait_turn(self, thread_id: str, turn_id: str, *, timeout: float = 1800,
                  cancel: Callable[[], bool] = lambda: False,
                  heartbeat: Callable[[], None] = lambda: None) -> dict:
        deadline = time.monotonic() + timeout
        items: dict[str, dict] = {}
        last_heartbeat = 0.0
        while time.monotonic() < deadline:
            if cancel():
                self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                raise InferenceOutcomeUnknown("已请求中断；已产生的模型用量不会撤销，需核验本次结果")
            if time.monotonic() - last_heartbeat >= 3:
                heartbeat()
                last_heartbeat = time.monotonic()
            try:
                message = self._pop_notification(min(1.0, max(0.001, deadline - time.monotonic())))
            except AppServerTimeoutError:
                continue
            params = message.get("params") or {}
            if params.get("threadId") != thread_id:
                continue
            method = message.get("method")
            if method == "item/completed" and params.get("turnId") == turn_id:
                item = params.get("item") or {}
                if isinstance(item.get("id"), str):
                    items[item["id"]] = item
            if method == "turn/completed" and (params.get("turn") or {}).get("id") == turn_id:
                turn = params["turn"]
                if turn.get("status") != "completed" or turn.get("error"):
                    raise InferenceOutcomeUnknown("模型回合未正常完成，已保留 ID；不会自动重发")
                for item in turn.get("items") or []:
                    items[item.get("id", str(len(items)))] = item
                return self.report_from_items(list(items.values()))
        raise InferenceOutcomeUnknown("模型回合超时，已保留提交回执；不会自动重发")

    @staticmethod
    def report_from_items(items: list[dict]) -> dict:
        finals = [item for item in items if item.get("type") == "agentMessage"
                  and item.get("phase") in {None, "final_answer"}]
        if not finals:
            raise InferenceOutcomeUnknown("没有可核对的最终交接输出")
        try:
            report = json.loads(finals[-1]["text"])
        except (ValueError, KeyError, TypeError) as exc:
            raise HandoffValidationError("最终输出不符合交接结构，需在会话中核对") from exc
        if not isinstance(report, dict):
            raise HandoffValidationError("最终交接输出不是对象")
        viewed = [str(Path(item["path"]).resolve()) for item in items
                  if item.get("type") == "imageView" and isinstance(item.get("path"), str)]
        return {"report": report, "viewed_images": viewed}

    def recover_turn(self, thread_id: str, turn_id: str, *, cwd: str, provider: str) -> dict:
        """Read a known turn without resuming it or sending another model request."""
        thread = self.read_thread(thread_id, include_turns=True, expected_cwd=cwd,
                                  expected_model_provider=provider)["thread"]
        matches = [turn for turn in thread.get("turns", []) if turn.get("id") == turn_id]
        if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("error"):
            raise InferenceOutcomeUnknown("原回合没有唯一且完成的结果；保持待核验，禁止重发")
        return self.report_from_items(matches[0].get("items") or [])
