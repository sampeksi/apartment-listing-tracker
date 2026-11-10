#!/usr/bin/env python3
"""Rent-comp estimation from a sibling rental-listing site's public search API.

Same anonymous-POST shape as the sale-listing search, just under a different
subdomain/path and with rental-specific fields (searchRent instead of
searchPrice, etc.) - confirmed working with no auth needed.

RENTAL_SITE_BASE_URL is not hardcoded, same reasoning as LISTING_SITE_BASE_URL
in fetch_listings.py: it's the same company as the sale-listing site, so the
same de-branding applies.

Comp matching is adaptive: start at city + room count (the same granularity
the business search criteria already uses - no district-level narrowing
needed here, unlike the hobby pipeline), and if that yields too few comps
(sparse markets in the smaller towns), widen to the whole configured city
cluster before giving up. Sample size is always reported alongside the
aggregate, so a thin sample doesn't get presented with false confidence.
"""
import statistics
from dataclasses import dataclass

import requests

PAGE_SIZE = 100
MIN_COMPS = 5
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    return session


def fetch_rental_listings(
    session: requests.Session, cities: list[str], room_count: str, base_url: str
) -> list[dict]:
    url = f"{base_url}/distant/swordsman/v3/announcements/rental/search/listpage"
    body = {
        "locationSearchCriteria": {"unclassifiedLocationTerms": cities},
        "lessorType": "ALL",
        "publishingTimeSearchCriteria": "ANY_DAY",
        "propertyType": "RESIDENTIAL",
        "freeTextSearch": "",
        "residentialPropertyTypes": ["APARTMENT_HOUSE"],
        "roomCounts": [room_count],
        "rightOfOccupancy": "NO_LIVING_RIGHT",
        "newBuildingSearchCriteria": "ALL_PROPERTIES",
    }

    listings = []
    first_result = 0
    while True:
        body["pagination"] = {
            "firstResult": first_result,
            "maxResults": PAGE_SIZE,
            "page": first_result // PAGE_SIZE + 1,
            "sortingOrder": {"property": "PUBLISHED_OR_UPDATED_AT", "direction": "DESC"},
        }
        resp = session.post(url, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("announcements", [])
        listings.extend(batch)
        total = data.get("countOfAllResults", 0)
        first_result += len(batch)
        if not batch or first_result >= total:
            break
    return listings


@dataclass
class RentEstimate:
    median_rent: float
    median_rent_per_sqm: float
    sample_size: int
    widened_to_region: bool  # True if the city-only sample was too thin


def aggregate(listings: list[dict]) -> tuple[float, float, int] | None:
    rents = []
    rents_per_sqm = []
    for listing in listings:
        rent = listing.get("searchRent")
        area = listing.get("area") or listing.get("totalArea")
        if rent is None:
            continue
        rents.append(rent)
        if area:
            rents_per_sqm.append(rent / area)
    if not rents:
        return None
    return (
        statistics.median(rents),
        statistics.median(rents_per_sqm) if rents_per_sqm else 0.0,
        len(rents),
    )


def estimate_rent(
    session: requests.Session, city: str, room_count: str, all_cities: list[str], base_url: str
) -> RentEstimate | None:
    """City-level comps first; widen to the whole configured city cluster if
    there aren't enough (sparse markets in the smaller towns)."""
    city_listings = fetch_rental_listings(session, [city], room_count, base_url)
    city_agg = aggregate(city_listings)

    if city_agg and city_agg[2] >= MIN_COMPS:
        rent, rent_per_sqm, n = city_agg
        return RentEstimate(rent, rent_per_sqm, n, widened_to_region=False)

    region_listings = fetch_rental_listings(session, all_cities, room_count, base_url)
    region_agg = aggregate(region_listings)
    if not region_agg:
        return None
    rent, rent_per_sqm, n = region_agg
    return RentEstimate(rent, rent_per_sqm, n, widened_to_region=True)
