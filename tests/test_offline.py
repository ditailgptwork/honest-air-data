"""Offline checks: CAMS extraction and interpolation with a synthetic grid."""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import cams  # noqa: E402
from pipeline.sources import kma_grid  # noqa: E402

base = dt.datetime(2026, 11, 3, 12, tzinfo=dt.timezone.utc)
lat = np.arange(90, -90.01, -0.4)
lon = np.arange(0, 360, 0.4)
step = np.array([np.timedelta64(h, "h") for h in range(0, 49, 3)])
shape = (len(step), len(lat), len(lon))
grid = np.fromfunction(lambda s, i, j: (s + 1) * 1e-9, shape)
ds = xr.Dataset({"pm2p5": (("step", "latitude", "longitude"), grid),
                 "pm10": (("step", "latitude", "longitude"), grid * 2),
                 "uvbed": (("step", "latitude", "longitude"), np.full(shape, 0.1))},
                coords={"step": step, "latitude": lat, "longitude": lon})
cities = [{"id": "seoul", "lat": 37.57, "lon": 126.98}, {"id": "la", "lat": 34.05, "lon": -118.24}]
c = cams.extract(ds, cities, base)
assert c["cities"]["seoul"]["pm25"][0] == 1.0 and c["cities"]["la"]["pm10"][2] == 6.0
assert c["cities"]["seoul"]["uv"][0] == 4.0
now = base + dt.timedelta(hours=4, minutes=20)
s = cams.series_from(c, "seoul", now)
assert s["t0"] == "2026-11-03T16:00:00Z" and len(s["pm25"]) == 24 and abs(s["pm25"][0] - 2.333) < 0.06, s["pm25"][:3]
assert cams.expected_base(dt.datetime(2026, 11, 4, 9, tzinfo=dt.timezone.utc)).hour == 12
assert cams.expected_base(dt.datetime(2026, 11, 4, 11, tzinfo=dt.timezone.utc)).day == 4
assert kma_grid(37.57, 126.98) == (60, 127)

# Fitting: flat model 100/200, a reading of 30 taken 4 h before the current hour.
import math  # noqa: E402

from pipeline import build, sources  # noqa: E402
from pipeline.common import iso, utcnow  # noqa: E402

flat = {"base": iso(base), "times": [iso(base + dt.timedelta(hours=h)) for h in range(49)],
        "cities": {"x": {"pm25": [100.0] * 49, "pm10": [200.0] * 49, "uv": [0.0] * 49}}}
m = cams.series_from(flat, "x", base + dt.timedelta(hours=10, minutes=30))
assert m["t0"] == iso(base + dt.timedelta(hours=10))
assert abs(cams.value_at(flat, "x", "pm25", base + dt.timedelta(hours=6)) - 100) < 1e-9
assert cams.value_at(flat, "x", "pm25", base - dt.timedelta(hours=1)) is None
obs = {"src": "station", "pm25": 30.0, "at": iso(base + dt.timedelta(hours=6)), "age_h": 4.5, "station": "s"}
build.fit_model(m, obs, flat, "x")
f = 1 + (0.3 - 1) * math.exp(-4 / 12)
assert m["fit"] and m["fit_f"] == {"pm25": 0.3, "pm10": 0.3}, m.get("fit_f")
assert m["pm25"][0] == round(100 * f, 1) and m["pm10"][0] == round(200 * f, 1), (m["pm25"][0], m["pm10"][0])
assert m["pm25"][0] < m["pm25"][12] < m["pm25"][23] < 100  # fades back toward the raw model
air = build.choose_air(m, obs)
assert air["src"] == "fit" and air["pm25"] == m["pm25"][0] and air["ref"]["station"] == "s"
assert build.choose_air(m, dict(obs, age_h=1.0))["src"] == "station"
assert build.choose_air(None, obs)["src"] == "station"  # no model: show the reading with its time
assert build.choose_air(m, None)["src"] == "model"


# Station choice: a fresh reading beats a closer old one; an old one is still returned alone.
def fake_get_json(url, params=None, headers=None, **kw):
    now = utcnow()
    locs = [{"id": 1, "name": "near-old", "coordinates": {"latitude": 37.57, "longitude": 126.98},
             "datetimeLast": {"utc": iso(now - dt.timedelta(hours=4))},
             "sensors": [{"id": 11, "parameter": {"name": "pm25", "units": "µg/m³"}},
                         {"id": 12, "parameter": {"name": "o3", "units": "ppm"}}]},
            {"id": 2, "name": "far-fresh", "coordinates": {"latitude": 37.70, "longitude": 127.10},
             "datetimeLast": {"utc": iso(now - dt.timedelta(hours=1))},
             "sensors": [{"id": 21, "parameter": {"name": "pm25", "units": "µg/m³"}}]}]
    if url.endswith("/locations"):
        return {"results": [loc for loc in locs if loc["id"] in fake_get_json.ids]}
    lid = int(url.rstrip("/").split("/")[-2])
    age = 4 if lid == 1 else 1
    rows = [{"sensorsId": lid * 10 + 1, "value": 12.0, "datetime": {"utc": iso(now - dt.timedelta(hours=age))}}]
    if lid == 1:
        rows.append({"sensorsId": 12, "value": 0.03, "datetime": {"utc": iso(now - dt.timedelta(hours=age))}})
    return {"results": rows}


sources.get_json = fake_get_json
fake_get_json.ids = {1, 2}
r = sources.openaq_nearest(37.57, 126.98, "k", pause=0)
assert r["station"] == "far-fresh" and r["age_h"] < 1.1 and r["pm25"] == 12.0, r
fake_get_json.ids = {1}
r = sources.openaq_nearest(37.57, 126.98, "k", pause=0)
assert r["station"] == "near-old" and 3.9 < r["age_h"] < 4.1 and r["o3"] == 58.9, r
fake_get_json.ids = set()
assert sources.openaq_nearest(37.57, 126.98, "k", pause=0) is None
print("offline tests ok")
