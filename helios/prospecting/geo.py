"""Where to look and what's there: OpenStreetMap only (Nominatim geocoding + Overpass queries).

* Never Google Maps: no Maps pages are fetched or scrolled; a Google Maps link listed as a
  business's "website" is treated as *no website*, and never opened (forbidden_source).
* OSM data is ODbL: attribution "© OpenStreetMap contributors"; fine for internal use.
* Usage policies: Nominatim max 1 request/second with an identifying User-Agent; Overpass fair use.
  Every answer is cached on disk (geocodes forever, Overpass cells for a week), so re-runs and the
  coverage planner don't re-ask.
* Coverage is reported as "searched OpenStreetMap across these cells" — OSM is incomplete for
  small businesses, so Helios never claims to have found *every* business.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .. import conf

UA = "Helios-desktop-assistant/1.0 (+https://github.com/zeeshan-rgb/Helios)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
SOURCE_NAME = "OpenStreetMap (Overpass API)"
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
OVERPASS_TTL = 7 * 86400

# category -> OSM tag filters (key, value). Edit/extend via [prospecting.categories] in settings.
CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "salon": [("shop", "hairdresser"), ("shop", "beauty"), ("amenity", "spa")],
    "clinic": [("amenity", "clinic"), ("amenity", "doctors"), ("healthcare", "clinic"),
               ("healthcare", "physiotherapist")],
    "dentist": [("amenity", "dentist"), ("healthcare", "dentist")],
    "restaurant": [("amenity", "restaurant"), ("amenity", "cafe"), ("amenity", "fast_food")],
    "bakery": [("shop", "bakery"), ("shop", "confectionery"), ("shop", "pastry")],
    "retail": [("shop", "clothes"), ("shop", "shoes"), ("shop", "jewelry"), ("shop", "electronics"),
               ("shop", "mobile_phone"), ("shop", "furniture"), ("shop", "optician"), ("shop", "gift"),
               ("shop", "cosmetics"), ("shop", "boutique"), ("shop", "hardware"), ("shop", "stationery")],
    "repair": [("shop", "car_repair"), ("shop", "repair"), ("craft", "electronics_repair"),
               ("shop", "computer"), ("craft", "tailor")],
    "fitness": [("leisure", "fitness_centre"), ("leisure", "sports_centre"), ("sport", "yoga")],
    "education": [("amenity", "language_school"), ("amenity", "driving_school"), ("amenity", "music_school"),
                  ("office", "educational_institution"), ("amenity", "training")],
    "professional": [("office", "lawyer"), ("office", "accountant"), ("office", "estate_agent"),
                     ("office", "architect"), ("office", "consulting"), ("office", "insurance")],
    "hospitality": [("tourism", "guest_house"), ("tourism", "hotel")],
    "workshop": [("craft", "carpenter"), ("craft", "metal_construction"), ("craft", "printer"),
                 ("craft", "photographer"), ("shop", "printing")],
}
# What kind of customer journey each category has (used by scoring for concrete reasons).
APPOINTMENT = {"salon", "clinic", "dentist", "fitness", "education", "professional", "repair"}
ORDERING = {"restaurant", "bakery"}
RETAIL = {"retail"}

_nominatim_lock = threading.Lock()
_last_nominatim = 0.0


def _cache_dir() -> Path:
    d = conf.DATA_DIR / "prospecting"
    d.mkdir(parents=True, exist_ok=True)
    return d


def forbidden_source(url: str) -> str:
    """Sources Helios must not fetch for prospecting ('' = fine)."""
    u = urllib.parse.urlsplit(url if "://" in (url or "") else f"https://{url}")
    host = (u.hostname or "").lower()
    path = (u.path or "").lower()
    if host in ("maps.app.goo.gl", "goo.gl") and (host != "goo.gl" or path.startswith("/maps")):
        return "Google Maps link (not fetched — Maps content isn't used for prospecting)"
    if re.match(r"^(maps\.)?google\.[a-z.]+$", host) and (host.startswith("maps.") or path.startswith("/maps")):
        return "Google Maps page (not fetched — Maps content isn't used for prospecting)"
    if host.startswith("www.google.") and path.startswith("/maps"):
        return "Google Maps page (not fetched — Maps content isn't used for prospecting)"
    return ""


def _get_json(url: str, *, data: bytes | None = None, timeout: float = 60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------------------------ geocoding

def geocode(area: str, *, fetch=None) -> dict | None:
    """'Mehdipatnam, Hyderabad' -> {name, lat, lon, bbox:(s,w,n,e)} (cached forever; ≤1 req/s)."""
    global _last_nominatim
    key = area.strip().lower()
    f = _cache_dir() / "geocode.json"
    try:
        cache = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    if key in cache:
        return cache[key]
    url = NOMINATIM + "?" + urllib.parse.urlencode({"q": area, "format": "jsonv2", "limit": 1})
    with _nominatim_lock:
        wait = _last_nominatim + 1.1 - time.monotonic()
        if wait > 0 and fetch is None:
            time.sleep(wait)
        _last_nominatim = time.monotonic()
        rows = (fetch or _get_json)(url)
    if not rows:
        return None
    r = rows[0]
    s, n, w, e = (float(x) for x in r["boundingbox"])
    out = {"name": r.get("display_name", area), "lat": float(r["lat"]), "lon": float(r["lon"]),
           "bbox": [s, w, n, e], "osm": f"{r.get('osm_type', '')}/{r.get('osm_id', '')}"}
    cache[key] = out
    f.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return out


# ------------------------------------------------------------------ cells (coverage planner)

def _km_per_deg_lon(lat: float) -> float:
    return 111.32 * math.cos(math.radians(lat))


def area_bbox(place: dict, radius_km: float | None) -> list[float]:
    """The search box: a radius around the place's centre, else its own bounding box."""
    if radius_km:
        dlat = radius_km / 110.57
        dlon = radius_km / max(_km_per_deg_lon(place["lat"]), 1e-6)
        return [place["lat"] - dlat, place["lon"] - dlon, place["lat"] + dlat, place["lon"] + dlon]
    return list(place["bbox"])


