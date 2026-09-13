import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import repair_stalled_history as repair
import switchboard


SOURCE_IDS = (
    "01a02dbd-4d0d-7861-a549-04b197c88544",
    "01a02dbd-4d0d-7861-a549-04b197c88545",
)
SEGMENT_IDS = (
    "01a03111-1111-7111-8111-111111111111",
    "01a03222-2222-7222-8222-222222222222",
)
PINNED_SECTION_ID = "01984de2-1111-7111-8111-111111111111"


def _line(kind, payload, ordinal=None):
    item = {"type": kind, "payload": payload}
    if ordinal is not None:
        item["ordinal"] = ordinal
    return json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def create_fixture(root, count=1, *, duplicate_payload_type="thread_settings_applied"):
    home = Path(root) / "home"
    sessions = home / "sessions" / "2026" / "08" / "30"
    sessions.mkdir(parents=True)
    state_path = home / "state_5.sqlite"
    history_path = home / "thread_history_1.sqlite"
    state = sqlite3.connect(state_path)
    history = sqlite3.connect(history_path)
    state.execute(
        """
        create table threads(
            id text primary key,
            rollout_path text not null,
            model_provider text not null,
            model text not null,
            cwd text not null,
            name text not null,
            archived integer not null,
            thread_section_id text,
            section_position integer,
            section_entered_at_ms integer
        )
        """
    )
    state.execute(
        "create table thread_sections(id text primary key, name text not null, appearance text)"
    )
    state.execute(
        "insert into thread_sections values(?,?,?)",
        (PINNED_SECTION_ID, "Pinned", "{}"),
    )
    history.execute(
        """
        create table thread_history_projection_state(
            thread_id text primary key,
            next_rollout_byte_offset integer not null,
            next_rollout_ordinal integer not null
        )
        """
    )
    specs = []
    rollout_bytes = {}
    for index in range(count):
        source_id = SOURCE_IDS[index]
        segment_id = SEGMENT_IDS[index]
        correct = f"correct-turn-{index}"
        excluded = f"excluded-turn-{index}"
        parts = [
            _line("session_meta", {"id": source_id, "session_id": source_id}),
            _line("turn_context", {"turn_id": correct}, 10),
        ]
        expected_offset = sum(map(len, parts))
        parts.extend(
            [
                _line("event_msg", {"type": duplicate_payload_type}, 11),
                _line("turn_context", {"turn_id": excluded}, 12),
                _line("response_item", {"type": "message"}, 13),
            ]
        )
        payload = b"".join(parts)
        rollout = sessions / f"rollout-2026-08-30-{source_id}_{segment_id}.jsonl"
        rollout.write_bytes(payload)
        rollout_bytes[source_id] = payload
        cwd = str(Path(root) / f"workspace-{index}")
        Path(cwd).mkdir()
        source_position = (index + 1) * 10_000_000
        state.execute(
            "insert into threads values(?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                str(rollout),
                "openai",
                "gpt-5.6-sol",
                cwd,
                f"task-{index}",
                0,
                PINNED_SECTION_ID,
                source_position,
                1_000 + index,
            ),
        )
        state.execute(
            "insert into threads values(?,?,?,?,?,?,?,?,?,?)",
            (
                f"sibling-{index}",
                str(rollout),
                "openai",
                "gpt-5.6-sol",
                cwd,
                f"sibling task {index}",
                0,
                PINNED_SECTION_ID,
                source_position + 1_000_000,
                2_000 + index,
            ),
        )
        history.execute(
            "insert into thread_history_projection_state values(?,?,?)",
            (segment_id, expected_offset, 12),
        )
        specs.append(
            repair.RepairSpec(
                label=f"repair-{index}",
                source_thread_id=source_id,
                segment_id=segment_id,
                expected_offset=expected_offset,
                expected_ordinal=12,
                correct_last_turn_id=correct,
                excluded_turn_id=excluded,
                new_name=f"clean-{index}",
                source_archive_name=f"task-{index}（错误继续）",
                expected_provider="openai",
                expected_model="gpt-5.6-sol",
            )
        )
    state.commit()
    history.commit()
    state.close()
    history.close()
    return home, tuple(specs), rollout_bytes


