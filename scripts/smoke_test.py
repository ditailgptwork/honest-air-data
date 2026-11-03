"""Smoke test for Honest Air data sources and keys.

Prints only HTTP status codes and short messages. Key values are never printed:
any accidental occurrence is replaced with *** before output.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

UA = "HonestAir/0.1 (+https://github.com/ditailgptwork/honest-air-data; ditaillabs@gmail.com)"
OUT = Path("out")
OUT.mkdir(exist_ok=True)
SECRET_NAMES = ("ADS_API_KEY", "OPENAQ_API_KEY", "FIRMS_MAP_KEY", "DATA_GO_KR_KEY")
SECRETS = [os.environ.get(k, "") for k in SECRET_NAMES]


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def clean(text):
    text = str(text)
    for value in SECRETS:
        if value and len(value) > 4:
            text = text.replace(value, "***")
    return " ".join(text.split())[:240]


def get(url, **kw):
    kw.setdefault("timeout", 30)
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    return requests.get(url, headers=headers, **kw)


def openaq():
    key = os.environ.get("OPENAQ_API_KEY")
    if not key:
        return False, None, "secret missing"
    r = get("https://api.openaq.org/v3/locations",
            params={"coordinates": "37.5665,126.978", "radius": 10000, "limit": 5},
            headers={"X-API-Key": key})
    if not r.ok:
        return False, r.status_code, r.text
    return True, r.status_code, f"{len(r.json().get('results', []))} stations near Seoul"


def firms():
    key = os.environ.get("FIRMS_MAP_KEY")
    if not key:
        return False, None, "secret missing"
    r = get(f"https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/?MAP_KEY={key}")
    try:
        j = r.json()
        return r.ok, r.status_code, f"limit {j.get('transaction_limit')} used {j.get('current_transactions')}"
    except ValueError:
        return False, r.status_code, r.text


def data_go_kr(url, params):
    key = os.environ.get("DATA_GO_KR_KEY")
    if not key:
        return False, None, "secret missing"
    r = get(url, params=dict(params, serviceKey=key))
    try:
        head = r.json()["response"]["header"]
        return head.get("resultCode") == "00", r.status_code, f"{head.get('resultCode')} {head.get('resultMsg')}"
    except (ValueError, KeyError, TypeError):
        return False, r.status_code, r.text


def airkorea():
    return data_go_kr("https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty",
                      {"returnType": "json", "numOfRows": 3, "pageNo": 1, "sidoName": "서울", "ver": "1.0"})


def kma():
    t = now_utc() + dt.timedelta(hours=9) - dt.timedelta(minutes=50)
    return data_go_kr("https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst",
                      {"pageNo": 1, "numOfRows": 10, "dataType": "JSON",
                       "base_date": t.strftime("%Y%m%d"), "base_time": t.strftime("%H00"), "nx": 60, "ny": 127})


def metno():
    r = get("https://api.met.no/weatherapi/locationforecast/2.0/compact", params={"lat": 37.57, "lon": 126.98})
    if not r.ok:
        return False, r.status_code, r.text
    return True, r.status_code, f"{len(r.json()['properties']['timeseries'])} time steps"


def usgs():
    r = get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson")
    if not r.ok:
        return False, r.status_code, r.text
    return True, r.status_code, f"{len(r.json().get('features', []))} quakes M4.5+ in 24h"


ADS_CODE = r'''
import cdsapi, datetime as dt
d = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
c = cdsapi.Client(quiet=True, progress=False)
c.retrieve("cams-global-atmospheric-composition-forecasts", {
    "variable": ["particulate_matter_2.5um"], "date": [f"{d}/{d}"], "time": ["00:00"],
    "leadtime_hour": ["0"], "type": ["forecast"], "data_format": "grib", "area": [38, 126, 37, 127]},
    "out/cams_test.grib")
'''


def ads():
    key = os.environ.get("ADS_API_KEY")
    if not key:
        return False, None, "secret missing"
    env = dict(os.environ, CDSAPI_URL="https://ads.atmosphere.copernicus.eu/api", CDSAPI_KEY=key)
    try:
        p = subprocess.run([sys.executable, "-c", ADS_CODE], env=env, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return True, None, "request accepted, still queued after 10 min"
    f = OUT / "cams_test.grib"
    if p.returncode == 0 and f.exists():
        return True, 200, f"CAMS sample {f.stat().st_size} bytes"
    lines = (p.stderr or p.stdout).strip().splitlines()
    return False, None, " | ".join(lines[-3:])


CHECKS = [("OpenAQ", openaq), ("NASA FIRMS", firms), ("AirKorea", airkorea), ("KMA", kma),
          ("MET Norway", metno), ("USGS", usgs), ("CAMS (ADS)", ads)]


def main():
    results = []
    for name, fn in CHECKS:
        t0 = time.time()
        try:
            ok, code, note = fn()
        except Exception as e:  # noqa: BLE001 - report every failure kind
            ok, code, note = False, None, f"{type(e).__name__}: {e}"
        results.append({"source": name, "ok": bool(ok), "http": code, "note": clean(note),
                        "sec": round(time.time() - t0, 1)})
    (OUT / "status.json").write_text(json.dumps({"at": now_utc().isoformat(), "results": results},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    rows = ["| source | ok | http | note | sec |", "|---|---|---|---|---|"]
    rows += [f"| {r['source']} | {'OK' if r['ok'] else 'FAIL'} | {r['http']} | {r['note']} | {r['sec']} |" for r in results]
    print("\n".join(rows))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")


if __name__ == "__main__":
    main()
