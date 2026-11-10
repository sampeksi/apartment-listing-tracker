#!/usr/bin/env python3
"""LLM-based per-listing investment analysis.

One Claude API call per listing does double duty:
1. Estimates inputs the DCF model can't get from structured listing data -
   the housing company's remaining loan term (from renovation history text),
   and any disclosed/upcoming renovation's effect on debt, rent, and value.
2. Writes the investment narrative/recommendation, given the ALREADY-COMPUTED
   baseline DCF scenario numbers. The LLM never does the financial
   arithmetic itself - only business_dcf.py does that - it only interprets
   the numbers and fills in the judgment-call inputs the deterministic
   model can't derive from structured fields alone.

Needs ANTHROPIC_API_KEY set (your own API account - see README; this is a
separate thing from a Claude.ai subscription).
"""
import json

import anthropic

MODEL = "claude-sonnet-5-5"

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "estimatedMonthlyRent": {
            "type": "number",
            "description": (
                "Your best estimate of achievable monthly rent for this "
                "specific apartment. Weigh the generic area-wide rent comp "
                "against this building's own rental history when available - "
                "the building's own history is more precise, especially when "
                "a same-size/same-layout unit in it has been repeatedly "
                "re-rented. Default to the generic comp when building-specific "
                "history is sparse, absent, or doesn't suggest a real "
                "difference - don't invent a deviation without a basis."
            ),
        },
        "housingCompanyLoanTermYears": {
            "type": ["integer", "null"],
            "description": (
                "Best estimate of the housing company's remaining loan "
                "amortization term in years, based on disclosed renovation "
                "history/financing details. Null if the text gives no basis "
                "for an estimate - a fallback will be used in that case."
            ),
        },
        "additionalFutureDebtShare": {
            "type": "number",
            "description": (
                "This apartment's share (EUR) of any disclosed UPCOMING "
                "(not yet reflected in the current debt-free price) housing "
                "company renovation loan, if explicitly stated or reliably "
                "inferable from the text. 0 if none."
            ),
        },
        "rentUpliftPct": {
            "type": "number",
            "description": (
                "Estimated fractional increase in achievable rent after a "
                "disclosed upcoming renovation completes, relative to "
                "current comparable rents (e.g. 0.05 for +5%). 0 if no "
                "upcoming renovation or no real basis for an estimate."
            ),
        },
        "valueUpliftPct": {
            "type": "number",
            "description": (
                "Estimated fractional increase in the apartment's market "
                "value after a disclosed upcoming renovation completes "
                "(e.g. 0.08 for +8%). 0 if no basis for an estimate."
            ),
        },
        "estimateConfidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": (
                "Confidence in the renovation-related estimates above, "
                "given how specific or vague the disclosed text is."
            ),
        },
        "narrative": {
            "type": "string",
            "description": (
                "2-4 sentence investment analysis summary for a real "
                "estate investor, given the provided DCF scenario numbers, "
                "rent comp data, and listing details."
            ),
        },
        "flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Short, specific red/green flags, e.g. 'thin rent margin', "
                "'strong building sale history', 'renovation already "
                "financed via disclosed 335k loan'."
            ),
        },
    },
    "required": [
        "estimatedMonthlyRent", "housingCompanyLoanTermYears",
        "additionalFutureDebtShare", "rentUpliftPct", "valueUpliftPct",
        "estimateConfidence", "narrative", "flags",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a real estate investment analyst reviewing Finnish buy-to-let apartment listings for a property investment company. You're given a specific listing's details (including Finnish-language disclosure text about the housing company's condition and renovation history/plans), comparable rent data (both a generic area-wide comp and, when available, this exact building's own rental history), and pre-computed DCF investment scenarios (unlevered and leveraged yield/return metrics across multiple financing scenarios). Those numbers are already correct, computed deterministically - your job is not to recompute or second-guess the arithmetic, only to interpret it and fill in the inputs that required judgment.

Three things you need to estimate, since they're not in any structured field:
1. Achievable monthly rent - weigh the building's own rental history (when present) against the generic area-wide comp; a generic city or district-wide comp can be skewed by a large, varied market (e.g. a big city's average mixes cheap suburbs with its expensive center), while a building's own history - especially the same-size unit being repeatedly re-rented - is a much more precise, directly comparable figure. Default to the generic comp when building-specific history is sparse or absent.
2. The housing company's own loan's remaining amortization term - Finnish disclosure text sometimes states this directly (an explicit completion date for an ongoing renovation loan, or an explicit repayment plan); otherwise return null and a fallback will be used.
3. Any UPCOMING (not yet reflected in the current price) disclosed renovation's likely effect on this apartment's future debt share, achievable rent, and market value - only when the text gives a real basis (e.g. an explicit per-apartment debt-share euro figure, or a dated, scoped renovation plan). If the text is vague or there's no upcoming renovation mentioned, return zeros rather than guessing.

