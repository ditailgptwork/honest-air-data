"""Data sources. Each function returns a small dict or None and raises on hard errors.

- OpenAQ v3: nearest fresh ground station (PM2.5, PM10, O3, NO2).
- MET Norway Locationforecast: weather outside Korea and Japan (CC BY 4.0).
- KMA short-range forecast: weather in Korea (official forecasts are relayed as issued).
- JMA forecast + AMeDAS: weather in Japan (official forecasts are relayed as issued).
- USGS: M4.5+ earthquakes in the last 24 hours.
"""
import datetime as dt
import math
import time

from .common import (apparent_temp, get_json, get_text, haversine_km, iso, parse_iso, r1, utcnow,
                     vmax, vmin, wmo_from_jma, wmo_from_kma, wmo_from_met)

# ---------------------------------------------------------------- OpenAQ
OPENAQ = "https://api.openaq.org/v3"
UG_PER_PPM = {"o3": 1963.0, "no2": 1881.0}
WANT = ("pm25", "pm10", "o3", "no2")
SANE_MAX = {"pm25": 1500.0, "pm10": 3000.0, "o3": 1500.0, "no2": 1500.0}


LAST_DEBUG = {}
FRESH_H = 3.0   # a reading this recent is shown as the value now
CALIB_H = 6.0   # an older reading, up to this age, only corrects the model


def openaq_nearest(lat, lon, key, radius_m=25000, max_age_h=CALIB_H, fresh_h=FRESH_H, pause=1.1, tries=3):
    """Best nearby station with a PM2.5 reading no older than max_age_h.

    Order: reference monitors before low-cost sensors, a reading within fresh_h
    before an older one, then distance. Up to `tries` stations are asked for their
    latest values; the first with a usable PM2.5 wins. The result carries age_h
    (hours since the PM2.5 reading) so the caller decides whether to show it as
    the value now or use it only to correct the model."""
    headers = {"X-API-Key": key}
    j = get_json(f"{OPENAQ}/locations",
                 {"coordinates": f"{lat:.4f},{lon:.4f}", "radius": radius_m, "limit": 100}, headers=headers)
    now = utcnow()
    cands = []
    ages = []
    results = j.get("results", [])
    for loc in results:
        sensors = {s["id"]: s.get("parameter") or {} for s in loc.get("sensors", [])}
        if not any(p.get("name") == "pm25" for p in sensors.values()):
            continue
        last = (loc.get("datetimeLast") or {}).get("utc")
        age = None if not last else (now - parse_iso(last)).total_seconds() / 3600
        if age is not None:
            ages.append(age)
        if age is None or age > max_age_h:
            continue
        c = loc.get("coordinates") or {}
        d = haversine_km(lat, lon, c.get("latitude", lat), c.get("longitude", lon))
        cands.append(((0 if loc.get("isMonitor") else 1, 0 if age <= fresh_h else 1, d), d, loc, sensors))
    cands.sort(key=lambda x: x[0])
    LAST_DEBUG.clear()
    LAST_DEBUG.update({"locations": len(results), "pm25_locations": len(ages), "usable": len(cands),
                       "monitors": sum(1 for x in cands if x[0][0] == 0),
                       "freshest_h": round(min(ages), 1) if ages else None})
    for _, d, loc, sensors in cands[:tries]:
        time.sleep(pause)
        lj = get_json(f"{OPENAQ}/locations/{loc['id']}/latest", headers=headers)
        out = _reading(lj, loc, sensors, d, now, max_age_h, fresh_h)
        if out:
            return out
    return None


def _reading(lj, loc, sensors, d, now, max_age_h, fresh_h):
    vals = {}
    for r in lj.get("results", []):
        p = sensors.get(r.get("sensorsId")) or {}
        name = p.get("name")
        if name not in WANT:
            continue
        v = r.get("value")
        t = (r.get("datetime") or {}).get("utc")
        # A PM reading of exactly 0 is a sensor fault in practice; gases can be 0.
        if v is None or v < 0 or (v == 0 and name in ("pm25", "pm10")) or not t:
            continue
        age = (now - parse_iso(t)).total_seconds() / 3600
        if age > max_age_h:
            continue
        unit = (p.get("units") or "").lower()
        if name in UG_PER_PPM:
            if unit == "ppm":
                v *= UG_PER_PPM[name]
            elif unit == "ppb":
                v *= UG_PER_PPM[name] / 1000
        if v > SANE_MAX[name]:
            continue
        vals[name] = (r1(v), t, age)
    if "pm25" not in vals:
        return None
    _, t25, a25 = vals["pm25"]
    limit = fresh_h if a25 <= fresh_h else max_age_h
    out = {"src": "station", "station": loc.get("name"), "dist_km": round(d, 1),
           "provider": (loc.get("provider") or {}).get("name"), "monitor": bool(loc.get("isMonitor")),
           "at": t25, "age_h": round(max(a25, 0.0), 2)}
    for name, (v, _, age) in vals.items():
        if age <= limit:
            out[name] = v
    return out


