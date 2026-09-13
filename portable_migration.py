"""Create, inspect, and restore portable Codex data packages.

The format is deliberately boring: a ZIP64 file with one JSON manifest and
relative payload paths.  Authentication material and machine/runtime state are
never admitted to the package.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from history_chain import is_benign_metadata_duplicate


PACK_SCHEMA = "codex-switchboard-portable"
PACK_VERSION = 1
MANIFEST_NAME = "manifest.json"

# These bounds are intentionally well above a normal Codex home while still
# rejecting obvious ZIP bombs before any payload is read or extracted.
MAX_ENTRIES = 250_000
MAX_FILE_SIZE = 128 * 1024**3
MAX_TOTAL_SIZE = 512 * 1024**3
MAX_MANIFEST_SIZE = 16 * 1024**2
MAX_COMPRESSION_RATIO = 10_000
MAX_TASKS = 100_000

_HOME_TREES = {
    "sessions",
    "archived_sessions",
    "attachments",
    "generated_images",
    "visualizations",
    "memories",
    "rules",
    "skills",
}
_HOME_FILES = {
    "AGENTS.md",
    "session_index.jsonl",
    "state_5.sqlite",
    "thread_history_1.sqlite",
    "transcription-history.jsonl",
}
_PUBLIC_SWITCHBOARD_FILES = {"active.json", "profiles.json", "provider-versions.json"}
_PUBLIC_SWITCHBOARD_TREES = {"model-catalogs"}
_SQLITE_FILES = {"state_5.sqlite", "thread_history_1.sqlite"}

_HOME_BLOCKED_DIRS = {
    ".cache",
    ".sandbox",
    ".sandbox-bin",
    ".sandbox-secrets",
    ".tmp",
    "backup",
    "backups",
    "browser",
    "cache",
    "history_sync_backups",
    "keys",
    "log",
    "logs",
    "mcp-oauth-locks",
    "process",
    "process_manager",
    "runtime-tmp",
    "thread-writer-locks",
    "tmp",
}
_PROJECT_BLOCKED_DIRS = {
    ".aws",
    ".azure",
    ".sandbox-secrets",
    ".ssh",
    "gcloud",
}
_SECRET_FILES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "_netrc",
    "auth.json",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_ecdsa",
    "id_rsa",
    "oauth.json",
    "secrets.json",
}
_SECRET_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
_RUNTIME_SUFFIXES = {".log", ".tmp"}
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_MACHINE_TABLES = (
    "remote_control_enrollments",
    "remote_control_devices",
    "remote_control_hosts",
    "remote_control_sessions",
    "device_enrollments",
    "device_registrations",
    "machine_bindings",
)
_TASK_INDEX_COLUMNS = (
    "id",
    "name",
    "title",
    "archived",
    "is_pinned",
    "model_provider",
    "model",
    "cwd",
    "updated_at",
    "updated_at_ms",
)
_PROJECT_PATH_COLUMNS = (("project_roots", "path"),)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PortableMigrationError(ValueError):
    """A package or migration target failed a safety/integrity check."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _emit(
    progress: Callable[[dict[str, Any]], Any] | None,
    phase: str,
    current: int = 0,
    total: int = 0,
    path: str | None = None,
) -> None:
    if progress is None:
        return
    event: dict[str, Any] = {
        "phase": phase,
        "current": current,
        "total": total,
    }
    if path is not None:
        event["path"] = path
    progress(event)


def _path_text(path: str | os.PathLike[str]) -> str:
    return os.fspath(Path(path).expanduser().absolute())


