"""Pair groupings for the Data tab dropdowns.

Single source of truth so Majors/Crosses/Metals/Indices/Crypto headings
stay consistent across the bars-download and tick-download cards.
"""

from __future__ import annotations

PAIR_GROUPS: dict[str, list[str]] = {
    "Majors": [
        "EUR_USD",
        "GBP_USD",
        "USD_JPY",
        "USD_CHF",
        "AUD_USD",
        "USD_CAD",
        "NZD_USD",
    ],
    "Crosses": [
        "EUR_GBP",
        "EUR_JPY",
        "EUR_CHF",
        "EUR_AUD",
        "EUR_CAD",
        "EUR_NZD",
        "GBP_JPY",
        "GBP_CHF",
        "GBP_AUD",
        "GBP_CAD",
        "GBP_NZD",
        "AUD_JPY",
        "AUD_CHF",
        "AUD_CAD",
        "AUD_NZD",
        "NZD_JPY",
        "NZD_CHF",
        "NZD_CAD",
        "CAD_JPY",
        "CAD_CHF",
        "CHF_JPY",
    ],
    "Metals": [
        "XAU_USD",
        "XAG_USD",
        "XAU_EUR",
        "XPT_USD",
        "XPD_USD",
    ],
    "Indices": [
        "SPX500_USD",
        "NAS100_USD",
        "US30_USD",
        "UK100_GBP",
        "DE30_EUR",
        "JP225_USD",
    ],
    "Crypto": [
        "BTC_USD",
        "ETH_USD",
        "LTC_USD",
        "XRP_USD",
    ],
}


def group_pairs(all_pairs: list[str]) -> dict[str, list[str]]:
    """Slot pairs into groups; unknowns go to 'Other'. Empty groups dropped."""
    available = set(all_pairs)
    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    for group, pairs in PAIR_GROUPS.items():
        bucket = [p for p in pairs if p in available]
        if bucket:
            out[group] = bucket
            seen.update(bucket)
    other = sorted(p for p in all_pairs if p not in seen)
    if other:
        out["Other"] = other
    return out
