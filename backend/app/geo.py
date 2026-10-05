"""
Addresses to places, and distances between them.

Lookups use OpenStreetMap's free Nominatim service. Its usage policy asks for an identifying
User-Agent, at most one request a second, and no hammering, so lookups are cached and spaced out.
Only the server looks addresses up. A buyer's own location stays in their browser.
"""

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "fullbatch-hackathon/1.0 (https://github.com/mahajanAarav/fullbatch)"
EARTH_RADIUS_KM = 6371.0088
KM_PER_MILE = 1.609344


class GeocodeError(Exception):
    """The lookup service could not be reached or gave an unusable answer."""


@dataclass(frozen=True)
class Place:
    lat: float
    lng: float
    area: str   # a public, neighborhood-level label such as "Koreatown, New York"
    label: str  # the full resolved address, so people can confirm it found the right place


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def public_coord(value: float) -> float:
    """Two decimals is about 1.1 km: close enough to sort by distance, too coarse to find a doorstep."""
    return round(value, 2)


def area_from_address(address: dict) -> str:
    """'Koreatown, New York' from Nominatim's address parts, with no street or house number."""
    local = address.get("neighbourhood") or address.get("suburb") or address.get("city_district")
    city = address.get("city") or address.get("town") or address.get("village") or address.get("hamlet") or address.get("county")
    state = address.get("state")
    parts = [p for p in (local, city) if p]
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    if not parts and state:
        return state
    return ", ".join(parts) or "Unknown area"


class NominatimGeocoder:
    def __init__(
        self,
        countries: str = "us",
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
        min_interval: float = 1.1,
        cache_size: int = 512,
    ):
        self._countries = countries
        self._http = httpx.Client(timeout=8, headers={"User-Agent": USER_AGENT}, transport=transport)
        self._sleep, self._clock, self._min_interval = sleep, clock, min_interval
        self._cache: OrderedDict[str, Place | None] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._last_call = -1e9

    def lookup(self, query: str) -> Place | None:
        """The best match for an address or ZIP, or None if there is none. Raises GeocodeError if the service is down."""
        key = " ".join(query.lower().split())[:200]
        if not key:
            return None
        with self._lock:  # one request at a time, spaced out, per the service's usage policy
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            try:
                r = self._http.get(
                    NOMINATIM_URL,
                    params={"q": key, "format": "jsonv2", "limit": 1, "addressdetails": 1, "countrycodes": self._countries},
                )
                self._last_call = self._clock()
                r.raise_for_status()
                results = r.json()
            except (httpx.HTTPError, ValueError) as err:
                raise GeocodeError(f"address lookup failed ({type(err).__name__})") from err
            place = None
            if results:
                hit = results[0]
                try:
                    place = Place(float(hit["lat"]), float(hit["lon"]), area_from_address(hit.get("address", {})), hit.get("display_name", key)[:300])
                except (KeyError, ValueError) as err:
                    raise GeocodeError("address lookup returned something unexpected") from err
            self._cache[key] = place  # misses are cached too, so a typo is not looked up twice
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return place
