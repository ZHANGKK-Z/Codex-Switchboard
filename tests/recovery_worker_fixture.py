"""Bounded synthetic-database worker; not a production CLI or exit detector."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery_jobs
import switchboard


def main():
    home = Path(sys.argv[1]).resolve(strict=True)
    marker = home / "SYNTHETIC_RECOVERY_FIXTURE.json"
    if json.loads(marker.read_text(encoding="utf-8")) != {"synthetic": True, "operation": sys.argv[2]}:
        return 2
    # Let the real launcher verify this business process before natural exit.
    time.sleep(1)
    result = recovery_jobs.run_worker(switchboard.Paths(home), sys.argv[2],
                                     process_probe=lambda _paths: [], poll_seconds=0.1,
                                     timeout_seconds=10)
    return 0 if result["status"] == "pending_native_replay" else 1


if __name__ == "__main__":
    raise SystemExit(main())
