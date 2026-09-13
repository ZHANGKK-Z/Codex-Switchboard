"""Confirmed, restart-surviving history maintenance; never calls a Provider.

The existing conversion mutex serializes receipt writes and the engine's CAS.
Immutable plan.json owns consent evidence, not task state. operation.json owns
only maintenance progress. A dispatch intent is never automatically retried.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import switchboard
import independent_worker
import projection_recovery
from handoff_bundle import atomic_json


TERMINAL = {"verified", "cancelled", "invalidated"}
WAITING = {"queued", "waiting_exit"}
MAX_RECEIPT_BYTES = 2 * 1024 * 1024


class RecoveryJobError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unlinked(path: Path) -> Path:
    """Check before resolve: junctions must not erase their own evidence."""
    path = Path(os.path.abspath(path))
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise RecoveryJobError("恢复记录路径包含链接，已停止")
        try:
            stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if getattr(stat, "st_file_attributes", 0) & 0x400:
            raise RecoveryJobError("恢复记录路径包含重解析点，已停止")
        if candidate.is_file() and stat.st_nlink != 1:
            raise RecoveryJobError("恢复记录不是独占普通文件，已停止")
    return path


def operation_folder(paths: switchboard.Paths, operation_id: str) -> Path:
    try:
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise RecoveryJobError("恢复操作 ID 不正确") from exc
    return _unlinked(paths.switchboard / "recoveries" / operation_id)


def _read(path: Path) -> dict:
    path = _unlinked(path)
    if path.stat().st_size > MAX_RECEIPT_BYTES:
        raise RecoveryJobError("恢复记录超出读取上限")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RecoveryJobError("恢复记录格式错误")
    return value


def load_job(paths: switchboard.Paths, operation_id: str) -> dict:
    folder = operation_folder(paths, operation_id)
    job = _read(folder / "operation.json")
    plan = _read(folder / "plan.json")
    # The engine owns plan canonicalization and its authorization fingerprint.
    try:
        plan = projection_recovery._validated_plan(paths, plan)
    except (projection_recovery.RecoveryError, ValueError, KeyError, TypeError) as exc:
        raise RecoveryJobError("冻结恢复计划已变化，禁止执行或覆盖") from exc
    if (job.get("schema") != 1 or job.get("id") != operation_id
            or job.get("plan_sha256") != plan.get("plan_sha256")
            or str(plan.get("home", "")).casefold() != str(_unlinked(paths.codex_home)).casefold()):
        raise RecoveryJobError("恢复回执与本次计划身份不一致，禁止重建覆盖")
    job["plan"] = plan
    return job


def _save(paths: switchboard.Paths, job: dict) -> None:
    # Caller owns the conversion mutex (including execute_plan's callback).
    body = {key: value for key, value in job.items() if key not in {"plan", "worker_alive", "cancel_requested"}}
    body["updated_at"] = _now()
    path = operation_folder(paths, body["id"]) / "operation.json"
    _unlinked(path)
    _unlinked(path.with_name(path.name + ".tmp"))
    atomic_json(path, body)


def _records(paths: switchboard.Paths) -> list[Path]:
    root = _unlinked(paths.switchboard / "recoveries")
    if not root.exists():
        return []
    entries = list(root.iterdir())
    if len(entries) > 2000:
        raise RecoveryJobError("恢复记录过多，请人工整理后再操作")
    result = []
    for entry in entries:
        _unlinked(entry)
        if not entry.is_dir():
            raise RecoveryJobError("恢复目录含未知文件，请人工核对")
        operation_folder(paths, entry.name)
        result.append(entry / "operation.json")
    return sorted(result, key=lambda file: file.stat().st_mtime_ns, reverse=True)


def _worker_alive(identity: dict | None) -> bool | None:
    if not identity:
        return None
    try:
        actual = independent_worker.process_identity(identity["pid"])
        return bool(actual.get("alive") and actual.get("creationTime") == identity.get("creationTime"))
    except Exception as exc:
        if getattr(exc, "winerror", None) == 87:
            return False  # Windows explicitly reports that this PID is absent.
        return None  # Permission/probe failure is not evidence of process exit.


def list_jobs(paths: switchboard.Paths) -> list[dict]:
    result = []
    for file in _records(paths)[:50]:
        try:
            job = load_job(paths, file.parent.name)
            job["worker_alive"] = _worker_alive(job.get("worker"))
            job["cancel_requested"] = (file.parent / "cancel.request").exists()
            if job["status"] not in TERMINAL | {"pending_native_replay"} and job["worker_alive"] is False:
                job["status"] = "needs_reconciliation"
                job["message"] = "后台已退出；先只读核验本次结果，不会自动重试。"
            result.append(job)
        except (OSError, ValueError, RecoveryJobError):
            result.append({"id": file.parent.name, "status": "needs_reconciliation",
                           "message": "回执损坏或身份不匹配，需人工核对；禁止重建覆盖。", "plan": {}})
    return result


def enqueue_recovery(paths: switchboard.Paths, plan: dict, *, consent: bool) -> dict:
    if consent is not True:
        raise RecoveryJobError("必须明确确认本次历史恢复")
    # Deep copy freezes the caller's selection before any background dispatch.
    frozen = json.loads(json.dumps(plan, ensure_ascii=False))
    if not frozen.get("plan_sha256") or not frozen.get("root_task_id"):
        raise RecoveryJobError("缺少完整恢复预览，不能开始")
    with switchboard.conversion_operation_lock(paths):
        # Malformed receipts fail closed, and the scan includes older records.
        for file in _records(paths):
            old = load_job(paths, file.parent.name)
            if old["plan"].get("root_task_id") == frozen["root_task_id"] and old["status"] not in TERMINAL:
                return {**old, "reused": True}
        fresh = projection_recovery.preview_recovery(paths, frozen["root_task_id"])
        if not fresh.get("supported") or fresh.get("plan") != frozen:
            raise RecoveryJobError("预览后证据已变化；未入队，请重新预览并确认")
        operation_id = str(uuid.uuid4())
        folder = operation_folder(paths, operation_id)
        folder.mkdir(parents=True, exist_ok=False)
        with (folder / "plan.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(frozen, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        job = {"schema": 1, "id": operation_id, "plan_sha256": frozen["plan_sha256"],
               "status": "queued", "created_at": _now(), "confirmed_at": _now(),
               "message": "已确认；正在验证独立后台。", "dispatch_intent": True}
        _save(paths, job)  # Durable intent precedes any process launch.
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--recovery-worker", "--home", str(paths.codex_home), "--operation", operation_id]
    else:
        command = [sys.executable, str(Path(__file__).resolve()), "--home", str(paths.codex_home), "--operation", operation_id]
    try:
        receipt = independent_worker.launch_independent(command, cwd=folder)
    except Exception:
        with switchboard.conversion_operation_lock(paths):
            job = load_job(paths, operation_id)
            if not job.get("worker") and job["status"] in {"queued", "launch_unknown"}:
                job.update(status="launch_unknown", message="后台启动结果未确认；禁止自动重试，请查看恢复记录。")
                _save(paths, job)
        return job
    with switchboard.conversion_operation_lock(paths):
        job = load_job(paths, operation_id)
        job["launcher"] = receipt
        _save(paths, job)
    return job


def request_cancel(paths: switchboard.Paths, operation_id: str) -> dict:
    with switchboard.conversion_operation_lock(paths):
        job = load_job(paths, operation_id)
        if job["status"] not in WAITING:
            raise RecoveryJobError("已进入核验或写入阶段，不能取消；请只读核验结果")
        path = _unlinked(operation_folder(paths, operation_id) / "cancel.request")
        if not path.exists():
            with path.open("x", encoding="ascii") as handle:
                handle.write("cancel\n")
                handle.flush()
                os.fsync(handle.fileno())
    return {"id": operation_id, "message": "已请求取消；后台确认前不要把它视为已取消。"}


def run_worker(paths: switchboard.Paths, operation_id: str, *, process_probe=None,
               sleep_fn=time.sleep, timeout_seconds=1800, poll_seconds=2.0) -> dict:
    independent_worker.require_current_independent()
    identity = independent_worker.process_identity(os.getpid())
    probe = process_probe or projection_recovery.blocking_writers
    with switchboard.conversion_operation_lock(paths):
        job = load_job(paths, operation_id)
        # One worker may claim a dispatched operation only once, even after death.
        if job.get("worker") or job["status"] not in {"queued", "launch_unknown"}:
            raise RecoveryJobError("本次后台已有执行记录，禁止重新执行")
        job.update(worker=identity, status="waiting_exit", message="独立后台已验证；请自行完全退出 Codex。连续三次确认后才继续。")
        _save(paths, job)
    folder = operation_folder(paths, operation_id)
    started = time.monotonic()
    empty = 0
    try:
        while empty < 3:
            if (folder / "cancel.request").exists():
                with switchboard.conversion_operation_lock(paths):
                    job = load_job(paths, operation_id)
                    job.update(status="cancelled", message="已取消，未修改历史数据库。")
                    _save(paths, job)
                return job
            if time.monotonic() - started >= timeout_seconds:
                raise RecoveryJobError("等待退出超时；未执行恢复，请重新预览后确认")
            blocked = probe(paths)
            if not isinstance(blocked, list):
                raise RecoveryJobError("退出检查返回格式错误，已停止")
            empty = 0 if blocked else empty + 1
            if empty < 3:
                sleep_fn(poll_seconds)
        with switchboard.conversion_operation_lock(paths):
            job = load_job(paths, operation_id)
            if (folder / "cancel.request").exists():
                job.update(status="cancelled", message="已取消，未修改历史数据库。")
                _save(paths, job)
                return job
            job.update(status="revalidating", message="正在重验已确认的同一份证据；发生变化将停止。")
            _save(paths, job)

        def backup_ready(backups):
            current = load_job(paths, operation_id)
            current.update(status="advancing_cursor", backups=backups, cas_intent=True,
                           message="备份完成，已记录写入意图；此后异常必须先核验，不能重试。")
            _save(paths, current)

        result = projection_recovery.execute_plan(paths, job["plan"], process_probe=probe, backup_ready=backup_ready)
        with switchboard.conversion_operation_lock(paths):
            job = load_job(paths, operation_id)
            status = result.get("status")
            mapped = {"cursor_repaired_pending_replay": "pending_native_replay", "pending_replay": "pending_native_replay",
                      "pending_native_replay": "pending_native_replay", "verified": "verified"}.get(status, "needs_reconciliation")
            job.update(status=mapped, result=result, message=("读取位置已调整；请自行重开 Codex、打开原任务，然后点击只读核验。"
                       if mapped == "pending_native_replay" else "请只读核验本次原生历史；不得重复执行。"))
            _save(paths, job)
        return job
    except Exception:
        with switchboard.conversion_operation_lock(paths):
            job = load_job(paths, operation_id)
            job.update(status="needs_reconciliation" if job.get("cas_intent") else "invalidated",
                       message="执行未能确认；保留计划和备份，请先只读核验。" if job.get("cas_intent") else "未进入数据库写入；进程检查或冻结证据未通过，请重新预览。")
            _save(paths, job)
        return job


def reconcile_job(paths: switchboard.Paths, operation_id: str) -> dict:
    with switchboard.conversion_operation_lock(paths):
        job = load_job(paths, operation_id)
        # Reconciliation must not overwrite a waiting/executing worker's state.
        # With no claim, this lock may revoke a late worker's authorization:
        # the worker must acquire this same lock and still see an eligible state.
        if job.get("worker") and job["status"] in WAITING | {"revalidating", "backing_up", "advancing_cursor"}:
            alive = _worker_alive(job.get("worker"))
            if alive is not False:
                raise RecoveryJobError("后台仍存活或存活状态未知；请等待后台结束再核验")
        result = projection_recovery.reconcile_plan(paths, job["plan"])
        status = result.get("status")
        mapped = {"verified": "verified", "pending_replay": "pending_native_replay",
                  "cursor_repaired_pending_replay": "pending_native_replay",
                  "pending_native_replay": "pending_native_replay", "not_applied": "invalidated"}.get(status, "needs_reconciliation")
        job.update(status=mapped, reconciliation=result,
                   message={"verified": "原生历史核验通过；不代表工具、后台业务或项目成功。",
                            "pending_native_replay": "尚未完成原生回放；请重开 Codex 并打开原任务，再核验。",
                            "invalidated": "原读取位置未改变；本次未应用。需要恢复时请重新预览并确认。",
                            "needs_reconciliation": "无法确认结果；禁止重复执行或自动还原备份，请人工核对。"}[mapped])
        _save(paths, job)
        return job


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Confirmed independent history recovery worker")
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args(argv)
    try:
        run_worker(switchboard.Paths(_unlinked(args.home)), args.operation)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
