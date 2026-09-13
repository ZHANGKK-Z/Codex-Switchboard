#!/usr/bin/env python3
"""Small, deliberately constrained Codex app-server client.

The switchboard only needs a handful of app-server operations while changing
accounts/providers.  This module keeps that boundary explicit: it can
initialize a child app-server, read account state, start/cancel-free login
flows, log out, read an existing thread, and fork a persisted task into
an explicitly selected provider.  It does not expose turn/start, thread/start,
archive, delete, or any provider request operation.

Transport is JSON-RPC over the app-server's newline-delimited stdio stream.
The process is intentionally injectable so tests never need a real Codex
binary, account, or model provider.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
DEFAULT_CLIENT_NAME = "codex-switchboard"
DEFAULT_CLIENT_VERSION = "0.1.0"

_UNSET = object()
_EOF = object()


class AppServerClientError(RuntimeError):
    """Base class for safe, local app-server client failures."""


class AppServerValidationError(AppServerClientError):
    """The server response or caller input violated the expected contract."""


class AppServerPostCommitValidationError(AppServerValidationError):
    """A durable thread was created, but its returned projection failed validation.

    ``created_thread_id`` is deliberately the only response detail retained so
    callers can reconcile the committed task through the state owner without
    serializing account or Provider payloads.
    """

    def __init__(self, message: str, *, created_thread_id: str) -> None:
        super().__init__(message)
        self.created_thread_id = created_thread_id


class AppServerTimeoutError(AppServerClientError):
    """A response/notification did not arrive before the deadline."""

    def __init__(self, message: str, *, method: str | None = None,
                 request_id: int | str | None = None,
                 created_thread_id: str | None = None) -> None:
        super().__init__(message)
        self.method = method
        self.request_id = request_id
        # A notification is evidence to reconcile, never proof of success.
        self.created_thread_id = created_thread_id


class AppServerProcessError(AppServerClientError):
    """The child process or its stdio stream stopped unexpectedly."""


class AppServerProtocolError(AppServerClientError):
    """The app-server returned a JSON-RPC error response."""

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        request_id: int | str | None = None,
        code: int | None = None,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.request_id = request_id
        self.code = code
        self.data = data


class _MalformedMessage:
    def __init__(self, error: BaseException) -> None:
        self.error = error


PopenFactory = Callable[..., Any]


def _path_key(value: str | os.PathLike[str]) -> str:
    """Normalize an absolute path for strict, Windows-safe comparison."""

    text = os.fspath(value)
    folded = text.casefold()
    # SQLite may persist Windows extended-length paths while App Server
    # returns the equivalent conventional spelling.  Compare their filesystem
    # identity, not the transport spelling; do not resolve junction targets.
    if folded.startswith("\\\\?\\unc\\"):
        text = "\\\\" + text[8:]
    elif folded.startswith("\\\\?\\"):
        candidate = text[4:]
        if len(candidate) >= 3 and candidate[1] == ":" and candidate[2] in "\\/":
            text = candidate

    path = Path(text)
    if not path.is_absolute():
        raise AppServerValidationError(f"path must be absolute: {value!s}")
    # normcase is a no-op on POSIX and case-folds drive/path names on Windows.
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppServerValidationError(f"{field} must be a non-empty string")
    return value


class AppServerClient:
    """Constrained JSON-RPC client for a separately launched app-server.

    Parameters are intentionally explicit.  ``codex_home`` is the expected
    state root; initialize() rejects a child that reports another root.  The
    child receives that path through ``CODEX_HOME`` and private ``TEMP/TMP``
    paths under it, so this helper does not create a second C-drive state
    root.
    """

    _ALLOWED_REQUESTS = frozenset(
        {
            "initialize",
            "account/read",
            "account/login/start",
            "account/logout",
            "config/batchWrite",
            "thread/read",
            "thread/list",
            "thread/fork",
            "thread/name/set",
            "thread/metadata/update",
            "thread/archive",
            "thread/unarchive",
        }
    )

    def __init__(
        self,
        executable: str | os.PathLike[str] = "codex",
        codex_home: str | os.PathLike[str] | None = None,
        *,
        command_args: Sequence[str] = ("app-server",),
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        request_timeout: float = 30.0,
        fork_timeout: float = 180.0,
        client_name: str = DEFAULT_CLIENT_NAME,
        client_version: str = DEFAULT_CLIENT_VERSION,
        popen_factory: PopenFactory | None = None,
    ) -> None:
        home = Path(codex_home) if codex_home is not None else DEFAULT_CODEX_HOME
        self.codex_home = home.absolute()
        if not self.codex_home.is_absolute():  # pragma: no cover - Path.absolute is absolute
            raise AppServerValidationError("codex_home must be absolute")
        if request_timeout <= 0 or fork_timeout <= 0:
            raise AppServerValidationError("request and fork timeouts must be positive")
        self.executable = os.fspath(executable)
        self.command_args = tuple(os.fspath(arg) for arg in command_args)
        self.cwd = os.fspath(cwd) if cwd is not None else None
        self.extra_env = dict(env or {})
        self.request_timeout = float(request_timeout)
        self.fork_timeout = float(fork_timeout)
        self.client_name = _require_nonempty_string(client_name, "client_name")
        self.client_version = _require_nonempty_string(client_version, "client_version")
        self._popen_factory = popen_factory or subprocess.Popen
        self._process: Any | None = None
        self._reader_thread: threading.Thread | None = None
        self._messages: queue.Queue[Any] = queue.Queue()
        self._notifications: deque[dict[str, Any]] = deque()
        self._request_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._closed = False
        self.initialized = False
        self.initialize_response: dict[str, Any] | None = None

    @property
    def process(self) -> Any | None:
        """Expose the child handle read-only for diagnostics/tests."""

        return self._process

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def __enter__(self) -> "AppServerClient":
        self.start()
        self.initialize()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def start(self) -> None:
        """Start the child process without sending any model/provider request."""

        if self._process is not None:
            if self.running:
                return
            raise AppServerProcessError("app-server process has already exited")
        if self._closed:
            raise AppServerProcessError("client is closed")

        runtime_tmp = self.codex_home / "runtime-tmp"
        runtime_tmp.mkdir(parents=True, exist_ok=True)
        child_env = os.environ.copy()
        child_env.update(self.extra_env)
        # The expected state root wins over an inherited or caller-provided
        # value.  TEMP/TMP are scoped to this child and never change the user
        # or desktop process environment.
        child_env["CODEX_HOME"] = os.fspath(self.codex_home)
        child_env["TEMP"] = os.fspath(runtime_tmp)
        child_env["TMP"] = os.fspath(runtime_tmp)

        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "cwd": self.cwd,
            "env": child_env,
            "bufsize": 1,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._process = self._popen_factory([self.executable, *self.command_args], **kwargs)
        except OSError as exc:
            detail = f"failed to start app-server: {exc}"
            if getattr(exc, "winerror", None) == 5 or getattr(exc, "errno", None) == 13:
                detail += f" (access denied for executable: {self.executable})"
            raise AppServerProcessError(detail) from exc
        if getattr(self._process, "stdin", None) is None or getattr(self._process, "stdout", None) is None:
            self.close()
            raise AppServerProcessError("app-server did not expose stdio pipes")
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="codex-appserver-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def close(self) -> None:
        """Close the child process; no history or thread files are touched."""

        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            stdin = getattr(process, "stdin", None)
            if stdin is not None:
                try:
                    stdin.close()
                except (OSError, ValueError):
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        finally:
            thread = self._reader_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=2)
            for stream_name in ("stdout", "stderr"):
                stream = getattr(process, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            self._process = None

    def _reader_loop(self) -> None:
        process = self._process
        if process is None:
            return
        stdout = getattr(process, "stdout", None)
        if stdout is None:
            self._messages.put(_MalformedMessage(AppServerProcessError("missing app-server stdout")))
            return
        try:
            while True:
                line = stdout.readline()
                if line in ("", b"", None):
                    self._messages.put(_EOF)
                    return
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._messages.put(_MalformedMessage(exc))
                    return
                if not isinstance(message, dict):
                    self._messages.put(
                        _MalformedMessage(AppServerValidationError("app-server message must be an object"))
                    )
                    return
                self._messages.put(message)
        except (OSError, ValueError) as exc:
            self._messages.put(_MalformedMessage(exc))

    def _ensure_process(self) -> None:
        if self._closed:
            raise AppServerProcessError("client is closed")
        if self._process is None:
            raise AppServerProcessError("app-server has not been started")
        if self._process.poll() is not None:
            raise AppServerProcessError("app-server process is not running")

    def _ensure_initialized(self) -> None:
        self._ensure_process()
        if not self.initialized:
            raise AppServerValidationError("call initialize() before using app-server methods")

    def _write_message(self, message: dict[str, Any]) -> None:
        self._ensure_process()
        stdin = getattr(self._process, "stdin", None)
        if stdin is None:
            raise AppServerProcessError("app-server stdin is unavailable")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._write_lock:
                stdin.write(payload)
                stdin.flush()
        except (OSError, ValueError) as exc:
            raise AppServerProcessError(f"failed to write app-server request: {exc}") from exc

    def _next_message(self, timeout: float | None = None) -> dict[str, Any]:
        wait_for = self.request_timeout if timeout is None else timeout
        try:
            item = self._messages.get(timeout=wait_for)
        except queue.Empty as exc:
            raise AppServerTimeoutError("timed out waiting for app-server response") from exc
        if item is _EOF:
            code = None
            if self._process is not None:
                try:
                    code = self._process.poll()
                except (OSError, AttributeError):
                    pass
            suffix = f" (exit code {code})" if code is not None else ""
            raise AppServerProcessError(f"app-server stdio closed{suffix}")
        if isinstance(item, _MalformedMessage):
            raise AppServerProcessError(f"invalid app-server message: {item.error}") from item.error
        if not isinstance(item, dict):
            raise AppServerProcessError("invalid app-server message")
        return item

    def _send_notification(self, method: str, params: Any = _UNSET) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not _UNSET:
            message["params"] = params
        self._write_message(message)

    def _request(self, method: str, params: Any = _UNSET, *,
                 timeout: float | None = None) -> dict[str, Any]:
        """Send one of the intentionally allowed requests and await its result."""

        if method not in self._ALLOWED_REQUESTS:
            raise AppServerValidationError(f"request method is outside safe client scope: {method}")
        if method != "initialize":
            self._ensure_initialized()
        else:
            self._ensure_process()
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not _UNSET:
            message["params"] = params
        with self._request_lock:
            self._write_message(message)
            deadline = time.monotonic() + (self.request_timeout if timeout is None else timeout)
            candidates: set[str] = set()
            while True:
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0:
                        raise AppServerTimeoutError("request deadline reached")
                    response = self._next_message(remaining)
                except AppServerTimeoutError as exc:
                    raise AppServerTimeoutError(
                        f"timed out waiting for app-server response ({method}, request {request_id})",
                        method=method, request_id=request_id,
                        created_thread_id=next(iter(candidates)) if len(candidates) == 1 else None,
                    ) from exc
                if "id" not in response:
                    self._notifications.append(response)
                    if method == "thread/fork" and response.get("method") == "thread/started":
                        notification = response.get("params") or {}
                        thread = notification.get("thread") if isinstance(notification, dict) else None
                        if (isinstance(thread, dict) and isinstance(params, dict)
                                and thread.get("forkedFromId") == params.get("threadId")
                                and isinstance(thread.get("id"), str)
                                and thread["id"] != params.get("threadId")):
                            candidates.add(thread["id"])
                    continue
                if response.get("id") != request_id:
                    # There is no public concurrent-request API in this
                    # constrained client. Keep an unmatched response visible
                    # as a protocol error rather than accidentally consuming
                    # another caller's result.
                    raise AppServerProtocolError(
                        "app-server returned an unexpected request id",
                        method=method,
                        request_id=request_id,
                        data=response,
                    )
                if response.get("error") is not None:
                    error = response.get("error")
                    if isinstance(error, dict):
                        message_text = str(error.get("message") or "app-server request failed")
                        code = error.get("code")
                        data = error.get("data")
                    else:
                        message_text = str(error)
                        code = None
                        data = None
                    raise AppServerProtocolError(
                        message_text,
                        method=method,
                        request_id=request_id,
                        code=code if isinstance(code, int) else None,
                        data=data,
                    )
                result = response.get("result")
                if result is None:
                    return {}
                if not isinstance(result, dict):
                    raise AppServerValidationError(f"{method} result must be an object")
                return result

    def initialize(
        self,
        *,
        capabilities: Mapping[str, Any] | None = None,
        client_name: str | None = None,
        client_version: str | None = None,
    ) -> dict[str, Any]:
        """Negotiate the protocol and verify the child's exact CODEX_HOME."""

        if self.initialized:
            return dict(self.initialize_response or {})
        params = {
            "clientInfo": {
                "name": client_name or self.client_name,
                "version": client_version or self.client_version,
            },
            "capabilities": dict(capabilities) if capabilities is not None else None,
        }
        result = self._request("initialize", params)
        reported_home = result.get("codexHome")
        if not isinstance(reported_home, str):
            raise AppServerValidationError("initialize response is missing codexHome")
        if _path_key(reported_home) != _path_key(self.codex_home):
            raise AppServerValidationError(
                f"app-server codexHome mismatch: expected {self.codex_home}, got {reported_home}"
            )
        self._send_notification("initialized")
        self.initialized = True
        self.initialize_response = dict(result)
        return dict(result)

    def read_account(self, *, refresh_token: bool = False) -> dict[str, Any]:
        """Read current managed account metadata without exposing credentials."""

        params: dict[str, Any] = {}
        if refresh_token:
            params["refreshToken"] = True
        return self._request("account/read", params)

    def login(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Start a supported account login flow.

        The caller may receive an OAuth URL/device code and complete it in the
        browser.  Secret values are passed through but never included in
        exception text or logs by this module.
        """

        if not isinstance(params, Mapping):
            raise AppServerValidationError("login params must be a mapping")
        payload = dict(params)
        login_type = payload.get("type")
        allowed_types = {"apiKey", "chatgpt", "chatgptDeviceCode", "chatgptAuthTokens", "amazonBedrock"}
        if login_type not in allowed_types:
            raise AppServerValidationError(f"unsupported login type: {login_type!r}")
        if login_type == "apiKey" and not isinstance(payload.get("apiKey"), str):
            raise AppServerValidationError("apiKey login requires apiKey")
        if login_type == "chatgptAuthTokens":
            required = ("accessToken", "chatgptAccountId")
            if any(not isinstance(payload.get(name), str) or not payload[name] for name in required):
                raise AppServerValidationError("chatgptAuthTokens login requires accessToken and chatgptAccountId")
        return self._request("account/login/start", payload)

    def wait_for_login_completed(
        self,
        *,
        login_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait for the server's account/login/completed notification."""

        if login_id is not None:
            _require_nonempty_string(login_id, "login_id")
        deadline = time.monotonic() + (self.request_timeout if timeout is None else timeout)
        while True:
            remaining = max(0.001, deadline - time.monotonic())
            if remaining <= 0:
                raise AppServerTimeoutError("timed out waiting for account/login/completed")
            message = self._pop_notification(remaining)
            if message.get("method") != "account/login/completed":
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                raise AppServerValidationError("account/login/completed params must be an object")
            notification_login_id = params.get("loginId")
            if login_id is not None and notification_login_id not in (None, login_id):
                continue
            if not isinstance(params.get("success"), bool):
                raise AppServerValidationError("account/login/completed must include boolean success")
            return dict(params)

    def logout(self) -> dict[str, Any]:
        """Log out the current managed OpenAI account."""

        return self._request("account/logout")

    def write_config_batch(
        self,
        edits: Sequence[Mapping[str, Any]],
        *,
        reload_user_config: bool = True,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        """Apply user-level config edits through the supported app-server API."""

        if not isinstance(edits, Sequence) or isinstance(edits, (str, bytes)) or not edits:
            raise AppServerValidationError("config edits must be a non-empty sequence")
        normalized: list[dict[str, Any]] = []
        for edit in edits:
            if not isinstance(edit, Mapping):
                raise AppServerValidationError("each config edit must be an object")
            key_path = _require_nonempty_string(edit.get("keyPath"), "config keyPath")
            strategy = edit.get("mergeStrategy")
            if strategy not in {"replace", "upsert"}:
                raise AppServerValidationError("config mergeStrategy must be replace or upsert")
            if "value" not in edit:
                raise AppServerValidationError("config edit is missing value")
            normalized.append({"keyPath": key_path, "mergeStrategy": strategy, "value": edit["value"]})
        params: dict[str, Any] = {
            "edits": normalized,
            "reloadUserConfig": bool(reload_user_config),
        }
        if expected_version is not None:
            params["expectedVersion"] = expected_version
        return self._request("config/batchWrite", params)

    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
        expected_cwd: str | os.PathLike[str] | None = None,
        expected_model_provider: str | None = None,
    ) -> dict[str, Any]:
        _require_nonempty_string(thread_id, "thread_id")
        result = self._request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": bool(include_turns)},
        )
        thread = result.get("thread")
        self._validate_thread(thread, thread_id, expected_cwd, expected_model_provider)
        return result

    def snapshot_thread(self, thread_id: str) -> dict[str, Any]:
        """Read stable identity fields used to gate a provider/account switch."""

        result = self.read_thread(thread_id, include_turns=False)
        thread = result["thread"]
        # Current App Server responses call this field ``name``; tolerate
        # ``title`` as well so identity validation remains meaningful across
        # protocol versions.
        thread_name = thread.get("name")
        if thread_name is None:
            thread_name = thread.get("title")
        return {
            "id": thread["id"],
            "name": thread_name,
            "cwd": thread["cwd"],
            "modelProvider": thread["modelProvider"],
        }

    def fork_thread(
        self,
        thread_id: str,
        *,
        model_provider: str,
        cwd: str | os.PathLike[str] | None = None,
        expected_cwd: str | os.PathLike[str] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Fork persisted history into a new task with a durable provider.

        App Server only treats ``modelProvider`` as durable when a task is
        created or forked. ``deferGoalContinuation`` prevents a carried goal
        from automatically starting a paid turn during this local migration.
        """

        source_id = _require_nonempty_string(thread_id, "thread_id")
        target_provider = _require_nonempty_string(model_provider, "model_provider")
        params: dict[str, Any] = {
            "threadId": source_id,
            "modelProvider": target_provider,
            "deferGoalContinuation": True,
            "threadSource": "user",
            # This changes the response payload only, never the inherited history.
            "excludeTurns": True,
        }
        if cwd is not None:
            params["cwd"] = os.fspath(cwd)
        if model is not None:
            params["model"] = _require_nonempty_string(model, "model")
        result = self._request("thread/fork", params, timeout=self.fork_timeout)
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerValidationError("thread/fork response is missing thread object")
        fork_id = _require_nonempty_string(thread.get("id"), "thread.id")
        if fork_id == source_id:
            raise AppServerValidationError("thread/fork returned the source thread id")
        if thread.get("forkedFromId") != source_id:
            raise AppServerValidationError("thread/fork response has an unexpected source id")
        try:
            expected_path = expected_cwd if expected_cwd is not None else cwd
            self._validate_thread(thread, fork_id, expected_path, target_provider)
            top_cwd = result.get("cwd")
            top_provider = result.get("modelProvider")
            if not isinstance(top_cwd, str) or not isinstance(top_provider, str):
                raise AppServerValidationError("thread/fork response missing cwd/modelProvider")
            if _path_key(top_cwd) != _path_key(thread["cwd"]):
                raise AppServerValidationError("thread/fork top-level cwd differs from thread.cwd")
            if top_provider != target_provider:
                raise AppServerValidationError(
                    "thread/fork effective modelProvider mismatch: "
                    f"expected {target_provider}, got {top_provider}"
                )
        except AppServerValidationError as exc:
            raise AppServerPostCommitValidationError(
                str(exc),
                created_thread_id=fork_id,
            ) from exc
        return result

    def set_thread_name(self, thread_id: str, name: str) -> dict[str, Any]:
        """Set a bounded display name on a task created by this client."""

        normalized_name = _require_nonempty_string(name, "name").strip()
        if len(normalized_name) > 200:
            normalized_name = normalized_name[:197].rstrip() + "..."
        return self._request(
            "thread/name/set",
            {
                "threadId": _require_nonempty_string(thread_id, "thread_id"),
                "name": normalized_name,
            },
        )

    def set_thread_pinned(self, thread_id: str, pinned: bool) -> dict[str, Any]:
        """Persist a task's pin state through the supported metadata owner."""

        normalized_id = _require_nonempty_string(thread_id, "thread_id")
        result = self._request(
            "thread/metadata/update",
            {"threadId": normalized_id, "isPinned": bool(pinned)},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != normalized_id:
            raise AppServerValidationError(
                "thread/metadata/update response has an unexpected task id"
            )
        if thread.get("isPinned") is not bool(pinned):
            raise AppServerValidationError(
                "thread/metadata/update response has an unexpected pin state"
            )
        return result

    def list_threads(
        self,
        *,
        archived: bool = False,
        limit: int = 50,
        model_providers: Sequence[str] | None = None,
        parent_thread_id: str | None = None,
        ancestor_thread_id: str | None = None,
        sort_key: str = "recency_at",
        sort_direction: str = "desc",
        use_state_db_only: bool = True,
    ) -> dict[str, Any]:
        """List tasks through App Server without scanning or rewriting history."""

        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise AppServerValidationError("thread list limit must be between 1 and 200")
        if parent_thread_id is not None and ancestor_thread_id is not None:
            raise AppServerValidationError("parent_thread_id and ancestor_thread_id are mutually exclusive")
        if sort_key not in {"created_at", "updated_at", "recency_at"}:
            raise AppServerValidationError("unsupported thread list sort key")
        if sort_direction not in {"asc", "desc"}:
            raise AppServerValidationError("unsupported thread list sort direction")
        params: dict[str, Any] = {
            "archived": bool(archived),
            "limit": limit,
            "sortKey": sort_key,
            "sortDirection": sort_direction,
            "useStateDbOnly": bool(use_state_db_only),
        }
        if model_providers is not None:
            params["modelProviders"] = [
                _require_nonempty_string(value, "model_provider") for value in model_providers
            ]
        if parent_thread_id is not None:
            params["parentThreadId"] = _require_nonempty_string(parent_thread_id, "parent_thread_id")
        if ancestor_thread_id is not None:
            params["ancestorThreadId"] = _require_nonempty_string(ancestor_thread_id, "ancestor_thread_id")
        result = self._request("thread/list", params)
        data = result.get("data")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise AppServerValidationError("thread/list response is missing data")
        return result

    def archive_thread(self, thread_id: str) -> dict[str, Any]:
        """Archive a task using the supported App Server owner."""

        return self._request(
            "thread/archive",
            {"threadId": _require_nonempty_string(thread_id, "thread_id")},
        )

    def unarchive_thread(self, thread_id: str) -> dict[str, Any]:
        """Restore an archived task and validate the returned identity."""

        normalized_id = _require_nonempty_string(thread_id, "thread_id")
        result = self._request("thread/unarchive", {"threadId": normalized_id})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != normalized_id:
            raise AppServerValidationError("thread/unarchive response has an unexpected task id")
        return result

    @staticmethod
    def _validate_thread(
        thread: Any,
        thread_id: str,
        expected_cwd: str | os.PathLike[str] | None,
        expected_provider: str | None,
    ) -> None:
        if not isinstance(thread, dict):
            raise AppServerValidationError("thread response is missing thread object")
        if thread.get("id") != thread_id:
            raise AppServerValidationError(
                f"thread id mismatch: expected {thread_id}, got {thread.get('id')!r}"
            )
        actual_cwd = thread.get("cwd")
        actual_provider = thread.get("modelProvider")
        if not isinstance(actual_cwd, str) or not isinstance(actual_provider, str):
            raise AppServerValidationError("thread response missing cwd/modelProvider")
        if expected_cwd is not None and _path_key(actual_cwd) != _path_key(expected_cwd):
            raise AppServerValidationError(
                f"thread cwd mismatch: expected {expected_cwd}, got {actual_cwd}"
            )
        if expected_provider is not None and actual_provider != expected_provider:
            raise AppServerValidationError(
                f"thread modelProvider mismatch: expected {expected_provider}, got {actual_provider}"
            )

    def _pop_notification(self, timeout: float) -> dict[str, Any]:
        if self._notifications:
            return self._notifications.popleft()
        message = self._next_message(timeout)
        if "id" in message:
            # A response arriving while waiting for a notification cannot be
            # safely assigned to a caller; retain it as a diagnostic failure.
            raise AppServerProtocolError("unexpected response while waiting for notification", data=message)
        return message


__all__ = [
    "AppServerClient",
    "AppServerClientError",
    "AppServerPostCommitValidationError",
    "AppServerProcessError",
    "AppServerProtocolError",
    "AppServerTimeoutError",
    "AppServerValidationError",
]
