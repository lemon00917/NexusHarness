---
name: weather
description: Get current weather and forecasts for any city worldwide (no API key required). Supports Chinese city names via pinyin or English.
homepage: https://wttr.in/:help
metadata: {"clawdbot":{"emoji":"🌤️","safety":"AUTO_APPROVE","requires":{"bins":["curl"]}}}
---

# Weather

Quick one-liner:
```bash
curl -s "wttr.in/{input}?format=3"
```

Compact format:
```bash
curl -s "wttr.in/{input}?format=%l:+%c+%t+%h+%w"
```

Full forecast:
```bash
curl -s "wttr.in/{input}?T"
```

Tips:
- Chinese cities: use pinyin (南京→Nanjing, 北京→Beijing, 上海→Shanghai, 西安→Xian)
- URL-encode spaces: `wttr.in/New+York`
- Airport codes: `wttr.in/JFK`
- Units: `?m` (metric) `?u` (USCS)
- Today only: `?1` · Current only: `?0`
- PNG: `curl -s "wttr.in/Berlin.png" -o /tmp/weather.png`

## Open-Meteo (fallback, JSON)

Get coordinates first, then query weather:

```bash
curl -s "https://geocoding-api.open-meteo.com/v1/search?name={input}&count=1"
```

```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
```

Docs: https://open-meteo.com/en/docs