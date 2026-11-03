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
print("offline tests ok")
