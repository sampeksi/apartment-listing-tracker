#!/usr/bin/env python3
"""Fetch new apartment listings for the investment-analysis business flow.

No Excel, no Google Drive - this flow is fetch -> analyze -> Telegram.
State is a small JSON file (friendlyId -> first-seen date) committed back to
the git repo by the workflow, just enough to know what's already been seen.
New listings (not yet analyzed/notified) are written to NEW_LISTINGS_PATH for
the next stage (DCF calc + LLM + Telegram, not built yet) to pick up.

A first run against existing search criteria naturally acts as a backfill:
everything found gets recorded as seen, so only genuinely new listings on
later runs show up as "new".

BASE_URL is not hardcoded - see fetch_listings.py for why.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

PAGE_SIZE = 100
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

REPO_ROOT = Path(__file__).resolve().parent


def load_criteria(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    return session


def build_search_body(criteria: dict) -> dict:
    body = {
        "propertyType": "RESIDENTIAL",
        "locationSearchCriteria": {"unclassifiedLocationTerms": criteria["cities"]},
        "newBuildingSearchCriteria": criteria.get("newBuildingSearchCriteria", "ALL_PROPERTIES"),
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


def main():
    criteria_path = Path(os.environ.get("BUSINESS_CRITERIA_PATH", REPO_ROOT / "config" / "business_criteria.json"))
    state_path = Path(os.environ.get("BUSINESS_STATE_PATH", REPO_ROOT / "state" / "business_seen.json"))
    new_listings_path = Path(os.environ.get("NEW_LISTINGS_PATH", REPO_ROOT / "new_business_listings.json"))
    base_url = os.environ.get("LISTING_SITE_BASE_URL")
    if not base_url:
        raise SystemExit("LISTING_SITE_BASE_URL environment variable must be set")

    config = load_criteria(criteria_path)
    profiles = config["profiles"]

    state = load_state(state_path)
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    session = make_session()

    new_listings = []
    for profile in profiles:
        listings = fetch_all_listings(session, profile, base_url)

        added = 0
        for listing in listings:
            fid = str(listing.get("friendlyId"))
            if fid in state:
                continue
            state[fid] = today
            listing["_profile"] = profile["name"]
            new_listings.append(listing)
            added += 1

        print(f"[{profile['name']}] fetched {len(listings)} matching listings, {added} new")

    save_state(state_path, state)

    with open(new_listings_path, "w", encoding="utf-8") as f:
        json.dump(new_listings, f, ensure_ascii=False, indent=2)

    print(f"Done. {len(new_listings)} new listings written to {new_listings_path}")
    if new_listings:
        print("(DCF calc + LLM analysis + Telegram delivery not built yet - "
              "new listings are recorded as seen but not yet analyzed/sent.)")


if __name__ == "__main__":
    main()
