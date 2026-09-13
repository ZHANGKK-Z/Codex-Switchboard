"""One fail-closed, offline metadata-duplicate projection repair.

Codex owns rollout content, task metadata and projection state.  This module
owns no task state: a plan is a frozen authorization snapshot and a result is
only maintenance evidence.  The only production mutation is one segment's
next_rollout_byte_offset, guarded by CAS.  No App Server or Provider is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import switchboard


MAX_HISTORY_BYTES = 512 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_SEGMENTS = 256
MAX_INDEX_FILES = 200_000
PLAN_VERSION = 1
KIND = "metadata_duplicate_cursor_v1"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_PROCESS_NAMES = {"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"}
_DELETE_TRIGGER = ("CREATE TRIGGER thread_realtime_items_projection_cleanup AFTER DELETE ON "
                   "thread_history_projection_state BEGIN DELETE FROM thread_realtime_items "
                   "WHERE thread_id = OLD.thread_id; END")


class RecoveryError(RuntimeError):
    """Only a fixed public code crosses the UI boundary, never source content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise RecoveryError(code)


def _canonical(value: Any) -> bytes:
    def encode(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"blob_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
        raise TypeError("unsupported snapshot value")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=encode).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def _ident(value: Any) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _path(value: Any) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        _fail("invalid_path")
    text = os.fspath(value)
    if text.casefold().startswith("\\\\?\\unc\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    path = Path(text)
    if not path.is_absolute():
        _fail("path_not_absolute")
    return path


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _safe_path(path: Path, *, file: bool = False, directory: bool = False,
               missing: bool = False) -> None:
    # Check BEFORE resolve: resolving first loses evidence of a parent junction.
    for parent in (path, *path.parents):
        if _linked(parent):
            _fail("linked_path")
        if parent.exists() and parent != path and not parent.is_dir():
            _fail("invalid_path_parent")
    if path.exists():
        if file and (not path.is_file() or path.stat().st_nlink != 1):
            _fail("not_unique_regular_file")
        if directory and not path.is_dir():
            _fail("not_directory")
        if _key(path.resolve()) != _key(path):
            _fail("linked_path")
    elif not missing:
        _fail("path_missing")


def _validate_paths(paths: switchboard.Paths) -> None:
    _safe_path(_path(paths.codex_home), directory=True)
    _safe_path(paths.codex_home / "sessions", directory=True)
    for db in (paths.state_database, paths.thread_history_database):
        _safe_path(db, file=True)
        for suffix in ("-wal", "-shm", "-journal"):
            _safe_path(Path(str(db) + suffix), file=True, missing=True)


def _ro(path: Path) -> sqlite3.Connection:
    _safe_path(path, file=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _quick_check(connection: sqlite3.Connection) -> None:
    if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
        _fail("database_integrity_failed")


def _schema(state: sqlite3.Connection, history: sqlite3.Connection) -> str:
    specs = ((state, "threads", {"id": "TEXT", "rollout_path": "TEXT", "name": "TEXT",
                               "cwd": "TEXT", "model_provider": "TEXT", "model": "TEXT",
                               "archived": "INTEGER"}),
             (history, "thread_history_projection_state", {"thread_id": "TEXT",
                           "next_rollout_byte_offset": "INTEGER", "next_rollout_ordinal": "INTEGER"}),
             (history, "thread_turns", {"thread_id": "TEXT", "turn_id": "TEXT", "status": "TEXT",
                                       "rollout_ordinal": "INTEGER", "rollout_end_ordinal": "INTEGER"}),
             (history, "thread_items", {"thread_id": "TEXT", "item_type": "TEXT",
                                       "rollout_ordinal": "INTEGER", "item_json": "TEXT"}))
    fingerprints = []
    for connection, table, required in specs:
        entries = [tuple(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name=? ORDER BY type,name", (table,))]
        tables = [entry for entry in entries if entry[0] == "table" and entry[1] == table]
        if len(tables) != 1 or not str(tables[0][3]).lstrip().upper().startswith("CREATE TABLE"):
            _fail("unsupported_schema")
        columns = {row[1]: tuple(row) for row in connection.execute(f"PRAGMA table_xinfo({table})")}
        if any(name not in columns or str(columns[name][2]).upper() != dtype
               or columns[name][6] != 0 for name, dtype in required.items()):
            _fail("unsupported_schema")
        if table in {"threads", "thread_history_projection_state"}:
            key = "id" if table == "threads" else "thread_id"
            if [(row[1], row[5]) for row in columns.values() if row[5]] != [(key, 1)]:
                _fail("unsupported_primary_key")
        if table == "thread_history_projection_state":
            if set(columns) != set(required):
                _fail("unsupported_projection_columns")
            for entry in entries:
                if entry[0] == "trigger" and " ".join(str(entry[3]).split()) != _DELETE_TRIGGER:
                    _fail("unexpected_projection_trigger")
            if list(connection.execute(f"PRAGMA foreign_key_list({table})")):
                _fail("unsupported_projection_foreign_key")
        fingerprints.append({"table": table, "entries": entries,
                             "columns": list(columns.values())})
    return _hash(fingerprints)


def _state_row(state: sqlite3.Connection, root: str) -> dict:
    rows = state.execute("SELECT * FROM threads WHERE id=?", (root,)).fetchall()
    if len(rows) != 1:
        _fail("task_missing_or_ambiguous")
    row = dict(rows[0])
    if row.get("archived") != 0:
        _fail("archived_task_unsupported")
    return row


def _cursor(history: sqlite3.Connection, segment: str) -> tuple[int, int]:
    rows = history.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal "
                           "FROM thread_history_projection_state WHERE thread_id=?", (segment,)).fetchall()
    if len(rows) != 1 or not all(_integer(value) for value in rows[0]):
        _fail("projection_cursor_missing_or_invalid")
    return tuple(rows[0])


def _segment(path: Path) -> str:
    match = _UUID.search(path.stem[-36:])
    if not path.name.startswith("rollout-") or not match or path.suffix.lower() != ".jsonl":
        _fail("rollout_filename_invalid")
    return match.group(0)


def _source_path(paths: switchboard.Paths, path: Path) -> None:
    _safe_path(path, file=True)
    for folder in ("sessions", "archived_sessions"):
        root = paths.codex_home / folder
        try:
            if os.path.commonpath((_key(root), _key(path))) == _key(root):
                return
        except ValueError:
            pass
    _fail("rollout_outside_home")


def _cancel(event: Any) -> None:
    if event is not None and event.is_set():
        _fail("cancelled")


def _record(raw: bytes) -> dict:
    if not raw.endswith(b"\n") or len(raw) > MAX_RECORD_BYTES:
        _fail("incomplete_or_oversized_record")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("duplicate_json_key")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique_object,
                           parse_constant=lambda _value: _fail("invalid_json_number"))
    except (ValueError, UnicodeError):
        _fail("invalid_rollout_json")
    if not isinstance(value, dict) or not _integer(value.get("ordinal")):
        _fail("invalid_rollout_record")
    return value


def _scan(path: Path, *, cursor: tuple[int, int] | None, cutoff: int | None,
          expected_end: int | None, budget: list[int], cancel_event: Any = None) -> dict:
    """Scan once, retaining hashes/identities only; never retain message bodies."""
    before = path.stat()
    size = before.st_size
    budget[0] += size
    if size <= 0 or budget[0] > MAX_HISTORY_BYTES:
        _fail("history_size_limit")
    if cutoff is not None and (not _integer(cutoff) or not 0 < cutoff <= size):
        _fail("ancestor_cutoff_invalid")
    digest = hashlib.sha256()
    previous = None
    expected = None
    meta = None
    duplicate = None
    repeated = None
    completed = []
    completed_ids = set()
    agent_messages = []
    final = None
    boundary_ordinal = None
    count = 0
    with path.open("rb") as handle:
        while True:
            _cancel(cancel_event)
            start = handle.tell()
            raw = handle.readline(MAX_RECORD_BYTES + 1)
            if not raw:
                break
            digest.update(raw)
            item = _record(raw)
            actual = item["ordinal"]
            payload = item.get("payload")
            if expected is None:
                if item.get("type") != "session_meta" or not isinstance(payload, dict) or not _ident(payload.get("id")):
                    _fail("invalid_session_metadata")
                owner = payload["id"]
                segment = _segment(path)
                if not (path.stem.endswith(owner) or path.stem.endswith(owner + "_" + segment)):
                    _fail("rollout_owner_mismatch")
                base = payload.get("history_base")
                if base is not None:
                    if (not isinstance(base, dict) or set(base) != {"thread_id", "end_byte_offset", "end_ordinal_exclusive"}
                            or not _ident(base.get("thread_id")) or not _integer(base.get("end_byte_offset"))
                            or base["end_byte_offset"] <= 0 or base.get("end_ordinal_exclusive") != actual):
                        _fail("invalid_history_base")
                elif actual != 0:
                    _fail("unanchored_history_root")
                meta = {"owner": owner, "history_base": base, "first_ordinal": actual}
                expected = actual
            if actual != expected:
                old = previous.get("payload") if previous else None
                if (cursor is None or duplicate is not None or start != cursor[0] or expected != cursor[1]
                        or actual != expected - 1 or not previous or previous.get("ordinal") != actual
                        or previous.get("type") != "event_msg" or item.get("type") != "event_msg"
                        or not isinstance(old, dict) or old.get("type") != "token_count"
                        or not isinstance(payload, dict) or payload.get("type") != "thread_settings_applied"):
                    _fail("unsupported_ordinal_anomaly")
                duplicate = {"old_offset": start, "new_offset": handle.tell(),
                             "next_ordinal": expected, "settings_sha256": _hash(payload)}
            else:
                expected += 1
                if (duplicate is not None and repeated is None and item.get("type") == "event_msg"
                        and isinstance(payload, dict) and payload.get("type") == "thread_settings_applied"
                        and _hash(payload) == duplicate["settings_sha256"]):
                    repeated = actual
            included = cutoff is None or handle.tell() <= cutoff
            if included and item.get("type") == "event_msg" and isinstance(payload, dict):
                if payload.get("type") == "task_complete":
                    turn = payload.get("turn_id")
                    if not _ident(turn) or turn in completed_ids:
                        _fail("invalid_completed_turn_identity")
                    completed_ids.add(turn)
                    completed.append({"turn_id": turn, "end_ordinal": actual})
                native = payload.get("item")
                if payload.get("type") == "item_completed" and isinstance(native, dict) and native.get("type") == "AgentMessage":
                    content = native.get("content")
                    if not isinstance(content, list):
                        _fail("invalid_native_agent_message")
                    if any(not isinstance(part, dict) or (part.get("type") == "Text"
                           and not isinstance(part.get("text"), str)) for part in content):
                        _fail("invalid_native_agent_message")
                    parts = [part["text"] for part in content if part.get("type") == "Text"]
                    text = "".join(parts)
                    if not text:
                        _fail("native_agent_text_unverifiable")
                    final = {"ordinal": actual, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                    agent_messages.append(final)
            if cutoff is not None and handle.tell() == cutoff:
                boundary_ordinal = expected
            previous = item
            count += 1
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        _fail("rollout_changed_during_scan")
    if cutoff is not None and boundary_ordinal != expected_end:
        _fail("ancestor_cutoff_ordinal_mismatch")
    if cursor is not None and (duplicate is None or repeated is None):
        _fail("no_supported_duplicate_with_identical_repeat")
    if cursor is not None and (not completed or final is None or final["ordinal"] < cursor[1]):
        _fail("terminal_native_evidence_missing")
    return {"path": str(path), "segment_id": _segment(path), "size": size,
            "sha256": digest.hexdigest(), "eof_ordinal": expected, "records": count,
            "metadata": meta, "cutoff": cutoff, "cutoff_ordinal": expected_end,
            "duplicate": duplicate, "settings_repeat_ordinal": repeated,
            "completed_turns": completed, "agent_messages": agent_messages, "final_message": final}


def _locate(paths: switchboard.Paths, segment: str, index: dict, event: Any) -> Path:
    if not index:
        count = 0
        for folder in ("sessions", "archived_sessions"):
            root = paths.codex_home / folder
            if not root.exists():
                continue
            _safe_path(root, directory=True)
            for directory, subdirs, files in os.walk(root, followlinks=False):
                _cancel(event)
                subdirs[:] = [name for name in subdirs if not _linked(Path(directory) / name)]
                for name in files:
                    count += 1
                    if count > MAX_INDEX_FILES:
                        _fail("history_index_limit")
                    if name.endswith(".jsonl"):
                        path = Path(directory) / name
                        try:
                            ident = _segment(path)
                        except RecoveryError:
                            continue
                        index.setdefault(ident, []).append(path)
        index["_loaded"] = True
    matches = index.get(segment, [])
    if len(matches) != 1:
        _fail("ancestor_missing_or_ambiguous")
    return matches[0]


def _build(paths: switchboard.Paths, root: str, *, frozen: dict | None = None,
           cancel_event: Any = None, history_connection: sqlite3.Connection | None = None) -> dict:
    _validate_paths(paths)
    if not _ident(root):
        _fail("invalid_task_id")
    state = _ro(paths.state_database)
    history = history_connection or _ro(paths.thread_history_database)
    try:
        schema = _schema(state, history)
        _quick_check(state)
        _quick_check(history)
        row = _state_row(state, root)
        path = _path(row.get("rollout_path"))
        _source_path(paths, path)
        segment = _segment(path)
        old_cursor = _cursor(history, segment) if frozen is None else (
            frozen["cursor"]["old_offset"], frozen["cursor"]["next_ordinal"])
        source = _scan(path, cursor=old_cursor, cutoff=None, expected_end=None,
                       budget=(budget := [0]), cancel_event=cancel_event)
        if source["metadata"]["owner"] != root:
            _fail("task_rollout_identity_mismatch")
        ancestors = []
        child = source
        seen = {segment}
        index: dict = {}
        while child["metadata"]["history_base"] is not None:
            base = child["metadata"]["history_base"]
            ident = base["thread_id"]
            if ident in seen or len(seen) >= MAX_SEGMENTS:
                _fail("history_cycle_or_depth_limit")
            seen.add(ident)
            ancestor_path = _locate(paths, ident, index, cancel_event)
            _source_path(paths, ancestor_path)
            ancestor = _scan(ancestor_path, cursor=None, cutoff=base["end_byte_offset"],
                             expected_end=base["end_ordinal_exclusive"], budget=budget, cancel_event=cancel_event)
            if _cursor(history, ident) != (ancestor["size"], ancestor["eof_ordinal"]):
                _fail("ancestor_projection_not_at_eof")
            ancestors.append(ancestor)
            child = ancestor
        plan = {"version": PLAN_VERSION, "kind": KIND, "home": str(paths.codex_home),
                "root_task_id": root, "segment_id": segment, "rollout_path": str(path),
                "state_sha256": _hash(row), "schema_sha256": schema,
                "source": source, "ancestors": ancestors,
                "cursor": {name: source["duplicate"][name] for name in ("old_offset", "new_offset", "next_ordinal")}}
        plan["plan_sha256"] = _hash(plan)
        # Close the read-only sampling window with another identity and cursor read.
        if _hash(_state_row(state, root)) != plan["state_sha256"] or _schema(state, history) != schema:
            _fail("database_changed_during_scan")
        if frozen is None and _cursor(history, segment) != old_cursor:
            _fail("cursor_changed_during_scan")
        return plan
    finally:
        state.close()
        if history_connection is None:
            history.close()


def _validated_plan(paths: switchboard.Paths, plan: Any) -> dict:
    if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION or plan.get("kind") != KIND:
        _fail("invalid_plan")
    try:
        claimed = plan["plan_sha256"]
        body = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if _hash(body) != claimed or _key(_path(plan["home"])) != _key(paths.codex_home):
            _fail("plan_fingerprint_mismatch")
        if not _ident(plan["root_task_id"]) or not _ident(plan["segment_id"]):
            _fail("invalid_plan")
        cursor = plan["cursor"]
        if set(cursor) != {"old_offset", "new_offset", "next_ordinal"} or not all(_integer(x) for x in cursor.values()):
            _fail("invalid_plan")
        if not 0 < cursor["old_offset"] < cursor["new_offset"] < plan["source"]["size"]:
            _fail("invalid_plan")
        return json.loads(json.dumps(plan))
    except (KeyError, TypeError, ValueError):
        _fail("invalid_plan")


def _same_plan(paths: switchboard.Paths, plan: dict, history: sqlite3.Connection | None = None) -> None:
    current = _build(paths, plan["root_task_id"], frozen=plan, history_connection=history)
    if current != plan:
        _fail("frozen_plan_changed")


def preview_recovery(paths: switchboard.Paths, thread_id: str, *, cancel_event: Any = None) -> dict:
    """Read-only discovery; unknown anomalies yield no executable plan."""
    try:
        plan = _build(paths, thread_id, cancel_event=cancel_event)
        return {"supported": True, "reason": "supported_metadata_duplicate",
                "message": "发现可核验的唯一元数据重复；恢复需退出全部 Codex，并先备份。", "plan": plan}
    except (RecoveryError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        # Details from SQLite/paths/JSON can contain private data; only a fixed code escapes.
        reason = exc.code if isinstance(exc, RecoveryError) else "inspection_failed"
        return {"supported": False, "reason": reason,
                "message": "未满足窄恢复条件；保持原状，仅允许只读排查。", "plan": None}


def strict_process_probe(_paths: switchboard.Paths | None = None) -> list[dict]:
    """Reuse native enumeration, without its permissive row parsing/PID ignore."""
    try:
        lines = switchboard.process_lines(strict=True)
        if not isinstance(lines, list) or not lines:
            _fail("process_inspection_failed")
        blocked = []
        for line in lines:
            row = json.loads(line)
            if (not isinstance(row, dict) or not isinstance(row.get("Name"), str) or not row["Name"]
                    or type(row.get("ProcessId")) is not int or row["ProcessId"] < 0):
                _fail("process_inspection_failed")
            if row["Name"].casefold() in _PROCESS_NAMES:
                blocked.append({"name": row["Name"].casefold(), "pid": row["ProcessId"]})
        return blocked
    except (OSError, RuntimeError, ValueError, TypeError):
        _fail("process_inspection_failed")


def _quiescent(paths: switchboard.Paths, probe: Callable) -> None:
    try:
        blockers = probe(paths)
    except Exception:
        _fail("process_inspection_failed")
    if not isinstance(blockers, list):
        _fail("process_inspection_failed")
    if blockers:
        _fail("codex_still_running")
    directory = paths.codex_home / "thread-writer-locks"
    _safe_path(directory, directory=True, missing=True)
    if directory.exists():
        for path in directory.glob("*.lock"):
            _safe_path(path, file=True)
            if path.stat().st_size:
                _fail("writer_lock_active")


def blocking_writers(paths: switchboard.Paths) -> list[dict]:
    """Strict read-only wait probe: unknown enumeration raises, never means idle."""
    _validate_paths(paths)
    result = strict_process_probe(paths)
    directory = paths.codex_home / "thread-writer-locks"
    _safe_path(directory, directory=True, missing=True)
    if directory.exists():
        for path in directory.glob("*.lock"):
            _safe_path(path, file=True)
            if path.stat().st_size:
                result.append({"kind": "writer_lock", "name": path.name})
    return result


def _backup(paths: switchboard.Paths, source: Path, label: str) -> str:
    """Native backup includes WAL; exclusive destination never follows a link."""
    _safe_path(paths.backups, directory=True, missing=True)
    paths.backups.mkdir(parents=True, exist_ok=True)
    _safe_path(paths.backups, directory=True)
    target = paths.backups / f"projection-recovery-{label}-{time.time_ns()}-{uuid.uuid4().hex}.sqlite"
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    src = _ro(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst, pages=1000, sleep=0.05)
        dst.commit()
        _quick_check(dst)
    finally:
        src.close()
        dst.close()
    return str(target)


def _verify_backups(paths: switchboard.Paths, plan: dict, backups: list[str]) -> None:
    state = _ro(Path(backups[0]))
    history = _ro(Path(backups[1]))
    try:
        if _schema(state, history) != plan["schema_sha256"] or _hash(_state_row(state, plan["root_task_id"])) != plan["state_sha256"]:
            _fail("backup_snapshot_mismatch")
        cursor = plan["cursor"]
        if _cursor(history, plan["segment_id"]) != (cursor["old_offset"], cursor["next_ordinal"]):
            _fail("backup_cursor_mismatch")
    finally:
        state.close()
        history.close()


@contextmanager
def _source_read_guard(plan: dict):
    """On Windows deny writes/deletes to each authorized source through commit.

    This is an OS sharing restriction, not a second task lock. Handles are
    released on scope/process exit. A pre-existing writer makes acquisition
    fail. Non-Windows fixtures retain validation but cannot claim this guard.
    """
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handles = []
    try:
        for part in [plan["source"], *plan["ancestors"]]:
            path = _path(part["path"])
            _safe_path(path, file=True)
            handle = kernel.CreateFileW(str(path), 0x80000000, 0x00000001, None, 3, 0x80, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                _fail("source_write_guard_unavailable")
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def _materialized(history: sqlite3.Connection, plan: dict) -> dict:
    verified = 0
    messages_verified = 0
    for part in [plan["source"], *plan["ancestors"]]:
        for turn in part["completed_turns"]:
            rows = history.execute("SELECT status,rollout_end_ordinal FROM thread_turns "
                                   "WHERE thread_id=? AND turn_id=?", (part["segment_id"], turn["turn_id"])).fetchall()
            if len(rows) != 1 or tuple(rows[0]) != ("completed", turn["end_ordinal"]):
                _fail("completed_turn_not_materialized")
            verified += 1
        for message in part["agent_messages"]:
            # Preserve the established final-message code while distinguishing
            # an earlier missing native message. Every visible message matters.
            error = "final_message_not_materialized" if message == part["final_message"] else "agent_message_not_materialized"
            rows = history.execute("SELECT item_json FROM thread_items WHERE thread_id=? "
                                   "AND item_type='agentMessage' AND rollout_ordinal=?",
                                   (part["segment_id"], message["ordinal"])).fetchall()
            if len(rows) != 1:
                _fail(error)
            try:
                item = json.loads(rows[0][0])
                text = item.get("text") if isinstance(item, dict) else None
            except (TypeError, ValueError):
                _fail(error)
            if not isinstance(text, str) or hashlib.sha256(text.encode("utf-8")).hexdigest() != message["text_sha256"]:
                _fail(error)
            messages_verified += 1
    return {"completed_turns_verified": verified, "agent_messages_verified": messages_verified,
            "final_message_verified": True}


def reconcile_plan(paths: switchboard.Paths, plan: dict) -> dict:
    """Observe frozen-source CAS/native replay states; never retry or restore."""
    try:
        frozen = _validated_plan(paths, plan)
        _same_plan(paths, frozen)
        history = _ro(paths.thread_history_database)
        try:
            # Cursor, turns and messages must come from one SQLite read
            # snapshot; mixing separately committed observations can fake EOF.
            history.execute("BEGIN")
            cursor = _cursor(history, frozen["segment_id"])
            expected = frozen["cursor"]
            if cursor == (expected["old_offset"], expected["next_ordinal"]):
                status, evidence = "not_applied", {}
            elif cursor == (expected["new_offset"], expected["next_ordinal"]):
                status, evidence = "cursor_repaired_pending_replay", {}
            elif cursor == (frozen["source"]["size"], frozen["source"]["eof_ordinal"]):
                status, evidence = "verified", _materialized(history, frozen)
            else:
                _fail("unknown_projection_cursor")
            if _cursor(history, frozen["segment_id"]) != cursor:
                _fail("cursor_changed_during_verification")
            # Source/state/schema are outside that history snapshot. Verify
            # their immutable authorization evidence again after materialized
            # rows have been read, and don't accept a scan-start-only proof.
            _same_plan(paths, frozen)
            history.commit()
            return {"status": status, "cursor": {"offset": cursor[0], "ordinal": cursor[1]},
                    "provider_requests": 0, "source_modified": False, **evidence}
        finally:
            history.close()
    except (RecoveryError, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        return {"status": "needs_review", "reason": exc.code if isinstance(exc, RecoveryError) else "reconciliation_failed",
                "provider_requests": 0, "automatic_retry": False}


def execute_plan(paths: switchboard.Paths, plan: dict, *, process_probe: Callable | None = None,
                 backup_ready: Callable[[list[str]], Any] | None = None) -> dict:
    """Execute once while offline. Caller must persist backup intent before CAS.

    The existing conversion lock serializes this with other Switchboard writes;
    it is not a claim to lock Codex. Process checks + SQLite transaction/CAS
    provide the fail-closed boundary, and native reopening remains user-owned.
    """
    backups: list[str] = []
    attempted = False
    committed = False
    try:
        frozen = _validated_plan(paths, plan)
        _validate_paths(paths)
        if backup_ready is None:
            _fail("backup_intent_callback_required")
        probe = process_probe if process_probe is not None else strict_process_probe
        _quiescent(paths, probe)
        with switchboard.conversion_operation_lock(paths):
            _quiescent(paths, probe)
            _same_plan(paths, frozen)
            current = _ro(paths.thread_history_database)
            try:
                old = frozen["cursor"]
                if _cursor(current, frozen["segment_id"]) != (old["old_offset"], old["next_ordinal"]):
                    _fail("already_applied_or_unknown_cursor")
            finally:
                current.close()
            for source, label in ((paths.state_database, "state"), (paths.thread_history_database, "history")):
                _quiescent(paths, probe)
                backups.append(_backup(paths, source, label))
            _verify_backups(paths, frozen, backups)
            _quiescent(paths, probe)
            # Failure here is a pre-commit failure, never permission to continue.
            backup_ready(list(backups))
            _quiescent(paths, probe)
            _validate_paths(paths)
            with _source_read_guard(frozen):
                history = sqlite3.connect(paths.thread_history_database, timeout=10, isolation_level=None)
                history.row_factory = sqlite3.Row
                try:
                    history.execute("PRAGMA busy_timeout=10000")
                    history.execute("BEGIN IMMEDIATE")
                    _quiescent(paths, probe)
                    _same_plan(paths, frozen, history)
                    _quiescent(paths, probe)
                    if _cursor(history, frozen["segment_id"]) != (old["old_offset"], old["next_ordinal"]):
                        _fail("cursor_cas_conflict")
                    attempted = True
                    changed = history.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=? "
                                              "WHERE thread_id=? AND next_rollout_byte_offset=? AND next_rollout_ordinal=?",
                                              (old["new_offset"], frozen["segment_id"], old["old_offset"], old["next_ordinal"]))
                    if changed.rowcount != 1 or history.total_changes != 1:
                        _fail("cursor_cas_not_exactly_one")
                    _quick_check(history)
                    _quiescent(paths, probe)
                    # A successful process probe is not proof that a source or
                    # task snapshot stayed fixed during that probe. Revalidate
                    # after it while the CAS can still be rolled back.
                    _same_plan(paths, frozen, history)
                    history.commit()
                    committed = True
                except BaseException:
                    history.rollback()
                    raise
                finally:
                    history.close()
        result = reconcile_plan(paths, frozen)
        result.update({"backups": backups, "commit_state": "committed", "automatic_replay": False})
        return result
    except Exception as exc:
        result = {"status": "needs_review" if committed or attempted else "failed_before_commit",
                  "reason": exc.code if isinstance(exc, RecoveryError) else "execution_failed",
                  "backups": backups, "commit_state": "committed" if committed else "unknown" if attempted else "not_started",
                  "provider_requests": 0, "automatic_retry": False}
        if attempted:
            observation = reconcile_plan(paths, plan)
            result["reconciliation"] = observation
            if observation["status"] == "not_applied":
                result["commit_state"] = "not_committed"
                result["status"] = "failed_before_commit"
        return result