def _canonical_archive_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _safe_archive_parts(name: str) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise PortableMigrationError(f"unsafe archive path: {name!r}")
    if name.startswith("/") or name.endswith("/"):
        raise PortableMigrationError(f"unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PortableMigrationError(f"unsafe archive path: {name!r}")
    if len(name) > 4096:
        raise PortableMigrationError("archive path is too long")
    for part in parts:
        if part.endswith((" ", ".")) or ":" in part:
            raise PortableMigrationError(f"Windows-unsafe archive path: {name!r}")
        if any(ord(character) < 32 for character in part):
            raise PortableMigrationError(f"unsafe archive path: {name!r}")
        stem = part.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED:
            raise PortableMigrationError(f"Windows-reserved archive path: {name!r}")
    return parts


def _is_secret_file(name: str) -> bool:
    lower = name.casefold()
    if lower in _SECRET_FILES or lower.startswith(".env."):
        return True
    if Path(lower).suffix in _SECRET_SUFFIXES:
        return True
    return lower.startswith("dpapi") or lower.endswith(".dpapi")


def _is_excluded(parts: tuple[str, ...], *, scope: str, is_dir: bool) -> bool:
    lowered = tuple(part.casefold() for part in parts)
    blocked = _HOME_BLOCKED_DIRS if scope == "home" else _PROJECT_BLOCKED_DIRS
    if any(part in blocked for part in lowered):
        return True
    if is_dir:
        return False
    name = lowered[-1]
    if _is_secret_file(name):
        return True
    if scope == "project" and (
        len(lowered) >= 2
        and (
            lowered[-2:] == (".git", "config")
            or lowered[-2:] == (".docker", "config.json")
        )
    ):
        return True
    if scope == "home" and (
        name in {"cap_sid", "installation_id"}
        or name.endswith(tuple(_RUNTIME_SUFFIXES))
        or name.endswith(".lock")
        or name.endswith(".bak")
        or name.endswith("-wal")
        or name.endswith("-shm")
    ):
        return True
    return False


def _iter_regular_files(root: Path, *, scope: str) -> Iterable[tuple[Path, tuple[str, ...]]]:
    """Yield a stable, symlink-free tree without following junctions."""

    root = root.resolve()
    stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while stack:
        directory, relative = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise PortableMigrationError(f"cannot scan {directory}: {exc}") from exc
        child_directories: list[tuple[Path, tuple[str, ...]]] = []
        for entry in entries:
            parts = (*relative, entry.name)
            path = Path(entry.path)
            try:
                is_link = entry.is_symlink() or path.is_junction()
            except OSError as exc:
                raise PortableMigrationError(f"cannot inspect {path}: {exc}") from exc
            if is_link:
                raise PortableMigrationError(
                    f"linked content cannot be represented safely in a portable package: {path}"
                )
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise PortableMigrationError(f"cannot inspect {path}: {exc}") from exc
            if _is_excluded(parts, scope=scope, is_dir=is_directory):
                continue
            if is_directory:
                child_directories.append((path, parts))
            elif entry.is_file(follow_symlinks=False):
                yield path, parts
        stack.extend(reversed(child_directories))


def _is_allowed_home_relative(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    if len(parts) == 1:
        return parts[0] in _HOME_FILES
    if parts[0] in _HOME_TREES:
        return not _is_excluded(parts, scope="home", is_dir=False)
    if parts[0] != "switchboard":
        return False
    if len(parts) == 2 and parts[1] in _PUBLIC_SWITCHBOARD_FILES:
        return True
    return len(parts) >= 3 and parts[1] in _PUBLIC_SWITCHBOARD_TREES and not _is_excluded(
        parts, scope="home", is_dir=False
    )


def _sanitize_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", name, flags=re.UNICODE).strip(" .-")
    if not cleaned:
        cleaned = fallback
    if cleaned.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        cleaned = f"{fallback}-{cleaned}"
    return cleaned[:96]


def _normalize_projects(projects: Iterable[Any] | Mapping[Any, Any]) -> list[dict[str, Any]]:
    if isinstance(projects, Mapping):
        raw_items = [(str(name), value) for name, value in projects.items()]
    else:
        raw_items = [(None, value) for value in projects]
    normalized: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_names: set[str] = set()
    for index, (mapping_name, value) in enumerate(raw_items, 1):
        if isinstance(value, Mapping):
            raw_path = value.get("path", value.get("source_path", value.get("source")))
            requested_name = value.get("name", mapping_name)
        else:
            raw_path = value
            requested_name = mapping_name
        if raw_path is None:
            raise PortableMigrationError(f"project {index} has no source path")
        source = Path(os.fspath(raw_path)).expanduser().resolve()
        if not source.is_dir() or source.is_symlink() or source.is_junction():
            raise PortableMigrationError(f"project source is not a real directory: {source}")
        source_key = os.path.normcase(os.fspath(source))
        if source_key in seen_sources:
            raise PortableMigrationError(f"duplicate project source: {source}")
        seen_sources.add(source_key)
        display_name = str(requested_name or source.name or f"project-{index}")
        directory_name = _sanitize_name(source.name or display_name, f"project-{index}")
        candidate = directory_name
        suffix = 2
        while candidate.casefold() in seen_names:
            candidate = f"{directory_name}-{suffix}"
            suffix += 1
        directory_name = candidate
        seen_names.add(directory_name.casefold())
        project_id = f"project-{index:04d}"
        normalized.append(
            {
                "id": project_id,
                "name": display_name,
                "directory_name": directory_name,
                "source_path": os.fspath(source),
                "archive_prefix": f"projects/{project_id}",
            }
        )
    return normalized


def _json_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PortableMigrationError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _is_private_json_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.casefold())
    if compact == "keyref":
        return False
    return compact in {
        "apikey",
        "authorization",
        "authtoken",
        "bearer",
        "bearertoken",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "dpapi",
        "password",
        "refreshtoken",
        "secret",
        "secrets",
        "token",
    } or compact.endswith(("apikey", "password", "secret", "token"))


def _strip_private_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_private_json(item)
            for key, item in value.items()
            if isinstance(key, str) and not _is_private_json_key(key)
        }
    if isinstance(value, list):
        return [_strip_private_json(item) for item in value]
    return value


