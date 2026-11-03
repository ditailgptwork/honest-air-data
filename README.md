# honest-air-data

Hourly open-data pipeline for the Honest Air app (Ditail Labs). It turns public air quality, weather, UV, earthquake and radiation feeds into small static JSON files. The app reads only those files, so user location never leaves the phone.

Data sources and attribution:

- Generated using Copernicus Atmosphere Monitoring Service information 2026 (CAMS global forecasts, CC-BY).
- OpenAQ (openaq.org), per-provider licenses apply.
- MET Norway Locationforecast (api.met.no), CC BY 4.0.
- U.S. Geological Survey earthquake feeds.
- 한국환경공단 에어코리아 대기오염정보 (공공누리 제3유형: 출처표시·변경금지, values are shown unmodified).
- 기상청 단기예보 조회서비스 (공공누리 제1유형: 출처표시).
- 한국수력원자력 원자력발전소 실시간 주변 방사선량.
- NASA FIRMS active fire data.

Keys live in GitHub Actions secrets and are never written to this repository.
