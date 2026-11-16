"""0.6.5 cross-check sources (offline): python3 tests/test_cross.py from the repository root."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import build  # noqa: E402
from pipeline.common import iso  # noqa: E402

base = dt.datetime(2026, 11, 16, 0, tzinfo=dt.timezone.utc)
H = dt.timedelta(hours=1)
cache = {"base": iso(base), "times": [iso(base + h * H) for h in range(49)],
         "cities": {"x": {"pm25": [float(h) for h in range(49)], "pm10": [2.0 * h for h in range(49)], "uv": [0.0] * 49}}}
air = {"src": "station", "station": "중구", "provider": "Korea Air Ministry of Environment", "monitor": True,
       "dist_km": 0.7, "pm25": 30.0, "pm10": 50.0, "at": iso(base + 10 * H), "fit_ok": False}
sensor = {"src": "station", "station": "s1", "provider": "AirGradient", "monitor": False, "dist_km": 2.1,
          "pm25": 12.0, "at": iso(base + 9.5 * H)}

c = build.cross_sources(air, cache, "x", sensor, {}, [])
assert [x["kind"] for x in c] == ["monitor", "model", "sensor"], c
assert c[1]["pm25"] == 10.0 and c[1]["pm10"] == 20.0 and c[1]["at"] == air["at"]  # raw model at the reading's hour
assert c[0]["pm25"] == 30.0 and c[2]["pm25"] == 12.0 and "pm10" not in c[2]       # as published, nothing filled in
assert [x["kind"] for x in build.cross_sources(air, cache, "x", sensor, {}, ["forecast"])] == ["monitor", "sensor"]
assert build.cross_sources(air, cache, "x", None, {}, ["forecast"]) is None           # one source: no card
old = dict(sensor, at=iso(base + 7.5 * H))
assert [x["kind"] for x in build.cross_sources(air, cache, "x", old, {}, [])] == ["monitor", "model"]  # 2.5 h apart
assert [x["kind"] for x in build.cross_sources(air, cache, "x", sensor, {"monitors_only": True}, [])] == ["monitor", "model"]
bad = dict(sensor, pm25=0.5)  # near zero against a model of 9.5: a fault
assert [x["kind"] for x in build.cross_sources(air, cache, "x", bad, {}, [])] == ["monitor", "model"]
assert build.cross_sources(dict(air, src="fit"), cache, "x", sensor, {}, []) is None  # only a reading of this hour
assert build.cross_sources(dict(air, provider="AirNow", monitor=False), cache, "x", None, {}, [])[0]["kind"] == "sensor"
print("cross ok")
