# File name: tune_catalyst_score_mix.py
# Tunes catalyst score mix with obj/ana ratio, analyst cap, and final threshold.
import csv
import math
import os
import sys

import scalping_catalyst_engine as catalyst


TARGET_WIN_RATE = 55.0
MIN_TRADES = 200
MIN_T_STAT = 2.58
HORIZONS = [1, 5]

# Fixed trust discount for manually scored analyst inputs.
# This is intentionally not tuned to avoid redundant degrees of freedom.
ANALYST_TRUST_MULTIPLIER = 0.20

# Fixed parameter pick after the first tuning pass.
MIX_RATIOS = [(0.70, 0.30)]

ANALYST_CAPS = [10]

THRESHOLDS = [35]

OBJECTIVE_MAX_SCORE = sum(weight for weight in catalyst.OBJECTIVE_WEIGHTS.values() if weight > 0)


def objective_score(record):
    score = 0.0
    for key, weight in catalyst.OBJECTIVE_WEIGHTS.items():
        if record.get(key):
            score += weight
    return score


def analyst_raw_score(record):
    linkage = record.get("fundamental_linkage_score")
    size = record.get("catalyst_size_score")
    if linkage is None or size is None:
        return 0.0
    raw = float(linkage) * abs(float(size)) / 100.0
    return -raw if float(size) < 0 else raw


def capped_analyst_score(raw_score, analyst_cap):
    signed = -1.0 if raw_score < 0 else 1.0
    discounted = abs(raw_score) * ANALYST_TRUST_MULTIPLIER
    return signed * min(discounted, analyst_cap)


def normalize_objective(score):
    if OBJECTIVE_MAX_SCORE <= 0:
        return 0.0
    return score / OBJECTIVE_MAX_SCORE * 100.0


def normalize_analyst(score, analyst_cap):
    if analyst_cap <= 0:
        return 0.0
    return score / analyst_cap * 100.0


def mixed_score(row, obj_weight, ana_weight, analyst_cap):
    obj_norm = normalize_objective(row["objective_score"])
    ana_score = capped_analyst_score(row["analyst_raw_score"], analyst_cap)
    ana_norm = normalize_analyst(ana_score, analyst_cap)
    return obj_norm * obj_weight + ana_norm * ana_weight


def forward_return(record, horizon):
    return catalyst._forward_return(record["ticker"], record["event_date"], horizon)


def build_trade_table(records):
    rows = []
    for record in records:
        obj = objective_score(record)
        if obj <= 0:
            continue

        row = {
            "event_date": record["event_date"],
            "ticker": record["ticker"],
            "country": record["country"],
            "objective_score": obj,
            "analyst_raw_score": analyst_raw_score(record),
        }

        has_return = False
        for horizon in HORIZONS:
            ret = forward_return(record, horizon)
            row[f"ret_{horizon}d"] = ret
            if ret is not None:
                has_return = True

        if has_return:
            rows.append(row)
    return rows


