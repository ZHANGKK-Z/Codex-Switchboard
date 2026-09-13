import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import switchboard as sb
from appserver_client import AppServerTimeoutError
from history_chain import HistoryChainError, read_projected_history


def record(ordinal, kind, payload):
    return (json.dumps({"ordinal": ordinal, "type": kind, "payload": payload}) + "\n").encode()


@contextlib.contextmanager
def fixture_db(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class ConversionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = sb.Paths(Path(self.temp.name))
        with contextlib.redirect_stdout(io.StringIO()):
            sb.initialize(self.paths)
        sb.atomic_write_json(self.paths.active, {"profile_id": "official", "revision": 0})
        self.sessions = self.paths.codex_home / "sessions"
        self.sessions.mkdir()
        self.source = self.sessions / "source.jsonl"
        self.source.write_bytes(record(0, "session_meta", {"id": "source"}) +
                                record(1, "event_msg", {"type": "task_started", "turn_id": "t1"}) +
                                record(2, "event_msg", {"type": "task_complete", "turn_id": "t1"}))
        with fixture_db(self.paths.state_database) as con:
            con.executescript("""
                CREATE TABLE threads(id TEXT PRIMARY KEY,model_provider TEXT,cwd TEXT,model TEXT,
                  name TEXT,title TEXT,archived INTEGER,is_pinned INTEGER,rollout_path TEXT,
                  created_at_ms INTEGER,updated_at_ms INTEGER,created_at INTEGER,updated_at INTEGER,
                  first_user_message TEXT);
            """)
        with fixture_db(self.paths.thread_history_database) as con:
            con.executescript("""
                CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,
                  next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER);
                CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,
                  rollout_end_ordinal INTEGER,status TEXT);
                CREATE TABLE thread_items(thread_id TEXT,item_id TEXT,rollout_ordinal INTEGER);
            """)
            con.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", ("source", self.source.stat().st_size, 3))
            con.execute("INSERT INTO thread_turns VALUES('source','t1',1,2,'completed')")
            con.execute("INSERT INTO thread_items VALUES('source','i1',1)")
        self.add_binding("source", "custom", self.source)

    def add_binding(self, ident, provider, path):
        with fixture_db(self.paths.state_database) as con:
            con.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ident, provider, str(self.paths.codex_home), "gpt-test", "demo", "demo",
                         0, 0, str(path), 1000, 1000, 1, 1, "hello"))

    def create_child(self, ident="child", provider="openai"):
        child = self.sessions / (ident + ".jsonl")
        child.write_bytes(record(3, "session_meta", {"id": ident, "forked_from_id": "source",
            "history_base": {"thread_id": "source", "end_byte_offset": self.source.stat().st_size,
                             "end_ordinal_exclusive": 3}}) +
            record(4, "event_msg", {"type": "thread_settings_applied"}))
        self.add_binding(ident, provider, child)
        with fixture_db(self.paths.thread_history_database) as con:
            con.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (ident, child.stat().st_size, 5))
        return child

    def factory(self, *, create=True, wrong_provider=False):
        case = self
        self.fork_calls = 0
        self.clients = []
        class Client:
            def __init__(self):
                self.closed = False
                case.clients.append(self)
            def snapshot_thread(self, ident):
                self.assert_open()
                binding = sb.thread_provider_binding(case.paths, ident)
                return {"id": ident, "name": binding["name"], "cwd": binding["cwd"],
                        "modelProvider": binding["provider_alias"]}
            def assert_open(self):
                if self.closed:
                    raise AssertionError("timed-out connection was reused")
            def read_account(self):
                return {"account": {"type": "chatgpt"}}
            def fork_thread(self, *args, **kwargs):
                case.fork_calls += 1
                if create:
                    case.create_child(provider="wrong" if wrong_provider else "openai")
                raise AppServerTimeoutError("simulated response lost", method="thread/fork", request_id=4,
                                            created_thread_id="child" if wrong_provider else None)
            def set_thread_name(self, *args):
                self.assert_open()
            def set_thread_pinned(self, ident, pinned):
                self.assert_open()
                with fixture_db(case.paths.state_database) as con:
                    con.execute("UPDATE threads SET is_pinned=? WHERE id=?", (int(pinned), ident))
                return {}
            def unarchive_thread(self, ident):
                self.assert_open()
                return {"thread": {"id": ident}}
            def close(self):
                self.closed = True
        return lambda *args: Client()

    def test_timeout_after_commit_reconciles_existing_child_and_repeat_does_not_fork(self):
        source_bytes = self.source.read_bytes()
        factory = self.factory()
        messages = []
        with mock.patch.object(sb, "_appserver_client", side_effect=factory):
            result = sb.fork_thread_provider(self.paths, "source", "official", blocker_probe=lambda p: [],
                                             progress_callback=messages.append)
            repeated = sb.fork_thread_provider(self.paths, "source", "official", blocker_probe=lambda p: [])
        self.assertEqual(self.fork_calls, 1)
        self.assertEqual(result["thread"]["id"], "child")
        self.assertTrue(result["completion"]["core_complete"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(result["restart_verification"]["turn_count"], 1)
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual(sb.thread_provider_binding(self.paths, "source")["provider_alias"], "custom")
        self.assertFalse(sb.thread_provider_binding(self.paths, "source")["archived"])
        self.assertEqual(sb._conversion_receipts(self.paths)["pending"], {})
        self.assertTrue(all(client.closed for client in self.clients))
        self.assertTrue(any("不会重试" in msg for msg in messages))

    def test_unknown_submission_survives_restart_and_prevents_blind_repeat(self):
        with mock.patch.object(sb, "_appserver_client", side_effect=self.factory(create=False)):
            for attempt in range(2):
                with self.assertRaises(sb.ConversionOutcomeUnknownError):
                    sb.fork_thread_provider(self.paths, "source", "official", blocker_probe=lambda p: [])
        self.assertEqual(self.fork_calls, 1)
        self.assertIn("source", sb._conversion_receipts(self.paths)["pending"])

    def test_notification_candidate_with_wrong_provider_never_counts_as_success(self):
        with mock.patch.object(sb, "_appserver_client", side_effect=self.factory(wrong_provider=True)):
            with self.assertRaises(sb.PartialThreadConversionError):
                sb.fork_thread_provider(self.paths, "source", "official", blocker_probe=lambda p: [])
        self.assertEqual(self.fork_calls, 1)
        self.assertIn("source", sb._conversion_receipts(self.paths)["pending"])
        self.assertFalse(sb.thread_provider_binding(self.paths, "child")["is_pinned"])

    def test_inherited_zero_local_turns_and_ancestor_growth(self):
        self.create_child()
        with self.source.open("ab") as handle:
            handle.write(record(3, "event_msg", {"type": "task_started", "turn_id": "later"}))
        with fixture_db(self.paths.thread_history_database) as con:
            con.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=4 WHERE thread_id='source'", (self.source.stat().st_size,))
            con.execute("INSERT INTO thread_turns VALUES('source','later',3,NULL,'inProgress')")
        status = sb.ensure_thread_history_readable(self.paths, "child")
        self.assertEqual(status["turn_ids"], ["t1"])
        self.assertEqual(status["history_segment_count"], 2)

    def test_missing_projection_fails_before_client_or_config_change(self):
        self.paths.thread_history_database.unlink()
        before = self.paths.config.read_bytes() if self.paths.config.exists() else None
        with mock.patch.object(sb, "_appserver_client") as client:
            with self.assertRaises(sb.UnreadableThreadHistoryError):
                sb.fork_thread_provider(self.paths, "source", "official", blocker_probe=lambda p: [])
        client.assert_not_called()
        self.assertEqual(self.paths.config.read_bytes() if self.paths.config.exists() else None, before)

    def test_cutoff_inside_record_and_missing_ancestor_rejected(self):
        child = self.create_child()
        lines = child.read_bytes().splitlines(keepends=True)
        meta = json.loads(lines[0])
        meta["payload"]["history_base"]["end_byte_offset"] -= 1
        child.write_bytes((json.dumps(meta) + "\n").encode() + lines[1])
        with fixture_db(self.paths.thread_history_database) as con:
            con.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id='child'", (child.stat().st_size,))
        with self.assertRaisesRegex(HistoryChainError, "inside_record"):
            read_projected_history(self.paths.codex_home, self.paths.state_database,
                                   self.paths.thread_history_database, "child")
        self.source.unlink()
        with self.assertRaisesRegex(HistoryChainError, "missing_or_ambiguous"):
            read_projected_history(self.paths.codex_home, self.paths.state_database,
                                   self.paths.thread_history_database, "child")

    def test_duplicate_in_inherited_file_blocks_even_when_turn_ids_match(self):
        payload = self.source.read_bytes().splitlines(keepends=True)
        self.source.write_bytes(payload[0] + payload[1] + record(1, "event_msg", {
            "type": "thread_settings_applied"}) + payload[2])
        with fixture_db(self.paths.thread_history_database) as con:
            con.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id='source'", (self.source.stat().st_size,))
        self.create_child()
        self.assertEqual(sb._projected_turn_ids(self.paths, "child"), ["t1"])
        status = sb.thread_history_projection_status(self.paths, "child", include_candidates=False)
        self.assertFalse(status["safe_to_fork"])
        self.assertEqual(status["reason"], "rollout_duplicate_ordinal")
        self.assertEqual(status["error_segment"], "source")

    def test_adjacent_metadata_duplicate_is_accepted_without_relaxing_content_checks(self):
        self.source.write_bytes(
            record(0, "session_meta", {"id": "source"})
            + record(1, "event_msg", {"type": "token_count"})
            + record(1, "event_msg", {"type": "thread_settings_applied"})
            + record(2, "event_msg", {"type": "task_started", "turn_id": "t1"})
            + record(3, "event_msg", {"type": "task_complete", "turn_id": "t1"})
        )
        with fixture_db(self.paths.thread_history_database) as con:
            con.execute(
                "UPDATE thread_history_projection_state "
                "SET next_rollout_byte_offset=?, next_rollout_ordinal=4 "
                "WHERE thread_id='source'",
                (self.source.stat().st_size,),
            )

        chain = read_projected_history(
            self.paths.codex_home,
            self.paths.state_database,
            self.paths.thread_history_database,
            "source",
            verify_rollout=True,
        )
        self.assertEqual(chain["turn_ids"], ["t1"])
        status = sb.thread_history_projection_status(
            self.paths, "source", include_candidates=False
        )
        self.assertEqual(status["health"], "healthy")
        self.assertTrue(status["safe_to_fork"])

    def test_repeated_metadata_type_is_still_rejected(self):
        self.source.write_bytes(
            record(0, "session_meta", {"id": "source"})
            + record(1, "event_msg", {"type": "token_count"})
            + record(1, "event_msg", {"type": "token_count"})
            + record(2, "event_msg", {"type": "task_complete", "turn_id": "t1"})
        )
        with fixture_db(self.paths.thread_history_database) as con:
            con.execute(
                "UPDATE thread_history_projection_state "
                "SET next_rollout_byte_offset=?, next_rollout_ordinal=3 "
                "WHERE thread_id='source'",
                (self.source.stat().st_size,),
            )

        status = sb.thread_history_projection_status(
            self.paths, "source", include_candidates=False
        )
        self.assertEqual(status["reason"], "rollout_duplicate_ordinal")
        self.assertFalse(status["safe_to_fork"])

    def test_cycle_and_cut_through_turn_fail_closed(self):
        child = self.create_child()
        with fixture_db(self.paths.thread_history_database) as con:
            con.execute("UPDATE thread_turns SET rollout_end_ordinal=3 WHERE turn_id='t1'")
        with self.assertRaisesRegex(HistoryChainError, "cuts_turn"):
            sb._projected_turn_ids(self.paths, "child")
        rows = child.read_bytes().splitlines(keepends=True)
        meta = json.loads(rows[0])
        meta["payload"]["history_base"]["thread_id"] = "child"
        child.write_bytes((json.dumps(meta) + "\n").encode() + rows[1])
        with fixture_db(self.paths.thread_history_database) as con:
            con.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id='child'", (child.stat().st_size,))
        with self.assertRaisesRegex(HistoryChainError, "cycle"):
            sb._projected_turn_ids(self.paths, "child")

    def test_resumed_segment_owns_new_turns_while_root_keeps_earlier_turns(self):
        segment = "01a00000-0000-7000-8000-000000000002"
        resumed = self.sessions / f"rollout-source_{segment}.jsonl"
        resumed.write_bytes(record(3, "session_meta", {"id": "source", "history_base": {
            "thread_id": "source", "end_byte_offset": self.source.stat().st_size,
            "end_ordinal_exclusive": 3}}) +
            record(4, "event_msg", {"type": "task_started", "turn_id": "t2"}) +
            record(5, "event_msg", {"type": "task_complete", "turn_id": "t2"}))
        with fixture_db(self.paths.state_database) as con:
            con.execute("UPDATE threads SET rollout_path=? WHERE id='source'", (str(resumed),))
        with fixture_db(self.paths.thread_history_database) as con:
            con.execute("INSERT INTO thread_history_projection_state VALUES(?,?,6)", (segment, resumed.stat().st_size))
            con.execute("INSERT INTO thread_turns VALUES(?,'t2',4,5,'completed')", (segment,))
        status = sb.ensure_thread_history_readable(self.paths, "source")
        self.assertEqual(status["turn_ids"], ["t1", "t2"])
        self.assertEqual(status["projected_turns"], 2)

    def test_unknown_relay_submission_does_not_remove_published_provider_version(self):
        document = sb.load_profiles(self.paths)
        profile = next(p for p in document["profiles"] if p["id"] == "relay-b")
        profile.update(enabled=True, base_url="https://relay.invalid/v1", model="gpt-5.6-sol",
                       models=["gpt-5.6-sol"])
        sb.atomic_write_json(self.paths.profiles, document)
        sb.key_path(self.paths, profile["key_ref"]).write_bytes(b"fixture-only")
        before = self.paths.provider_versions.read_bytes()
        with mock.patch.object(sb, "_appserver_client", side_effect=self.factory(create=False)), \
             mock.patch.object(sb, "_prepare_model_catalog_for_runtime", return_value=None):
            with self.assertRaises(sb.ConversionOutcomeUnknownError):
                sb.fork_thread_provider(self.paths, "source", "relay-b", blocker_probe=lambda p: [])
        after = self.paths.provider_versions.read_bytes()
        self.assertNotEqual(before, after)
        alias = sb._conversion_receipts(self.paths)["pending"]["source"]["provider"]
        self.assertIn(alias.encode(), after)
        self.assertEqual(sb.load_active(self.paths)["profile_id"], "official")
