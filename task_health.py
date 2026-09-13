"""One-shot, read-only task evidence. Native Codex remains the only owner.

No app-server, model, process discovery, repair, cache or persistence is used.
The result describes a bounded observation, never business or live runtime truth.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import switchboard
from history_chain import HistoryChainError, read_projected_history

MAX_DEEP_BYTES = 512 * 1024 * 1024
MAX_TAIL_BYTES = 4 * 1024 * 1024
MAX_ITEM_BYTES = 2 * 1024 * 1024
MAX_ITEM_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ITEMS = 2000
MAX_FAILURES = 20
_TOOLS = frozenset({"commandExecution", "mcpToolCall", "dynamicToolCall",
                    "collabAgentToolCall", "fileChange", "webSearch", "imageGeneration"})
_FAILED = frozenset({"failed", "error", "errored", "declined"})
_PENDING = frozenset({"inProgress", "in_progress", "running", "pending"})
_KNOWN_CODES = ("PILOT_PROCESS_INSPECTION_FAILED",)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class _Cancelled(Exception):
    pass


class _Unavailable(Exception):
    pass


def _check(cancel_event: Any) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _Cancelled


def _progress(callback: Any, stage: str, cancel_event: Any) -> None:
    _check(cancel_event)
    if callback is not None:
        callback(stage)
    _check(cancel_event)


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _card(status: str, label: str, detail: str, **extra: Any) -> dict:
    return {"status": status, "label": label, "detail": detail, **extra}


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _state(paths: switchboard.Paths, thread_id: str) -> dict | None:
    connection = _connect(paths.state_database)
    try:
        columns = _columns(connection, "threads")
        if not {"id", "rollout_path"}.issubset(columns):
            raise _Unavailable
        wanted = [name for name in ("id", "name", "rollout_path", "updated_at_ms", "updated_at")
                  if name in columns]
        row = connection.execute(f"SELECT {','.join(wanted)} FROM threads WHERE id=?",
                                 (thread_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def _name(value: Any) -> str:
    # `title` may be the entire first user prompt; only the explicit native name
    # is a display field. Never fall back to prompt/title/session content.
    if not isinstance(value, str) or not value.strip():
        return "未命名任务"
    value = " ".join(value.split())[:120]
    if re.search(r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|bearer\s|api[_ -]?key\s*[:=]|password\s*[:=]|https?://)", value):
        return "任务（名称含敏感格式，已隐藏）"
    return value


def _file_stamp(path: Path) -> tuple | None:
    try:
        result = path.stat()
        return result.st_size, result.st_mtime_ns, result.st_ino
    except OSError:
        return None


def _fingerprint(paths: switchboard.Paths, thread_id: str, parts: list[dict]) -> tuple:
    """In-memory observation only; no lock/revision/hash or second state store.

    Compare only this task/chain (not whole WAL files changed by other tasks).
    A change invalidates this report and asks for a fresh read, never a repair.
    """
    state = _state(paths, thread_id)
    stamps = tuple((part["segment_id"], _file_stamp(Path(part["path"]))) for part in parts)
    history = _connect(paths.thread_history_database)
    try:
        turn_columns = _columns(history, "thread_turns")
        item_columns = _columns(history, "thread_items")
        result = []
        for part in parts:
            ident = part["segment_id"]
            cursor = history.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal "
                                     "FROM thread_history_projection_state WHERE thread_id=?", (ident,)).fetchone()
            turns = history.execute("SELECT COUNT(*),MAX(rowid) FROM thread_turns WHERE thread_id=?", (ident,)).fetchone()
            fields = [field for field in ("turn_id", "status", "rollout_ordinal", "rollout_end_ordinal", "completed_at")
                      if field in turn_columns]
            order = "rollout_ordinal DESC,rowid DESC" if "rollout_ordinal" in turn_columns else "rowid DESC"
            latest = history.execute(f"SELECT {','.join(fields)} FROM thread_turns WHERE thread_id=? ORDER BY {order} LIMIT 1", (ident,)).fetchone() if fields else None
            update = "MAX(updated_at_ordinal)" if "updated_at_ordinal" in item_columns else "MAX(rowid)"
            items = history.execute(f"SELECT COUNT(*),{update} FROM thread_items WHERE thread_id=?", (ident,)).fetchone()
            result.append((ident, tuple(cursor) if cursor else None, tuple(turns),
                           tuple(latest) if latest else None, tuple(items)))
        return state, stamps, tuple(result)
    finally:
        history.close()


def _history_card(value: dict | None) -> dict:
    value = value or {}
    status = value.get("health", "unknown")
    labels = {
        "healthy": ("历史完整", "原始历史与投影已同步，继承链检查通过。"),
        "pending": ("历史等待同步", "投影尚未追上原始历史；单次检查不能判定卡死。"),
        "stalled": ("历史存在异常证据", "读取位置或历史序号不一致；请先复核证据，勿自动修复。"),
        "unreadable": ("历史投影不可读", "原始历史非空，但未找到可读取回合。"),
        "unknown": ("历史尚不能确认", "历史文件、数据库结构或继承边界缺少可核验证据。"),
        "unchecked": ("历史未完成深检", "只读结构检查已运行，尚未完成原始历史连续性检查。"),
    }
    if status not in labels:
        status = "unknown"
    label, detail = labels[status]
    result = _card(status, label, detail)
    for key in ("projected_turns", "projected_items", "rollout_size", "projection_offset", "projection_ordinal", "history_segment_count"):
        result[key] = _number(value.get(key))
    result["projection_complete"] = value.get("projection_complete") is True
    # Reasons are source-owned static enums, but never relay an arbitrary error.
    reason = value.get("reason")
    result["reason"] = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,96}", reason) else "unavailable"
    return result


def _latest_projected(paths: switchboard.Paths, parts: list[dict], fresh: bool,
                      cancel_event: Any) -> tuple[dict, dict]:
    history = _connect(paths.thread_history_database)
    tools = _card("unknown", "工具记录尚不能确认", "没有足够的工具执行证据。",
                  failed_count=0, failures=[], inspected_count=0, pending_count=0,
                  success_count=0, unknown_count=0, truncated=False)
    latest = _card("unknown", "最近回合尚不能确认", "当前投影内未找到可核验回合。", id=None)
    try:
        turn_columns = _columns(history, "thread_turns")
        item_columns = _columns(history, "thread_items")
        if not {"thread_id", "turn_id", "status", "rollout_ordinal"}.issubset(turn_columns):
            return latest, tools
        row = None
        owner = None
        for part in reversed(parts):
            _check(cancel_event)
            wanted = [name for name in ("turn_id", "status", "rollout_ordinal", "rollout_end_ordinal", "started_at", "completed_at")
                      if name in turn_columns]
            sql = f"SELECT {','.join(wanted)} FROM thread_turns WHERE thread_id=?"
            args: list[Any] = [part["segment_id"]]
            if part.get("end_ordinal") is not None:
                sql += " AND rollout_ordinal<?"
                args.append(part["end_ordinal"])
            sql += " ORDER BY rollout_ordinal DESC,rowid DESC LIMIT 1"
            row = history.execute(sql, args).fetchone()
            if row is not None:
                owner = part
                break
        if row is None or owner is None:
            return latest, tools
        status = row["status"]
        labels = {"completed": "最近回合已结束", "interrupted": "最近回合已中断",
                  "failed": "最近回合有失败状态", "inProgress": "记录中回合未结束"}
        normalized = {"completed": "completed", "interrupted": "interrupted", "failed": "failed", "inProgress": "unfinished"}.get(status, "unknown")
        detail = "这是持久化回合状态，不代表实时运行、后台进程存活或业务成功。"
        if not fresh:
            detail = "仅为投影内最近回合，非已确认的最新回合。" + detail
        latest = _card(normalized, labels.get(status, "最近回合状态未知"), detail,
                       id=_id(row["turn_id"]), ordinal=_number(row["rollout_ordinal"]),
                       scope="latest_verified_projection" if fresh else "projection_only",
                       started_at=_number(row["started_at"]) if "started_at" in row.keys() else None,
                       completed_at=_number(row["completed_at"]) if "completed_at" in row.keys() else None)
        if not {"thread_id", "turn_id", "item_id", "item_json", "item_type", "rollout_ordinal"}.issubset(item_columns):
            return latest, tools
        updated = "updated_at_ordinal" if "updated_at_ordinal" in item_columns else "rollout_ordinal"
        sql = (f"SELECT item_id,item_type,rollout_ordinal,{updated} AS updated_ordinal,"
               "length(CAST(item_json AS BLOB)) AS byte_length,"
               "CASE WHEN length(CAST(item_json AS BLOB))<=? THEN item_json ELSE NULL END AS payload "
               "FROM thread_items WHERE thread_id=? AND turn_id=?")
        args = [MAX_ITEM_BYTES, owner["segment_id"], row["turn_id"]]
        if owner.get("end_ordinal") is not None:
            sql += " AND rollout_ordinal<?"
            args.append(owner["end_ordinal"])
        sql += f" ORDER BY {updated} DESC,rollout_ordinal DESC LIMIT ?"
        args.append(MAX_ITEMS + 1)
        total_bytes = seen = 0
        for item in history.execute(sql, args):
            _check(cancel_event)
            seen += 1
            if seen > MAX_ITEMS:
                tools["truncated"] = True
                break
            item_type = item["item_type"]
            if item_type not in _TOOLS:
                continue
            # A frozen inherited cutoff must not borrow a later update from the
            # ancestor's live projection. That older version is not available.
            if owner.get("end_ordinal") is not None and item["updated_ordinal"] >= owner["end_ordinal"]:
                tools["unknown_count"] += 1
                continue
            total_bytes += int(item["byte_length"] or 0)
            if total_bytes > MAX_ITEM_TOTAL_BYTES:
                tools["truncated"] = True
                tools["unknown_count"] += 1
                break
            if item["payload"] is None:
                tools["truncated"] = True
                tools["unknown_count"] += 1
                continue
            try:
                payload = json.loads(item["payload"])
            except (TypeError, ValueError, UnicodeError):
                tools["unknown_count"] += 1
                continue
            if not isinstance(payload, dict) or payload.get("type") != item_type:
                tools["unknown_count"] += 1
                continue
            tools["inspected_count"] += 1
            status = payload.get("status")
            exit_code = _number(payload.get("exitCode"))
            failed = status in _FAILED or (item_type == "commandExecution" and exit_code is not None and exit_code != 0)
            result = payload.get("result")
            failed = failed or (isinstance(result, dict) and result.get("isError") is True)
            failed = failed or (item_type in {"mcpToolCall", "dynamicToolCall"} and bool(payload.get("error")))
            if failed:
                tools["failed_count"] += 1
                if len(tools["failures"]) < MAX_FAILURES:
                    output = payload.get("aggregatedOutput")
                    codes = [code for code in _KNOWN_CODES if isinstance(output, str) and re.search(r"\b" + code + r"\b", output)]
                    tools["failures"].append({"id": _id(item["item_id"]), "type": item_type,
                                              "status": "failed", "exit_code": exit_code,
                                              "error_codes": codes, "ordinal": _number(item["rollout_ordinal"]),
                                              "updated_ordinal": _number(item["updated_ordinal"])})
            elif status in _PENDING:
                tools["pending_count"] += 1
            elif status == "completed" and (item_type != "commandExecution" or exit_code == 0):
                tools["success_count"] += 1
            else:
                tools["unknown_count"] += 1
        if tools["failed_count"]:
            tools.update(status="failure_records", label="最近回合有失败记录",
                         detail=f"发现 {tools['failed_count']} 条工具失败记录；后续成功不自动证明同一操作已恢复，也不据此断言业务目前失败。")
        elif tools["pending_count"]:
            tools.update(status="pending_records", label="工具记录中有未结束项",
                         detail="存在未结束的持久化工具记录；是否仍在运行需另行实时核验。")
        elif tools["unknown_count"] or tools["truncated"]:
            tools.update(status="unknown", label="部分工具记录未能核验", detail="部分记录格式未知、超过读取上限或位于继承边界之外。")
        elif tools["inspected_count"]:
            tools.update(status="no_failure_observed", label="未发现工具失败记录", detail="仅指所检查回合内的已知工具结果；不证明业务成功。")
        else:
            tools.update(status="no_records", label="最近回合无工具记录", detail="当前检查范围内没有已知类型的工具执行记录。")
        if not fresh:
            tools["detail"] = "仅检查投影内最近回合，原始历史尚未完整同步。" + tools["detail"]
        return latest, tools
    finally:
        history.close()


def _tail_evidence(path: Path, cancel_event: Any) -> dict:
    """Bounded event metadata only, not a second history-continuity algorithm."""
    size = path.stat().st_size
    start = max(0, size - MAX_TAIL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        raw = handle.read(MAX_TAIL_BYTES)
    lines = raw.splitlines()
    if start and lines:
        lines = lines[1:]
    latest = None
    last_record_at = None
    invalid = 0
    for line in lines:
        _check(cancel_event)
        try:
            item = json.loads(line)
        except (ValueError, UnicodeError):
            invalid += 1
            continue
        if not isinstance(item, dict):
            continue
        # Only a timezone-qualified ISO timestamp is displayable metadata.
        # Record text, malformed timestamps and exception messages never pass.
        stamp = item.get("timestamp")
        last_record_at = None
        if isinstance(stamp, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", stamp):
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                last_record_at = parsed.isoformat().replace("+00:00", "Z")
            except ValueError:
                pass
        if item.get("type") != "event_msg":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict) or payload.get("type") not in {"task_started", "task_complete", "turn_aborted"}:
            continue
        latest = {"type": payload["type"], "turn_id": _id(payload.get("turn_id")),
                  "ordinal": _number(item.get("ordinal"))}
    return {"bytes_read": len(raw), "bounded": start > 0, "invalid_records": invalid,
            "latest_turn_event": latest, "last_record_at": last_record_at}


def diagnose_task(paths: switchboard.Paths, thread_id: str, *, progress: Any = None,
                  cancel_event: Any = None) -> dict:
    """Return schema v1; every result including unavailable/cancelled is JSON-safe."""
    started = time.monotonic()
    report = {"schema_version": 1, "thread_id": _id(thread_id), "title": "未命名任务",
              "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
              "elapsed_ms": 0, "summary": "证据不足，尚不能判断任务健康", "tone": "blue",
              "history": _history_card(None),
              "runtime": _card("unknown", "实时运行态未核验", "本次仅查看本地持久化证据；不能据此判定任务空闲、运行或后台进程存活。"),
              "latest_turn": _card("unknown", "最近回合尚不能确认", "尚未读取持久化回合状态。", id=None),
              "tools": _card("unknown", "工具记录尚不能确认", "尚未读取工具执行记录。", failed_count=0, failures=[]),
              "business": _card("unverified", "业务结果未核验", "没有检查项目服务、产物或验收标准；聊天结束、工具退出成功都不等于业务成功。"),
              "recommendations": [], "evidence": {"read_only": True, "runtime_source": "not_queried",
                  "provider_calls": 0, "snapshot_stable": None}, "warnings": []}
    try:
        _progress(progress, "读取任务与历史结构", cancel_event)
        if not _id(thread_id):
            raise _Unavailable
        row = _state(paths, thread_id)
        if row is None:
            report["history"] = _card("unknown", "未找到任务", "当前原生数据库中不存在该任务。", reason="thread_missing")
            raise _Unavailable
        report["title"] = _name(row.get("name"))
        source = Path(row.get("rollout_path") or "")
        segment = switchboard._rollout_segment_id(source, thread_id)
        parts = [{"segment_id": segment or thread_id, "path": str(source)}]
        initial = _fingerprint(paths, thread_id, parts)
        chain = None
        try:
            chain = read_projected_history(paths.codex_home, paths.state_database,
                                           paths.thread_history_database, thread_id,
                                           verify_rollout=False)
            parts = chain["segments"]
        except (HistoryChainError, OSError, ValueError, TypeError):
            pass
        # Detect changes during the initial discovery before taking a full-chain
        # observation; never upgrade a moving file to a corruption conclusion.
        if initial != _fingerprint(paths, thread_id, [{"segment_id": segment or thread_id, "path": str(source)}]):
            report["evidence"]["snapshot_stable"] = False
        before = _fingerprint(paths, thread_id, parts)
        _progress(progress, "正在完整核对历史，取消将在本阶段读取结束后生效", cancel_event)
        deep_bytes = sum(int(part.get("size", 0)) for part in parts) if chain else (source.stat().st_size if source.is_file() else 0)
        deep_allowed = deep_bytes <= MAX_DEEP_BYTES
        status = switchboard.thread_history_projection_status(
            paths, thread_id, include_candidates=False, verify_rollout=deep_allowed)
        _check(cancel_event)
        report["history"] = _history_card(status)
        if not deep_allowed:
            report["warnings"].append("历史规模超过本次深检上限；未完成全量连续性核验。")
        report["evidence"]["deep_scan_complete"] = report["history"]["status"] == "healthy"
        report["evidence"]["segment_count"] = len(parts)
        _progress(progress, "读取最近回合与工具结果", cancel_event)
        report["latest_turn"], report["tools"] = _latest_projected(
            paths, parts, report["history"]["status"] == "healthy", cancel_event)
        if source.is_file():
            report["evidence"]["raw_tail"] = _tail_evidence(source, cancel_event)
        _progress(progress, "复核扫描期间是否发生变化", cancel_event)
        changed = report["evidence"]["snapshot_stable"] is False or before != _fingerprint(paths, thread_id, parts)
        report["evidence"]["snapshot_stable"] = not changed
        if changed:
            report["evidence"]["deep_scan_complete"] = False
            report["history"] = _card("changed", "检查期间历史有更新", "原始文件、投影或任务在扫描期间变化；本次快照不能用于判断历史异常。", reason="changed_during_inspection")
            report["latest_turn"]["scope"] = "changed_snapshot"
            report["latest_turn"]["detail"] = "扫描期间数据已更新，下面仅为途中观察值；请刷新。"
            report["tools"]["detail"] = "扫描期间数据已更新，工具计数仅为途中观察值；请刷新。"
            report["summary"] = "任务历史正在更新，请刷新体检"
            report["recommendations"] = ["等待当前活动稳定后重新体检；不要依据本次结果修复历史。"]
        elif report["history"]["status"] in {"stalled", "unreadable"}:
            report["summary"] = "历史存在异常证据，建议先核验读取链"
            report["tone"] = "orange"
            report["recommendations"] = ["先复核原始历史与投影读取位置；本工具不会自动修复或重试。"]
        elif report["history"]["status"] == "pending":
            report["summary"] = "历史尚未同步完整，暂不能确认最新状态"
            report["tone"] = "orange"
            report["recommendations"] = ["稍后重新体检；若读取位置持续不动，再单独排查历史投影。"]
        elif report["tools"].get("failed_count"):
            report["summary"] = "历史检查已完成；最近回合有工具失败记录" if report["history"]["status"] == "healthy" else "最近投影回合有失败记录，历史仍需核验"
            report["tone"] = "orange"
            report["recommendations"] = ["在原任务中查看对应失败记录和后续恢复证据；不要把旧失败直接当成当前失败，也不要盲目重试未知付费结果。"]
        elif report["latest_turn"]["status"] == "failed":
            report["summary"] = "最近投影回合标记为失败，需查看原任务中的恢复证据"
            report["tone"] = "orange"
            report["recommendations"] = ["查看原任务的失败状态与后续处理；回合失败不等于业务目前失败，不要盲目重试未知付费结果。"]
        elif report["history"]["status"] == "healthy":
            report["summary"] = "历史完整；实时运行与业务结果仍未核验"
            report["recommendations"] = ["如需确认后台工作是否完成，请另行核验目标进程、服务或产物。"]
        else:
            report["recommendations"] = ["缺少完整证据；请先核验历史格式、数据库可读性或重新体检。"]
        if report["tools"].get("truncated"):
            report["warnings"].append("工具记录达到有界读取上限，计数可能不完整。")
    except _Cancelled:
        report["history"] = _card("cancelled", "体检已取消", "未完成本次检查，没有执行任何修复。", reason="cancelled")
        report["summary"] = "体检已取消，不能据此判断任务健康"
        report["recommendations"] = ["需要时重新开始体检。"]
        report["evidence"]["snapshot_stable"] = None
        report["evidence"]["deep_scan_complete"] = False
    except (sqlite3.Error, OSError, ValueError, TypeError, _Unavailable):
        if report["history"].get("reason") != "thread_missing":
            report["history"] = _card("unknown", "历史尚不能确认", "证据读取未能完整结束，不能据此判定历史正常或损坏。", reason="inspection_incomplete")
        report["summary"] = "部分本地证据无法读取，暂不能判断任务健康"
        report["warnings"].append("本地证据缺失、格式不支持或暂不可读；错误正文未展示。")
        report["recommendations"] = ["确认任务仍存在并稍后重试；本工具不会修复或修改数据库。"]
        report["evidence"]["snapshot_stable"] = None
        report["evidence"]["deep_scan_complete"] = False
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读体检一个 Codex 任务（不启动进程、不调用模型）")
    parser.add_argument("--home", type=Path, default=switchboard.DEFAULT_CODEX_HOME)
    parser.add_argument("--thread", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(diagnose_task(switchboard.Paths(args.home), args.thread), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
