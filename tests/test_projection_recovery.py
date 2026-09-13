from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import projection_recovery as recovery
import switchboard


ROOT = "11111111-1111-4111-8111-111111111111"
SEGMENT = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
TURN = "44444444-4444-4444-8444-444444444444"
FINAL_TEXT = "native final answer"


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.sessions = self.home / "sessions" / "2026" / "09" / "12"
        self.sessions.mkdir(parents=True)
        self.paths = switchboard.Paths(self.home)
        self.raw = self.sessions / f"rollout-2026-09-12T12-00-00-{ROOT}_{SEGMENT}.jsonl"
        self.rows = self._rows()
        self._write_rows()
        state = sqlite3.connect(self.paths.state_database)
        state.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,name TEXT,cwd TEXT,"
                      "model_provider TEXT,model TEXT,archived INTEGER,first_user_message TEXT,updated_at INTEGER)")
        state.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)",
                      (ROOT, str(self.raw), "fixture", str(self.home / "workspace"), "openai", "test-model", 0,
                       "SECRET FIRST USER PROMPT MUST NOT LEAK", 100))
        state.commit()
        state.close()
        history = sqlite3.connect(self.paths.thread_history_database)
        history.executescript("""
            CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,
                next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER);
            CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,
                rollout_ordinal INTEGER,rollout_end_ordinal INTEGER);
            CREATE TABLE thread_items(thread_id TEXT,item_type TEXT,rollout_ordinal INTEGER,item_json TEXT);
            INSERT INTO thread_history_projection_state VALUES('unrelated',900,77);
        """)
        history.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (SEGMENT, self.old, 2))
        history.commit()
        history.close()

    def tearDown(self):
        self.temporary.cleanup()

    def _rows(self, base=None):
        first = base["end_ordinal_exclusive"] if base else 0
        metadata = {"id": ROOT}
        if base:
            metadata["history_base"] = base
        settings = {"type": "thread_settings_applied", "settings": {"model": "test-model", "mode": "safe"}}
        return [
            {"ordinal": first, "type": "session_meta", "payload": metadata},
            {"ordinal": first + 1, "type": "event_msg", "payload": {"type": "token_count"}},
            {"ordinal": first + 1, "type": "event_msg", "payload": settings},
            {"ordinal": first + 2, "type": "event_msg", "payload": copy.deepcopy(settings)},
            {"ordinal": first + 3, "type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN}},
            {"ordinal": first + 4, "type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "AgentMessage", "content": [{"type": "Text", "text": FINAL_TEXT},
                                                     {"type": "memory_citation", "text": "not native UI text"}]}}},
            {"ordinal": first + 5, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": TURN}},
        ]

    def _write_rows(self):
        encoded = [json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n" for row in self.rows]
        self.raw.write_bytes(b"".join(encoded))
        self.old = len(encoded[0]) + len(encoded[1])
        self.new = self.old + len(encoded[2])

    def _set_cursor(self, offset, ordinal):
        connection = sqlite3.connect(self.paths.thread_history_database)
        connection.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?",
                           (offset, ordinal, SEGMENT))
        connection.commit()
        connection.close()

    def _cursor(self):
        connection = sqlite3.connect(self.paths.thread_history_database)
        try:
            return tuple(connection.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?",
                                            (SEGMENT,)).fetchone())
        finally:
            connection.close()

    def _plan(self):
        result = recovery.preview_recovery(self.paths, ROOT)
        self.assertTrue(result["supported"], result)
        return result["plan"]

    def _execute(self, plan=None, **kwargs):
        return recovery.execute_plan(self.paths, plan or self._plan(), process_probe=kwargs.pop("process_probe", lambda _paths: []),
                                     backup_ready=kwargs.pop("backup_ready", lambda _backups: None), **kwargs)

    def _materialize(self, plan, *, text=FINAL_TEXT, completed=True):
        connection = sqlite3.connect(self.paths.thread_history_database)
        for part in [plan["source"], *plan["ancestors"]]:
            for turn in part["completed_turns"]:
                connection.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?)",
                                   (part["segment_id"], turn["turn_id"], "completed" if completed else "in_progress",
                                    turn["end_ordinal"] - 1, turn["end_ordinal"]))
            for final in part["agent_messages"]:
                connection.execute("INSERT INTO thread_items VALUES(?,?,?,?)",
                                   (part["segment_id"], "agentMessage", final["ordinal"], json.dumps({"text": text})))
        connection.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?",
                           (plan["source"]["size"], plan["source"]["eof_ordinal"], SEGMENT))
        connection.commit()
        connection.close()

    def _ancestor(self):
        path = self.sessions / f"rollout-2026-09-11T12-00-00-{ROOT}.jsonl"
        rows = [{"ordinal": 0, "type": "session_meta", "payload": {"id": ROOT}},
                {"ordinal": 1, "type": "event_msg", "payload": {"type": "token_count"}},
                {"ordinal": 2, "type": "event_msg", "payload": {"type": "token_count"}}]
        path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
        base = {"thread_id": ROOT, "end_byte_offset": path.stat().st_size, "end_ordinal_exclusive": 3}
        self.rows = self._rows(base)
        self._write_rows()
        self._set_cursor(self.old, 5)
        connection = sqlite3.connect(self.paths.thread_history_database)
        connection.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (ROOT, path.stat().st_size, 3))
        connection.commit()
        connection.close()
        return path

    def test_preview_is_read_only_and_plan_never_contains_message_or_settings_body(self):
        before = {p: p.read_bytes() for p in (self.raw, self.paths.state_database, self.paths.thread_history_database)}
        plan = self._plan()
        encoded = json.dumps(plan)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn(FINAL_TEXT, encoded)
        self.assertNotIn('"mode": "safe"', encoded)
        self.assertEqual(plan["cursor"], {"old_offset": self.old, "new_offset": self.new, "next_ordinal": 2})
        self.assertEqual(recovery.reconcile_plan(self.paths, plan)["status"], "not_applied")
        self.assertFalse(self.paths.backups.exists())
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_execute_only_updates_offset_preserves_source_state_and_other_rows(self):
        plan = self._plan()
        before = self.raw.read_bytes(), self.paths.state_database.read_bytes()
        callbacks = []
        def ready(backups):
            self.assertEqual(self._cursor(), (self.old, 2))
            self.assertEqual(len(backups), 2)
            self.assertTrue(all(Path(p).is_file() for p in backups))
            callbacks.append(backups)
        result = self._execute(plan, backup_ready=ready)
        self.assertEqual(result["status"], "cursor_repaired_pending_replay", result)
        self.assertEqual(result["commit_state"], "committed")
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(self._cursor(), (self.new, 2))
        self.assertEqual((self.raw.read_bytes(), self.paths.state_database.read_bytes()), before)
        connection = sqlite3.connect(self.paths.thread_history_database)
        self.assertEqual(tuple(connection.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id='unrelated'").fetchone()), (900, 77))
        connection.close()

    def test_missing_identical_repeat_fails_closed(self):
        self.rows[3]["payload"]["settings"]["model"] = "different-model"
        self._write_rows()
        self._set_cursor(self.old, 2)
        result = recovery.preview_recovery(self.paths, ROOT)
        self.assertFalse(result["supported"])
        self.assertEqual(result["reason"], "no_supported_duplicate_with_identical_repeat")

    def test_unsafe_duplicate_or_gap_is_not_supported(self):
        for mutate in (lambda rows: rows[2]["payload"].update(type="assistant_message"),
                       lambda rows: rows[4].update(ordinal=99),
                       lambda rows: rows[2].update(type="response_item")):
            with self.subTest(mutate=mutate):
                self.rows = self._rows()
                mutate(self.rows)
                self._write_rows()
                self._set_cursor(self.old, 2)
                self.assertFalse(recovery.preview_recovery(self.paths, ROOT)["supported"])

    def test_duplicate_elsewhere_than_frozen_cursor_rejected(self):
        self._set_cursor(self.old + 1, 2)
        self.assertFalse(recovery.preview_recovery(self.paths, ROOT)["supported"])

    def test_second_duplicate_is_rejected(self):
        extra = [{"ordinal": 6, "type": "event_msg", "payload": {"type": "token_count"}},
                 {"ordinal": 6, "type": "event_msg", "payload": self.rows[2]["payload"]}]
        self.rows.extend(extra)
        self._write_rows()
        self._set_cursor(self.old, 2)
        self.assertFalse(recovery.preview_recovery(self.paths, ROOT)["supported"])

    def test_missing_native_final_or_terminal_event_rejected(self):
        for index in (5, 6):
            with self.subTest(index=index):
                self.rows = self._rows()
                self.rows[index]["payload"] = {"type": "token_count"}
                self._write_rows()
                self._set_cursor(self.old, 2)
                self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "terminal_native_evidence_missing")

    def test_frozen_source_change_prevents_backup(self):
        plan = self._plan()
        self.rows[5]["payload"]["item"]["content"][0]["text"] = "changed"
        self._write_rows()
        with mock.patch.object(recovery, "_backup") as backup:
            result = self._execute(plan)
        backup.assert_not_called()
        self.assertEqual(result["reason"], "frozen_plan_changed")
        self.assertEqual(self._cursor(), (self.old, 2))

    def test_any_state_column_change_invalidates_authorized_plan(self):
        plan = self._plan()
        connection = sqlite3.connect(self.paths.state_database)
        connection.execute("UPDATE threads SET updated_at=101 WHERE id=?", (ROOT,))
        connection.commit()
        connection.close()
        with mock.patch.object(recovery, "_backup") as backup:
            result = self._execute(plan)
        backup.assert_not_called()
        self.assertEqual(result["reason"], "frozen_plan_changed")

    def test_tampered_plan_fingerprint_rejected(self):
        plan = self._plan()
        plan["cursor"]["new_offset"] += 1
        self.assertEqual(self._execute(plan)["reason"], "plan_fingerprint_mismatch")

    def test_default_process_probe_is_strict_and_does_not_ignore_parent_codex(self):
        rows = [json.dumps({"Name": "codex.exe", "ProcessId": os.getppid()})]
        with mock.patch.object(switchboard, "process_lines", return_value=rows) as probe:
            result = recovery.execute_plan(self.paths, self._plan(), backup_ready=lambda _backups: None)
        probe.assert_called_with(strict=True)
        self.assertEqual(result["reason"], "codex_still_running")
        self.assertFalse(self.paths.backups.exists())

    def test_empty_or_malformed_process_enumeration_never_means_idle(self):
        for rows in ([], ["{}"], [json.dumps({"Name": "codex.exe", "ProcessId": "123"})], ["null"], ["broken"]):
            with self.subTest(rows=rows), mock.patch.object(switchboard, "process_lines", return_value=rows):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.blocking_writers(self.paths)

    def test_injected_probe_invalid_result_is_not_quiescent(self):
        result = self._execute(process_probe=lambda _paths: None)
        self.assertEqual(result["reason"], "process_inspection_failed")
        self.assertEqual(self._cursor(), (self.old, 2))

    def test_writer_lock_nonempty_blocks_zero_marker_is_preserved(self):
        directory = self.home / "thread-writer-locks"
        directory.mkdir()
        marker = directory / "writer.lock"
        marker.write_bytes(b"active")
        with mock.patch.object(recovery, "strict_process_probe", return_value=[]):
            self.assertTrue(recovery.blocking_writers(self.paths))
        self.assertEqual(self._execute()["reason"], "writer_lock_active")
        marker.write_bytes(b"")
        self.assertEqual(self._execute()["status"], "cursor_repaired_pending_replay")
        self.assertTrue(marker.exists())

    def test_backup_intent_callback_cannot_be_omitted_or_fail(self):
        plan = self._plan()
        result = recovery.execute_plan(self.paths, plan, process_probe=lambda _paths: [])
        self.assertEqual(result["reason"], "backup_intent_callback_required")
        def failing(_backups):
            raise RuntimeError("SECRET callback error")
        result = self._execute(plan, backup_ready=failing)
        self.assertEqual(result["status"], "failed_before_commit")
        self.assertEqual(len(result["backups"]), 2)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(self._cursor(), (self.old, 2))

    def test_second_backup_failure_has_no_cas(self):
        original = recovery._backup
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated")
            return original(*args)
        with mock.patch.object(recovery, "_backup", side_effect=fail_second):
            result = self._execute()
        self.assertEqual(result["status"], "failed_before_commit")
        self.assertEqual(len(result["backups"]), 1)
        self.assertEqual(self._cursor(), (self.old, 2))

    def test_backup_api_includes_committed_wal_rows(self):
        connection = sqlite3.connect(self.paths.thread_history_database)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("INSERT INTO thread_history_projection_state VALUES('wal-only',1234,55)")
            connection.commit()
            self.assertTrue(Path(str(self.paths.thread_history_database) + "-wal").exists())
            backup = recovery._backup(self.paths, self.paths.thread_history_database, "wal-test")
        finally:
            connection.close()
        connection = sqlite3.connect(backup)
        self.assertEqual(tuple(connection.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id='wal-only'").fetchone()), (1234, 55))
        connection.close()

    def test_codex_reopen_after_update_rolls_back(self):
        calls = 0
        def probe(_paths):
            nonlocal calls
            calls += 1
            return [{"name": "codex.exe", "pid": 123}] if calls == 9 else []
        result = self._execute(process_probe=probe)
        self.assertEqual(result["status"], "failed_before_commit", result)
        self.assertEqual(result["commit_state"], "not_committed")
        self.assertEqual(self._cursor(), (self.old, 2))

    def test_final_process_probe_mutating_source_cannot_commit_cursor(self):
        calls = 0
        def probe(_paths):
            nonlocal calls
            calls += 1
            if calls == 9:
                self.raw.write_bytes(self.raw.read_bytes().replace(b'"mode": "safe"', b'"mode": "evil"'))
            return []
        result = self._execute(process_probe=probe)
        self.assertIn(result["status"], {"failed_before_commit", "needs_review"}, result)
        self.assertNotEqual(result["commit_state"], "committed")
        self.assertEqual(self._cursor(), (self.old, 2))

    @unittest.skipUnless(os.name == "nt", "Windows sharing-mode source guard")
    def test_windows_source_guard_denies_write_and_delete_until_release(self):
        plan = self._plan()
        original = self.raw.read_bytes()
        with recovery._source_read_guard(plan):
            self.assertEqual(self.raw.read_bytes(), original)
            with self.assertRaises(OSError):
                self.raw.write_bytes(b"bad")
            with self.assertRaises(OSError):
                self.raw.unlink()
        self.assertEqual(self.raw.read_bytes(), original)
        self.raw.write_bytes(original)

    def test_unknown_cursor_and_repeated_execute_never_write_again(self):
        plan = self._plan()
        self.assertEqual(self._execute(plan)["status"], "cursor_repaired_pending_replay")
        with mock.patch.object(recovery, "_backup") as backup:
            repeated = self._execute(plan)
        backup.assert_not_called()
        self.assertEqual(repeated["reason"], "already_applied_or_unknown_cursor")
        self._set_cursor(self.new + 1, 2)
        self.assertEqual(recovery.reconcile_plan(self.paths, plan)["status"], "needs_review")

    def test_commit_success_does_not_claim_history_verified(self):
        plan = self._plan()
        result = self._execute(plan)
        self.assertEqual(result["status"], "cursor_repaired_pending_replay")
        self.assertNotIn("final_message_verified", result)
        self._materialize(plan)
        verified = recovery.reconcile_plan(self.paths, plan)
        self.assertEqual(verified["status"], "verified", verified)
        self.assertEqual(verified["completed_turns_verified"], 1)
        self.assertTrue(verified["final_message_verified"])

    def test_eof_without_completed_turn_or_exact_final_text_is_not_verified(self):
        plan = self._plan()
        self._materialize(plan, text="wrong", completed=False)
        self.assertEqual(recovery.reconcile_plan(self.paths, plan)["reason"], "completed_turn_not_materialized")
        connection = sqlite3.connect(self.paths.thread_history_database)
        connection.execute("UPDATE thread_turns SET status='completed'")
        connection.execute("INSERT INTO thread_items VALUES(?,?,?,?)", (SEGMENT, "agentMessage", 0, json.dumps({"text": FINAL_TEXT})))
        connection.commit()
        connection.close()
        self.assertEqual(recovery.reconcile_plan(self.paths, plan)["reason"], "final_message_not_materialized")

    def test_two_completed_turns_missing_first_native_message_is_not_verified(self):
        first_turn = "55555555-5555-4555-8555-555555555555"
        first_message = copy.deepcopy(self.rows[5])
        self.rows[6]["payload"]["turn_id"] = first_turn
        self.rows += [{"ordinal": 6, "type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN}},
                      {**first_message, "ordinal": 7},
                      {"ordinal": 8, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": TURN}}]
        self._write_rows()
        plan = self._plan()
        self.assertEqual(len(plan["source"]["agent_messages"]), 2)
        self._materialize(plan)
        self.assertEqual(recovery.reconcile_plan(self.paths, plan)["agent_messages_verified"], 2)
        connection = sqlite3.connect(self.paths.thread_history_database)
        connection.execute("DELETE FROM thread_items WHERE rollout_ordinal=4")
        connection.commit()
        connection.close()
        result = recovery.reconcile_plan(self.paths, plan)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "agent_message_not_materialized")

    def test_source_mutation_during_materialization_cannot_return_verified(self):
        plan = self._plan()
        self._materialize(plan)
        original = recovery._materialized
        def mutate_after_items(history, frozen):
            result = original(history, frozen)
            self.raw.write_bytes(self.raw.read_bytes().replace(b'"mode": "safe"', b'"mode": "evil"'))
            return result
        with mock.patch.object(recovery, "_materialized", side_effect=mutate_after_items):
            result = recovery.reconcile_plan(self.paths, plan)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["reason"], "frozen_plan_changed")

    def test_materialized_cursor_and_items_share_one_read_transaction(self):
        plan = self._plan()
        self._materialize(plan)
        original = recovery._materialized
        def assert_transaction(history, frozen):
            self.assertTrue(history.in_transaction)
            return original(history, frozen)
        with mock.patch.object(recovery, "_materialized", side_effect=assert_transaction):
            self.assertEqual(recovery.reconcile_plan(self.paths, plan)["status"], "verified")

    def test_known_delete_trigger_allowed_unknown_update_trigger_rejected(self):
        connection = sqlite3.connect(self.paths.thread_history_database)
        connection.execute("CREATE TABLE thread_realtime_items(thread_id TEXT)")
        connection.execute(recovery._DELETE_TRIGGER)
        connection.commit()
        connection.close()
        self._plan()
        connection = sqlite3.connect(self.paths.thread_history_database)
        connection.execute("CREATE TRIGGER dangerous AFTER UPDATE ON thread_history_projection_state BEGIN DELETE FROM thread_items; END")
        connection.commit()
        connection.close()
        self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "unexpected_projection_trigger")

    def test_schema_change_after_authorization_rejected(self):
        plan = self._plan()
        connection = sqlite3.connect(self.paths.thread_history_database)
        connection.execute("CREATE INDEX new_index ON thread_items(item_type)")
        connection.commit()
        connection.close()
        self.assertEqual(self._execute(plan)["reason"], "frozen_plan_changed")

    def test_ancestor_chain_is_fully_bound_and_missing_parent_rejected(self):
        ancestor = self._ancestor()
        plan = self._plan()
        self.assertEqual(len(plan["ancestors"]), 1)
        self.assertEqual(plan["ancestors"][0]["sha256"], hashlib.sha256(ancestor.read_bytes()).hexdigest())
        ancestor.unlink()
        self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "ancestor_missing_or_ambiguous")

    def test_cycle_and_cutoff_mismatch_rejected(self):
        self._ancestor()
        self.rows[0]["payload"]["history_base"]["thread_id"] = SEGMENT
        self._write_rows()
        self._set_cursor(self.old, 5)
        self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "history_cycle_or_depth_limit")
        self.rows[0]["payload"]["history_base"]["thread_id"] = ROOT
        self.rows[0]["payload"]["history_base"]["end_byte_offset"] -= 1
        self._write_rows()
        self._set_cursor(self.old, 5)
        self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "ancestor_cutoff_ordinal_mismatch")

    def test_ancestor_projection_incomplete_rejected(self):
        self._ancestor()
        connection = sqlite3.connect(self.paths.thread_history_database)
        connection.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=0 WHERE thread_id=?", (ROOT,))
        connection.commit()
        connection.close()
        self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "ancestor_projection_not_at_eof")

    def test_source_oversize_partial_record_and_cancel_fail_closed(self):
        with mock.patch.object(recovery, "MAX_HISTORY_BYTES", 10):
            self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "history_size_limit")
        event = mock.Mock()
        event.is_set.return_value = True
        self.assertEqual(recovery.preview_recovery(self.paths, ROOT, cancel_event=event)["reason"], "cancelled")
        self.raw.write_bytes(self.raw.read_bytes().rstrip(b"\n"))
        self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "incomplete_or_oversized_record")

    def test_duplicate_json_keys_or_nonstandard_numbers_are_rejected(self):
        original = self.raw.read_bytes()
        for before, after, reason in ((b'"ordinal": 0', b'"ordinal": 0, "ordinal": 0', "duplicate_json_key"),
                                      (b'"ordinal": 0', b'"ordinal": NaN', "invalid_json_number")):
            with self.subTest(reason=reason):
                self.raw.write_bytes(original.replace(before, after, 1))
                self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], reason)

    def test_malformed_text_and_nonidentity_turn_id_fail_without_leaking_content(self):
        self.rows[6]["payload"]["turn_id"] = "SECRET arbitrary non-identity text"
        self._write_rows()
        result = recovery.preview_recovery(self.paths, ROOT)
        self.assertEqual(result["reason"], "invalid_completed_turn_identity")
        self.assertNotIn("SECRET", json.dumps(result))
        self.rows = self._rows()
        self.rows[5]["payload"]["item"]["content"][0]["text"] = ["malformed"]
        self._write_rows()
        self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "invalid_native_agent_message")

    def test_hardlinked_database_rejected(self):
        link = self.home / "state-copy.sqlite"
        os.link(self.paths.state_database, link)
        self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "not_unique_regular_file")

    def test_linked_parent_rejected_before_resolve(self):
        original = recovery._linked
        with mock.patch.object(recovery, "_linked", side_effect=lambda path: path == self.home.parent or original(path)):
            self.assertEqual(recovery.preview_recovery(self.paths, ROOT)["reason"], "linked_path")

    def test_result_does_not_leak_arbitrary_sqlite_error(self):
        with mock.patch.object(recovery, "_schema", side_effect=sqlite3.DatabaseError("SECRET SQL BODY")):
            result = recovery.preview_recovery(self.paths, ROOT)
        self.assertEqual(result["reason"], "inspection_failed")
        self.assertNotIn("SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
