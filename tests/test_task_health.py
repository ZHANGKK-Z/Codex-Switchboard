import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import switchboard
import task_health

THREAD = "11111111-1111-1111-1111-111111111111"
SEGMENT = "22222222-2222-2222-2222-222222222222"
TURN = "turn-1"
SECRET = "sk-this-is-a-secret-not-for-a-health-report"


def _line(ordinal, kind, payload):
    return (json.dumps({"ordinal": ordinal, "type": kind, "payload": payload}) + "\n").encode()


def fixture(home, *, tool_items=None, turn_status="completed", pending=False, stalled=False):
    paths = switchboard.Paths(home)
    sessions = home / "sessions"
    sessions.mkdir()
    rollout = sessions / f"{THREAD}.jsonl"
    lines = [_line(0, "session_meta", {"id": THREAD}),
             _line(1, "event_msg", {"type": "task_started", "turn_id": TURN}),
             _line(2, "response_item", {"type": "message", "role": "assistant", "text": SECRET}),
             _line(3, "event_msg", {"type": "task_complete", "turn_id": TURN}),
             _line(4, "event_msg", {"type": "item_completed", "turn_id": TURN})]
    if stalled:
        lines.append(_line(4, "event_msg", {"type": "item_completed", "turn_id": TURN}))
    rollout.write_bytes(b"".join(lines))
    state = sqlite3.connect(paths.state_database)
    state.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, name TEXT, title TEXT, first_user_message TEXT, rollout_path TEXT, updated_at_ms INTEGER)")
    state.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (THREAD, "体检样例", SECRET, SECRET, str(rollout), 1))
    state.commit()
    state.close()
    history = sqlite3.connect(paths.thread_history_database)
    history.executescript("""
        CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER);
        CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,status TEXT,started_at INTEGER,completed_at INTEGER,rollout_end_ordinal INTEGER);
        CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INTEGER,item_json TEXT,item_type TEXT,updated_at_ordinal INTEGER);
    """)
    offset = sum(map(len, lines[:2])) if pending else rollout.stat().st_size
    ordinal = 2 if pending else 5
    history.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (THREAD, offset, ordinal))
    history.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)", (THREAD, TURN, 1, turn_status, 10, 20 if turn_status == "completed" else None, 3 if turn_status == "completed" else None))
    for index, item in enumerate(tool_items or []):
        payload = {"type": "commandExecution", "id": f"command-{index}", "status": "completed", "exitCode": 0,
                   "command": SECRET, "aggregatedOutput": SECRET, **item}
        ordinal = payload.pop("ordinal", 2)
        updated = payload.pop("updated", 4)
        history.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)", (THREAD, TURN, payload["id"], ordinal, json.dumps(payload), payload["type"], updated))
    history.commit()
    history.close()
    return paths, rollout


