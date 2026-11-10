"""Build the static files the app reads.

site/v1/index.json       cities and countries
site/v1/c/{CC}.json      everything for the cities of one country
site/v1/quakes.json      M4.5+ earthquakes, last 24 h
site/v1/ledger.json      corrected forecasts and their verified errors (shadow mode, not read by the app)
site/v1/status.json      what worked in this run (no keys, no personal data)

The app downloads one country file and picks the nearest city on the phone,
so no coordinates ever reach this server.
"""
import json
import math
import os
import sys
import time
from pathlib import Path

from . import bias, cams, ledger, sources
from .common import clean, haversine_km, iso, parse_iso, utcnow

SITE = Path("site/v1")
ATTR = {
    "cams": "Contains modified Copernicus Atmosphere Monitoring Service information 2026 (CC BY 4.0, "
            "https://creativecommons.org/licenses/by/4.0/), corrected with station readings. Neither the European "
            "Commission nor ECMWF is responsible for any use that may be made of the Copernicus information or "
            "data it contains.",
    "openaq": "Data: OpenAQ (openaq.org) and the original providers",
    "airkorea": "한국환경공단 에어코리아 (공공누리 제3유형: 출처표시·변경금지, www.airkorea.or.kr). 실시간 자료는 확정 전 자료예요.",
    "airnow": "Preliminary data from AirNow (US EPA) and local agencies; not fully verified, subject to change.",
    "eea": "European Environment Agency (latest data, not yet validated).",
    "MET Norway": "Based on data from MET Norway (CC BY 4.0, api.met.no).",
    "KMA": "자료: 기상청 단기예보 (공공누리 제1유형)",
    "JMA": "出典：気象庁ホームページ（https://www.jma.go.jp/）を加工して作成",
    "usgs": "Source: U.S. Geological Survey (earthquake.usgs.gov)",
}
POLICY = json.loads(Path("policy.json").read_text(encoding="utf-8")) if Path("policy.json").exists() else {}


def ratios_at_reading(model, obs, cache, city_id):
    """Reading / raw model at the time of the reading, per pollutant (call before fitting)."""
    t_obs = parse_iso(obs["at"]) if obs.get("at") else parse_iso(model["t0"])
    out = {}
    for k in ("pm25", "pm10"):
        series = model.get(k) or []
        m = cams.value_at(cache, city_id, k, t_obs) if cache else None
        if m is None and series:
            m = series[0]
        v = obs.get(k)
        if m and m > 0 and v is not None and v > 0:
            out[k] = v / m
    return out


def fit_model(model, obs, ratios, steady=None, tau_h=12.0, lo=0.1, hi=10.0):
    """Scale the model series to a measured value.

    CAMS can be several times off at a single point (Seoul, 2026-11-03: model 101 vs
    station 29 ug/m3), and much of that is steady. In log space the factor moves from
    the reading's ratio r0 toward the steady ratio rs: log f = log rs + (log r0 - log rs)
    * exp(-lag / tau), where lag is the hours between the reading and each forecast hour.
    rs is the median ratio of the last 48 h (bias.py); without enough history half of
    the log ratio is assumed steady."""
    t0 = parse_iso(model["t0"])
    t_obs = parse_iso(obs["at"]) if obs.get("at") else t0
    lag0 = max(0.0, (t0 - t_obs).total_seconds() / 3600)
    steady = steady or {}
    clamp = lambda x: min(hi, max(lo, x))  # noqa: E731
    for k in ("pm25", "pm10"):
        series = model.get(k) or []
        r0 = ratios.get(k) or (ratios.get("pm25") if k == "pm10" else None)
        if not series or not r0:
            continue
        l0 = math.log(clamp(r0))
        rs = steady.get(k) or (steady.get("pm25") if k == "pm10" else None)
        ls = math.log(clamp(rs)) if rs else 0.5 * l0
        model[k] = [round(x * math.exp(ls + (l0 - ls) * math.exp(-(lag0 + h) / tau_h)), 1)
                    for h, x in enumerate(series)]
        model["fit"] = True
        model.setdefault("fit_f", {})[k] = round(math.exp(l0), 2)
        model.setdefault("steady_f", {})[k] = round(math.exp(ls), 2)


