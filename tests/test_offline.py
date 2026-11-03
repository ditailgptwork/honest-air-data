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

from pipeline import bias, build, sources  # noqa: E402
from pipeline.common import iso, utcnow  # noqa: E402

H = dt.timedelta(hours=1)
flat = {"base": iso(base), "times": [iso(base + h * H) for h in range(49)],
        "cities": {"x": {"pm25": [100.0] * 49, "pm10": [200.0] * 49, "uv": [0.0] * 49}}}
now = base + 10.5 * H
m = cams.series_from(flat, "x", now)
assert m["t0"] == iso(base + 10 * H)
assert abs(cams.value_at(flat, "x", "pm25", base + 6 * H) - 100) < 1e-9
assert cams.value_at(flat, "x", "pm25", base - H) is None
obs = {"src": "station", "pm25": 30.0, "at": iso(base + 6 * H), "age_h": 4.5, "station": "s", "monitor": True}
ratios = build.ratios_at_reading(m, obs, flat, "x")
assert ratios == {"pm25": 0.3}, ratios
build.fit_model(m, obs, ratios)  # no history: half of the log ratio is taken as steady
l0 = math.log(0.3)
ls = 0.5 * l0
f = lambda lag: math.exp(ls + (l0 - ls) * math.exp(-lag / 12))  # noqa: E731
assert m["fit"] and m["fit_f"] == {"pm25": 0.3, "pm10": 0.3} and m["steady_f"]["pm25"] == round(math.exp(ls), 2)
assert m["pm25"][0] == round(100 * f(4), 1) and m["pm10"][0] == round(200 * f(4), 1), (m["pm25"][0], m["pm10"][0])
assert m["pm25"][23] == round(100 * f(27), 1) and m["pm25"][23] < 100 * math.exp(ls) * 1.05
m2 = cams.series_from(flat, "x", now)
build.fit_model(m2, obs, ratios, {"pm25": 0.3})  # a steady 0.3 keeps the whole day near the reading
assert all(abs(x - 30) < 0.11 for x in m2["pm25"]), m2["pm25"][:3]
air = build.choose_air(m, obs)
assert air["src"] == "fit" and air["pm25"] == m["pm25"][0] and air["ref"]["station"] == "s"
assert build.choose_air(m, dict(obs, age_h=1.0))["src"] == "station"
assert build.choose_air(None, obs)["src"] == "station"  # no model: show the reading with its time
assert build.choose_air(m, None)["src"] == "model"
assert build.suspect({"monitor": False, "pm25": 0.5}, {"pm25": 0.04})        # Lagos: near zero
assert build.suspect({"monitor": False, "pm25": 1.5}, {"pm25": 0.1})         # Sao Paulo
assert not build.suspect({"monitor": False, "pm25": 50.2}, {"pm25": 0.19})   # Beijing: model bias
assert not build.suspect({"monitor": True, "pm25": 0.5}, {"pm25": 0.04})     # monitors are trusted
assert build.suspect({"monitor": False, "pm25": 400}, {"pm25": 9})

# History: one row per reading time, median of the last 48 h once there are 6 rows.
hist = {}
for h in range(8):
    bias.add(hist, "x", iso(base + h * H), {"pm25": 0.3}, now)
bias.add(hist, "x", iso(base + 7 * H), {"pm25": 0.5}, now)
assert len(hist["x"]["pm25"]) == 8
assert abs(bias.steady(hist, "x", now)["pm25"] - 0.3) < 1e-9
assert bias.steady({"x": {"pm25": [[iso(base), 0.3]]}}, "x", now) == {}
assert bias.prune({"x": {"pm25": [[iso(base - 100 * H), 0.3]]}}, now) == {}


# Station choice: monitors first, then a fresh reading, then distance; a PM reading of 0 is skipped.
def fake_get_json(url, params=None, headers=None, **kw):
    t = utcnow()
    spec = fake_get_json.spec  # id -> (monitor, age_h, lat, pm25)
    if url.endswith("/locations"):
        return {"results": [{"id": i, "name": f"loc{i}", "isMonitor": mon,
                             "coordinates": {"latitude": la, "longitude": 126.98},
                             "datetimeLast": {"utc": iso(t - age * H)},
                             "sensors": [{"id": i * 10 + 1, "parameter": {"name": "pm25", "units": "µg/m³"}},
                                         {"id": i * 10 + 2, "parameter": {"name": "o3", "units": "ppm"}}]}
                            for i, (mon, age, la, v) in spec.items()]}
    i = int(url.rstrip("/").split("/")[-2])
    mon, age, la, v = spec[i]
    return {"results": [{"sensorsId": i * 10 + 1, "value": v, "datetime": {"utc": iso(t - age * H)}},
                        {"sensorsId": i * 10 + 2, "value": 0.03, "datetime": {"utc": iso(t - age * H)}}]}


sources.get_json = fake_get_json
q = lambda: sources.openaq_nearest(37.57, 126.98, "k", pause=0)  # noqa: E731
fake_get_json.spec = {1: (True, 4, 37.57, 12.0), 2: (True, 1, 37.70, 15.0)}
assert q()["station"] == "loc2"  # both monitors: the fresh one wins over the closer old one
fake_get_json.spec = {1: (True, 4, 37.57, 12.0), 2: (False, 1, 37.58, 15.0)}
r = q()
assert r["station"] == "loc1" and r["monitor"] and 3.9 < r["age_h"] < 4.1 and r["o3"] == 58.9, r
fake_get_json.spec = {1: (True, 1, 37.57, 0.0), 2: (False, 1, 37.60, 15.0)}
assert q()["station"] == "loc2"  # loc1 reads 0, so the next station is asked
fake_get_json.spec = {}
assert q() is None
# CAMS refresh: nothing to do when the run is current; fetch the same run again for new cities.
import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

tmp = Path(tempfile.mkdtemp())
cams.CACHE = tmp / "cams.json"
cams.CACHE.write_text(json.dumps(dict(flat, base=iso(base))), encoding="utf-8")
calls = []
cams.fetch_grib = lambda b, out, step=1, timeout=1800: (calls.append(iso(b)), Path(out).write_text("g"))
cams.open_grib = lambda path: ds
os.environ.setdefault("ADS_API_KEY", "offline-test-key")
late = base + 11 * H  # 23 UTC: the 12 UTC run is the one to have
one = [{"id": "x", "lat": 37.57, "lon": 126.98}]
c1, upd = cams.refresh(one, late, workdir=tmp, log=lambda *a: None)
assert not upd and calls == []
c2, upd = cams.refresh(one + [{"id": "seoul", "lat": 37.57, "lon": 126.98}], late, workdir=tmp, log=lambda *a: None)
assert upd and calls == [iso(base)] and "seoul" in c2["cities"] and c2["base"] == iso(base), (calls, c2.get("base"))
print("offline tests ok")
