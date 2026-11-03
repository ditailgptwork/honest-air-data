"""Shared helpers: HTTP, time, units, weather code mapping.

Nothing here ever prints or stores key values. clean() masks any key that
slips into an error message before it reaches logs or output files.
"""
import datetime as dt
import math
import os
import time

import requests

UA = "HonestAir/0.3 (+https://github.com/ditailgptwork/honest-air-data; ditaillabs@gmail.com)"
SECRET_ENV = ("ADS_API_KEY", "OPENAQ_API_KEY", "FIRMS_MAP_KEY", "DATA_GO_KR_KEY", "CLOUDFLARE_API_TOKEN")

_session = requests.Session()
_session.headers["User-Agent"] = UA


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def clean(text):
    text = str(text)
    for name in SECRET_ENV:
        value = os.environ.get(name, "")
        if value and len(value) > 4:
            text = text.replace(value, "***")
    return " ".join(text.split())[:300]


def get_text(url, params=None, headers=None, timeout=30):
    r = _session.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def get_json(url, params=None, headers=None, timeout=30, tries=3, pause=2.0):
    last = None
    for attempt in range(tries):
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(pause * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(pause * (attempt + 1))
    raise RuntimeError(clean(last))


def haversine_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def apparent_temp(t, rh, ws):
    """Australian BoM apparent temperature in the shade. t in C, rh in %, ws in m/s."""
    if t is None:
        return None
    rh = 50.0 if rh is None else float(rh)
    ws = 0.0 if ws is None else float(ws)
    t = float(t)
    e = rh / 100 * 6.105 * math.exp(17.27 * t / (237.7 + t))
    return round(t + 0.33 * e - 0.70 * ws - 4.00, 1)


def r1(x):
    return None if x is None else round(float(x), 1)


def vmax(xs):
    xs = [x for x in xs if x is not None]
    return max(xs) if xs else None


def vmin(xs):
    xs = [x for x in xs if x is not None]
    return min(xs) if xs else None


# Weather codes are mapped to WMO 4677 style codes, the same set the app
# already understands (0 clear ... 95/99 thunder).
_MET = {
    "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45,
    "lightrain": 61, "rain": 63, "heavyrain": 65,
    "lightrainshowers": 80, "rainshowers": 81, "heavyrainshowers": 82,
    "lightsleet": 66, "sleet": 66, "heavysleet": 67,
    "lightsleetshowers": 66, "sleetshowers": 66, "heavysleetshowers": 67,
    "lightsnow": 71, "snow": 73, "heavysnow": 75,
    "lightsnowshowers": 85, "snowshowers": 85, "heavysnowshowers": 86,
}


def wmo_from_met(symbol):
    if not symbol:
        return None
    s = symbol.split("_")[0]
    if "thunder" in s:
        return 99 if s.startswith("heavy") else 95
    return _MET.get(s, 3)


def wmo_from_kma(sky, pty):
    pty = int(float(pty or 0))
    sky = int(float(sky or 1))
    if pty == 0:
        return {1: 0, 3: 2, 4: 3}.get(sky, 2)
    return {1: 63, 2: 66, 3: 73, 4: 80, 5: 51, 6: 66, 7: 71}.get(pty, 61)


def wmo_from_jma(code, text=""):
    if text and "雷" in text:
        return 95
    c = str(code or "")
    if not c:
        return None
    head = c[0]
    if head == "1":
        return 0 if c == "100" else 1
    if head == "2":
        return 3
    if head == "3":
        return 63
    if head == "4":
        return 73
    return 3
