import hashlib
import json
import sqlite3
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

import portable_migration


def create_home(root: Path, project: Path | None = None) -> tuple[Path, bytes]:
    home = root / "source-home"
    session = home / "sessions" / "2026" / "08" / "30" / "rollout-thread-1.jsonl"
    session.parent.mkdir(parents=True)
    session_bytes = b'{"type":"session_meta","payload":{"id":"thread-1"}}\n{"text":"hello"}\n'
    session.write_bytes(session_bytes)
    (home / "archived_sessions").mkdir()
    (home / "attachments").mkdir()
    (home / "attachments" / "image.txt").write_text("attachment", encoding="utf-8")
    (home / "generated_images").mkdir()
    (home / "generated_images" / "output.txt").write_text("generated", encoding="utf-8")
    (home / "visualizations").mkdir()
    (home / "visualizations" / "view.html").write_text("<p>view</p>", encoding="utf-8")
    (home / "memories").mkdir()
    (home / "memories" / "memory.md").write_text("memory", encoding="utf-8")
    (home / "rules").mkdir()
    (home / "rules" / "default.rules").write_text("rule", encoding="utf-8")
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("skill", encoding="utf-8")
    (home / "AGENTS.md").write_text("agents", encoding="utf-8")
    (home / "session_index.jsonl").write_text(
        '{"id":"thread-1","title":"Portable task"}\n', encoding="utf-8"
    )

    cwd = str((project or home / "workspace") / "nested")
    state = sqlite3.connect(home / "state_5.sqlite")
    try:
        state.execute(
            "CREATE TABLE threads("
            "id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INTEGER DEFAULT 0, "
            "name TEXT, title TEXT, is_pinned INTEGER, model_provider TEXT, model TEXT, "
            "updated_at INTEGER)"
        )
        state.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "thread-1",
                str(session),
                cwd,
                0,
                "Portable task",
                "Portable title",
                1,
                "openai",
                "gpt-test",
                123,
            ),
        )
        state.execute(
            "CREATE TABLE project_roots("
            "project_id TEXT, position INTEGER, path TEXT, PRIMARY KEY(project_id, position))"
        )
        if project is not None:
            state.execute("INSERT INTO project_roots VALUES('project-row',0,?)", (str(project),))
        state.execute("CREATE TABLE remote_control_enrollments(id TEXT)")
        state.execute("INSERT INTO remote_control_enrollments VALUES('old-machine')")
        state.commit()
    finally:
        state.close()

    history = sqlite3.connect(home / "thread_history_1.sqlite")
    try:
        history.execute("CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT)")
        history.execute("INSERT INTO thread_turns VALUES('thread-1','turn-1')")
        history.commit()
    finally:
        history.close()

    switchboard = home / "switchboard"
    (switchboard / "model-catalogs").mkdir(parents=True)
    (switchboard / "active.json").write_text(
        json.dumps({"profile_id": "relay", "access_token": "do-not-pack"}),
        encoding="utf-8",
    )
    (switchboard / "profiles.json").write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "relay",
                        "key_ref": "relay-main",
                        "api_key": "super-secret",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (switchboard / "provider-versions.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    (switchboard / "model-catalogs" / "relay.json").write_text(
        json.dumps({"models": []}), encoding="utf-8"
    )
    (switchboard / "keys").mkdir()
    (switchboard / "keys" / "relay-main.dpapi").write_text("ciphertext", encoding="utf-8")

    (home / "auth.json").write_text("official-token", encoding="utf-8")
    (home / ".sandbox-secrets").mkdir()
    (home / ".sandbox-secrets" / "secret").write_text("sandbox-token", encoding="utf-8")
    (home / "logs").mkdir()
    (home / "logs" / "runtime.log").write_text("log", encoding="utf-8")
    (home / "cache").mkdir()
    (home / "cache" / "cache.bin").write_bytes(b"cache")
    (home / "backups").mkdir()
    (home / "backups" / "old.sqlite").write_bytes(b"backup")
    return home, session_bytes


