#!/usr/bin/env python3
"""Per-listing DCF + LLM analysis, delivered as a Telegram digest.

Reads NEW_LISTINGS_PATH (written by fetch_business.py - raw search-result
listings, missing the richer per-listing fields this needs), then for each:
fetches full details, gets a rent comp estimate, runs baseline DCF scenarios
(fallback housing-company loan term), asks the LLM to fill in the loan term
(re-running DCF with that refined term - the one thing it's safe to silently
recompute, a single clean variable substitution) and any renovation-driven
estimates (presented as separate line items, not auto-merged into the DCF
numbers - merging them correctly would mean handling compounding effects on
the housing-debt/value/selling-price relationship, which risks introducing
subtle errors into numbers that are meant to be trustworthy). Sends one
Telegram message per listing.

One listing failing (bad data, API hiccup, no rent comps) doesn't abort the
whole batch - logged and skipped, matching the rest of this project's
resilience pattern.

Test mode, for iterating on rent-estimate quality without touching real
state or sending real Telegram messages:
- DRY_RUN=true - print each formatted message instead of sending it.
- TEST_FRIENDLY_IDS=id1,id2 - analyze exactly these listings instead of
  reading NEW_LISTINGS_PATH.
- RANDOM_SAMPLE_COUNT=N - analyze N random listings currently matching
  config/business_criteria.json (ignored if TEST_FRIENDLY_IDS is set).
Neither test input touches state/business_seen.json - only fetch_business.py
does that.
"""
import json
import os
import random
from pathlib import Path

import requests

from building_history import fetch_building_rental_history, resolve_business_id
from business_dcf import ListingInputs, load_assumptions, run_scenarios
from fetch_business import fetch_all_listings
from llm_analysis import analyze_listing
from rent_comps import make_session as make_rental_session, estimate_rent
from telegram_notify import send_message

REPO_ROOT = Path(__file__).resolve().parent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def make_sale_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
    return session


