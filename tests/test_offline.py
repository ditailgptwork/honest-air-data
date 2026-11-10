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

assert build.is_airkorea("Korea Air Ministry of Environment") and build.is_airkorea("AirKorea")
assert not build.is_airkorea("AirNow") and not build.is_airkorea(None)

ak = dict(obs, provider="Korea Air Ministry of Environment", fit_ok=False, age_h=4.5)
assert build.choose_air(m, ak)["src"] == "station"  # no-derivatives reading: shown as is, never mixed in
assert sources.no_deriv("Korea Air Ministry of Environment") and not sources.no_deriv("AirGradient")
pend = [({"id": "a", "cc": "KR", "lat": 36.99, "lon": 127.93}, {"pm25": [100.0, 100.0], "pm10": [200.0, 200.0]}),
        ({"id": "far", "cc": "KR", "lat": 33.50, "lon": 126.53}, {"pm25": [100.0], "pm10": [200.0]})]
don = [({"id": "b", "cc": "KR", "lat": 37.57, "lon": 126.98}, {"steady_f": {"pm25": 0.3}})]
notes = []
build.borrow_fits(pend, don, notes)
assert pend[0][1]["pm25"] == [30.0, 30.0] and pend[0][1]["pm10"] == [60.0, 60.0] and pend[0][1]["fit_by"]["city"] == "b"
assert pend[1][1]["pm25"] == [100.0] and "fit" not in pend[1][1] and notes and "far" in notes[0]

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
                             "provider": {"name": getattr(fake_get_json, "prov", {}).get(i, "X")},
                             "licenses": [{"id": 2 if "Korea" in getattr(fake_get_json, "prov", {}).get(i, "") else 1,
                                           "name": "KOGL Type 3" if "Korea" in getattr(fake_get_json, "prov", {}).get(i, "")
                                           else "CC BY 4.0"}],
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
sources.LICENCES.update({1: {"name": "CC BY 4.0", "commercial": True, "modify": True, "share_alike": False},
                         2: {"name": "KOGL Type 3", "commercial": True, "modify": False, "share_alike": False},
                         3: {"name": "CC BY-NC 4.0", "commercial": False, "modify": True, "share_alike": False},
                         4: {"name": "CC BY-SA 4.0", "commercial": True, "modify": True, "share_alike": True}})
lu = sources.licence_use
assert lu({"licenses": []}) is None and lu({"licenses": [{"id": 3, "name": "CC BY-NC 4.0"}]}) is None
assert lu({"licenses": [{"id": 9, "name": "SINAICA terms"}]}) is None  # not described by OpenAQ
sources.LICENCES[5] = {"name": "Términos de uso SINAICA", "commercial": True, "modify": True, "share_alike": False}
assert lu({"licenses": [{"id": 5, "name": "Términos de uso SINAICA"}]}) is None  # custom terms: held back
sources.LICENCES[6] = {"name": "Korea Open Government License Type 3", "commercial": True, "modify": False, "share_alike": False}
assert lu({"licenses": [{"id": 6, "name": "Korea Open Government License Type 3"}]}) == "show"
assert lu({"licenses": [], "provider": {"name": "Korea Air Ministry of Environment"}}) == "show"  # checked at data.go.kr
sources.LICENCES[7] = {"name": "政府標準利用規約（第2.0版） (Government Standard Terms of Use v2.0)", "commercial": True,
                       "modify": True, "share_alike": False}
assert lu({"licenses": [{"id": 7, "name": "政府標準利用規約（第2.0版） (Government Standard Terms of Use v2.0)"}]}) == "fit"
assert lu({"licenses": [{"id": 4, "name": "CC BY-SA 4.0"}]}) == "show" and lu({"licenses": [{"id": 1, "name": "CC BY 4.0"}]}) == "fit"
assert lu({"licenses": [{"id": 1, "name": "CC BY 4.0"}], "provider": {"name": "AirKorea"}}) == "show"
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
fake_get_json.prov = {1: "Korea Air Ministry of Environment", 2: "AirGradient"}
fake_get_json.spec = {1: (True, 1, 37.57, 29.0), 2: (False, 1, 37.60, 12.0)}
r = q()
assert r["station"] == "loc1" and r["calib"]["station"] == "loc2" and r["calib"]["provider"] == "AirGradient", r
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
# Ledger: one snapshot per local 6-hour slot; errors only from later readings of the same station.
from pipeline import ledger  # noqa: E402

led = {}
city = {"id": "x", "tz": "Asia/Seoul"}
mdl = {"t0": iso(base + 10 * H), "pm25": [20.0] * 24, "pm10": [40.0] * 24,
       "fit_f": {"pm25": 0.3}, "steady_f": {"pm25": 0.5}}
