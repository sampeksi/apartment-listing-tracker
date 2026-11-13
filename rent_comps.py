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

The blended comp above mixes all conditions together, which hides a real
GOOD/SATISFACTORY/TOLERABLE spread - confirmed both the sale and rental
search APIs accept an overallConditions filter, so estimate_rent_by_condition()
runs three condition-filtered queries instead of one unconditioned query (no
per-listing detail fetch needed - a listing's condition is just which query
returned its friendlyId). Unlike the single blended estimate, the three
conditions are widened to region together, not independently - confirmed by
testing that letting them widen independently produces a geography
confound (a thin condition's region-widened sample can pull in a pricier
city than the other conditions' city-only samples, making a cross-condition
comparison compare location, not condition). If every bucket for the
apartment's own room count comes back empty, it also reports the same
breakdown for an adjacent room count, so there's at least something to
derive a relative condition adjustment from.
"""
import statistics
from dataclasses import dataclass

import requests

PAGE_SIZE = 100
MIN_COMPS = 5
CONDITIONS = ("GOOD", "SATISFACTORY", "TOLERABLE")
ROOM_COUNT_ORDER = ["ONE_ROOM", "TWO_ROOMS", "THREE_ROOMS", "FOUR_ROOMS", "FIVE_ROOMS"]
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
    session: requests.Session, cities: list[str], room_count: str, base_url: str,
    overall_conditions: list[str] | None = None,
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
    if overall_conditions:
        body["overallConditions"] = overall_conditions

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


def _widened_estimate(
    session: requests.Session, city: str, room_count: str, all_cities: list[str], base_url: str,
    overall_conditions: list[str] | None = None,
) -> RentEstimate | None:
    """City-level comps first; widen to the whole configured city cluster if
    there aren't enough (sparse markets in the smaller towns)."""
    city_listings = fetch_rental_listings(session, [city], room_count, base_url, overall_conditions)
    city_agg = aggregate(city_listings)

    if city_agg and city_agg[2] >= MIN_COMPS:
        rent, rent_per_sqm, n = city_agg
        return RentEstimate(rent, rent_per_sqm, n, widened_to_region=False)

    region_listings = fetch_rental_listings(session, all_cities, room_count, base_url, overall_conditions)
    region_agg = aggregate(region_listings)
    if not region_agg:
        return None
    rent, rent_per_sqm, n = region_agg
    return RentEstimate(rent, rent_per_sqm, n, widened_to_region=True)


def estimate_rent(
    session: requests.Session, city: str, room_count: str, all_cities: list[str], base_url: str
) -> RentEstimate | None:
    return _widened_estimate(session, city, room_count, all_cities, base_url)


def _condition_estimates(
    session: requests.Session, city: str, room_count: str, all_cities: list[str], base_url: str
) -> dict:
    """GOOD/SATISFACTORY/TOLERABLE comps for one room count, all sharing the
    same geographic scope - city-only if every condition meets MIN_COMPS
    there, otherwise all three widened to the region together. Letting each
    condition widen independently would mean a thin condition's region-wide
    sample could be pulling in a pricier/cheaper city than another
    condition's city-only sample, confounding condition with geography."""
    city_aggs = {
        condition: aggregate(fetch_rental_listings(session, [city], room_count, base_url, [condition]))
        for condition in CONDITIONS
    }
    if all(agg and agg[2] >= MIN_COMPS for agg in city_aggs.values()):
        return {c: RentEstimate(agg[0], agg[1], agg[2], widened_to_region=False) for c, agg in city_aggs.items()}

    region_aggs = {
        condition: aggregate(fetch_rental_listings(session, all_cities, room_count, base_url, [condition]))
        for condition in CONDITIONS
    }
    return {
        c: (RentEstimate(agg[0], agg[1], agg[2], widened_to_region=True) if agg and agg[2] >= MIN_COMPS else None)
        for c, agg in region_aggs.items()
    }


def estimate_rent_by_condition(
    session: requests.Session, city: str, room_count: str, all_cities: list[str], base_url: str,
    subject_condition: str | None = None,
) -> dict:
    """GOOD/SATISFACTORY/TOLERABLE rent estimates for room_count, each
    independently city->region widened the same way as estimate_rent(). A
    listing's condition is inferred purely from which of the three queries
    returned its friendlyId - no per-listing detail fetch needed.

    If the bucket matching the apartment's own condition has no data (or,
    with no known subject condition, if none of the three buckets have any
    data), also returns the same breakdown for an adjacent room count - not
    a substitute, just enough for the LLM to derive a relative condition
    adjustment from a bigger sample rather than having nothing at all."""
    primary = _condition_estimates(session, city, room_count, all_cities, base_url)

    have_enough = primary.get(subject_condition) if subject_condition else any(primary.values())
    if have_enough:
        return {"roomCount": room_count, "estimates": primary, "fallbackRoomCount": None, "fallbackEstimates": None}

    fallback_room_count = None
    if room_count in ROOM_COUNT_ORDER:
        idx = ROOM_COUNT_ORDER.index(room_count)
        for candidate_idx in (idx - 1, idx + 1):
            if 0 <= candidate_idx < len(ROOM_COUNT_ORDER):
                fallback_room_count = ROOM_COUNT_ORDER[candidate_idx]
                break

    fallback_estimates = (
        _condition_estimates(session, city, fallback_room_count, all_cities, base_url)
        if fallback_room_count else None
    )
    return {
        "roomCount": room_count,
        "estimates": primary,
        "fallbackRoomCount": fallback_room_count,
        "fallbackEstimates": fallback_estimates,
    }
