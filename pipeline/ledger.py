"""Forecast ledger: corrected forecasts and their verified errors (shadow mode).

Before the app may say "on days like this, 7 times out of 10 the window stayed clean",
its own record has to say so. Four times a local day (from 00, 06, 12 and 18 h) the
corrected PM forecast of every city fitted to a station of its own is written down;
later runs fill in 100 * ln(reading / forecast) for each hour that the same station
reports. Nothing here is shown in the app yet.

Like bias.json the file is carried forward through the published site. A copy also
travels in the Actions cache, so a failed read never publishes an empty history over
a good one. Countries without a forecast licence (policy forecast:false) are never
written down, because the snapshots are forecasts too.
"""
import datetime as dt
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import get_json, iso, parse_iso

URL = os.environ.get("LEDGER_URL", "https://ditailgptwork.github.io/honest-air-data/v1/ledger.json")
VERSION = 1
KEEP_D = 60     # days of snapshots kept
HOURS = 24      # forecast hours written down per snapshot
SLOT_H = 6      # one snapshot per city every 6 local hours
SCALE = 100     # errors are stored as round(SCALE * ln(reading / forecast))
CLIP = 500      # |error| cap, about a factor of 150 either way
ST_LEN = 40     # station names are cut to this length


def _hours(t):
    """Whole hours since the epoch (floor)."""
    return int(t.timestamp() // 3600)


def slot(now, tz):
    """Local date and 6-hour slot, e.g. '2026-11-09/1' for 06:00-11:59."""
    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001  unknown zone: fall back to UTC slots
        local = now
    return f"{local:%Y-%m-%d}/{local.hour // SLOT_H}"


def load(url=URL, fallback=Path("cache/ledger.json")):
    """(cities, source). source: 'site', 'new' (first run), 'cache' (site unreadable) or 'lost'."""
    try:
        j = get_json(url, {"t": int(dt.datetime.now(dt.timezone.utc).timestamp())}, tries=3, pause=2.0)
        if isinstance(j, dict) and j.get("v") == VERSION:
            return j.get("cities", {}), "site"
        return {}, "new"
    except Exception as e:  # noqa: BLE001
        if "HTTP 404" in str(e):
            return {}, "new"
    try:
        j = json.loads(Path(fallback).read_text(encoding="utf-8"))
        if isinstance(j, dict) and j.get("v") == VERSION:
            return j.get("cities", {}), "cache"
    except Exception:  # noqa: BLE001
        pass
    return {}, "lost"


def snapshot(led, city, model, src, base, now):
    """Write down this slot's corrected forecast once. Returns True when written."""
    rows = led.setdefault(city["id"], [])
    key = slot(now, city.get("tz") or "UTC")
    if any(r.get("s") == key for r in rows):
        return False
    f25 = [x for x in (model.get("pm25") or [])][:HOURS]
    if not f25 or not model.get("t0"):
        return False
    f10 = [x for x in (model.get("pm10") or [])][:HOURS]
    t0 = parse_iso(model["t0"])
    t_obs = parse_iso(src["at"]) if src.get("at") else t0
    rows.append({
        "s": key, "t0": _hours(t0), "to": _hours(t_obs),
        "lag": round(max(0.0, (t0 - t_obs).total_seconds() / 3600), 1),
        "b": _hours(parse_iso(base)) if base else None,
        "st": (src.get("station") or "")[:ST_LEN],
        "r0": model.get("fit_f") or {}, "rs": model.get("steady_f") or {},
        "f25": f25, "f10": f10, "e25": [None] * len(f25), "e10": [None] * len(f10),
    })
    return True


def verify(led, city_id, src, now):
    """Fill in errors for every snapshot hour this reading covers. Returns how many."""
    if not src.get("at"):
        return 0
    th = _hours(parse_iso(src["at"]))
    station = (src.get("station") or "")[:ST_LEN]
    n = 0
    for r in led.get(city_id, []):
        h = th - r["t0"]
        if h < 0 or th <= r.get("to", r["t0"]) or r.get("st") != station:
            continue  # before the forecast, the reading it was fitted to, or another station
        for k, fk, ek in (("pm25", "f25", "e25"), ("pm10", "f10", "e10")):
            if h >= len(r[fk]) or r[ek][h] is not None:
                continue
            v, f = src.get(k), r[fk][h]
            if v and f and v > 0 and f > 0:
                r[ek][h] = max(-CLIP, min(CLIP, round(SCALE * math.log(v / f))))
                n += 1
    return n


def prune(led, now):
    cut = _hours(now) - KEEP_D * 24
    for cid in list(led):
        led[cid] = [r for r in led[cid] if r.get("t0", 0) >= cut]
        if not led[cid]:
            del led[cid]
    return led


def stats(led, source):
    snaps = sum(len(v) for v in led.values())
    errs = sum(1 for v in led.values() for r in v for e in r.get("e25", []) if e is not None)
    return {"from": source, "cities": len(led), "snapshots": snaps, "errors_pm25": errs}


def payload(led, now):
    return {"v": VERSION, "generated": iso(now), "keep_d": KEEP_D, "slot_h": SLOT_H,
            "scale": SCALE, "cities": led}