# ---------------------------------------------------------------- MET Norway
def metno_weather(lat, lon):
    j = get_json("https://api.met.no/weatherapi/locationforecast/2.0/complete",
                 {"lat": round(lat, 4), "lon": round(lon, 4)})
    now = utcnow()
    rows = [t for t in j["properties"]["timeseries"]
            if parse_iso(t["time"]) >= now - dt.timedelta(minutes=59)][:24]
    if not rows:
        return None

    def det(t, k):
        return t["data"]["instant"]["details"].get(k)

    def nxt(t):
        return t["data"].get("next_1_hours") or t["data"].get("next_6_hours") or {}

    cur = rows[0]
    temps = [det(t, "air_temperature") for t in rows]
    feels = [apparent_temp(det(t, "air_temperature"), det(t, "relative_humidity"), det(t, "wind_speed"))
             for t in rows]
    pops = [(nxt(t).get("details") or {}).get("probability_of_precipitation") for t in rows]
    uv = [det(t, "ultraviolet_index_clear_sky") for t in rows]
    rain = [(t["data"].get("next_1_hours") or {}).get("details", {}).get("precipitation_amount") for t in rows]
    t0 = det(cur, "air_temperature")
    return {
        "src": "MET Norway", "at": cur["time"],
        "temp": r1(t0), "feels": apparent_temp(t0, det(cur, "relative_humidity"), det(cur, "wind_speed")),
        "hum": r1(det(cur, "relative_humidity")),
        "wmo": wmo_from_met((nxt(cur).get("summary") or {}).get("symbol_code")),
        "tmax": r1(vmax(temps)), "tmin": r1(vmin(temps)), "fmax": r1(vmax(feels)), "fmin": r1(vmin(feels)),
        "pop": None if vmax(pops) is None else int(round(vmax(pops))),
        "uvmax_clear": r1(vmax(uv)),
        "rain_mm": r1(sum(x for x in rain if x)), "rain_h": sum(1 for x in rain if x and x >= 0.1),
    }


# ---------------------------------------------------------------- KMA (Korea)
KMA = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
KMA_TIMEOUT = (6, 20)  # data.go.kr is sometimes unreachable from abroad: fail fast, fall back to MET Norway


def kma_grid(lat, lon):
    """Lat/lon to KMA 5 km Lambert grid (nx, ny), from the KMA open API guide."""
    re_, grid, slat1, slat2, olon, olat, xo, yo = 6371.00877, 5.0, 30.0, 60.0, 126.0, 38.0, 43, 136
    rad = math.pi / 180.0
    re_ /= grid
    slat1, slat2, olon, olat = slat1 * rad, slat2 * rad, olon * rad, olat * rad
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5))
    sf = math.pow(math.tan(math.pi * 0.25 + slat1 * 0.5), sn) * math.cos(slat1) / sn
    ro = re_ * sf / math.pow(math.tan(math.pi * 0.25 + olat * 0.5), sn)
    ra = re_ * sf / math.pow(math.tan(math.pi * 0.25 + lat * rad * 0.5), sn)
    theta = lon * rad - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    return int(math.floor(ra * math.sin(theta) + xo + 0.5)), int(math.floor(ro - ra * math.cos(theta) + yo + 0.5))


def _kma_items(j):
    head = j["response"]["header"]
    if head.get("resultCode") != "00":
        raise RuntimeError(f"KMA {head.get('resultCode')} {head.get('resultMsg')}")
    return j["response"]["body"]["items"]["item"]


