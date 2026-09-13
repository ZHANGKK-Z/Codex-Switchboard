"""Read Codex's segment-owned history projection without changing any state.

The rollout metadata owns inheritance and cutoffs; SQLite owns projected turns.
The filename index lives for one read only. It is not a new persistent cache.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

_UUID_SUFFIX = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
_MAX_RECORD = 4 * 1024 * 1024


class HistoryChainError(RuntimeError):
    pass


def _ordinal(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _record(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise HistoryChainError("history_chain_invalid_json") from exc
    if not isinstance(value, dict) or not _ordinal(value.get("ordinal")):
        raise HistoryChainError("history_chain_missing_ordinal")
    return value


def is_benign_metadata_duplicate(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    expected: int,
) -> bool:
    """Recognize Codex's narrow, metadata-only duplicate ordinal pattern.

    A small number of modern rollouts contain adjacent ``event_msg`` records
    where a ``token_count`` event is immediately followed by
    ``thread_settings_applied`` with the same ordinal.  Both records are
    metadata and the projector consumes the ordinal only once.  This helper
    deliberately accepts that exact pair only; content records and repeated
    metadata events remain integrity failures.
    """

    if previous is None:
        return False
    if previous.get("ordinal") != current.get("ordinal"):
        return False
    if current.get("ordinal") != expected - 1:
        return False
    previous_payload = previous.get("payload")
    current_payload = current.get("payload")
    return (
        previous.get("type") == "event_msg"
        and current.get("type") == "event_msg"
        and isinstance(previous_payload, dict)
        and isinstance(current_payload, dict)
        and previous_payload.get("type") == "token_count"
        and current_payload.get("type") == "thread_settings_applied"
    )


def read_projected_history(home: Path, state_path: Path, history_path: Path,
                           thread_id: str, *, verify_rollout: bool = False,
                           path_index: dict | None = None) -> dict:
    if not state_path.is_file() or not history_path.is_file():
        raise HistoryChainError("history_database_missing")
    state = sqlite3.connect(state_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    history = sqlite3.connect(history_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    history.row_factory = sqlite3.Row
    try:
        row = state.execute("SELECT rollout_path FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None:
            raise HistoryChainError("history_thread_missing")
        current = Path(row[0] or "")
        index: dict[str, list[Path]] | None = path_index.get("segments") if path_index is not None else None

        def segment_id(path: Path) -> str:
            match = _UUID_SUFFIX.search(path.stem)
            return match.group(1) if match else path.stem

        def locate(ident: str) -> Path:
            nonlocal index
            if index is None:
                index = {}
                for directory in (home / "sessions", home / "archived_sessions"):
                    if directory.is_dir():
                        for file in directory.rglob("*.jsonl"):
                            index.setdefault(segment_id(file), []).append(file)
                if path_index is not None:
                    path_index["segments"] = index
            matches = index.get(ident, [])
            if len(matches) != 1:
                raise HistoryChainError("history_segment_missing_or_ambiguous")
            return matches[0]

        turn_columns = {r[1] for r in history.execute("PRAGMA table_info(thread_turns)")}
        item_columns = {r[1] for r in history.execute("PRAGMA table_info(thread_items)")}
        parts = []
        seen = set()
        cutoff = end_ordinal = None
        while True:
            ident = segment_id(current)
            if ident in seen or len(seen) >= 256:
                raise HistoryChainError("history_chain_cycle_or_too_deep")
            seen.add(ident)
            size = current.stat().st_size
            limit = size if cutoff is None else cutoff
            if not _ordinal(limit) or limit <= 0 or limit > size:
                raise HistoryChainError("history_base_offset_invalid")
            with current.open("rb") as handle:
                first = _record(handle.readline(_MAX_RECORD + 1))
                meta = first.get("payload")
                if first.get("type") != "session_meta" or not isinstance(meta, dict):
                    raise HistoryChainError("history_chain_metadata_invalid")
                owner = meta.get("id")
                if (not isinstance(owner, str) or not owner
                        or not (current.stem.endswith(owner) or current.stem.endswith(owner + "_" + ident))
                        or (not parts and owner != thread_id)):
                    raise HistoryChainError("history_chain_identity_invalid")
                handle.seek(limit - 1)
                if handle.read(1) != b"\n":
                    raise HistoryChainError("history_base_offset_inside_record")
                start = max(0, limit - _MAX_RECORD)
                handle.seek(start)
                tail = handle.read(limit - start).splitlines()
                if start:
                    tail = tail[1:]
                if not tail:
                    raise HistoryChainError("history_chain_tail_too_large")
                last = _record(tail[-1])
                exclusive = last["ordinal"] + 1
                if end_ordinal is not None and exclusive != end_ordinal:
                    raise HistoryChainError("history_base_ordinal_mismatch")
                if first["ordinal"] >= exclusive:
                    raise HistoryChainError("history_base_empty_interval")
                cursor = history.execute(
                    "SELECT next_rollout_byte_offset,next_rollout_ordinal "
                    "FROM thread_history_projection_state WHERE thread_id=?", (ident,)
                ).fetchone()
                if cursor is None or cursor[0] < limit or cursor[1] < exclusive:
                    raise HistoryChainError("history_base_projection_incomplete")
                if not parts and (cursor[0] != limit or cursor[1] != exclusive):
                    raise HistoryChainError("history_projection_not_at_eof")
                digest = None
                if verify_rollout:
                    handle.seek(0)
                    checksum = hashlib.sha256()
                    position = 0
                    expected = first["ordinal"]
                    previous_record: dict[str, Any] | None = None
                    while position < limit:
                        raw = handle.readline(limit - position)
                        if not raw:
                            raise HistoryChainError("history_chain_unexpected_eof")
                        record = _record(raw)
                        actual = record["ordinal"]
                        if actual != expected:
                            if not (
                                actual < expected
                                and is_benign_metadata_duplicate(
                                    previous_record, record, expected
                                )
                            ):
                                raise HistoryChainError(
                                    "rollout_duplicate_ordinal"
                                    if actual < expected
                                    else "rollout_ordinal_gap"
                                )
                        checksum.update(raw)
                        position += len(raw)
                        if actual == expected:
                            expected += 1
                        previous_record = record
                    digest = checksum.hexdigest()
            if "rollout_ordinal" in turn_columns:
                fields = ["turn_id", "rollout_ordinal"]
                if "rollout_end_ordinal" in turn_columns:
                    fields.append("rollout_end_ordinal")
                turns = history.execute(
                    f"SELECT {','.join(fields)} FROM thread_turns WHERE thread_id=? "
                    "AND rollout_ordinal>=? AND rollout_ordinal<? ORDER BY rollout_ordinal,rowid",
                    (ident, first["ordinal"], exclusive),
                ).fetchall()
                if any(t["rollout_end_ordinal"] is not None and t["rollout_end_ordinal"] >= exclusive
                       for t in turns if "rollout_end_ordinal" in t.keys()):
                    raise HistoryChainError("history_base_cuts_turn")
            else:
                # Explicit compatibility for old, unsegmented projection schemas.
                if meta.get("history_base") is not None or parts:
                    raise HistoryChainError("history_projection_schema_cannot_bound_turns")
                turns = history.execute("SELECT turn_id FROM thread_turns WHERE thread_id=? ORDER BY rowid", (ident,)).fetchall()
            if "rollout_ordinal" in item_columns:
                count = history.execute(
                    "SELECT COUNT(*) FROM thread_items WHERE thread_id=? AND rollout_ordinal>=? AND rollout_ordinal<?",
                    (ident, first["ordinal"], exclusive),
                ).fetchone()[0]
            else:
                count = history.execute("SELECT COUNT(*) FROM thread_items WHERE thread_id=?", (ident,)).fetchone()[0]
            parts.append({"segment_id": ident, "path": str(current), "size": limit,
                          "end_ordinal": exclusive, "sha256": digest,
                          "turn_ids": [t["turn_id"] for t in turns], "item_count": count})
            base = meta.get("history_base")
            if base is None:
                break
            if (not isinstance(base, dict) or not isinstance(base.get("thread_id"), str)
                    or not base["thread_id"] or not _ordinal(base.get("end_byte_offset"))
                    or not _ordinal(base.get("end_ordinal_exclusive"))
                    or base["end_ordinal_exclusive"] != first["ordinal"]):
                raise HistoryChainError("history_base_boundary_invalid")
            cutoff, end_ordinal = base["end_byte_offset"], base["end_ordinal_exclusive"]
            current = locate(base["thread_id"])
        ordered = list(reversed(parts))
        ids = [ident for part in ordered for ident in part["turn_ids"]]
        if len(ids) != len(set(ids)):
            raise HistoryChainError("history_duplicate_turn_id")
        return {"turn_ids": ids, "item_count": sum(p["item_count"] for p in parts), "segments": ordered}
    except HistoryChainError as exc:
        exc.segment_id = locals().get("ident")
        raise
    except (OSError, sqlite3.Error) as exc:
        raise HistoryChainError("history_chain_unavailable") from exc
    finally:
        history.close()
        state.close()
