#!/usr/bin/env python3
"""Fetch new Etuovi listings matching saved criteria and append them to an Excel sheet.

Uses Etuovi's public (unauthenticated) announcement search API. District-level
filtering (e.g. "Kaleva") isn't resolvable by name through the API, so this
fetches all matching listings for the configured city and filters client-side
on the listing's addressLine2.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from openpyxl import Workbook, load_workbook

API_URL = "https://www.etuovi.com/api/v2/announcements/search/listpage"
PAGE_SIZE = 100
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

ROOM_COUNT_LABELS = {
    "ONE_ROOM": "1",
    "TWO_ROOMS": "2",
    "THREE_ROOMS": "3",
    "FOUR_ROOMS": "4",
    "FIVE_ROOMS": "5",
    "SIX_ROOMS": "6",
    "SEVEN_ROOMS_OR_MORE": "7+",
}

COLUMNS = [
    "FriendlyId", "Address", "District", "Price", "PreviousPrice", "Rooms",
    "RoomStructure", "AreaM2", "Floor", "YearBuilt", "Agency", "Published",
    "PublishedOrUpdated", "Link", "FirstSeen",
]

REPO_ROOT = Path(__file__).resolve().parent


def load_criteria(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_search_body(criteria: dict) -> dict:
    body = {
        "propertyType": "RESIDENTIAL",
        "locationSearchCriteria": {"unclassifiedLocationTerms": [criteria["city"]]},
        "newBuildingSearchCriteria": criteria.get("newBuildingSearchCriteria", "NO_NEW_BUILDINGS"),
        "overallConditions": criteria.get("overallConditions") or [],
        "ownershipTypes": criteria.get("ownershipTypes") or ["OWN"],
        "residentialPropertyTypes": ["APARTMENT_HOUSE"],
        "bidType": criteria.get("bidType", "ALL"),
        "sellerType": criteria.get("sellerType", "ALL"),
        "publishingTimeSearchCriteria": "ANY_DAY",
        "showingSearchCriteria": {},
        "priceMax": criteria.get("priceMax"),
        "priceMin": criteria.get("priceMin"),
        "roomCounts": criteria.get("roomCounts"),
        "yearMin": criteria.get("yearMin"),
        "yearMax": criteria.get("yearMax"),
        "sortingOrder": {"property": "PUBLISHED_OR_UPDATED_AT", "direction": "DESC"},
    }
    return {k: v for k, v in body.items() if v is not None}


def fetch_all_listings(criteria: dict) -> list:
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    body = build_search_body(criteria)

    listings = []
    first_result = 0
    while True:
        body["pagination"] = {
            "firstResult": first_result,
            "maxResults": PAGE_SIZE,
            "page": first_result // PAGE_SIZE + 1,
        }
        resp = session.post(API_URL, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("announcements", [])
        listings.extend(batch)
        total = data.get("countOfAllResults", 0)
        first_result += len(batch)
        if not batch or first_result >= total:
            break
    return listings


def matches_districts(listing: dict, districts: list) -> bool:
    if not districts:
        return True
    addr2 = listing.get("addressLine2", "") or ""
    return any(d.lower() in addr2.lower() for d in districts)


def ms_to_iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def listing_to_row(listing: dict, first_seen: str) -> dict:
    office = listing.get("office") or {}
    room_count = listing.get("roomCount")
    return {
        "FriendlyId": listing.get("friendlyId"),
        "Address": listing.get("addressLine1"),
        "District": listing.get("addressLine2"),
        "Price": listing.get("searchPrice"),
        "PreviousPrice": listing.get("previousPrice"),
        "Rooms": ROOM_COUNT_LABELS.get(room_count, room_count),
        "RoomStructure": listing.get("roomStructure"),
        "AreaM2": listing.get("totalArea") or listing.get("area"),
        "Floor": listing.get("floorLevel"),
        "YearBuilt": listing.get("constructionFinishedYear"),
        "Agency": office.get("name"),
        "Published": ms_to_iso(listing.get("publishingTime")),
        "PublishedOrUpdated": ms_to_iso(listing.get("publishedOrUpdatedAt")),
        "Link": f"https://www.etuovi.com/kohde/{listing.get('friendlyId')}",
        "FirstSeen": first_seen,
    }


def load_workbook_or_create(path: Path):
    if path.exists():
        wb = load_workbook(path)
        ws = wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Listings"
        ws.append(COLUMNS)
    return wb, ws


def existing_ids(ws) -> set:
    ids = set()
    header = [c.value for c in ws[1]]
    if "FriendlyId" not in header:
        return ids
    id_col = header.index("FriendlyId")
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and row[id_col] is not None:
            ids.add(str(row[id_col]))
    return ids


def main():
    criteria_path = Path(os.environ.get("CRITERIA_PATH", REPO_ROOT / "config" / "criteria.json"))
    excel_path = Path(os.environ.get("EXCEL_PATH", REPO_ROOT / "etuovi_listings.xlsx"))

    criteria = load_criteria(criteria_path)
    listings = fetch_all_listings(criteria)
    districts = criteria.get("districts") or []
    listings = [l for l in listings if matches_districts(l, districts)]

    wb, ws = load_workbook_or_create(excel_path)
    seen = existing_ids(ws)

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for listing in listings:
        fid = str(listing.get("friendlyId"))
        if fid in seen:
            continue
        row = listing_to_row(listing, now)
        ws.append([row[c] for c in COLUMNS])
        seen.add(fid)
        added += 1

    wb.save(excel_path)
    print(f"Fetched {len(listings)} matching listings, added {added} new rows to {excel_path}")


if __name__ == "__main__":
    main()
