#!/usr/bin/env python3
"""Housing-company page fetching/parsing, shared by both pipelines.

The page embeds four separate arrays: publishedAnnouncements/
unpublishedAnnouncements (sale listings - what the hobby pipeline's
BuildingHistory feature uses) and publishedRentalAnnouncements/
unpublishedRentalAnnouncements (rental listings for the same building -
often literally the same physical unit, re-rented repeatedly over years,
which makes this a far more precise rent comp than a city/district-wide
aggregate). These are genuinely separate keys, not one array mixing both
kinds of listing together.
"""
import json

import requests


def extract_embedded_json(html: str, key: str):
    """Pull out the JSON value for "key": ... embedded in a page's inline
    script, by bracket-matching rather than assuming the rest of the page
    parses as JSON."""
    marker = f'"{key}":'
    idx = html.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    while html[start] in " \t\n\r":
        start += 1
    if html[start] not in "[{":
        return None
    open_ch, close_ch = html[start], ("]" if html[start] == "[" else "}")
    depth, in_string, escape = 0, False, False
    for i in range(start, len(html)):
        c = html[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return json.loads(html[start:i + 1])
    return None


def fetch_housing_company_page(session: requests.Session, business_id: str, base_url: str) -> str:
    # municipality/district path segments are decorative - the server
    # resolves purely off the business ID and 301-redirects to the real URL
    resp = session.get(
        f"{base_url}/myytavat-asunnot/x/x/taloyhtiot/{business_id}",
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def fetch_building_history(session: requests.Session, business_id: str, base_url: str) -> list:
    """Sale history for this building (hobby pipeline's BuildingHistory)."""
    html = fetch_housing_company_page(session, business_id, base_url)
    return extract_embedded_json(html, "unpublishedAnnouncements") or []


def fetch_building_rental_history(session: requests.Session, business_id: str, base_url: str) -> tuple[list, list]:
    """(active, past) rental listings for this specific building - often the
    exact same unit, re-rented repeatedly over years. Much more precise than
    a city/district-wide comp, when it exists at all (not every building has
    rental history)."""
    html = fetch_housing_company_page(session, business_id, base_url)
    active = extract_embedded_json(html, "publishedRentalAnnouncements") or []
    past = extract_embedded_json(html, "unpublishedRentalAnnouncements") or []
    return active, past
