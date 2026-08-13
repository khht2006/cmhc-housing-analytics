"""
Run the whole thing, start to finish.

Run:  python run_all.py

Steps, in order:
    1. extract.py   download the data
    2. build.py     build the star schema
    3. check.py     check our numbers against the published ones
    4. export.py    write Parquet files for Power BI

The important bit is the ORDER of steps 3 and 4. If the checks fail, we stop
and DON'T export. That means Power BI keeps showing last month's numbers, which
we know were correct, instead of picking up new numbers we can't vouch for.

A dashboard that's quietly wrong is worse than one that's obviously out of
date, because people believe it and make decisions on it.

To re-run without downloading again (much faster while you're experimenting):
    python run_all.py --skip-download
"""

import sys
import time

import build
import check
import export
import extract


def main():
    skip_download = "--skip-download" in sys.argv
    started = time.time()

    if skip_download:
        print("Skipping download, using the files already in data/raw/\n")
    else:
        extract.main()
        print()

    build.main()
    print()

    results = check.run_all_checks()
    failures = int((~results.close_enough & ~results.known_problem).sum())
    print()

    if failures:
        print("=" * 70)
        print(f"STOPPING: {failures} checks failed.")
        print("Not exporting, so Power BI keeps the last set of numbers we trust.")
        print("Look at the FAILED lines above to see what went wrong.")
        print("=" * 70)
        return 1

    export.main()

    print(f"\nAll done in {time.time() - started:.0f} seconds.")
    print("Next: run 'python explore.py' to see what the data says,")
    print("      or open Power BI and follow powerbi/setup.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
