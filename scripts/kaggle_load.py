#!/usr/bin/env python3
"""Download a Kaggle Reddit corpus and ingest it into the raw archive.

  python3 scripts/kaggle_load.py mattpodolak/reddit-wallstreetbets-comments

Credentials come from KAGGLE_USERNAME and KAGGLE_KEY in the environment.
Get them from kaggle.com -> Settings -> API -> Create New Token.

Each dataset lands under its own path (`kaggle/<owner>/<slug>`) and every record
is tagged with its source. That is deliberate: these corpora were assembled by
different people with different completeness, so a jump in mention volume at the
boundary between two of them is an artefact rather than an event. Combining them
should be a decision an analysis makes explicitly, not a side effect of loading.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.kaggle_source import (
    MissingCredentials,
    SchemaError,
    download_dataset,
    ingest_csv,
)
from reddit_alpha.storage import RawStore


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref", help="Kaggle dataset ref, e.g. owner/dataset-slug")
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--download-only", action="store_true")
    ap.add_argument("--skip-download", action="store_true",
                    help="ingest files already downloaded")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    raw_dir = args.data_dir / "kaggle_downloads" / args.ref.replace("/", "__")

    if not args.skip_download:
        try:
            download_dataset(args.ref, raw_dir)
        except MissingCredentials as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return 2

    csv_files = sorted(raw_dir.rglob("*.csv"))
    other = [p for p in raw_dir.rglob("*") if p.is_file() and p.suffix not in (".csv", ".zip")]
    if other:
        # Named rather than skipped: a parquet file quietly ignored looks
        # identical to a dataset that simply had less in it.
        print(f"\nNote: {len(other)} non-CSV file(s) not ingested: "
              f"{[p.name for p in other[:5]]}")

    if not csv_files:
        print(f"No CSV files found under {raw_dir}", file=sys.stderr)
        return 1
    if args.download_only:
        print(f"Downloaded {len(csv_files)} CSV file(s) to {raw_dir}")
        return 0

    store = RawStore(args.data_dir / "raw")
    reports = []
    for path in csv_files:
        size_mb = path.stat().st_size / 1e6
        print(f"\n--- {path.name} ({size_mb:.0f} MB)")
        try:
            report = ingest_csv(path, store, args.ref)
        except SchemaError as exc:
            print(f"  SKIPPED: {exc}", file=sys.stderr)
            continue
        reports.append(report.as_dict())
        r = report.as_dict()
        print(f"  columns : {r['column_mapping']}")
        print(f"  read    : {r['rows_read']:,}")
        print(f"  written : {r['rows_written']:,}  ({r['kept_share']:.1%} kept)")
        print(f"  dropped : {r['bad_timestamps']:,} bad timestamps, "
              f"{r['missing_text']:,} empty, {r['withdrawn']:,} withdrawn, "
              f"{r['duplicates']:,} duplicate")

    out = args.data_dir / "kaggle_ingest_report.json"
    existing = json.loads(out.read_text()) if out.exists() else []
    out.write_text(json.dumps(existing + reports, indent=2))
    print(f"\nreport -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
