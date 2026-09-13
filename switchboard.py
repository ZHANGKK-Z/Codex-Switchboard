#!/usr/bin/env python3
"""Local Codex Switchboard.

This utility deliberately keeps the Codex task database and rollout history in
one CODEX_HOME.  It manages provider aliases and migration plans without
forking, deleting, or rewriting conversation history.

The program is Windows-first and uses only the Python standard library.  It
does not print credential values.  Relay keys are protected with Windows
DPAPI; official ChatGPT login is intentionally left to Codex's supported
login flow and the user.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wintypes
import getpass
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from history_chain import (
    HistoryChainError,
    is_benign_metadata_duplicate,
    read_projected_history,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ is required on Windows
    tomllib = None  # type: ignore[assignment]


DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SWITCHBOARD_DIRNAME = "switchboard"
MODEL_CATALOG_DIRNAME = "model-catalogs"
DEFAULT_ROUTER_HOST = "127.0.0.1"
DEFAULT_ROUTER_PORT = 8765
THREAD_SORT_RECENT = "recent"
THREAD_SORT_CODEX = "codex"
THREAD_SORT_MODES = frozenset({THREAD_SORT_RECENT, THREAD_SORT_CODEX})
MAX_THREAD_INVENTORY_ROWS = 5000
MAX_REQUEST_BYTES = 128 * 1024 * 1024
MAX_MODEL_DISCOVERY_BYTES = 2 * 1024 * 1024
WRITER_LOCK_STALE_SECONDS = 10 * 60
CONVERSION_LOCK_STALE_SECONDS = 30 * 60
DEFAULT_APP_SERVER_TIMEOUT = 45.0
DEFAULT_BACKUP_KEEP_PER_SERIES = 2
DEFAULT_BACKUP_MIN_AGE_DAYS = 3.0

_LOCAL_CONVERSION_MUTEXES: set[str] = set()
_LOCAL_CONVERSION_MUTEX_GUARD = threading.Lock()


class MigrationRetryable(RuntimeError):
    """A writer or transient directory handle reappeared during migration."""


class UnreadableThreadHistoryError(RuntimeError):
    """A persisted task cannot be proven safe to fork from its projection."""

    def __init__(
        self,
        thread_id: str,
        candidate_thread_ids: Iterable[str] = (),
        *,
        reason: str = "unreadable",
        segment_id: str | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.candidate_thread_ids = tuple(candidate_thread_ids)
        self.reason = reason
        self.segment_id = segment_id
        candidates = ",".join(self.candidate_thread_ids) or "none"
        super().__init__(
            "thread history projection is unreadable: "
            f"thread_id={thread_id}; reason={reason}; candidates={candidates}"
            + (f"; segment={segment_id}" if segment_id else "")
        )


class PartialThreadConversionError(RuntimeError):
    """A new task exists, but Switchboard could not safely finish reconciliation."""

    def __init__(self, thread_id: str, detail: str) -> None:
        self.thread_id = thread_id
        self.detail = detail
        super().__init__(
            f"task {thread_id} was created, but conversion reconciliation failed: {detail}"
        )


class ConversionOutcomeUnknownError(RuntimeError):
    """A submitted fork must be reconciled, never blindly submitted again."""

    def __init__(self, source_thread_id: str, candidate_thread_ids: Iterable[str] = ()) -> None:
        self.source_thread_id = source_thread_id
        self.candidate_thread_ids = tuple(candidate_thread_ids)
        candidates = ", ".join(self.candidate_thread_ids) or "暂未确认"
        super().__init__(f"转换回执待核验，已暂停重复创建。来源：{source_thread_id}；"
                         f"候选新任务：{candidates}。请勿反复转换；原任务保持保留。")


def _conversion_receipts(paths: Paths) -> dict[str, Any]:
    # The existing conversion mutex owns this operation journal. It records
    # uncertain submissions, never task/provider truth or conversation data.
    value = read_json(paths.switchboard / "conversion-receipts.json", {"version": 1, "pending": {}})
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("pending"), dict):
        raise RuntimeError("转换回执格式异常，已阻止新建副本，请先核验回执")
    return value


def _save_conversion_receipt(paths: Paths, source_id: str, receipt: dict | None) -> None:
    value = _conversion_receipts(paths)
    if receipt is None:
        value["pending"].pop(source_id, None)
    else:
        value["pending"][source_id] = receipt
    atomic_write_json(paths.switchboard / "conversion-receipts.json", value)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # Flush the replacement file before publishing it.  The active profile is
    # read by a separate router process, so a plain write can otherwise leave a
    # truncated JSON document visible after a power loss or process crash.
    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Publish a file atomically after flushing it to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_bytes(value: int | float) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def directory_bytes(path: Path) -> tuple[int, int]:
    files = 0
    total = 0
    if not path.exists():
        return 0, 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        entry_path = Path(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            # Do not descend through junction/reparse-point
                            # directories; they are separate roots and are
                            # copied as links by robocopy's /SJ option.
                            is_junction = getattr(entry_path, "is_junction", None)
                            if is_junction is not None and is_junction():
                                continue
                            pending.append(entry_path)
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return files, total


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob_from_bytes(data: bytes) -> tuple[DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    blob = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required for credential storage")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, source_buffer = _blob_from_bytes(data)
    target = DataBlob()
    if not crypt32.CryptProtectData(ctypes.byref(source), "Codex Switchboard", None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
        _ = source_buffer


def dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required for credential storage")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, source_buffer = _blob_from_bytes(data)
    target = DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
        _ = source_buffer


@dataclass(frozen=True)
class Paths:
    codex_home: Path

    @property
    def switchboard(self) -> Path:
        return self.codex_home / SWITCHBOARD_DIRNAME

    @property
    def profiles(self) -> Path:
        return self.switchboard / "profiles.json"

    @property
    def active(self) -> Path:
        return self.switchboard / "active.json"

    @property
    def provider_versions(self) -> Path:
        return self.switchboard / "provider-versions.json"

    @property
    def keys(self) -> Path:
        return self.switchboard / "keys"

    @property
    def backups(self) -> Path:
        return self.switchboard / "backups"

    @property
    def migration_manifest(self) -> Path:
        return self.switchboard / "migration-manifest.json"

    @property
    def runtime_tmp(self) -> Path:
        return self.codex_home / "runtime-tmp"

    @property
    def config(self) -> Path:
        return self.codex_home / "config.toml"

    @property
    def global_state(self) -> Path:
        return self.codex_home / ".codex-global-state.json"

    @property
    def state_database(self) -> Path:
        return self.codex_home / "state_5.sqlite"

    @property
    def thread_history_database(self) -> Path:
        return self.codex_home / "thread_history_1.sqlite"

    @property
    def model_catalogs(self) -> Path:
        return self.switchboard / MODEL_CATALOG_DIRNAME

    @property
    def conversion_lock(self) -> Path:
        return self.switchboard / "conversion-operation.lock"


def _readonly_sqlite(path: Path) -> sqlite3.Connection:
    """Open one Codex database through a fail-closed, read-only URI."""

    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _thread_cwd_key(value: Any) -> str:
    """Normalize persisted Windows paths without resolving junction targets."""

    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.abspath(text))


def discover_readable_thread_candidates(
    paths: Paths,
    thread_id: str,
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Find recent readable tasks with the same cwd and first user message.

    ``state_5.sqlite`` owns task identity and source metadata, while
    ``thread_history_1.sqlite`` owns the desktop projection.  The returned
    records deliberately omit message text and credentials.
    """

    if limit <= 0 or not paths.state_database.is_file() or not paths.thread_history_database.is_file():
        return []
    state = _readonly_sqlite(paths.state_database)
    history = _readonly_sqlite(paths.thread_history_database)
    try:
        source = state.execute(
            "SELECT cwd, first_user_message FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if source is None or not str(source["first_user_message"] or "").strip():
            return []
        source_cwd = _thread_cwd_key(source["cwd"])
        rows = state.execute(
            """
            SELECT id, cwd, COALESCE(updated_at_ms, updated_at * 1000, 0) AS updated_at_ms
            FROM threads
            WHERE id <> ? AND archived = 0 AND first_user_message = ?
            ORDER BY updated_at_ms DESC
            LIMIT 100
            """,
            (thread_id, source["first_user_message"]),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            if _thread_cwd_key(row["cwd"]) != source_cwd:
                continue
            projected_turns = int(
                history.execute(
                    "SELECT COUNT(*) FROM thread_turns WHERE thread_id = ?",
                    (row["id"],),
                ).fetchone()[0]
            )
            if projected_turns <= 0:
                continue
            candidates.append(
                {
                    "id": str(row["id"]),
                    "projected_turns": projected_turns,
                    "updated_at_ms": int(row["updated_at_ms"] or 0),
                }
            )
            if len(candidates) >= limit:
                break
        return candidates
    finally:
        history.close()
        state.close()


_CODEX_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_PROJECTION_CURSOR_RECORD_BYTES = 4 * 1024 * 1024


def _rollout_segment_id(rollout_path: Path, thread_id: str) -> str:
    """Return the projection owner for a rollout's latest persisted segment."""

    stem = rollout_path.stem
    if stem.endswith(thread_id):
        return thread_id
    marker = f"{thread_id}_"
    if marker in stem:
        suffix = stem.rsplit(marker, 1)[1]
        if _CODEX_ID_RE.fullmatch(suffix):
            return suffix
    return ""


def _projection_cursor_record(path: Path, offset: int) -> dict[str, Any]:
    """Read one bounded JSONL record without loading a large rollout."""

    size = path.stat().st_size
    if offset < 0:
        return {"valid": False, "reason": "negative_projection_offset"}
    if offset > size:
        return {"valid": False, "reason": "projection_offset_past_eof"}
    if offset == size:
        return {"valid": True, "eof": True}
    with path.open("rb") as handle:
        if offset:
            handle.seek(offset - 1)
            if handle.read(1) != b"\n":
                return {"valid": False, "reason": "projection_offset_inside_record"}
        handle.seek(offset)
        raw = handle.readline(_MAX_PROJECTION_CURSOR_RECORD_BYTES + 1)
    if len(raw) > _MAX_PROJECTION_CURSOR_RECORD_BYTES and not raw.endswith(b"\n"):
        return {"valid": False, "reason": "projection_record_too_large"}
    try:
        item = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {"valid": False, "reason": "projection_record_invalid_json"}
    if not isinstance(item, dict):
        return {"valid": False, "reason": "projection_record_not_object"}
    ordinal = item.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        return {"valid": False, "reason": "projection_record_missing_ordinal"}
    payload = item.get("payload")
    return {
        "valid": True,
        "eof": False,
        "ordinal": ordinal,
        "record_type": item.get("type"),
        "payload_type": payload.get("type") if isinstance(payload, dict) else None,
    }


def _rollout_metadata_status(path: Path, thread_id: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.readline(_MAX_PROJECTION_CURSOR_RECORD_BYTES + 1)
    except OSError:
        return {"valid": False, "reason": "rollout_metadata_unreadable"}
    if len(raw) > _MAX_PROJECTION_CURSOR_RECORD_BYTES and not raw.endswith(b"\n"):
        return {"valid": False, "reason": "rollout_metadata_too_large"}
    try:
        item = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {"valid": False, "reason": "rollout_metadata_invalid_json"}
    payload = item.get("payload") if isinstance(item, dict) else None
    ordinal = item.get("ordinal") if isinstance(item, dict) else None
    if (
        not isinstance(item, dict)
        or item.get("type") != "session_meta"
        or not isinstance(payload, dict)
        or payload.get("id") != thread_id
        or isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
    ):
        return {"valid": False, "reason": "rollout_metadata_identity_invalid"}
    history_base = payload.get("history_base")
    if history_base is not None:
        if not isinstance(history_base, dict):
            return {"valid": False, "reason": "history_base_invalid"}
        base_id = history_base.get("thread_id")
        base_offset = history_base.get("end_byte_offset")
        base_ordinal = history_base.get("end_ordinal_exclusive")
        if (
            not isinstance(base_id, str)
            or not base_id
            or isinstance(base_offset, bool)
            or not isinstance(base_offset, int)
            or base_offset < 0
            or isinstance(base_ordinal, bool)
            or not isinstance(base_ordinal, int)
            or base_ordinal < 0
            or ordinal != base_ordinal
        ):
            return {"valid": False, "reason": "history_base_boundary_invalid"}
    return {"valid": True, "ordinal": ordinal, "history_base": history_base}


def _last_rollout_record(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size <= 0:
        return {"valid": False, "reason": "rollout_empty"}
    start = max(0, size - _MAX_PROJECTION_CURSOR_RECORD_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        tail = handle.read()
    lines = tail.splitlines()
    if start and lines:
        lines = lines[1:]
    if not lines:
        return {"valid": False, "reason": "rollout_tail_record_too_large"}
    try:
        item = json.loads(lines[-1].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {"valid": False, "reason": "rollout_tail_invalid_json"}
    ordinal = item.get("ordinal") if isinstance(item, dict) else None
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        return {"valid": False, "reason": "rollout_tail_missing_ordinal"}
    return {"valid": True, "ordinal": ordinal}


def _rollout_ordinal_integrity(path: Path) -> dict[str, Any]:
    """Stream one selected rollout and reject unsafe ordinal anomalies."""

    previous: int | None = None
    previous_record: dict[str, Any] | None = None
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, 1):
                digest.update(raw)
                size += len(raw)
                try:
                    item = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    return {"valid": False, "reason": "rollout_record_invalid_json"}
                record = item if isinstance(item, dict) else None
                ordinal = record.get("ordinal") if record is not None else None
                if isinstance(ordinal, bool) or not isinstance(ordinal, int):
                    return {"valid": False, "reason": "rollout_record_missing_ordinal"}
                if previous is not None and ordinal != previous + 1:
                    expected = previous + 1
                    if not (
                        ordinal < expected
                        and is_benign_metadata_duplicate(
                            previous_record, record, expected
                        )
                    ):
                        return {
                            "valid": False,
                            "reason": (
                                "rollout_duplicate_ordinal"
                                if ordinal <= previous
                                else "rollout_ordinal_gap"
                            ),
                            "line": line_number,
                        }
                else:
                    previous = ordinal
                previous_record = record
    except OSError:
        return {"valid": False, "reason": "rollout_unreadable"}
    return {
        "valid": previous is not None,
        "reason": "rollout_ordinals_contiguous",
        "size": size,
        "sha256": digest.hexdigest(),
    }


def thread_history_projection_status(
    paths: Paths,
    thread_id: str,
    *,
    include_candidates: bool = True,
    verify_rollout: bool = True,
    history_path_index: dict | None = None,
) -> dict[str, Any] | None:
    """Inspect whether a persisted task is safe to use as a fork source.

    Missing projection data fails closed. Turns belong to segments, including
    inherited segments bounded by history_base, rather than necessarily to the
    task's root ID. A cursor behind EOF is still blocked.
    """

    if not paths.state_database.is_file() or not paths.thread_history_database.is_file():
        return {"available": False, "thread_id": thread_id, "health": "unknown",
                "reason": "history_database_missing", "safe_to_fork": False,
                "unreadable": False, "candidates": []}
    state = _readonly_sqlite(paths.state_database)
    history = _readonly_sqlite(paths.thread_history_database)
    try:
        row = state.execute(
            """
            SELECT id, rollout_path, first_user_message
            FROM threads
            WHERE id = ?
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            return {
                "available": True,
                "thread_found": False,
                "thread_id": thread_id,
                "unreadable": False,
                "candidates": [],
            }
        projected_turns = int(
            history.execute(
                "SELECT COUNT(*) FROM thread_turns WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
        )
        projected_items = int(
            history.execute(
                "SELECT COUNT(*) FROM thread_items WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
        )
        rollout_path = Path(str(row["rollout_path"] or ""))
        try:
            rollout_size = rollout_path.stat().st_size
        except (OSError, ValueError):
            rollout_size = None
        metadata = (
            _rollout_metadata_status(rollout_path, thread_id)
            if rollout_size is not None
            else {"valid": False, "reason": "rollout_missing_or_unreadable"}
        )
        segment_id = _rollout_segment_id(rollout_path, thread_id)
        projection = history.execute(
            """
            SELECT next_rollout_byte_offset, next_rollout_ordinal
            FROM thread_history_projection_state
            WHERE thread_id = ?
            """,
            (segment_id,),
        ).fetchone()
        history_base = metadata.get("history_base") if metadata.get("valid") else None
        if isinstance(history_base, dict):
            base_projection = history.execute(
                "SELECT next_rollout_byte_offset, next_rollout_ordinal "
                "FROM thread_history_projection_state WHERE thread_id = ?",
                (history_base["thread_id"],),
            ).fetchone()
            if (
                base_projection is None
                or int(base_projection["next_rollout_byte_offset"])
                < int(history_base["end_byte_offset"])
                or int(base_projection["next_rollout_ordinal"])
                < int(history_base["end_ordinal_exclusive"])
            ):
                metadata = {"valid": False, "reason": "history_base_projection_incomplete"}
        projection_offset = int(projection["next_rollout_byte_offset"]) if projection is not None else None
        projection_ordinal = int(projection["next_rollout_ordinal"]) if projection is not None else None
        projection_complete = bool(projection_offset is not None and projection_offset == rollout_size)
        raw_nonempty = bool(str(row["first_user_message"] or "").strip())
        cursor_record: dict[str, Any] | None = None
        chain: dict[str, Any] | None = None
        error_segment: str | None = None
        if rollout_size is None:
            health = "unknown"
            reason = "rollout_missing_or_unreadable"
        elif not metadata.get("valid"):
            health = "unknown"
            reason = str(metadata.get("reason") or "rollout_metadata_invalid")
        elif projection is None:
            health = "unknown"
            reason = "projection_cursor_missing"
        elif projection_complete:
            try:
                tail_record = _last_rollout_record(rollout_path)
            except OSError:
                tail_record = {"valid": False, "reason": "rollout_tail_unreadable"}
            if not tail_record.get("valid"):
                health = "unknown"
                reason = str(tail_record.get("reason") or "rollout_tail_invalid")
            elif tail_record.get("ordinal") != projection_ordinal - 1:
                health = "stalled"
                reason = "projection_eof_ordinal_mismatch"
            else:
                try:
                    chain = read_projected_history(
                        paths.codex_home, paths.state_database, paths.thread_history_database,
                        thread_id, verify_rollout=verify_rollout, path_index=history_path_index,
                    )
                    projected_turns = len(chain["turn_ids"])
                    projected_items = chain["item_count"]
                    health = "unreadable" if raw_nonempty and not projected_turns else "healthy"
                    reason = "projection_eof_without_turns" if health == "unreadable" else "projection_at_eof"
                except HistoryChainError as exc:
                    reason = str(exc)
                    error_segment = getattr(exc, "segment_id", None)
                    health = "stalled" if reason in {"rollout_duplicate_ordinal", "rollout_ordinal_gap"} else "unknown"
        else:
            try:
                cursor_record = _projection_cursor_record(rollout_path, projection_offset)
            except OSError:
                cursor_record = {"valid": False, "reason": "rollout_cursor_unreadable"}
            if not cursor_record.get("valid"):
                health = "stalled"
                reason = str(cursor_record.get("reason") or "projection_cursor_invalid")
            else:
                actual = int(cursor_record["ordinal"])
                if actual == projection_ordinal:
                    health = "pending"
                    reason = "projection_not_at_eof"
                elif actual < projection_ordinal:
                    health = "stalled"
                    reason = "projection_duplicate_or_rewind"
                else:
                    health = "stalled"
                    reason = "projection_ordinal_gap"
        rollout_integrity: dict[str, Any] | None = None
        if health == "healthy":
            if verify_rollout:
                current = chain["segments"][-1]
                rollout_integrity = {"valid": True, "size": current["size"],
                                     "sha256": current["sha256"]}
                if not rollout_integrity.get("valid"):
                    reason = str(
                        rollout_integrity.get("reason") or "rollout_integrity_unknown"
                    )
                    health = (
                        "stalled"
                        if reason in {"rollout_duplicate_ordinal", "rollout_ordinal_gap"}
                        else "unknown"
                    )
            else:
                health = "unchecked"
                reason = "rollout_integrity_not_checked"
        safe_to_fork = health == "healthy"
        unreadable = health == "unreadable"
    finally:
        history.close()
        state.close()
    candidates = (
        discover_readable_thread_candidates(paths, thread_id)
        if include_candidates and not safe_to_fork
        else []
    )
    return {
        "available": True,
        "thread_found": True,
        "thread_id": thread_id,
        "raw_nonempty": raw_nonempty,
        "rollout_size": rollout_size,
        "segment_id": segment_id,
        "projection_offset": projection_offset,
        "projection_ordinal": projection_ordinal,
        "projection_complete": projection_complete,
        "projected_turns": projected_turns,
        "projected_items": projected_items,
        "health": health,
        "reason": reason,
        "safe_to_fork": safe_to_fork,
        "cursor_record_ordinal": cursor_record.get("ordinal") if cursor_record else None,
        "cursor_record_type": cursor_record.get("record_type") if cursor_record else None,
        "cursor_payload_type": cursor_record.get("payload_type") if cursor_record else None,
        "rollout_integrity": rollout_integrity,
        "turn_ids": chain["turn_ids"] if chain is not None else None,
        "history_segment_count": len(chain["segments"]) if chain is not None else None,
        "error_segment": error_segment,
        "unreadable": unreadable,
        "candidates": candidates,
    }


def ensure_thread_history_readable(paths: Paths, thread_id: str) -> dict[str, Any] | None:
    """Fail closed unless a task's latest history projection is complete."""

    status = thread_history_projection_status(paths, thread_id, verify_rollout=True)
    if status is not None and not status.get("safe_to_fork"):
        raise UnreadableThreadHistoryError(
            thread_id,
            (candidate["id"] for candidate in status.get("candidates", [])),
            reason=str(status.get("reason") or "unsafe_projection"),
            segment_id=status.get("error_segment"),
        )
    return status


def _projected_turn_ids(paths: Paths, thread_id: str) -> list[str] | None:
    return read_projected_history(
        paths.codex_home, paths.state_database, paths.thread_history_database, thread_id,
    )["turn_ids"]


def thread_provider_binding(paths: Paths, thread_id: str) -> dict[str, Any]:
    """Read the persisted provider owner from Codex's task index."""

    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread id is required")
    if not paths.state_database.exists():
        raise FileNotFoundError("Codex task database is missing")
    connection = _readonly_sqlite(paths.state_database)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)")}
        required = {"id", "model_provider", "cwd"}
        if not required.issubset(columns):
            raise RuntimeError("Codex task database does not expose provider bindings")
        optional = [
            name
            for name in (
                "model",
                "name",
                "title",
                "archived",
                "is_pinned",
                "rollout_path",
                "created_at",
                "created_at_ms",
                "updated_at",
                "updated_at_ms",
                "recency_at",
                "recency_at_ms",
            )
            if name in columns
        ]
        fields = ["id", "model_provider", "cwd", *optional]
        row = connection.execute(
            f"SELECT {', '.join(fields)} FROM threads WHERE id = ?",
            (thread_id.strip(),),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown thread: {thread_id}")
        result = {name: row[name] for name in fields}
    finally:
        connection.close()
    provider_alias = str(result.pop("model_provider") or "")
    try:
        target = profile_for_provider_alias(paths, provider_alias)
    except ValueError:
        target = {
            "provider_alias": provider_alias,
            "profile_id": None,
            "label": "未知 Provider",
            "kind": "unknown",
        }
    result.update(
        {
            "thread_id": result.pop("id"),
            "provider_alias": provider_alias,
            "profile_id": target.get("profile_id"),
            "profile_label": target.get("label"),
            "provider_kind": target.get("kind"),
        }
    )
    return result


def _thread_session_payload(rollout_path: Any) -> dict[str, Any]:
    """Read only the first session metadata payload from a rollout."""

    if not isinstance(rollout_path, str) or not rollout_path.strip():
        return {}
    try:
        with Path(rollout_path).open("r", encoding="utf-8") as handle:
            first_line = handle.readline(1024 * 1024)
        record = json.loads(first_line)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return {}
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _thread_subagent_metadata(payload: dict[str, Any]) -> dict[str, str] | None:
    """Return bounded internal-subagent metadata from immutable session source."""

    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    spawn = subagent.get("thread_spawn")
    details = spawn if isinstance(spawn, dict) else subagent
    agent_path = str(details.get("agent_path") or "").strip()
    nickname = str(details.get("agent_nickname") or "").strip()
    path_label = agent_path.rstrip("/").rsplit("/", 1)[-1] if agent_path else ""
    return {
        "agent_path": agent_path,
        "agent_nickname": nickname,
        "label": path_label or nickname or "未命名",
    }


def codex_pinned_thread_order(paths: Paths) -> list[str]:
    """Read the desktop-owned pinned Codex task order without modifying it.

    The Electron atom is the UI order owner.  The older top-level list remains
    a compatibility fallback for desktop builds that have not published the
    atom yet.  A concurrently replaced or malformed file simply yields no pin
    overlay, so callers can fall back to recent-task order.
    """

    candidates = (paths.global_state, Path(f"{paths.global_state}.bak"))
    for candidate in candidates:
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        atom_state = document.get("electron-persisted-atom-state")
        preferred = (
            atom_state.get("app-server-pinned-thread-order-v1")
            if isinstance(atom_state, dict)
            else None
        )
        raw_ids = preferred if isinstance(preferred, list) else document.get("pinned-thread-ids")
        if not isinstance(raw_ids, list):
            continue
        ordered: list[str] = []
        seen: set[str] = set()
        for value in raw_ids[:500]:
            thread_id = str(value or "").strip()
            if thread_id and thread_id not in seen:
                ordered.append(thread_id)
                seen.add(thread_id)
        return ordered
    return []


def _thread_parent_id(rollout_path: Any) -> str | None:
    """Read only the session metadata record that owns fork lineage."""

    payload = _thread_session_payload(rollout_path)
    parent = payload.get("forked_from_id")
    return parent if isinstance(parent, str) and parent.strip() else None


def _history_base_matches_source(
    source: dict[str, Any],
    candidate: dict[str, Any],
) -> bool | None:
    """Compare a fork's immutable history cursor with the current source.

    ``None`` means an older rollout has no ``history_base`` metadata, in
    which case callers may use the legacy timestamp fallback.  A present but
    malformed cursor fails closed and must never fall back to mutable task
    metadata.
    """

    payload = _thread_session_payload(candidate.get("rollout_path"))
    if "history_base" not in payload:
        return None
    history_base = payload.get("history_base")
    if not isinstance(history_base, dict):
        return False
    end_offset = history_base.get("end_byte_offset")
    if isinstance(end_offset, bool) or not isinstance(end_offset, int) or end_offset < 0:
        return False
    source_id = str(source.get("thread_id") or "")
    rollout_path = source.get("rollout_path")
    if not isinstance(rollout_path, str) or not rollout_path.strip():
        return False
    try:
        source_path = Path(rollout_path)
        return bool(
            history_base.get("thread_id") == _rollout_segment_id(source_path, source_id)
            and source_path.stat().st_size == end_offset
        )
    except OSError:
        return False


def recent_thread_bindings(
    paths: Paths,
    *,
    limit: int = 30,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Return a bounded, read-only recent-task projection for the UI."""

    if not isinstance(limit, int) or not 1 <= limit <= MAX_THREAD_INVENTORY_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_THREAD_INVENTORY_ROWS}")
    if not paths.state_database.exists():
        return []
    pinned_order = codex_pinned_thread_order(paths)
    pinned_positions = {thread_id: index + 1 for index, thread_id in enumerate(pinned_order)}
    connection = _readonly_sqlite(paths.state_database)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)")}
        required = {"id", "model_provider", "cwd"}
        if not required.issubset(columns):
            return []
        wanted = [
            name
            for name in (
                "id",
                "model_provider",
                "cwd",
                "model",
                "name",
                "title",
                "archived",
                "is_pinned",
                "rollout_path",
                "created_at",
                "created_at_ms",
                "updated_at",
                "updated_at_ms",
                "recency_at",
                "recency_at_ms",
            )
            if name in columns
        ]
        order_column = next(
            (name for name in ("recency_at_ms", "updated_at_ms", "updated_at", "created_at_ms", "created_at") if name in columns),
            "id",
        )
        where = "" if include_archived or "archived" not in columns else " WHERE archived = 0"
        rows = list(connection.execute(
            f"SELECT {', '.join(wanted)} FROM threads{where} "
            f"ORDER BY {order_column} DESC LIMIT ?",
            (limit,),
        ).fetchall())
        seen_ids = {str(row["id"] or "") for row in rows}
        missing_pinned = [thread_id for thread_id in pinned_order if thread_id not in seen_ids]
        if missing_pinned:
            placeholders = ", ".join("?" for _ in missing_pinned)
            pin_where = f"id IN ({placeholders})"
            if not include_archived and "archived" in columns:
                pin_where += " AND archived = 0"
            rows.extend(
                connection.execute(
                    f"SELECT {', '.join(wanted)} FROM threads WHERE {pin_where}",
                    missing_pinned,
                ).fetchall()
            )
    finally:
        connection.close()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = {name: row[name] for name in wanted}
        provider_alias = str(item.pop("model_provider") or "")
        try:
            profile = profile_for_provider_alias(paths, provider_alias)
        except ValueError:
            profile = {
                "profile_id": None,
                "label": "未知 Provider",
                "kind": "unknown",
            }
        session_payload = _thread_session_payload(item.get("rollout_path"))
        subagent = _thread_subagent_metadata(session_payload)
        raw_display_name = item.get("name") or item.get("title")
        if subagent is not None:
            internal_name = str(raw_display_name or subagent["label"] or "未命名").strip()
            display_name = f"内部子任务：{internal_name}"
        else:
            display_name = raw_display_name or item.get("id")
        parent = session_payload.get("forked_from_id")
        parent_thread_id = parent if isinstance(parent, str) and parent.strip() else None
        thread_id = str(item.get("id") or "")
        item.update(
            {
                "thread_id": item.pop("id"),
                "display_name": str(display_name),
                "provider_alias": provider_alias,
                "profile_id": profile.get("profile_id"),
                "profile_label": profile.get("label"),
                "provider_kind": profile.get("kind"),
                "parent_thread_id": parent_thread_id,
                "is_subagent": subagent is not None,
                "subagent": subagent,
                "codex_pinned_index": pinned_positions.get(thread_id),
            }
        )
        results.append(item)
    return results


def task_list_bindings(
    paths: Paths,
    *,
    limit: int = 40,
    include_archived: bool = False,
    include_subagents: bool = False,
    sort_mode: str = THREAD_SORT_RECENT,
    bindings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return a UI-ready task list with explicit filtering and sort semantics."""

    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if sort_mode not in THREAD_SORT_MODES:
        raise ValueError(f"unsupported thread sort mode: {sort_mode}")
    rows = (
        [dict(row) for row in bindings]
        if bindings is not None
        else recent_thread_bindings(
            paths,
            limit=MAX_THREAD_INVENTORY_ROWS,
            include_archived=True,
        )
    )
    if not include_archived:
        rows = [row for row in rows if not bool(int(row.get("archived") or 0))]
    if not include_subagents:
        rows = [row for row in rows if not row.get("is_subagent")]
    if sort_mode == THREAD_SORT_CODEX:
        positions = {str(row.get("thread_id") or ""): index for index, row in enumerate(rows)}
        known_pins = len(codex_pinned_thread_order(paths))

        def codex_key(row: dict[str, Any]) -> tuple[int, int]:
            native_index = row.get("codex_pinned_index")
            if isinstance(native_index, int) and native_index > 0:
                return (0, native_index)
            if bool(int(row.get("is_pinned") or 0)):
                return (0, known_pins + positions.get(str(row.get("thread_id") or ""), len(rows)) + 1)
            return (1, positions.get(str(row.get("thread_id") or ""), len(rows)))

        rows = sorted(rows, key=codex_key)
    return rows[:limit]


def thread_family_bindings(
    paths: Paths,
    *,
    limit: int = 500,
    include_archived: bool = True,
    include_subagents: bool = True,
    sort_mode: str = THREAD_SORT_RECENT,
    query: str = "",
    bindings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Annotate task families from immutable fork lineage without new state.

    ``state_5.sqlite`` and rollout ``forked_from_id`` remain the identity
    owners.  Search is family-aware, while final row order follows the explicit
    user selection instead of forcing every ancestor between pinned heads.
    """

    if sort_mode not in THREAD_SORT_MODES:
        raise ValueError(f"unsupported thread sort mode: {sort_mode}")
    rows = (
        [dict(row) for row in bindings]
        if bindings is not None
        else recent_thread_bindings(
            paths,
            limit=MAX_THREAD_INVENTORY_ROWS,
            include_archived=True,
        )
    )
    if not include_subagents:
        rows = [row for row in rows if not row.get("is_subagent")]
    by_id = {str(row.get("thread_id") or ""): row for row in rows}
    positions = {str(row.get("thread_id") or ""): index for index, row in enumerate(rows)}

    def family_root(thread_id: str) -> tuple[str, int]:
        current = thread_id
        depth = 0
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            row = by_id.get(current)
            parent = str(row.get("parent_thread_id") or "") if row else ""
            if not parent:
                return current, depth
            depth += 1
            if parent not in by_id:
                return parent, depth
            current = parent
        # Corrupt/cyclic lineage is grouped deterministically and never loops.
        return min(seen) if seen else thread_id, depth

    groups: dict[str, list[dict[str, Any]]] = {}
    annotated: dict[str, dict[str, Any]] = {}
    for row in rows:
        thread_id = str(row.get("thread_id") or "")
        root, depth = family_root(thread_id)
        item = dict(row)
        item["family_id"] = root
        item["family_depth"] = depth
        annotated[thread_id] = item
        groups.setdefault(root, []).append(item)

    for family_id, members in groups.items():
        active_members = [member for member in members if not bool(int(member.get("archived") or 0))]
        head = min(
            active_members or members,
            key=lambda member: positions.get(str(member.get("thread_id") or ""), len(rows)),
        )
        head_id = str(head.get("thread_id") or "")
        ancestor_ids: set[str] = set()
        cursor = head_id
        while cursor and cursor not in ancestor_ids:
            ancestor_ids.add(cursor)
            cursor_row = annotated.get(cursor)
            cursor = str(cursor_row.get("parent_thread_id") or "") if cursor_row else ""
        for member in members:
            member_id = str(member.get("thread_id") or "")
            member["family_size"] = len(members)
            member["family_head_id"] = head_id
            member["family_role"] = (
                "head"
                if member_id == head_id
                else "ancestor"
                if member_id in ancestor_ids
                else "branch"
            )
    normalized_query = query.strip().casefold()
    selected_families: set[str] = set(groups)
    if normalized_query:
        selected_families = set()
        for family_id, members in groups.items():
            for member in members:
                haystack = "\n".join(
                    str(member.get(field) or "")
                    for field in (
                        "display_name",
                        "thread_id",
                        "provider_alias",
                        "profile_label",
                        "model",
                        "cwd",
                    )
                ).casefold()
                if normalized_query in haystack:
                    selected_families.add(family_id)
                    break

    result: list[dict[str, Any]] = []
    for row in rows:
        member = annotated.get(str(row.get("thread_id") or ""))
        if member is None or str(member.get("family_id") or "") not in selected_families:
            continue
        if include_archived or not bool(int(member.get("archived") or 0)):
            result.append(member)
    if sort_mode == THREAD_SORT_CODEX:
        known_pins = len(codex_pinned_thread_order(paths))

        def codex_key(member: dict[str, Any]) -> tuple[int, int]:
            thread_id = str(member.get("thread_id") or "")
            recent_position = positions.get(thread_id, len(rows))
            native_index = member.get("codex_pinned_index")
            active = not bool(int(member.get("archived") or 0))
            if active and isinstance(native_index, int) and native_index > 0:
                return (0, native_index)
            if active and bool(int(member.get("is_pinned") or 0)):
                return (0, known_pins + recent_position + 1)
            return (1, recent_position)

        result = sorted(result, key=codex_key)
    return result[:limit]


def reusable_target_thread(
    paths: Paths,
    source_thread_id: str,
    target_provider: str,
) -> dict[str, Any] | None:
    """Reuse a direct child only when the source has not changed since it was forked."""

    source = thread_provider_binding(paths, source_thread_id)
    if source.get("provider_alias") == target_provider and not int(source.get("archived") or 0):
        return source
    source_updated = source.get("updated_at_ms") or source.get("updated_at")
    for candidate in recent_thread_bindings(paths, limit=500, include_archived=True):
        if candidate.get("parent_thread_id") != source_thread_id:
            continue
        if candidate.get("provider_alias") != target_provider:
            continue
        if _thread_cwd_key(candidate.get("cwd")) != _thread_cwd_key(source.get("cwd")):
            continue
        cursor_matches = _history_base_matches_source(source, candidate)
        if cursor_matches is True:
            return candidate
        if cursor_matches is False:
            continue
        # Compatibility for older App Server rollouts that predate
        # ``history_base``.  Mutable timestamps are never consulted when an
        # immutable cursor is present.
        if not isinstance(source_updated, (int, float)):
            continue
        created = candidate.get("created_at_ms") or candidate.get("created_at")
        if isinstance(created, (int, float)) and source_updated <= created:
            return candidate
    return None


def thread_binding_matches_profile(
    paths: Paths,
    binding: dict[str, Any],
    profile_id: str,
    *,
    model: str | None = None,
) -> bool:
    """Compare a frozen task binding with the profile's current version."""

    profile = profile_by_id(load_profiles(paths), profile_id)
    alias = str(binding.get("provider_alias") or "")
    if profile.get("kind") == "official":
        return alias == "openai"
    try:
        version = profile_for_provider_alias(paths, alias)
    except (RuntimeError, ValueError):
        return False
    target_model = str(model or profile.get("model") or "").strip()
    return version.get("profile_id") == profile_id and (
        str(version.get("base_url") or "") == str(profile.get("base_url") or "")
        and str(version.get("key_ref") or "") == str(profile.get("key_ref") or "")
        and str(version.get("model") or "") == target_model
    )


def default_profiles() -> dict[str, Any]:
    return {
        "version": 1,
        "router": {"host": DEFAULT_ROUTER_HOST, "port": DEFAULT_ROUTER_PORT},
        "profiles": [
            {
                "id": "maylily",
                "label": "Maylily",
                "kind": "relay",
                "base_url": "https://maylily.xyz",
                "model": "gpt-5.6-sol",
                "models": ["gpt-5.6-sol"],
                "key_ref": "maylily-main",
                "enabled": True,
            },
            {
                "id": "relay-b",
                "label": "另一个中转站（待配置）",
                "kind": "relay",
                "base_url": "",
                "model": "",
                "models": [],
                "key_ref": "relay-b-main",
                "enabled": False,
            },
            {
                "id": "official",
                "label": "官方账号（由 Codex 登录管理）",
                "kind": "official",
                "base_url": "",
                "model": "",
                "key_ref": "",
                "enabled": True,
            },
        ],
    }


def load_profiles(paths: Paths) -> dict[str, Any]:
    return read_json(paths.profiles, default_profiles())


def load_active(paths: Paths) -> dict[str, Any]:
    return read_json(paths.active, {"profile_id": "maylily", "revision": 0, "changed_at": None})


def profile_by_id(document: dict[str, Any], profile_id: str) -> dict[str, Any]:
    for profile in document.get("profiles", []):
        if profile.get("id") == profile_id:
            return profile
    raise ValueError(f"unknown profile: {profile_id}")


def normalize_model_ids(values: Any, *, default_model: str = "") -> list[str]:
    """Normalize a bounded model allow-list without contacting a Provider."""

    raw_values: list[Any]
    if isinstance(values, str):
        raw_values = re.split(r"[,\r\n]+", values)
    elif isinstance(values, (list, tuple)):
        raw_values = list(values)
    elif values is None:
        raw_values = []
    else:
        raise ValueError("models must be a list or comma-separated string")
    if default_model:
        raw_values.insert(0, default_model)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        model = str(value or "").strip()
        if not model:
            continue
        if len(model) > 200 or any(character.isspace() for character in model):
            raise ValueError(f"invalid model id: {model[:40]}")
        if model not in seen:
            normalized.append(model)
            seen.add(model)
    if len(normalized) > 100:
        raise ValueError("a relay profile can contain at most 100 models")
    return normalized


def relay_model_ids(profile: dict[str, Any], *, required_model: str | None = None) -> list[str]:
    """Return one relay profile's configured models with its default first."""

    default_model = str(profile.get("model") or "").strip()
    models = normalize_model_ids(profile.get("models"), default_model=default_model)
    if required_model:
        models = normalize_model_ids(models, default_model=required_model.strip())
    return models


def load_provider_versions(paths: Paths) -> dict[str, Any]:
    """Load immutable relay snapshots used by persisted thread aliases."""

    document = read_json(paths.provider_versions, {"version": 1, "versions": []})
    if not isinstance(document, dict) or document.get("version") != 1:
        raise RuntimeError("unsupported provider version registry")
    versions = document.get("versions")
    if not isinstance(versions, list):
        raise RuntimeError("provider version registry is invalid")
    aliases: set[str] = set()
    hashes: set[tuple[str, str]] = set()
    for item in versions:
        if not isinstance(item, dict):
            raise RuntimeError("provider version registry contains an invalid entry")
        alias = str(item.get("provider_alias") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", alias) or alias == "openai":
            raise RuntimeError("provider version registry contains an invalid alias")
        if alias in aliases:
            raise RuntimeError(f"duplicate provider alias: {alias}")
        aliases.add(alias)
        snapshot = {
            "profile_id": str(item.get("profile_id") or ""),
            "base_url": str(item.get("base_url") or ""),
            "model": str(item.get("model") or ""),
            "key_ref": str(item.get("key_ref") or ""),
        }
        if item.get("kind") != "relay":
            raise RuntimeError("provider version registry contains a non-relay entry")
        try:
            _relay_snapshot({"id": snapshot["profile_id"], "kind": "relay", **snapshot})
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError("provider version registry contains an invalid snapshot") from exc
        config_hash = str(item.get("config_hash") or "")
        if config_hash != _relay_snapshot_hash(snapshot):
            raise RuntimeError(f"provider version integrity check failed: {alias}")
        identity = (snapshot["profile_id"], config_hash)
        if identity in hashes:
            raise RuntimeError("provider version registry contains a duplicate snapshot")
        hashes.add(identity)
        if item.get("version_id") != f"{snapshot['profile_id']}@{config_hash[:16]}":
            raise RuntimeError(f"provider version id mismatch: {alias}")
        expected_alias = (
            "custom"
            if alias == "custom" and snapshot["profile_id"] == "maylily"
            else f"{_provider_alias_base(snapshot['profile_id'])}_{config_hash[:12]}"
        )
        if alias != expected_alias:
            raise RuntimeError(f"provider version alias mismatch: {alias}")
    return document


def _relay_snapshot(profile: dict[str, Any]) -> dict[str, str]:
    profile_id = str(profile.get("id") or "")
    base_url = str(profile.get("base_url") or "").rstrip("/")
    model = str(profile.get("model") or "").strip()
    key_ref = str(profile.get("key_ref") or "")
    if profile.get("kind") != "relay":
        raise ValueError("only relay profiles have immutable provider versions")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", profile_id):
        raise ValueError("invalid profile id")
    if not base_url or not model or not key_ref:
        raise RuntimeError(f"relay profile is incomplete: {profile_id}")
    normalized_upstream(base_url, "/v1/responses")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", key_ref):
        raise ValueError("invalid key reference")
    return {
        "profile_id": profile_id,
        "base_url": base_url,
        "model": model,
        "key_ref": key_ref,
    }


def _relay_snapshot_hash(snapshot: dict[str, str]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _provider_alias_base(profile_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", profile_id).strip("_").lower()
    if not normalized:
        raise ValueError("profile id cannot produce a provider alias")
    return f"switchboard_{normalized}"


def provider_version_by_alias(document: dict[str, Any], provider_alias: str) -> dict[str, Any]:
    for version in document.get("versions", []):
        if isinstance(version, dict) and version.get("provider_alias") == provider_alias:
            return version
    raise ValueError(f"unknown provider alias: {provider_alias}")


def ensure_provider_version(
    paths: Paths,
    profile_id: str,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Publish or reuse one immutable relay snapshot.

    ``custom`` is permanently reserved for the first Maylily snapshot so
    existing Codex tasks keep their persisted provider identifier. Later
    snapshots receive content-addressed aliases and never rewrite old ones.
    """

    profile = profile_by_id(load_profiles(paths), profile_id)
    version_profile = dict(profile)
    if model is not None:
        selected_model = str(model).strip()
        if selected_model not in relay_model_ids(profile):
            raise ValueError(f"model is not configured for {profile_id}: {selected_model}")
        version_profile["model"] = selected_model
    snapshot = _relay_snapshot(version_profile)
    config_hash = _relay_snapshot_hash(snapshot)
    document = load_provider_versions(paths)
    for version in document["versions"]:
        if version.get("config_hash") == config_hash and version.get("profile_id") == profile_id:
            return version

    used_aliases = {str(item.get("provider_alias") or "") for item in document["versions"]}
    if profile_id == "maylily" and "custom" not in used_aliases:
        provider_alias = "custom"
    else:
        base = _provider_alias_base(profile_id)
        provider_alias = f"{base}_{config_hash[:12]}"
        if provider_alias in used_aliases:
            raise RuntimeError(f"provider alias collision: {provider_alias}")
    version = {
        "version_id": f"{profile_id}@{config_hash[:16]}",
        "provider_alias": provider_alias,
        "profile_id": profile_id,
        "label": str(profile.get("label") or profile_id),
        "kind": "relay",
        **snapshot,
        "config_hash": config_hash,
        "created_at": utc_now(),
    }
    document["versions"].append(version)
    atomic_write_json(paths.provider_versions, document)
    return version


def profile_for_provider_alias(paths: Paths, provider_alias: str) -> dict[str, Any]:
    if provider_alias == "openai":
        profile = profile_by_id(load_profiles(paths), "official")
        return {
            "provider_alias": "openai",
            "profile_id": "official",
            "label": str(profile.get("label") or "official"),
            "kind": "official",
            "model": "",
        }
    return provider_version_by_alias(load_provider_versions(paths), provider_alias)


def model_catalog_path(paths: Paths, profile_id: str) -> Path:
    """Return the E-drive catalog projection for one relay profile."""

    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", profile_id):
        raise ValueError("invalid profile id")
    return paths.model_catalogs / f"{profile_id}.json"


def _catalog_model_is_complete(item: Any, target_model: str) -> bool:
    """Return whether one exported ModelInfo is standalone-loadable today."""

    if not isinstance(item, dict) or str(item.get("slug") or "") != target_model:
        return False
    base_instructions = item.get("base_instructions")
    return (
        isinstance(base_instructions, str)
        and bool(base_instructions.strip())
        and item.get("supported_in_api") is not False
    )


def model_catalog_status(
    paths: Paths,
    profile_id: str,
    *,
    model: str | None = None,
    models: Any = None,
) -> dict[str, Any]:
    """Read a secret-free status for a generated model catalog."""

    document = load_profiles(paths)
    profile = profile_by_id(document, profile_id)
    target_model = str(model or profile.get("model") or "").strip()
    target_models = (
        normalize_model_ids(models, default_model=target_model)
        if models is not None
        else relay_model_ids(profile, required_model=target_model or None)
    )
    path = model_catalog_path(paths, profile_id)
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "ready": False,
        "model": target_model,
        "models": target_models,
        "model_count": len(target_models),
        "ready_models": [],
        "missing_models": list(target_models),
    }
    if not path.exists():
        result["state"] = "missing"
        return result
    try:
        catalog = read_json(path, {})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result["state"] = "invalid"
        result["error"] = str(exc)[:240]
        return result
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        result["state"] = "invalid"
        return result
    by_slug = {
        str(item.get("slug") or ""): item
        for item in models
        if isinstance(item, dict) and str(item.get("slug") or "")
    }
    ready_models = [
        model_id
        for model_id in target_models
        if _catalog_model_is_complete(by_slug.get(model_id), model_id)
    ]
    missing_models = [model_id for model_id in target_models if model_id not in ready_models]
    result["ready_models"] = ready_models
    result["missing_models"] = missing_models
    result["ready"] = bool(target_models and not missing_models)
    result["state"] = "ready" if result["ready"] else "stale"
    if missing_models:
        result["error"] = "catalog models are missing or incomplete: " + ", ".join(missing_models[:8])
    return result


def load_bundled_model_catalog(
    paths: Paths,
    *,
    executable: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Load the catalog exported by the exact local Codex runtime.

    ``debug models --bundled`` is local-only and deliberately skips remote
    refresh. Its output includes fields that ``models_cache.json`` omits and
    that a standalone ``model_catalog_json`` must carry.
    """

    candidate = resolve_appserver_executable(executable, paths=paths)
    environment = os.environ.copy()
    environment["CODEX_HOME"] = os.fspath(paths.codex_home)
    environment["TEMP"] = os.fspath(paths.runtime_tmp)
    environment["TMP"] = os.fspath(paths.runtime_tmp)
    paths.runtime_tmp.mkdir(parents=True, exist_ok=True)
    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": environment,
        "timeout": DEFAULT_APP_SERVER_TIMEOUT,
        "check": False,
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            [candidate, "debug", "models", "--bundled"],
            **run_kwargs,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"failed to export bundled Codex model catalog: {candidate}") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            "failed to export bundled Codex model catalog: "
            f"{candidate} (exit {completed.returncode})"
        )
    try:
        catalog = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Codex returned an invalid bundled model catalog: {candidate}") from exc
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise RuntimeError(f"Codex returned an invalid bundled model catalog: {candidate}")

    version = ""
    try:
        version_result = subprocess.run(
            [candidate, "--version"],
            **{**run_kwargs, "timeout": 10.0},
        )
        if version_result.returncode == 0:
            version = version_result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "catalog": catalog,
        "executable": candidate,
        "client_version": version,
    }


def bundled_model_choices(
    paths: Paths,
    *,
    executable: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """List locally complete Codex models without refreshing any Provider."""

    exported = load_bundled_model_catalog(paths, executable=executable)
    models = [
        str(item.get("slug") or "")
        for item in exported["catalog"].get("models", [])
        if isinstance(item, dict)
        and _catalog_model_is_complete(item, str(item.get("slug") or ""))
    ]
    return {
        "models": normalize_model_ids(models),
        "model_count": len(models),
        "client_version": exported.get("client_version"),
        "source": "bundled",
    }


def prepare_model_catalog(
    paths: Paths,
    profile_id: str,
    *,
    model: str | None = None,
    models: Any = None,
    executable: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Generate a standalone catalog from the exact local Codex runtime.

    Maylily and other OpenAI-compatible relays expose the standard
    ``/v1/models`` shape, while Codex's custom-provider catalog expects its
    complete internal ``ModelInfo`` objects. Exporting the bundled catalog
    avoids a provider request and prevents an account-specific cache projection
    from being mistaken for a standalone catalog.
    """

    document = load_profiles(paths)
    profile = profile_by_id(document, profile_id)
    if profile.get("kind") != "relay":
        raise ValueError("model catalogs are only used by relay profiles")
    target_model = str(model or profile.get("model") or "").strip()
    target_models = (
        normalize_model_ids(models, default_model=target_model)
        if models is not None
        else relay_model_ids(profile, required_model=target_model or None)
    )
    if not target_models:
        raise ValueError("at least one model is required")
    exported = load_bundled_model_catalog(paths, executable=executable)
    catalog = exported["catalog"]
    models = catalog["models"]
    by_slug = {
        str(item.get("slug") or ""): item
        for item in models
        if isinstance(item, dict) and str(item.get("slug") or "")
    }
    selected_models: list[dict[str, Any]] = []
    for model_id in target_models:
        selected = by_slug.get(model_id)
        if selected is None:
            raise RuntimeError(
                f"bundled Codex model catalog does not contain {model_id}; "
                "update the desktop Codex runtime or remove it from the relay model list"
            )
        if selected.get("supported_in_api") is False:
            raise RuntimeError(f"bundled Codex model catalog marks {model_id} as unavailable")
        if not _catalog_model_is_complete(selected, model_id):
            raise RuntimeError(
                f"bundled Codex model catalog entry for {model_id} is incomplete"
            )
        selected_models.append(selected)
    catalog_path = model_catalog_path(paths, profile_id)
    atomic_write_json(catalog_path, {"models": selected_models})
    return {
        "path": str(catalog_path),
        "profile_id": profile_id,
        "model": target_model,
        "models": target_models,
        "model_count": len(target_models),
        "client_version": exported["client_version"],
        "executable": exported["executable"],
        "source": "bundled",
        "ready": True,
    }


def _prepare_model_catalog_for_runtime(
    paths: Paths,
    profile_id: str,
    *,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Prepare a catalog when a real local Codex runtime is available.

    Synthetic test homes may not have a Codex runtime. The explicit
    ``prepare-model-catalog`` command remains strict; production E-drive use
    exports from the installed runtime while isolated tests can defer it.
    """

    status = model_catalog_status(paths, profile_id, model=model)
    if status.get("ready"):
        return status
    if paths.codex_home.resolve() == DEFAULT_CODEX_HOME.resolve():
        return prepare_model_catalog(paths, profile_id, model=model)
    return None


def configure_relay_profile(
    paths: Paths,
    profile_id: str,
    *,
    base_url: str,
    model: str,
    models: Any = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Persist non-secret relay metadata; keys remain in DPAPI storage."""

    document = load_profiles(paths)
    profile = profile_by_id(document, profile_id)
    if profile.get("kind") != "relay":
        raise ValueError("only relay profiles can be configured here")
    normalized_upstream(base_url, "/v1/responses")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model is required")
    configured_models = normalize_model_ids(
        profile.get("models") if models is None else models,
        default_model=model.strip(),
    )
    profile["base_url"] = base_url.rstrip("/")
    profile["model"] = model.strip()
    profile["models"] = configured_models
    profile["enabled"] = bool(enabled)
    atomic_write_json(paths.profiles, document)
    return {"profile": profile, "key_configured": bool(profile.get("key_ref") and key_path(paths, str(profile["key_ref"])).exists())}


def _remote_model_ids(payload: Any) -> list[str]:
    """Extract a bounded OpenAI-compatible model list from an untrusted body."""

    if not isinstance(payload, dict):
        raise RuntimeError("relay model detection returned an invalid JSON object")
    raw_models = payload.get("data")
    if not isinstance(raw_models, list):
        raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise RuntimeError("relay model detection response has no model list")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_models[:1000]:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("id") or item.get("slug") or ""
        else:
            continue
        model_id = str(value).strip()
        if (
            not model_id
            or len(model_id) > 200
            or any(character.isspace() for character in model_id)
            or model_id in seen
        ):
            continue
        result.append(model_id)
        seen.add(model_id)
        if len(result) >= 500:
            break
    if not result:
        raise RuntimeError("relay model detection returned an empty model list")
    return result


def thread_family_cleanup_plan(
    paths: Paths,
    thread_id: str,
    *,
    limit: int = 500,
) -> dict[str, Any]:
    """Plan a recoverable family cleanup while keeping the derived head."""

    normalized_id = str(thread_id or "").strip()
    if not normalized_id:
        raise ValueError("thread_id is required")
    rows = thread_family_bindings(
        paths,
        limit=limit,
        include_archived=True,
        include_subagents=False,
    )
    selected = next(
        (row for row in rows if str(row.get("thread_id") or "") == normalized_id),
        None,
    )
    if selected is None:
        raise ValueError(f"thread not found in recent family projection: {normalized_id}")
    family_id = str(selected.get("family_id") or normalized_id)
    head_id = str(selected.get("family_head_id") or normalized_id)
    if normalized_id != head_id or bool(int(selected.get("archived") or 0)):
        raise ValueError("select the active current family head before cleanup")
    members = [row for row in rows if str(row.get("family_id") or "") == family_id]
    if not any(str(row.get("thread_id") or "") == head_id for row in members):
        raise RuntimeError("task family head is outside the bounded projection")
    active_member_ids = [
        str(row.get("thread_id") or "")
        for row in members
        if not bool(int(row.get("archived") or 0))
    ]
    candidate_ids = [thread for thread in active_member_ids if thread != head_id]
    return {
        "operation": "family_cleanup_plan",
        "selected_thread_id": normalized_id,
        "family_id": family_id,
        "family_size": len(members),
        "head_thread_id": head_id,
        "active_member_ids": active_member_ids,
        "candidate_thread_ids": candidate_ids,
        "candidate_count": len(candidate_ids),
        "permanent_delete": False,
    }


def archive_thread_family(
    paths: Paths,
    thread_id: str,
    *,
    executable: str | None = None,
    blocker_probe: Any | None = None,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    """Archive active non-head family members through the App Server owner."""

    with conversion_operation_lock(paths):
        probe = blocker_probe or appserver_blocking_processes
        blockers = probe(paths)
        if blockers:
            raise RuntimeError(
                "family cleanup requires the Codex desktop/App Server to be closed; "
                f"active_processes={len(blockers)}"
            )
        plan = thread_family_cleanup_plan(paths, thread_id)
        candidate_ids = list(plan["candidate_thread_ids"])
        head_id = str(plan["head_thread_id"])
        if not candidate_ids:
            return {
                "operation": "family_cleanup",
                "plan": plan,
                "changed": False,
                "archived_thread_ids": [],
                "head_thread_id": head_id,
                "complete": True,
                "warning": None,
            }
        client = client_factory() if client_factory is not None else _appserver_client(paths, executable)
        errors: list[str] = []
        try:
            for candidate_id in candidate_ids:
                try:
                    if hasattr(client, "set_thread_pinned"):
                        client.set_thread_pinned(candidate_id, False)
                except Exception as exc:
                    errors.append(f"取消置顶 {candidate_id[-8:]}：{str(exc)[:120]}")
                try:
                    client.archive_thread(candidate_id)
                except Exception as exc:
                    errors.append(f"归档 {candidate_id[-8:]}：{str(exc)[:120]}")
            # Archiving an ancestor can cascade to its descendants.  Restore
            # and pin the one derived head last so the family always keeps a
            # visible continuation.
            try:
                client.unarchive_thread(head_id)
            except Exception as exc:
                errors.append(f"恢复 head {head_id[-8:]}：{str(exc)[:120]}")
            try:
                if hasattr(client, "set_thread_pinned"):
                    client.set_thread_pinned(head_id, True)
            except Exception as exc:
                errors.append(f"置顶 head {head_id[-8:]}：{str(exc)[:120]}")
        finally:
            client.close()

        archived_ids: list[str] = []
        active_ids: list[str] = []
        for candidate_id in candidate_ids:
            binding = thread_provider_binding(paths, candidate_id)
            if bool(int(binding.get("archived") or 0)):
                archived_ids.append(candidate_id)
            else:
                active_ids.append(candidate_id)
        head = thread_provider_binding(paths, head_id)
        head_active = not bool(int(head.get("archived") or 0))
        head_pinned = bool(int(head.get("is_pinned") or 0))
        complete = not active_ids and head_active and head_pinned
        if active_ids:
            errors.append("仍活跃的旧成员：" + "、".join(item[-8:] for item in active_ids[:8]))
        if not head_active:
            errors.append("家族 head 仍在归档区")
        if not head_pinned:
            errors.append("家族 head 未进入置顶区")
        return {
            "operation": "family_cleanup",
            "plan": plan,
            "changed": bool(archived_ids),
            "archived_thread_ids": archived_ids,
            "head_thread_id": head_id,
            "head_active": head_active,
            "head_pinned": head_pinned,
            "complete": complete,
            "warning": "；".join(errors[:12]) or None,
        }


def probe_relay_models(
    paths: Paths,
    profile_id: str,
    *,
    timeout: float = 10.0,
    connection_factory: Any | None = None,
    key_loader: Any | None = None,
) -> dict[str, Any]:
    """Manually query one relay's model endpoint without sending inference.

    The call is deliberately single-shot: an unknown network result is never
    retried.  The decrypted key remains inside this provider-layer function
    and neither the response nor the return value contains it.
    """

    if timeout <= 0 or timeout > 60:
        raise ValueError("relay model detection timeout must be between 0 and 60 seconds")
    profile = profile_by_id(load_profiles(paths), profile_id)
    if profile.get("kind") != "relay":
        raise ValueError("model detection is only available for relay profiles")
    if not profile.get("enabled", False):
        raise RuntimeError(f"profile is disabled: {profile_id}")
    base_url = str(profile.get("base_url") or "").strip()
    key_ref = str(profile.get("key_ref") or "").strip()
    if not base_url or not key_ref or not key_path(paths, key_ref).exists():
        raise RuntimeError("relay address or encrypted key is not configured")
    host, request_path, port, use_tls = normalized_upstream(base_url, "/v1/models")
    loader = key_loader or load_key
    secret = loader(paths, key_ref)
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("relay key could not be loaded")
    connection: Any | None = None
    try:
        if connection_factory is None:
            connection = (
                http.client.HTTPSConnection(host, port, timeout=timeout)
                if use_tls
                else http.client.HTTPConnection(host, port, timeout=timeout)
            )
        else:
            connection = connection_factory(host, port, use_tls, timeout)
        connection.request(
            "GET",
            request_path,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {secret}",
                "User-Agent": "Codex-Switchboard/0.7",
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_MODEL_DISCOVERY_BYTES + 1)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise RuntimeError("relay model detection failed; no inference request was sent") from exc
    finally:
        secret = ""
        if connection is not None:
            connection.close()
    if len(body) > MAX_MODEL_DISCOVERY_BYTES:
        raise RuntimeError("relay model detection response is too large")
    status = int(getattr(response, "status", 0) or 0)
    if not 200 <= status < 300:
        raise RuntimeError(f"relay model detection returned HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("relay model detection returned invalid JSON") from exc
    remote_models = _remote_model_ids(payload)
    configured = relay_model_ids(profile)
    remote_set = set(remote_models)
    configured_set = set(configured)
    return {
        "operation": "model_probe",
        "profile_id": profile_id,
        "profile_label": str(profile.get("label") or profile_id),
        "checked_at": utc_now(),
        "request": "GET /v1/models",
        "inference_sent": False,
        "remote_models": remote_models,
        "remote_count": len(remote_models),
        "configured_models": configured,
        "supported_configured": [model for model in configured if model in remote_set],
        "missing_remote": [model for model in configured if model not in remote_set],
        "available_not_configured": [model for model in remote_models if model not in configured_set],
    }


def adopt_legacy_bearer_key(paths: Paths, profile_id: str | None = None) -> dict[str, Any]:
    """Move a legacy plaintext bearer token into DPAPI and remove it atomically.

    This is intentionally one-way from the active config: the token is never
    printed, copied to a backup, or returned.  If validation or encrypted
    storage fails, config.toml is left untouched.
    """

    if tomllib is None or not paths.config.exists():
        raise RuntimeError("config.toml is missing or TOML parsing is unavailable")
    raw = paths.config.read_text(encoding="utf-8")
    parsed = tomllib.loads(raw)
    custom = parsed.get("model_providers", {}).get("custom", {})
    token = custom.get("experimental_bearer_token")
    if not isinstance(token, str) or not token.strip():
        return {"status": "not-found", "config_changed": False}
    document = load_profiles(paths)
    active = load_active(paths)
    selected_id = profile_id or str(active.get("profile_id") or "maylily")
    profile = profile_by_id(document, selected_id)
    if profile.get("kind") != "relay":
        raise RuntimeError("legacy bearer token can only be adopted for a relay profile")
    key_ref = str(profile.get("key_ref") or "")
    if not key_ref:
        raise RuntimeError("relay profile has no key reference")
    target = key_path(paths, key_ref)
    if target.exists():
        raise FileExistsError(f"encrypted key already exists: {key_ref}")
    # Encrypt first. If this fails, the plaintext config remains usable and is
    # not partially modified.
    store_key(paths, key_ref, token)
    try:
        router = document.get("router", {})
        config_result = update_config_provider(
            paths.config,
            selected_id,
            model=str(profile.get("model") or "") or None,
            router_host=str(router.get("host", DEFAULT_ROUTER_HOST)),
            router_port=int(router.get("port", DEFAULT_ROUTER_PORT)),
        )
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return {
        "status": "adopted",
        "profile_id": selected_id,
        "key_ref": key_ref,
        "config": config_result,
        "config_changed": bool(config_result.get("changed")),
    }


def key_path(paths: Paths, key_ref: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", key_ref):
        raise ValueError("invalid key reference")
    return paths.keys / f"{key_ref}.dpapi"


def router_required(paths: Paths) -> bool:
    """Return whether a frozen relay route has a locally stored credential.

    This is a local readiness check only.  It never decrypts a key or contacts
    a Provider.  A published Provider version remains authoritative for old
    relay tasks even when the current new-task default is official.
    """

    try:
        versions = load_provider_versions(paths).get("versions", [])
    except (OSError, RuntimeError, ValueError):
        return False
    if not isinstance(versions, list):
        return False
    for version in versions:
        if not isinstance(version, dict):
            continue
        key_ref = str(version.get("key_ref") or "").strip()
        if not key_ref:
            continue
        try:
            if key_path(paths, key_ref).is_file():
                return True
        except ValueError:
            continue
    return False


def store_key(paths: Paths, key_ref: str, secret: str) -> None:
    if not secret.strip():
        raise ValueError("empty key is not allowed")
    paths.keys.mkdir(parents=True, exist_ok=True)
    encrypted = dpapi_protect(secret.encode("utf-8"))
    target = key_path(paths, key_ref)
    tmp = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_bytes(base64.b64encode(encrypted))
    os.replace(tmp, target)


def load_key(paths: Paths, key_ref: str) -> str:
    target = key_path(paths, key_ref)
    if not target.exists():
        raise FileNotFoundError(f"key is not configured: {key_ref}")
    encrypted = base64.b64decode(target.read_bytes())
    return dpapi_unprotect(encrypted).decode("utf-8")


def writer_locks(
    paths: Paths,
    *,
    probe: Any | None = None,
    now: float | None = None,
    stale_after_seconds: float = WRITER_LOCK_STALE_SECONDS,
    include_zero_byte: bool = False,
) -> list[Path]:
    """Return likely-live writer locks without treating old debris as active.

    The current Codex lock files are intentionally empty, so callers that need
    exact liveness can inject ``probe(path)`` (for example, an app-server
    status query).  The default is deliberately conservative for maintenance:
    zero-byte locks and files older than the stale window are ignored.  The
    migration path separately blocks the desktop/app-server processes, which
    prevents a live process from racing a filesystem migration.
    """
    lock_dir = paths.codex_home / "thread-writer-locks"
    if not lock_dir.exists():
        return []
    current = time.time() if now is None else now
    active: list[Path] = []
    for lock in sorted(lock_dir.glob("*.lock")):
        try:
            stat = lock.stat()
        except OSError:
            continue
        if probe is not None:
            try:
                if bool(probe(lock)):
                    active.append(lock)
            except Exception:
                # A failed liveness probe must fail closed.
                active.append(lock)
            continue
        if not include_zero_byte and stat.st_size == 0:
            continue
        if current - stat.st_mtime > stale_after_seconds:
            continue
        active.append(lock)
    return active


def _pid_is_alive(pid: int) -> bool:
    """Fail closed when checking whether a conversion-lock owner is alive."""

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied means the process may still exist; only an invalid
            # PID is safe to classify as dead.
            return int(kernel32.GetLastError()) not in {87, 1168}
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def conversion_mutex_name(paths: Paths) -> str:
    """Return a stable, path-free mutex name for one authoritative home."""

    normalized = _thread_cwd_key(paths.codex_home).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"Local\\CodexSwitchboardConversion-{digest}"


@contextmanager
def _windows_conversion_operation_lock(paths: Paths):
    """Use a kernel-owned mutex so a crashed process releases immediately."""

    name = conversion_mutex_name(paths)
    with _LOCAL_CONVERSION_MUTEX_GUARD:
        if name in _LOCAL_CONVERSION_MUTEXES:
            raise RuntimeError("another Switchboard task conversion is already in progress")
        _LOCAL_CONVERSION_MUTEXES.add(name)
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateMutexW(None, False, name)
    acquired = False
    try:
        if not handle:
            raise ctypes.WinError()
        wait_result = int(kernel32.WaitForSingleObject(handle, 0))
        if wait_result == 0x102:  # WAIT_TIMEOUT
            raise RuntimeError("another Switchboard task conversion is already in progress")
        if wait_result not in {0x00000000, 0x00000080}:  # WAIT_OBJECT_0 / WAIT_ABANDONED
            raise ctypes.WinError()
        acquired = True
        yield
    finally:
        if handle:
            if acquired:
                kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        with _LOCAL_CONVERSION_MUTEX_GUARD:
            _LOCAL_CONVERSION_MUTEXES.discard(name)


@contextmanager
def _file_conversion_operation_lock(
    paths: Paths,
    *,
    stale_after_seconds: float = CONVERSION_LOCK_STALE_SECONDS,
):
    """Portable atomic-file fallback for non-Windows environments.

    The lock file is diagnostic state owned only by the process that created
    its random token.  A stale file is removed only after its PID is confirmed
    dead and its age exceeds the bounded stale window.
    """

    paths.switchboard.mkdir(parents=True, exist_ok=True)
    lock_path = paths.conversion_lock
    token = uuid.uuid4().hex
    descriptor: int | None = None
    for _attempt in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            break
        except FileExistsError:
            try:
                existing = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                existing = {}
            try:
                age = max(0.0, time.time() - lock_path.stat().st_mtime)
            except OSError:
                continue
            owner_pid = existing.get("pid")
            owner_alive = (
                isinstance(owner_pid, int)
                and not isinstance(owner_pid, bool)
                and _pid_is_alive(owner_pid)
            )
            if owner_alive or age <= stale_after_seconds:
                raise RuntimeError("another Switchboard task conversion is already in progress")
            try:
                lock_path.unlink()
            except FileNotFoundError:
                continue
    if descriptor is None:
        raise RuntimeError("unable to acquire the Switchboard task conversion lock")
    payload = {
        "pid": os.getpid(),
        "token": token,
        "created_at": utc_now(),
    }
    try:
        encoded = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    try:
        yield
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            current = {}
        if current.get("token") == token:
            lock_path.unlink(missing_ok=True)


@contextmanager
def conversion_operation_lock(
    paths: Paths,
    *,
    stale_after_seconds: float = CONVERSION_LOCK_STALE_SECONDS,
):
    """Serialize task conversions, auto-releasing on Windows process exit."""

    if os.name == "nt":
        with _windows_conversion_operation_lock(paths):
            yield
        return
    with _file_conversion_operation_lock(
        paths,
        stale_after_seconds=stale_after_seconds,
    ):
        yield


def process_lines(*, strict: bool = False) -> list[str]:
    command = (
        "Get-CimInstance Win32_Process | "
        "Select-Object Name,ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(result.stdout or "[]")
        if isinstance(payload, dict):
            payload = [payload]
        return [json.dumps(item, ensure_ascii=False) for item in payload]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        if strict:
            raise RuntimeError("无法可靠读取本机进程，尚不能确认 Codex 已退出") from exc
        return []


def blocking_processes(paths: Paths) -> list[str]:
    # A Codex desktop process does not necessarily include CODEX_HOME in its
    # command line.  Name-only detection is intentional here: migration must
    # never race the desktop app, even when it currently has no active turn.
    app_names = {"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"}
    needles = [
        os.path.normcase(os.path.abspath(os.fspath(source))).lower()
        for _label, source, _destination, _junction in migration_targets()
    ]
    blocked: list[str] = []
    ignored_pids = {os.getpid(), os.getppid()}
    for line in process_lines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            item = {}
        name = str(item.get("Name") or "").lower()
        searchable = " ".join(
            str(item.get(field) or "")
            for field in ("Name", "ExecutablePath", "CommandLine")
        ).lower()
        try:
            pid = int(item.get("ProcessId"))
        except (TypeError, ValueError):
            pid = -1
        if pid in ignored_pids:
            continue
        if name in app_names or any(needle in searchable for needle in needles):
            blocked.append(line)
    return blocked


def appserver_blocking_processes(_paths: Paths | None = None, *,
                                strict: bool = False,
                                ignored_tree_root_pid: int | None = None) -> list[str]:
    """Return only processes that can own the live Codex thread database.

    ``blocking_processes`` is intentionally broader because filesystem
    migration must also stop project workers and anything running from a
    source tree that may be moved.  A thread/provider copy has a narrower
    invariant: no Codex desktop, CLI/app-server, or code-mode host may access
    the same state while the constrained helper forks the original thread.
    Keeping this probe separate prevents the switchboard UI and local relay
    router from deadlocking their own switch operation merely because their
    scripts live under ``Documents/Codex``.
    """

    app_names = {"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"}
    blocked: list[str] = []
    ignored_pids = {os.getpid(), os.getppid()}
    lines = process_lines(strict=True) if strict else process_lines()
    if strict and not lines:
        raise RuntimeError("进程检查未返回可信结果，尚不能确认 Codex 已退出")
    if ignored_tree_root_pid is not None:
        owned_pids = {ignored_tree_root_pid}
        rows = [json.loads(line) for line in lines]
        changed = True
        while changed:
            changed = False
            for row in rows:
                if row.get("ParentProcessId") in owned_pids and row.get("ProcessId") not in owned_pids:
                    owned_pids.add(row.get("ProcessId"))
                    changed = True
        ignored_pids.update(owned_pids)
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            item = {}
        name = str(item.get("Name") or "").lower()
        try:
            pid = int(item.get("ProcessId"))
        except (TypeError, ValueError):
            pid = -1
        if pid in ignored_pids:
            continue
        if name in app_names:
            blocked.append(line)
    return blocked


def wait_for_appserver_exit(
    paths: Paths,
    *,
    process_probe: Any | None = None,
    poll_seconds: float = 1.0,
    stable_empty_checks: int = 2,
    timeout_seconds: float | None = 30 * 60,
    sleep_fn: Any = time.sleep,
    monotonic_fn: Any = time.monotonic,
    cancel_probe: Any | None = None,
    progress_fn: Any | None = None,
) -> dict[str, Any]:
    """Wait until Codex/App Server is stably closed before a task copy."""

    if poll_seconds < 0:
        raise ValueError("poll_seconds must be non-negative")
    if stable_empty_checks < 1:
        raise ValueError("stable_empty_checks must be positive")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive or None")
    probe = process_probe or appserver_blocking_processes
    started = monotonic_fn()
    empty_checks = 0
    last_reported: tuple[int, int] | None = None
    while True:
        if cancel_probe is not None and cancel_probe():
            raise RuntimeError("thread copy wait was cancelled before publication")
        blockers = probe(paths)
        if blockers:
            empty_checks = 0
        else:
            empty_checks += 1
        elapsed = max(0.0, monotonic_fn() - started)
        report_key = (len(blockers), empty_checks)
        if progress_fn is not None and report_key != last_reported:
            progress_fn(
                {
                    "blockers": len(blockers),
                    "stable_empty_checks": empty_checks,
                    "required_empty_checks": stable_empty_checks,
                    "elapsed_seconds": elapsed,
                }
            )
            last_reported = report_key
        if not blockers and empty_checks >= stable_empty_checks:
            return {
                "status": "ready",
                "elapsed_seconds": elapsed,
                "stable_empty_checks": empty_checks,
            }
        if timeout_seconds is not None and elapsed >= timeout_seconds:
            raise TimeoutError(
                "timed out waiting for the Codex desktop/App Server to close; "
                f"active_processes={len(blockers)}"
            )
        sleep_fn(poll_seconds)


def backup_file(paths: Paths, source: Path, label: str) -> Path:
    if not source.exists():
        raise FileNotFoundError(source)
    paths.backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = paths.backups / f"{label}-{stamp}{source.suffix}"
    shutil.copy2(source, target)
    return target


def backup_sqlite(paths: Paths, source: Path, label: str) -> Path:
    """Create a consistent SQLite backup using the SQLite backup API.

    Copying a live ``.sqlite`` byte-for-byte can miss pages committed to its
    WAL.  The backup API reads a consistent snapshot and includes those pages
    without touching the source database.  The destination is verified with
    ``quick_check`` before it is reported to the caller.
    """
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    paths.backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = paths.backups / f"{label}-{stamp}.sqlite"
    if target.exists():
        raise FileExistsError(target)
    source_uri = "file:" + source.as_posix() + "?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True, timeout=30)
    target_conn = sqlite3.connect(target, timeout=30)
    try:
        source_conn.backup(target_conn, pages=1000, sleep=0.05)
        target_conn.commit()
        check = target_conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"SQLite backup quick_check failed: {check}")
    except Exception:
        target_conn.close()
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        source_conn.close()
        target_conn.close()
    return target


def backup_active_databases(paths: Paths) -> list[Path]:
    """Back up the two mutable Codex databases before a filesystem migration."""
    backups: list[Path] = []
    for name in ("state_5.sqlite", "logs_2.sqlite"):
        source = paths.codex_home / name
        if source.exists():
            backups.append(backup_sqlite(paths, source, f"before-migration-{name[:-7]}"))
    return backups


def snapshot_active_databases(paths: Paths) -> list[Path]:
    """Compatibility alias used by callers that describe this as a snapshot."""
    return backup_active_databases(paths)


def _backup_series_name(name: str) -> str:
    match = re.fullmatch(
        r"(?P<prefix>.+?)-\d{8}-\d{6}(?:-\d{6})?(?P<suffix>\.[^.]+)?",
        name,
    )
    if match is None:
        return name
    return f"{match.group('prefix')}{match.group('suffix') or ''}"


def backup_retention_plan(
    paths: Paths,
    *,
    keep_per_series: int = DEFAULT_BACKUP_KEEP_PER_SERIES,
    min_age_days: float = DEFAULT_BACKUP_MIN_AGE_DAYS,
    now_timestamp: float | None = None,
    max_entries: int = 500,
) -> dict[str, Any]:
    """Build a read-only backup retention preview across known E-drive roots.

    This function never deletes, moves, compresses, or opens a backup.  The
    returned candidates are informational until the user handles them through
    an explicitly recoverable workflow outside this status check.
    """

    if keep_per_series < 1:
        raise ValueError("keep_per_series must be positive")
    if min_age_days < 0:
        raise ValueError("min_age_days must be non-negative")
    if max_entries < 1 or max_entries > 5000:
        raise ValueError("max_entries must be between 1 and 5000")
    root_candidates = [paths.backups, paths.codex_home / "backups"]
    roots: list[Path] = []
    seen_roots: set[str] = set()
    for root in root_candidates:
        key = _config_path_key(str(root))
        if key not in seen_roots:
            roots.append(root)
            seen_roots.add(key)
    entries: list[dict[str, Any]] = []
    available_entries = 0
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        available_entries += len(children)
        for child in children:
            if len(entries) >= max_entries:
                break
            try:
                is_junction = getattr(child, "is_junction", None)
                if child.is_symlink() or (is_junction is not None and is_junction()):
                    continue
                stat_result = child.stat()
                if child.is_dir():
                    files, size = directory_bytes(child)
                    kind = "directory"
                elif child.is_file():
                    files, size = 1, int(stat_result.st_size)
                    kind = "file"
                else:
                    continue
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "root": str(root),
                    "kind": kind,
                    "series": _backup_series_name(child.name),
                    "files": files,
                    "bytes": size,
                    "modified_at": datetime.fromtimestamp(
                        stat_result.st_mtime,
                        tz=timezone.utc,
                    ).isoformat().replace("+00:00", "Z"),
                    "modified_timestamp": stat_result.st_mtime,
                }
            )
    now_value = time.time() if now_timestamp is None else float(now_timestamp)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault((entry["root"], entry["series"]), []).append(entry)
    candidates: list[dict[str, Any]] = []
    for group_entries in groups.values():
        ordered = sorted(
            group_entries,
            key=lambda item: (float(item["modified_timestamp"]), item["name"]),
            reverse=True,
        )
        for index, entry in enumerate(ordered):
            if index < keep_per_series:
                continue
            age_days = max(0.0, (now_value - float(entry["modified_timestamp"])) / 86400.0)
            if age_days >= min_age_days:
                candidate = dict(entry)
                candidate["age_days"] = round(age_days, 1)
                candidates.append(candidate)
    total_bytes = sum(int(entry["bytes"]) for entry in entries)
    candidate_bytes = sum(int(entry["bytes"]) for entry in candidates)
    return {
        "operation": "backup_retention",
        "roots": [str(root) for root in roots],
        "policy": {
            "keep_per_series": keep_per_series,
            "min_age_days": min_age_days,
            "automatic_deletion": False,
        },
        "entry_count": len(entries),
        "total_bytes": total_bytes,
        "total_size": human_bytes(total_bytes),
        "candidate_count": len(candidates),
        "candidate_bytes": candidate_bytes,
        "candidate_size": human_bytes(candidate_bytes),
        "candidates": sorted(
            candidates,
            key=lambda item: (float(item["modified_timestamp"]), item["path"]),
        ),
        "truncated": available_entries > len(entries),
    }


def _line_ending(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n"


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^\s*\[[^\]]+\]\s*(?:#.*)?(?:\r?\n)?$", lines[i]):
            end = i
            break
    return start, end


def _replace_assignment(lines: list[str], key: str, value: str, start: int, end: int) -> bool:
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=).*$")
    for index in range(start, end):
        match = pattern.match(lines[index])
        if match:
            lines[index] = f'{match.group(1)} {value}{_line_ending(lines[index])}'
            return True
    return False


def _remove_assignments(lines: list[str], keys: set[str], start: int, end: int) -> int:
    pattern = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
    removed = 0
    index = start
    while index < end:
        match = pattern.match(lines[index])
        if match and match.group(1) in keys:
            del lines[index]
            end -= 1
            removed += 1
            continue
        index += 1
    return removed


def _upsert_provider_section(
    lines: list[str],
    provider_alias: str,
    *,
    label: str,
    base_url: str,
    newline: str,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", provider_alias):
        raise ValueError("invalid provider alias")
    section = f"model_providers.{provider_alias}"
    bounds = _section_bounds(lines, section)
    if bounds is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        lines.extend(
            [
                f"[{section}]{newline}",
                f"name = {json.dumps(label)}{newline}",
                f"base_url = {json.dumps(base_url)}{newline}",
                f'wire_api = "responses"{newline}',
                f"requires_openai_auth = false{newline}",
            ]
        )
        return

    assignments = {
        "name": json.dumps(label),
        "base_url": json.dumps(base_url),
        "wire_api": json.dumps("responses"),
        "requires_openai_auth": "false",
    }
    for key, value in assignments.items():
        start, end = _section_bounds(lines, section) or (0, 0)
        if not _replace_assignment(lines, key, value, start + 1, end):
            lines.insert(end, f"{key} = {value}{newline}")


def update_config_provider(
    config_path: Path,
    profile_id: str,
    *,
    provider_alias: str | None = None,
    provider_versions: Iterable[dict[str, Any]] | None = None,
    model: str | None = None,
    model_catalog_json: str | os.PathLike[str] | None = None,
    router_host: str = DEFAULT_ROUTER_HOST,
    router_port: int = DEFAULT_ROUTER_PORT,
) -> dict[str, Any]:
    """Update only the provider settings in config.toml and publish atomically.

    This is intentionally a line-preserving edit instead of a TOML round-trip:
    comments, plugin settings, project trust entries and unknown future keys
    remain byte-for-byte unchanged.  The resulting document is parsed before
    replacement when ``tomllib`` is available.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", profile_id):
        raise ValueError(f"unsupported config profile: {profile_id}")
    target_alias = "openai" if profile_id == "official" else (provider_alias or "custom")
    if target_alias != "openai" and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", target_alias):
        raise ValueError("invalid provider alias")
    old = config_path.read_bytes() if config_path.exists() else b""
    newline = "\r\n" if b"\r\n" in old else "\n"
    text = old.decode("utf-8") if old else ""
    lines = text.splitlines(keepends=True)
    if not lines:
        lines = []

    # Remove bearer-token assignments everywhere, not just from the custom
    # provider table.  A previous hand-edited config may have placed the key
    # in a different table; it must never survive this repair operation.
    _remove_assignments(lines, {"experimental_bearer_token", "model_catalog_json"}, 0, len(lines))

    # The built-in OpenAI provider is Codex's documented default.  Omitting
    # the top-level override in official mode preserves the native ChatGPT
    # account UI, while persisted official tasks still bind to ``openai``.
    # Relay modes remain explicit so new tasks freeze the selected immutable
    # provider alias at creation time.
    first_section = next(
        (i for i, line in enumerate(lines) if re.match(r"^\s*\[[^\]]+\]", line)),
        len(lines),
    )
    if profile_id == "official":
        # Official mode must also release a relay-selected top-level model.
        # Codex can then advertise the signed-in account's native model set
        # instead of inheriting a single relay model as the global default.
        _remove_assignments(lines, {"model_provider", "model"}, 0, first_section)
        first_section = next(
            (i for i, line in enumerate(lines) if re.match(r"^\s*\[[^\]]+\]", line)),
            len(lines),
        )
    elif not _replace_assignment(lines, "model_provider", json.dumps(target_alias), 0, first_section):
        lines.insert(0, f'model_provider = {json.dumps(target_alias)}{newline}')
        first_section += 1

    if model:
        if not _replace_assignment(lines, "model", json.dumps(model), 0, first_section):
            lines.insert(0, f'model = {json.dumps(model)}{newline}')
            first_section += 1

    # Codex reads model_catalog_json as a top-level setting.  Keeping it out
    # of [model_providers.custom] is important: Codex rewrites that provider
    # table on startup and drops unknown nested keys.
    if profile_id != "official":
        catalog_path = (
            Path(model_catalog_json)
            if model_catalog_json is not None
            else config_path.parent / SWITCHBOARD_DIRNAME / MODEL_CATALOG_DIRNAME / f"{profile_id}.json"
        )
        catalog_value = json.dumps(str(catalog_path.resolve()))
        first_section = next(
            (i for i, line in enumerate(lines) if re.match(r"^\s*\[[^\]]+\]", line)),
            len(lines),
        )
        if not _replace_assignment(lines, "model_catalog_json", catalog_value, 0, first_section):
            lines.insert(0, f"model_catalog_json = {catalog_value}{newline}")
            first_section += 1

    versions = [dict(item) for item in (provider_versions or [])]
    if target_alias != "openai" and not any(item.get("provider_alias") == target_alias for item in versions):
        versions.append(
            {
                "provider_alias": target_alias,
                "profile_id": profile_id,
                "label": profile_id,
            }
        )
    for version in versions:
        alias = str(version.get("provider_alias") or "")
        version_profile = str(version.get("profile_id") or alias)
        label = str(version.get("label") or version_profile)
        route = f"http://{router_host}:{router_port}/profiles/{alias}/v1"
        _upsert_provider_section(
            lines,
            alias,
            label=f"Switchboard: {label}",
            base_url=route,
            newline=newline,
        )

    new_text = "".join(lines)
    if tomllib is not None:
        try:
            tomllib.loads(new_text)
        except Exception as exc:
            raise RuntimeError(f"refusing invalid config.toml update: {exc}") from exc
    changed = new_text.encode("utf-8") != old
    if changed:
        atomic_write_bytes(config_path, new_text.encode("utf-8"))
    return {
        "path": str(config_path),
        "profile_id": profile_id,
        "provider_alias": target_alias,
        "changed": changed,
    }


def robocopy_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "robocopy.exe",
        str(source),
        str(destination),
        "/E",
        "/COPY:DAT",
        "/DCOPY:DAT",
        "/SJ",
        "/R:1",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    if result.returncode > 7:
        raise RuntimeError(f"robocopy failed ({result.returncode}) for {source}")


def make_junction(source: Path, destination: Path) -> None:
    if source.exists() or source.is_symlink():
        raise FileExistsError(f"junction target already exists: {source}")
    source.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(source), str(destination)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mklink failed for {source}: {result.stderr.strip()}")


def is_directory_link(path: Path) -> bool:
    """Return whether ``path`` is a symlink or Windows directory junction."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def link_resolves_to(source: Path, destination: Path) -> bool:
    if not is_directory_link(source):
        return False
    try:
        return source.resolve() == destination.resolve()
    except OSError:
        return False


def _retired_source_candidates(source: Path) -> list[Path]:
    pattern = re.compile(rf"^{re.escape(source.name)}\.migrated-\d{{8}}-\d{{6}}(?:-\d{{6}})?$")
    try:
        candidates = list(source.parent.glob(f"{source.name}.migrated-*"))
    except OSError:
        return []
    return sorted(
        path
        for path in candidates
        if pattern.fullmatch(path.name) and path.is_dir() and not is_directory_link(path)
    )


def _retry_rmtree_readonly(function: Any, path: str, exc_info: tuple[Any, Any, Any]) -> None:
    """Clear a read-only attribute inside a verified retirement tree and retry."""

    error = exc_info[1]
    if not isinstance(error, PermissionError):
        raise error
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        raise error
    readonly_attribute = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0)
    has_readonly_attribute = bool(
        readonly_attribute and getattr(file_stat, "st_file_attributes", 0) & readonly_attribute
    )
    if file_stat.st_mode & stat.S_IWRITE and not has_readonly_attribute:
        # Do not hide a real sharing violation or ACL failure.  Those require
        # the writer/owner to be resolved before the retirement copy is safe to
        # remove.
        raise error
    os.chmod(path, file_stat.st_mode | stat.S_IWRITE)
    function(path)


def preserve_retired_residue(retired: Path, destination: Path) -> Path:
    """Copy a partially cleaned retirement tree to E before removing it from C."""

    expected = directory_bytes(retired)
    recovery_root = destination.parent / ".migration-recovery"
    recovery_root.mkdir(parents=True, exist_ok=True)
    recovery = recovery_root / f"{retired.name}-{uuid.uuid4().hex[:8]}"
    robocopy_copy(retired, recovery)
    if directory_bytes(recovery) != expected:
        raise RuntimeError(f"retired residue recovery verification failed: {recovery}")
    atomic_write_json(
        recovery_root / f"{recovery.name}.receipt.json",
        {
            "version": 1,
            "preserved_at": utc_now(),
            "retired_source": str(retired),
            "active_destination": str(destination),
            "recovery": str(recovery),
            "files": expected[0],
            "bytes": expected[1],
        },
    )
    return recovery


def remove_retired_source_copy(
    retired: Path,
    source: Path,
    destination: Path,
    *,
    junction: bool,
    expected: tuple[int, int] | None = None,
) -> Path | None:
    """Remove only a verified, precisely named C-drive retirement copy."""

    expected_parent = source.parent.resolve()
    if retired.parent.resolve() != expected_parent or retired not in _retired_source_candidates(source):
        raise RuntimeError(f"refusing unexpected retired source path: {retired}")
    if junction:
        if not link_resolves_to(source, destination):
            raise RuntimeError(f"refusing cleanup before junction verification: {source}")
    elif source.exists() or is_directory_link(source):
        raise RuntimeError(f"refusing legacy cleanup while source is still active: {source}")
    retired_stats = directory_bytes(retired)
    destination_stats = directory_bytes(destination)
    recovery: Path | None = None
    if expected is not None:
        if retired_stats != expected or destination_stats != expected:
            raise RuntimeError(f"refusing cleanup after content verification mismatch: {retired}")
    elif retired_stats != destination_stats:
        # A previous rmtree may have removed many files before Windows stopped
        # on a read-only/locked entry.  The active destination can also evolve
        # after its junction is published.  Preserve every remaining byte on E
        # before resuming C-drive cleanup instead of weakening the equality
        # guard or assuming the residue is disposable.
        recovery = preserve_retired_residue(retired, destination)
    shutil.rmtree(retired, onerror=_retry_rmtree_readonly)
    if retired.exists():
        raise RuntimeError(f"retired source cleanup did not complete: {retired}")
    return recovery


def migrate_directory(
    source: Path,
    destination: Path,
    *,
    execute: bool,
    junction: bool = True,
    resume_existing: bool = False,
    pre_publish_probe: Any | None = None,
    cleanup_retired: bool = False,
) -> dict[str, Any]:
    if execute and link_resolves_to(source, destination):
        removed: list[str] = []
        recoveries: list[str] = []
        if cleanup_retired:
            for retired in _retired_source_candidates(source):
                recovery = remove_retired_source_copy(retired, source, destination, junction=True)
                removed.append(str(retired))
                if recovery is not None:
                    recoveries.append(str(recovery))
        return {
            "source": str(source),
            "destination": str(destination),
            "source_files": 0,
            "source_bytes": 0,
            "executed": True,
            "junction": junction,
            "resumed_existing": True,
            "removed_source_copies": removed,
            "retired_residue_recoveries": recoveries,
            "status": "already_migrated",
        }
    if execute and resume_existing and not junction and not source.exists() and destination.exists():
        candidates = _retired_source_candidates(source)
        if candidates:
            removed = []
            recoveries = []
            if cleanup_retired:
                for retired in candidates:
                    recovery = remove_retired_source_copy(retired, source, destination, junction=False)
                    removed.append(str(retired))
                    if recovery is not None:
                        recoveries.append(str(recovery))
            return {
                "source": str(source),
                "destination": str(destination),
                "source_files": 0,
                "source_bytes": 0,
                "executed": True,
                "junction": False,
                "resumed_existing": True,
                "removed_source_copies": removed,
                "retired_residue_recoveries": recoveries,
                "status": "already_migrated",
            }
    files, total = directory_bytes(source)
    result: dict[str, Any] = {
        "source": str(source),
        "destination": str(destination),
        "source_files": files,
        "source_bytes": total,
        "executed": execute,
        "junction": junction,
        "resumed_existing": resume_existing,
    }
    if not execute:
        return result
    if not source.exists():
        result["status"] = "missing"
        return result
    if destination.exists() and not resume_existing:
        raise FileExistsError(f"destination already exists: {destination}")
    if destination.exists() and not destination.is_dir():
        raise FileExistsError(f"destination is not a directory: {destination}")
    destination_preexisted = destination.exists()
    try:
        robocopy_copy(source, destination)
    except Exception:
        # A failed copy may leave a partial destination.  It was created by
        # this invocation and the source was not renamed, so removing only
        # that new destination is safe and makes retry deterministic.
        if destination.exists() and not destination_preexisted:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    result["destination_files"], result["destination_bytes"] = directory_bytes(destination)
    # Detect a source that changed while it was being copied.  Renaming a
    # moving source would make the junction point at an incomplete snapshot.
    source_after = directory_bytes(source)
    if source_after != (files, total):
        raise MigrationRetryable(f"source changed during migration: {source}")
    if result["destination_files"] != files or result["destination_bytes"] != total:
        raise RuntimeError(f"destination verification failed: {destination}")
    if pre_publish_probe is not None and list(pre_publish_probe()):
        raise MigrationRetryable(f"writers restarted before migration publication: {source}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    retired = source.with_name(f"{source.name}.migrated-{stamp}")
    try:
        source.rename(retired)
    except OSError as exc:
        if getattr(exc, "winerror", None) in {5, 32}:
            raise MigrationRetryable(f"source became busy before migration publication: {source}") from exc
        raise
    try:
        if junction:
            make_junction(source, destination)
    except Exception:
        # Never strand the original path if the link publication fails.  The
        # verified destination is retained for manual cleanup/retry, while the
        # source directory is restored to its exact pre-migration location.
        if source.exists() or source.is_symlink():
            try:
                source.unlink()
            except OSError:
                pass
        if retired.exists():
            retired.rename(source)
        raise
    result["retired_source"] = str(retired)
    if cleanup_retired:
        recovery = remove_retired_source_copy(
            retired,
            source,
            destination,
            junction=junction,
            expected=(files, total),
        )
        result["source_copy_removed"] = True
        if recovery is not None:
            result["retired_residue_recovery"] = str(recovery)
    result["status"] = "migrated"
    return result


def validate_migration_targets(
    targets: list[tuple[str, Path, Path, bool]],
    *,
    allow_existing_destinations: bool = False,
    existing_destination_labels: set[str] | None = None,
) -> None:
    """Reject conflicts before any source directory is renamed."""

    seen_sources: set[str] = set()
    seen_destinations: set[str] = set()
    for label, source, destination, _junction in targets:
        source_key = os.path.normcase(os.path.abspath(os.fspath(source)))
        destination_key = os.path.normcase(os.path.abspath(os.fspath(destination)))
        if source_key in seen_sources or destination_key in seen_destinations:
            raise RuntimeError(f"duplicate migration target: {label}")
        seen_sources.add(source_key)
        seen_destinations.add(destination_key)
        if source_key == destination_key:
            raise RuntimeError(f"source and destination are identical: {label}")
        if (
            source.exists()
            and destination.exists()
            and not (
                allow_existing_destinations
                and (existing_destination_labels is None or label in existing_destination_labels)
            )
        ):
            raise FileExistsError(f"destination already exists: {destination}")


def migration_targets() -> list[tuple[str, Path, Path, bool]]:
    user = Path(os.environ.get("USERPROFILE", str(Path.home())))
    return [
        ("project-workspaces", user / "Documents" / "Codex", Path(r"E:\Codex-Projects\Codex"), True),
        ("codex-web-data", user / "AppData" / "Roaming" / "Codex", Path(r"E:\Codex-AppData\Roaming\Codex"), True),
        ("codex-runtime-data", user / "AppData" / "Local" / "OpenAI" / "Codex", Path(r"E:\Codex-AppData\Local\OpenAI\Codex"), True),
        ("codex-runtime-cache", user / ".cache" / "codex-runtimes", Path(r"E:\Codex-AppData\cache\codex-runtimes"), True),
        ("legacy-codex-home", user / ".codex", Path(r"E:\Codex-Archive\legacy-dot-codex"), False),
    ]


def build_migration_manifest(
    paths: Paths,
    targets: list[tuple[str, Path, Path, bool]] | None = None,
) -> dict[str, Any]:
    manifest_targets = []
    selected_targets = migration_targets() if targets is None else targets
    for label, source, destination, junction in selected_targets:
        files, total = directory_bytes(source)
        manifest_targets.append(
            {
                "label": label,
                "source": str(source),
                "destination": str(destination),
                "junction": junction,
                "exists": source.exists(),
                "files": files,
                "bytes": total,
            }
        )
    manifest = {"version": 1, "created_at": utc_now(), "targets": manifest_targets}
    atomic_write_json(paths.migration_manifest, manifest)
    return manifest


def execute_migration(
    paths: Paths,
    targets: list[tuple[str, Path, Path, bool]] | None = None,
    *,
    process_probe: Any | None = None,
    lock_probe: Any | None = None,
    backup_fn: Any | None = None,
    resume_existing: bool = False,
) -> dict[str, Any]:
    def current_blockers() -> list[Any]:
        processes = blocking_processes(paths) if process_probe is None else list(process_probe(paths))
        locks_now = writer_locks(paths) if lock_probe is None else list(lock_probe(paths))
        return [*processes, *locks_now]

    blockers = blocking_processes(paths) if process_probe is None else list(process_probe(paths))
    locks = writer_locks(paths) if lock_probe is None else list(lock_probe(paths))
    if blockers or locks:
        raise RuntimeError(
            "migration requires Codex and project writers to be closed; "
            f"process_blockers={len(blockers)}, writer_locks={len(locks)}"
        )
    selected_targets = migration_targets() if targets is None else targets
    existing_destination_labels: set[str] = set()
    if resume_existing:
        previous_manifest = read_json(paths.migration_manifest, {})
        for item in previous_manifest.get("targets", []):
            if isinstance(item, dict):
                existing_destination_labels.add(str(item.get("label") or ""))
    validate_migration_targets(
        selected_targets,
        allow_existing_destinations=resume_existing,
        existing_destination_labels=existing_destination_labels,
    )
    # Take database snapshots before moving any directory.  This uses the
    # SQLite backup API and therefore includes committed WAL pages.  If a
    # backup fails, do not begin a partial filesystem migration.
    database_backups = backup_active_databases(paths) if backup_fn is None else list(backup_fn(paths))
    paths.runtime_tmp.mkdir(parents=True, exist_ok=True)
    results = []
    for label, source, destination, junction in selected_targets:
        if not source.exists():
            continue
        results.append(
            migrate_directory(
                source,
                destination,
                execute=True,
                junction=junction,
                resume_existing=resume_existing,
                pre_publish_probe=current_blockers,
                cleanup_retired=True,
            )
        )
    manifest = {
        "version": 1,
        "completed_at": utc_now(),
        "database_backups": [str(path) for path in database_backups],
        "results": results,
    }
    atomic_write_json(paths.migration_manifest, manifest)
    return manifest


def _append_migration_wait_log(paths: Paths, event: str, **details: Any) -> None:
    """Append a bounded, secret-free event to the detached migration log."""

    paths.switchboard.mkdir(parents=True, exist_ok=True)
    record = {"at": utc_now(), "event": event, **details}
    log_path = paths.switchboard / "migration-wait.jsonl"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def wait_for_migration(
    paths: Paths,
    targets: list[tuple[str, Path, Path, bool]] | None = None,
    *,
    process_probe: Any | None = None,
    lock_probe: Any | None = None,
    backup_fn: Any | None = None,
    poll_seconds: float = 5.0,
    stable_empty_checks: int = 2,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """Wait for desktop/project writers to exit, then execute one migration.

    This is intended for a detached helper started before the desktop app is
    closed.  It requires two consecutive empty probes to avoid racing a quick
    app restart and records only counts/status in ``migration-wait.jsonl``.
    """

    if poll_seconds < 0:
        raise ValueError("poll_seconds must be non-negative")
    if stable_empty_checks < 1:
        raise ValueError("stable_empty_checks must be positive")
    _append_migration_wait_log(paths, "started", stable_empty_checks=stable_empty_checks)
    empty_checks = 0
    last_report: tuple[int, int] | None = None
    while True:
        blockers = blocking_processes(paths) if process_probe is None else list(process_probe(paths))
        locks = writer_locks(paths) if lock_probe is None else list(lock_probe(paths))
        counts = (len(blockers), len(locks))
        if counts == (0, 0):
            empty_checks += 1
            if empty_checks >= stable_empty_checks:
                _append_migration_wait_log(paths, "ready")
                try:
                    result = execute_migration(
                        paths,
                        targets,
                        process_probe=process_probe,
                        lock_probe=lock_probe,
                        backup_fn=backup_fn,
                        resume_existing=True,
                    )
                except MigrationRetryable as exc:
                    _append_migration_wait_log(paths, "writers_restarted", error=str(exc))
                    empty_checks = 0
                    last_report = None
                    sleep_fn(poll_seconds)
                    continue
                except Exception as exc:
                    _append_migration_wait_log(paths, "failed", error=str(exc))
                    raise
                _append_migration_wait_log(paths, "completed", migrated=len(result.get("results", [])))
                return result
        else:
            empty_checks = 0
            if counts != last_report:
                _append_migration_wait_log(
                    paths,
                    "waiting",
                    process_blockers=counts[0],
                    writer_locks=counts[1],
                )
                last_report = counts
        sleep_fn(poll_seconds)


def set_active_profile(
    paths: Paths,
    profile_id: str,
    *,
    force: bool = False,
    allow_official: bool = False,
    model: str | None = None,
) -> dict[str, Any]:
    if writer_locks(paths) and not force:
        raise RuntimeError("a thread writer is active; finish the turn before switching")
    document = load_profiles(paths)
    profile = profile_by_id(document, profile_id)
    if not profile.get("enabled", False):
        raise RuntimeError(f"profile is disabled: {profile_id}")
    if profile.get("kind") == "official" and not allow_official:
        raise RuntimeError("official account must be activated through the Codex login flow")
    previous_active = paths.active.read_bytes() if paths.active.exists() else None
    previous_config = paths.config.read_bytes() if paths.config.exists() else None
    previous_versions = paths.provider_versions.read_bytes() if paths.provider_versions.exists() else None
    if profile.get("kind") == "relay":
        base_url = str(profile.get("base_url") or "")
        # Fail before publishing active.json.  A half-configured profile would
        # otherwise make the router return 503 for every subsequent request.
        normalized_upstream(base_url, "/v1/responses")
        key_ref = str(profile.get("key_ref") or "")
        if not key_ref or not key_path(paths, key_ref).exists():
            raise RuntimeError(f"relay key is not configured: {key_ref or '<missing reference>'}")
        target_selection = str(model or profile.get("model") or "").strip()
        if target_selection not in relay_model_ids(profile):
            raise RuntimeError(f"model is not configured for {profile_id}: {target_selection}")
        catalog_result = _prepare_model_catalog_for_runtime(
            paths,
            profile_id,
            model=target_selection,
        )
        provider_version = ensure_provider_version(paths, profile_id, model=target_selection)
        provider_alias = str(provider_version["provider_alias"])
        target_model = str(provider_version.get("model") or "") or None
    else:
        catalog_result = None
        provider_version = None
        provider_alias = "openai"
        target_model = None
    router = document.get("router", {})
    try:
        config_result = update_config_provider(
            paths.config,
            profile_id,
            provider_alias=provider_alias,
            provider_versions=load_provider_versions(paths).get("versions", []),
            model=target_model,
            model_catalog_json=catalog_result["path"] if catalog_result else None,
            router_host=str(router.get("host", DEFAULT_ROUTER_HOST)),
            router_port=int(router.get("port", DEFAULT_ROUTER_PORT)),
        )
        active = load_active(paths)
        next_state = {
            "profile_id": profile_id,
            "provider_alias": provider_alias,
            "version_id": provider_version.get("version_id") if provider_version else "openai",
            "model": target_model,
            "revision": int(active.get("revision", 0)) + 1,
            "changed_at": utc_now(),
        }
        atomic_write_json(paths.active, next_state)
    except Exception:
        # Config, immutable-version publication, and the default pointer are
        # one transaction. An unused alias must not survive a failed switch.
        for path, previous in (
            (paths.config, previous_config),
            (paths.provider_versions, previous_versions),
            (paths.active, previous_active),
        ):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_bytes(path, previous)
        raise
    return {
        "active": next_state,
        "profile": profile,
        "provider_version": provider_version,
        "config": config_result,
        "model_catalog": catalog_result,
    }


def _configured_codex_cli_path(paths: Paths | None) -> Path | None:
    """Read the desktop-published user-executable Codex path, if present."""

    if paths is None or tomllib is None or not paths.config.exists():
        return None
    try:
        document = tomllib.loads(paths.config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = (
        document.get("mcp_servers", {})
        .get("node_repl", {})
        .get("env", {})
        .get("CODEX_CLI_PATH")
    )
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value)


def _windows_codex_native_candidates(paths: Paths | None = None) -> list[Path]:
    """Find a user-executable Codex native binary on Windows.

    Prefer the user-executable runtime published by the desktop app so catalog
    export and thread resume use the same schema. The npm CLI remains a
    fallback; the MSIX ``WindowsApps`` executable is excluded because a normal
    desktop helper can receive ``WinError 5`` when launching it directly.
    """

    if os.name != "nt":
        return []
    machine = platform.machine().lower()
    if "arm64" in machine or "aarch64" in machine:
        package_name = "codex-win32-arm64"
        target = "aarch64-pc-windows-msvc"
    else:
        package_name = "codex-win32-x64"
        target = "x86_64-pc-windows-msvc"

    candidates: list[Path] = []
    seen_candidates: set[str] = set()

    def add_candidate(candidate: Path) -> None:
        try:
            key = os.path.normcase(os.path.abspath(os.fspath(candidate)))
        except (OSError, ValueError):
            return
        if key in seen_candidates or not candidate.is_file():
            return
        seen_candidates.add(key)
        candidates.append(candidate)

    configured = _configured_codex_cli_path(paths)
    if configured is not None:
        add_candidate(configured)

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        desktop_bin = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
        try:
            desktop_candidates = list(desktop_bin.glob("*/codex.exe"))
        except OSError:
            desktop_candidates = []

        def modified_at(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        for candidate in sorted(desktop_candidates, key=modified_at, reverse=True):
            add_candidate(candidate)

    roots: list[Path] = []
    seen_roots: set[str] = set()

    def add_root(path: Path) -> None:
        try:
            key = os.path.normcase(os.path.abspath(os.fspath(path)))
        except (OSError, ValueError):
            return
        if key not in seen_roots:
            seen_roots.add(key)
            roots.append(path)

    # Keep the user's active npm installation as a compatibility fallback.
    for command_name in ("codex.cmd", "codex"):
        wrapper = shutil.which(command_name)
        if not wrapper:
            continue
        wrapper_path = Path(wrapper)
        if wrapper_path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            add_root(wrapper_path.parent / "node_modules" / "@openai" / "codex")

    appdata = os.environ.get("APPDATA")
    if appdata:
        add_root(Path(appdata) / "npm" / "node_modules" / "@openai" / "codex")
    if local_appdata:
        add_root(Path(local_appdata) / "npm" / "node_modules" / "@openai" / "codex")

    for root in roots:
        package_roots = (
            root / "node_modules" / "@openai" / package_name,
            root / package_name,
            root,
        )
        for package_root in package_roots:
            candidate = package_root / "vendor" / target / "bin" / "codex.exe"
            add_candidate(candidate)
    return candidates


def resolve_appserver_executable(
    executable: str | os.PathLike[str] | None = None,
    *,
    paths: Paths | None = None,
) -> str:
    """Resolve the standalone app-server executable without WindowsApps ACLs."""

    requested = executable
    if requested is None:
        requested = os.environ.get("CODEX_SWITCHBOARD_CODEX")
    requested_text = os.fspath(requested).strip() if requested is not None else ""
    # An explicit path/name other than the conventional bare Codex command is
    # an intentional override and must not be silently replaced.
    if requested_text and requested_text.lower() not in {"codex", "codex.exe", "codex.cmd", "codex.ps1"}:
        return requested_text
    candidates = _windows_codex_native_candidates(paths)
    if candidates:
        return os.fspath(candidates[0])
    return requested_text or "codex"


def _appserver_client(paths: Paths, executable: str | None = None):
    from appserver_client import AppServerClient

    candidate = resolve_appserver_executable(executable, paths=paths)
    client = AppServerClient(
        executable=candidate,
        codex_home=paths.codex_home,
        request_timeout=DEFAULT_APP_SERVER_TIMEOUT,
    )
    client.start()
    try:
        client.initialize(capabilities={"experimentalApi": True})
    except BaseException:
        client.close()
        raise
    return client


def _prepare_profile_runtime(
    paths: Paths,
    profile_id: str,
    *,
    selected_model: str | None = None,
) -> dict[str, Any]:
    """Make one target provider loadable without changing active.json."""

    document = load_profiles(paths)
    profile = profile_by_id(document, profile_id)
    if not profile.get("enabled", False):
        raise RuntimeError(f"profile is disabled: {profile_id}")
    if profile.get("kind") == "relay":
        key_ref = str(profile.get("key_ref") or "")
        if not key_ref or not key_path(paths, key_ref).exists():
            raise RuntimeError(f"relay key is not configured: {key_ref or '<missing reference>'}")
        model_choice = str(selected_model or profile.get("model") or "").strip()
        if model_choice not in relay_model_ids(profile):
            raise RuntimeError(f"model is not configured for {profile_id}: {model_choice}")
        version = ensure_provider_version(paths, profile_id, model=model_choice)
        provider_alias = str(version["provider_alias"])
        model = str(version.get("model") or "") or None
        catalog = _prepare_model_catalog_for_runtime(paths, profile_id, model=model)
    elif profile.get("kind") == "official":
        version = None
        provider_alias = "openai"
        model = None
        catalog = None
    else:
        raise RuntimeError(f"unsupported profile kind: {profile.get('kind')}")
    router = document.get("router", {})
    config = update_config_provider(
        paths.config,
        profile_id,
        provider_alias=provider_alias,
        provider_versions=load_provider_versions(paths).get("versions", []),
        model=model,
        model_catalog_json=catalog["path"] if catalog else None,
        router_host=str(router.get("host", DEFAULT_ROUTER_HOST)),
        router_port=int(router.get("port", DEFAULT_ROUTER_PORT)),
    )
    return {
        "profile": profile,
        "provider_version": version,
        "provider_alias": provider_alias,
        "model": model,
        "model_catalog": catalog,
        "config": config,
    }


def _restore_optional_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_bytes(path, previous)


def _archive_source_keep_head(
    paths: Paths,
    client: Any,
    source_thread_id: str,
    head_thread_id: str,
    *,
    enabled: bool,
) -> dict[str, Any]:
    """Publish the new head and optionally archive the source lineage.

    App Server deliberately archives spawned descendants together with their
    ancestor.  The new head is always restored and pinned; enabled controls
    only whether the source is archived and unpinned.
    """

    source_before = thread_provider_binding(paths, source_thread_id)
    status: dict[str, Any] = {
        "requested": bool(enabled),
        "source_archived": False,
        "head_unarchived": False,
        "head_active": False,
        "source_unpinned": False,
        "source_preserved": False,
        "head_pinned": False,
        "complete": False,
        "warning": None,
    }
    errors: list[str] = []

    def attempt(label: str, operation: Any) -> None:
        try:
            operation()
        except Exception as exc:
            # Idempotent App Server errors are accepted only if the persisted
            # state read below proves that the requested state already holds.
            errors.append(f"{label}: {str(exc)[:240]}")

    source_was_archived = bool(int(source_before.get("archived") or 0))
    if enabled and not source_was_archived:
        if hasattr(client, "archive_thread"):
            attempt("归档来源", lambda: client.archive_thread(source_thread_id))
        else:
            errors.append("归档来源: 当前 App Server 不支持 thread/archive")
    # Archive may cascade to the new head, so restore only when needed.
    head_now = thread_provider_binding(paths, head_thread_id)
    if bool(int(head_now.get("archived") or 0)):
        if hasattr(client, "unarchive_thread"):
            attempt("恢复新任务", lambda: client.unarchive_thread(head_thread_id))
        else:
            errors.append("恢复新任务: 当前 App Server 不支持 thread/unarchive")
    if hasattr(client, "set_thread_pinned"):
        if enabled and bool(int(source_before.get("is_pinned") or 0)):
            attempt("取消旧任务置顶", lambda: client.set_thread_pinned(source_thread_id, False))
        if not bool(int(head_now.get("is_pinned") or 0)):
            attempt("置顶新任务", lambda: client.set_thread_pinned(head_thread_id, True))
    else:
        errors.append("任务置顶: 当前 App Server 不支持 thread/metadata/update")

    source_after = thread_provider_binding(paths, source_thread_id)
    head_after = thread_provider_binding(paths, head_thread_id)
    status["source_archived"] = bool(int(source_after.get("archived") or 0))
    status["head_active"] = not bool(int(head_after.get("archived") or 0))
    status["head_unarchived"] = status["head_active"]
    status["source_unpinned"] = not bool(int(source_after.get("is_pinned") or 0))
    status["head_pinned"] = bool(int(head_after.get("is_pinned") or 0))
    status["source_preserved"] = bool(
        int(source_after.get("archived") or 0) == int(source_before.get("archived") or 0)
        and int(source_after.get("is_pinned") or 0) == int(source_before.get("is_pinned") or 0)
    )

    if not status["head_active"]:
        # Never leave the usable fork hidden.  If restoring the head failed,
        # make both tasks visible best-effort and report partial completion.
        if hasattr(client, "unarchive_thread"):
            attempt("恢复新任务兜底", lambda: client.unarchive_thread(head_thread_id))
            if enabled:
                attempt("恢复旧任务兜底", lambda: client.unarchive_thread(source_thread_id))
        source_after = thread_provider_binding(paths, source_thread_id)
        head_after = thread_provider_binding(paths, head_thread_id)
        status["source_archived"] = bool(int(source_after.get("archived") or 0))
        status["head_active"] = not bool(int(head_after.get("archived") or 0))
        status["head_unarchived"] = status["head_active"]
        status["source_unpinned"] = not bool(int(source_after.get("is_pinned") or 0))
        status["head_pinned"] = bool(int(head_after.get("is_pinned") or 0))
        status["source_preserved"] = bool(
            int(source_after.get("archived") or 0) == int(source_before.get("archived") or 0)
            and int(source_after.get("is_pinned") or 0) == int(source_before.get("is_pinned") or 0)
        )

    status["complete"] = bool(
        status["head_active"]
        and status["head_pinned"]
        and (
            status["source_archived"] and status["source_unpinned"]
            if enabled
            else status["source_preserved"]
        )
    )
    if not status["complete"]:
        missing = []
        if enabled and not status["source_archived"]:
            missing.append("旧任务未归档")
        if not status["head_active"]:
            missing.append("新任务仍在归档区")
        if enabled and not status["source_unpinned"]:
            missing.append("旧任务仍置顶")
        if not enabled and not status["source_preserved"]:
            missing.append("旧任务状态发生变化")
        if not status["head_pinned"]:
            missing.append("新任务未置顶")
        detail = "；".join(missing)
        if errors:
            detail += f"（{errors[0]}）"
        status["warning"] = f"任务 Provider 已转换，但整理只完成了一部分：{detail}"
    return status


def official_account_status(
    paths: Paths,
    *,
    executable: str | None = None,
    blocker_probe: Any | None = None,
) -> dict[str, Any]:
    """Read bounded official-login metadata through the supported owner."""

    probe = blocker_probe or appserver_blocking_processes
    blockers = probe(paths)
    if blockers:
        return {"available": False, "logged_in": None, "message": "关闭 Codex 后可核验具体官方账号"}
    client = _appserver_client(paths, executable)
    try:
        state = client.read_account()
    finally:
        client.close()
    account = state.get("account")
    logged_in = isinstance(account, dict) and account.get("type") == "chatgpt"
    safe_account: dict[str, Any] = {}
    if isinstance(account, dict):
        for key in ("type", "email", "planType", "name"):
            value = account.get(key)
            if isinstance(value, str) and value:
                safe_account[key] = value
    return {
        "available": True,
        "logged_in": logged_in,
        "account": safe_account,
        "message": "已登录 ChatGPT 官方账号" if logged_in else "未登录 ChatGPT 官方账号",
    }


def login_official_account(
    paths: Paths,
    *,
    switch_account: bool = False,
    executable: str | None = None,
    blocker_probe: Any | None = None,
) -> dict[str, Any]:
    """Run an explicit browser login; logout occurs only for account switching."""

    probe = blocker_probe or appserver_blocking_processes
    blockers = probe(paths)
    if blockers:
        raise RuntimeError(
            "official login requires the Codex desktop/App Server to be closed; "
            f"active_processes={len(blockers)}"
        )
    client = _appserver_client(paths, executable)
    try:
        before = client.read_account().get("account")
        already_logged_in = isinstance(before, dict) and before.get("type") == "chatgpt"
        if already_logged_in and not switch_account:
            return {"logged_in": True, "switched": False, "message": "已登录 ChatGPT 官方账号"}
        if switch_account and before is not None:
            client.logout()
        login_result = client.login({"type": "chatgpt", "appBrand": "codex"})
        auth_url = login_result.get("authUrl")
        login_id = login_result.get("loginId")
        if isinstance(auth_url, str) and auth_url:
            webbrowser.open(auth_url)
        completed = client.wait_for_login_completed(
            login_id=login_id if isinstance(login_id, str) else None,
            timeout=10 * 60,
        )
        if not completed.get("success"):
            raise RuntimeError("官方账号登录未成功完成")
        after = client.read_account().get("account")
        logged_in = isinstance(after, dict) and after.get("type") == "chatgpt"
        if not logged_in:
            raise RuntimeError("登录完成后未检测到 ChatGPT 官方账号")
        return {"logged_in": True, "switched": bool(switch_account), "message": "ChatGPT 官方账号登录成功"}
    finally:
        client.close()


def _reconcile_committed_fork(
    paths: Paths,
    source_binding: dict[str, Any],
    source_thread_id: str,
    created_thread_id: str,
    target_provider: str,
    expected_cwd: str | os.PathLike[str],
) -> dict[str, Any]:
    """Validate a task that App Server committed before local validation failed."""

    if not created_thread_id or created_thread_id == source_thread_id:
        raise RuntimeError("App Server did not return a distinct committed task id")
    last_error: BaseException | None = None
    binding: dict[str, Any] | None = None
    for _attempt in range(20):
        try:
            binding = thread_provider_binding(paths, created_thread_id)
            break
        except (ValueError, sqlite3.Error, OSError) as exc:
            last_error = exc
            time.sleep(0.05)
    if binding is None:
        raise RuntimeError(f"committed task is not present in the task index: {last_error}")
    if binding.get("provider_alias") != target_provider:
        raise RuntimeError("committed task Provider does not match the selected target")
    if _thread_cwd_key(binding.get("cwd")) != _thread_cwd_key(expected_cwd):
        raise RuntimeError("committed task working directory does not match the source")
    if _thread_parent_id(binding.get("rollout_path")) != source_thread_id:
        raise RuntimeError("committed task is not a direct child of the source")
    if _history_base_matches_source(source_binding, binding) is not True:
        raise RuntimeError("committed task history cursor does not match the current source")
    return binding


def _direct_target_ids(paths: Paths, source_id: str, provider: str, cwd: str) -> list[str]:
    connection = _readonly_sqlite(paths.state_database)
    try:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(threads)")}
        if "rollout_path" not in columns:
            raise RuntimeError("task index is missing rollout paths")
        rows = connection.execute(
            "SELECT id,cwd,rollout_path FROM threads WHERE model_provider=?", (provider,)
        ).fetchall()
        return [str(r["id"]) for r in rows
                if _thread_cwd_key(r["cwd"]) == _thread_cwd_key(cwd)
                and _thread_parent_id(r["rollout_path"]) == source_id]
    finally:
        connection.close()


def _reconcile_uncertain_receipt(paths: Paths, source: dict, receipt: dict) -> str:
    source_id = str(source["thread_id"])
    provider, cwd = str(receipt["provider"]), str(receipt["cwd"])
    candidates = set(_direct_target_ids(paths, source_id, provider, cwd)) - set(receipt["before_ids"])
    if receipt.get("created_thread_id"):
        candidates.add(str(receipt["created_thread_id"]))
    # Even a notification candidate must independently match the durable owner.
    if len(candidates) != 1:
        raise ConversionOutcomeUnknownError(source_id, sorted(candidates))
    ident = next(iter(candidates))
    _reconcile_committed_fork(paths, source, source_id, ident, provider, cwd)
    fingerprint = receipt.get("source_fingerprint")
    if fingerprint:
        path = Path(str(source.get("rollout_path") or ""))
        if (path.name != fingerprint["name"] or path.stat().st_size != fingerprint["size"]
                or sha256_file(path) != fingerprint["sha256"]):
            raise PartialThreadConversionError(ident, "来源记录已变化，不能把旧副本当成本次完整转换")
    return ident


def _verify_fork_after_restart(
    paths: Paths,
    source_binding: dict[str, Any],
    source_thread_id: str,
    fork_id: str,
    target_provider: str,
    expected_cwd: str | os.PathLike[str],
    *,
    executable: str | None,
    expected_turn_ids: list[str] | None,
    allow_additional_turns: bool = False,
) -> dict[str, Any]:
    """Reopen one durable fork through a fresh App Server connection."""

    client = _appserver_client(paths, executable)
    try:
        deadline = time.monotonic() + DEFAULT_APP_SERVER_TIMEOUT
        while True:
            snapshot = client.snapshot_thread(fork_id)
            quick_history = thread_history_projection_status(
                paths,
                fork_id,
                include_candidates=False,
                verify_rollout=False,
            )
            if quick_history is None or quick_history.get("health") == "unchecked":
                break
            health = str(quick_history.get("health") or "unknown")
            reason = str(quick_history.get("reason") or "unknown")
            if health in {"stalled", "unreadable"} or (
                health == "unknown"
                and reason
                not in {
                    "projection_cursor_missing",
                    "history_base_projection_incomplete",
                    "rollout_missing_or_unreadable",
                }
            ):
                raise RuntimeError(
                    f"fresh App Server projection validation failed: {reason}"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"fresh App Server projection did not reach EOF: {reason}"
                )
            time.sleep(0.1)
    finally:
        client.close()
    if snapshot.get("modelProvider") != target_provider:
        raise RuntimeError("fresh App Server read returned the wrong Provider")
    if _thread_cwd_key(snapshot.get("cwd")) != _thread_cwd_key(expected_cwd):
        raise RuntimeError("fresh App Server read returned the wrong working directory")
    binding = thread_provider_binding(paths, fork_id)
    rollout_path = binding.get("rollout_path")
    if isinstance(rollout_path, str) and rollout_path.strip() and Path(rollout_path).is_file():
        if _thread_parent_id(rollout_path) != source_thread_id:
            raise RuntimeError("fresh App Server read returned a fork with the wrong parent")
        history_base_matches = _history_base_matches_source(source_binding, binding)
        if history_base_matches is False:
            raise RuntimeError("fresh App Server read returned an incomplete history base")
    else:
        history_base_matches = None
    history = ensure_thread_history_readable(paths, fork_id)
    turn_ids = _projected_turn_ids(paths, fork_id)
    turn_sequence_matches = bool(
        expected_turn_ids is None
        or turn_ids == expected_turn_ids
        or (
            allow_additional_turns
            and turn_ids is not None
            and turn_ids[: len(expected_turn_ids)] == expected_turn_ids
        )
    )
    if not turn_sequence_matches:
        raise RuntimeError(
            "fresh App Server read returned a different turn sequence: "
            f"expected={len(expected_turn_ids)} actual={len(turn_ids or [])}"
        )
    return {
        "thread": snapshot,
        "history": history,
        "history_base_matches": history_base_matches,
        "turn_count": len(turn_ids) if turn_ids is not None else None,
        "last_turn_id": turn_ids[-1] if turn_ids else None,
    }


def fork_thread_provider(
    paths: Paths,
    thread_id: str,
    profile_id: str,
    *,
    expected_cwd: str | os.PathLike[str] | None = None,
    executable: str | None = None,
    blocker_probe: Any | None = None,
    reuse_existing: bool = True,
    archive_source: bool = False,
    target_model: str | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Continue one task on another provider under a cross-process lock."""

    with conversion_operation_lock(paths):
        return _fork_thread_provider_locked(
            paths,
            thread_id,
            profile_id,
            expected_cwd=expected_cwd,
            executable=executable,
            blocker_probe=blocker_probe,
            reuse_existing=reuse_existing,
            archive_source=archive_source,
            target_model=target_model,
            progress_callback=progress_callback,
        )


def _fork_thread_provider_locked(
    paths: Paths,
    thread_id: str,
    profile_id: str,
    *,
    expected_cwd: str | os.PathLike[str] | None = None,
    executable: str | None = None,
    blocker_probe: Any | None = None,
    reuse_existing: bool = True,
    archive_source: bool = False,
    target_model: str | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Locked implementation for a recoverable Provider fork."""

    def progress(message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)

    ensure_state(paths)
    source_binding = thread_provider_binding(paths, thread_id)
    # Selecting the task's already-bound profile is a true no-op.  It neither
    # requires closing Codex nor creates another task ID.
    if thread_binding_matches_profile(
        paths,
        source_binding,
        profile_id,
        model=target_model,
    ) and not bool(
        int(source_binding.get("archived") or 0)
    ):
        return {
            "source_thread": {
                "id": thread_id,
                "name": source_binding.get("name") or source_binding.get("title"),
                "cwd": source_binding.get("cwd"),
                "modelProvider": source_binding.get("provider_alias"),
            },
            "source_binding": source_binding,
            "thread": {
                "id": thread_id,
                "name": source_binding.get("name") or source_binding.get("title"),
                "cwd": source_binding.get("cwd"),
                "modelProvider": source_binding.get("provider_alias"),
            },
            "binding": source_binding,
            "provider_alias": source_binding.get("provider_alias"),
            "model": source_binding.get("model"),
            "profile": profile_by_id(load_profiles(paths), profile_id),
            "provider_version": None,
            "fork": None,
            "official_account_ready": profile_id == "official",
            "name_updated": False,
            "reused": True,
            "same_task": True,
            "cleanup": {
                "requested": False,
                "source_archived": False,
                "head_unarchived": True,
                "head_active": True,
                "source_unpinned": not bool(int(source_binding.get("is_pinned") or 0)),
                "head_pinned": bool(int(source_binding.get("is_pinned") or 0)),
                "complete": True,
                "warning": None,
            },
            "completion": {
                "provider_valid": True,
                "source_archived": None,
                "head_active": True,
                "head_pinned": bool(int(source_binding.get("is_pinned") or 0)),
                "navigation_launched": False,
                "core_complete": True,
                "complete": False,
            },
        }
    probe = blocker_probe or appserver_blocking_processes
    blockers = probe(paths)
    if blockers:
        raise RuntimeError(
            "copy-thread requires the Codex desktop/App Server to be closed; "
            f"active_processes={len(blockers)}"
        )
    progress("1/5 正在核验原任务及全部继承历史；不会改写聊天…")
    source_history = ensure_thread_history_readable(paths, thread_id)
    source_turn_ids = _projected_turn_ids(paths, thread_id)
    if source_history is not None and not source_turn_ids:
        raise UnreadableThreadHistoryError(
            thread_id,
            reason="source_turn_sequence_missing",
        )
    source_rollout_fingerprint: dict[str, Any] | None = None
    source_rollout_path = source_binding.get("rollout_path")
    if source_history is not None and isinstance(source_rollout_path, str):
        source_path = Path(source_rollout_path)
        integrity = source_history.get("rollout_integrity") or {}
        source_rollout_fingerprint = {
            "path": _thread_cwd_key(source_path),
            "name": source_path.name,
            "size": int(integrity["size"]),
            "sha256": str(integrity["sha256"]),
        }
    previous_config = paths.config.read_bytes() if paths.config.exists() else None
    previous_versions = paths.provider_versions.read_bytes() if paths.provider_versions.exists() else None
    runtime: dict[str, Any] | None = None
    client = None
    fork_id: str | None = None
    result: dict[str, Any] | None = None
    recovered_post_commit = False
    receipt = _conversion_receipts(paths)["pending"].get(thread_id)
    outcome_uncertain = bool(receipt)
    try:
        progress("2/5 正在准备所选配置和本地连接…")
        runtime = _prepare_profile_runtime(
            paths,
            profile_id,
            selected_model=target_model,
        )
        profile = runtime["profile"]
        target_provider = str(runtime["provider_alias"])
        target_model = runtime.get("model")
        client = _appserver_client(paths, executable)
        before = client.snapshot_thread(thread_id)
        cwd = expected_cwd or before["cwd"]
        if _thread_cwd_key(cwd) != _thread_cwd_key(source_binding.get("cwd")):
            raise RuntimeError("thread identity validation failed before fork")
        official_account_ready = False
        if profile.get("kind") == "official":
            account_state = client.read_account()
            account = account_state.get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise RuntimeError("尚未登录 ChatGPT 官方账号；请先使用“登录/切换官方账号”按钮")
            official_account_ready = isinstance(account, dict) and account.get("type") == "chatgpt"
            if not official_account_ready:
                raise RuntimeError("official ChatGPT account is not available")
        if receipt is not None:
            if receipt.get("provider") != target_provider:
                raise ConversionOutcomeUnknownError(thread_id, [receipt["created_thread_id"]] if receipt.get("created_thread_id") else [])
            progress("3/5 正在核对上次转换回执，不会再次创建…")
            fork_id = _reconcile_uncertain_receipt(paths, source_binding, receipt)
            reusable = thread_provider_binding(paths, fork_id)
            recovered_post_commit = True
        else:
            reusable = reusable_target_thread(paths, thread_id, target_provider) if reuse_existing else None
        if reusable is not None and reusable.get("thread_id") != thread_id:
            fork_id = str(reusable["thread_id"])
            forked = None
            reused = True
            progress("3/5 已找到可复用副本，正在核验…")
        else:
            receipt = {"provider": target_provider, "cwd": str(cwd),
                       "before_ids": _direct_target_ids(paths, thread_id, target_provider, str(cwd)),
                       "source_fingerprint": source_rollout_fingerprint,
                       "submitted_at": utc_now(), "state": "submitted"}
            _save_conversion_receipt(paths, thread_id, receipt)
            progress("3/5 正在创建分支（仅接收元数据，最长等待 180 秒）…")
            try:
                forked = client.fork_thread(
                    thread_id,
                    model_provider=target_provider,
                    cwd=cwd,
                    expected_cwd=cwd,
                    model=target_model,
                )
                fork_thread = forked.get("thread")
                fork_id = str(fork_thread.get("id") or "") if isinstance(fork_thread, dict) else ""
                if not fork_id or fork_id == thread_id:
                    raise RuntimeError("thread/fork did not create a distinct task")
            except Exception as exc:
                from appserver_client import AppServerProtocolError

                # Only protocol-level rejection before dispatch is known not
                # to commit. Internal errors and malformed/lost responses are
                # uncertain, even if they are not timeout-shaped.
                if isinstance(exc, AppServerProtocolError) and exc.code in {-32600, -32601, -32602}:
                    _save_conversion_receipt(paths, thread_id, None)
                    raise
                outcome_uncertain = True
                receipt["state"] = "awaiting_reconciliation"
                receipt["created_thread_id"] = getattr(exc, "created_thread_id", None)
                _save_conversion_receipt(paths, thread_id, receipt)
                # Late responses must not contaminate a subsequent request.
                client.close()
                client = None
                progress("3/5 响应未确认，正在只读核对已创建副本；不会重试创建…")
                try:
                    fork_id = _reconcile_uncertain_receipt(paths, source_binding, receipt)
                except Exception as recovery_error:
                    if receipt.get("created_thread_id"):
                        raise PartialThreadConversionError(receipt["created_thread_id"], str(recovery_error)) from exc
                    raise recovery_error from exc
                if probe(paths):
                    raise PartialThreadConversionError(fork_id, "Codex 已重新打开，请关闭后核验现有副本，不要重复创建")
                client = _appserver_client(paths, executable)
                forked = None
                recovered_post_commit = True
            reused = False
        if receipt is not None:
            receipt["state"] = "created"
            receipt["created_thread_id"] = fork_id
            _save_conversion_receipt(paths, thread_id, receipt)
        progress("4/5 正在核验 Provider、保留原任务并置顶新任务…")
        name_updated = False
        if isinstance(before.get("name"), str) and before["name"].strip():
            copied_name = before["name"].strip()
            try:
                client.set_thread_name(fork_id, copied_name)
                name_updated = True
            except Exception:
                # The provider binding and history copy are already durable.
                # A cosmetic naming failure must not turn a valid fork into a
                # misleading all-or-nothing failure.
                name_updated = False
        after_source = client.snapshot_thread(thread_id)
        if (
            before["id"] != after_source["id"]
            or before.get("name") != after_source.get("name")
            or before["cwd"] != after_source["cwd"]
            or before["modelProvider"] != after_source["modelProvider"]
        ):
            raise RuntimeError("source thread identity changed during fork")
        cleanup = _archive_source_keep_head(
            paths,
            client,
            thread_id,
            fork_id,
            enabled=archive_source,
        )
        created = client.snapshot_thread(fork_id)
        if created.get("modelProvider") != target_provider:
            raise RuntimeError(
                "thread/fork persisted modelProvider mismatch: "
                f"expected {target_provider}, got {created.get('modelProvider')}"
            )
        created_binding = thread_provider_binding(paths, fork_id)
        provider_valid = created_binding.get("provider_alias") == target_provider
        if not provider_valid:
            raise RuntimeError("Codex task index did not persist the forked provider")
        core_complete = bool(provider_valid and cleanup.get("complete"))
        result = {
            "source_thread": before,
            "source_binding": source_binding,
            "source_history": source_history,
            "thread": created,
            "binding": created_binding,
            "provider_alias": target_provider,
            "model": target_model,
            "profile": profile,
            "provider_version": runtime.get("provider_version"),
            "fork": forked,
            "official_account_ready": official_account_ready,
            "name_updated": name_updated,
            "reused": reused,
            "recovered_post_commit": recovered_post_commit,
            "same_task": False,
            "cleanup": cleanup,
            "completion": {
                "provider_valid": provider_valid,
                "source_archived": bool(cleanup.get("source_archived")),
                "head_active": bool(cleanup.get("head_active")),
                "head_pinned": bool(cleanup.get("head_pinned")),
                "navigation_launched": False,
                "core_complete": core_complete,
                "complete": False,
            },
        }
    except Exception as exc:
        if fork_id is not None and not isinstance(exc, PartialThreadConversionError):
            raise PartialThreadConversionError(fork_id, str(exc)) from exc
        raise
    finally:
        if client is not None:
            client.close()
        if fork_id is None and not outcome_uncertain:
            _restore_optional_file(paths.config, previous_config)
            _restore_optional_file(paths.provider_versions, previous_versions)
        else:
            try:
                restored = repair_config(paths)
            except Exception as exc:
                if fork_id is not None:
                    raise PartialThreadConversionError(fork_id, f"restoring default Provider failed: {exc}") from exc
                raise RuntimeError(f"转换结果仍待核验；恢复默认 Provider 失败：{exc}") from exc
            if result is not None:
                result["default_restored"] = restored
    assert result is not None
    if source_rollout_fingerprint is not None:
        current_source = thread_provider_binding(paths, thread_id)
        current_path = Path(str(current_source.get("rollout_path") or ""))
        if (
            current_path.name != source_rollout_fingerprint["name"]
            or (not archive_source and _thread_cwd_key(current_path) != source_rollout_fingerprint["path"])
            or current_path.stat().st_size != source_rollout_fingerprint["size"]
            or sha256_file(current_path) != source_rollout_fingerprint["sha256"]
        ):
            raise PartialThreadConversionError(fork_id or "unknown", "source rollout changed during fork")
        result["source_rollout_preserved"] = True
        result["source_rollout_sha256"] = source_rollout_fingerprint["sha256"]
    try:
        progress("5/5 正在用全新连接复核分段历史及轮次序列…")
        if probe(paths):
            raise RuntimeError("Codex 已重新打开，已暂停辅助连接；副本保留，等待核验")
        restart_verification = _verify_fork_after_restart(
            paths,
            source_binding,
            thread_id,
            fork_id or "",
            str(result["provider_alias"]),
            str(result["thread"].get("cwd") or source_binding.get("cwd") or ""),
            executable=executable,
            expected_turn_ids=source_turn_ids,
            allow_additional_turns=bool(result.get("reused")),
        )
    except Exception as exc:
        raise PartialThreadConversionError(fork_id or "unknown", str(exc)) from exc
    result["restart_verification"] = restart_verification
    result["completion"]["restart_verified"] = True
    result["completion"]["core_complete"] = bool(
        result["completion"].get("provider_valid")
        and result["cleanup"].get("complete")
        and result["completion"].get("restart_verified")
    )
    if result["completion"]["core_complete"]:
        _save_conversion_receipt(paths, thread_id, None)
    progress("核验完成；原任务未删除，新任务已保留。" if result["completion"]["core_complete"]
             else "新任务已保留，但整理尚未全部完成，请查看结果。")
    return result


def switch_thread_provider(
    paths: Paths,
    thread_id: str,
    profile_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility name for the durable fork-based migration."""

    return fork_thread_provider(paths, thread_id, profile_id, **kwargs)


def wait_and_fork_thread_provider(
    paths: Paths,
    thread_id: str,
    profile_id: str,
    *,
    expected_cwd: str | os.PathLike[str] | None = None,
    executable: str | None = None,
    process_probe: Any | None = None,
    poll_seconds: float = 1.0,
    timeout_seconds: float | None = 30 * 60,
    cancel_probe: Any | None = None,
    progress_fn: Any | None = None,
    target_model: str | None = None,
) -> dict[str, Any]:
    """Wait for Codex to close, then copy one task to a durable provider."""

    probe = process_probe or appserver_blocking_processes
    wait_result = wait_for_appserver_exit(
        paths,
        process_probe=probe,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        cancel_probe=cancel_probe,
        progress_fn=progress_fn,
    )
    result = fork_thread_provider(
        paths,
        thread_id,
        profile_id,
        expected_cwd=expected_cwd,
        executable=executable,
        blocker_probe=probe,
        target_model=target_model,
    )
    result["wait"] = wait_result
    return result


def wait_and_switch_thread_provider(
    paths: Paths,
    thread_id: str,
    profile_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility name for wait-and-fork behavior."""

    return wait_and_fork_thread_provider(paths, thread_id, profile_id, **kwargs)


def repair_config(paths: Paths) -> dict[str, Any]:
    """Reconcile config.toml with the already-selected switchboard profile.

    This command changes no task rows and does not increment the active
    profile revision.  It is useful after an external config edit or a Codex
    update that restored an older provider block.
    """
    ensure_state(paths)
    document = load_profiles(paths)
    active = load_active(paths)
    profile_id = str(active.get("profile_id") or "")
    profile = profile_by_id(document, profile_id)
    router = document.get("router", {})
    provider_alias = str(active.get("provider_alias") or "")
    versions = load_provider_versions(paths)
    if profile.get("kind") == "relay":
        version = provider_version_by_alias(versions, provider_alias)
        if version.get("profile_id") != profile_id:
            raise RuntimeError("active profile and provider version disagree")
        target_model = str(version.get("model") or "") or None
        catalog_result = _prepare_model_catalog_for_runtime(paths, profile_id, model=target_model)
    else:
        if provider_alias != "openai":
            raise RuntimeError("official active profile must use the openai provider")
        version = None
        target_model = None
        catalog_result = None
    result = update_config_provider(
        paths.config,
        profile_id,
        provider_alias=provider_alias,
        provider_versions=versions.get("versions", []),
        model=target_model,
        model_catalog_json=catalog_result["path"] if catalog_result else None,
        router_host=str(router.get("host", DEFAULT_ROUTER_HOST)),
        router_port=int(router.get("port", DEFAULT_ROUTER_PORT)),
    )
    return {
        "active": active,
        "profile": profile,
        "provider_version": version,
        "config": result,
        "model_catalog": catalog_result,
    }


def _mapping_contains_key(value: Any, target: str) -> bool:
    if not isinstance(value, dict):
        return False
    for key, child in value.items():
        if str(key) == target or _mapping_contains_key(child, target):
            return True
    return False


def _config_path_key(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return os.path.normcase(os.path.abspath(value.strip())).casefold()
    except (OSError, ValueError):
        return value.strip().casefold()


def config_projection_status(paths: Paths) -> dict[str, Any]:
    """Compare config.toml with active/profile owners without changing either."""

    reasons: list[str] = []
    if tomllib is None:
        return {"ready": False, "state": "invalid", "reasons": ["TOML parser unavailable"]}
    if not paths.config.exists():
        return {"ready": False, "state": "missing", "reasons": ["config.toml 不存在"]}
    try:
        parsed = tomllib.loads(paths.config.read_text(encoding="utf-8"))
        profiles = load_profiles(paths)
        active = load_active(paths)
        versions = load_provider_versions(paths)
        profile_id = str(active.get("profile_id") or "")
        profile = profile_by_id(profiles, profile_id)
    except (OSError, UnicodeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return {
            "ready": False,
            "state": "invalid",
            "reasons": [f"配置无法校验：{str(exc)[:160]}"],
        }
    provider_alias = str(active.get("provider_alias") or "")
    kind = str(profile.get("kind") or "")
    repairable = False
    top_provider = parsed.get("model_provider")
    top_model = parsed.get("model")
    top_catalog = parsed.get("model_catalog_json")
    if kind == "official":
        if provider_alias != "openai":
            reasons.append("官方 active Provider 不是 openai")
        else:
            repairable = True
        if "model_provider" in parsed:
            reasons.append("官方模式残留 model_provider 覆盖")
        if "model" in parsed:
            reasons.append("官方模式残留 relay 模型覆盖")
        if "model_catalog_json" in parsed:
            reasons.append("官方模式残留 relay 模型目录")
    elif kind == "relay":
        try:
            active_version = provider_version_by_alias(versions, provider_alias)
        except ValueError:
            active_version = {}
            reasons.append("active Provider 版本不存在")
        else:
            if str(active_version.get("profile_id") or "") != profile_id:
                reasons.append("active Provider 版本属于其他入口")
            else:
                repairable = True
        if top_provider != provider_alias:
            reasons.append("model_provider 与 active 版本不一致")
        expected_model = str(active_version.get("model") or "")
        if top_model != expected_model:
            reasons.append("默认模型与 active 版本不一致")
        expected_catalog = model_catalog_path(paths, profile_id)
        if _config_path_key(top_catalog) != _config_path_key(str(expected_catalog)):
            reasons.append("模型目录与 active 入口不一致")
        catalog = model_catalog_status(paths, profile_id, model=expected_model)
        if not catalog.get("ready"):
            reasons.append("active 中转站模型目录未就绪")
    else:
        reasons.append("active profile 类型不受支持")

    router = profiles.get("router", {})
    router_host = str(router.get("host", DEFAULT_ROUTER_HOST))
    router_port = int(router.get("port", DEFAULT_ROUTER_PORT))
    provider_tables = parsed.get("model_providers")
    provider_tables = provider_tables if isinstance(provider_tables, dict) else {}
    for version in versions.get("versions", []):
        if not isinstance(version, dict):
            continue
        alias = str(version.get("provider_alias") or "")
        if not alias:
            continue
        table = provider_tables.get(alias)
        if not isinstance(table, dict):
            reasons.append(f"Provider 定义缺失：{alias}")
            continue
        expected_route = f"http://{router_host}:{router_port}/profiles/{alias}/v1"
        if str(table.get("base_url") or "") != expected_route:
            reasons.append(f"Provider 路由漂移：{alias}")
        if table.get("wire_api") != "responses":
            reasons.append(f"Provider 协议漂移：{alias}")
        if table.get("requires_openai_auth") is not False:
            reasons.append(f"Provider 认证标志漂移：{alias}")
    if _mapping_contains_key(parsed, "experimental_bearer_token"):
        reasons.append("config.toml 中出现明文 relay 凭据字段")
    # Keep UI/log payload bounded even if a hand-edited config is pathological.
    reasons = reasons[:20]
    return {
        "ready": not reasons,
        "state": "synced" if not reasons else "drift",
        "profile_id": profile_id,
        "provider_alias": provider_alias,
        "reasons": reasons,
        "repairable": bool(profile_id and repairable),
    }


def normalized_upstream(base_url: str, request_path: str) -> tuple[str, str, int, bool]:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("profile base_url must be an http(s) URL")
    prefix = parsed.path.rstrip("/")
    path = request_path if request_path.startswith("/") else f"/{request_path}"
    if prefix.endswith("/v1") and path.startswith("/v1"):
        # Treat a provider URL ending in /v1 as the version prefix.  Codex's
        # request path also contains /v1, so remove only the request copy.
        # Removing both copies would incorrectly send /responses upstream.
        path = path[3:] or "/"
    upstream_path = f"{prefix}{path}" if prefix else path
    if parsed.query:
        upstream_path += f"?{parsed.query}"
    return parsed.hostname or "", upstream_path, parsed.port or (443 if parsed.scheme == "https" else 80), parsed.scheme == "https"


def resolve_router_target(paths: Paths, request_path: str) -> tuple[dict[str, Any], str]:
    """Resolve a local immutable provider route without consulting active.json."""

    parsed = urlsplit(request_path)
    path = unquote(parsed.path)
    if path == "/v1" or path.startswith("/v1/"):
        # Compatibility for App Servers that loaded the pre-0.4 custom URL.
        # ``custom`` is permanently the first Maylily snapshot, never the
        # mutable default profile.
        provider_alias = "custom"
        upstream_path = path
    else:
        match = re.fullmatch(r"/profiles/([A-Za-z0-9_-]{1,100})(/.*)?", path)
        if match is None:
            raise ValueError("unknown switchboard route")
        provider_alias = match.group(1)
        upstream_path = match.group(2) or "/"
    if parsed.query:
        upstream_path += f"?{parsed.query}"
    version = provider_version_by_alias(load_provider_versions(paths), provider_alias)
    if version.get("kind") != "relay":
        raise RuntimeError("provider route is not a relay")
    return version, upstream_path


class RouterHandler(BaseHTTPRequestHandler):
    server_version = "CodexSwitchboard/0.1"
    # EOF-delimited responses let us relay Responses/SSE incrementally without
    # buffering an entire model response in memory.  The upstream client still
    # receives the original Content-Type and status code.
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json_error(self, status: int, message: str) -> None:
        payload = json.dumps({"error": {"message": message, "type": "switchboard"}}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _health(self) -> None:
        """Answer a local liveness probe without touching the upstream.

        The desktop UI and launcher use this endpoint to decide whether the
        local router is ready.  It must remain side-effect free: forwarding a
        health request to a relay would both leak an unnecessary request and
        make a simple status refresh consume provider quota.
        """

        payload = json.dumps({"status": "ok", "service": "codex-switchboard"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _forward(self) -> None:
        paths: Paths = self.server.paths  # type: ignore[attr-defined]
        try:
            version, request_path = resolve_router_target(paths, self.path)
        except ValueError:
            self._json_error(404, "unknown switchboard provider route")
            return
        except Exception as exc:
            self._json_error(503, str(exc))
            return
        base_url = str(version.get("base_url") or "")
        if not base_url:
            self._json_error(503, "the provider version has no base_url")
            return
        key_ref = str(version.get("key_ref") or "")
        try:
            key = load_key(paths, key_ref)
            host, upstream_path, port, tls = normalized_upstream(base_url, request_path)
        except Exception as exc:
            self._json_error(503, str(exc))
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if self.headers.get("Transfer-Encoding"):
            self._json_error(411, "chunked request bodies are not supported")
            return
        if length > MAX_REQUEST_BYTES:
            self._json_error(413, "request body too large")
            return
        body = self.rfile.read(length) if length else b""
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            if name.lower() in {"host", "authorization", "content-length", "connection"}:
                continue
            headers[name] = value
        headers["Authorization"] = f"Bearer {key}"
        headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPSConnection(host, port, timeout=120) if tls else http.client.HTTPConnection(host, port, timeout=120)
        try:
            connection.request(self.command, upstream_path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            content_length: str | None = None
            for name, value in response.getheaders():
                lowered = name.lower()
                if lowered == "content-length":
                    content_length = value
                    continue
                if lowered in {"connection", "transfer-encoding", "keep-alive"}:
                    continue
                self.send_header(name, value)
            # Do not call response.read() without a size: SSE responses can
            # remain open for minutes and must be visible to the caller as they
            # arrive.  HTTP/1.0 closes the connection at EOF when no length is
            # known, which is valid for both regular and streaming responses.
            if content_length is not None:
                self.send_header("Content-Length", content_length)
            self.end_headers()
            while True:
                data = response.read(64 * 1024)
                if not data:
                    break
                self.wfile.write(data)
                self.wfile.flush()
        except Exception:
            # Headers may already have reached the client, so a second JSON
            # response would corrupt the stream.  Close the connection instead.
            if not self.wfile.closed:
                self.close_connection = True
        finally:
            connection.close()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] in {"/health", "/healthz"}:
            self._health()
            return
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_PUT(self) -> None:  # noqa: N802
        self._forward()

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward()

    def do_HEAD(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] in {"/health", "/healthz"}:
            self._health()
            return
        self._forward()


def run_router(paths: Paths) -> None:
    ensure_state(paths)
    document = load_profiles(paths)
    router = document.get("router", {})
    host = str(router.get("host", DEFAULT_ROUTER_HOST))
    port = int(router.get("port", DEFAULT_ROUTER_PORT))
    server = ThreadingHTTPServer((host, port), RouterHandler)
    server.paths = paths  # type: ignore[attr-defined]
    print(f"Codex Switchboard router listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def print_status(paths: Paths) -> None:
    document = load_profiles(paths)
    active = load_active(paths)
    active_profile = profile_by_id(document, active.get("profile_id", "maylily"))
    print(f"CODEX_HOME: {paths.codex_home}")
    print(f"switchboard: {paths.switchboard}")
    print(f"active profile: {active_profile['id']} ({active_profile.get('label', '')})")
    print(f"provider alias: {active.get('provider_alias') or 'legacy-unversioned'}")
    print(f"revision: {active.get('revision', 0)}")
    print(f"writer locks: {len(writer_locks(paths))}")
    print(f"config exists: {paths.config.exists()}")
    for profile in document.get("profiles", []):
        key_ref = profile.get("key_ref") or "-"
        key_state = "configured" if key_ref and key_path(paths, key_ref).exists() else "not configured"
        catalog = (
            model_catalog_status(paths, str(profile.get("id") or ""), model=str(profile.get("model") or ""))
            if profile.get("kind") == "relay"
            else {"state": "official"}
        )
        print(
            f"  - {profile.get('id')}: {profile.get('kind')} / "
            f"{profile.get('base_url') or '-'} / key={key_state} / "
            f"catalog={catalog.get('state')}"
        )


def ensure_state(paths: Paths) -> None:
    paths.switchboard.mkdir(parents=True, exist_ok=True)
    paths.keys.mkdir(parents=True, exist_ok=True)
    paths.backups.mkdir(parents=True, exist_ok=True)
    paths.model_catalogs.mkdir(parents=True, exist_ok=True)
    paths.runtime_tmp.mkdir(parents=True, exist_ok=True)
    if not paths.profiles.exists():
        atomic_write_json(paths.profiles, default_profiles())
    if not paths.provider_versions.exists():
        atomic_write_json(paths.provider_versions, {"version": 1, "versions": []})
    if not paths.active.exists():
        version = ensure_provider_version(paths, "maylily")
        atomic_write_json(
            paths.active,
            {
                "profile_id": "maylily",
                "provider_alias": version["provider_alias"],
                "version_id": version["version_id"],
                "model": version.get("model"),
                "revision": 0,
                "changed_at": utc_now(),
            },
        )
    else:
        active = load_active(paths)
        if not active.get("provider_alias"):
            profile = profile_by_id(load_profiles(paths), str(active.get("profile_id") or "maylily"))
            if profile.get("kind") == "relay":
                version = ensure_provider_version(paths, str(profile["id"]))
                active["provider_alias"] = version["provider_alias"]
                active["version_id"] = version["version_id"]
                active["model"] = version.get("model")
            else:
                active["provider_alias"] = "openai"
                active["version_id"] = "openai"
                active["model"] = None
            atomic_write_json(paths.active, active)


def initialize(paths: Paths) -> None:
    ensure_state(paths)
    print_status(paths)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E-drive Codex account/provider switchboard")
    parser.add_argument("--home", type=Path, default=DEFAULT_CODEX_HOME, help="fixed CODEX_HOME (default: E:\\Codex-Home)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("plan-migration")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--execute", action="store_true", help="perform the offline migration")
    migrate.add_argument(
        "--wait-for-exit",
        action="store_true",
        help="wait for Codex/project writers to exit, then execute the migration",
    )
    migrate.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="poll interval used with --wait-for-exit (default: 5)",
    )
    migrate.add_argument(
        "--start-router-after",
        action="store_true",
        help="start the local relay router after a successful migration",
    )
    switch = sub.add_parser("switch-provider")
    switch.add_argument("profile_id")
    switch.add_argument("--model", help="select one configured relay model")
    switch.add_argument("--force", action="store_true")
    for command_name in ("copy-thread", "switch-thread"):
        thread_copy = sub.add_parser(
            command_name,
            help="copy a task into a durable Provider binding" if command_name == "copy-thread" else argparse.SUPPRESS,
        )
        thread_copy.add_argument("thread_id")
        thread_copy.add_argument("profile_id")
        thread_copy.add_argument("--cwd")
        thread_copy.add_argument("--executable")
        thread_copy.add_argument("--model", help="select one configured relay model")
        thread_copy.add_argument(
            "--wait-for-exit",
            action="store_true",
            help="wait for the Codex desktop/App Server to close before copying",
        )
        thread_copy.add_argument("--poll-seconds", type=float, default=1.0)
        thread_copy.add_argument("--timeout-seconds", type=float, default=30 * 60)
    binding = sub.add_parser("thread-binding", help="read one task's persisted Provider binding")
    binding.add_argument("thread_id")
    relay_config = sub.add_parser("configure-relay")
    relay_config.add_argument("profile_id")
    relay_config.add_argument("--base-url", required=True)
    relay_config.add_argument("--model", required=True)
    relay_config.add_argument("--models", help="comma-separated relay model allow-list")
    relay_config.add_argument("--disabled", action="store_true")
    sub.add_parser("repair-config", help="reconcile config.toml with active profile")
    catalog = sub.add_parser("prepare-model-catalog", help="generate a relay model catalog from the installed Codex runtime")
    catalog.add_argument("profile_id")
    catalog.add_argument("--models", help="comma-separated model allow-list override")
    adopt = sub.add_parser("adopt-legacy-key", help="encrypt legacy config bearer token with DPAPI")
    adopt.add_argument("--profile-id")
    key = sub.add_parser("set-key")
    key.add_argument("key_ref")
    key.add_argument("--value", help=argparse.SUPPRESS)
    sub.add_parser("router")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    paths = Paths(args.home.resolve())
    if args.command == "init":
        initialize(paths)
        return 0
    if args.command == "status":
        print_status(paths)
        return 0
    if args.command == "plan-migration":
        manifest = build_migration_manifest(paths)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    if args.command == "migrate":
        if not args.execute:
            print(json.dumps(build_migration_manifest(paths), ensure_ascii=False, indent=2))
            print("dry-run only; rerun with --execute after closing Codex and project writers")
            return 0
        if args.wait_for_exit:
            result = wait_for_migration(paths, poll_seconds=args.poll_seconds)
        else:
            result = execute_migration(paths)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        if args.start_router_after:
            run_router(paths)
        return 0
    if args.command == "switch-provider":
        result = set_active_profile(paths, args.profile_id, force=args.force, model=args.model)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"copy-thread", "switch-thread"}:
        if args.wait_for_exit:
            result = wait_and_fork_thread_provider(
                paths,
                args.thread_id,
                args.profile_id,
                expected_cwd=args.cwd,
                executable=args.executable,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
                target_model=args.model,
            )
        else:
            result = fork_thread_provider(
                paths,
                args.thread_id,
                args.profile_id,
                expected_cwd=args.cwd,
                executable=args.executable,
                target_model=args.model,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "thread-binding":
        print(json.dumps(thread_provider_binding(paths, args.thread_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "repair-config":
        print(json.dumps(repair_config(paths), ensure_ascii=False, indent=2))
        return 0
    if args.command == "prepare-model-catalog":
        result = prepare_model_catalog(paths, args.profile_id, models=args.models)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "configure-relay":
        result = configure_relay_profile(
            paths,
            args.profile_id,
            base_url=args.base_url,
            model=args.model,
            models=args.models,
            enabled=not args.disabled,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "adopt-legacy-key":
        result = adopt_legacy_bearer_key(paths, args.profile_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "set-key":
        secret = args.value if args.value is not None else getpass.getpass(f"Enter secret for {args.key_ref}: ")
        store_key(paths, args.key_ref, secret)
        print(f"stored encrypted key: {args.key_ref}")
        return 0
    if args.command == "router":
        run_router(paths)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        raise SystemExit(130)
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
        # User-facing commands should report a bounded operational error
        # instead of a traceback containing local implementation details.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)

