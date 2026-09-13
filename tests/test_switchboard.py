import json
import os
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock
from functools import wraps
from pathlib import Path
import shutil
import tomllib

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import switchboard
from appserver_client import AppServerPostCommitValidationError, AppServerProtocolError


def binding_only_history(test):
    """Older routing fixtures intentionally do not model the history store."""
    @wraps(test)
    def run(*args, **kwargs):
        with mock.patch.object(switchboard, "ensure_thread_history_readable", return_value=None), \
             mock.patch.object(switchboard, "thread_history_projection_status", return_value=None), \
             mock.patch.object(switchboard, "_projected_turn_ids", return_value=None):
            return test(*args, **kwargs)
    return run


def create_projection_fixture(paths, rows):
    paths.codex_home.mkdir(parents=True, exist_ok=True)
    state = sqlite3.connect(paths.state_database)
    history = sqlite3.connect(paths.thread_history_database)
    try:
        state.execute(
            """
            create table threads(
                id text primary key,
                rollout_path text not null,
                cwd text not null,
                title text,
                first_user_message text not null,
                updated_at integer not null,
                updated_at_ms integer,
                archived integer not null,
                model_provider text not null,
                model text,
                name text
            )
            """
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
        history.execute("create table thread_turns(thread_id text, turn_id text)")
        history.execute("create table thread_items(thread_id text, item_id text)")
        sessions = paths.codex_home / "sessions"
        sessions.mkdir()
        for index, row in enumerate(rows):
            rollout = sessions / f"{row['id']}.jsonl"
            payload = row.get(
                "rollout_bytes",
                (
                    json.dumps(
                        {
                            "ordinal": 0,
                            "type": "session_meta",
                            "payload": {"id": row["id"]},
                        }
                    )
                    + "\n"
                ).encode(),
            )
            rollout.write_bytes(payload)
            state.execute(
                "insert into threads values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    str(rollout),
                    row.get("cwd", str(paths.codex_home / "workspace")),
                    row.get("title"),
                    row.get("first_user_message", "hello"),
                    row.get("updated_at", index + 1),
                    row.get("updated_at_ms", (index + 1) * 1000),
                    int(row.get("archived", 0)),
                    row.get("model_provider", "openai"),
                    row.get("model", "gpt-test"),
                    row.get("name", row["id"]),
                ),
            )
            projection_offset = row.get("projection_offset", len(payload))
            if projection_offset is not None:
                history.execute(
                    "insert into thread_history_projection_state values(?,?,?)",
                    (row["id"], projection_offset, row.get("projection_ordinal", 1)),
                )
            for turn_index in range(row.get("projected_turns", 0)):
                history.execute(
                    "insert into thread_turns values(?,?)",
                    (row["id"], f"turn-{turn_index}"),
                )
            for item_index in range(row.get("projected_items", 0)):
                history.execute(
                    "insert into thread_items values(?,?)",
                    (row["id"], f"item-{item_index}"),
                )
        state.commit()
        history.commit()
    finally:
        history.close()
        state.close()


