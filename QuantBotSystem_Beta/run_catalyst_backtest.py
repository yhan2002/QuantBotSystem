# File name: run_catalyst_backtest.py
# Runs a local-only catalyst overlay backtest against cached market data.
import html
import math
import os
import sys

import scalping_catalyst_engine as catalyst


def format_block(title, stats):
    lines = [title]
    for horizon, row in stats.items():
        lines.append(
            f"- {horizon}: count {row['count']}, "
            f"avg {row['avg']:+.2f}%, std {row['std']:.2f}%, "
            f"win {row['win_rate']:.1f}%"
        )
    return "\n".join(lines)


def _percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * pct / 100.0
    low = int(idx)
    high = min(low + 1, len(ordered) - 1)
    weight = idx - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _trade_stats(trades):
    values = [t["return_pct"] for t in trades]
    if not values:
        return {
            "count": 0,
            "avg": 0.0,
            "std": 0.0,
            "t_stat": 0.0,
            "win_rate": 0.0,
            "min": 0.0,
            "max": 0.0,
        }
    avg = sum(values) / len(values)
    variance = sum((v - avg) ** 2 for v in values) / (len(values) - 1) if len(values) > 1 else 0.0
    std = variance ** 0.5
    t_stat = avg / (std / math.sqrt(len(values))) if std > 0 and len(values) > 1 else 0.0
    wins = sum(1 for v in values if v > 0)
    return {
        "count": len(values),
        "avg": avg,
        "std": std,
        "t_stat": t_stat,
        "win_rate": wins / len(values) * 100.0,
        "min": min(values),
        "max": max(values),
    }


def _svg_scatter_panel(trades, x, y, width, height, title):
    margin_left = 42
    margin_right = 14
    margin_top = 34
    margin_bottom = 28
    plot_x = x + margin_left
    plot_y = y + margin_top
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    values = [t["return_pct"] for t in trades]
    stats = _trade_stats(trades)

    parts = [
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" fill="#ffffff" stroke="#d7dce2"/>',
        f'<text x="{x + 12}" y="{y + 20}" font-size="13" font-weight="700" fill="#1d2630">{html.escape(title)}</text>',
        (
            f'<text x="{x + width - 12}" y="{y + 20}" font-size="11" text-anchor="end" fill="#52606d">'
            f'n={stats["count"]}, avg={stats["avg"]:+.2f}%, t={stats["t_stat"]:.2f}, win={stats["win_rate"]:.1f}%</text>'
        ),
    ]

    if not trades:
        parts.append(
            f'<text x="{x + width / 2}" y="{y + height / 2}" text-anchor="middle" '
            f'font-size="12" fill="#8b949e">No valid trades</text>'
        )
        return "\n".join(parts)

    y_min = _percentile(values, 1)
    y_max = _percentile(values, 99)
    if y_min == y_max:
        y_min -= 1.0
        y_max += 1.0
    y_pad = max((y_max - y_min) * 0.12, 0.5)
    y_min -= y_pad
    y_max += y_pad

    def map_x(idx):
        if len(trades) == 1:
            return plot_x + plot_w / 2
        return plot_x + idx / (len(trades) - 1) * plot_w

    def map_y(value):
        clipped = min(max(value, y_min), y_max)
        return plot_y + (y_max - clipped) / (y_max - y_min) * plot_h

    zero_y = map_y(0.0)
    parts.extend(
        [
            f'<line x1="{plot_x}" y1="{plot_y}" x2="{plot_x}" y2="{plot_y + plot_h}" stroke="#aab3bd"/>',
            f'<line x1="{plot_x}" y1="{plot_y + plot_h}" x2="{plot_x + plot_w}" y2="{plot_y + plot_h}" stroke="#aab3bd"/>',
            f'<line x1="{plot_x}" y1="{zero_y}" x2="{plot_x + plot_w}" y2="{zero_y}" stroke="#7d8791" stroke-dasharray="4 4"/>',
            f'<text x="{plot_x - 6}" y="{plot_y + 4}" font-size="10" text-anchor="end" fill="#6b7480">{y_max:+.1f}%</text>',
            f'<text x="{plot_x - 6}" y="{plot_y + plot_h}" font-size="10" text-anchor="end" fill="#6b7480">{y_min:+.1f}%</text>',
            f'<text x="{plot_x - 6}" y="{zero_y + 3}" font-size="10" text-anchor="end" fill="#4d5660">0%</text>',
        ]
    )

    for idx, trade in enumerate(trades):
        ret = trade["return_pct"]
        color = "#1f7a4d" if ret >= 0 else "#b23b3b"
        parts.append(
            f'<circle cx="{map_x(idx):.2f}" cy="{map_y(ret):.2f}" r="2.1" '
            f'fill="{color}" fill-opacity="0.58"/>'
        )

    parts.append(
        f'<text x="{x + 12}" y="{y + height - 8}" font-size="10" fill="#7b8490">'
        f'y-axis clipped near p1/p99 for readability; raw min={stats["min"]:+.2f}%, max={stats["max"]:+.2f}%</text>'
    )
    return "\n".join(parts)


def save_distribution_svg(records, output_path):
    fixed_trades = catalyst.get_fixed_trade_returns(records)
    panel_w = 720
    panel_h = 260
    width = panel_w + 40
    height = 72 + panel_h + 30
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f5f7f9"/>',
        '<text x="20" y="30" font-size="20" font-weight="700" fill="#17202a">Fixed Catalyst Return Distribution</text>',
        (
            '<text x="20" y="52" font-size="12" fill="#5b6570">'
            f'obj:ana={catalyst.FIXED_OBJ_WEIGHT:.0%}:{catalyst.FIXED_ANA_WEIGHT:.0%}, '
            f'ana_cap={catalyst.ANALYST_CAP}, threshold&gt;={catalyst.FIXED_THRESHOLD}, '
            f'holding={catalyst.FIXED_HOLDING_DAYS}d</text>'
        ),
    ]

    parts.append(
        _svg_scatter_panel(
            fixed_trades,
            20,
            72,
            panel_w,
            panel_h,
            "Fixed parameter set, positive catalyst long",
        )
    )

    parts.append("</svg>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def main(workbook_path):
    records = catalyst.load_scalping_events(workbook_path)
    print(f"Loaded catalyst records: {len(records)}")
    fixed_trades = catalyst.get_fixed_trade_returns(records)
    stats = _trade_stats(fixed_trades)
    print("\n" + "=" * 48)
    print("Fixed Catalyst Parameters")
    print(f"obj:ana = {catalyst.FIXED_OBJ_WEIGHT:.0%}:{catalyst.FIXED_ANA_WEIGHT:.0%}")
    print(f"analyst cap = {catalyst.ANALYST_CAP}")
    print(f"threshold >= {catalyst.FIXED_THRESHOLD}")
    print(f"holding = {catalyst.FIXED_HOLDING_DAYS}d")
    print("\nFixed Backtest Result")
    print(
        f"count {stats['count']}, avg {stats['avg']:+.2f}%, "
        f"std {stats['std']:.2f}%, t {stats['t_stat']:.2f}, "
        f"win {stats['win_rate']:.1f}%"
    )

    output_path = os.path.join(os.path.dirname(__file__), "catalyst_return_distribution.svg")
    save_distribution_svg(records, output_path)
    print("\n" + "=" * 48)
    print(f"Scatter chart saved: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python run_catalyst_backtest.py workbook.xlsx")
    main(sys.argv[1])
