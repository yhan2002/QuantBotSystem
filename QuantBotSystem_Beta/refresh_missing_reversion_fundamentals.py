# File name: refresh_missing_reversion_fundamentals.py
# Downloads only missing fundamentals needed by the value reversion strategy.
import os
import sys
import time
from datetime import datetime

import config
import value_reversion_engine


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SLEEP_SECONDS = 0.5


class TeeOutput:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


def _cached_fundamental_tickers():
    conn = value_reversion_engine._get_connection()
    try:
        rows = conn.execute(
            """
            SELECT ticker
            FROM fundamentals
            WHERE shares_outstanding > 0 AND total_equity > 0
            """
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def refresh_missing():
    universe = sorted(set(config.universe))
    cached = _cached_fundamental_tickers()
    missing = [ticker for ticker in universe if ticker not in cached]

    print("Refresh Missing Value Reversion Fundamentals")
    print("========================================")
    print(f"Universe: {len(universe)} tickers")
    print(f"Already cached: {len(cached & set(universe))} tickers")
    print(f"Missing before refresh: {len(missing)} tickers")
    if missing:
        print(f"Missing sample: {', '.join(missing[:30])}")
    print("")

    if not missing:
        print("Nothing to download.")
        return

    updated = []
    failed = []
    for idx, ticker in enumerate(missing, start=1):
        try:
            data = value_reversion_engine.get_fundamentals(
                ticker,
                allow_download=True,
            )
            if data:
                updated.append(ticker)
                status = "OK"
            else:
                failed.append(ticker)
                status = "FAILED"
        except Exception as exc:
            failed.append(ticker)
            status = f"ERROR: {exc}"

        print(f"{idx:>3}/{len(missing)} {ticker:<10} {status}")

        if SLEEP_SECONDS and idx < len(missing):
            time.sleep(SLEEP_SECONDS)

    refreshed_cached = _cached_fundamental_tickers()
    still_missing = [ticker for ticker in universe if ticker not in refreshed_cached]

    print("")
    print("Summary")
    print("----------------------------------------")
    print(f"Downloaded: {len(updated)} tickers")
    print(f"Failed this run: {len(failed)} tickers")
    print(f"Missing after refresh: {len(still_missing)} tickers")
    if still_missing:
        print(f"Still missing sample: {', '.join(still_missing[:40])}")
    print("")
    print("DB updated in place. You can rerun the value-reversion scan now.")


if __name__ == "__main__":
    output_path = os.path.join(
        BASE_DIR,
        f"refresh_missing_reversion_fundamentals_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
    )
    with open(output_path, "w", encoding="utf-8") as output_file:
        original_stdout = sys.stdout
        sys.stdout = TeeOutput(original_stdout, output_file)
        try:
            refresh_missing()
            print(f"Saved output: {output_path}")
        finally:
            sys.stdout = original_stdout