Be conservative: a wrong estimate here feeds directly into real investment decisions, so when the data doesn't give a clear basis, default to the safer/more generic figure (or null/0 for the renovation estimates) rather than inventing a plausible-sounding number."""


def format_rental_history(active: list, past: list) -> str:
    if not active and not past:
        return "none available for this building"
    lines = []
    for r in active:
        lines.append(f"- ACTIVE: {r.get('area')}m², {r.get('searchPrice')} EUR/mo, {r.get('roomStructure')}")
    for r in past[:15]:  # most recent first, already sorted by the source data
        lines.append(f"- past: {r.get('area')}m², {r.get('searchPrice')} EUR/mo, {r.get('roomStructure')}")
    return "\n".join(lines)


def build_user_message(
    listing_details: dict, rent_estimate, scenarios: dict,
    building_rental_history: tuple[list, list] = ([], []),
) -> str:
    property_info = listing_details.get("property") or {}
    housing_company = property_info.get("housingCompany") or {}
    residence = listing_details.get("residenceDetailsDTO") or {}
    inspection = residence.get("inspection") or {}

    # open-bidding ("tarjouskauppa") listings have no fixed sellingPrice/
    # debfFreePrice - only a starting bid, under different field names
    selling_price = listing_details.get("sellingPrice") or listing_details.get("openBiddingStartingSellingPrice")
    debt_free_price = listing_details.get("debfFreePrice") or listing_details.get("openBiddingStartingDebtFreePrice")
    bidding_note = " (open bidding - this is the starting price, not a fixed asking price)" if listing_details.get("openBiddingInUse") else ""

    parts = [
        f"Address: {property_info.get('streetAddressFreeForm', 'unknown')}",
        f"Selling price: {selling_price} EUR{bidding_note}",
        f"Debt-free price: {debt_free_price} EUR{bidding_note}",
        f"Construction year: {residence.get('constructionFinishedYear')}",
        f"Room structure: {residence.get('roomStructure')}",
        f"Overall condition: {inspection.get('overallCondition')}",
        "",
        "Listing description:",
        (listing_details.get("text") or "")[:2000],
        "",
        "Housing company renovation history (done):",
        (housing_company.get("renovationsDoneDescription") or "none disclosed")[:2000],
        "",
        "Housing company renovation plans (upcoming):",
        (housing_company.get("renovationsPlannedDescription") or "none disclosed")[:2000],
        "",
        f"Generic area-wide rent comp: {rent_estimate.median_rent:.0f} EUR/month "
        f"(median {rent_estimate.median_rent_per_sqm:.2f} EUR/sqm, "
        f"sample size {rent_estimate.sample_size}"
        f"{', widened to regional cluster' if rent_estimate.widened_to_region else ''})",
        "",
        "This specific building's own rental history (more precise when present):",
        format_rental_history(*building_rental_history),
        "",
        "Baseline DCF scenarios (using the generic comp above, fallback housing-company loan term, no renovation adjustment):",
        json.dumps(scenarios, indent=2),
    ]
    return "\n".join(parts)


def analyze_listing(
    listing_details: dict, rent_estimate, scenarios: dict,
    building_rental_history: tuple[list, list] = ([], []),
) -> dict:
    client = anthropic.Anthropic()
    user_message = build_user_message(listing_details, rent_estimate, scenarios, building_rental_history)

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    )
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)
