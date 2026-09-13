"""Machine-readable offline acceptance; defaults to the new recovery surface."""
import argparse
import contextlib
import io
import json
import time
import unittest
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Run the complete existing offline suite")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    loader = unittest.TestLoader()
    if args.full:
        suite = loader.discover(str(root / "tests"))
    else:
        suite = loader.loadTestsFromNames([
            "tests.test_projection_recovery", "tests.test_independent_worker",
            "tests.test_recovery_jobs", "tests.test_recovery_ui", "tests.test_recovery_controller"])
    output = io.StringIO()
    started = time.monotonic()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
    report = {"scope": "full_offline" if args.full else "recovery_offline",
              "successful": result.wasSuccessful(), "tests": result.testsRun,
              "passed": result.testsRun - len(result.errors) - len(result.failures) - len(result.skipped),
              "seconds": round(time.monotonic() - started, 3),
              "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
              "failures": [{"test": test.id(), "trace": trace} for test, trace in result.failures],
              "errors": [{"test": test.id(), "trace": trace} for test, trace in result.errors],
              "source_root": str(root)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