class FakeAppServer:
    def __init__(self, paths, plan, *, fail_move=False):
        self.paths = paths
        self.plan = plan
        self.fail_move = fail_move
        self.started = False
        self.closed = False
        self.capabilities = None
        self.fork_calls = []
        self.move_calls = []
        self.unarchive_calls = []
        self.new_ids = {}

    def start(self):
        self.started = True

    def initialize(self, *, capabilities):
        self.capabilities = capabilities
        return {"codexHome": str(self.paths.codex_home)}

    def _thread(self, item, thread_id, turns, *, forked_from=None):
        value = {
            "id": thread_id,
            "cwd": item.state_snapshot["cwd"],
            "modelProvider": item.spec.expected_provider,
            "model": item.spec.expected_model,
            "turns": [{"id": turn_id} for turn_id in turns],
        }
        if forked_from is not None:
            value["forkedFromId"] = forked_from
        return value

    def fork_thread_at(self, thread_id, *, last_turn_id, model_provider, model):
        item = next(value for value in self.plan.repairs if value.spec.source_thread_id == thread_id)
        history = sqlite3.connect(self.paths.thread_history_database)
        history.execute(
            "update thread_history_projection_state set next_rollout_byte_offset=? where thread_id=?",
            (item.rollout_size, item.spec.segment_id),
        )
        history.commit()
        history.close()
        self.fork_calls.append(
            {
                "thread_id": thread_id,
                "last_turn_id": last_turn_id,
                "model_provider": model_provider,
                "model": model,
            }
        )
        new_id = f"new-{len(self.new_ids)}"
        self.new_ids[thread_id] = new_id
        connection = sqlite3.connect(self.paths.state_database)
        connection.execute(
            "insert into threads values(?,?,?,?,?,?,?,?,?,?)",
            (
                new_id,
                item.state_snapshot["rollout_path"],
                model_provider,
                model,
                item.state_snapshot["cwd"],
                "new task",
                0,
                None,
                None,
                None,
            ),
        )
        connection.commit()
        connection.close()
        return {
            "thread": self._thread(
                item,
                new_id,
                [last_turn_id],
                forked_from=thread_id,
            )
        }

    def read_thread(self, thread_id, **_kwargs):
        source_id = next(source for source, new_id in self.new_ids.items() if new_id == thread_id)
        item = next(value for value in self.plan.repairs if value.spec.source_thread_id == source_id)
        return {"thread": self._thread(item, thread_id, [item.spec.correct_last_turn_id])}

    def _update(self, sql, parameters):
        connection = sqlite3.connect(self.paths.state_database)
        connection.execute(sql, parameters)
        connection.commit()
        connection.close()

    def _append_source_metadata(self, thread_id, operation):
        item = next(
            (value for value in self.plan.repairs if value.spec.source_thread_id == thread_id),
            None,
        )
        if item is not None:
            connection = sqlite3.connect(self.paths.state_database)
            current_path = Path(
                connection.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()[0]
            )
            connection.close()
            with current_path.open("ab") as handle:
                handle.write(_line("event_msg", {"type": "test_metadata", "operation": operation}, 99))

    def _move_source_rollout(self, thread_id, *, archived):
        item = next(
            (value for value in self.plan.repairs if value.spec.source_thread_id == thread_id),
            None,
        )
        if item is None:
            return
        connection = sqlite3.connect(self.paths.state_database)
        current_path = Path(
            connection.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()[0]
        )
        if archived:
            relative = item.rollout_path.relative_to(self.paths.codex_home / "sessions")
            target = self.paths.codex_home / "archived_sessions" / relative
        else:
            target = item.rollout_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if current_path != target:
            current_path.replace(target)
        connection.execute("update threads set rollout_path=? where id=?", (str(target), thread_id))
        connection.commit()
        connection.close()

    def set_thread_name(self, thread_id, name):
        self._update("update threads set name=? where id=?", (name, thread_id))
        self._append_source_metadata(thread_id, "name")
        return {}

    def archive_thread(self, thread_id):
        self._move_source_rollout(thread_id, archived=True)
        self._update("update threads set archived=1 where id=?", (thread_id,))
        self._append_source_metadata(thread_id, "archive")
        return {}

    def unarchive_thread(self, thread_id):
        self.unarchive_calls.append(thread_id)
        self._move_source_rollout(thread_id, archived=False)
        self._update("update threads set archived=0 where id=?", (thread_id,))
        self._append_source_metadata(thread_id, "unarchive")
        return {"thread": {"id": thread_id}}

    def move_thread_section(self, thread_id, section_id, *, before_thread_id=None):
        self.move_calls.append((thread_id, section_id, before_thread_id))
        if self.fail_move and thread_id in self.new_ids.values() and section_id is not None:
            self.fail_move = False
            raise RuntimeError("simulated section move failure")
        connection = sqlite3.connect(self.paths.state_database)
        if section_id is None:
            connection.execute(
                "update threads set thread_section_id=null,section_position=null,section_entered_at_ms=null where id=?",
                (thread_id,),
            )
        else:
            if before_thread_id is not None:
                before = connection.execute(
                    "select section_position from threads where id=?",
                    (before_thread_id,),
                ).fetchone()
                position = before[0] - 1 if before is not None else 99_000_000
            else:
                maximum = connection.execute(
                    "select max(section_position) from threads where thread_section_id=?",
                    (section_id,),
                ).fetchone()[0]
                position = (maximum or 0) + 1
            connection.execute(
                "update threads set thread_section_id=?,section_position=?,section_entered_at_ms=9999 where id=?",
                (section_id, position, thread_id),
            )
        connection.commit()
        connection.close()
        return {"thread": {"id": thread_id}}

    def close(self):
        self.closed = True