src = {"at": iso(base + 9 * H), "station": "s", "pm25": 21.0, "pm10": 40.0}
t_run = base + 10 * H + dt.timedelta(minutes=5)  # 07:05 KST
assert ledger.slot(t_run, "Asia/Seoul") == "2026-11-04/1"
assert ledger.snapshot(led, city, mdl, src, iso(base), t_run)
assert not ledger.snapshot(led, city, mdl, src, iso(base), t_run + H)  # same slot: written once
r = led["x"][0]
assert r["t0"] == ledger._hours(base + 10 * H) and r["to"] == ledger._hours(base + 9 * H) and r["lag"] == 1.0
assert r["b"] == ledger._hours(base) and len(r["e25"]) == 24 and r["st"] == "s"
later = {"at": iso(base + 13 * H), "station": "s", "pm25": 30.0, "pm10": 40.0}
assert ledger.verify(led, "x", later, t_run + 3 * H) == 2
assert r["e25"][3] == round(100 * math.log(30 / 20)) and r["e10"][3] == 0
assert ledger.verify(led, "x", later, t_run + 3 * H) == 0                       # filled once
assert ledger.verify(led, "x", dict(later, station="other"), t_run + 3 * H) == 0  # another station
assert ledger.verify(led, "x", src, t_run) == 0                                 # the fitted reading is no test
assert ledger.verify(led, "x", dict(later, at=iso(base + 40 * H)), t_run) == 0  # beyond 24 h
assert ledger.snapshot(led, city, mdl, src, iso(base), t_run + 6 * H) and len(led["x"]) == 2
st = ledger.stats(led, "site")
assert st == {"from": "site", "cities": 1, "snapshots": 2, "errors_pm25": 1}, st
assert ledger.prune(led, t_run + dt.timedelta(days=ledger.KEEP_D + 2)) == {}
# Loading: a missing file starts a new ledger; an unreadable site falls back to the cached copy.
import json as _json  # noqa: E402
import tempfile  # noqa: E402

def _fail(msg):
    def f(*a, **k):
        raise RuntimeError(msg)
    return f

real = ledger.get_json
ledger.get_json = _fail("HTTP 404: not found")
assert ledger.load(fallback=Path("does-not-exist.json")) == ({}, "new")
with tempfile.TemporaryDirectory() as d:
    fb = Path(d) / "ledger.json"
    fb.write_text(_json.dumps(ledger.payload({"x": [{"t0": 1}]}, base)), encoding="utf-8")
    ledger.get_json = _fail("timed out")
    assert ledger.load(fallback=fb) == ({"x": [{"t0": 1}]}, "cache")
    assert ledger.load(fallback=Path(d) / "none.json") == ({}, "lost")
ledger.get_json = real

# AirKorea: KST hours (24:00 = next day), grades only for the hour shown, a newer hour replaces the OpenAQ copy.
from pipeline.common import clean, parse_iso  # noqa: E402

assert sources.airkorea_time("2026-11-10 20:00") == "2026-11-10T11:00:00Z"
assert sources.airkorea_time("2026-11-10 24:00") == "2026-11-10T15:00:00Z"
items = [{"stationName": "중구", "dataTime": "2026-11-10 20:00", "pm25Value": "18", "pm10Value": "24",
          "o3Value": "0.021", "no2Value": "-", "pm25Grade1h": "2", "pm10Grade1h": "1"},
         {"stationName": "점검", "dataTime": "2026-11-10 20:00", "pm25Value": "-", "pm10Value": "-", "pm25Grade1h": None},
         {"stationName": "", "dataTime": "2026-11-10 20:00"}, {"stationName": "x", "dataTime": None}]
sources.get_text = lambda url, params=None, **kw: _json.dumps(
    {"response": {"header": {"resultCode": "00"}, "body": {"items": items}}})
akr = sources.airkorea_now("k", tries=1)
assert set(akr) == {"중구", "점검"}, akr
assert akr["중구"]["grade"] == {"pm25": 2, "pm10": 1} and akr["중구"]["o3"] == 41.2 and akr["중구"]["no2"] is None
assert "grade" not in akr["점검"] and akr["점검"]["pm25"] is None
t_ak = parse_iso("2026-11-10T11:30:00Z")
a1 = {"src": "station", "station": "중구", "at": "2026-11-10T11:00:00Z", "pm25": 17.0, "pm10": 24.0}
assert build.official(a1, akr["중구"], t_ak) and a1["kr"] == {"pm25": 2, "pm10": 1} and a1["pm25"] == 18.0
a2 = {"src": "station", "station": "중구", "at": "2026-11-10T10:00:00Z", "pm25": 30.0, "pm10": 40.0, "age_h": 1.5}
assert build.official(a2, akr["중구"], t_ak) and a2["at"] == "2026-11-10T11:00:00Z" and a2["age_h"] == 0.5
assert a2["pm25"] == 18.0 and a2["o3"] == 41.2
a3 = {"src": "station", "station": "중구", "at": "2026-11-10T12:00:00Z", "pm25": 30.0, "pm10": 40.0}
assert not build.official(a3, akr["중구"], t_ak) and "kr" not in a3 and a3["pm25"] == 30.0  # older: OpenAQ stays
a4 = {"src": "station", "station": "점검", "at": "2026-11-10T11:00:00Z", "pm25": 9.0, "pm10": 20.0}
assert not build.official(a4, akr["점검"], t_ak) and a4["pm25"] == 9.0 and "kr" not in a4
assert not build.official(a4, None, t_ak)
sources.get_text = lambda url, params=None, **kw: ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
    "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg></cmmMsgHeader></OpenAPI_ServiceResponse>")
try:
    sources.airkorea_now("k", tries=1)
    raise AssertionError("no error")
except RuntimeError as e:
    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in str(e), e
# Keys never reach notes, even percent-encoded inside a URL.
os.environ["DATA_GO_KR_KEY"] = "abc+def/ghi=="
msg = clean("Max retries exceeded with url: /x?serviceKey=abc%2Bdef%2Fghi%3D%3D&pageNo=1 (abc+def/ghi==)")
assert "abc" not in msg and "pageNo=1" in msg, msg
assert "SERVICE_KEY" in clean("SERVICE_KEY_IS_NOT_REGISTERED_ERROR")

print("offline tests ok")