class TaskHealthTests(unittest.TestCase):
    def test_complete_history_does_not_mean_business_success(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), tool_items=[{}])
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["history"]["status"], "healthy")
        self.assertEqual(report["latest_turn"]["status"], "completed")
        self.assertEqual(report["tools"]["status"], "no_failure_observed")
        self.assertEqual(report["runtime"]["status"], "unknown")
        self.assertEqual(report["business"]["status"], "unverified")

    def test_completed_turn_still_reports_late_failed_background_command(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), tool_items=[{"status": "failed", "exitCode": 1,
                "aggregatedOutput": f"PILOT_PROCESS_INSPECTION_FAILED {SECRET}", "ordinal": 2, "updated": 4}])
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["history"]["status"], "healthy")
        self.assertEqual(report["latest_turn"]["status"], "completed")
        self.assertEqual(report["tools"]["failed_count"], 1)
        self.assertEqual(report["tools"]["failures"][0]["updated_ordinal"], 4)
        self.assertEqual(report["tools"]["failures"][0]["error_codes"], ["PILOT_PROCESS_INSPECTION_FAILED"])
        self.assertEqual(report["tone"], "orange")
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn("command\"", json.dumps(report))

    def test_failed_then_success_keeps_fact_but_does_not_claim_current_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), tool_items=[{"status": "failed", "exitCode": 1, "updated": 2}, {"updated": 4}])
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["tools"]["failed_count"], 1)
        self.assertEqual(report["tools"]["success_count"], 1)
        self.assertIn("不据此断言业务目前失败", report["tools"]["detail"])

    def test_pending_does_not_mislabel_projection_as_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), pending=True)
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["history"]["status"], "pending")
        self.assertEqual(report["latest_turn"]["scope"], "projection_only")
        self.assertIn("非已确认的最新回合", report["latest_turn"]["detail"])
        self.assertEqual(report["evidence"]["raw_tail"]["latest_turn_event"]["type"], "task_complete")

    def test_v6_style_duplicate_is_history_issue_not_runtime_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), stalled=True)
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["history"]["status"], "stalled")
        self.assertEqual(report["runtime"]["status"], "unknown")
        self.assertEqual(report["tools"]["failed_count"], 0)

    def test_running_record_is_not_live_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), turn_status="inProgress", tool_items=[{"status": "inProgress", "exitCode": None}])
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["latest_turn"]["status"], "unfinished")
        self.assertEqual(report["runtime"]["status"], "unknown")
        self.assertEqual(report["tools"]["status"], "pending_records")

    def test_changed_raw_snapshot_invalidates_damage_result(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, rollout = fixture(Path(directory), stalled=True)
            original = switchboard.thread_history_projection_status
            def moving(*args, **kwargs):
                result = original(*args, **kwargs)
                with rollout.open("ab") as handle:
                    handle.write(_line(5, "event_msg", {"type": "token_count"}))
                return result
            with mock.patch.object(switchboard, "thread_history_projection_status", side_effect=moving):
                report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["history"]["status"], "changed")
        self.assertFalse(report["evidence"]["snapshot_stable"])
        self.assertEqual(report["tone"], "blue")

    def test_changed_projection_invalidates_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory))
            def progress(stage):
                if "读取最近回合" in stage:
                    connection = sqlite3.connect(paths.thread_history_database)
                    connection.execute("UPDATE thread_history_projection_state SET next_rollout_ordinal=99")
                    connection.commit()
                    connection.close()
            report = task_health.diagnose_task(paths, THREAD, progress=progress)
        self.assertEqual(report["history"]["status"], "changed")

    def test_cancel_before_start_reads_nothing(self):
        event = threading.Event()
        event.set()
        with mock.patch.object(task_health, "_connect", side_effect=AssertionError("must not read")):
            report = task_health.diagnose_task(switchboard.Paths(Path("missing")), THREAD, cancel_event=event)
        self.assertEqual(report["history"]["status"], "cancelled")
        self.assertFalse(report["evidence"]["deep_scan_complete"])

    def test_cancel_after_deep_stage_discards_completed_check(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory))
            event = threading.Event()
            original = switchboard.thread_history_projection_status
            def cancelled(*args, **kwargs):
                result = original(*args, **kwargs)
                event.set()
                return result
            with mock.patch.object(switchboard, "thread_history_projection_status", side_effect=cancelled):
                report = task_health.diagnose_task(paths, THREAD, cancel_event=event)
        self.assertEqual(report["history"]["status"], "cancelled")
        self.assertFalse(report["evidence"]["deep_scan_complete"])

    def test_post_scan_read_failure_discards_history_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory))
            with mock.patch.object(task_health, "_latest_projected", side_effect=OSError(SECRET)):
                report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["history"]["status"], "unknown")
        self.assertFalse(report["evidence"]["deep_scan_complete"])
        self.assertIsNone(report["evidence"]["snapshot_stable"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_failed_turn_without_tool_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), turn_status="failed")
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["latest_turn"]["status"], "failed")
        self.assertEqual(report["tone"], "orange")
        self.assertIn("回合标记为失败", report["summary"])

    def test_bounded_tail_timestamp_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tail.jsonl"
            path.write_text(json.dumps({"timestamp": "2026-09-12T01:02:03.123Z", "ordinal": 1, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": TURN}}) + "\n", encoding="utf-8")
            result = task_health._tail_evidence(path, None)
            self.assertEqual(result["last_record_at"], "2026-09-12T01:02:03.123000Z")
            path.write_text(json.dumps({"timestamp": SECRET, "ordinal": 2, "type": "event_msg", "payload": {}}) + "\n", encoding="utf-8")
            result = task_health._tail_evidence(path, None)
        self.assertIsNone(result["last_record_at"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_item_total_budget_stops_iteration(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), tool_items=[{}, {}, {}])
            with mock.patch.object(task_health, "MAX_ITEM_TOTAL_BYTES", 1):
                report = task_health.diagnose_task(paths, THREAD)
        self.assertTrue(report["tools"]["truncated"])
        self.assertEqual(report["tools"]["unknown_count"], 1)
        self.assertEqual(report["tools"]["inspected_count"], 0)

    def test_mcp_is_error_is_failure_without_exposing_error(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), tool_items=[{"type": "mcpToolCall", "status": "completed", "exitCode": None,
                "result": {"isError": True, "content": [{"text": SECRET}]}}])
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["tools"]["failed_count"], 1)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_missing_database_and_unknown_schema_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            report = task_health.diagnose_task(paths, THREAD)
            self.assertEqual(report["history"]["status"], "unknown")
            self.assertFalse(paths.state_database.exists())
            paths, _ = fixture(Path(directory))
            connection = sqlite3.connect(paths.thread_history_database)
            connection.execute("DROP TABLE thread_turns")
            connection.commit()
            connection.close()
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["history"]["status"], "unknown")

    def test_unknown_item_json_and_oversized_blob_are_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), tool_items=[{}])
            connection = sqlite3.connect(paths.thread_history_database)
            connection.execute("UPDATE thread_items SET item_json=?", (json.dumps([SECRET]),))
            connection.commit()
            connection.close()
            report = task_health.diagnose_task(paths, THREAD)
            self.assertEqual(report["tools"]["status"], "unknown")
            with mock.patch.object(task_health, "MAX_ITEM_BYTES", 1):
                report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["tools"]["status"], "unknown")
        self.assertTrue(report["tools"]["truncated"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_large_history_skips_full_scan_with_explicit_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory))
            with mock.patch.object(task_health, "MAX_DEEP_BYTES", 1):
                report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["history"]["status"], "unchecked")
        self.assertFalse(report["evidence"]["deep_scan_complete"])
        self.assertTrue(report["warnings"])

    def test_zero_turn_latest_segment_inherits_frozen_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, parent = fixture(Path(directory), tool_items=[{}])
            child = parent.with_name(f"{THREAD}_{SEGMENT}.jsonl")
            child.write_bytes(_line(5, "session_meta", {"id": THREAD, "history_base": {
                "thread_id": THREAD, "end_byte_offset": parent.stat().st_size,
                "end_ordinal_exclusive": 5}}))
            connection = sqlite3.connect(paths.state_database)
            connection.execute("UPDATE threads SET rollout_path=?", (str(child),))
            connection.commit()
            connection.close()
            connection = sqlite3.connect(paths.thread_history_database)
            connection.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (SEGMENT, child.stat().st_size, 6))
            connection.commit()
            connection.close()
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["history"]["status"], "healthy")
        self.assertEqual(report["history"]["history_segment_count"], 2)
        self.assertEqual(report["latest_turn"]["id"], TURN)

    def test_native_name_only_no_prompt_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory))
            connection = sqlite3.connect(paths.state_database)
            connection.execute("UPDATE threads SET name=NULL")
            connection.commit()
            connection.close()
            report = task_health.diagnose_task(paths, THREAD)
        self.assertEqual(report["title"], "未命名任务")
        self.assertNotIn(SECRET, json.dumps(report))

    def test_cli_outputs_same_contract_and_does_not_write_files(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = fixture(Path(directory), tool_items=[{}])
            before = {str(path): path.read_bytes() for path in Path(directory).rglob("*") if path.is_file()}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = task_health.main(["--home", directory, "--thread", THREAD])
            after = {str(path): path.read_bytes() for path in Path(directory).rglob("*") if path.is_file()}
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["history"]["status"], "healthy")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