class RepairStalledHistoryTests(unittest.TestCase):
    def test_default_writer_probe_ignores_zero_byte_marker_but_blocks_real_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            lock_dir = paths.codex_home / "thread-writer-locks"
            lock_dir.mkdir(parents=True)
            marker = lock_dir / "task.lock"
            marker.write_bytes(b"")

            self.assertEqual(repair._default_writer_probe(paths), [])

            marker.write_bytes(b"active")
            self.assertEqual(repair._default_writer_probe(paths), [marker])

    def test_build_plan_and_default_run_are_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home, specs, rollouts = create_fixture(directory)
            state_before = (home / "state_5.sqlite").read_bytes()
            history_before = (home / "thread_history_1.sqlite").read_bytes()

            result = repair.run_repair(home, specs)

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["mode"], "dry-run")
            self.assertEqual(result["writes"], [])
            self.assertEqual((home / "state_5.sqlite").read_bytes(), state_before)
            self.assertEqual((home / "thread_history_1.sqlite").read_bytes(), history_before)
            self.assertFalse((home / "switchboard" / "backups").exists())
            self.assertEqual(next((home / "sessions").rglob("*.jsonl")).read_bytes(), rollouts[SOURCE_IDS[0]])

    def test_cas_advances_two_offsets_and_preserves_ordinals(self):
        with tempfile.TemporaryDirectory() as directory:
            home, specs, rollouts = create_fixture(directory, count=2)
            plan = repair.build_plan(home, specs)
            paths = switchboard.Paths(home)

            repair.cas_advance_projection(paths, plan)

            connection = sqlite3.connect(paths.thread_history_database)
            rows = connection.execute(
                "select thread_id,next_rollout_byte_offset,next_rollout_ordinal "
                "from thread_history_projection_state order by thread_id"
            ).fetchall()
            connection.close()
            expected = sorted(
                (item.spec.segment_id, item.new_offset, item.spec.expected_ordinal)
                for item in plan.repairs
            )
            self.assertEqual(rows, expected)
            for item in plan.repairs:
                self.assertEqual(item.rollout_path.read_bytes(), rollouts[item.spec.source_thread_id])

    def test_duplicate_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home, specs, _rollouts = create_fixture(directory, duplicate_payload_type="different_event")
            with self.assertRaises(repair.RepairError):
                repair.build_plan(home, specs)

    def test_execute_rejects_process_blocker_before_backup_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            home, specs, _rollouts = create_fixture(directory)
            history_before = (home / "thread_history_1.sqlite").read_bytes()
            backup_calls = []

            with self.assertRaises(repair.RepairError):
                repair.run_repair(
                    home,
                    specs,
                    execute=True,
                    process_probe=lambda _paths: ["busy"],
                    writer_probe=lambda _paths: [],
                    backup_fn=lambda *args: backup_calls.append(args),
                )

            self.assertEqual(backup_calls, [])
            self.assertEqual((home / "thread_history_1.sqlite").read_bytes(), history_before)

    def test_wait_for_exit_replans_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            home, specs, _rollouts = create_fixture(directory)
            paths = switchboard.Paths(home)
            backup_calls = []
            original_wait = switchboard.wait_for_appserver_exit

            def fake_wait(wait_paths, **_kwargs):
                self.assertEqual(wait_paths.codex_home, home)
                self.assertFalse((home / "switchboard" / "backups").exists())
                connection = sqlite3.connect(paths.thread_history_database)
                connection.execute(
                    "update thread_history_projection_state set next_rollout_byte_offset=next_rollout_byte_offset+1"
                )
                connection.commit()
                connection.close()
                return {"status": "ready", "stable_empty_checks": 2}

            switchboard.wait_for_appserver_exit = fake_wait
            try:
                with self.assertRaises(repair.RepairError):
                    repair.run_repair(
                        home,
                        specs,
                        execute=True,
                        wait_for_exit=True,
                        process_probe=lambda _paths: [],
                        writer_probe=lambda _paths: [],
                        backup_fn=lambda *args: backup_calls.append(args),
                    )
            finally:
                switchboard.wait_for_appserver_exit = original_wait
            self.assertEqual(backup_calls, [])

    def test_fake_appserver_repairs_forks_and_publishes_clean_task(self):
        with tempfile.TemporaryDirectory() as directory:
            home, specs, rollouts = create_fixture(directory)
            plan = repair.build_plan(home, specs)
            fake = FakeAppServer(switchboard.Paths(home), plan)
            self.assertNotIn("thread/resume", repair.RepairAppServerClient._ALLOWED_REQUESTS)
            self.assertIn("thread/section/move", repair.RepairAppServerClient._ALLOWED_REQUESTS)
            self.assertEqual(plan.repairs[0].section_snapshot["name"], "Pinned")
            self.assertEqual(plan.repairs[0].original_next_thread_id, "sibling-0")

            result = repair.run_repair(
                home,
                specs,
                execute=True,
                process_probe=lambda _paths: [],
                writer_probe=lambda _paths: [],
                client_factory=lambda _paths, _executable: fake,
            )

            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["cursor_cas_complete"])
            self.assertEqual(result["provider_requests"], 0)
            self.assertTrue(result["source_rollout_prefix_preserved"])
            self.assertTrue(result["source_rollouts_modified"])
            self.assertGreater(result["repairs"][0]["appended_bytes"], 0)
            self.assertTrue(fake.started)
            self.assertTrue(fake.closed)
            self.assertEqual(fake.capabilities, {"experimentalApi": True})
            self.assertEqual(fake.fork_calls[0]["last_turn_id"], specs[0].correct_last_turn_id)
            self.assertEqual(fake.fork_calls[0]["model"], specs[0].expected_model)
            new_id = result["repairs"][0]["new_thread_id"]
            self.assertEqual(
                fake.move_calls[0],
                (new_id, PINNED_SECTION_ID, specs[0].source_thread_id),
            )
            self.assertNotIn(new_id, fake.unarchive_calls)
            self.assertEqual(len(result["backups"]), 2)
            for name in result["backups"]:
                self.assertTrue((home / "switchboard" / "backups" / name).is_file())
            connection = sqlite3.connect(home / "state_5.sqlite")
            source = connection.execute(
                "select name,archived,thread_section_id,section_position from threads where id=?",
                (specs[0].source_thread_id,),
            ).fetchone()
            new = connection.execute(
                "select name,archived,thread_section_id,section_position,model_provider,model "
                "from threads where id=?",
                (new_id,),
            ).fetchone()
            connection.close()
            self.assertEqual(source, (specs[0].source_archive_name, 1, PINNED_SECTION_ID, 10_000_000))
            self.assertEqual(new, (specs[0].new_name, 0, PINNED_SECTION_ID, 9_999_999, "openai", "gpt-5.6-sol"))
            connection = sqlite3.connect(home / "state_5.sqlite")
            current_path = Path(
                connection.execute(
                    "select rollout_path from threads where id=?",
                    (specs[0].source_thread_id,),
                ).fetchone()[0]
            )
            connection.close()
            self.assertIn("archived_sessions", current_path.parts)
            current_rollout = current_path.read_bytes()
            self.assertEqual(
                current_rollout[: plan.repairs[0].rollout_size],
                rollouts[specs[0].source_thread_id],
            )

    def test_failure_restores_source_metadata_and_archives_new_task(self):
        with tempfile.TemporaryDirectory() as directory:
            home, specs, _rollouts = create_fixture(directory)
            plan = repair.build_plan(home, specs)
            fake = FakeAppServer(switchboard.Paths(home), plan, fail_move=True)

            result = repair.run_repair(
                home,
                specs,
                execute=True,
                process_probe=lambda _paths: [],
                writer_probe=lambda _paths: [],
                client_factory=lambda _paths, _executable: fake,
            )

            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["rollback"]["complete"])
            self.assertTrue(result["source_rollout_prefix_preserved"])
            self.assertGreater(result["repairs"][0]["appended_bytes"], 0)
            connection = sqlite3.connect(home / "state_5.sqlite")
            source = connection.execute(
                "select name,archived,thread_section_id,section_position from threads where id=?",
                (specs[0].source_thread_id,),
            ).fetchone()
            new = connection.execute(
                "select archived,thread_section_id from threads where id=?",
                (fake.new_ids[specs[0].source_thread_id],),
            ).fetchone()
            connection.close()
            self.assertEqual(source, ("task-0", 0, PINNED_SECTION_ID, 10_999_999))
            self.assertEqual(new, (1, None))

    def test_section_move_client_emits_official_contract(self):
        client = object.__new__(repair.RepairAppServerClient)
        calls = []
        client._request = lambda method, params: calls.append((method, params)) or {}

        client.move_thread_section("thread-a", PINNED_SECTION_ID, before_thread_id="thread-b")
        client.move_thread_section("thread-a", None)

        self.assertEqual(
            calls,
            [
                (
                    "thread/section/move",
                    {
                        "threadId": "thread-a",
                        "sectionId": PINNED_SECTION_ID,
                        "beforeThreadId": "thread-b",
                    },
                ),
                ("thread/section/move", {"threadId": "thread-a", "sectionId": None}),
            ],
        )

    def test_rollout_status_rejects_invalid_appended_json(self):
        with tempfile.TemporaryDirectory() as directory:
            home, specs, _rollouts = create_fixture(directory)
            plan = repair.build_plan(home, specs)
            with plan.repairs[0].rollout_path.open("ab") as handle:
                handle.write(b"not-json\n")

            status = repair._source_rollout_status(switchboard.Paths(home), plan)

            self.assertFalse(status["prefix_preserved"])
            self.assertTrue(status["modified"])

    def test_cli_wait_and_open_require_explicit_execute(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = repair.main(["--home", "X:\\missing", "--spec", "missing.json", "--wait-for-exit"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["type"], "explicit_execute_required")


if __name__ == "__main__":
    unittest.main()
