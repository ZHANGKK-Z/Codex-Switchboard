"""Repair one known Codex history-projector duplicate, then fork before a bad turn.

The command is deliberately incident-specific.  It never writes rollout JSONL
and defaults to a read-only plan.  ``--execute`` is required before it backs up
or changes either Codex SQLite database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import switchboard
from appserver_client import AppServerClient


SPEC_KEYS = {
    "label",
    "source_thread_id",
    "segment_id",
    "expected_offset",
    "expected_ordinal",
    "correct_last_turn_id",
    "excluded_turn_id",
    "new_name",
    "source_archive_name",
    "expected_provider",
    "expected_model",
}
STATE_COLUMNS = {
    "id",
    "rollout_path",
    "model_provider",
    "model",
    "cwd",
    "name",
    "archived",
    "thread_section_id",
    "section_position",
    "section_entered_at_ms",
}
THREAD_SECTION_COLUMNS = {"id", "name", "appearance"}
PROJECTION_COLUMNS = {
    "thread_id",
    "next_rollout_byte_offset",
    "next_rollout_ordinal",
}


class RepairError(RuntimeError):
    """A fail-closed validation or execution rejection."""


@dataclass(frozen=True)
class RepairSpec:
    label: str
    source_thread_id: str
    segment_id: str
    expected_offset: int
    expected_ordinal: int
    correct_last_turn_id: str
    excluded_turn_id: str
    new_name: str
    source_archive_name: str
    expected_provider: str
    expected_model: str


@dataclass(frozen=True)
class PlannedRepair:
    spec: RepairSpec
    rollout_path: Path
    rollout_size: int
    rollout_sha256: str
    new_offset: int
    state_snapshot: Mapping[str, Any]
    section_snapshot: Mapping[str, Any]
    original_next_thread_id: str | None


@dataclass(frozen=True)
class RepairPlan:
    home: Path
    repairs: tuple[PlannedRepair, ...]


class RepairAppServerClient(AppServerClient):
    """Narrow experimental client used only for a turn-bounded local fork."""

    _ALLOWED_REQUESTS = AppServerClient._ALLOWED_REQUESTS | {"thread/section/move"}

    def move_thread_section(
        self,
        thread_id: str,
        section_id: str | None,
        *,
        before_thread_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": _required_text(thread_id, "thread_id"),
            "sectionId": None if section_id is None else _required_text(section_id, "section_id"),
        }
        if before_thread_id is not None:
            params["beforeThreadId"] = _required_text(before_thread_id, "before_thread_id")
        return self._request("thread/section/move", params)

    def fork_thread_at(
        self,
        thread_id: str,
        *,
        last_turn_id: str,
        model_provider: str,
        model: str,
    ) -> dict[str, Any]:
        return self._request(
            "thread/fork",
            {
                "threadId": thread_id,
                "lastTurnId": last_turn_id,
                "modelProvider": model_provider,
                "model": model,
                "deferGoalContinuation": True,
                "excludeTurns": False,
            },
        )


def _required_text(value: Any, field: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepairError(f"{field} must be a non-empty string")
    text = value.strip()
    if maximum is not None and len(text) > maximum:
        raise RepairError(f"{field} exceeds {maximum} characters")
    return text


def _required_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RepairError(f"{field} must be an integer >= {minimum}")
    return value


def load_spec(path: str | os.PathLike[str]) -> tuple[RepairSpec, ...]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError("repair spec is not readable JSON") from exc
    if not isinstance(document, dict) or set(document) != {"version", "repairs"}:
        raise RepairError("repair spec must contain exactly version and repairs")
    if document["version"] != 1:
        raise RepairError("unsupported repair spec version")
    rows = document["repairs"]
    if not isinstance(rows, list) or not rows:
        raise RepairError("repair spec repairs must be a non-empty list")
    repairs: list[RepairSpec] = []
    for index, row in enumerate(rows):
        field = f"repairs[{index}]"
        if not isinstance(row, dict) or set(row) != SPEC_KEYS:
            raise RepairError(f"{field} has missing or unknown fields")
        archive_name = _required_text(row["source_archive_name"], f"{field}.source_archive_name", maximum=200)
        if "错误继续" not in archive_name:
            raise RepairError(f"{field}.source_archive_name must contain 错误继续")
        repairs.append(
            RepairSpec(
                label=_required_text(row["label"], f"{field}.label", maximum=80),
                source_thread_id=_required_text(row["source_thread_id"], f"{field}.source_thread_id"),
                segment_id=_required_text(row["segment_id"], f"{field}.segment_id"),
                expected_offset=_required_int(row["expected_offset"], f"{field}.expected_offset"),
                expected_ordinal=_required_int(row["expected_ordinal"], f"{field}.expected_ordinal", minimum=1),
                correct_last_turn_id=_required_text(
                    row["correct_last_turn_id"], f"{field}.correct_last_turn_id"
                ),
                excluded_turn_id=_required_text(row["excluded_turn_id"], f"{field}.excluded_turn_id"),
                new_name=_required_text(row["new_name"], f"{field}.new_name", maximum=200),
                source_archive_name=archive_name,
                expected_provider=_required_text(row["expected_provider"], f"{field}.expected_provider"),
                expected_model=_required_text(row["expected_model"], f"{field}.expected_model"),
            )
        )
    source_ids = [item.source_thread_id for item in repairs]
    segment_ids = [item.segment_id for item in repairs]
    if len(set(source_ids)) != len(source_ids) or len(set(segment_ids)) != len(segment_ids):
        raise RepairError("repair spec contains duplicate source_thread_id or segment_id")
    return tuple(repairs)


def _path_key(value: str | os.PathLike[str]) -> str:
    text = os.fspath(value)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.abspath(text))


def _readonly_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {str(row[1]): row for row in connection.execute(f"PRAGMA table_info({table})")}


def _quick_check(connection: sqlite3.Connection, label: str) -> None:
    rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    if rows != ["ok"]:
        raise RepairError(f"{label} SQLite quick_check failed")


def _next_active_sibling(
    connection: sqlite3.Connection,
    *,
    source_thread_id: str,
    section_id: str,
    section_position: int | float,
) -> str | None:
    row = connection.execute(
        "SELECT id FROM threads WHERE thread_section_id = ? AND archived = 0 "
        "AND id <> ? AND section_position > ? ORDER BY section_position ASC LIMIT 1",
        (section_id, source_thread_id, section_position),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _validate_home(home: str | os.PathLike[str]) -> tuple[Path, switchboard.Paths]:
    candidate = Path(home)
    if not candidate.is_absolute():
        raise RepairError("--home must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RepairError("Codex home does not exist") from exc
    if not resolved.is_dir() or candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
        raise RepairError("Codex home must be a real directory, not a link or junction")
    paths = switchboard.Paths(resolved)
    for database in (paths.state_database, paths.thread_history_database):
        if not database.is_file() or database.is_symlink():
            raise RepairError(f"required database is missing or linked: {database.name}")
    return resolved, paths


def _json_line(raw: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError(f"{description} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RepairError(f"{description} must be a JSON object")
    return value


def _ordinal(item: Mapping[str, Any], description: str) -> int:
    value = item.get("ordinal")
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepairError(f"{description} has no integer ordinal")
    return value


def _turn_context_id(item: Mapping[str, Any]) -> str | None:
    if item.get("type") != "turn_context" or not isinstance(item.get("payload"), dict):
        return None
    value = item["payload"].get("turn_id")
    return value if isinstance(value, str) and value else None


def _inspect_rollout(path: Path, spec: RepairSpec) -> tuple[int, int, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RepairError(f"{spec.label}: rollout is unreadable") from exc
    if not data:
        raise RepairError(f"{spec.label}: rollout is empty")
    offset = spec.expected_offset
    if offset <= 0 or offset >= len(data):
        raise RepairError(f"{spec.label}: expected_offset is outside the rollout")
    if data[offset - 1 : offset] != b"\n":
        raise RepairError(f"{spec.label}: expected_offset is not a JSONL line boundary")
    duplicate_end = data.find(b"\n", offset)
    if duplicate_end < 0:
        raise RepairError(f"{spec.label}: duplicate line is not followed by another line")
    new_offset = duplicate_end + 1
    next_end = data.find(b"\n", new_offset)
    if next_end < 0:
        next_end = len(data)
    duplicate = _json_line(data[offset:duplicate_end].rstrip(b"\r"), f"{spec.label}: duplicate line")
    following = _json_line(data[new_offset:next_end].rstrip(b"\r"), f"{spec.label}: next line")
    payload = duplicate.get("payload")
    if (
        duplicate.get("type") != "event_msg"
        or not isinstance(payload, dict)
        or payload.get("type") != "thread_settings_applied"
        or _ordinal(duplicate, f"{spec.label}: duplicate line") != spec.expected_ordinal - 1
    ):
        raise RepairError(f"{spec.label}: cursor is not on the expected duplicate thread_settings_applied")
    if _ordinal(following, f"{spec.label}: next line") != spec.expected_ordinal:
        raise RepairError(f"{spec.label}: line after duplicate does not have expected ordinal")

    lines = data.splitlines()
    session = _json_line(lines[0], f"{spec.label}: session metadata")
    session_payload = session.get("payload")
    if (
        session.get("type") != "session_meta"
        or not isinstance(session_payload, dict)
        or session_payload.get("id") != spec.source_thread_id
    ):
        raise RepairError(f"{spec.label}: rollout session metadata does not match source task")
    turn_ids: list[str] = []
    for line_number, raw in enumerate(lines, 1):
        item = _json_line(raw, f"{spec.label}: rollout line {line_number}")
        turn_id = _turn_context_id(item)
        if turn_id is not None:
            turn_ids.append(turn_id)
    if turn_ids.count(spec.correct_last_turn_id) != 1 or turn_ids.count(spec.excluded_turn_id) != 1:
        raise RepairError(f"{spec.label}: correct or excluded turn is not unique in latest segment")
    if turn_ids.index(spec.correct_last_turn_id) >= turn_ids.index(spec.excluded_turn_id):
        raise RepairError(f"{spec.label}: correct turn does not precede excluded turn")
    return len(data), new_offset, hashlib.sha256(data).hexdigest()


def build_plan(home: str | os.PathLike[str], repairs: Sequence[RepairSpec]) -> RepairPlan:
    resolved, paths = _validate_home(home)
    state = _readonly_database(paths.state_database)
    history = _readonly_database(paths.thread_history_database)
    planned: list[PlannedRepair] = []
    try:
        state_columns = _table_columns(state, "threads")
        section_columns = _table_columns(state, "thread_sections")
        projection_columns = _table_columns(history, "thread_history_projection_state")
        if not STATE_COLUMNS.issubset(state_columns):
            raise RepairError("state_5.sqlite threads schema is incompatible")
        if not THREAD_SECTION_COLUMNS.issubset(section_columns):
            raise RepairError("state_5.sqlite thread_sections schema is incompatible")
        if not PROJECTION_COLUMNS.issubset(projection_columns):
            raise RepairError("thread_history_1.sqlite projection schema is incompatible")
        if int(projection_columns["thread_id"][5] or 0) != 1:
            raise RepairError("projection thread_id is not the primary key")
        _quick_check(state, "state_5.sqlite")
        _quick_check(history, "thread_history_1.sqlite")
        sessions_key = _path_key(resolved / "sessions")
        for spec in repairs:
            row = state.execute(
                "SELECT id, rollout_path, model_provider, model, cwd, name, archived, "
                "thread_section_id, section_position, section_entered_at_ms FROM threads WHERE id = ?",
                (spec.source_thread_id,),
            ).fetchone()
            if row is None:
                raise RepairError(f"{spec.label}: source task is missing from state_5.sqlite")
            snapshot = dict(row)
            if snapshot["model_provider"] != spec.expected_provider or snapshot["model"] != spec.expected_model:
                raise RepairError(f"{spec.label}: source Provider or model differs from spec")
            if not isinstance(snapshot["cwd"], str) or not snapshot["cwd"].strip():
                raise RepairError(f"{spec.label}: source cwd is invalid")
            if not isinstance(snapshot["name"], str) or not snapshot["name"].strip():
                raise RepairError(f"{spec.label}: source name is invalid")
            if snapshot["archived"] != 0:
                raise RepairError(f"{spec.label}: source task must be active")
            section_id = snapshot["thread_section_id"]
            position = snapshot["section_position"]
            if not isinstance(section_id, str) or not section_id:
                raise RepairError(f"{spec.label}: source sidebar section is invalid")
            if isinstance(position, bool) or not isinstance(position, (int, float)):
                raise RepairError(f"{spec.label}: source section_position is invalid")
            section_row = state.execute(
                "SELECT id, name, appearance FROM thread_sections WHERE id = ?",
                (section_id,),
            ).fetchone()
            if section_row is None or section_row["name"] != "Pinned":
                raise RepairError(f"{spec.label}: source task is not in the named Pinned section")
            section_snapshot = dict(section_row)
            original_next_thread_id = _next_active_sibling(
                state,
                source_thread_id=spec.source_thread_id,
                section_id=section_id,
                section_position=position,
            )
            rollout_text = snapshot["rollout_path"]
            if not isinstance(rollout_text, str) or not rollout_text.strip():
                raise RepairError(f"{spec.label}: source rollout_path is invalid")
            rollout_path = Path(rollout_text)
            if not rollout_path.is_absolute() or not rollout_path.is_file() or rollout_path.is_symlink():
                raise RepairError(f"{spec.label}: source rollout_path is missing, relative, or linked")
            rollout_path = rollout_path.resolve()
            try:
                inside_sessions = os.path.commonpath((sessions_key, _path_key(rollout_path))) == sessions_key
            except ValueError:
                inside_sessions = False
            if not inside_sessions or rollout_path.suffix.lower() != ".jsonl":
                raise RepairError(f"{spec.label}: source rollout_path is outside the Codex sessions tree")
            if spec.source_thread_id not in rollout_path.stem:
                raise RepairError(f"{spec.label}: rollout filename does not contain source task id")
            parsed_segment = rollout_path.stem.rsplit("_", 1)[1] if "_" in rollout_path.stem else spec.source_thread_id
            if parsed_segment != spec.segment_id:
                raise RepairError(f"{spec.label}: rollout filename segment does not match spec")
            cursor = history.execute(
                "SELECT next_rollout_byte_offset, next_rollout_ordinal "
                "FROM thread_history_projection_state WHERE thread_id = ?",
                (spec.segment_id,),
            ).fetchone()
            if cursor is None:
                raise RepairError(f"{spec.label}: projection cursor row is missing")
            if (
                int(cursor["next_rollout_byte_offset"]) != spec.expected_offset
                or int(cursor["next_rollout_ordinal"]) != spec.expected_ordinal
            ):
                raise RepairError(f"{spec.label}: projection cursor differs from spec")
            rollout_size, new_offset, digest = _inspect_rollout(rollout_path, spec)
            planned.append(
                PlannedRepair(
                    spec=spec,
                    rollout_path=rollout_path,
                    rollout_size=rollout_size,
                    rollout_sha256=digest,
                    new_offset=new_offset,
                    state_snapshot=snapshot,
                    section_snapshot=section_snapshot,
                    original_next_thread_id=original_next_thread_id,
                )
            )
    except sqlite3.Error as exc:
        raise RepairError("Codex SQLite validation failed") from exc
    finally:
        history.close()
        state.close()
    return RepairPlan(resolved, tuple(planned))


def _public_plan(plan: RepairPlan) -> dict[str, Any]:
    return {
        "status": "ready",
        "repair_count": len(plan.repairs),
        "repairs": [
            {
                "label": item.spec.label,
                "source_thread_id": item.spec.source_thread_id,
                "segment_id": item.spec.segment_id,
                "cursor": {
                    "old_offset": item.spec.expected_offset,
                    "new_offset": item.new_offset,
                    "ordinal": item.spec.expected_ordinal,
                },
                "rollout_size": item.rollout_size,
                "correct_last_turn_id": item.spec.correct_last_turn_id,
                "excluded_turn_id": item.spec.excluded_turn_id,
                "provider": item.spec.expected_provider,
                "model": item.spec.expected_model,
                "source_section": {
                    "id": item.section_snapshot["id"],
                    "name": item.section_snapshot["name"],
                    "position": item.state_snapshot["section_position"],
                    "next_thread_id": item.original_next_thread_id,
                },
            }
            for item in plan.repairs
        ],
        "writes": [],
        "provider_requests": 0,
        "source_rollouts_modified": False,
    }


def _state_snapshot(paths: switchboard.Paths, thread_id: str) -> dict[str, Any]:
    connection = _readonly_database(paths.state_database)
    try:
        row = connection.execute(
            "SELECT id, rollout_path, model_provider, model, cwd, name, archived, "
            "thread_section_id, section_position, section_entered_at_ms FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise RepairError("source task disappeared during execution")
        return dict(row)
    finally:
        connection.close()


def _section_snapshot(paths: switchboard.Paths, section_id: str) -> dict[str, Any]:
    connection = _readonly_database(paths.state_database)
    try:
        row = connection.execute(
            "SELECT id, name, appearance FROM thread_sections WHERE id = ?",
            (section_id,),
        ).fetchone()
        if row is None:
            raise RepairError("source sidebar section disappeared during execution")
        return dict(row)
    finally:
        connection.close()


def _current_next_sibling(paths: switchboard.Paths, item: PlannedRepair) -> str | None:
    connection = _readonly_database(paths.state_database)
    try:
        return _next_active_sibling(
            connection,
            source_thread_id=item.spec.source_thread_id,
            section_id=str(item.state_snapshot["thread_section_id"]),
            section_position=item.state_snapshot["section_position"],
        )
    finally:
        connection.close()


def cas_advance_projection(paths: switchboard.Paths, plan: RepairPlan) -> None:
    """Advance every duplicate line in one transaction, preserving ordinal."""

    connection = sqlite3.connect(paths.thread_history_database, timeout=30, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for item in plan.repairs:
            if _state_snapshot(paths, item.spec.source_thread_id) != dict(item.state_snapshot):
                raise RepairError(f"{item.spec.label}: source state changed after planning")
            if _section_snapshot(paths, str(item.section_snapshot["id"])) != dict(item.section_snapshot):
                raise RepairError(f"{item.spec.label}: source sidebar section changed after planning")
            if _current_next_sibling(paths, item) != item.original_next_thread_id:
                raise RepairError(f"{item.spec.label}: source section order changed after planning")
            cursor = connection.execute(
                "SELECT next_rollout_byte_offset, next_rollout_ordinal "
                "FROM thread_history_projection_state WHERE thread_id = ?",
                (item.spec.segment_id,),
            ).fetchone()
            if cursor is None or tuple(cursor) != (item.spec.expected_offset, item.spec.expected_ordinal):
                raise RepairError(f"{item.spec.label}: projection cursor lost CAS race")
            result = connection.execute(
                "UPDATE thread_history_projection_state SET next_rollout_byte_offset = ? "
                "WHERE thread_id = ? AND next_rollout_byte_offset = ? AND next_rollout_ordinal = ?",
                (
                    item.new_offset,
                    item.spec.segment_id,
                    item.spec.expected_offset,
                    item.spec.expected_ordinal,
                ),
            )
            if result.rowcount != 1:
                raise RepairError(f"{item.spec.label}: projection cursor CAS did not update exactly one row")
        _quick_check(connection, "thread_history_1.sqlite")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _default_writer_probe(paths: switchboard.Paths) -> list[Path]:
    # Codex leaves zero-byte marker files behind after a clean desktop exit.
    # The shared writer-lock helper treats only non-empty locks as active.
    return switchboard.writer_locks(paths)


def _ensure_quiescent(
    paths: switchboard.Paths,
    process_probe: Callable[[switchboard.Paths], Sequence[Any]],
    writer_probe: Callable[[switchboard.Paths], Sequence[Any]],
) -> None:
    processes = process_probe(paths)
    locks = writer_probe(paths)
    if processes or locks:
        raise RepairError(
            "execute requires Codex/App Server and all writer locks to be closed; "
            f"active_processes={len(processes)}, active_writer_locks={len(locks)}"
        )


def _turn_ids(thread: Any, description: str) -> list[str]:
    if not isinstance(thread, dict) or not isinstance(thread.get("turns"), list):
        raise RepairError(f"{description}: App Server response has no complete turns list")
    ids: list[str] = []
    for turn in thread["turns"]:
        if not isinstance(turn, dict):
            raise RepairError(f"{description}: App Server returned an invalid turn")
        value = turn.get("id")
        if value is None:
            value = turn.get("turnId")
        if not isinstance(value, str) or not value:
            raise RepairError(f"{description}: App Server returned a turn without id")
        ids.append(value)
    return ids


def _validate_app_thread(
    thread: Any,
    item: PlannedRepair,
    *,
    expected_id: str,
    description: str,
) -> list[str]:
    if not isinstance(thread, dict) or thread.get("id") != expected_id:
        raise RepairError(f"{item.spec.label}: {description} task id mismatch")
    if thread.get("modelProvider") != item.spec.expected_provider:
        raise RepairError(f"{item.spec.label}: {description} Provider mismatch")
    if _path_key(str(thread.get("cwd") or "")) != _path_key(str(item.state_snapshot["cwd"])):
        raise RepairError(f"{item.spec.label}: {description} cwd mismatch")
    if "model" in thread and thread.get("model") not in (None, item.spec.expected_model):
        raise RepairError(f"{item.spec.label}: {description} model mismatch")
    return _turn_ids(thread, f"{item.spec.label}: {description}")


def _projection_at_eof(paths: switchboard.Paths, item: PlannedRepair) -> bool:
    history = _readonly_database(paths.thread_history_database)
    try:
        row = history.execute(
            "SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id = ?",
            (item.spec.segment_id,),
        ).fetchone()
        return row is not None and int(row[0]) == item.rollout_size
    finally:
        history.close()


def _wait_for_projection_eof(paths: switchboard.Paths, item: PlannedRepair) -> None:
    for _attempt in range(40):
        if _projection_at_eof(paths, item):
            return
        time.sleep(0.05)
    raise RepairError(f"{item.spec.label}: thread/fork did not project latest segment to EOF")


def _wait_for_state_row(paths: switchboard.Paths, thread_id: str) -> dict[str, Any]:
    for _attempt in range(40):
        try:
            return _state_snapshot(paths, thread_id)
        except (RepairError, sqlite3.Error, OSError):
            time.sleep(0.05)
    raise RepairError("forked task was not persisted in state_5.sqlite")


def _validate_fork_state(item: PlannedRepair, row: Mapping[str, Any]) -> None:
    if row["model_provider"] != item.spec.expected_provider or row["model"] != item.spec.expected_model:
        raise RepairError(f"{item.spec.label}: forked task Provider or model was not persisted")
    if _path_key(str(row["cwd"])) != _path_key(str(item.state_snapshot["cwd"])):
        raise RepairError(f"{item.spec.label}: forked task cwd was not persisted")


def _new_client(paths: switchboard.Paths, executable: str | None) -> RepairAppServerClient:
    return RepairAppServerClient(
        executable=switchboard.resolve_appserver_executable(executable, paths=paths),
        codex_home=paths.codex_home,
        request_timeout=switchboard.DEFAULT_APP_SERVER_TIMEOUT,
    )


def _restore_metadata(
    paths: switchboard.Paths,
    client: Any,
    plan: RepairPlan,
    created_ids: Sequence[str],
) -> dict[str, Any]:
    failures = 0

    def attempt(operation: Callable[[], Any]) -> None:
        nonlocal failures
        try:
            operation()
        except Exception:
            failures += 1

    for item in plan.repairs:
        original = item.state_snapshot
        try:
            source_now = _state_snapshot(paths, item.spec.source_thread_id)
        except Exception:
            source_now = {"archived": 1}
        if int(source_now["archived"]) == 1:
            attempt(lambda item=item: client.unarchive_thread(item.spec.source_thread_id))
        attempt(lambda item=item, original=original: client.set_thread_name(item.spec.source_thread_id, str(original["name"])))
        before = item.original_next_thread_id
        if before is not None:
            try:
                sibling = _state_snapshot(paths, before)
                if int(sibling["archived"]) != 0 or sibling["thread_section_id"] != original["thread_section_id"]:
                    before = None
            except Exception:
                before = None
        attempt(
            lambda item=item, original=original, before=before: client.move_thread_section(
                item.spec.source_thread_id,
                str(original["thread_section_id"]),
                before_thread_id=before,
            )
        )
    for thread_id in reversed(created_ids):
        attempt(lambda thread_id=thread_id: client.move_thread_section(thread_id, None))
        try:
            new_now = _state_snapshot(paths, thread_id)
        except Exception:
            new_now = {"archived": 0}
        if int(new_now["archived"]) == 0:
            attempt(lambda thread_id=thread_id: client.archive_thread(thread_id))
    for item in plan.repairs:
        try:
            restored = _state_snapshot(paths, item.spec.source_thread_id)
            if (
                restored["name"] != item.state_snapshot["name"]
                or int(restored["archived"]) != 0
                or restored["thread_section_id"] != item.state_snapshot["thread_section_id"]
            ):
                failures += 1
            if item.original_next_thread_id is not None:
                sibling = _state_snapshot(paths, item.original_next_thread_id)
                if (
                    int(sibling["archived"]) == 0
                    and sibling["thread_section_id"] == restored["thread_section_id"]
                    and restored["section_position"] >= sibling["section_position"]
                ):
                    failures += 1
        except Exception:
            failures += 1
    for thread_id in created_ids:
        try:
            retired = _state_snapshot(paths, thread_id)
            if int(retired["archived"]) != 1 or retired["thread_section_id"] is not None:
                failures += 1
        except Exception:
            failures += 1
    return {"attempted": True, "complete": failures == 0, "failed_actions": failures}


def _source_rollout_status(paths: switchboard.Paths, plan: RepairPlan) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    all_prefixes_preserved = True
    for item in plan.repairs:
        prefix_preserved = False
        appended_bytes = 0
        try:
            current_state = _state_snapshot(paths, item.spec.source_thread_id)
            current_path = Path(str(current_state["rollout_path"] or ""))
            allowed_roots = (
                _path_key(paths.codex_home / "sessions"),
                _path_key(paths.codex_home / "archived_sessions"),
            )
            current_key = _path_key(current_path)
            inside_allowed = any(
                os.path.commonpath((root, current_key)) == root for root in allowed_roots
            )
            if (
                not current_path.is_absolute()
                or not current_path.is_file()
                or current_path.is_symlink()
                or current_path.name != item.rollout_path.name
                or not inside_allowed
            ):
                raise OSError("current rollout path failed validation")
            current = current_path.read_bytes()
            appended_bytes = max(0, len(current) - item.rollout_size)
            prefix_preserved = (
                len(current) >= item.rollout_size
                and hashlib.sha256(current[: item.rollout_size]).hexdigest() == item.rollout_sha256
            )
            tail = current[item.rollout_size :] if prefix_preserved else b""
            if tail:
                if not current[: item.rollout_size].endswith(b"\n") or not tail.endswith(b"\n"):
                    prefix_preserved = False
                else:
                    for line_number, raw in enumerate(tail.splitlines(), 1):
                        if not raw:
                            prefix_preserved = False
                            break
                        try:
                            _json_line(raw, f"{item.spec.label}: appended rollout line {line_number}")
                        except RepairError:
                            prefix_preserved = False
                            break
        except (OSError, ValueError, RepairError, sqlite3.Error):
            prefix_preserved = False
        entries[item.spec.source_thread_id] = {
            "label": item.spec.label,
            "prefix_preserved": prefix_preserved,
            "appended_bytes": appended_bytes,
        }
        all_prefixes_preserved = all_prefixes_preserved and prefix_preserved
    return {
        "prefix_preserved": all_prefixes_preserved,
        "modified": any(entry["appended_bytes"] > 0 for entry in entries.values()),
        "entries": entries,
    }


def _record_rollout_status(receipt: dict[str, Any], status: Mapping[str, Any]) -> None:
    receipt["source_rollout_prefix_preserved"] = bool(status["prefix_preserved"])
    receipt["source_rollouts_modified"] = bool(status["modified"])
    entries = status["entries"]
    for row in receipt.get("repairs", []):
        entry = entries.get(row.get("source_thread_id"))
        if entry is not None:
            row["appended_bytes"] = int(entry["appended_bytes"])


def _appserver_workflow(
    paths: switchboard.Paths,
    plan: RepairPlan,
    *,
    executable: str | None,
    client_factory: Callable[[switchboard.Paths, str | None], Any],
    receipt: dict[str, Any],
) -> None:
    client: Any | None = None
    created_ids: list[str] = []
    stage = "appserver-start"
    try:
        client = client_factory(paths, executable)
        client.start()
        client.initialize(capabilities={"experimentalApi": True})
        for index, item in enumerate(plan.repairs):
            spec = item.spec
            result_row = receipt["repairs"][index]
            stage = f"{spec.label}:fork"
            forked = client.fork_thread_at(
                spec.source_thread_id,
                last_turn_id=spec.correct_last_turn_id,
                model_provider=spec.expected_provider,
                model=spec.expected_model,
            )
            _wait_for_projection_eof(paths, item)
            result_row["source_projected_to_eof"] = True
            result_row["excluded_turn_confirmed_in_source_rollout"] = True
            fork_thread = forked.get("thread")
            if not isinstance(fork_thread, dict):
                raise RepairError(f"{spec.label}: thread/fork returned no task")
            new_id = fork_thread.get("id")
            if not isinstance(new_id, str) or not new_id or new_id == spec.source_thread_id:
                raise RepairError(f"{spec.label}: thread/fork did not create a distinct task")
            created_ids.append(new_id)
            result_row["new_thread_id"] = new_id
            if fork_thread.get("forkedFromId") != spec.source_thread_id:
                raise RepairError(f"{spec.label}: fork source identity mismatch")
            fork_turns = _validate_app_thread(
                fork_thread,
                item,
                expected_id=new_id,
                description="fork response",
            )
            if not fork_turns or fork_turns[-1] != spec.correct_last_turn_id or spec.excluded_turn_id in fork_turns:
                raise RepairError(f"{spec.label}: fork response did not stop exactly before excluded turn")
            reread = client.read_thread(
                new_id,
                include_turns=True,
                expected_cwd=item.state_snapshot["cwd"],
                expected_model_provider=spec.expected_provider,
            )
            reread_turns = _validate_app_thread(
                reread.get("thread"),
                item,
                expected_id=new_id,
                description="fork reread",
            )
            if not reread_turns or reread_turns[-1] != spec.correct_last_turn_id or spec.excluded_turn_id in reread_turns:
                raise RepairError(f"{spec.label}: persisted fork did not stop exactly before excluded turn")
            _validate_fork_state(item, _wait_for_state_row(paths, new_id))
            result_row["fork_last_turn_id"] = spec.correct_last_turn_id
            result_row["excluded_turn_absent"] = True
            result_row["provider_valid"] = True

            stage = f"{spec.label}:metadata"
            stage = f"{spec.label}:metadata:name-new"
            client.set_thread_name(new_id, spec.new_name)
            stage = f"{spec.label}:metadata:name-source"
            client.set_thread_name(spec.source_thread_id, spec.source_archive_name)
            source_section_id = str(item.section_snapshot["id"])
            stage = f"{spec.label}:metadata:move-new-section"
            client.move_thread_section(
                new_id,
                source_section_id,
                before_thread_id=spec.source_thread_id,
            )
            stage = f"{spec.label}:metadata:archive-source"
            client.archive_thread(spec.source_thread_id)
            new_after_archive = _state_snapshot(paths, new_id)
            if int(new_after_archive["archived"]) == 1:
                stage = f"{spec.label}:metadata:unarchive-new"
                client.unarchive_thread(new_id)
            stage = f"{spec.label}:metadata:verify"
            source_after = _state_snapshot(paths, spec.source_thread_id)
            new_after = _state_snapshot(paths, new_id)
            new_section = _section_snapshot(paths, str(new_after["thread_section_id"] or ""))
            if (
                source_after["name"] != spec.source_archive_name
                or int(source_after["archived"]) != 1
                or new_after["name"] != spec.new_name
                or int(new_after["archived"]) != 0
                or new_after["thread_section_id"] != source_section_id
                or new_section["name"] != "Pinned"
                or not isinstance(new_after["section_position"], (int, float))
                or not isinstance(source_after["section_position"], (int, float))
                or new_after["section_position"] >= source_after["section_position"]
            ):
                raise RepairError(f"{spec.label}: final name/archive/section state is incomplete")
            _validate_fork_state(item, new_after)
            result_row["metadata_complete"] = True
            result_row["section_name"] = "Pinned"

        stage = "source-rollout-verification"
        rollout_status = _source_rollout_status(paths, plan)
        _record_rollout_status(receipt, rollout_status)
        if not rollout_status["prefix_preserved"]:
            raise RepairError("source rollout prefix changed or appended metadata is invalid")
        receipt["status"] = "completed"
    except BaseException as exc:
        receipt["status"] = "failed"
        receipt["error"] = {
            "stage": stage,
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }
        receipt["rollback"] = (
            _restore_metadata(paths, client, plan, created_ids)
            if client is not None
            else {"attempted": False, "complete": True, "failed_actions": 0}
        )
        _record_rollout_status(receipt, _source_rollout_status(paths, plan))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                if receipt.get("status") == "completed":
                    receipt["status"] = "failed"
                    receipt["error"] = {"stage": "appserver-close", "type": "client_close_failed"}
                    receipt["rollback"] = _restore_metadata(paths, client, plan, created_ids)
                    _record_rollout_status(receipt, _source_rollout_status(paths, plan))


def run_repair(
    home: str | os.PathLike[str],
    repairs: Sequence[RepairSpec],
    *,
    execute: bool = False,
    wait_for_exit: bool = False,
    executable: str | None = None,
    process_probe: Callable[[switchboard.Paths], Sequence[Any]] | None = None,
    writer_probe: Callable[[switchboard.Paths], Sequence[Any]] | None = None,
    client_factory: Callable[[switchboard.Paths, str | None], Any] | None = None,
    backup_fn: Callable[[switchboard.Paths, Path, str], Path] | None = None,
) -> dict[str, Any]:
    initial_plan = build_plan(home, repairs)
    if not execute:
        result = _public_plan(initial_plan)
        result["mode"] = "dry-run"
        return result

    paths = switchboard.Paths(initial_plan.home)
    process_probe = process_probe or switchboard.appserver_blocking_processes
    writer_probe = writer_probe or _default_writer_probe
    wait_result = None
    if wait_for_exit:
        wait_result = switchboard.wait_for_appserver_exit(paths, process_probe=process_probe)

    with switchboard.conversion_operation_lock(paths):
        # Planning is repeated only after stable process exit.  No backups or
        # database writes occur while --wait-for-exit is polling.
        plan = build_plan(initial_plan.home, repairs)
        _ensure_quiescent(paths, process_probe, writer_probe)
        receipt: dict[str, Any] = {
            "receipt_version": 1,
            "mode": "execute",
            "status": "running",
            "repair_count": len(plan.repairs),
            "backups": [],
            "repairs": [
                {
                    "label": item.spec.label,
                    "source_thread_id": item.spec.source_thread_id,
                    "segment_id": item.spec.segment_id,
                    "cursor": {
                        "old_offset": item.spec.expected_offset,
                        "new_offset": item.new_offset,
                        "ordinal": item.spec.expected_ordinal,
                    },
                    "source_section": {
                        "id": item.section_snapshot["id"],
                        "name": item.section_snapshot["name"],
                        "position": item.state_snapshot["section_position"],
                        "next_thread_id": item.original_next_thread_id,
                    },
                }
                for item in plan.repairs
            ],
            "provider_requests": 0,
            "source_rollouts_modified": False,
        }
        if wait_result is not None:
            receipt["wait"] = {
                "status": wait_result.get("status"),
                "stable_empty_checks": wait_result.get("stable_empty_checks"),
            }
        backup = backup_fn or switchboard.backup_sqlite
        try:
            for source, label in (
                (paths.state_database, "before-stalled-history-state"),
                (paths.thread_history_database, "before-stalled-history-projection"),
            ):
                target = backup(paths, source, label)
                receipt["backups"].append(Path(target).name)
            _ensure_quiescent(paths, process_probe, writer_probe)
            cas_advance_projection(paths, plan)
            receipt["cursor_cas_complete"] = True
            _ensure_quiescent(paths, process_probe, writer_probe)
        except BaseException as exc:
            receipt["status"] = "failed"
            receipt["error"] = {
                "stage": "backup-or-cursor-cas",
                "type": type(exc).__name__,
                "message": str(exc)[:500],
            }
            return receipt
        _appserver_workflow(
            paths,
            plan,
            executable=executable,
            client_factory=client_factory or _new_client,
            receipt=receipt,
        )
        return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, help="absolute CODEX_HOME to inspect")
    parser.add_argument("--spec", required=True, help="versioned repair JSON")
    parser.add_argument("--execute", action="store_true", help="back up, repair, and fork")
    parser.add_argument(
        "--wait-for-exit",
        action="store_true",
        help="wait for two stable Codex/App Server exit checks before replanning and executing",
    )
    parser.add_argument(
        "--open-first",
        action="store_true",
        help="after a completed receipt and closed helper, open the first repaired task",
    )
    parser.add_argument("--codex-executable", help="explicit Codex app-server executable")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.wait_for_exit or args.open_first) and not args.execute:
        print(
            json.dumps(
                {"receipt_version": 1, "status": "rejected", "error": {"type": "explicit_execute_required"}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    if args.open_first and not hasattr(os, "startfile"):
        print(
            json.dumps(
                {"receipt_version": 1, "status": "rejected", "error": {"type": "open_first_requires_windows"}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    try:
        repairs = load_spec(args.spec)
        result = run_repair(
            args.home,
            repairs,
            execute=args.execute,
            wait_for_exit=args.wait_for_exit,
            executable=args.codex_executable,
        )
    except BaseException as exc:
        result = {
            "receipt_version": 1,
            "status": "rejected",
            "error": {"type": type(exc).__name__, "message": str(exc)[:500]},
            "provider_requests": 0,
            "source_rollouts_modified": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.stdout.flush()
    if result.get("status") == "completed" and args.open_first:
        first = next(
            (item.get("new_thread_id") for item in result.get("repairs", []) if item.get("new_thread_id")),
            None,
        )
        if isinstance(first, str):
            os.startfile(f"codex://threads/{first}")  # type: ignore[attr-defined]
    return 0 if result.get("status") in {"ready", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
