"""Per-city history of station / model ratios, kept in the published site.

CAMS is often off at a single point by a steady factor (Seoul, Beijing and Jakarta
read 3 to 5 times lower than the model on 2026-11-03). One reading corrects the next
hours, but a steady bias should not fade back to the raw model by tomorrow. Each run
reads the previous v1/bias.json from the live site, adds this hour's ratios and
publishes it again, so no state lives in the repository or the Actions cache.
"""
import math
import os
import statistics
import time

from .common import get_json, parse_iso

URL = os.environ.get("BIAS_URL", "https://ditailgptwork.github.io/honest-air-data/v1/bias.json")
KEEP_H = 72      # rows older than this are dropped
USE_H = 48       # rows used for the steady factor
MIN_N = 6        # rows needed before the steady factor is trusted
LO, HI = 0.05, 20.0


def load(url=URL):
    """Previous history, or {} on the first run or any error."""
    try:
        j = get_json(url, {"t": int(time.time())}, tries=2, pause=1.0)
    except Exception:  # noqa: BLE001
        return {}
    return j.get("cities", {}) if isinstance(j, dict) and j.get("v") == 1 else {}


def _age_h(now, t):
    return (now - parse_iso(t)).total_seconds() / 3600


def add(hist, city_id, at, ratios, now):
    """Record this reading's ratios once per reading time."""
    rec = hist.setdefault(city_id, {})
    for k, r in ratios.items():
        rows = [row for row in rec.get(k, []) if row[0] != at and _age_h(now, row[0]) <= KEEP_H]
        rows.append([at, round(min(HI, max(LO, r)), 3)])
        rec[k] = sorted(rows)[-KEEP_H:]


def steady(hist, city_id, now):
    """Median ratio over the last USE_H hours per pollutant, when there are MIN_N rows."""
    out = {}
    for k, rows in (hist.get(city_id) or {}).items():
        xs = [math.log(r) for t, r in rows if r > 0 and _age_h(now, t) <= USE_H]
        if len(xs) >= MIN_N:
            out[k] = math.exp(statistics.median(xs))
    return out


def prune(hist, now):
    for cid in list(hist):
        for k in list(hist[cid]):
            hist[cid][k] = [row for row in hist[cid][k] if _age_h(now, row[0]) <= KEEP_H]
            if not hist[cid][k]:
                del hist[cid][k]
        if not hist[cid]:
            del hist[cid]
    return hist
