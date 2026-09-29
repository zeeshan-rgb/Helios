"""Real weather via Open-Meteo (keyless) — ported from Helios-main
actions/weather_report.py (2026-07-08 consolidation). Geocode the city, fetch current +
daily forecast, return a spoken-style summary WITH figures. The return string is the
tool's whole output (workflow steps and the brain only ever see this string), so it must
carry the data. Every HTTP call has a timeout: a hung request would stall a turn."""
from __future__ import annotations

import requests

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8   # seconds per call

# WMO weather interpretation codes (Open-Meteo `weather_code`).
_WMO = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "icy fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def weather_report(city: str, when: str = "today") -> str:
    if not city or not str(city).strip():
        return "Sir, the city is missing for the weather report."

    city = str(city).strip()
    when = (str(when) or "today").strip().lower()
    tomorrow = "tomorrow" in when

    try:
        geo = requests.get(GEO_URL, params={
            "name": city, "count": 1, "language": "en", "format": "json",
        }, timeout=_TIMEOUT)
        geo.raise_for_status()
        results = (geo.json() or {}).get("results") or []
        if not results:
            return f"Sir, I couldn't find a city called {city} for the weather report."
        place = results[0]
        label = place.get("name") or city
        if place.get("country"):
            label += f", {place['country']}"

        fx = requests.get(FORECAST_URL, params={
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,weather_code",
            "forecast_days": 2,
            "timezone": "auto",
        }, timeout=_TIMEOUT)
        fx.raise_for_status()
        msg = _summary(label, fx.json() or {}, tomorrow)
        if msg is None:
            msg = f"Sir, I couldn't fetch weather data for {city}: incomplete response."
        return msg
    except requests.RequestException as e:
        return f"Sir, I couldn't fetch weather data for {city}: {e}"


def _summary(label: str, data: dict, tomorrow: bool):
    """Spoken-style ASCII summary; None if the API response is missing the pieces."""
    daily = data.get("daily") or {}
    idx = 1 if tomorrow else 0
    try:
        hi = daily["temperature_2m_max"][idx]
        lo = daily["temperature_2m_min"][idx]
        rain = daily["precipitation_probability_max"][idx]
        code = daily["weather_code"][idx]
    except (KeyError, IndexError, TypeError):
        return None
    cond = _WMO.get(code, "mixed conditions")
    if tomorrow:
        return (f"Tomorrow in {label}: {cond}, high {_n(hi)}, low {_n(lo)}, "
                f"{_n(rain)} percent chance of rain, sir.")

    cur = data.get("current") or {}
    temp, feels = cur.get("temperature_2m"), cur.get("apparent_temperature")
    wind = cur.get("wind_speed_10m")
    now_cond = _WMO.get(cur.get("weather_code"), cond)
    if temp is None:
        return (f"Today in {label}: {cond}, high {_n(hi)}, low {_n(lo)}, "
                f"{_n(rain)} percent chance of rain, sir.")
    now = f"It's {_n(temp)} degrees in {label} right now"
    if feels is not None:
        now += f", feels like {_n(feels)}"
    now += f", {now_cond}"
    if wind is not None:
        now += f", wind {_n(wind)} km/h"
    return (f"{now}. Today: high {_n(hi)}, low {_n(lo)}, "
            f"{_n(rain)} percent chance of rain, sir.")


def _n(v) -> str:
    """Round numerics for speech: 31.2 -> '31'."""
    try:
        return str(int(round(float(v))))
    except (TypeError, ValueError):
        return str(v)
