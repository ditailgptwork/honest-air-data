"""Build the static files the app reads.

site/v1/index.json       cities and countries
site/v1/c/{CC}.json      everything for the cities of one country
site/v1/quakes.json      M4.5+ earthquakes, last 24 h
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

from . import cams, sources
from .common import clean, iso, parse_iso, utcnow

SITE = Path("site/v1")
ATTR = {
    "cams": "Generated using Copernicus Atmosphere Monitoring Service information 2026",
    "openaq": "OpenAQ (openaq.org)",
    "airkorea": "한국환경공단 에어코리아 (공공누리 제3유형)",
    "MET Norway": "MET Norway Locationforecast (CC BY 4.0)",
    "KMA": "기상청 단기예보",
    "JMA": "出典：気象庁ホームページ",
    "usgs": "U.S. Geological Survey",
}


def fit_model(model, obs, cache=None, city_id=None, tau_h=12.0):
    """Scale the model series to a measured value, fading back to the raw model.

    CAMS can be several times off at a single point (Seoul, 2026-11-03: model 101 vs
    station 29 ug/m3). The factor f0 compares the reading with the model at the time
    of the reading, and it fades from that time: 1 + (f0 - 1) * exp(-lag / tau),
    where lag is the hours between the reading and each forecast hour."""
    t0 = parse_iso(model["t0"])
    t_obs = parse_iso(obs["at"]) if obs.get("at") else t0
    lag0 = max(0.0, (t0 - t_obs).total_seconds() / 3600)
    f25 = None
    for k in ("pm25", "pm10"):
        series = model.get(k) or []
        if not series:
            continue
        m_obs = cams.value_at(cache, city_id, k, t_obs) if cache else None
        if m_obs is None:
            m_obs = series[0]
        v = obs.get(k)
        f0 = None
        if m_obs and m_obs > 0 and v is not None:
            f0 = max(0.2, min(5.0, v / m_obs))
        elif k == "pm10" and f25 is not None:
            f0 = f25
        if f0 is None:
            continue
        if k == "pm25":
            f25 = f0
        model[k] = [round(x * (1 + (f0 - 1) * math.exp(-(lag0 + h) / tau_h)), 1) for h, x in enumerate(series)]
        model["fit"] = True
        model.setdefault("fit_f", {})[k] = round(f0, 2)


def choose_air(model, obs, fresh_h=sources.FRESH_H):
    """What the app shows as the air now.

    station: a reading within fresh_h, shown as measured.
    fit:     the model at this hour after fitting it to an older reading (ref).
    model:   the raw model, no usable station.
    An older reading with no model at all is still shown as measured, with its time."""
    if obs and obs.get("age_h", 0) <= fresh_h:
        return obs
    if model and model.get("pm25"):
        air = {"src": "fit" if obs else "model", "pm25": model["pm25"][0],
               "pm10": (model.get("pm10") or [None])[0], "at": model["t0"]}
        if obs:
            air["ref"] = {k: obs.get(k) for k in ("station", "dist_km", "provider", "pm25", "pm10", "at")}
        return air
    return obs


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    now = utcnow()
    cities = json.loads(Path("cities.json").read_text(encoding="utf-8"))
    errors, notes = [], []
    cache, cams_updated = cams.refresh(cities, now, log=print)
    oa_key = os.environ.get("OPENAQ_API_KEY")
    kr_key = os.environ.get("DATA_GO_KR_KEY")
    by_cc = {}
    for c in cities:
        rec = {k: c[k] for k in ("id", "name", "lat", "lon", "cc", "tz")}
        model = cams.series_from(cache, c["id"], now)
        obs = None
        if oa_key:
            try:
                obs = sources.openaq_nearest(c["lat"], c["lon"], oa_key)
                if obs is None:
                    notes.append(f"no station {c['id']}: {dict(sources.LAST_DEBUG)}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"openaq {c['id']}: {clean(e)}")
            time.sleep(1.1)
        if model and obs:
            fit_model(model, obs, cache, c["id"])
        air = choose_air(model, obs)
        if air and air.get("src") == "fit":
            notes.append(f"fit only {c['id']}: {obs.get('station')} read {obs.get('age_h')} h ago")
        wx = None
        try:
            if c["wx"] == "kma":
                wx = sources.kma_weather(c["lat"], c["lon"], kr_key) if kr_key else None
            elif c["wx"] == "jma":
                wx = sources.jma_weather(**c["jma"])
            else:
                wx = sources.metno_weather(c["lat"], c["lon"])
                time.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            errors.append(f"wx {c['id']}: {clean(e)}")
        if wx is not None:
            uv = (model or {}).get("uv") or []
            wx["uvmax"] = max(uv) if uv else wx.get("uvmax_clear")
        rec.update(air=air, model=model, wx=wx)
        by_cc.setdefault(c["cc"], []).append(rec)

    ok_air = sum(1 for recs in by_cc.values() for r in recs if r["air"])
    ok_wx = sum(1 for recs in by_cc.values() for r in recs if r["wx"])
    if ok_air == 0 and ok_wx == 0:
        print("nothing usable, keeping the previous deployment")
        for e in errors[:20]:
            print(" -", e)
        sys.exit(1)

    for cc, recs in by_cc.items():
        used = set()
        for r in recs:
            if r["model"]:
                used.add("cams")
            a = r["air"] or {}
            st = a if a.get("src") == "station" else a.get("ref")
            if st:
                used.add("openaq")
                if (st.get("provider") or "").lower().startswith("airkorea"):
                    used.add("airkorea")
            if r["wx"]:
                used.add(r["wx"]["src"])
        write(SITE / "c" / f"{cc}.json", {"v": 1, "generated": iso(now), "cc": cc,
                                          "attr": [ATTR[k] for k in ATTR if k in used], "cities": recs})
    write(SITE / "index.json", {
        "v": 1, "generated": iso(now), "countries": sorted(by_cc),
        "cities": [{k: c[k] for k in ("id", "name", "cc", "lat", "lon")} for c in cities]})
    try:
        write(SITE / "quakes.json", {"v": 1, "generated": iso(now), "attr": [ATTR["usgs"]],
                                     "quakes": sources.usgs_quakes()[:60]})
    except Exception as e:  # noqa: BLE001
        errors.append(f"usgs: {clean(e)}")
    srcs = [r["air"]["src"] for recs in by_cc.values() for r in recs if r["air"]]
    n_station, n_fit = srcs.count("station"), srcs.count("fit")
    fits = {r["id"]: r["model"].get("fit_f") for recs in by_cc.values() for r in recs
            if r["model"] and r["model"].get("fit_f")}
    write(SITE / "status.json", {"v": 1, "generated": iso(now), "cams_base": (cache or {}).get("base"),
                                 "cities": len(cities), "air": ok_air, "station": n_station, "fit": n_fit,
                                 "weather": ok_wx, "fit_f": fits, "errors": errors[:40], "notes": notes[:60]})
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