def _safe_public_json(path: Path) -> bytes:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_without_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortableMigrationError(f"invalid public Switchboard JSON {path}: {exc}") from exc
    return (json.dumps(_strip_private_json(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _sqlite_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _sqlite_summary(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only = ON")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
        ).fetchone()
        if table is None:
            return 0, 0
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        total = int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
        archived = (
            int(connection.execute("SELECT COUNT(*) FROM threads WHERE archived != 0").fetchone()[0])
            if "archived" in columns
            else 0
        )
        return total, archived
    except sqlite3.Error as exc:
        raise PortableMigrationError(f"cannot read task database {path}: {exc}") from exc
    finally:
        connection.close()


def _task_index(path: Path) -> list[dict[str, Any]]:
    """Read the bounded, public task metadata used by read-only pack previews."""

    connection = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only = ON")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
        ).fetchone() is None:
            return []
        available = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        columns = [column for column in _TASK_INDEX_COLUMNS if column in available]
        if "id" not in columns:
            raise PortableMigrationError("threads table has no task id column")
        count = int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
        if count > MAX_TASKS:
            raise PortableMigrationError(f"task index exceeds {MAX_TASKS} entries")
        order_column = "updated_at_ms" if "updated_at_ms" in available else (
            "updated_at" if "updated_at" in available else "id"
        )
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM threads ORDER BY {order_column} DESC"
        ).fetchall()
        return [
            {
                column: value
                for column, value in zip(columns, row)
                if value is None or isinstance(value, (str, int, float))
            }
            for row in rows
        ]
    except sqlite3.Error as exc:
        raise PortableMigrationError(f"cannot build task index from {path}: {exc}") from exc
    finally:
        connection.close()


def _plan_item(
    source: Path,
    archive_path: str,
    *,
    kind: str,
    project_id: str | None = None,
    snapshot: bool = False,
    sanitize_json: bool = False,
) -> dict[str, Any]:
    _safe_archive_parts(archive_path)
    size = len(_safe_public_json(source)) if sanitize_json else source.stat().st_size
    item: dict[str, Any] = {
        "path": archive_path,
        "source_path": os.fspath(source),
        "size": size,
        "kind": kind,
    }
    if project_id is not None:
        item["project_id"] = project_id
    if snapshot:
        item["sqlite_snapshot"] = True
    if sanitize_json:
        item["sanitize_json"] = True
    return item


def _build_export_plan(
    source_home: str | os.PathLike[str],
    projects: Iterable[Any] | Mapping[Any, Any],
    *,
    include_projection: bool,
) -> dict[str, Any]:
    home = Path(source_home).expanduser().resolve()
    if not home.is_dir() or home.is_symlink() or home.is_junction():
        raise PortableMigrationError(f"source CODEX_HOME is not a real directory: {home}")
    state_database = home / "state_5.sqlite"
    if not state_database.is_file():
        raise FileNotFoundError(state_database)
    if state_database.is_symlink() or state_database.is_junction():
        raise PortableMigrationError("state_5.sqlite cannot be a link or junction")
    projection_database = home / "thread_history_1.sqlite"
    if include_projection and not projection_database.is_file():
        raise FileNotFoundError(projection_database)
    if include_projection and (projection_database.is_symlink() or projection_database.is_junction()):
        raise PortableMigrationError("thread_history_1.sqlite cannot be a link or junction")

    project_records = _normalize_projects(projects)
    items: list[dict[str, Any]] = []
    for name in sorted(_HOME_FILES):
        source = home / name
        if name == "thread_history_1.sqlite" and not include_projection:
            continue
        if not source.is_file():
            if name in _SQLITE_FILES:
                continue
            continue
        if source.is_symlink() or source.is_junction():
            if name in _SQLITE_FILES:
                raise PortableMigrationError(f"SQLite source cannot be a link: {source}")
            continue
        items.append(
            _plan_item(
                source,
                f"home/{name}",
                kind="home",
                snapshot=name in _SQLITE_FILES,
            )
        )
    for tree_name in sorted(_HOME_TREES):
        root = home / tree_name
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            continue
        for source, relative in _iter_regular_files(root, scope="home"):
            archive_path = PurePosixPath("home", tree_name, *relative).as_posix()
            items.append(_plan_item(source, archive_path, kind="home"))
    switchboard = home / "switchboard"
    for name in sorted(_PUBLIC_SWITCHBOARD_FILES):
        source = switchboard / name
        if source.is_file() and not source.is_symlink():
            items.append(
                _plan_item(
                    source,
                    f"home/switchboard/{name}",
                    kind="home",
                    sanitize_json=True,
                )
            )
    for tree_name in sorted(_PUBLIC_SWITCHBOARD_TREES):
        root = switchboard / tree_name
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            continue
        for source, relative in _iter_regular_files(root, scope="home"):
            archive_path = PurePosixPath("home", "switchboard", tree_name, *relative).as_posix()
            items.append(
                _plan_item(
                    source,
                    archive_path,
                    kind="home",
                    sanitize_json=source.suffix.casefold() == ".json",
                )
            )

    for project in project_records:
        root = Path(project["source_path"])
        project_size = 0
        project_files = 0
        for source, relative in _iter_regular_files(root, scope="project"):
            archive_path = PurePosixPath(project["archive_prefix"], *relative).as_posix()
            item = _plan_item(
                source,
                archive_path,
                kind="project",
                project_id=project["id"],
            )
            items.append(item)
            project_size += item["size"]
            project_files += 1
        project["file_count"] = project_files
        project["size"] = project_size

    items.sort(key=lambda item: _canonical_archive_name(item["path"]))
    seen: set[str] = set()
    for item in items:
        key = _canonical_archive_name(item["path"])
        if key in seen:
            raise PortableMigrationError(f"case-insensitive path collision: {item['path']}")
        seen.add(key)
    total_size = sum(int(item["size"]) for item in items)
    if len(items) > MAX_ENTRIES or total_size > MAX_TOTAL_SIZE:
        raise PortableMigrationError("export exceeds the portable package safety limits")
    if any(int(item["size"]) > MAX_FILE_SIZE for item in items):
        raise PortableMigrationError("an export item exceeds the per-file safety limit")
    task_count, archived_task_count = _sqlite_summary(state_database)
    return {
        "schema": PACK_SCHEMA,
        "version": PACK_VERSION,
        "source_home": os.fspath(home),
        "items": items,
        "projects": project_records,
        "summary": {
            "task_count": task_count,
            "archived_task_count": archived_task_count,
            "project_count": len(project_records),
            "file_count": len(items),
            "total_size": total_size,
        },
    }


def build_export_plan(
    source_home: str | os.PathLike[str], projects: Iterable[Any] | Mapping[Any, Any] = ()
) -> dict[str, Any]:
    """Build a read-only preview that excludes the rebuildable projection."""

    return _build_export_plan(source_home, projects, include_projection=False)


def _sqlite_backup(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(_sqlite_uri(source), uri=True, timeout=30)
    destination_connection = sqlite3.connect(destination, timeout=30)
    try:
        source_connection.backup(destination_connection, pages=1000, sleep=0.05)
        destination_connection.commit()
        result = destination_connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise PortableMigrationError(f"SQLite snapshot quick_check failed: {source.name}")
    except Exception:
        destination_connection.close()
        destination.unlink(missing_ok=True)
        raise
    finally:
        source_connection.close()
        destination_connection.close()


def _delete_machine_rows(
    connection: sqlite3.Connection, tables: set[str] | None = None
) -> dict[str, int]:
    tables = tables or {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    cleared: dict[str, int] = {}
    for table in _MACHINE_TABLES:
        if table in tables:
            before = connection.total_changes
            connection.execute(f'DELETE FROM "{table}"')
            cleared[table] = connection.total_changes - before
    return cleared


def _sanitize_state_snapshot(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path, timeout=30)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
            raise PortableMigrationError("cannot make the state snapshot self-contained")
        connection.execute("PRAGMA secure_delete=ON")
        cleared = _delete_machine_rows(connection)
        connection.commit()
        if any(cleared.values()):
            connection.execute("VACUUM")
        result = connection.execute("PRAGMA quick_check").fetchall()
        if not result or any(row[0] != "ok" for row in result):
            raise PortableMigrationError("state_5.sqlite quick_check failed after sanitizing")
        return cleared
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _zip_info(name: str, source: Path | None = None) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.flag_bits |= 0x800
    mode = 0o100600
    if source is not None:
        try:
            mode = stat.S_IFREG | stat.S_IMODE(source.stat().st_mode)
        except OSError:
            pass
    info.external_attr = mode << 16
    return info


def _copy_and_hash(source: BinaryIO, destination: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        destination.write(chunk)
        digest.update(chunk)
        size += len(chunk)
        if size > MAX_FILE_SIZE:
            raise PortableMigrationError("an item exceeds the per-file safety limit")
    return size, digest.hexdigest()


def _write_payload(
    archive: zipfile.ZipFile,
    archive_path: str,
    source: Path,
    *,
    data: bytes | None = None,
) -> tuple[int, str]:
    info = _zip_info(archive_path, source)
    with archive.open(info, "w", force_zip64=True) as destination:
        if data is None:
            with source.open("rb") as handle:
                return _copy_and_hash(handle, destination)
        digest = hashlib.sha256(data).hexdigest()
        destination.write(data)
        return len(data), digest


def _validate_rollout_ordinals(path: Path) -> None:
    """Reject a modern rollout whose canonical ordinal stream is unsafe."""

    previous: int | None = None
    previous_record: dict[str, Any] | None = None
    modern = False
    try:
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, 1):
                try:
                    item = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise PortableMigrationError(
                        f"rollout history integrity failed at line {line_number}"
                    ) from exc
                record = item if isinstance(item, dict) else None
                ordinal = record.get("ordinal") if record is not None else None
                if line_number == 1 and ordinal is None:
                    return  # Legacy non-paginated rollout.
                modern = True
                if isinstance(ordinal, bool) or not isinstance(ordinal, int):
                    raise PortableMigrationError(
                        f"rollout history integrity failed at line {line_number}"
                    )
                if previous is not None and ordinal != previous + 1:
                    expected = previous + 1
                    if not (
                        ordinal < expected
                        and is_benign_metadata_duplicate(
                            previous_record, record, expected
                        )
                    ):
                        raise PortableMigrationError(
                            f"rollout history integrity failed at line {line_number}"
                        )
                else:
                    previous = ordinal
                previous_record = record
    except OSError as exc:
        raise PortableMigrationError("rollout history integrity check failed") from exc
    if modern and previous is None:
        raise PortableMigrationError("rollout history integrity check failed")


def create_pack(
    source_home: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    projects: Iterable[Any] | Mapping[Any, Any] = (),
    include_projection: bool = False,
    progress: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Create and verify one atomic ZIP64 ``.codexpack`` file."""

    destination_path = Path(destination).expanduser().absolute()
    if destination_path.exists() or destination_path.is_symlink():
        raise FileExistsError(f"package destination already exists: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    _emit(progress, "planning")
    plan = _build_export_plan(source_home, projects, include_projection=include_projection)
    rollout_items = [
        item
        for item in plan["items"]
        if (
            item["path"].startswith(("home/sessions/", "home/archived_sessions/"))
            and Path(item["path"]).name.startswith("rollout-")
            and item["path"].endswith(".jsonl")
        )
    ]
    for index, item in enumerate(rollout_items, 1):
        _emit(progress, "integrity", index, len(rollout_items), item["path"])
        _validate_rollout_ordinals(Path(item["source_path"]))
    temp_pack = destination_path.parent / f".{destination_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with tempfile.TemporaryDirectory(
            prefix="codexpack-snapshots-", dir=destination_path.parent
        ) as snapshot_directory:
            snapshots: dict[str, Path] = {}
            database_items = [item for item in plan["items"] if item.get("sqlite_snapshot")]
            for index, item in enumerate(database_items, 1):
                _emit(progress, "snapshot", index, len(database_items), item["path"])
                snapshot = Path(snapshot_directory) / Path(item["path"]).name
                _sqlite_backup(Path(item["source_path"]), snapshot)
                if item["path"] == "home/state_5.sqlite":
                    _sanitize_state_snapshot(snapshot)
                snapshots[item["path"]] = snapshot

            manifest_items: list[dict[str, Any]] = []
            total = len(plan["items"])
            with zipfile.ZipFile(
                temp_pack,
                "x",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as archive:
                for index, item in enumerate(plan["items"], 1):
                    _emit(progress, "archive", index, total, item["path"])
                    source = snapshots.get(item["path"], Path(item["source_path"]))
                    data = _safe_public_json(source) if item.get("sanitize_json") else None
                    size, digest = _write_payload(archive, item["path"], source, data=data)
                    record: dict[str, Any] = {
                        "path": item["path"],
                        "kind": item["kind"],
                        "size": size,
                        "sha256": digest,
                    }
                    if "project_id" in item:
                        record["project_id"] = item["project_id"]
                    if item.get("sqlite_snapshot"):
                        record["sqlite_snapshot"] = True
                    manifest_items.append(record)

                snapshot_state = snapshots["home/state_5.sqlite"]
                task_index = _task_index(snapshot_state)
                task_count = len(task_index)
                archived_task_count = sum(
                    1 for task in task_index if bool(task.get("archived", False))
                )
                summary = {
                    "task_count": task_count,
                    "archived_task_count": archived_task_count,
                    "project_count": len(plan["projects"]),
                    "file_count": len(manifest_items),
                    "total_size": sum(item["size"] for item in manifest_items),
                }
                projects_manifest = []
                for project in plan["projects"]:
                    project_items = [
                        item for item in manifest_items if item.get("project_id") == project["id"]
                    ]
                    projects_manifest.append(
                        {
                            **project,
                            "file_count": len(project_items),
                            "size": sum(item["size"] for item in project_items),
                        }
                    )
                manifest = {
                    "schema": PACK_SCHEMA,
                    "version": PACK_VERSION,
                    "created_at": _utc_now(),
                    "source": {"codex_home": plan["source_home"]},
                    "task_index": task_index,
                    "items": manifest_items,
                    "projects": projects_manifest,
                    "summary": summary,
                }
                manifest_bytes = (
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
                if len(manifest_bytes) > MAX_MANIFEST_SIZE:
                    raise PortableMigrationError("manifest exceeds the safety limit")
                archive.writestr(_zip_info(MANIFEST_NAME), manifest_bytes)

        with temp_pack.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        _emit(progress, "verify")
        report = inspect_pack(temp_pack, verify=True)
        if destination_path.exists() or destination_path.is_symlink():
            raise FileExistsError(f"package destination already exists: {destination_path}")
        os.rename(temp_pack, destination_path)
        report.update(
            {
                "status": "created",
                "package": os.fspath(destination_path),
                "path": os.fspath(destination_path),
                "pack_size": destination_path.stat().st_size,
            }
        )
        _emit(progress, "complete", report["file_count"], report["file_count"])
        return report
    finally:
        temp_pack.unlink(missing_ok=True)


def _validate_zip_entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES + 1:
        raise PortableMigrationError("package has too many entries")
    by_name: dict[str, zipfile.ZipInfo] = {}
    canonical_names: set[str] = set()
    total_size = 0
    for info in infos:
        _safe_archive_parts(info.filename)
        canonical = _canonical_archive_name(info.filename)
        if info.filename in by_name or canonical in canonical_names:
            raise PortableMigrationError(f"duplicate archive entry: {info.filename}")
        if info.is_dir():
            raise PortableMigrationError(f"directory entries are not allowed: {info.filename}")
        if info.flag_bits & 0x1:
            raise PortableMigrationError("encrypted ZIP entries are not supported")
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise PortableMigrationError(f"symbolic-link entry is not allowed: {info.filename}")
        if info.file_size < 0 or info.file_size > MAX_FILE_SIZE:
            raise PortableMigrationError(f"oversized archive entry: {info.filename}")
        total_size += info.file_size
        if total_size > MAX_TOTAL_SIZE:
            raise PortableMigrationError("package exceeds the total size limit")
        if (
            info.file_size > 1024 * 1024
            and info.compress_size > 0
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise PortableMigrationError(f"suspicious compression ratio: {info.filename}")
        by_name[info.filename] = info
        canonical_names.add(canonical)
    return by_name


def _read_manifest(archive: zipfile.ZipFile, entries: Mapping[str, zipfile.ZipInfo]) -> dict[str, Any]:
    info = entries.get(MANIFEST_NAME)
    if info is None:
        raise PortableMigrationError("package has no manifest.json")
    if info.file_size > MAX_MANIFEST_SIZE:
        raise PortableMigrationError("manifest exceeds the safety limit")
    try:
        raw = archive.read(info)
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_without_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise PortableMigrationError(f"invalid package manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PortableMigrationError("manifest must be a JSON object")
    if manifest.get("schema") != PACK_SCHEMA or manifest.get("version") != PACK_VERSION:
        raise PortableMigrationError("unsupported package schema or version")
    return manifest


def _validate_manifest(
    manifest: dict[str, Any], entries: Mapping[str, zipfile.ZipInfo]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    raw_items = manifest.get("items")
    raw_projects = manifest.get("projects")
    raw_tasks = manifest.get("task_index")
    if not all(isinstance(value, list) for value in (raw_items, raw_projects, raw_tasks)):
        raise PortableMigrationError("manifest items/projects/task_index must be lists")
    if len(raw_tasks) > MAX_TASKS:
        raise PortableMigrationError("task index exceeds the safety limit")
    tasks: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    allowed_task_keys = set(_TASK_INDEX_COLUMNS)
    for value in raw_tasks:
        if not isinstance(value, dict) or not set(value).issubset(allowed_task_keys):
            raise PortableMigrationError("invalid task index entry")
        task_id = value.get("id")
        if not isinstance(task_id, str) or not task_id or task_id in task_ids:
            raise PortableMigrationError("invalid or duplicate task id in task index")
        if any(
            item is not None and not isinstance(item, (str, int, float))
            for item in value.values()
        ):
            raise PortableMigrationError("invalid task index value")
        task_ids.add(task_id)
        tasks.append(value)
    projects: list[dict[str, Any]] = []
    project_ids: set[str] = set()
    project_prefixes: set[str] = set()
    directory_names: set[str] = set()
    for value in raw_projects:
        if not isinstance(value, dict):
            raise PortableMigrationError("invalid project manifest entry")
        project_id = value.get("id")
        prefix = value.get("archive_prefix")
        directory_name = value.get("directory_name")
        if not all(isinstance(item, str) and item for item in (project_id, prefix, directory_name)):
            raise PortableMigrationError("project identity fields are missing")
        if prefix != f"projects/{project_id}" or len(_safe_archive_parts(prefix)) != 2:
            raise PortableMigrationError("invalid project archive prefix")
        _safe_archive_parts(f"projects/{project_id}/{directory_name}")
        if "/" in directory_name or "\\" in directory_name:
            raise PortableMigrationError("invalid project directory name")
        if project_id in project_ids or _canonical_archive_name(prefix) in project_prefixes:
            raise PortableMigrationError("duplicate project identity")
        if directory_name.casefold() in directory_names:
            raise PortableMigrationError("duplicate project directory name")
        project_ids.add(project_id)
        project_prefixes.add(_canonical_archive_name(prefix))
        directory_names.add(directory_name.casefold())
        projects.append(value)

    items: list[dict[str, Any]] = []
    item_names: set[str] = set()
    for value in raw_items:
        if not isinstance(value, dict):
            raise PortableMigrationError("invalid manifest item")
        path = value.get("path")
        size = value.get("size")
        digest = value.get("sha256")
        kind = value.get("kind")
        if not isinstance(path, str):
            raise PortableMigrationError("manifest item has no path")
        parts = _safe_archive_parts(path)
        canonical = _canonical_archive_name(path)
        if canonical in item_names:
            raise PortableMigrationError(f"duplicate manifest item: {path}")
        item_names.add(canonical)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_FILE_SIZE:
            raise PortableMigrationError(f"invalid size for {path}")
        if not isinstance(digest, str) or _HEX_SHA256.fullmatch(digest) is None:
            raise PortableMigrationError(f"invalid SHA-256 for {path}")
        info = entries.get(path)
        if info is None or info.file_size != size:
            raise PortableMigrationError(f"manifest/ZIP size mismatch for {path}")
        if kind == "home":
            if parts[0] != "home" or not _is_allowed_home_relative(parts[1:]):
                raise PortableMigrationError(f"disallowed CODEX_HOME payload: {path}")
        elif kind == "project":
            project_id = value.get("project_id")
            if project_id not in project_ids or not path.startswith(f"projects/{project_id}/"):
                raise PortableMigrationError(f"invalid project payload: {path}")
            if _is_excluded(parts[2:], scope="project", is_dir=False):
                raise PortableMigrationError(f"disallowed project payload: {path}")
        else:
            raise PortableMigrationError(f"invalid payload kind for {path}")
        items.append(value)

    expected_names = {MANIFEST_NAME, *(item["path"] for item in items)}
    if set(entries) != expected_names:
        extras = sorted(set(entries) - expected_names)
        missing = sorted(expected_names - set(entries))
        raise PortableMigrationError(f"manifest payload mismatch; extra={extras[:3]}, missing={missing[:3]}")
    if "home/state_5.sqlite" not in expected_names:
        raise PortableMigrationError("package has no state_5.sqlite snapshot")
    if len(items) > MAX_ENTRIES or sum(item["size"] for item in items) > MAX_TOTAL_SIZE:
        raise PortableMigrationError("manifest exceeds the safety limits")
    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        raise PortableMigrationError("manifest has no summary")
    task_count = summary.get("task_count")
    archived_task_count = summary.get("archived_task_count")
    if (
        not isinstance(task_count, int)
        or isinstance(task_count, bool)
        or task_count != len(tasks)
        or not isinstance(archived_task_count, int)
        or isinstance(archived_task_count, bool)
        or archived_task_count < 0
    ):
        raise PortableMigrationError("task index does not match the manifest summary")
    if all("archived" in task for task in tasks) and archived_task_count != sum(
        1 for task in tasks if bool(task["archived"])
    ):
        raise PortableMigrationError("archived task count does not match the task index")
    return items, projects, tasks


def inspect_pack(pack: str | os.PathLike[str], verify: bool = True) -> dict[str, Any]:
    """Inspect a package without extracting or changing it."""

    package = Path(pack).expanduser().absolute()
    if not package.is_file():
        raise FileNotFoundError(package)
    try:
        with zipfile.ZipFile(package, "r", allowZip64=True) as archive:
            entries = _validate_zip_entries(archive)
            manifest = _read_manifest(archive, entries)
            items, projects, tasks = _validate_manifest(manifest, entries)
            if verify:
                for item in items:
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(entries[item["path"]], "r") as handle:
                        while True:
                            chunk = handle.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                            size += len(chunk)
                    if size != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise PortableMigrationError(f"payload verification failed: {item['path']}")
    except (zipfile.BadZipFile, OSError) as exc:
        if isinstance(exc, PortableMigrationError):
            raise
        raise PortableMigrationError(f"invalid or damaged package: {exc}") from exc

    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    total_size = sum(item["size"] for item in items)
    return {
        "status": "valid" if verify else "parsed",
        "path": os.fspath(package),
        "package": os.fspath(package),
        "schema": manifest["schema"],
        "version": manifest["version"],
        "verified": bool(verify),
        "task_count": int(summary.get("task_count", 0)),
        "archived_task_count": int(summary.get("archived_task_count", 0)),
        "project_count": len(projects),
        "file_count": len(items),
        "total_size": total_size,
        "pack_size": package.stat().st_size,
        "tasks": tasks,
        "projects": projects,
        "manifest": manifest,
    }


def _empty_target(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(path.resolve(strict=False)))


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        common = os.path.commonpath([_path_key(first), _path_key(second)])
    except ValueError:
        return False
    return common in {_path_key(first), _path_key(second)}


def _resolve_project_destinations(
    projects: list[dict[str, Any]],
    target_home: Path,
    project_root: str | os.PathLike[str] | None,
    project_destinations: Mapping[Any, Any] | None,
) -> dict[str, Path]:
    explicit = {os.fspath(key): Path(os.fspath(value)).expanduser().absolute() for key, value in (project_destinations or {}).items()}
    root = Path(project_root).expanduser().absolute() if project_root is not None else None
    destinations: dict[str, Path] = {}
    used_keys: set[str] = set()
    for project in projects:
        aliases = (
            project["id"],
            str(project.get("source_path", "")),
            str(project.get("name", "")),
            project["directory_name"],
        )
        matches = [alias for alias in aliases if alias in explicit]
        if len(matches) > 1 and len({_path_key(explicit[key]) for key in matches}) > 1:
            raise PortableMigrationError(f"conflicting destinations for {project['id']}")
        if matches:
            key = matches[0]
            destination = explicit[key]
            used_keys.update(matches)
        elif root is not None:
            destination = root / project["directory_name"]
        else:
            raise PortableMigrationError(f"no destination selected for {project['name']}")
        destinations[project["id"]] = destination
    unknown = set(explicit) - used_keys
    if unknown:
        raise PortableMigrationError(f"unknown project destination keys: {sorted(unknown)!r}")

    paths = [target_home, *destinations.values()]
    for index, first in enumerate(paths):
        for second in paths[index + 1 :]:
            if _paths_overlap(first, second):
                raise PortableMigrationError(f"migration destinations overlap: {first} and {second}")
    for destination in destinations.values():
        if destination.exists():
            raise FileExistsError(f"project destination already exists: {destination}")
    return destinations


def _extract_item(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    expected_size: int,
    expected_digest: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PortableMigrationError(f"duplicate extraction target: {destination}")
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as source, destination.open("xb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            if size > expected_size:
                raise PortableMigrationError(f"extracted size mismatch: {info.filename}")
    if size != expected_size or digest.hexdigest() != expected_digest:
        destination.unlink(missing_ok=True)
        raise PortableMigrationError(f"extracted payload verification failed: {info.filename}")


def _quick_check(path: Path) -> None:
    try:
        connection = sqlite3.connect(path, timeout=10)
        try:
            rows = connection.execute("PRAGMA quick_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PortableMigrationError(f"SQLite quick_check failed for {path.name}: {exc}") from exc
    if not rows or any(row[0] != "ok" for row in rows):
        raise PortableMigrationError(f"SQLite quick_check failed for {path.name}")


def _normalized_path(value: str) -> tuple[str, str]:
    text = value.strip()
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\") or "\\" in text:
        return "windows", ntpath.normcase(ntpath.normpath(text))
    return "posix", posixpath.normcase(posixpath.normpath(text))


def _replace_path_prefix(value: Any, mappings: list[tuple[str, Path]]) -> Any:
    if not isinstance(value, str) or not value.strip():
        return value
    style, normalized = _normalized_path(value)
    candidates: list[tuple[int, str, Path]] = []
    for source, destination in mappings:
        source_style, source_normalized = _normalized_path(source)
        if source_style != style:
            continue
        module = ntpath if style == "windows" else posixpath
        try:
            if module.commonpath([normalized, source_normalized]) != source_normalized:
                continue
        except ValueError:
            continue
        candidates.append((len(source_normalized), source_normalized, destination))
    if not candidates:
        return value
    _, source_normalized, destination = max(candidates, key=lambda item: item[0])
    module = ntpath if style == "windows" else posixpath
    relative = module.relpath(normalized, source_normalized)
    if relative == ".":
        return os.fspath(destination)
    return module.normpath(module.join(os.fspath(destination), relative))


def _rewrite_state_database(
    database: Path,
    old_home: str,
    target_home: Path,
    projects: list[dict[str, Any]],
    destinations: Mapping[str, Path],
) -> tuple[dict[str, int], dict[str, int]]:
    connection = sqlite3.connect(database, timeout=30)
    rewrites = {"rollout_path": 0, "cwd": 0, "project_roots.path": 0}
    cleared: dict[str, int] = {}
    try:
        connection.execute("PRAGMA secure_delete=ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        project_mappings = [
            (str(project.get("source_path", "")), destinations[project["id"]])
            for project in projects
        ]
        if "threads" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            selected = [name for name in ("rollout_path", "cwd") if name in columns]
            if selected:
                rows = connection.execute(
                    f"SELECT rowid, {', '.join(selected)} FROM threads"
                ).fetchall()
                for row in rows:
                    values = dict(zip(selected, row[1:]))
                    updates: dict[str, Any] = {}
                    if "rollout_path" in values:
                        updated = _replace_path_prefix(
                            values["rollout_path"], [(old_home, target_home)]
                        )
                        if updated != values["rollout_path"]:
                            updates["rollout_path"] = updated
                            rewrites["rollout_path"] += 1
                    if "cwd" in values:
                        updated = _replace_path_prefix(
                            values["cwd"], [*project_mappings, (old_home, target_home)]
                        )
                        if updated != values["cwd"]:
                            updates["cwd"] = updated
                            rewrites["cwd"] += 1
                    if updates:
                        assignments = ", ".join(f'"{key}" = ?' for key in updates)
                        connection.execute(
                            f"UPDATE threads SET {assignments} WHERE rowid = ?",
                            (*updates.values(), row[0]),
                        )
        for table, column in _PROJECT_PATH_COLUMNS:
            if table not in tables:
                continue
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                continue
            for rowid, value in connection.execute(
                f'SELECT rowid, "{column}" FROM "{table}"'
            ).fetchall():
                updated = _replace_path_prefix(
                    value, [*project_mappings, (old_home, target_home)]
                )
                if updated != value:
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                        (updated, rowid),
                    )
                    rewrites[f"{table}.{column}"] += 1
        cleared.update(_delete_machine_rows(connection, tables))
        connection.commit()
        if any(cleared.values()):
            connection.execute("VACUUM")
        result = connection.execute("PRAGMA quick_check").fetchall()
        if not result or any(row[0] != "ok" for row in result):
            raise PortableMigrationError("state_5.sqlite quick_check failed after rewrite")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return rewrites, cleared


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def import_pack(
    pack: str | os.PathLike[str],
    target_home: str | os.PathLike[str],
    project_root: str | os.PathLike[str] | None = None,
    project_destinations: Mapping[Any, Any] | None = None,
    progress: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Verify and restore a package without activating the restored home."""

    package = Path(pack).expanduser().absolute()
    target = Path(target_home).expanduser().absolute()
    if target.is_symlink() or (target.exists() and target.is_junction()):
        raise PortableMigrationError("target CODEX_HOME cannot be a link or junction")
    if target.exists() and not _empty_target(target):
        raise FileExistsError(f"target CODEX_HOME is not empty: {target}")
    _emit(progress, "inspect")
    inspection = inspect_pack(package, verify=True)
    manifest = inspection["manifest"]
    projects = inspection["projects"]
    destinations = _resolve_project_destinations(
        projects, target, project_root, project_destinations
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    home_staging_root = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.import-", dir=target.parent)
    )
    staged_home = home_staging_root / "home"
    staged_home.mkdir()
    project_staging: dict[str, Path] = {}
    published_projects: list[tuple[Path, Path]] = []
    original_empty: Path | None = None
    home_published = False
    try:
        for project in projects:
            destination = destinations[project["id"]]
            destination.parent.mkdir(parents=True, exist_ok=True)
            project_staging[project["id"]] = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.import-", dir=destination.parent
                )
            )

        with zipfile.ZipFile(package, "r", allowZip64=True) as archive:
            entries = _validate_zip_entries(archive)
            current_manifest = _read_manifest(archive, entries)
            items, current_projects, _current_tasks = _validate_manifest(current_manifest, entries)
            if current_manifest != manifest or current_projects != projects:
                raise PortableMigrationError("package changed during import")
            for index, item in enumerate(items, 1):
                _emit(progress, "extract", index, len(items), item["path"])
                parts = _safe_archive_parts(item["path"])
                if item["kind"] == "home":
                    destination = staged_home.joinpath(*parts[1:])
                else:
                    destination = project_staging[item["project_id"]].joinpath(*parts[2:])
                _extract_item(
                    archive,
                    entries[item["path"]],
                    destination,
                    item["size"],
                    item["sha256"],
                )

        _emit(progress, "database_check")
        state_database = staged_home / "state_5.sqlite"
        _quick_check(state_database)
        projection_database = staged_home / "thread_history_1.sqlite"
        if projection_database.exists():
            _quick_check(projection_database)
        old_home = str(manifest.get("source", {}).get("codex_home", ""))
        if not old_home:
            raise PortableMigrationError("manifest has no source CODEX_HOME")
        rewrites, cleared = _rewrite_state_database(
            state_database, old_home, target, projects, destinations
        )

        _emit(progress, "publish")
        for project in projects:
            stage = project_staging[project["id"]]
            destination = destinations[project["id"]]
            os.replace(stage, destination)
            published_projects.append((destination, stage))
        if target.exists():
            original_empty = target.parent / f".{target.name}.empty-{uuid.uuid4().hex}"
            os.replace(target, original_empty)
        os.replace(staged_home, target)
        home_published = True
        if original_empty is not None:
            original_empty.rmdir()
            original_empty = None
    except Exception as exc:
        rollback_errors: list[str] = []
        if home_published and target.exists():
            try:
                os.replace(target, staged_home)
                home_published = False
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if original_empty is not None and original_empty.exists() and not target.exists():
            try:
                os.replace(original_empty, target)
                original_empty = None
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for destination, stage in reversed(published_projects):
            if destination.exists() and not stage.exists():
                try:
                    os.replace(destination, stage)
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise RuntimeError(
                f"import failed and rollback was incomplete: {rollback_errors}"
            ) from exc
        raise
    finally:
        _remove_tree(home_staging_root)
        for stage in project_staging.values():
            _remove_tree(stage)

    report = {
        **{key: inspection[key] for key in (
            "task_count",
            "archived_task_count",
            "project_count",
            "file_count",
            "total_size",
            "pack_size",
        )},
        "status": "imported",
        "verified": True,
        "package": os.fspath(package),
        "target_home": os.fspath(target),
        "project_destinations": {
            project_id: os.fspath(destination)
            for project_id, destination in destinations.items()
        },
        "path_rewrites": rewrites,
        "machine_bindings_cleared": cleared,
        "activated": False,
    }
    _emit(progress, "complete", report["file_count"], report["file_count"])
    return report


__all__ = [
    "PACK_SCHEMA",
    "PACK_VERSION",
    "PortableMigrationError",
    "build_export_plan",
    "create_pack",
    "inspect_pack",
    "import_pack",
]
