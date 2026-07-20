"""WorldQuant-style cross-sectional group demeaning for candidate alpha scores."""
from collections import defaultdict
import math

import core_market_data_store


NEUTRALIZATION_LABELS = {
    "NONE": "None",
    "MARKET": "Market",
    "COUNTRY": "Country",
    "SECTOR": "Sector",
    "INDUSTRY": "Industry",
}


def normalize_mode(mode):
    mode = str(mode or "NONE").upper()
    return mode if mode in NEUTRALIZATION_LABELS else "NONE"


def mode_label(mode):
    return NEUTRALIZATION_LABELS[normalize_mode(mode)]


def _group_key(ticker, mode, metadata):
    if mode == "MARKET":
        return "MARKET"
    row = metadata.get(ticker, {})
    if mode == "COUNTRY":
        value = row.get("listing_country")
        return f"COUNTRY:{value or 'UNCLASSIFIED'}"
    if mode == "SECTOR":
        value = row.get("sector")
        if value:
            return f"SECTOR:{value}"
        country = row.get("listing_country")
        return f"SECTOR_UNCLASSIFIED:{country or 'UNCLASSIFIED'}"
    if mode == "INDUSTRY":
        value = row.get("industry")
        if value:
            return f"INDUSTRY:{value}"
        sector = row.get("sector")
        if sector:
            return f"INDUSTRY_UNCLASSIFIED:{sector}"
        country = row.get("listing_country")
        return f"INDUSTRY_UNCLASSIFIED:{country or 'UNCLASSIFIED'}"
    return "NONE"


def neutralize_scores(raw_scores, mode="NONE", metadata_by_ticker=None):
    """Subtract the selected group mean from each valid cross-sectional score."""
    mode = normalize_mode(mode)
    clean_scores = {
        ticker: float(score)
        for ticker, score in raw_scores.items()
        if score is not None and math.isfinite(float(score))
    }
    if mode == "NONE" or not clean_scores:
        return dict(clean_scores)

    metadata = (
        metadata_by_ticker
        if metadata_by_ticker is not None
        else core_market_data_store.get_security_metadata(clean_scores)
    )
    grouped_values = defaultdict(list)
    ticker_groups = {}
    for ticker, score in clean_scores.items():
        group = _group_key(ticker, mode, metadata)
        ticker_groups[ticker] = group
        grouped_values[group].append(score)

    group_means = {
        group: sum(values) / len(values)
        for group, values in grouped_values.items()
    }
    return {
        ticker: score - group_means[ticker_groups[ticker]]
        for ticker, score in clean_scores.items()
    }


def classification_coverage(mode, tickers):
    mode = normalize_mode(mode)
    tickers = list(tickers)
    if mode in ("NONE", "MARKET"):
        return len(tickers), len(tickers)
    metadata = core_market_data_store.get_security_metadata(tickers)
    if mode == "COUNTRY":
        present = sum(
            bool(row.get("listing_country"))
            for row in metadata.values()
        )
    else:
        field = "sector" if mode == "SECTOR" else "industry"
        present = sum(bool(row.get(field)) for row in metadata.values())
    return present, len(tickers)


def report_line(mode, tickers=None):
    mode = normalize_mode(mode)
    if mode == "NONE":
        return "Neutralization: None"
    line = (
        f"Neutralization: {mode_label(mode)} | group mean removed from today's "
        "cross-sectional candidate score"
    )
    if tickers is not None and mode != "MARKET":
        present, total = classification_coverage(mode, tickers)
        line += f" | labels {present}/{total}"
        if present < total:
            line += " (missing labels use parent/Unclassified group)"
    return line