def suspect(obs, ratios):
    """A low-cost sensor reading that looks like a fault rather than the air.

    Faulty sensors mostly read near zero (Lagos 0.5, Sao Paulo 1.5 ug/m3 against a
    model 10 to 25 times higher on 2026-11-03), while a plausible value far below the
    model is often the model's own bias (Beijing 50 against 263). So: near zero and
    well below the model, or wildly off either way. Reference monitors are trusted."""
    r, v = ratios.get("pm25"), (obs or {}).get("pm25")
    if not obs or obs.get("monitor") or r is None or v is None:
        return False
    return (v < 3 and r < 0.25) or r < 0.05 or r > 8


def official(air, row, now):
    """AirKorea's own latest hour for the station shown. A newer hour replaces the OpenAQ copy of it, and the
    1-hour grades go out as published (air["kr"]), only for the hour on screen. True when grades were added."""
    if not air or not row or not row.get("at"):
        return False
    at = air.get("at") or ""
    if row["at"] >= at and row.get("pm25") is not None and row.get("pm10") is not None:
        air.update({k: row.get(k) for k in ("pm25", "pm10", "o3", "no2")})
        if row["at"] > at:
            air.update(at=row["at"], age_h=round((now - parse_iso(row["at"])).total_seconds() / 3600, 2))
    if row["at"] == air.get("at") and row.get("grade"):
        air["kr"] = dict(row["grade"])
        return True
    return False


def choose_air(model, obs, fresh_h=sources.FRESH_H):
    """What the app shows as the air now.

    station: a reading within fresh_h, shown as measured.
    fit:     the model at this hour after fitting it to an older reading (ref).
    model:   the raw model, no usable station.
    An older reading with no model at all is still shown as measured, with its time."""
    if obs and (obs.get("age_h", 0) <= fresh_h or not obs.get("fit_ok", True)):
        return obs  # a no-derivatives reading is never mixed into the model: shown as is, with its time
    if model and model.get("pm25"):
        air = {"src": "fit" if obs else "model", "pm25": model["pm25"][0],
               "pm10": (model.get("pm10") or [None])[0], "at": model["t0"]}
        if obs:
            air["ref"] = {k: obs.get(k) for k in ("station", "dist_km", "provider", "pm25", "pm10", "at")}
        return air
    return obs


def is_airkorea(provider):
    """OpenAQ names AirKorea data 'Korea Air Ministry of Environment' (seen 2026-11-03)."""
    p = (provider or "").lower()
    return "airkorea" in p or ("korea" in p and "environment" in p)


