---
name: weather
description: Get current weather and forecasts for any city worldwide (no API key required). Uses wttr.in service.
homepage: https://wttr.in/:help
metadata: {"clawdbot":{"emoji":"🌤️","safety":"AUTO_APPROVE","requires":{"bins":["curl"]}}}
---

# Weather

Two free services, no API keys needed.

## wttr.in (primary)

Quick one-liner:
```bash
curl -s "wttr.in/{city}?format=3"
# Output: Tokyo: ⛅️ +28°C
```

Compact format:
```bash
curl -s "wttr.in/{city}?format=%l:+%c+%t+%h+%w"
# Output: Tokyo: ⛅️ +28°C 71% ↙5km/h
```

Full forecast:
```bash
curl -s "wttr.in/{city}?T"
```

Format codes: `%c` condition · `%t` temp · `%h` humidity · `%w` wind · `%l` location · `%m` moon

Tips:
- URL-encode spaces: `wttr.in/New+York`
- Airport codes: `wttr.in/JFK`
- Units: `?m` (metric) `?u` (USCS)
- Today only: `?1` · Current only: `?0`
- PNG: `curl -s "wttr.in/Berlin.png" -o /tmp/weather.png`

## Open-Meteo (fallback, JSON)

Free, no key, good for programmatic use:
```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=-0.12&current_weather=true"
```

Find coordinates for a city, then query. Returns JSON with temp, windspeed, weathercode.

Docs: https://open-meteo.com/en/docs