def replace_zip_entry(
    source: Path,
    destination: Path,
    entry_name: str,
    payload: bytes,
    *,
    update_manifest: bool = False,
) -> None:
    with zipfile.ZipFile(source, "r") as old:
        contents = {info.filename: old.read(info) for info in old.infolist()}
    contents[entry_name] = payload
    if update_manifest:
        manifest = json.loads(contents[portable_migration.MANIFEST_NAME])
        for item in manifest["items"]:
            if item["path"] == entry_name:
                item["size"] = len(payload)
                item["sha256"] = hashlib.sha256(payload).hexdigest()
        manifest["summary"]["total_size"] = sum(item["size"] for item in manifest["items"])
        contents[portable_migration.MANIFEST_NAME] = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    with zipfile.ZipFile(destination, "w", allowZip64=True) as new:
        for name, data in contents.items():
            new.writestr(name, data)


class PortableMigrationTests(unittest.TestCase):
    def test_roundtrip_restores_safe_home_and_project_without_changing_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "source-project"
            (project / "nested").mkdir(parents=True)
            (project / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (project / ".env").write_text("API_KEY=do-not-pack", encoding="utf-8")
            (project / ".npmrc").write_text("//registry/:_authToken=do-not-pack", encoding="utf-8")
            (project / ".git").mkdir()
            (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (project / ".git" / "config").write_text(
                "[remote \"origin\"]\nurl=https://token@example.invalid/repo\n",
                encoding="utf-8",
            )
            (project / ".sandbox-secrets").mkdir()
            (project / ".sandbox-secrets" / "key").write_text("secret", encoding="utf-8")
            home, original_session = create_home(root, project)
            package = root / "migration.codexpack"

            plan = portable_migration.build_export_plan(home, [project])
            self.assertEqual(plan["summary"]["task_count"], 1)
            self.assertFalse(any("auth.json" in item["path"] for item in plan["items"]))
            self.assertFalse(
                any(item["path"] == "home/thread_history_1.sqlite" for item in plan["items"])
            )

            created = portable_migration.create_pack(home, package, projects=[project])
            inspected = portable_migration.inspect_pack(package)
            self.assertEqual(created["package"], str(package))
            self.assertTrue(inspected["verified"])
            self.assertEqual(inspected["task_count"], 1)
            self.assertEqual(inspected["project_count"], 1)
            self.assertEqual(inspected["tasks"][0]["id"], "thread-1")
            self.assertEqual(inspected["tasks"][0]["name"], "Portable task")
            self.assertEqual(inspected["tasks"][0]["model_provider"], "openai")

            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())
                self.assertNotIn("home/auth.json", names)
                self.assertNotIn("home/thread_history_1.sqlite", names)
                self.assertFalse(any("sandbox-secrets" in name for name in names))
                self.assertFalse(any("switchboard/keys" in name for name in names))
                self.assertFalse(any(name.endswith("/.env") for name in names))
                self.assertFalse(any(name.endswith("/.npmrc") for name in names))
                self.assertFalse(any(name.endswith("/.git/config") for name in names))
                self.assertTrue(any(name.endswith("/.git/HEAD") for name in names))
                profiles = json.loads(archive.read("home/switchboard/profiles.json"))
                self.assertEqual(profiles["profiles"][0]["key_ref"], "relay-main")
                self.assertNotIn("api_key", profiles["profiles"][0])
                packaged_state = root / "packaged-state.sqlite"
                packaged_state.write_bytes(archive.read("home/state_5.sqlite"))
            state_in_pack = sqlite3.connect(packaged_state)
            try:
                self.assertEqual(
                    state_in_pack.execute(
                        "SELECT COUNT(*) FROM remote_control_enrollments"
                    ).fetchone()[0],
                    0,
                )
            finally:
                state_in_pack.close()
            self.assertNotIn(b"old-machine", packaged_state.read_bytes())

            target = root / "restored-home"
            target.mkdir()
            project_root = root / "restored-projects"
            project_root.mkdir()
            report = portable_migration.import_pack(
                package, target, project_root=project_root
            )
            restored_project = Path(report["project_destinations"]["project-0001"])
            self.assertEqual((restored_project / "app.py").read_text(), "print('ok')\n")
            self.assertFalse((restored_project / ".env").exists())
            restored_session = (
                target / "sessions" / "2026" / "08" / "30" / "rollout-thread-1.jsonl"
            )
            self.assertEqual(restored_session.read_bytes(), original_session)
            self.assertIn("Portable task", (target / "session_index.jsonl").read_text())

            state = sqlite3.connect(target / "state_5.sqlite")
            try:
                rollout_path, cwd = state.execute(
                    "SELECT rollout_path, cwd FROM threads WHERE id='thread-1'"
                ).fetchone()
                project_path = state.execute("SELECT path FROM project_roots").fetchone()[0]
                enrollment_count = state.execute(
                    "SELECT COUNT(*) FROM remote_control_enrollments"
                ).fetchone()[0]
            finally:
                state.close()
            self.assertEqual(Path(rollout_path), restored_session)
            self.assertEqual(Path(cwd), restored_project / "nested")
            self.assertEqual(Path(project_path), restored_project)
            self.assertEqual(enrollment_count, 0)
            self.assertFalse(report["activated"])

    def test_overlap_detection_resolves_existing_directory_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory links are unavailable: {exc}")
            self.assertTrue(
                portable_migration._paths_overlap(real / "future", linked / "future")
            )

    def test_nonempty_home_and_existing_project_destination_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "file.txt").write_text("x")
            home, _ = create_home(root, project)
            package = root / "migration.codexpack"
            portable_migration.create_pack(home, package, projects=[project])
            original_package = package.read_bytes()
            with self.assertRaises(FileExistsError):
                portable_migration.create_pack(home, package, projects=[project])
            self.assertEqual(package.read_bytes(), original_package)

            nonempty = root / "nonempty-home"
            nonempty.mkdir()
            (nonempty / "keep.txt").write_text("keep")
            with self.assertRaises(FileExistsError):
                portable_migration.import_pack(package, nonempty, project_root=root / "projects-a")
            self.assertEqual((nonempty / "keep.txt").read_text(), "keep")

            target = root / "new-home"
            project_root = root / "projects-b"
            (project_root / project.name).mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                portable_migration.import_pack(package, target, project_root=project_root)
            self.assertFalse(target.exists())

    def test_zip_slip_duplicate_entry_and_hash_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            traversal = root / "traversal.codexpack"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../escape.txt", b"escape")
                archive.writestr("manifest.json", b"{}")
            with self.assertRaises(portable_migration.PortableMigrationError):
                portable_migration.inspect_pack(traversal)

            duplicate = root / "duplicate.codexpack"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "w") as archive:
                    archive.writestr("manifest.json", b"{}")
                    archive.writestr("manifest.json", b"{}")
            with self.assertRaises(portable_migration.PortableMigrationError):
                portable_migration.inspect_pack(duplicate)

            home, _ = create_home(root)
            valid = root / "valid.codexpack"
            tampered = root / "tampered.codexpack"
            portable_migration.create_pack(home, valid)
            replace_zip_entry(
                valid,
                tampered,
                "home/sessions/2026/08/30/rollout-thread-1.jsonl",
                b"tampered",
            )
            with self.assertRaises(portable_migration.PortableMigrationError):
                portable_migration.inspect_pack(tampered)

    def test_sqlite_backup_includes_committed_wal_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = create_home(root)
            state = sqlite3.connect(home / "state_5.sqlite")
            history = sqlite3.connect(home / "thread_history_1.sqlite")
            try:
                state.execute("PRAGMA journal_mode=WAL")
                history.execute("PRAGMA journal_mode=WAL")
                state.execute(
                    "INSERT INTO threads(id,rollout_path,cwd,archived) VALUES(?,?,?,?)",
                    (
                        "thread-wal",
                        str(home / "sessions" / "wal.jsonl"),
                        str(home / "workspace"),
                        1,
                    ),
                )
                history.execute("INSERT INTO thread_turns VALUES('thread-wal','turn-wal')")
                state.commit()
                history.commit()

                package = root / "wal.codexpack"
                portable_migration.create_pack(home, package, include_projection=True)
            finally:
                state.close()
                history.close()

            target = root / "wal-home"
            portable_migration.import_pack(package, target)
            restored_state = sqlite3.connect(target / "state_5.sqlite")
            restored_history = sqlite3.connect(target / "thread_history_1.sqlite")
            try:
                self.assertEqual(
                    restored_state.execute(
                        "SELECT COUNT(*) FROM threads WHERE id='thread-wal'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    restored_history.execute(
                        "SELECT COUNT(*) FROM thread_turns WHERE thread_id='thread-wal'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                restored_state.close()
                restored_history.close()

    def test_projection_is_included_only_when_explicitly_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = create_home(root)
            package = root / "with-projection.codexpack"

            portable_migration.create_pack(home, package, include_projection=True)

            with zipfile.ZipFile(package) as archive:
                self.assertIn("home/thread_history_1.sqlite", set(archive.namelist()))

    def test_duplicate_modern_rollout_is_rejected_before_pack_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = create_home(root)
            rollout = home / "sessions" / "2026" / "08" / "30" / "rollout-thread-1.jsonl"
            rollout.write_text(
                json.dumps(
                    {"ordinal": 0, "type": "session_meta", "payload": {"id": "thread-1"}}
                )
                + "\n"
                + json.dumps(
                    {"ordinal": 0, "type": "event_msg", "payload": {"type": "thread_settings_applied"}}
                )
                + "\n",
                encoding="utf-8",
            )
            package = root / "unsafe.codexpack"

            with self.assertRaisesRegex(
                portable_migration.PortableMigrationError,
                "rollout history integrity failed",
            ):
                portable_migration.create_pack(home, package)

            self.assertFalse(package.exists())

    def test_metadata_only_duplicate_is_accepted_for_portable_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = create_home(root)
            rollout = home / "sessions" / "2026" / "08" / "30" / "rollout-thread-1.jsonl"
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"ordinal": 0, "type": "session_meta", "payload": {"id": "thread-1"}}
                        ),
                        json.dumps(
                            {"ordinal": 1, "type": "event_msg", "payload": {"type": "token_count"}}
                        ),
                        json.dumps(
                            {
                                "ordinal": 1,
                                "type": "event_msg",
                                "payload": {"type": "thread_settings_applied"},
                            }
                        ),
                        json.dumps(
                            {"ordinal": 2, "type": "event_msg", "payload": {"type": "task_complete"}}
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            package = root / "metadata-duplicate.codexpack"

            result = portable_migration.create_pack(home, package)

            self.assertTrue(package.exists())
            self.assertTrue(result["verified"])

    def test_explicit_project_destination_rewrites_nested_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "old-project"
            (project / "src" / "nested").mkdir(parents=True)
            (project / "src" / "nested" / "app.txt").write_text("app")
            home, _ = create_home(root, project / "src")
            package = root / "mapping.codexpack"
            portable_migration.create_pack(home, package, projects=[project])
            destination = root / "chosen" / "renamed-project"
            destination.parent.mkdir()
            target = root / "mapped-home"

            report = portable_migration.import_pack(
                package,
                target,
                project_destinations={str(project.resolve()): destination},
            )

            state = sqlite3.connect(target / "state_5.sqlite")
            try:
                cwd = state.execute("SELECT cwd FROM threads WHERE id='thread-1'").fetchone()[0]
            finally:
                state.close()
            self.assertEqual(Path(cwd), destination / "src" / "nested")
            self.assertEqual(report["path_rewrites"]["cwd"], 1)

    def test_invalid_sqlite_fails_before_publishing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = create_home(root)
            valid = root / "valid.codexpack"
            invalid = root / "invalid-db.codexpack"
            portable_migration.create_pack(home, valid)
            replace_zip_entry(
                valid,
                invalid,
                "home/state_5.sqlite",
                b"not a sqlite database",
                update_manifest=True,
            )
            target = root / "never-published"

            with self.assertRaises(portable_migration.PortableMigrationError):
                portable_migration.import_pack(invalid, target)

            self.assertFalse(target.exists())
            self.assertFalse(any(root.glob(f".{target.name}.import-*")))


if __name__ == "__main__":
    unittest.main()
