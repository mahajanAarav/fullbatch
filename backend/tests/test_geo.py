"""Address lookups and distances. No network: the HTTP layer is faked."""

import httpx
import pytest

from app import geo
from app.geo import GeocodeError, NominatimGeocoder, area_from_address, haversine_km, public_coord

HIT = [{
    "lat": "40.7484421", "lon": "-73.9856589", "display_name": "Empire State Building, 350, 5th Avenue, Koreatown, Manhattan, New York",
    "address": {"neighbourhood": "Koreatown", "suburb": "Manhattan", "city": "New York", "state": "New York", "house_number": "350", "road": "5th Avenue"},
}]


def make(handler, **kw):
    sleeps, now = [], [0.0]
    g = NominatimGeocoder(transport=httpx.MockTransport(handler), sleep=sleeps.append, clock=lambda: now[0], **kw)
    g.now, g.sleeps = now, sleeps
    return g


# ---- distance ---------------------------------------------------------------------

def test_known_distances():
    assert haversine_km(40.0, -74.0, 40.0, -74.0) == 0
    assert haversine_km(40.7128, -74.0060, 34.0522, -118.2437) == pytest.approx(3936, abs=15)   # New York to Los Angeles
    assert haversine_km(40.7484, -73.9857, 40.7580, -73.9855) == pytest.approx(1.07, abs=0.05)  # about a kilometre apart
    assert haversine_km(40.7, -74.0, 40.8, -73.9) == haversine_km(40.8, -73.9, 40.7, -74.0)     # symmetric


def test_public_coordinates_are_too_coarse_to_find_a_doorstep():
    assert public_coord(40.7484421) == 40.75 and public_coord(-73.9856589) == -73.99
    assert haversine_km(40.7484421, -73.9856589, public_coord(40.7484421), public_coord(-73.9856589)) < 1.6


def test_miles_conversion_constant():
    assert geo.KM_PER_MILE == pytest.approx(1.609344)


# ---- area labels ---------------------------------------------------------------------

@pytest.mark.parametrize("address,expected", [
    ({"neighbourhood": "Koreatown", "city": "New York"}, "Koreatown, New York"),
    ({"suburb": "Park Slope", "city": "New York"}, "Park Slope, New York"),
    ({"city": "Austin"}, "Austin"),
    ({"suburb": "Austin", "city": "Austin"}, "Austin"),            # not "Austin, Austin"
    ({"town": "Hudson", "suburb": "Uptown"}, "Uptown, Hudson"),
    ({"state": "Ohio"}, "Ohio"),
    ({}, "Unknown area"),
])
def test_area_labels_never_include_a_street(address, expected):
    assert area_from_address({**address, "road": "5th Avenue", "house_number": "350"}) == expected


# ---- the lookup -----------------------------------------------------------------------

def test_a_lookup_returns_a_place_with_a_public_area():
    place = make(lambda r: httpx.Response(200, json=HIT)).lookup("350 5th Ave, New York")
    assert (place.lat, place.lng) == (40.7484421, -73.9856589)
    assert place.area == "Koreatown, New York" and "350" not in place.area


def test_the_request_identifies_us_and_stays_inside_the_configured_country():
    seen = []
    make(lambda r: (seen.append(r), httpx.Response(200, json=HIT))[1], countries="us").lookup("11215")
    req = seen[0]
    assert "fullbatch" in req.headers["user-agent"]                       # the service's policy asks for this
    assert req.url.params["countrycodes"] == "us" and req.url.params["q"] == "11215"
    assert req.url.params["addressdetails"] == "1" and req.url.params["limit"] == "1"


def test_unknown_places_are_none_not_errors():
    assert make(lambda r: httpx.Response(200, json=[])).lookup("zzzz qqqq") is None


def test_repeat_lookups_are_cached_including_misses():
    calls = []
    g = make(lambda r: (calls.append(1), httpx.Response(200, json=HIT))[1])
    g.lookup("350 5th Ave")
    g.lookup("  350   5TH ave ")            # same address, different spacing and case
    assert len(calls) == 1
    miss = make(lambda r: (calls.append(2), httpx.Response(200, json=[]))[1])
    miss.lookup("nowhere")
    miss.lookup("nowhere")
    assert calls.count(2) == 1


def test_lookups_are_spaced_out_to_respect_the_services_policy():
    g = make(lambda r: httpx.Response(200, json=HIT))
    g.lookup("a")
    g.lookup("b")                           # immediately after: must wait
    assert len(g.sleeps) == 1 and g.sleeps[0] == pytest.approx(1.1, abs=0.01)
    g.now[0] += 5.0
    g.lookup("c")                           # long enough ago: no wait
    assert len(g.sleeps) == 1


def test_an_empty_query_is_none_without_a_request():
    calls = []
    assert make(lambda r: (calls.append(1), httpx.Response(200, json=HIT))[1]).lookup("   ") is None and not calls


def test_the_cache_is_bounded():
    g = make(lambda r: httpx.Response(200, json=HIT), cache_size=2)
    for q in ("a", "b", "c"):
        g.lookup(q)
    assert len(g._cache) == 2 and "a" not in g._cache


@pytest.mark.parametrize("response", [
    httpx.Response(503, text="busy"),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json=[{"nope": 1}]),
])
def test_failures_are_geocode_errors_not_crashes(response):
    with pytest.raises(GeocodeError):
        make(lambda r: response).lookup("anything")


def test_network_failure_is_a_geocode_error():
    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(GeocodeError):
        make(boom).lookup("anything")
