"""CAMS global forecast: fetch twice a day, keep a small per-city cache.

The forecast runs at 00 and 12 UTC and is usually ready about 10 hours later.
The hourly job only downloads when a newer run should exist; otherwise it
reuses cache/cams.json. Values: PM in ug/m3, UV index = uvbed (W/m2) x 40.
"""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

from .common import clean, iso, parse_iso

CACHE = Path("cache/cams.json")
DATASET = "cams-global-atmospheric-composition-forecasts"
VARIABLES = ["particulate_matter_2.5um", "particulate_matter_10um", "uv_biologically_effective_dose"]
NAMES = {"pm25": ("pm2p5", "pm2.5", "pm25"), "pm10": ("pm10",), "uv": ("uvbed",)}

_FETCH = r'''
import cdsapi, json, sys
req, out = json.loads(sys.argv[1]), sys.argv[2]
cdsapi.Client(quiet=True, progress=False).retrieve("%s", req, out)
''' % DATASET


def expected_base(now):
    d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now >= d0 + dt.timedelta(hours=22, minutes=30):
        return d0 + dt.timedelta(hours=12)
    if now >= d0 + dt.timedelta(hours=10, minutes=30):
        return d0
    return d0 - dt.timedelta(hours=12)


def load_cache():
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def fetch_grib(base, out_path, step=1, timeout=1800):
    req = {"variable": VARIABLES, "date": [f"{base:%Y-%m-%d}/{base:%Y-%m-%d}"], "time": [f"{base:%H}:00"],
           "leadtime_hour": [str(h) for h in range(0, 49, step)], "type": ["forecast"], "data_format": "grib"}
    env = dict(os.environ, CDSAPI_URL="https://ads.atmosphere.copernicus.eu/api",
               CDSAPI_KEY=os.environ.get("ADS_API_KEY", ""))
    p = subprocess.run([sys.executable, "-c", _FETCH, json.dumps(req), str(out_path)],
                       env=env, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 or not Path(out_path).exists():
        tail = (p.stderr or p.stdout).strip().splitlines()[-2:]
        raise RuntimeError(clean(" | ".join(tail)))


def open_grib(path):
    import cfgrib
    import xarray as xr
    parts = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
    return xr.merge(parts, compat="override")


def _var(ds, key):
    for n in NAMES[key]:
        if n in ds.data_vars:
            return ds[n]
    raise KeyError(f"{key} not in {list(ds.data_vars)}")


def extract(ds, cities, base):
    """Nearest grid point per city. Returns cache dict with hourly series."""
    lats = np.asarray(ds.latitude.values, dtype=float)
    lons = np.asarray(ds.longitude.values, dtype=float)
    steps = ds.step.values.astype("timedelta64[h]").astype(int)
    times = [iso(base + dt.timedelta(hours=int(h))) for h in steps]
    pm25, pm10, uv = _var(ds, "pm25"), _var(ds, "pm10"), _var(ds, "uv")
    out = {}
    for c in cities:
        lon = c["lon"] % 360 if lons.max() > 180 else ((c["lon"] + 180) % 360) - 180
        i, j = int(np.abs(lats - c["lat"]).argmin()), int(np.abs(lons - lon).argmin())
        out[c["id"]] = {
            "pm25": [round(float(v) * 1e9, 1) for v in pm25[:, i, j].values],
            "pm10": [round(float(v) * 1e9, 1) for v in pm10[:, i, j].values],
            "uv": [round(max(float(v), 0.0) * 40, 1) for v in uv[:, i, j].values],
        }
    return {"base": iso(base), "times": times, "cities": out}


def refresh(cities, now, workdir=Path("cache"), log=print):
    """Return (cache, updated). Never raises: errors are logged and the old cache is kept."""
    cache = load_cache()
    want = expected_base(now)
    have = parse_iso(cache["base"]) if cache else None
    missing = [c["id"] for c in cities if c["id"] not in (cache or {}).get("cities", {})]
    bases = [want, want - dt.timedelta(hours=12)]
    if have and have >= want:
        if not missing:
            return cache, False
        bases = [have]  # same run again, only to add cities that are new in cities.json
        log(f"CAMS: {len(missing)} cities not in the cache, fetching run {iso(have)} again")
    if not os.environ.get("ADS_API_KEY"):
        log("CAMS: no key, using cache")
        return cache, False
    workdir.mkdir(parents=True, exist_ok=True)
    grib = workdir / "cams.grib"
    for base in bases:
        if have and (have > base or (have == base and not missing)):
            break  # never step back to an older run
        for step in (1, 3):
            try:
                fetch_grib(base, grib, step=step)
                new = extract(open_grib(grib), cities, base)
                CACHE.write_text(json.dumps(new, separators=(",", ":")), encoding="utf-8")
                grib.unlink(missing_ok=True)
                log(f"CAMS: updated to run {new['base']} (every {step} h)")
                return new, True
            except subprocess.TimeoutExpired:
                log(f"CAMS: run {iso(base)} still queued, keep cache")
                return cache, False
            except Exception as e:  # noqa: BLE001
                log(f"CAMS: run {iso(base)} step {step} failed: {clean(e)}")
    return cache, False


def value_at(cache, city_id, key, t):
    """Model value at time t by linear interpolation, or None outside the run."""
    if not cache or city_id not in cache.get("cities", {}):
        return None
    xs = [parse_iso(x).timestamp() for x in cache["times"]]
    x = t.timestamp()
    if not xs or x < xs[0] or x > xs[-1]:
        return None
    return float(np.interp(x, xs, np.array(cache["cities"][city_id][key], dtype=float)))


def series_from(cache, city_id, now, hours=24):
    """Hourly values from the current hour, linear interpolation if the run is 3-hourly."""
    if not cache or city_id not in cache.get("cities", {}):
        return None
    ts = [parse_iso(t) for t in cache["times"]]
    start = now.replace(minute=0, second=0, microsecond=0)
    if not ts or start > ts[-1]:
        return None
    rec = cache["cities"][city_id]
    want = [start + dt.timedelta(hours=h) for h in range(hours)]
    xs = np.array([t.timestamp() for t in ts])
    out = {"base": cache["base"], "t0": iso(start)}
    for k in ("pm25", "pm10", "uv"):
        ys = np.array(rec[k], dtype=float)
        out[k] = [round(float(v), 1) for v in np.interp([w.timestamp() for w in want if w <= ts[-1]], xs, ys)]
    return out
