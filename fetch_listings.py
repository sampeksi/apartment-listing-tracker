#!/usr/bin/env python3
"""Fetch new listings matching saved criteria and append them to an Excel sheet.

Uses the target site's public (unauthenticated) announcement search API.
District-level filtering (e.g. "Kaleva") isn't resolvable by name through the
API, so this fetches all matching listings for the configured city and
filters client-side on the listing's addressLine2.

BASE_URL is deliberately not hardcoded here - it comes only from the
LISTING_SITE_BASE_URL env var (a GitHub secret in CI), so the target site
isn't a plain string in this source file.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from openpyxl import Workbook, load_workbook

from building_history import fetch_building_history

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

BUILDING_HISTORY_SHEET = "BuildingHistory"
BUILDING_HISTORY_COLUMNS = [
    "Id", "BusinessId", "HousingCompanyName", "Address", "District", "Price",
    "PricePerSqm", "AreaM2", "RoomStructure", "DaysOnMarket", "UnpublishedDate",
    "FirstSeen",
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


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    return session


def fetch_all_listings(session: requests.Session, criteria: dict, base_url: str) -> list:
    body = build_search_body(criteria)
    url = f"{base_url}/api/v2/announcements/search/listpage"

    listings = []
    first_result = 0
    while True:
        body["pagination"] = {
            "firstResult": first_result,
            "maxResults": PAGE_SIZE,
            "page": first_result // PAGE_SIZE + 1,
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


def fetch_announcement_details(session: requests.Session, friendly_id: str, base_url: str) -> dict:
    resp = session.get(
        f"{base_url}/api/v2/announcement/details",
        params={"friendlyId": friendly_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def matches_districts(listing: dict, districts: list) -> bool:
    if not districts:
        return True
    addr2 = listing.get("addressLine2", "") or ""
    return any(d.lower() in addr2.lower() for d in districts)


def ms_to_date_str(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d.%m.%Y")


def listing_to_row(listing: dict, first_seen: str, base_url: str) -> dict:
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
        "Link": f"{base_url}/kohde/{listing.get('friendlyId')}",
        "FirstSeen": first_seen,
        "Status": "Active",
        "RemovedDate": None,
    }


def building_history_row(business_id: str, company_name, record: dict, first_seen: str) -> dict:
    return {
        "Id": str(record.get("id")),
        "BusinessId": business_id,
        "HousingCompanyName": company_name,
        "Address": record.get("addressLine1"),
        "District": record.get("addressLine2"),
        "Price": record.get("searchPrice"),
        "PricePerSqm": record.get("pricePerSquare"),
        "AreaM2": record.get("area"),
        "RoomStructure": record.get("roomStructure"),
        "DaysOnMarket": record.get("daysOnMarket"),
        "UnpublishedDate": ms_to_date_str(record.get("unpublishingTime")),
        "FirstSeen": first_seen,
    }


def load_or_create_workbook(path: Path) -> Workbook:
    if path.exists():
        return load_workbook(path)
    return Workbook()


def get_or_create_sheet(wb: Workbook, sheet_name: str, columns: list):
    if sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if [c.value for c in ws[1]] == columns:
            return ws
        # column schema changed since this sheet was created; it's just a
        # fetched mirror of the source site's live data, so rebuild it from
        # scratch rather than risk misaligned columns
        del wb[sheet_name]
    # create_sheet() always starts a genuinely empty sheet at row 1; reusing
    # the workbook's default "Sheet" by renaming it instead leaves a phantom
    # blank row 1 that shifts the header down and breaks dedup on reload.
    ws = wb.create_sheet(title=sheet_name)
    ws.append(columns)
    if "Sheet" in wb.sheetnames and wb["Sheet"].max_row <= 1 and wb["Sheet"]["A1"].value is None:
        del wb["Sheet"]
    return ws


def existing_values(ws, column_name: str) -> set:
    header = [c.value for c in ws[1]]
    if column_name not in header:
        return set()
    col_idx = header.index(column_name)
    return {
        str(row[col_idx]) for row in ws.iter_rows(min_row=2, values_only=True)
        if row and row[col_idx] is not None
    }


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
    excel_path = Path(os.environ.get("EXCEL_PATH", REPO_ROOT / "listings.xlsx"))
    base_url = os.environ.get("LISTING_SITE_BASE_URL")
    if not base_url:
        raise SystemExit("LISTING_SITE_BASE_URL environment variable must be set")

    config = load_criteria(criteria_path)
    profiles = config["profiles"]

    wb = load_or_create_workbook(excel_path)
    if "Listings" in wb.sheetnames:
        # leftover from the original single-profile version, fully superseded
        # by the per-profile sheets below
        del wb["Listings"]

    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    total_added = 0
    session = make_session()
    all_listings = []

    for profile in profiles:
        listings = fetch_all_listings(session, profile, base_url)
        districts = profile.get("districts") or []
        listings = [l for l in listings if matches_districts(l, districts)]
        all_listings.extend(listings)

        sheet_name = profile.get("sheet", profile["name"])
        ws = get_or_create_sheet(wb, sheet_name, COLUMNS)
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
            row = listing_to_row(listing, today, base_url)
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

    # --- building history enrichment ---
    # resolve each listing's housing company, deduped by business ID so a
    # building with several listings only costs one history fetch
    business_ids = {}
    for listing in all_listings:
        fid = listing.get("friendlyId")
        if not fid:
            continue
        try:
            details = fetch_announcement_details(session, fid, base_url)
        except requests.RequestException as e:
            print(f"Warning: could not fetch details for {fid}: {e}")
            continue
        housing_company = (details.get("property") or {}).get("housingCompany") or {}
        business_id = housing_company.get("businessId")
        if business_id and business_id not in business_ids:
            business_ids[business_id] = housing_company.get("name")

    bh_ws = get_or_create_sheet(wb, BUILDING_HISTORY_SHEET, BUILDING_HISTORY_COLUMNS)
    seen_record_ids = existing_values(bh_ws, "Id")
    bh_added = 0
    for business_id, company_name in business_ids.items():
        try:
            history = fetch_building_history(session, business_id, base_url)
        except requests.RequestException as e:
            print(f"Warning: could not fetch building history for {business_id}: {e}")
            continue
        for record in history:
            record_id = str(record.get("id"))
            if record_id in seen_record_ids:
                continue
            row = building_history_row(business_id, company_name, record, today)
            bh_ws.append([row[c] for c in BUILDING_HISTORY_COLUMNS])
            seen_record_ids.add(record_id)
            bh_added += 1

    print(
        f"[building-history] resolved {len(business_ids)} unique buildings, "
        f"added {bh_added} new history rows"
    )

    wb.save(excel_path)
    print(f"Done. {total_added} new rows added total to {excel_path}")


if __name__ == "__main__":
    main()
