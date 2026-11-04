# honest-air-data

Hourly open-data pipeline for the Honest Air app (Ditail Labs). It turns public air quality, weather, UV and earthquake feeds into small static JSON files. The app downloads one country file and picks the nearest city on the phone, so user location never leaves the device.

Files (published every hour):

- `v1/index.json`: cities and countries
- `v1/c/{CC}.json`: air, 24 h model forecast, weather. Air is a ground station read within 3 h (`src: station`); otherwise the CAMS model at this hour, fitted to a station read within 6 h when there is one (`src: fit`, the reading in `ref`), or the raw model (`src: model`). Reference monitors come before low-cost sensors, and a low-cost reading more than 5 times off the model is set aside as a likely fault. The forecast is scaled by the reading's ratio to the model, moving over about 12 h toward the city's steady ratio (median of the last 48 h).
- `v1/bias.json`: station / model ratios per city for the last 72 h, carried from run to run
- `v1/quakes.json`: M4.5+ earthquakes in the last 24 h
- `v1/status.json`: what worked in the last run

Data sources and attribution:

- Generated using Copernicus Atmosphere Monitoring Service information 2026 (CAMS global forecasts, CC-BY).
- OpenAQ (openaq.org); per-provider licenses apply.
- MET Norway Locationforecast (api.met.no), CC BY 4.0.
- 기상청 단기예보 조회서비스 (공공누리 제1유형). Official forecasts are relayed as issued.
- 出典：気象庁ホームページ. Official forecasts are relayed as issued.
- 한국환경공단 에어코리아 (공공누리 제3유형: 출처표시·변경금지). Values are shown exactly as published and are never used to fit the model or kept in `bias.json`; the forecast for Korean cities is fitted to other open stations nearby, or takes the steady ratio of the nearest such city within 200 km, or stays the raw CAMS model.
- U.S. Geological Survey earthquake feeds.

Licence and country rules (pre-launch review, 2026-11-04):

- A station is used only when OpenAQ lists a reviewed licence for it that allows commercial use. No licence, non-commercial or unreviewed custom terms: not used. No-derivatives (AirKorea) or share-alike: shown exactly as published, never used to fit the model.
- `policy.json` turns off forecasts, weather or earthquakes per country where a local licence is needed (Japan, Taiwan, Vietnam, Indonesia, Saudi Arabia, Kenya) and limits Türkiye to government monitors. Earthquakes in and around Korea are left out (지진관측법 §16).

Keys live in GitHub Actions secrets and are never written to this repository or to the published files.
