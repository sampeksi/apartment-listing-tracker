#!/usr/bin/env python3
"""Housing-company page fetching/parsing, shared by both pipelines.

The hobby pipeline uses this for its BuildingHistory sheet (sale history).
The business pipeline uses it to ground rent estimates in a building's own
actual rental history, rather than only a city/district-wide market
aggregate - the same page's "unpublishedAnnouncements" array turns out to
mix sale AND rental entries together, distinguished by rental-only fields
(rentalUniqueNo, rentPeriod) that sale entries don't have.
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


def fetch_unpublished_history(session: requests.Session, business_id: str, base_url: str) -> list:
    """A building's full unpublished-listing history - sale and rental
    entries mixed together, undifferentiated."""
    # municipality/district path segments are decorative - the server
    # resolves purely off the business ID and 301-redirects to the real URL
    resp = session.get(
        f"{base_url}/myytavat-asunnot/x/x/taloyhtiot/{business_id}",
        timeout=30,
    )
    resp.raise_for_status()
    return extract_embedded_json(resp.text, "unpublishedAnnouncements") or []


def split_sale_and_rental(records: list) -> tuple[list, list]:
    """Rental entries carry rentalUniqueNo and rentPeriod (their searchPrice
    is a monthly rent, not a sale price); sale entries have neither."""
    sale, rental = [], []
    for r in records:
        if r.get("rentalUniqueNo") or r.get("rentPeriod"):
            rental.append(r)
        else:
            sale.append(r)
    return sale, rental


def fetch_building_history(session: requests.Session, business_id: str, base_url: str) -> list:
    """Backwards-compatible alias for the hobby pipeline: sale-only history
    (what it's always meant by "building history")."""
    sale, _rental = split_sale_and_rental(fetch_unpublished_history(session, business_id, base_url))
    return sale