def kma_weather(lat, lon, key):
    nx, ny = kma_grid(lat, lon)
    kst = utcnow() + dt.timedelta(hours=9)
    b = kst - dt.timedelta(minutes=45)
    now_items = _kma_items(get_json(f"{KMA}/getUltraSrtNcst", {
        "serviceKey": key, "pageNo": 1, "numOfRows": 30, "dataType": "JSON",
        "base_date": b.strftime("%Y%m%d"), "base_time": b.strftime("%H00"), "nx": nx, "ny": ny},
        timeout=KMA_TIMEOUT, tries=2))
    cur = {i["category"]: i["obsrValue"] for i in now_items}
    b2 = kst - dt.timedelta(minutes=15)
    hours = [h for h in (2, 5, 8, 11, 14, 17, 20, 23) if h <= b2.hour]
    if hours:
        bd, bt = b2.strftime("%Y%m%d"), f"{max(hours):02d}00"
    else:
        bd, bt = (b2 - dt.timedelta(days=1)).strftime("%Y%m%d"), "2300"
    fc_items = _kma_items(get_json(f"{KMA}/getVilageFcst", {
        "serviceKey": key, "pageNo": 1, "numOfRows": 1000, "dataType": "JSON",
        "base_date": bd, "base_time": bt, "nx": nx, "ny": ny},
        timeout=KMA_TIMEOUT, tries=2))
    fc = {}
    for i in fc_items:
        fc.setdefault(i["fcstDate"] + i["fcstTime"], {})[i["category"]] = i["fcstValue"]
    now_key = kst.strftime("%Y%m%d%H00")
    keys = [k for k in sorted(fc) if k >= now_key][:24]
    num = lambda v: None if v in (None, "", "-") else float(v)  # noqa: E731
    temps = [num(fc[k].get("TMP")) for k in keys]
    feels = [apparent_temp(num(fc[k].get("TMP")), num(fc[k].get("REH")), num(fc[k].get("WSD"))) for k in keys]
    pops = [num(fc[k].get("POP")) for k in keys]
    t0, rh, ws = num(cur.get("T1H")), num(cur.get("REH")), num(cur.get("WSD"))
    sky = fc[keys[0]].get("SKY") if keys else None
    return {
        "src": "KMA", "at": iso(b.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=9)),
        "temp": r1(t0), "feels": apparent_temp(t0, rh, ws), "hum": r1(rh),
        "wmo": wmo_from_kma(sky, cur.get("PTY")),
        "tmax": r1(vmax(temps + [t0])), "tmin": r1(vmin(temps + [t0])),
        "fmax": r1(vmax(feels)), "fmin": r1(vmin(feels)),
        "pop": None if vmax(pops) is None else int(vmax(pops)),
        "grid": [nx, ny],
    }


# ---------------------------------------------------------------- JMA (Japan)
JMA = "https://www.jma.go.jp/bosai"


def jma_weather(office, temp_area, amedas):
    f = get_json(f"{JMA}/forecast/data/forecast/{office}.json")
    ts = f[0]["timeSeries"]
    a0 = ts[0]["areas"][0]
    pops = [float(p) for p in ts[1]["areas"][0].get("pops", []) if p not in ("", None)][:4]
    temps = []
    for a in ts[2]["areas"]:
        if a["area"]["code"] == temp_area:
            temps = [float(x) for x in a.get("temps", []) if x not in ("", None)]
    latest = parse_iso(get_text(f"{JMA}/amedas/data/latest_time.txt").strip())
    block = latest.replace(hour=latest.hour - latest.hour % 3, minute=0, second=0, microsecond=0)
    pj = get_json(f"{JMA}/amedas/data/point/{amedas}/{block.strftime('%Y%m%d_%H')}.json")
    last = pj[max(pj)]
    first = lambda k: (last.get(k) or [None])[0]  # noqa: E731
    t0, rh, ws = first("temp"), first("humidity"), first("wind")
    tmax, tmin = vmax(temps + [t0]), vmin(temps + [t0])
    return {
        "src": "JMA", "at": iso(latest),
        "temp": r1(t0), "feels": apparent_temp(t0, rh, ws), "hum": r1(rh),
        "wmo": wmo_from_jma(a0.get("weatherCodes", [None])[0], (a0.get("weathers") or [""])[0]),
        "tmax": r1(tmax), "tmin": r1(tmin),
        "fmax": apparent_temp(tmax, rh, ws), "fmin": apparent_temp(tmin, rh, ws),
        "pop": None if not pops else int(max(pops)),
    }


# ---------------------------------------------------------------- USGS
def usgs_quakes():
    j = get_json("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson")
    out = []
    for feat in j.get("features", []):
        p = feat.get("properties") or {}
        c = (feat.get("geometry") or {}).get("coordinates") or [None, None, None]
        if p.get("time") is None:
            continue
        out.append({"mag": p.get("mag"), "place": p.get("place"),
                    "t": iso(dt.datetime.fromtimestamp(p["time"] / 1000, dt.timezone.utc)),
                    "lat": c[1], "lon": c[0], "depth_km": c[2]})
    return sorted(out, key=lambda q: q["t"], reverse=True)
