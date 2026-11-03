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
    "RoomStructure", "AreaM2", "Floor", "YearBuilt", "Published",
    "PublishedOrUpdated", "Link", "FirstSeen", "Status", "RemovedDate",
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


def ms_to_date_str(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d.%m.%Y")


def listing_to_row(listing: dict, first_seen: str) -> dict:
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
        "Published": ms_to_date_str(listing.get("publishingTime")),
        "PublishedOrUpdated": ms_to_date_str(listing.get("publishedOrUpdatedAt")),
        "Link": f"https://www.etuovi.com/kohde/{listing.get('friendlyId')}",
        "FirstSeen": first_seen,
        "Status": "Active",
        "RemovedDate": None,
    }


def load_or_create_workbook(path: Path) -> Workbook:
    if path.exists():
        return load_workbook(path)
    return Workbook()


def get_or_create_sheet(wb: Workbook, sheet_name: str):
    if sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if [c.value for c in ws[1]] == COLUMNS:
            return ws
        # column schema changed since this sheet was created; it's just a
        # fetched mirror of Etuovi's live data, so rebuild it from scratch
        # rather than risk misaligned columns
        del wb[sheet_name]
    # create_sheet() always starts a genuinely empty sheet at row 1; reusing
    # the workbook's default "Sheet" by renaming it instead leaves a phantom
    # blank row 1 that shifts the header down and breaks dedup on reload.
    ws = wb.create_sheet(title=sheet_name)
    ws.append(COLUMNS)
    if "Sheet" in wb.sheetnames and wb["Sheet"].max_row <= 1 and wb["Sheet"]["A1"].value is None:
        del wb["Sheet"]
    return ws


def header_map(ws) -> dict:
    return {c.value: i + 1 for i, c in enumerate(ws[1])}  # 1-based column index


def existing_rows(ws) -> dict:
    """Map FriendlyId -> (row_number, status) for all existing data rows."""
    cols = header_map(ws)
    id_col = cols.get("FriendlyId")
    status_col = cols.get("Status")
    if id_col is None:
        return {}
    result = {}
    for row_idx in range(2, ws.max_row + 1):
        fid = ws.cell(row=row_idx, column=id_col).value
        if fid is None:
            continue
        status = ws.cell(row=row_idx, column=status_col).value if status_col else None
        result[str(fid)] = (row_idx, status)
    return result


def main():
    criteria_path = Path(os.environ.get("CRITERIA_PATH", REPO_ROOT / "config" / "criteria.json"))
    excel_path = Path(os.environ.get("EXCEL_PATH", REPO_ROOT / "etuovi_listings.xlsx"))

    config = load_criteria(criteria_path)
    profiles = config["profiles"]

    wb = load_or_create_workbook(excel_path)
    if "Listings" in wb.sheetnames:
        # leftover from the original single-profile version, fully superseded
        # by the per-profile sheets below
        del wb["Listings"]

    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    total_added = 0

    for profile in profiles:
        listings = fetch_all_listings(profile)
        districts = profile.get("districts") or []
        listings = [l for l in listings if matches_districts(l, districts)]

        sheet_name = profile.get("sheet", profile["name"])
        ws = get_or_create_sheet(wb, sheet_name)
        cols = header_map(ws)
        existing = existing_rows(ws)

        current_ids = set()
        added = 0
        for listing in listings:
            fid = str(listing.get("friendlyId"))
            current_ids.add(fid)
            if fid in existing:
                row_idx, status = existing[fid]
                if status == "Removed":
                    ws.cell(row=row_idx, column=cols["Status"]).value = "Active"
                    ws.cell(row=row_idx, column=cols["RemovedDate"]).value = None
                continue
            row = listing_to_row(listing, today)
            ws.append([row[c] for c in COLUMNS])
            added += 1

        removed = 0
        for fid, (row_idx, status) in existing.items():
            if fid not in current_ids and status != "Removed":
                ws.cell(row=row_idx, column=cols["Status"]).value = "Removed"
                ws.cell(row=row_idx, column=cols["RemovedDate"]).value = today
                removed += 1

        print(
            f"[{profile['name']}] fetched {len(listings)} matching listings, "
            f"added {added} new, marked {removed} as removed, in sheet '{sheet_name}'"
        )
        total_added += added

    wb.save(excel_path)
    print(f"Done. {total_added} new rows added total to {excel_path}")


if __name__ == "__main__":
    main()
