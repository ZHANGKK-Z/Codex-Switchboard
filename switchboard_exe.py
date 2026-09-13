"""Minimal frozen entrypoint that keeps the router child free of Qt startup."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _router_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--router-process", action="store_true")
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)
    import switchboard

    switchboard.run_router(switchboard.Paths(args.home.expanduser().resolve()))
    return 0


def main() -> int:
    if "--independent-worker" in sys.argv[1:]:
        import independent_worker

        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--independent-worker", required=True)
        args = parser.parse_args(sys.argv[1:])
        return independent_worker.worker_main(args.independent_worker)
    if "--recovery-worker" in sys.argv[1:]:
        import recovery_jobs

        return recovery_jobs.main([arg for arg in sys.argv[1:] if arg != "--recovery-worker"])
    if "--handoff-worker" in sys.argv[1:]:
        import handoff_jobs

        return handoff_jobs.main([arg for arg in sys.argv[1:] if arg != "--handoff-worker"])
    if "--router-process" in sys.argv[1:]:
        return _router_main(sys.argv[1:])
    import switchboard_modern_ui

    return switchboard_modern_ui.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