def summary(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    avg = sum(values) / len(values)
    variance = sum((v - avg) ** 2 for v in values) / (len(values) - 1) if len(values) > 1 else 0.0
    wins = sum(1 for v in values if v > 0)
    std = variance ** 0.5
    t_stat = avg / (std / math.sqrt(len(values))) if std > 0 and len(values) > 1 else 0.0
    return {
        "count": len(values),
        "avg": avg,
        "std": std,
        "t_stat": t_stat,
        "win_rate": wins / len(values) * 100.0,
    }


def run_grid(trade_table):
    results = []
    for obj_weight, ana_weight in MIX_RATIOS:
        for analyst_cap in ANALYST_CAPS:
            scored = [
                (mixed_score(row, obj_weight, ana_weight, analyst_cap), row)
                for row in trade_table
            ]

            for threshold in THRESHOLDS:
                selected = [row for final_score, row in scored if final_score >= threshold]
                if not selected:
                    continue

                for horizon in HORIZONS:
                    stats = summary([row[f"ret_{horizon}d"] for row in selected])
                    if not stats:
                        continue
                    if stats["t_stat"] < MIN_T_STAT:
                        continue
                    results.append(
                        {
                            "obj_weight": obj_weight,
                            "ana_weight": ana_weight,
                            "analyst_trust_multiplier": ANALYST_TRUST_MULTIPLIER,
                            "analyst_cap": analyst_cap,
                            "threshold": threshold,
                            "horizon": horizon,
                            "count": stats["count"],
                            "avg": stats["avg"],
                            "std": stats["std"],
                            "t_stat": stats["t_stat"],
                            "win_rate": stats["win_rate"],
                            "target_gap": abs(stats["win_rate"] - TARGET_WIN_RATE),
                            "meets_min_trades": stats["count"] >= MIN_TRADES,
                        }
                    )
    return results


def rank_key(row):
    sample_penalty = 0 if row["meets_min_trades"] else 100
    return (
        sample_penalty,
        row["target_gap"],
        -row["count"],
        -row["avg"],
    )


def format_row(row):
    return (
        f"obj:ana={row['obj_weight']:.0%}:{row['ana_weight']:.0%}, "
        f"ana_cap={row['analyst_cap']}, "
        f"threshold>={row['threshold']}, "
        f"{row['horizon']}d, "
        f"count={row['count']}, "
        f"avg={row['avg']:+.2f}%, "
        f"std={row['std']:.2f}%, "
        f"t={row['t_stat']:.2f}, "
        f"win={row['win_rate']:.1f}%"
    )


def write_csv(results, output_path):
    fieldnames = [
        "obj_weight",
        "ana_weight",
        "analyst_trust_multiplier",
        "analyst_cap",
        "threshold",
        "horizon",
        "count",
        "avg",
        "std",
        "t_stat",
        "win_rate",
        "target_gap",
        "meets_min_trades",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def write_summary(ranked, output_path):
    stable = [row for row in ranked if row["meets_min_trades"]]
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("Catalyst score mix tuning\n")
        f.write(f"Target win rate: {TARGET_WIN_RATE:.1f}%\n")
        f.write(f"Minimum valid trades: {MIN_TRADES}\n")
        f.write(f"Minimum t-stat: {MIN_T_STAT:.2f}\n")
        f.write(f"Fixed analyst trust multiplier: {ANALYST_TRUST_MULTIPLIER:.2f}\n")
        f.write("Hyperparameters: obj/ana ratio, analyst cap, final threshold\n\n")
        f.write("Top stable candidates\n")
        for row in stable[:30]:
            f.write(format_row(row) + "\n")
        f.write("\nTop all candidates, including small samples\n")
        for row in ranked[:30]:
            f.write(format_row(row) + "\n")


def main(workbook_path):
    print("Loading scalping workbook...")
    records = catalyst.load_scalping_events(workbook_path)
    print(f"Loaded records: {len(records)}")

    print("Precomputing valid trade returns...")
    trade_table = build_trade_table(records)
    print(f"Valid scored trade rows: {len(trade_table)}")

    print("Running fixed-parameter check...")
    print("Fixed params: obj:ana=70%:30%, analyst cap=10, threshold>=35")
    print(f"Fixed analyst trust multiplier: {ANALYST_TRUST_MULTIPLIER:.2f}")
    print(f"Keeping only cases with t-stat >= {MIN_T_STAT:.2f}")

    results = run_grid(trade_table)
    ranked = sorted(results, key=rank_key)
    stable = [row for row in ranked if row["meets_min_trades"]]

    base_dir = os.path.dirname(__file__)
    csv_path = os.path.join(base_dir, "catalyst_score_mix_tuning.csv")
    txt_path = os.path.join(base_dir, "catalyst_score_mix_tuning_summary.txt")
    write_csv(results, csv_path)
    write_summary(ranked, txt_path)

    print("\nTop stable candidates")
    for row in stable[:15]:
        print("- " + format_row(row))

    print("\nOutput files")
    print(f"- {csv_path}")
    print(f"- {txt_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python tune_catalyst_score_mix.py workbook.xlsx")
    main(sys.argv[1])