def create_binding_fixture(paths, rows):
    paths.codex_home.mkdir(parents=True, exist_ok=True)
    sessions = paths.codex_home / "sessions"
    sessions.mkdir(exist_ok=True)
    connection = sqlite3.connect(paths.state_database)
    try:
        connection.execute(
            """
            create table threads(
                id text primary key,
                model_provider text not null,
                cwd text not null,
                model text,
                name text,
                archived integer not null default 0,
                rollout_path text,
                created_at_ms integer,
                updated_at_ms integer,
                is_pinned integer not null default 0
            )
            """
        )
        for index, row in enumerate(rows):
            rollout = sessions / f"{row['id']}.jsonl"
            payload = {
                "type": "session_meta",
                "payload": {
                    "id": row["id"],
                    "session_id": row["id"],
                    "model_provider": row.get("model_provider", "custom"),
                },
            }
            if row.get("parent_thread_id"):
                payload["payload"]["forked_from_id"] = row["parent_thread_id"]
            if row.get("source"):
                payload["payload"]["source"] = row["source"]
            rollout.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            connection.execute(
                "insert into threads(id, model_provider, cwd, model, name, archived, rollout_path, "
                "created_at_ms, updated_at_ms, is_pinned) values(?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row.get("model_provider", "custom"),
                    row.get("cwd", str(paths.codex_home / "workspace")),
                    row.get("model", "gpt-5.6-sol"),
                    row.get("name", "fixture"),
                    int(row.get("archived", 0)),
                    str(rollout),
                    int(row.get("created_at_ms", (index + 1) * 1000)),
                    int(row.get("updated_at_ms", (index + 1) * 1000)),
                    int(row.get("is_pinned", 0)),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def write_history_base(paths, child_id, source_id, *, end_byte_offset=None):
    source_rollout = paths.codex_home / "sessions" / f"{source_id}.jsonl"
    child_rollout = paths.codex_home / "sessions" / f"{child_id}.jsonl"
    offset = source_rollout.stat().st_size if end_byte_offset is None else end_byte_offset
    payload = {
        "type": "session_meta",
        "payload": {
            "id": child_id,
            "session_id": child_id,
            "forked_from_id": source_id,
            "history_base": {
                "thread_id": source_id,
                "end_byte_offset": offset,
                "end_ordinal_exclusive": 1,
            },
        },
    }
    child_rollout.write_text(json.dumps(payload) + "\n", encoding="utf-8")


class SwitchboardTests(unittest.TestCase):
    def test_profile_switch_is_atomic_and_does_not_touch_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            # The production path stores this file as DPAPI ciphertext.  The
            # switch operation only needs its presence; no secret is needed in
            # this unit test.
            switchboard.key_path(paths, "maylily-main").write_bytes(b"test")
            before = paths.active.read_text(encoding="utf-8")
            result = switchboard.set_active_profile(paths, "maylily")
            after = paths.active.read_text(encoding="utf-8")
            self.assertEqual(result["profile"]["id"], "maylily")
            self.assertNotEqual(before, after)
            self.assertEqual(json.loads(after)["profile_id"], "maylily")

    def test_changed_relay_configuration_gets_new_immutable_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"test")
            original = switchboard.provider_version_by_alias(
                switchboard.load_provider_versions(paths),
                "custom",
            )
            switchboard.configure_relay_profile(
                paths,
                "maylily",
                base_url="https://new-maylily.example/v1",
                model="gpt-5.6-sol",
            )

            switched = switchboard.set_active_profile(paths, "maylily")

            alias = switched["active"]["provider_alias"]
            self.assertNotEqual(alias, "custom")
            versions = switchboard.load_provider_versions(paths)["versions"]
            self.assertEqual(len(versions), 2)
            self.assertEqual(original["base_url"], "https://maylily.xyz")
            parsed = tomllib.loads(paths.config.read_text(encoding="utf-8"))
            self.assertIn("custom", parsed["model_providers"])
            self.assertIn(alias, parsed["model_providers"])
            self.assertEqual(parsed["model_provider"], alias)

    def test_relay_models_create_distinct_immutable_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            original_custom = dict(
                switchboard.provider_version_by_alias(
                    switchboard.load_provider_versions(paths),
                    "custom",
                )
            )
            switchboard.configure_relay_profile(
                paths,
                "maylily",
                base_url="https://maylily.xyz",
                model="gpt-5.6-sol",
                models=["gpt-5.6-sol", "gpt-5.4"],
            )

            selected = switchboard.ensure_provider_version(
                paths,
                "maylily",
                model="gpt-5.4",
            )

            self.assertNotEqual(selected["provider_alias"], "custom")
            self.assertEqual(selected["model"], "gpt-5.4")
            self.assertEqual(
                switchboard.provider_version_by_alias(
                    switchboard.load_provider_versions(paths),
                    "custom",
                ),
                original_custom,
            )
            with self.assertRaisesRegex(ValueError, "model is not configured"):
                switchboard.ensure_provider_version(
                    paths,
                    "maylily",
                    model="gpt-unconfigured",
                )

    def test_provider_version_registry_rejects_mutated_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            document = json.loads(paths.provider_versions.read_text(encoding="utf-8"))
            document["versions"][0]["base_url"] = "https://tampered.example"
            switchboard.atomic_write_json(paths.provider_versions, document)

            with self.assertRaisesRegex(RuntimeError, "integrity check failed"):
                switchboard.load_provider_versions(paths)

    def test_official_default_does_not_change_legacy_custom_route(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.set_active_profile(paths, "official", allow_official=True)

            version, upstream_path = switchboard.resolve_router_target(paths, "/v1/responses")

            self.assertEqual(version["provider_alias"], "custom")
            self.assertEqual(version["profile_id"], "maylily")
            self.assertEqual(version["base_url"], "https://maylily.xyz")
            self.assertEqual(upstream_path, "/v1/responses")

    def test_second_relay_uses_distinct_route_and_preserves_maylily(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.configure_relay_profile(
                paths,
                "relay-b",
                base_url="https://relay-b.example/v1",
                model="gpt-5.6-sol",
            )
            switchboard.key_path(paths, "relay-b-main").write_bytes(b"test")

            switched = switchboard.set_active_profile(paths, "relay-b")
            alias = switched["active"]["provider_alias"]
            relay_b, relay_path = switchboard.resolve_router_target(
                paths,
                f"/profiles/{alias}/v1/responses?stream=true",
            )
            maylily, maylily_path = switchboard.resolve_router_target(paths, "/v1/responses")

            self.assertNotEqual(alias, "custom")
            self.assertEqual(relay_b["base_url"], "https://relay-b.example/v1")
            self.assertEqual(relay_path, "/v1/responses?stream=true")
            self.assertEqual(maylily["base_url"], "https://maylily.xyz")
            self.assertEqual(maylily_path, "/v1/responses")

    def test_router_forwards_by_alias_to_local_fixture_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            captured = {}

            class UpstreamHandler(switchboard.BaseHTTPRequestHandler):
                def log_message(self, _format, *_args):
                    return

                def do_POST(self):  # noqa: N802
                    length = int(self.headers.get("Content-Length", "0"))
                    captured.update(
                        {
                            "path": self.path,
                            "authorization": self.headers.get("Authorization"),
                            "body": self.rfile.read(length),
                        }
                    )
                    payload = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            upstream = switchboard.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
            upstream_thread = switchboard.threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.configure_relay_profile(
                paths,
                "relay-b",
                base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                model="gpt-5.6-sol",
            )
            switchboard.key_path(paths, "relay-b-main").write_bytes(b"fixture")
            alias = switchboard.set_active_profile(paths, "relay-b")["active"]["provider_alias"]
            router = switchboard.ThreadingHTTPServer(("127.0.0.1", 0), switchboard.RouterHandler)
            router.paths = paths
            router_thread = switchboard.threading.Thread(target=router.serve_forever, daemon=True)
            router_thread.start()
            original_load_key = switchboard.load_key
            try:
                switchboard.load_key = lambda *_args, **_kwargs: "fixture-secret"
                connection = switchboard.http.client.HTTPConnection(
                    "127.0.0.1",
                    router.server_port,
                    timeout=2,
                )
                connection.request(
                    "POST",
                    f"/profiles/{alias}/v1/responses?stream=false",
                    body=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                body = response.read()
                connection.close()
            finally:
                switchboard.load_key = original_load_key
                router.shutdown()
                router.server_close()
                router_thread.join(timeout=2)
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=2)

            self.assertEqual(response.status, 200)
            self.assertEqual(body, b'{"ok":true}')
            self.assertEqual(captured["path"], "/v1/responses?stream=false")
            self.assertEqual(captured["authorization"], "Bearer fixture-secret")
            self.assertEqual(captured["body"], b"{}")

    def test_thread_binding_uses_codex_persisted_provider_as_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            create_binding_fixture(
                paths,
                [
                    {"id": "relay-task", "model_provider": "custom"},
                    {"id": "official-task", "model_provider": "openai"},
                ],
            )

            relay = switchboard.thread_provider_binding(paths, "relay-task")
            official = switchboard.thread_provider_binding(paths, "official-task")

            self.assertEqual((relay["profile_id"], relay["provider_alias"]), ("maylily", "custom"))
            self.assertEqual((official["profile_id"], official["provider_alias"]), ("official", "openai"))

    def test_prepare_model_catalog_exports_only_complete_selected_model(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            bundled = {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6-Sol",
                        "supported_in_api": True,
                        "base_instructions": "test instructions",
                    },
                    {
                        "slug": "gpt-other",
                        "display_name": "Other",
                        "supported_in_api": True,
                        "base_instructions": "other instructions",
                    },
                ],
            }
            original_loader = switchboard.load_bundled_model_catalog
            try:
                switchboard.load_bundled_model_catalog = lambda *_args, **_kwargs: {
                    "catalog": bundled,
                    "executable": "test-codex.exe",
                    "client_version": "codex-cli test",
                }
                result = switchboard.prepare_model_catalog(paths, "maylily")
            finally:
                switchboard.load_bundled_model_catalog = original_loader

            self.assertTrue(result["ready"])
            self.assertEqual(result["source"], "bundled")
            catalog = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            self.assertEqual([item["slug"] for item in catalog["models"]], ["gpt-5.6-sol"])
            self.assertEqual(catalog["models"][0]["base_instructions"], "test instructions")
            self.assertEqual(
                switchboard.model_catalog_status(paths, "maylily")["state"],
                "ready",
            )

    def test_prepare_model_catalog_exports_all_configured_models(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.configure_relay_profile(
                paths,
                "maylily",
                base_url="https://maylily.xyz",
                model="gpt-5.6-sol",
                models=["gpt-5.6-sol", "gpt-5.4", "gpt-5.4"],
            )
            bundled = {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "supported_in_api": True,
                        "base_instructions": "sol instructions",
                    },
                    {
                        "slug": "gpt-5.4",
                        "supported_in_api": True,
                        "base_instructions": "5.4 instructions",
                    },
                ],
            }
            original_loader = switchboard.load_bundled_model_catalog
            try:
                switchboard.load_bundled_model_catalog = lambda *_args, **_kwargs: {
                    "catalog": bundled,
                    "executable": "test-codex.exe",
                    "client_version": "codex-cli test",
                }
                result = switchboard.prepare_model_catalog(paths, "maylily")
            finally:
                switchboard.load_bundled_model_catalog = original_loader

            catalog = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            self.assertEqual(
                [item["slug"] for item in catalog["models"]],
                ["gpt-5.6-sol", "gpt-5.4"],
            )
            self.assertEqual(result["model_count"], 2)
            status = switchboard.model_catalog_status(paths, "maylily")
            self.assertTrue(status["ready"])
            self.assertEqual(status["ready_models"], ["gpt-5.6-sol", "gpt-5.4"])

    def test_manual_relay_model_probe_is_single_shot_and_secret_free(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"ciphertext")
            switchboard.configure_relay_profile(
                paths,
                "maylily",
                base_url="https://maylily.xyz",
                model="gpt-5.6-sol",
                models=["gpt-5.6-sol", "gpt-5.4"],
            )
            captured = {"requests": 0, "closed": False}

            class FakeResponse:
                status = 200

                def read(self, _limit):
                    return json.dumps(
                        {"data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-extra"}]}
                    ).encode("utf-8")

            class FakeConnection:
                def request(self, method, path, *, headers):
                    captured["requests"] += 1
                    captured["method"] = method
                    captured["path"] = path
                    captured["authorization"] = headers.get("Authorization")

                def getresponse(self):
                    return FakeResponse()

                def close(self):
                    captured["closed"] = True

            result = switchboard.probe_relay_models(
                paths,
                "maylily",
                connection_factory=lambda *_args: FakeConnection(),
                key_loader=lambda *_args: "fixture-secret",
            )

            self.assertEqual(captured["requests"], 1)
            self.assertEqual((captured["method"], captured["path"]), ("GET", "/v1/models"))
            self.assertEqual(captured["authorization"], "Bearer fixture-secret")
            self.assertTrue(captured["closed"])
            self.assertFalse(result["inference_sent"])
            self.assertEqual(result["supported_configured"], ["gpt-5.6-sol"])
            self.assertEqual(result["missing_remote"], ["gpt-5.4"])
            self.assertNotIn("fixture-secret", json.dumps(result))

    def test_prepare_model_catalog_reports_missing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            original_loader = switchboard.load_bundled_model_catalog
            try:
                switchboard.load_bundled_model_catalog = lambda *_args, **_kwargs: {
                    "catalog": {"models": [{"slug": "gpt-other"}]},
                    "executable": "test-codex.exe",
                    "client_version": "codex-cli test",
                }
                with self.assertRaisesRegex(RuntimeError, "does not contain gpt-5.6-sol"):
                    switchboard.prepare_model_catalog(paths, "maylily")
            finally:
                switchboard.load_bundled_model_catalog = original_loader

    def test_model_catalog_status_rejects_cache_projection_without_base_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.atomic_write_json(
                switchboard.model_catalog_path(paths, "maylily"),
                {
                    "models": [
                        {
                            "slug": "gpt-5.6-sol",
                            "supported_in_api": True,
                            "model_messages": {"instructions_template": "not standalone"},
                        }
                    ]
                },
            )

            status = switchboard.model_catalog_status(paths, "maylily")

            self.assertFalse(status["ready"])
            self.assertEqual(status["state"], "stale")
            self.assertIn("incomplete", status["error"])

    def test_migration_plan_is_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            root = Path(directory) / "fixture-source"
            root.mkdir()
            (root / "one.txt").write_text("fixture", encoding="utf-8")
            targets = [("fixture", root, Path(directory) / "fixture-dest", True)]
            manifest = switchboard.build_migration_manifest(paths, targets)
            self.assertEqual(manifest["version"], 1)
            self.assertTrue(paths.migration_manifest.exists())
            self.assertEqual(manifest["targets"][0]["files"], 1)

    def test_migration_verifies_copy_and_creates_junction_only_after_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            source = Path(directory) / "source"
            source.mkdir()
            (source / "one.txt").write_text("fixture", encoding="utf-8")
            destination = Path(directory) / "dest"
            result = switchboard.migrate_directory(source, destination, execute=True, junction=False)
            self.assertEqual(result["status"], "migrated")
            self.assertTrue(destination.joinpath("one.txt").exists())
            self.assertFalse(source.exists())

    def test_migration_can_resume_verified_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            destination = Path(directory) / "dest"
            source.mkdir()
            destination.mkdir()
            (source / "one.txt").write_text("one", encoding="utf-8")
            result = switchboard.migrate_directory(
                source,
                destination,
                execute=True,
                junction=False,
                resume_existing=True,
            )
            self.assertEqual(result["status"], "migrated")
            self.assertTrue(destination.joinpath("one.txt").exists())
            self.assertFalse(source.exists())

    def test_migration_cleanup_removes_only_verified_retired_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            destination = Path(directory) / "dest"
            source.mkdir()
            (source / "one.txt").write_text("one", encoding="utf-8")
            result = switchboard.migrate_directory(
                source,
                destination,
                execute=True,
                junction=False,
                cleanup_retired=True,
            )
            self.assertTrue(result["source_copy_removed"])
            self.assertFalse(source.exists())
            self.assertEqual(list(Path(directory).glob("source.migrated-*")), [])
            self.assertEqual(destination.joinpath("one.txt").read_text(encoding="utf-8"), "one")

    def test_migration_cleanup_clears_readonly_retired_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            destination = Path(directory) / "dest"
            source.mkdir()
            readonly = source / "readonly.idx"
            readonly.write_bytes(b"fixture")
            readonly.chmod(stat.S_IREAD)
            result = switchboard.migrate_directory(
                source,
                destination,
                execute=True,
                junction=False,
                cleanup_retired=True,
            )
            self.assertTrue(result["source_copy_removed"])
            self.assertEqual(list(Path(directory).glob("source.migrated-*")), [])
            self.assertEqual(destination.joinpath("readonly.idx").read_bytes(), b"fixture")

    def test_partial_retired_cleanup_is_preserved_on_destination_drive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "dest"
            retired = root / "source.migrated-20260820-120000-000001"
            destination.mkdir()
            retired.mkdir()
            (destination / "active.txt").write_text("new active state", encoding="utf-8")
            (retired / "old-only.txt").write_text("preserve me", encoding="utf-8")
            recovery = switchboard.remove_retired_source_copy(
                retired,
                source,
                destination,
                junction=False,
            )
            self.assertIsNotNone(recovery)
            assert recovery is not None
            self.assertFalse(retired.exists())
            self.assertEqual(recovery.joinpath("old-only.txt").read_text(encoding="utf-8"), "preserve me")
            receipt = recovery.parent / f"{recovery.name}.receipt.json"
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["files"], 1)

    def test_migration_defers_publication_if_writer_reappears(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            destination = Path(directory) / "dest"
            source.mkdir()
            (source / "one.txt").write_text("one", encoding="utf-8")
            with self.assertRaises(switchboard.MigrationRetryable):
                switchboard.migrate_directory(
                    source,
                    destination,
                    execute=True,
                    junction=False,
                    pre_publish_probe=lambda: ["busy"],
                )
            self.assertTrue(source.exists())
            self.assertTrue(destination.joinpath("one.txt").exists())

    def test_migration_recognizes_completed_junction_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            destination = Path(directory) / "dest"
            source.mkdir()
            destination.mkdir()
            (destination / "one.txt").write_text("one", encoding="utf-8")
            source.rmdir()
            switchboard.make_junction(source, destination)
            result = switchboard.migrate_directory(
                source,
                destination,
                execute=True,
                junction=True,
                resume_existing=True,
            )
            self.assertEqual(result["status"], "already_migrated")

    def test_migration_preflight_rejects_later_destination_before_first_move(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            first = Path(directory) / "first"
            second = Path(directory) / "second"
            first.mkdir()
            second.mkdir()
            (first / "one.txt").write_text("one", encoding="utf-8")
            (second / "two.txt").write_text("two", encoding="utf-8")
            first_dest = Path(directory) / "first-dest"
            second_dest = Path(directory) / "second-dest"
            second_dest.mkdir()
            with self.assertRaises(FileExistsError):
                switchboard.execute_migration(
                    paths,
                    [
                        ("first", first, first_dest, False),
                        ("second", second, second_dest, False),
                    ],
                    process_probe=lambda _: [],
                    lock_probe=lambda _: [],
                    backup_fn=lambda _: [],
                )
            self.assertTrue(first.exists())
            self.assertFalse(first_dest.exists())

    def test_wait_for_migration_requires_stable_clear_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            source = Path(directory) / "source"
            destination = Path(directory) / "dest"
            source.mkdir()
            (source / "one.txt").write_text("one", encoding="utf-8")
            targets = [("fixture", source, destination, False)]
            probe_results = [["busy"], [], []]

            def process_probe(_paths):
                return probe_results.pop(0) if probe_results else []

            result = switchboard.wait_for_migration(
                paths,
                targets,
                process_probe=process_probe,
                lock_probe=lambda _: [],
                backup_fn=lambda _: [],
                poll_seconds=0,
                stable_empty_checks=2,
                sleep_fn=lambda _seconds: None,
            )
            self.assertEqual(result["results"][0]["status"], "migrated")
            self.assertTrue(destination.joinpath("one.txt").exists())
            events = [
                json.loads(line)["event"]
                for line in (paths.switchboard / "migration-wait.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events, ["started", "waiting", "ready", "completed"])

    def test_projection_status_rejects_complete_zero_turn_thread_and_suggests_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            cwd = str(Path(directory) / "workspace")
            create_projection_fixture(
                paths,
                [
                    {
                        "id": "broken",
                        "cwd": cwd,
                        "first_user_message": "same task",
                        "projected_turns": 0,
                        "projected_items": 0,
                        "updated_at_ms": 4000,
                    },
                    {
                        "id": "readable",
                        "cwd": cwd,
                        "first_user_message": "same task",
                        "projected_turns": 4,
                        "projected_items": 9,
                        "updated_at_ms": 3000,
                    },
                    {
                        "id": "zero-candidate",
                        "cwd": cwd,
                        "first_user_message": "same task",
                        "projected_turns": 0,
                        "updated_at_ms": 5000,
                    },
                    {
                        "id": "wrong-cwd",
                        "cwd": str(Path(directory) / "other"),
                        "first_user_message": "same task",
                        "projected_turns": 10,
                        "updated_at_ms": 6000,
                    },
                ],
            )

            status = switchboard.thread_history_projection_status(paths, "broken")

            self.assertIsNotNone(status)
            self.assertTrue(status["projection_complete"])
            self.assertTrue(status["raw_nonempty"])
            self.assertTrue(status["unreadable"])
            self.assertEqual(status["projected_turns"], 0)
            self.assertEqual([item["id"] for item in status["candidates"]], ["readable"])
            with self.assertRaises(switchboard.UnreadableThreadHistoryError) as raised:
                switchboard.ensure_thread_history_readable(paths, "broken")
            self.assertEqual(raised.exception.candidate_thread_ids, ("readable",))

    def test_projection_status_blocks_pending_but_accepts_complete_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            create_projection_fixture(
                paths,
                [
                    {
                        "id": "pending",
                        "first_user_message": "pending task",
                        "projection_offset": 0,
                        "projection_ordinal": 0,
                        "projected_turns": 0,
                    },
                    {
                        "id": "readable",
                        "first_user_message": "readable task",
                        "projected_turns": 1,
                    },
                ],
            )

            pending = switchboard.thread_history_projection_status(paths, "pending")
            readable = switchboard.ensure_thread_history_readable(paths, "readable")

            self.assertFalse(pending["projection_complete"])
            self.assertEqual(pending["health"], "pending")
            self.assertFalse(pending["safe_to_fork"])
            self.assertFalse(pending["unreadable"])
            with self.assertRaises(switchboard.UnreadableThreadHistoryError):
                switchboard.ensure_thread_history_readable(paths, "pending")
            self.assertEqual(readable["projected_turns"], 1)
            self.assertEqual(readable["health"], "healthy")
            self.assertTrue(readable["safe_to_fork"])
            self.assertFalse(readable["unreadable"])

    def test_projection_status_detects_duplicate_gap_midline_and_missing_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))

            def line(ordinal, kind="event_msg"):
                return (json.dumps({"ordinal": ordinal, "type": kind, "payload": {}}) + "\n").encode()

            rows = []
            for thread_id, expected, offset_adjustment in (
                ("duplicate", 2, 0),
                ("gap", 0, 0),
                ("midline", 1, 1),
            ):
                metadata = (
                    json.dumps(
                        {
                            "ordinal": 0,
                            "type": "session_meta",
                            "payload": {"id": thread_id},
                        }
                    )
                    + "\n"
                ).encode()
                rows.append(
                    {
                        "id": thread_id,
                        "rollout_bytes": metadata + line(1),
                        "projection_offset": len(metadata) + offset_adjustment,
                        "projection_ordinal": expected,
                        "projected_turns": 1,
                    }
                )
            rows.append({"id": "missing", "projection_offset": None, "projected_turns": 1})
            create_projection_fixture(paths, rows)

            expected = {
                "duplicate": "projection_duplicate_or_rewind",
                "gap": "projection_ordinal_gap",
                "midline": "projection_offset_inside_record",
                "missing": "projection_cursor_missing",
            }
            for thread_id, reason in expected.items():
                with self.subTest(thread_id=thread_id):
                    status = switchboard.thread_history_projection_status(paths, thread_id)
                    self.assertFalse(status["safe_to_fork"])
                    self.assertEqual(status["reason"], reason)
                    with self.assertRaises(switchboard.UnreadableThreadHistoryError):
                        switchboard.ensure_thread_history_readable(paths, thread_id)

    def test_projection_status_uses_latest_segment_and_validates_history_base(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            root_id = "01a00000-0000-7000-8000-000000000001"
            segment_id = "01a00000-0000-7000-8000-000000000002"
            base_id = "01a00000-0000-7000-8000-000000000003"
            metadata = (
                json.dumps(
                    {
                        "ordinal": 5,
                        "type": "session_meta",
                        "payload": {
                            "id": root_id,
                            "history_base": {
                                "thread_id": base_id,
                                "end_byte_offset": 10,
                                "end_ordinal_exclusive": 5,
                            },
                        },
                    }
                )
                + "\n"
            ).encode()
            payload = metadata + (json.dumps({"ordinal": 6, "type": "event_msg", "payload": {}}) + "\n").encode()
            create_projection_fixture(
                paths,
                [{"id": root_id, "rollout_bytes": payload, "projected_turns": 1, "projection_ordinal": 7}],
            )
            original = paths.codex_home / "sessions" / f"{root_id}.jsonl"
            segmented = original.with_name(f"rollout-{root_id}_{segment_id}.jsonl")
            original.rename(segmented)
            state = sqlite3.connect(paths.state_database)
            history = sqlite3.connect(paths.thread_history_database)
            try:
                state.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(segmented), root_id))
                history.execute("DELETE FROM thread_history_projection_state WHERE thread_id=?", (root_id,))
                history.execute(
                    "INSERT INTO thread_history_projection_state VALUES(?,?,?)",
                    (segment_id, len(payload), 7),
                )
                history.execute(
                    "INSERT INTO thread_history_projection_state VALUES(?,?,?)",
                    (base_id, 10, 5),
                )
                state.commit()
                history.commit()
            finally:
                history.close()
                state.close()

            # A fabricated cursor with no base file cannot prove inheritance.
            unsafe_base = switchboard.thread_history_projection_status(paths, root_id)
            self.assertEqual(unsafe_base["segment_id"], segment_id)
            self.assertFalse(unsafe_base["safe_to_fork"])

            history = sqlite3.connect(paths.thread_history_database)
            try:
                history.execute(
                    "UPDATE thread_history_projection_state SET next_rollout_byte_offset=9 "
                    "WHERE thread_id=?",
                    (base_id,),
                )
                history.commit()
            finally:
                history.close()
            unsafe = switchboard.thread_history_projection_status(paths, root_id)
            self.assertEqual(unsafe["health"], "unknown")
            self.assertEqual(unsafe["reason"], "history_base_projection_incomplete")

    def test_complete_projection_still_rejects_duplicate_ordinal_in_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            thread_id = "duplicate-at-eof"
            payload = (
                json.dumps(
                    {"ordinal": 0, "type": "session_meta", "payload": {"id": thread_id}}
                )
                + "\n"
                + json.dumps(
                    {"ordinal": 0, "type": "event_msg", "payload": {"type": "thread_settings_applied"}}
                )
                + "\n"
            ).encode()
            create_projection_fixture(
                paths,
                [
                    {
                        "id": thread_id,
                        "rollout_bytes": payload,
                        "projection_ordinal": 1,
                        "projected_turns": 1,
                    }
                ],
            )

            status = switchboard.thread_history_projection_status(paths, thread_id)
            self.assertEqual(status["health"], "stalled")
            self.assertEqual(status["reason"], "rollout_duplicate_ordinal")
            with self.assertRaises(switchboard.UnreadableThreadHistoryError) as raised:
                switchboard.ensure_thread_history_readable(paths, thread_id)
            self.assertEqual(raised.exception.reason, "rollout_duplicate_ordinal")

    def test_thread_switch_rejects_unreadable_history_before_profile_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"test")
            paths.config.write_text(
                'model = "old-model"\nmodel_provider = "openai"\n',
                encoding="utf-8",
            )
            create_projection_fixture(
                paths,
                [
                    {
                        "id": "broken",
                        "cwd": str(Path(directory) / "workspace"),
                        "first_user_message": "non-empty task",
                        "projected_turns": 0,
                    }
                ],
            )
            before_active = paths.active.read_bytes()
            before_config = paths.config.read_bytes()
            client_calls = []
            original_client = switchboard._appserver_client
            try:
                switchboard._appserver_client = lambda *_args, **_kwargs: client_calls.append(True)
                with self.assertRaises(switchboard.UnreadableThreadHistoryError):
                    switchboard.switch_thread_provider(
                        paths,
                        "broken",
                        "maylily",
                        blocker_probe=lambda _paths: [],
                    )
            finally:
                switchboard._appserver_client = original_client

            self.assertEqual(client_calls, [])
            self.assertEqual(paths.active.read_bytes(), before_active)
            self.assertEqual(paths.config.read_bytes(), before_config)

    def test_relay_switch_rejects_missing_key_before_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            before = switchboard.load_active(paths)
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                switchboard.set_active_profile(paths, "maylily")
            self.assertEqual(switchboard.load_active(paths), before)

    @binding_only_history
    def test_thread_fork_rolls_back_runtime_projection_when_fork_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"test")
            paths.config.write_text(
                'model = "old-model"\nmodel_provider = "openai"\n',
                encoding="utf-8",
            )
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [{"id": "thread-1", "model_provider": "openai", "cwd": cwd, "name": "demo"}],
            )
            before_active = paths.active.read_bytes()
            before_config = paths.config.read_bytes()
            before_versions = paths.provider_versions.read_bytes()

            class FakeClient:
                def __init__(self):
                    self.closed = False

                def snapshot_thread(self, _thread_id):
                    return {
                        "id": "thread-1",
                        "name": "demo",
                        "cwd": cwd,
                        "modelProvider": "openai",
                    }

                def fork_thread(self, *_args, **_kwargs):
                    raise AppServerProtocolError("simulated fork rejection", method="thread/fork", code=-32602)

                def close(self):
                    self.closed = True

            fake = FakeClient()
            original_client = switchboard._appserver_client
            original_blockers = switchboard.appserver_blocking_processes
            try:
                switchboard._appserver_client = lambda *_args, **_kwargs: fake
                switchboard.appserver_blocking_processes = lambda _paths: []
                with self.assertRaisesRegex(RuntimeError, "simulated fork rejection"):
                    switchboard.fork_thread_provider(paths, "thread-1", "maylily")
            finally:
                switchboard._appserver_client = original_client
                switchboard.appserver_blocking_processes = original_blockers
            self.assertTrue(fake.closed)
            self.assertEqual(paths.active.read_bytes(), before_active)
            self.assertEqual(paths.config.read_bytes(), before_config)
            self.assertEqual(paths.provider_versions.read_bytes(), before_versions)

    @binding_only_history
    def test_thread_fork_detects_source_identity_change(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"test")
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [{"id": "thread-1", "model_provider": "openai", "cwd": cwd, "name": "before"}],
            )

            class FakeClient:
                def __init__(self):
                    self.snapshot_count = 0
                    self.closed = False

                def snapshot_thread(self, requested_id):
                    self.snapshot_count += 1
                    if requested_id == "thread-2":
                        return {
                            "id": "thread-2",
                            "name": "before",
                            "cwd": cwd,
                            "modelProvider": "custom",
                        }
                    return {
                        "id": "thread-1",
                        "name": "before" if self.snapshot_count == 1 else "after",
                        "cwd": cwd,
                        "modelProvider": "openai",
                    }

                def fork_thread(self, *_args, **_kwargs):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute(
                        "insert into threads(id, model_provider, cwd, model, name, archived) values(?,?,?,?,?,0)",
                        ("thread-2", "custom", cwd, "gpt-5.6-sol", "before"),
                    )
                    connection.commit()
                    connection.close()
                    return {
                        "thread": {
                            "id": "thread-2",
                            "name": "before",
                            "cwd": cwd,
                            "modelProvider": "custom",
                            "forkedFromId": "thread-1",
                        },
                        "cwd": cwd,
                        "modelProvider": "custom",
                    }

                def close(self):
                    self.closed = True

            fake = FakeClient()
            original_client = switchboard._appserver_client
            original_blockers = switchboard.appserver_blocking_processes
            try:
                switchboard._appserver_client = lambda *_args, **_kwargs: fake
                switchboard.appserver_blocking_processes = lambda _paths: []
                with self.assertRaisesRegex(RuntimeError, "source thread identity changed"):
                    switchboard.fork_thread_provider(paths, "thread-1", "maylily")
            finally:
                switchboard._appserver_client = original_client
                switchboard.appserver_blocking_processes = original_blockers
            self.assertTrue(fake.closed)

    @binding_only_history
    def test_thread_fork_persists_new_binding_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"test")
            switchboard.repair_config(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [{"id": "thread-1", "model_provider": "openai", "cwd": cwd, "name": "legacy"}],
            )
            before_active = paths.active.read_bytes()

            class FakeClient:
                def __init__(self):
                    self.closed = False

                def snapshot_thread(self, requested_id):
                    if requested_id == "thread-2":
                        return {
                            "id": "thread-2",
                            "name": "legacy",
                            "cwd": cwd,
                            "modelProvider": "custom",
                        }
                    return {
                        "id": "thread-1",
                        "name": "legacy",
                        "cwd": cwd,
                        "modelProvider": "openai",
                    }

                def fork_thread(self, *_args, **_kwargs):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute(
                        "insert into threads(id, model_provider, cwd, model, name, archived) values(?,?,?,?,?,0)",
                        ("thread-2", "custom", cwd, "gpt-5.6-sol", "legacy"),
                    )
                    connection.commit()
                    connection.close()
                    return {
                        "thread": {
                            "id": "thread-2",
                            "name": "legacy",
                            "cwd": cwd,
                            "modelProvider": "custom",
                            "forkedFromId": "thread-1",
                        },
                        "cwd": cwd,
                        "modelProvider": "custom",
                    }

                def close(self):
                    self.closed = True

            fake = FakeClient()
            client_starts = []
            original_client = switchboard._appserver_client
            original_blockers = switchboard.appserver_blocking_processes
            try:
                switchboard._appserver_client = lambda *_args, **_kwargs: (
                    client_starts.append(True) or fake
                )
                switchboard.appserver_blocking_processes = lambda _paths: []
                result = switchboard.fork_thread_provider(paths, "thread-1", "maylily")
            finally:
                switchboard._appserver_client = original_client
                switchboard.appserver_blocking_processes = original_blockers

            self.assertTrue(fake.closed)
            self.assertEqual(len(client_starts), 2)
            self.assertTrue(result["completion"]["restart_verified"])
            self.assertEqual(result["source_thread"]["modelProvider"], "openai")
            self.assertEqual(result["thread"]["id"], "thread-2")
            self.assertEqual(result["binding"]["provider_alias"], "custom")
            self.assertEqual(switchboard.thread_provider_binding(paths, "thread-1")["provider_alias"], "openai")
            self.assertEqual(paths.active.read_bytes(), before_active)

    def test_restart_verification_waits_and_rejects_missing_latest_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            statuses = [
                {"health": "pending", "reason": "projection_not_at_eof"},
                {"health": "unchecked", "reason": "rollout_integrity_not_checked"},
            ]

            class FakeClient:
                closed = False

                def snapshot_thread(self, _thread_id):
                    return {
                        "id": "child",
                        "cwd": str(paths.codex_home),
                        "modelProvider": "custom",
                    }

                def close(self):
                    self.closed = True

            fake = FakeClient()
            originals = (
                switchboard._appserver_client,
                switchboard.thread_history_projection_status,
                switchboard.ensure_thread_history_readable,
                switchboard._projected_turn_ids,
                switchboard.thread_provider_binding,
            )
            try:
                switchboard._appserver_client = lambda *_args, **_kwargs: fake
                switchboard.thread_history_projection_status = (
                    lambda *_args, **_kwargs: statuses.pop(0)
                )
                switchboard.ensure_thread_history_readable = lambda *_args, **_kwargs: {
                    "health": "healthy"
                }
                switchboard._projected_turn_ids = lambda *_args, **_kwargs: ["turn-1"]
                switchboard.thread_provider_binding = lambda *_args, **_kwargs: {
                    "thread_id": "child",
                    "provider_alias": "custom",
                    "cwd": str(paths.codex_home),
                    "rollout_path": None,
                }
                with self.assertRaisesRegex(RuntimeError, "different turn sequence"):
                    switchboard._verify_fork_after_restart(
                        paths,
                        {"thread_id": "source", "rollout_path": None},
                        "source",
                        "child",
                        "custom",
                        str(paths.codex_home),
                        executable=None,
                        expected_turn_ids=["turn-1", "turn-2"],
                    )
            finally:
                (
                    switchboard._appserver_client,
                    switchboard.thread_history_projection_status,
                    switchboard.ensure_thread_history_readable,
                    switchboard._projected_turn_ids,
                    switchboard.thread_provider_binding,
                ) = originals

            self.assertTrue(fake.closed)
            self.assertEqual(statuses, [])

    def test_restart_verification_reuse_requires_source_turn_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            projected = [["turn-1", "turn-2"]]

            class FakeClient:
                def snapshot_thread(self, _thread_id):
                    return {
                        "id": "child",
                        "cwd": str(paths.codex_home),
                        "modelProvider": "custom",
                    }

                def close(self):
                    return None

            originals = (
                switchboard._appserver_client,
                switchboard.thread_history_projection_status,
                switchboard.ensure_thread_history_readable,
                switchboard._projected_turn_ids,
                switchboard.thread_provider_binding,
            )
            try:
                switchboard._appserver_client = lambda *_args, **_kwargs: FakeClient()
                switchboard.thread_history_projection_status = lambda *_args, **_kwargs: {
                    "health": "unchecked",
                    "reason": "rollout_integrity_not_checked",
                }
                switchboard.ensure_thread_history_readable = lambda *_args, **_kwargs: {
                    "health": "healthy"
                }
                switchboard._projected_turn_ids = lambda *_args, **_kwargs: projected[0]
                switchboard.thread_provider_binding = lambda *_args, **_kwargs: {
                    "thread_id": "child",
                    "rollout_path": None,
                }
                verified = switchboard._verify_fork_after_restart(
                    paths,
                    {"thread_id": "source", "rollout_path": None},
                    "source",
                    "child",
                    "custom",
                    str(paths.codex_home),
                    executable=None,
                    expected_turn_ids=["turn-1"],
                    allow_additional_turns=True,
                )
                self.assertEqual(verified["last_turn_id"], "turn-2")

                projected[0] = ["other", "turn-2"]
                with self.assertRaisesRegex(RuntimeError, "different turn sequence"):
                    switchboard._verify_fork_after_restart(
                        paths,
                        {"thread_id": "source", "rollout_path": None},
                        "source",
                        "child",
                        "custom",
                        str(paths.codex_home),
                        executable=None,
                        expected_turn_ids=["turn-1"],
                        allow_additional_turns=True,
                    )
            finally:
                (
                    switchboard._appserver_client,
                    switchboard.thread_history_projection_status,
                    switchboard.ensure_thread_history_readable,
                    switchboard._projected_turn_ids,
                    switchboard.thread_provider_binding,
                ) = originals

    @binding_only_history
    def test_committed_fork_validation_failure_is_reconciled_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"test")
            switchboard.repair_config(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [{"id": "source", "model_provider": "openai", "cwd": cwd, "name": "legacy"}],
            )

            class FakeClient:
                def __init__(self):
                    self.fork_calls = 0

                def snapshot_thread(self, requested_id):
                    provider = "custom" if requested_id == "child" else "openai"
                    return {
                        "id": requested_id,
                        "name": "legacy",
                        "cwd": cwd,
                        "modelProvider": provider,
                    }

                def fork_thread(self, *_args, **_kwargs):
                    self.fork_calls += 1
                    write_history_base(paths, "child", "source")
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute(
                        "insert into threads(id, model_provider, cwd, model, name, archived, "
                        "rollout_path, created_at_ms, updated_at_ms, is_pinned) "
                        "values(?,?,?,?,?,?,?,?,?,?)",
                        (
                            "child",
                            "custom",
                            cwd,
                            "gpt-5.6-sol",
                            "legacy",
                            0,
                            str(paths.codex_home / "sessions" / "child.jsonl"),
                            2000,
                            2000,
                            0,
                        ),
                    )
                    connection.commit()
                    connection.close()
                    raise AppServerPostCommitValidationError(
                        "returned projection differed after commit",
                        created_thread_id="child",
                    )

                def set_thread_name(self, *_args, **_kwargs):
                    return {}

                def archive_thread(self, _thread_id):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute("update threads set archived = 1")
                    connection.commit()
                    connection.close()

                def unarchive_thread(self, requested_id):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute("update threads set archived = 0 where id = ?", (requested_id,))
                    connection.commit()
                    connection.close()
                    return {"thread": {"id": requested_id}}

                def set_thread_pinned(self, requested_id, pinned):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute(
                        "update threads set is_pinned = ? where id = ?",
                        (int(pinned), requested_id),
                    )
                    connection.commit()
                    connection.close()
                    return {"thread": {"id": requested_id, "isPinned": bool(pinned)}}

                def close(self):
                    return None

            fake = FakeClient()
            original_client = switchboard._appserver_client
            original_blockers = switchboard.appserver_blocking_processes
            try:
                switchboard._appserver_client = lambda *_args, **_kwargs: fake
                switchboard.appserver_blocking_processes = lambda _paths: []
                first = switchboard.fork_thread_provider(paths, "source", "maylily")
                second = switchboard.fork_thread_provider(paths, "source", "maylily")
            finally:
                switchboard._appserver_client = original_client
                switchboard.appserver_blocking_processes = original_blockers

            self.assertTrue(first["recovered_post_commit"])
            self.assertEqual(first["thread"]["id"], "child")
            self.assertTrue(first["completion"]["core_complete"])
            self.assertTrue(second["reused"])
            self.assertEqual(second["thread"]["id"], "child")
            self.assertEqual(fake.fork_calls, 1)

    def test_reuses_only_fresh_direct_target_child(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {
                        "id": "source",
                        "model_provider": "custom",
                        "cwd": cwd,
                        "created_at_ms": 100,
                        "updated_at_ms": 150,
                    },
                    {
                        "id": "official-child",
                        "model_provider": "openai",
                        "cwd": cwd,
                        "parent_thread_id": "source",
                        "created_at_ms": 200,
                        "updated_at_ms": 220,
                    },
                ],
            )
            reused = switchboard.reusable_target_thread(paths, "source", "openai")
            self.assertEqual(reused["thread_id"], "official-child")
            connection = sqlite3.connect(paths.state_database)
            connection.execute("update threads set updated_at_ms = 250 where id = 'source'")
            connection.commit()
            connection.close()
            self.assertIsNone(switchboard.reusable_target_thread(paths, "source", "openai"))

    def test_reuses_history_cursor_even_when_mutable_source_timestamp_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {
                        "id": "source",
                        "model_provider": "custom",
                        "cwd": cwd,
                        "created_at_ms": 100,
                        "updated_at_ms": 500,
                    },
                    {
                        "id": "official-child",
                        "model_provider": "openai",
                        "cwd": cwd,
                        "parent_thread_id": "source",
                        "created_at_ms": 200,
                        "updated_at_ms": 220,
                    },
                ],
            )
            write_history_base(paths, "official-child", "source")

            reused = switchboard.reusable_target_thread(paths, "source", "openai")

            self.assertEqual(reused["thread_id"], "official-child")

    def test_does_not_reuse_history_cursor_after_source_rollout_grows(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {"id": "source", "model_provider": "custom", "cwd": cwd},
                    {
                        "id": "official-child",
                        "model_provider": "openai",
                        "cwd": cwd,
                        "parent_thread_id": "source",
                    },
                ],
            )
            write_history_base(paths, "official-child", "source")
            source_rollout = paths.codex_home / "sessions" / "source.jsonl"
            with source_rollout.open("ab") as handle:
                handle.write(b'{"type":"event_msg"}\n')

            self.assertIsNone(
                switchboard.reusable_target_thread(paths, "source", "openai")
            )

    def test_thread_family_projection_groups_lineage_and_searches_whole_family(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {
                        "id": "source",
                        "name": "original",
                        "model_provider": "custom",
                        "cwd": cwd,
                        "archived": 1,
                    },
                    {
                        "id": "relay-child",
                        "name": "relay history",
                        "model_provider": "custom",
                        "cwd": cwd,
                        "archived": 1,
                        "parent_thread_id": "source",
                    },
                    {
                        "id": "official-head",
                        "name": "current",
                        "model_provider": "openai",
                        "cwd": cwd,
                        "parent_thread_id": "relay-child",
                    },
                ],
            )

            rows = switchboard.thread_family_bindings(paths, query="relay history")

            self.assertEqual([row["thread_id"] for row in rows], [
                "official-head",
                "relay-child",
                "source",
            ])
            self.assertTrue(all(row["family_id"] == "source" for row in rows))
            self.assertTrue(all(row["family_size"] == 3 for row in rows))
            self.assertEqual(rows[0]["family_role"], "head")
            self.assertEqual(rows[1]["family_role"], "ancestor")
            active_only = switchboard.thread_family_bindings(paths, include_archived=False)
            self.assertEqual([row["thread_id"] for row in active_only], ["official-head"])

    def test_internal_subagents_are_identified_named_and_optionally_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {
                        "id": "source",
                        "name": "用户任务",
                        "model_provider": "custom",
                        "cwd": cwd,
                        "updated_at_ms": 1000,
                    },
                    {
                        "id": "internal-worker",
                        "name": None,
                        "model_provider": "custom",
                        "cwd": cwd,
                        "parent_thread_id": "source",
                        "updated_at_ms": 3000,
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "agent_path": "/root/diagnose_skirt_underlayer",
                                    "agent_nickname": "Popper",
                                }
                            }
                        },
                    },
                    {
                        "id": "official-head",
                        "name": "用户任务",
                        "model_provider": "openai",
                        "cwd": cwd,
                        "parent_thread_id": "source",
                        "updated_at_ms": 4000,
                    },
                ],
            )

            raw = switchboard.recent_thread_bindings(paths, limit=10, include_archived=True)
            internal = next(row for row in raw if row["thread_id"] == "internal-worker")
            self.assertTrue(internal["is_subagent"])
            self.assertEqual(internal["display_name"], "内部子任务：diagnose_skirt_underlayer")

            hidden = switchboard.thread_family_bindings(
                paths,
                include_subagents=False,
            )
            self.assertEqual([row["thread_id"] for row in hidden], ["official-head", "source"])
            self.assertTrue(all(row["family_size"] == 2 for row in hidden))
            shown = switchboard.thread_family_bindings(paths, include_subagents=True)
            self.assertIn("internal-worker", [row["thread_id"] for row in shown])

    def test_task_list_supports_recent_and_native_codex_pin_order(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {"id": "pinned-first", "name": "A", "cwd": cwd, "updated_at_ms": 1000},
                    {"id": "pinned-second", "name": "B", "cwd": cwd, "updated_at_ms": 2000},
                    {"id": "recent", "name": "C", "cwd": cwd, "updated_at_ms": 5000},
                ],
            )
            switchboard.atomic_write_json(
                paths.global_state,
                {
                    "electron-persisted-atom-state": {
                        "app-server-pinned-thread-order-v1": [
                            "pinned-second",
                            "pinned-first",
                        ]
                    }
                },
            )

            recent = switchboard.task_list_bindings(
                paths,
                limit=10,
                sort_mode=switchboard.THREAD_SORT_RECENT,
            )
            self.assertEqual([row["thread_id"] for row in recent], [
                "recent",
                "pinned-second",
                "pinned-first",
            ])
            codex = switchboard.task_list_bindings(
                paths,
                limit=10,
                sort_mode=switchboard.THREAD_SORT_CODEX,
            )
            self.assertEqual([row["thread_id"] for row in codex], [
                "pinned-second",
                "pinned-first",
                "recent",
            ])
            self.assertEqual(codex[0]["codex_pinned_index"], 1)

    def test_task_list_filters_subagents_before_applying_user_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            rows = [
                {
                    "id": "older-user-task",
                    "name": "较旧的正常任务",
                    "cwd": cwd,
                    "updated_at_ms": 1,
                }
            ]
            rows.extend(
                {
                    "id": f"internal-{index:04d}",
                    "name": None,
                    "cwd": cwd,
                    "updated_at_ms": 1000 + index,
                    "source": {
                        "subagent": {
                            "thread_spawn": {"agent_path": f"/root/internal_{index:04d}"}
                        }
                    },
                }
                for index in range(520)
            )
            create_binding_fixture(paths, rows)

            visible = switchboard.task_list_bindings(
                paths,
                limit=40,
                include_subagents=False,
                sort_mode=switchboard.THREAD_SORT_RECENT,
            )

            self.assertEqual([row["thread_id"] for row in visible], ["older-user-task"])

    def test_router_required_uses_frozen_route_and_key_presence_only(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.ensure_provider_version(paths, "maylily")
            self.assertFalse(switchboard.router_required(paths))

            paths.keys.mkdir(parents=True, exist_ok=True)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"not-decrypted")

            self.assertTrue(switchboard.router_required(paths))

    def test_codex_family_sort_uses_native_pinned_head_order(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {"id": "older-pinned", "name": "置顶", "cwd": cwd, "updated_at_ms": 1000},
                    {"id": "newer", "name": "最近", "cwd": cwd, "updated_at_ms": 5000},
                ],
            )
            switchboard.atomic_write_json(
                paths.global_state,
                {
                    "electron-persisted-atom-state": {
                        "app-server-pinned-thread-order-v1": ["older-pinned"]
                    }
                },
            )

            rows = switchboard.thread_family_bindings(
                paths,
                include_subagents=False,
                sort_mode=switchboard.THREAD_SORT_CODEX,
            )

            self.assertEqual([row["thread_id"] for row in rows], ["older-pinned", "newer"])

    def test_family_cleanup_plan_and_action_keep_latest_head(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {
                        "id": "source",
                        "model_provider": "custom",
                        "cwd": cwd,
                        "updated_at_ms": 100,
                    },
                    {
                        "id": "head",
                        "model_provider": "openai",
                        "cwd": cwd,
                        "parent_thread_id": "source",
                        "updated_at_ms": 300,
                    },
                ],
            )
            with self.assertRaisesRegex(ValueError, "current family head"):
                switchboard.thread_family_cleanup_plan(paths, "source")
            plan = switchboard.thread_family_cleanup_plan(paths, "head")
            self.assertEqual(plan["head_thread_id"], "head")
            self.assertEqual(plan["candidate_thread_ids"], ["source"])

            class FakeClient:
                closed = False

                def archive_thread(self, requested_id):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute("update threads set archived = 1 where id = ?", (requested_id,))
                    connection.commit()
                    connection.close()

                def unarchive_thread(self, requested_id):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute("update threads set archived = 0 where id = ?", (requested_id,))
                    connection.commit()
                    connection.close()
                    return {"thread": {"id": requested_id}}

                def set_thread_pinned(self, requested_id, pinned):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute(
                        "update threads set is_pinned = ? where id = ?",
                        (int(pinned), requested_id),
                    )
                    connection.commit()
                    connection.close()
                    return {"thread": {"id": requested_id, "isPinned": bool(pinned)}}

                def close(self):
                    self.closed = True

            fake = FakeClient()
            result = switchboard.archive_thread_family(
                paths,
                "head",
                blocker_probe=lambda _paths: [],
                client_factory=lambda: fake,
            )

            self.assertTrue(fake.closed)
            self.assertTrue(result["complete"])
            self.assertEqual(result["archived_thread_ids"], ["source"])
            self.assertTrue(result["head_active"])
            self.assertTrue(result["head_pinned"])

    def test_archive_cleanup_keeps_new_head_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {"id": "source", "model_provider": "custom", "cwd": cwd},
                    {
                        "id": "head",
                        "model_provider": "openai",
                        "cwd": cwd,
                        "parent_thread_id": "source",
                    },
                ],
            )

            class FakeClient:
                def archive_thread(self, _thread_id):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute("update threads set archived = 1")
                    connection.commit()
                    connection.close()

                def unarchive_thread(self, requested_id):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute("update threads set archived = 0 where id = ?", (requested_id,))
                    connection.commit()
                    connection.close()
                    return {"thread": {"id": requested_id}}

                def set_thread_pinned(self, requested_id, pinned):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute(
                        "update threads set is_pinned = ? where id = ?",
                        (int(pinned), requested_id),
                    )
                    connection.commit()
                    connection.close()
                    return {"thread": {"id": requested_id, "isPinned": bool(pinned)}}

            status = switchboard._archive_source_keep_head(
                paths,
                FakeClient(),
                "source",
                "head",
                enabled=True,
            )
            self.assertTrue(status["source_archived"])
            self.assertTrue(status["head_unarchived"])
            self.assertTrue(status["source_unpinned"])
            self.assertTrue(status["head_pinned"])
            self.assertTrue(status["complete"])
            self.assertEqual(switchboard.thread_provider_binding(paths, "source")["archived"], 1)
            self.assertEqual(switchboard.thread_provider_binding(paths, "head")["archived"], 0)

    def test_default_publication_preserves_source_and_pins_new_head(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            cwd = str(Path(directory).resolve())
            create_binding_fixture(
                paths,
                [
                    {
                        "id": "source",
                        "model_provider": "openai",
                        "cwd": cwd,
                        "is_pinned": 1,
                    },
                    {
                        "id": "head",
                        "model_provider": "custom",
                        "cwd": cwd,
                        "parent_thread_id": "source",
                        "is_pinned": 0,
                    },
                ],
            )

            class FakeClient:
                def set_thread_pinned(self, requested_id, pinned):
                    connection = sqlite3.connect(paths.state_database)
                    connection.execute(
                        "UPDATE threads SET is_pinned=? WHERE id=?",
                        (int(pinned), requested_id),
                    )
                    connection.commit()
                    connection.close()
                    return {"thread": {"id": requested_id, "isPinned": bool(pinned)}}

            status = switchboard._archive_source_keep_head(
                paths,
                FakeClient(),
                "source",
                "head",
                enabled=False,
            )

            source = switchboard.thread_provider_binding(paths, "source")
            head = switchboard.thread_provider_binding(paths, "head")
            self.assertTrue(status["source_preserved"])
            self.assertFalse(status["source_archived"])
            self.assertEqual(source["archived"], 0)
            self.assertEqual(source["is_pinned"], 1)
            self.assertEqual(head["archived"], 0)
            self.assertEqual(head["is_pinned"], 1)
            self.assertTrue(status["complete"])

    def test_conversion_operation_lock_rejects_a_concurrent_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            with switchboard.conversion_operation_lock(paths):
                with self.assertRaisesRegex(RuntimeError, "already in progress"):
                    with switchboard.conversion_operation_lock(paths):
                        pass

    def test_conversion_mutex_name_is_stable_and_path_free(self):
        with tempfile.TemporaryDirectory() as directory:
            first = switchboard.Paths(Path(directory) / "home-a")
            second = switchboard.Paths(Path(directory) / "home-b")
            name = switchboard.conversion_mutex_name(first)

            self.assertEqual(name, switchboard.conversion_mutex_name(first))
            self.assertNotEqual(name, switchboard.conversion_mutex_name(second))
            self.assertNotIn(str(first.codex_home), name)

    def test_direct_official_profile_switch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            with self.assertRaisesRegex(RuntimeError, "official account"):
                switchboard.set_active_profile(paths, "official")

    def test_config_update_preserves_unrelated_settings_and_removes_bearer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'model = "gpt-test"\n'
                'model_provider = "openai"\n'
                '\n'
                '[features]\n'
                'memories = true\n'
                '\n'
                '[model_providers.custom]\n'
                'name = "maylily"\n'
                'base_url = "https://maylily.xyz"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = true\n'
                'model_catalog_json = "old-nested-catalog.json"\n'
                'experimental_bearer_token = "redacted-test"\n',
                encoding="utf-8",
            )
            result = switchboard.update_config_provider(path, "maylily")
            self.assertTrue(result["changed"])
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["model_provider"], "custom")
            self.assertEqual(parsed["features"]["memories"], True)
            self.assertEqual(
                parsed["model_providers"]["custom"]["base_url"],
                "http://127.0.0.1:8765/profiles/custom/v1",
            )
            self.assertFalse(parsed["model_providers"]["custom"]["requires_openai_auth"])
            self.assertTrue(parsed["model_catalog_json"].endswith(
                "switchboard\\model-catalogs\\maylily.json"
            ) or parsed["model_catalog_json"].endswith(
                "switchboard/model-catalogs/maylily.json"
            ))
            self.assertNotIn("model_catalog_json", parsed["model_providers"]["custom"])
            self.assertNotIn("experimental_bearer_token", path.read_text(encoding="utf-8"))
            switchboard.update_config_provider(path, "official")
            official = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("model_provider", official)
            self.assertNotIn("model", official)
            self.assertNotIn("model_catalog_json", official)
            self.assertNotIn("model_catalog_json", official["model_providers"]["custom"])

    def test_official_default_omits_override_but_preserves_relay_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switchboard.key_path(paths, "maylily-main").write_bytes(b"test")
            switchboard.set_active_profile(paths, "maylily")

            result = switchboard.set_active_profile(
                paths,
                "official",
                allow_official=True,
            )

            parsed = tomllib.loads(paths.config.read_text(encoding="utf-8"))
            self.assertNotIn("model_provider", parsed)
            self.assertNotIn("model_catalog_json", parsed)
            self.assertIn("custom", parsed["model_providers"])
            self.assertEqual(result["active"]["provider_alias"], "openai")
            self.assertEqual(
                switchboard.load_active(paths)["profile_id"],
                "official",
            )

    def test_repair_config_follows_active_profile_without_changing_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            key_path = switchboard.key_path(paths, "maylily-main")
            key_path.write_bytes(b"test")
            switched = switchboard.set_active_profile(paths, "maylily")
            revision = switched["active"]["revision"]
            paths.config.write_text('model_provider = "openai"\n', encoding="utf-8")
            result = switchboard.repair_config(paths)
            self.assertEqual(result["active"]["revision"], revision)
            self.assertEqual(tomllib.loads(paths.config.read_text())["model_provider"], "custom")

    def test_config_projection_detects_and_repairs_official_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            switched = switchboard.set_active_profile(
                paths,
                "official",
                allow_official=True,
            )
            revision = switched["active"]["revision"]
            self.assertTrue(switchboard.config_projection_status(paths)["ready"])
            original = paths.config.read_text(encoding="utf-8")
            paths.config.write_text('model = "relay-leftover"\n' + original, encoding="utf-8")

            drift = switchboard.config_projection_status(paths)

            self.assertFalse(drift["ready"])
            self.assertTrue(any("relay 模型覆盖" in reason for reason in drift["reasons"]))
            repaired = switchboard.repair_config(paths)
            self.assertEqual(repaired["active"]["revision"], revision)
            self.assertTrue(switchboard.config_projection_status(paths)["ready"])

    def test_configure_relay_updates_only_non_secret_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            result = switchboard.configure_relay_profile(
                paths,
                "relay-b",
                base_url="https://relay.example/v1",
                model="gpt-test",
            )
            self.assertEqual(result["profile"]["base_url"], "https://relay.example/v1")
            self.assertEqual(result["profile"]["model"], "gpt-test")
            self.assertFalse(result["key_configured"])
            stored = switchboard.load_profiles(paths)
            relay = switchboard.profile_by_id(stored, "relay-b")
            self.assertEqual(relay["key_ref"], "relay-b-main")

    def test_configure_relay_normalizes_default_and_allowed_models(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)

            result = switchboard.configure_relay_profile(
                paths,
                "relay-b",
                base_url="https://relay.example/v1",
                model="gpt-5.4",
                models="gpt-5.6-sol, gpt-5.4, gpt-5.6-sol",
            )

            self.assertEqual(result["profile"]["model"], "gpt-5.4")
            self.assertEqual(
                result["profile"]["models"],
                ["gpt-5.4", "gpt-5.6-sol"],
            )

    def test_adopt_legacy_key_removes_plaintext_after_encryption(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            paths.config.write_text(
                'model = "gpt-test"\nmodel_provider = "custom"\n\n'
                '[model_providers.custom]\n'
                'name = "maylily"\nbase_url = "https://maylily.xyz"\n'
                'experimental_bearer_token = "legacy-secret"\n',
                encoding="utf-8",
            )
            original_store = switchboard.store_key
            try:
                switchboard.store_key = lambda p, ref, secret: switchboard.key_path(p, ref).write_bytes(b"encrypted")
                result = switchboard.adopt_legacy_bearer_key(paths, "maylily")
            finally:
                switchboard.store_key = original_store
            self.assertEqual(result["status"], "adopted")
            self.assertTrue(switchboard.key_path(paths, "maylily-main").exists())
            text = paths.config.read_text(encoding="utf-8")
            self.assertNotIn("experimental_bearer_token", text)
            self.assertNotIn("legacy-secret", text)

    def test_writer_locks_ignore_old_empty_debris_but_probe_can_force_active(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            lock_dir = paths.codex_home / "thread-writer-locks"
            lock_dir.mkdir(parents=True)
            stale = lock_dir / "stale.lock"
            stale.write_bytes(b"")
            live = lock_dir / "live.lock"
            live.write_bytes(b"x")
            self.assertEqual(switchboard.writer_locks(paths, now=stale.stat().st_mtime + 3600), [])
            self.assertEqual(switchboard.writer_locks(paths, probe=lambda path: path == stale), [stale])

    def test_process_blocker_matches_real_migration_source_path(self):
        paths = switchboard.Paths(Path(r"E:\Codex-Home"))
        source = switchboard.migration_targets()[0][1] / "fixture" / "worker.py"
        original_lines = switchboard.process_lines
        try:
            switchboard.process_lines = lambda: [
                json.dumps(
                    {
                        "Name": "python.exe",
                        "ProcessId": 987654,
                        "ParentProcessId": 1,
                        "ExecutablePath": r"C:\Python\python.exe",
                        "CommandLine": f"python {source}",
                    }
                )
            ]
            self.assertEqual(len(switchboard.blocking_processes(paths)), 1)
        finally:
            switchboard.process_lines = original_lines

    def test_appserver_blocker_ignores_router_and_project_workers(self):
        paths = switchboard.Paths(Path(r"E:\Codex-Home"))
        original_lines = switchboard.process_lines
        lines = [
            json.dumps(
                {
                    "Name": "ChatGPT.exe",
                    "ProcessId": 987651,
                    "ExecutablePath": r"C:\Program Files\ChatGPT\ChatGPT.exe",
                    "CommandLine": "ChatGPT.exe",
                }
            ),
            json.dumps(
                {
                    "Name": "codex-code-mode-host.exe",
                    "ProcessId": 987652,
                    "ExecutablePath": r"C:\Codex\codex-code-mode-host.exe",
                    "CommandLine": "codex-code-mode-host.exe",
                }
            ),
            json.dumps(
                {
                    "Name": "python.exe",
                    "ProcessId": 987653,
                    "ExecutablePath": r"C:\Python\python.exe",
                    "CommandLine": r"python E:\Codex-Home\switchboard\switchboard.py router",
                }
            ),
            json.dumps(
                {
                    "Name": "python.exe",
                    "ProcessId": 987654,
                    "ExecutablePath": r"C:\Python\python.exe",
                    "CommandLine": r"python C:\Users\developer\Documents\Codex\project\worker.py",
                }
            ),
        ]
        try:
            switchboard.process_lines = lambda: lines
            result = switchboard.appserver_blocking_processes(paths)
        finally:
            switchboard.process_lines = original_lines
        names = [json.loads(line)["Name"] for line in result]
        self.assertEqual(names, ["ChatGPT.exe", "codex-code-mode-host.exe"])

    def test_appserver_executable_avoids_restricted_windowsapps_binary(self):
        if switchboard.os.name != "nt":
            self.skipTest("Windows executable resolution is platform-specific")
        resolved = Path(switchboard.resolve_appserver_executable())
        self.assertTrue(resolved.is_file())
        self.assertNotIn("WindowsApps", str(resolved))
        self.assertTrue(str(resolved).lower().endswith("codex.exe"))

    def test_explicit_appserver_executable_override_is_preserved(self):
        self.assertEqual(
            switchboard.resolve_appserver_executable(r"C:\custom\codex.exe"),
            r"C:\custom\codex.exe",
        )

    def test_appserver_executable_prefers_desktop_published_cli(self):
        if switchboard.os.name != "nt":
            self.skipTest("Windows executable resolution is platform-specific")
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            desktop_cli = Path(directory) / "desktop-codex.exe"
            desktop_cli.write_bytes(b"fixture")
            paths.config.write_text(
                "[mcp_servers.node_repl.env]\n"
                f"CODEX_CLI_PATH = {json.dumps(str(desktop_cli))}\n",
                encoding="utf-8",
            )

            resolved = switchboard.resolve_appserver_executable(paths=paths)

            self.assertEqual(Path(resolved), desktop_cli)

    def test_appserver_client_is_closed_when_initialize_fails(self):
        import appserver_client

        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)

            class FakeClient:
                def __init__(self, **_kwargs):
                    self.closed = False

                def start(self):
                    return None

                def initialize(self, **_kwargs):
                    raise RuntimeError("initialize failed")

                def close(self):
                    self.closed = True

            fake = FakeClient()
            original_client = appserver_client.AppServerClient
            try:
                appserver_client.AppServerClient = lambda **_kwargs: fake
                with self.assertRaisesRegex(RuntimeError, "initialize failed"):
                    switchboard._appserver_client(paths, executable="test-codex.exe")
            finally:
                appserver_client.AppServerClient = original_client

            self.assertTrue(fake.closed)

    def test_wait_for_appserver_exit_requires_stable_empty_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            probe_results = [["busy"], [], []]
            reports = []

            def process_probe(_paths):
                return probe_results.pop(0) if probe_results else []

            result = switchboard.wait_for_appserver_exit(
                paths,
                process_probe=process_probe,
                poll_seconds=0,
                stable_empty_checks=2,
                sleep_fn=lambda _seconds: None,
                progress_fn=reports.append,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(
                [(item["blockers"], item["stable_empty_checks"]) for item in reports],
                [(1, 0), (0, 1), (0, 2)],
            )

    def test_wait_and_fork_waits_before_creating_thread_result(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            switchboard.initialize(paths)
            original_fork = switchboard.fork_thread_provider
            calls = []
            try:
                def fake_fork(*args, **kwargs):
                    calls.append((args, kwargs))
                    return {"thread": {"id": "thread-2", "modelProvider": "custom"}}

                switchboard.fork_thread_provider = fake_fork
                result = switchboard.wait_and_fork_thread_provider(
                    paths,
                    "thread-1",
                    "maylily",
                    process_probe=lambda _paths: [],
                    poll_seconds=0,
                )
            finally:
                switchboard.fork_thread_provider = original_fork
            self.assertEqual(result["wait"]["status"], "ready")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][1:3], ("thread-1", "maylily"))
            self.assertIsNotNone(calls[0][1].get("blocker_probe"))

    def test_sqlite_backup_uses_consistent_backup_api(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            paths.backups.mkdir(parents=True)
            source = paths.codex_home / "state_5.sqlite"
            source.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(source)
            try:
                connection.execute("create table sample(value integer)")
                connection.execute("insert into sample values(7)")
                connection.commit()
            finally:
                connection.close()
            backup = switchboard.backup_sqlite(paths, source, "state")
            check = sqlite3.connect(backup)
            try:
                self.assertEqual(check.execute("pragma quick_check").fetchone()[0], "ok")
                self.assertEqual(check.execute("select value from sample").fetchone()[0], 7)
            finally:
                check.close()

    def test_backup_retention_plan_only_marks_old_duplicate_series(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = switchboard.Paths(Path(directory))
            paths.backups.mkdir(parents=True)
            now = 1_800_000_000.0
            fixtures = [
                ("before-migration-logs_2-20260101-010101-000001.sqlite", 10, 10),
                ("before-migration-logs_2-20260102-010101-000002.sqlite", 20, 2),
                ("before-migration-logs_2-20260103-010101-000003.sqlite", 30, 1),
            ]
            for name, size, age_days in fixtures:
                path = paths.backups / name
                path.write_bytes(b"x" * size)
                modified = now - age_days * 86400
                os.utime(path, (modified, modified))

            plan = switchboard.backup_retention_plan(
                paths,
                keep_per_series=2,
                min_age_days=3,
                now_timestamp=now,
            )

            self.assertEqual(plan["entry_count"], 3)
            self.assertEqual(plan["candidate_count"], 1)
            self.assertEqual(
                plan["candidates"][0]["name"],
                "before-migration-logs_2-20260101-010101-000001.sqlite",
            )
            self.assertFalse(plan["policy"]["automatic_deletion"])
            self.assertTrue(all(path.exists() for path in paths.backups.iterdir()))

    def test_upstream_normalization(self):
        host, path, port, tls = switchboard.normalized_upstream(
            "https://relay.example/v1", "/v1/responses"
        )
        self.assertEqual(host, "relay.example")
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(port, 443)
        self.assertTrue(tls)


if __name__ == "__main__":
    unittest.main()