def borrow_fits(pending, donors, notes, max_km=200.0):
    """Fit cities that had no open station of their own with the steady ratio of the
    nearest fitted city in the same country, within max_km. pending and donors are
    lists of (city, model)."""
    for c, m in pending:
        near = [(haversine_km(c["lat"], c["lon"], d["lat"], d["lon"]), d, dm)
                for d, dm in donors if d["cc"] == c["cc"] and d["id"] != c["id"]]
        km, d, dm = min(near, key=lambda x: x[0]) if near else (None, None, None)
        if km is None or km > max_km:
            notes.append(f"no open sensor near {c['id']}: forecast left as the raw model")
            continue
        f = dm.get("steady_f") or {}
        for k in ("pm25", "pm10"):
            fk = f.get(k) or f.get("pm25")
            if m.get(k) and fk:
                m[k] = [round(x * fk, 1) for x in m[k]]
        m.update(fit=True, steady_f=dict(f), fit_by={"city": d["id"], "km": round(km)})


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    now = utcnow()
    cities = json.loads(Path("cities.json").read_text(encoding="utf-8"))
    errors, notes = [], []
    cache, cams_updated = cams.refresh(cities, now, log=print)
    hist = bias.load()
    led, led_from = ledger.load()
    if led_from == "lost":
        errors.append("ledger: the published file and the cached copy were both unreadable; a new one starts")
    if os.environ.get("OPENAQ_API_KEY"):
        try:
            sources.load_licences(os.environ["OPENAQ_API_KEY"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"openaq licences: {clean(e)} (no station is used this run)")
    down = {}  # weather service -> failures in a row during this run
    donors, pending = [], []  # (city, model) fitted to an open station / waiting to borrow a fit
    oa_key = os.environ.get("OPENAQ_API_KEY")
    kr_key = os.environ.get("DATA_GO_KR_KEY")
    ak = {}
    if kr_key and any(c["cc"] == "KR" for c in cities):
        try:
            ak = sources.airkorea_now(kr_key)
            notes.append(f"airkorea: {len(ak)} stations, {sum(1 for v in ak.values() if 'grade' in v)} with 1-hour grades")
        except Exception as e:  # noqa: BLE001
            notes.append(f"airkorea: {clean(e)[:160]} (OpenAQ copy only, no grades this run)")
    n_kr = 0
    by_cc = {}
    for c in cities:
        rec = {k: c[k] for k in ("id", "name", "lat", "lon", "cc", "tz")}
        pol = POLICY.get(c["cc"], {})
        limits = [k for k in ("forecast", "weather", "quakes") if pol.get(k) is False]
        if limits:
            rec["limits"] = limits
        model = cams.series_from(cache, c["id"], now, hours=30)  # 30 h: the app shows 24 from the hour it opens
        if model and pol.get("uv") is False:
            model.pop("uv", None)  # no own UV outlook here (policy.json)
        obs = None
        if oa_key:
            try:
                obs = sources.openaq_nearest(c["lat"], c["lon"], oa_key, monitors_only=bool(pol.get("monitors_only")))
                if obs is None:
                    notes.append(f"no station {c['id']}: {dict(sources.LAST_DEBUG)}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"openaq {c['id']}: {clean(e)}")
            time.sleep(1.1)
        calib = obs.pop("calib", None) if obs else None
        if model and obs:
            # The model is fitted to the shown reading, or, when that one may not be
            # changed (AirKorea), to another open station nearby, unless policy.json says
            # the open sensors of that country read too far from its official monitors
            # (calib_fit: false); then the outlook stays the raw model.
            calib_ok = pol.get("calib_fit", True)
            src = obs if obs.get("fit_ok", True) else (calib if calib_ok else None)
            if src:
                ratios = ratios_at_reading(model, src, cache, c["id"])
                if suspect(src, ratios):
                    notes.append(f"suspect sensor {c['id']}: {src.get('station')} pm25 {src.get('pm25')}, "
                                 f"{round(ratios['pm25'], 2)} x model")
                    if src is obs:
                        obs = None
                else:
                    bias.add(hist, c["id"], src["at"], ratios, now)
                    fit_model(model, src, ratios, bias.steady(hist, c["id"], now))
                    model["fit_by"] = {k: src.get(k) for k in ("station", "provider", "dist_km")}
                    donors.append((c, model))
                    if "forecast" not in limits:
                        try:
                            ledger.verify(led, c["id"], src, now)
                            ledger.snapshot(led, c, model, src, (cache or {}).get("base"), now)
                        except Exception as e:  # noqa: BLE001
                            notes.append(f"ledger {c['id']}: {clean(e)[:120]}")
            elif calib_ok:
                pending.append((c, model))
        if "forecast" in limits:
            model = None  # forecasts need a local licence here (policy.json); show readings only
        air = choose_air(model, obs)
        if ak and air and air.get("src") == "station" and is_airkorea(air.get("provider")):
            n_kr += official(air, ak.get(air.get("station")), now)
        if air and air.get("src") == "fit":
            notes.append(f"fit only {c['id']}: {obs.get('station')} read {obs.get('age_h')} h ago")
        wx = None
        try:
            if "weather" in limits or (pol.get("wx_only") and c["wx"] != pol["wx_only"]):
                raise LookupError("skip")
            if c["wx"] == "kma" and kr_key and down.get("kma", 0) < 2:
                wx = sources.kma_weather(c["lat"], c["lon"], kr_key)
                down["kma"] = 0
            elif c["wx"] == "jma" and down.get("jma", 0) < 2:
                wx = sources.jma_weather(**c["jma"])
                down["jma"] = 0
        except LookupError:
            pass
        except Exception as e:  # noqa: BLE001
            # Two failures in a row: the service is down for this run, so the other
            # cities go straight to MET Norway instead of waiting on timeouts.
            down[c["wx"]] = down.get(c["wx"], 0) + 1
            alt = "" if pol.get("wx_only") else ", MET Norway instead"
            notes.append(f"wx {c['id']} {c['wx']} failed{alt}: {clean(e)[:120]}")
        if wx is None and "weather" not in limits and not pol.get("wx_only"):
            try:
                wx = sources.metno_weather(c["lat"], c["lon"])
                time.sleep(0.2)
            except Exception as e:  # noqa: BLE001
                errors.append(f"wx {c['id']}: {clean(e)}")
        if wx is not None:
            uv = (model or {}).get("uv") or []
            if pol.get("uv") is False:
                wx["uvmax"] = None
            else:
                wx["uvmax"] = max(uv) if uv else (None if "forecast" in limits else wx.get("uvmax_clear"))
        rec.update(air=air, model=model, wx=wx)
        by_cc.setdefault(c["cc"], []).append(rec)

    borrow_fits(pending, donors, notes)
    if ak:
        notes.append(f"airkorea grades on {n_kr} Korean cities")
    ok_air = sum(1 for recs in by_cc.values() for r in recs if r["air"])
    ok_wx = sum(1 for recs in by_cc.values() for r in recs if r["wx"])
    if ok_air == 0 and ok_wx == 0:
        print("nothing usable, keeping the previous deployment")
        for e in errors[:20]:
            print(" -", e)
        sys.exit(1)

    for cc, recs in by_cc.items():
        used, providers = set(), set()
        for r in recs:
            if r["model"]:
                used.add("cams")
            a = r["air"] or {}
            st = a if a.get("src") == "station" else a.get("ref")
            fb = (r["model"] or {}).get("fit_by") or {}
            for prov in [(st or {}).get("provider"), fb.get("provider")]:
                if not prov:
                    continue
                used.add("openaq")
                low = prov.lower()
                if is_airkorea(prov):
                    used.add("airkorea")
                if "airnow" in low:
                    used.add("airnow")
                if "eea" in low or "european environment" in low:
                    used.add("eea")
                providers.add(prov)
            if r["wx"]:
                used.add(r["wx"]["src"])
        write(SITE / "c" / f"{cc}.json", {"v": 1, "generated": iso(now), "cc": cc,
                                          "attr": [ATTR[k] for k in ATTR if k in used]
                                          + [f"Station data: {p} via OpenAQ" for p in sorted(providers)],
                                          "cities": recs})
    write(SITE / "index.json", {
        "v": 1, "generated": iso(now), "countries": sorted(by_cc),
        "cities": [{k: c[k] for k in ("id", "name", "cc", "lat", "lon")} for c in cities]})
    write(SITE / "bias.json", {"v": bias.VERSION, "generated": iso(now), "keep_h": bias.KEEP_H,
                               "cities": bias.prune(hist, now)})
    try:
        book = ledger.payload(ledger.prune(led, now), now)
        write(SITE / "ledger.json", book)
        write(Path("cache") / "ledger.json", book)  # travels in the Actions cache as a second copy
    except Exception as e:  # noqa: BLE001
        errors.append(f"ledger: {clean(e)}")
    try:
        write(SITE / "quakes.json", {"v": 1, "generated": iso(now), "attr": [ATTR["usgs"]],
                                     "quakes": sources.usgs_quakes()[:60]})
    except Exception as e:  # noqa: BLE001
        errors.append(f"usgs: {clean(e)}")
    srcs = [r["air"]["src"] for recs in by_cc.values() for r in recs if r["air"]]
    n_station, n_fit = srcs.count("station"), srcs.count("fit")
    fits = {r["id"]: {"now": (r["model"].get("fit_f") or {}).get("pm25"),
                      "steady": (r["model"].get("steady_f") or {}).get("pm25"),
                      "by": (r["model"].get("fit_by") or {}).get("city") or (r["model"].get("fit_by") or {}).get("provider")}
            for recs in by_cc.values() for r in recs if r["model"] and r["model"].get("fit")}
    if sources.HELD:
        notes.append("licences held back: " + ", ".join(f"{k} x{v}" for k, v in
                                                     sorted(sources.HELD.items(), key=lambda kv: -kv[1])[:8]))
    write(SITE / "status.json", {"v": 1, "generated": iso(now), "cams_base": (cache or {}).get("base"),
                                 "cities": len(cities), "air": ok_air, "station": n_station, "fit": n_fit,
                                 "weather": ok_wx, "fit_f": fits, "ledger": ledger.stats(led, led_from),
                                 "errors": errors[:40], "notes": notes[:60]})
    summary = (f"cities {len(cities)} | air {ok_air} (station {n_station}, fit {n_fit}) | weather {ok_wx} | "
               f"CAMS {(cache or {}).get('base')} | errors {len(errors)} | notes {len(notes)}")
    print(summary)
    for e in errors[:40] + notes[:60]:
        print(" -", e)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"cams_updated={'true' if cams_updated else 'false'}\n")
    gh_sum = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_sum:
        with open(gh_sum, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n\n" + "\n".join(f"- {e}" for e in errors[:40] + notes[:60]) + "\n")


if __name__ == "__main__":
    main()
