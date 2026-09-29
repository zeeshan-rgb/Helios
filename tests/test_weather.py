"""helios.weather — Open-Meteo fetch, all HTTP mocked (ported from Helios-main). Guards
the contract that the return string carries REAL figures — a placeholder string made the
old assistant's morning-brief agent hallucinate the weather."""
import pytest
import requests

from helios import weather as wr


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


GEO = {"results": [{"name": "Larnaca", "country": "Cyprus",
                    "latitude": 34.9, "longitude": 33.6}]}
FX = {"current": {"temperature_2m": 31.2, "apparent_temperature": 33.0,
                  "weather_code": 0, "wind_speed_10m": 14.0},
      "daily": {"temperature_2m_max": [33.1, 32.0],
                "temperature_2m_min": [24.0, 23.1],
                "precipitation_probability_max": [5, 10],
                "weather_code": [0, 2]}}


def _mock_http(monkeypatch, geo=GEO, fx=FX):
    def get(url, params=None, timeout=None):
        assert timeout, "every weather HTTP call must set a timeout"
        return _Resp(geo if "geocoding" in url else fx)
    monkeypatch.setattr(wr.requests, "get", get)


def test_today_has_real_figures(monkeypatch):
    _mock_http(monkeypatch)
    out = wr.weather_report("Larnaca")
    assert "31" in out and "Larnaca, Cyprus" in out and "high 33" in out
    assert "clear sky" in out and "5 percent" in out


def test_tomorrow_uses_day_index_1(monkeypatch):
    _mock_http(monkeypatch)
    out = wr.weather_report("Larnaca", "tomorrow")
    assert out.startswith("Tomorrow in Larnaca, Cyprus")
    assert "high 32" in out and "low 23" in out and "10 percent" in out
    assert "partly cloudy" in out


def test_unknown_city(monkeypatch):
    _mock_http(monkeypatch, geo={"results": []})
    out = wr.weather_report("Xyzzyville")
    assert "couldn't find a city called Xyzzyville" in out


def test_network_error(monkeypatch):
    def get(url, params=None, timeout=None):
        raise requests.ConnectionError("dns down")
    monkeypatch.setattr(wr.requests, "get", get)
    assert "couldn't fetch weather data for Larnaca" in wr.weather_report("Larnaca")


def test_partial_response_degrades_cleanly(monkeypatch):
    _mock_http(monkeypatch, fx={"current": {}})      # no daily block at all
    out = wr.weather_report("Larnaca")
    assert "couldn't fetch weather data" in out and "incomplete" in out


def test_missing_current_falls_back_to_daily(monkeypatch):
    _mock_http(monkeypatch, fx={"daily": FX["daily"]})
    out = wr.weather_report("Larnaca")
    assert out.startswith("Today in Larnaca, Cyprus") and "high 33" in out


def test_missing_city_never_calls_network(monkeypatch):
    def get(*a, **k):
        raise AssertionError("missing city must not hit the network")
    monkeypatch.setattr(wr.requests, "get", get)
    assert "city is missing" in wr.weather_report("")
    assert "city is missing" in wr.weather_report("   ")
