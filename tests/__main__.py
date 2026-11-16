"""Run every test suite: `python3 -m tests` from the repository root.

The suites are selftest.py (the core) and every tests/test_*.py.  Options are
passed to the core self-test (`-v`, `-k substring`, `--quick`); with `-k` the
other suites are skipped.
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv):
    suites = ["selftest.py"] + sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, "test_*.py")))
    failed = []
    for suite in suites:
        if suite != "selftest.py" and "-k" in argv:
            continue
        print(f"== {suite}", flush=True)
        args = argv if suite == "selftest.py" else []
        if subprocess.run([sys.executable, os.path.join(HERE, suite), *args]).returncode:
            failed.append(suite)
    print("all suites passed" if not failed else f"FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
