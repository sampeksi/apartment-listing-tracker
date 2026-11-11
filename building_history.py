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

Also: a fallback to resolve a housing company's businessId (Y-tunnus) by
name, for when Etuovi's own announcement/details API doesn't include one
(confirmed happening - not every listing has it, even though the Etuovi
housing-company page itself exists once you have the Y-tunnus). Uses PRH's
(Finland's Patent and Registration Office) open-data company search,
avoindata.prh.fi - explicitly meant for this kind of programmatic lookup,
unlike the interactive tietopalvelu.ytj.fi search, whose terms explicitly
ban robotic/programmatic queries. Per PRH's stated attribution requirement:
company data sourced from PRH's avoindata.prh.fi open data service.
"""
import json

import requests

PRH_COMPANY_SEARCH_URL = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies"


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


def _prh_company_search(session: requests.Session, name: str) -> list:
    resp = session.get(
        PRH_COMPANY_SEARCH_URL,
        params={"name": name, "companyForm": "AOY"},
        headers={"accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("companies", [])


def _company_postal_codes(company: dict) -> set:
    return {a.get("postCode") for a in company.get("addresses", []) if a.get("postCode")}


def _strip_company_form_prefix(name: str):
    # e.g. "As Oy Halimaanhaka" -> "Halimaanhaka" - the registered name
    # sometimes omits the informal "As"/"Asunto" prefix Etuovi displays
    idx = name.find(" Oy ")
    if idx == -1:
        return None
    stripped = name[idx + len(" Oy "):].strip()
    return stripped or None


def _strip_municipality_suffix(name: str):
    # e.g. "Asunto Oy Kirkkoharju, Kangasala" -> "Asunto Oy Kirkkoharju" -
    # Etuovi sometimes appends a disambiguating municipality after a comma
    # that isn't part of the company's actual registered name
    if "," not in name:
        return None
    stripped = name.split(",", 1)[0].strip()
    return stripped or None


def resolve_business_id(session: requests.Session, housing_company_name: str, postal_code: str | None) -> str | None:
    """Resolve a missing businessId (Y-tunnus) by housing company name via
    PRH's open-data company search. Only meant to be called when Etuovi's
    own data has no businessId already.

    Tries the name as-is, then two fallback variants if that yields zero
    results (Etuovi's displayed name doesn't always match the registered
    one exactly). At any point multiple results come back, disambiguates by
    the listing's postal code; if that doesn't narrow it to exactly one,
    gives up rather than guessing - a wrong businessId would pull in a
    different building's rental history entirely."""
    if not housing_company_name:
        return None

    name_attempts = [housing_company_name]
    stripped_prefix = _strip_company_form_prefix(housing_company_name)
    if stripped_prefix:
        name_attempts.append(stripped_prefix)
    stripped_suffix = _strip_municipality_suffix(housing_company_name)
    if stripped_suffix:
        name_attempts.append(stripped_suffix)

    for attempt in name_attempts:
        companies = _prh_company_search(session, attempt)
        if not companies:
            continue
        if len(companies) == 1:
            return companies[0]["businessId"]["value"]
        if postal_code:
            matches = [c for c in companies if postal_code in _company_postal_codes(c)]
            if len(matches) == 1:
                return matches[0]["businessId"]["value"]
        return None  # still ambiguous - don't guess
    return None