def cells_for(bbox: list[float], cell_km: float = 1.0, *, centre: tuple[float, float] | None = None,
              max_cells: int = 400) -> list[str]:
    """Split a box into ~cell_km squares, nearest-to-centre first (so partial coverage is useful)."""
    s, w, n, e = bbox
    lat0 = (s + n) / 2
    dlat = cell_km / 110.57
    dlon = cell_km / max(_km_per_deg_lon(lat0), 1e-6)
    rows = max(1, math.ceil((n - s) / dlat))
    cols = max(1, math.ceil((e - w) / dlon))
    cells = []
    for i in range(rows):
        for j in range(cols):
            cs, cw = s + i * dlat, w + j * dlon
            cells.append((round(cs, 5), round(cw, 5), round(min(cs + dlat, n), 5), round(min(cw + dlon, e), 5)))
    cy, cx = centre or (lat0, (w + e) / 2)
    cells.sort(key=lambda c: ((c[0] + c[2]) / 2 - cy) ** 2 + (((c[1] + c[3]) / 2 - cx) * math.cos(math.radians(cy))) ** 2)
    return [",".join(f"{v:.5f}" for v in c) for c in cells[:max_cells]]


# ------------------------------------------------------------------ Overpass

def tag_filters(categories: list[str]) -> list[tuple[str, str, str]]:
    extra = conf.SETTINGS.get("prospecting", {}).get("categories", {}) if isinstance(conf.SETTINGS.get("prospecting"), dict) else {}
    out = []
    for cat in categories:
        pairs = CATEGORIES.get(cat, [])
        if isinstance(extra, dict) and isinstance(extra.get(cat), list):
            pairs = [tuple(str(x).split("=", 1)) for x in extra[cat] if "=" in str(x)]
        out += [(cat, k, v) for k, v in pairs]
    return out


def overpass_query(cell: str, categories: list[str], limit: int = 400) -> str:
    s, w, n, e = cell.split(",")
    lines = [f'  nwr["{k}"="{v}"]["name"]({s},{w},{n},{e});' for _cat, k, v in tag_filters(categories)]
    return f"[out:json][timeout:60];\n(\n" + "\n".join(lines) + f"\n);\nout center tags {int(limit)};"


def query_cell(cell: str, categories: list[str], *, fetch=None) -> list[dict]:
    """Named businesses of these categories inside one cell (cached a week)."""
    q = overpass_query(cell, categories)
    key = hashlib.sha256(q.encode()).hexdigest()[:24]
    f = _cache_dir() / "overpass" / f"{key}.json"
    f.parent.mkdir(exist_ok=True)
    try:
        cached = json.loads(f.read_text(encoding="utf-8"))
        if time.time() - cached["at"] < OVERPASS_TTL:
            return cached["elements"]
    except Exception:
        pass
    data = (fetch or _get_json)(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode())
    elements = data.get("elements") or []
    f.write_text(json.dumps({"at": time.time(), "elements": elements}), encoding="utf-8")
    return elements


def categorize(tags: dict) -> str:
    for cat, pairs in CATEGORIES.items():
        if any(tags.get(k) == v for k, v in pairs):
            return cat
    return ""


def to_business(el: dict, area: str) -> dict | None:
    """OSM element -> normalized business profile (or None for chains / unnamed / no location)."""
    from . import store
    t = el.get("tags") or {}
    name = (t.get("name") or "").strip()
    if not name:
        return None
    if t.get("brand:wikidata") or t.get("brand:wikipedia"):
        return None                       # a big chain, not a small local business
    lat = el.get("lat", (el.get("center") or {}).get("lat"))
    lon = el.get("lon", (el.get("center") or {}).get("lon"))
    website = (t.get("website") or t.get("contact:website") or t.get("url") or "").strip()
    note = ""
    if website and forbidden_source(website):
        note, website = "the listed link is a Google Maps page, not a website", ""
    email = (t.get("email") or t.get("contact:email") or "").strip()
    phone = (t.get("phone") or t.get("contact:phone") or t.get("contact:mobile") or "").split(";")[0].strip()
    addr = ", ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:suburb") or
                                  t.get("addr:neighbourhood"), t.get("addr:postcode")) if x)
    socials = [t[k] for k in ("contact:facebook", "facebook", "contact:instagram", "instagram") if t.get(k)]
    osm = f"{el.get('type', 'node')}/{el.get('id')}"
    return {"dedupe_key": f"osm:{osm}", "name": name[:150], "category": categorize(t), "area": area,
            "address": addr[:200], "city": (t.get("addr:city") or "")[:80], "lat": lat, "lon": lon,
            "website": website[:300], "domain": store.domain_of(website) if website else "",
            "email": email[:120], "email_source": "OpenStreetMap" if email else "",
            "phone": phone[:40], "socials": socials[:4],
            "source_name": SOURCE_NAME, "source_url": f"https://www.openstreetmap.org/{osm}",
            "verification": "listed in OpenStreetMap" + (f" ({note})" if note else ""),
            "opening_hours": t.get("opening_hours", "")}
