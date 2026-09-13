"""Harmless, bounded real-Windows fixtures; never touch Codex or Providers."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import independent_worker as worker


def write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    mode, root = sys.argv[1], Path(sys.argv[2])
    if mode == "launch":
        result = worker.launch_independent(
            [sys.executable, str(Path(__file__).resolve()), "child", str(root), *sys.argv[3:]], root)
        write(root / "launch.json", {**result, "launcherPid": os.getpid()})
        return 0
    if mode == "child":
        identity = worker.require_current_independent()
        write(root / "child.json", {**identity, "args": sys.argv[3:], "prefix": sys.prefix,
                                    "executable": sys.executable})
        # Stay alive after the short-lived launcher exits, then finish naturally.
        time.sleep(2.0)
        write(root / "completed.json", {"completed": True, **worker.require_current_independent()})
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