def fetch_announcement_details(session: requests.Session, friendly_id: str, base_url: str) -> dict:
    resp = session.get(
        f"{base_url}/api/v2/announcement/details",
        params={"friendlyId": friendly_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_selling_price(details: dict) -> float:
    # open-bidding ("tarjouskauppa") listings have no fixed sellingPrice -
    # only a starting bid, under a completely different field name
    return details.get("sellingPrice") or details.get("openBiddingStartingSellingPrice") or 0.0


def get_debt_free_price(details: dict) -> float:
    return details.get("debfFreePrice") or details.get("openBiddingStartingDebtFreePrice") or 0.0


def build_listing_inputs(details: dict, monthly_rent: float, loan_term: float | None = None) -> ListingInputs:
    property_info = details.get("property") or {}
    residence = details.get("residenceDetailsDTO") or {}
    charges = property_info.get("periodicCharges") or []
    maintenance = next(
        (c["price"] for c in charges if c.get("periodicCharge") == "HOUSING_COMPANY_MAINTENANCE_CHARGE"),
        0.0,
    )
    return ListingInputs(
        selling_price=get_selling_price(details),
        debt_free_price=get_debt_free_price(details),
        monthly_rent=monthly_rent,
        monthly_maintenance_charge=maintenance,
        city=(property_info.get("municipality") or {}).get("defaultName", ""),
        room_count=residence.get("roomCount", ""),
        housing_company_loan_term_years=loan_term,
    )


def format_pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


SCENARIO_ORDER = ("conservative", "balanced", "fullLeverage")


def format_message(listing: dict, details: dict, rent_estimate, scenarios: dict, llm_result: dict) -> str:
    property_info = details.get("property") or {}
    address = property_info.get("streetAddressFreeForm") or listing.get("addressLine1", "")
    friendly_id = listing.get("friendlyId")

    lines = [
        f"*{address}*",
        listing.get("addressLine2", ""),
        f"Selling price: {get_selling_price(details):,.0f} €"
        f"{' (open bidding, starting price)' if details.get('openBiddingInUse') else ''} | "
        f"Debt-free: {get_debt_free_price(details):,.0f} €",
        "",
        f"Area rent comp: {rent_estimate.median_rent:,.0f} €/mo "
        f"(n={rent_estimate.sample_size}"
        f"{', regional' if rent_estimate.widened_to_region else ''})",
    ]

    refined_rent = llm_result.get("estimatedMonthlyRent")
    if refined_rent and abs(refined_rent - rent_estimate.median_rent) >= 1:
        lines.append(f"Rent used in scenarios: {refined_rent:,.0f} €/mo (refined from building history)")

    lines += ["", "*Scenarios (conservative / balanced / full leverage):*"]

    rows = [
        ("netYieldBeforeTax", "Net yield (pre-tax)", "pct"),
        ("cashFlowAfterTaxYear1", "Cash flow Y1 (after tax)", "eur"),
        ("cashFlowBreakEvenYear", "Break-even year", "int"),
        ("totalReturnCashFlowAdjusted", "Total return (adj.)", "pct"),
    ]
    for key, label, kind in rows:
        values = []
        for scenario_name in SCENARIO_ORDER:
            v = (scenarios.get(scenario_name) or {}).get(key)
            if kind == "eur":
                values.append(f"{v:,.0f}€" if v is not None else "n/a")
            elif kind == "int":
                values.append(str(v) if v is not None else "n/a")
            else:
                values.append(format_pct(v))
        lines.append(f"{label}: {' / '.join(values)}")

    lines += ["", f"*Analysis:* {llm_result.get('narrative', '')}"]

    renovation_notes = []
    if llm_result.get("additionalFutureDebtShare"):
        renovation_notes.append(f"- Additional future debt share: {llm_result['additionalFutureDebtShare']:,.0f}€")
    if llm_result.get("rentUpliftPct"):
        renovation_notes.append(f"- Est. rent uplift: {format_pct(llm_result['rentUpliftPct'])}")
    if llm_result.get("valueUpliftPct"):
        renovation_notes.append(f"- Est. value uplift: {format_pct(llm_result['valueUpliftPct'])}")
    if renovation_notes:
        lines += ["", "*Disclosed renovation outlook (not folded into the scenario numbers above):*"] + renovation_notes

    flags = llm_result.get("flags") or []
    if flags:
        lines += ["", "*Flags:*"] + [f"- {f}" for f in flags]

    lines += ["", f"https://www.etuovi.com/kohde/{friendly_id}"]
    return "\n".join(lines)


def main():
    new_listings_path = Path(os.environ.get("NEW_LISTINGS_PATH", REPO_ROOT / "new_business_listings.json"))
    assumptions_path = Path(
        os.environ.get("BUSINESS_ASSUMPTIONS_PATH", REPO_ROOT / "config" / "business_assumptions.json")
    )
    business_criteria_path = Path(
        os.environ.get("BUSINESS_CRITERIA_PATH", REPO_ROOT / "config" / "business_criteria.json")
    )
    sale_base_url = os.environ["LISTING_SITE_BASE_URL"]
    rental_base_url = os.environ["RENTAL_SITE_BASE_URL"]

    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    test_friendly_ids = os.environ.get("TEST_FRIENDLY_IDS", "").strip()
    random_sample_count = os.environ.get("RANDOM_SAMPLE_COUNT", "").strip()

    assumptions = load_assumptions(assumptions_path)
    with open(business_criteria_path, "r", encoding="utf-8") as f:
        criteria = json.load(f)
    all_cities = criteria["profiles"][0]["cities"]

    sale_session = make_sale_session()
    rental_session = make_rental_session()

    if test_friendly_ids:
        new_listings = [{"friendlyId": fid.strip()} for fid in test_friendly_ids.split(",") if fid.strip()]
    elif random_sample_count:
        matching = []
        for profile in criteria["profiles"]:
            matching.extend(fetch_all_listings(sale_session, profile, sale_base_url))
        sample_size = min(int(random_sample_count), len(matching))
        new_listings = random.sample(matching, sample_size)
        print(f"Randomly sampled {sample_size} of {len(matching)} listings matching current criteria.")
    else:
        with open(new_listings_path, "r", encoding="utf-8") as f:
            new_listings = json.load(f)

    if not new_listings:
        print("No new listings to analyze.")
        return

    for listing in new_listings:
        friendly_id = listing.get("friendlyId")
        print(f"Analyzing {friendly_id}...")
        try:
            details = fetch_announcement_details(sale_session, friendly_id, sale_base_url)
            residence = details.get("residenceDetailsDTO") or {}
            property_info = details.get("property") or {}
            city = (property_info.get("municipality") or {}).get("defaultName", "")
            room_count = residence.get("roomCount", "")

            rent_estimate = estimate_rent(rental_session, city, room_count, all_cities, rental_base_url)
            if rent_estimate is None:
                print(f"  No rent comps found for {city}/{room_count}, skipping.")
                continue

            housing_company = property_info.get("housingCompany") or {}
            business_id = housing_company.get("businessId")
            if not business_id:
                postal_code = (property_info.get("postCode") or {}).get("postCode")
                try:
                    business_id = resolve_business_id(sale_session, housing_company.get("name"), postal_code)
                except Exception as e:
                    print(f"  Warning: businessId lookup via PRH failed: {e}")
                if business_id:
                    print(f"  Resolved businessId via PRH company search: {business_id}")
                else:
                    print("  No businessId in listing data and none resolved via PRH, skipping building rental history.")

            building_rental_history = ([], [])
            if business_id:
                try:
                    building_rental_history = fetch_building_rental_history(sale_session, business_id, sale_base_url)
                except Exception as e:
                    print(f"  Warning: could not fetch building rental history: {e}")

            baseline_inputs = build_listing_inputs(details, rent_estimate.median_rent)
            baseline_scenarios = run_scenarios(baseline_inputs, assumptions)

            llm_result = analyze_listing(details, rent_estimate, baseline_scenarios, building_rental_history)

            # safe single-variable substitutions, same reasoning for both:
            # no compounding cross-variable interaction with the rest of the
            # model, unlike the renovation uplift estimates
            refined_rent = llm_result.get("estimatedMonthlyRent") or rent_estimate.median_rent
            loan_term = llm_result.get("housingCompanyLoanTermYears")
            refined_inputs = build_listing_inputs(details, refined_rent, loan_term=loan_term)
            scenarios = run_scenarios(refined_inputs, assumptions)

            message = format_message(listing, details, rent_estimate, scenarios, llm_result)
            if dry_run:
                print(f"  --- DRY RUN: message for {friendly_id} ---\n{message}\n  --- end message ---")
            else:
                send_message(message)
                print(f"  Sent Telegram message for {friendly_id}")
        except Exception as e:
            print(f"  Warning: failed to analyze/notify for {friendly_id}: {e}")
            continue


if __name__ == "__main__":
    main()
