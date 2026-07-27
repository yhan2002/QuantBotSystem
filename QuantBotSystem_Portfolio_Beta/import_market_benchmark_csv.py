"""Import a locally obtained MSCI ACWI Date/Close CSV into the shared cache."""

import argparse
from pathlib import Path

import pandas as pd

from core_market_data_store import upsert_close_series


MARKET_BENCHMARK_TICKER = "MIWD00000PUS"


def import_market_benchmark_csv(csv_path):
    """Read one local CSV and import it without any download fallback."""
    path = Path(csv_path).expanduser()
    frame = pd.read_csv(path)
    return upsert_close_series(MARKET_BENCHMARK_TICKER, frame)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Import a locally obtained Date/Close MSCI ACWI CSV. "
            "No data is downloaded."
        )
    )
    parser.add_argument("csv_path", help="Path to the local Date/Close CSV file")
    args = parser.parse_args(argv)

    summary = import_market_benchmark_csv(args.csv_path)
    print(
        f"Imported {summary['updated']} rows for {summary['ticker']} "
        f"({summary['first_date']} to {summary['latest_date']}) into "
        f"{summary['db_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
