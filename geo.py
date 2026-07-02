#!/usr/bin/env python3
"""
Import-time geo guard for the outreach pipeline.

Google's text search is fuzzy: "restaurants in Botoșani" happily returns venues
in Suceava, and a neighborhood-scoped query can drift hundreds of km (a
Maramureș venue was once imported as `iasi`). Since Places details gives us the
exact coordinates anyway, we gate every import on them: the venue must be
NEAREST to the city we searched for (among ALL city centroids the web app
knows) and within an acceptance radius of that centroid. Anything else is
skipped — better to miss a suburb than to show a Suceava restaurant in
Botoșani.

CITY_CENTROIDS mirrors web/src/lib/cities.ts (id + city-centre lat/lng).
Keep in sync if cities change there — including cities we don't target yet:
they act as "magnets" that catch venues belonging to a neighboring city.
"""

from __future__ import annotations

import math

CITY_CENTROIDS: dict[str, tuple[float, float]] = {
    "bucuresti": (44.4268, 26.1025),
    "cluj-napoca": (46.7712, 23.6236),
    "timisoara": (45.7489, 21.2087),
    "iasi": (47.1585, 27.6014),
    "constanta": (44.1598, 28.6348),
    "craiova": (44.3302, 23.7949),
    "brasov": (45.6427, 25.5887),
    "galati": (45.4353, 28.008),
    "ploiesti": (44.9366, 26.0367),
    "oradea": (47.0465, 21.9189),
    "braila": (45.2692, 27.9574),
    "arad": (46.1866, 21.3123),
    "pitesti": (44.8565, 24.8692),
    "sibiu": (45.7983, 24.1256),
    "bacau": (46.567, 26.9146),
    "targu-mures": (46.5425, 24.5575),
    "baia-mare": (47.6533, 23.5795),
    "buzau": (45.15, 26.8203),
    "botosani": (47.7486, 26.6694),
    "satu-mare": (47.7921, 22.8574),
    "ramnicu-valcea": (45.0997, 24.3693),
    "suceava": (47.6635, 26.2732),
    "piatra-neamt": (46.9275, 26.3708),
    "drobeta-turnu-severin": (44.6369, 22.6597),
    "focsani": (45.696, 27.1864),
    "targoviste": (44.9247, 25.457),
    "bistrita": (47.1329, 24.5108),
    "resita": (45.2971, 21.8892),
    "tulcea": (45.178, 28.805),
    "slatina": (44.4297, 24.362),
    "calarasi": (44.2058, 27.3306),
    "alba-iulia": (46.0733, 23.5805),
    "deva": (45.8779, 22.9147),
    "giurgiu": (43.9037, 25.9699),
    "zalau": (47.1911, 23.0572),
    "alexandria": (43.9702, 25.3339),
    "vaslui": (46.6407, 27.7276),
    "sfantu-gheorghe": (45.862, 25.7877),
    "targu-jiu": (45.0362, 23.2745),
    "slobozia": (44.5647, 27.3661),
    "miercurea-ciuc": (46.3612, 25.8018),
    "buftea": (44.5667, 25.95),
}

# Acceptance radius (km) from the searched city's centroid. Default fits a
# mid-size Romanian city plus its close suburbs; Bucharest sprawls further.
DEFAULT_MAX_KM = 15.0
MAX_KM_OVERRIDES = {"bucuresti": 20.0}


def km(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    """Haversine distance in km."""
    d_lat = math.radians(lat_b - lat_a)
    d_lng = math.radians(lng_b - lng_a)
    h = (math.sin(d_lat / 2) ** 2
         + math.sin(d_lng / 2) ** 2
         * math.cos(math.radians(lat_a)) * math.cos(math.radians(lat_b)))
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(h)))


def nearest_city(lat: float, lng: float) -> tuple[str, float]:
    """(city slug, distance km) of the closest known centroid."""
    best, best_km = "", float("inf")
    for slug, (c_lat, c_lng) in CITY_CENTROIDS.items():
        d = km(lat, lng, c_lat, c_lng)
        if d < best_km:
            best, best_km = slug, d
    return best, best_km


def check_city(lat: float, lng: float, city_slug: str) -> tuple[bool, str]:
    """
    Does this coordinate genuinely belong to `city_slug`?

    Returns (ok, reason). ok requires BOTH:
      - city_slug is the nearest known centroid (so a Suceava venue can never
        pass as Botoșani even if it's within the radius), and
      - the distance to it is within the city's acceptance radius.
    """
    if city_slug not in CITY_CENTROIDS:
        return False, f"unknown city slug {city_slug!r}"
    near_slug, near_km = nearest_city(lat, lng)
    target_km = km(lat, lng, *CITY_CENTROIDS[city_slug])
    if near_slug != city_slug:
        return False, (f"nearest city is {near_slug} ({near_km:.0f}km), "
                       f"not {city_slug} ({target_km:.0f}km)")
    max_km = MAX_KM_OVERRIDES.get(city_slug, DEFAULT_MAX_KM)
    if target_km > max_km:
        return False, f"{target_km:.0f}km from {city_slug} centre (max {max_km:.0f}km)"
    return True, f"{target_km:.1f}km from {city_slug} centre"
