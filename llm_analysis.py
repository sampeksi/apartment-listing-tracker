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
from datetime import datetime, timezone

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
        "rentSourceUsed": {
            "type": "string",
            "enum": [
                "building_history", "condition_matched_comp",
                "adjacent_room_count_condition_adjustment", "disclosed_contract_rent",
                "generic_area_comp",
            ],
            "description": (
                "Which single source primarily drove estimatedMonthlyRent. "
                "building_history = this building's own rental history. "
                "condition_matched_comp = the area comp bucket matching this "
                "apartment's own disclosed condition, for its own room count. "
                "adjacent_room_count_condition_adjustment = derived a relative "
                "condition adjustment from a different room count's condition "
                "breakdown and applied it to another baseline. "
                "disclosed_contract_rent = an explicit rent figure stated in "
                "the listing text itself (e.g. sold with a sitting tenant). "
                "generic_area_comp = the blended, condition-agnostic comp, "
                "used because nothing more specific gave a clear basis. If "
                "more than one source agreed and reinforced each other, name "
                "the most specific one you actually relied on, and explain "
                "the rest in rentReasoning."
            ),
        },
        "rentReasoning": {
            "type": "string",
            "description": (
                "1-2 sentences: specifically what data point(s) drove "
                "estimatedMonthlyRent, and whether/why it deviates from the "
                "generic area-wide comp. This is for diagnosing which inputs "
                "are actually useful, not for the investor-facing narrative - "
                "be concrete (cite numbers/sources), not vague."
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
        "estimatedMonthlyRent", "rentSourceUsed", "rentReasoning", "housingCompanyLoanTermYears",
        "additionalFutureDebtShare", "rentUpliftPct", "valueUpliftPct",
        "estimateConfidence", "narrative", "flags",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a real estate investment analyst reviewing Finnish buy-to-let apartment listings for a property investment company. You're given a specific listing's details (including Finnish-language disclosure text about the housing company's condition and renovation history/plans), comparable rent data (both a generic area-wide comp and, when available, this exact building's own rental history), and pre-computed DCF investment scenarios (unlevered and leveraged yield/return metrics across multiple financing scenarios). Those numbers are already correct, computed deterministically - your job is not to recompute or second-guess the arithmetic, only to interpret it and fill in the inputs that required judgment.

Three things you need to estimate, since they're not in any structured field:
1. Achievable monthly rent, plus rentSourceUsed and rentReasoning explaining the pick - priority order, most to least trustworthy:
   a. An explicit rent figure disclosed in the listing text itself (e.g. sold with a sitting tenant at a stated contract rent) - this is a known fact about this exact unit, not an estimate, so it outranks every comp below. rentSourceUsed = disclosed_contract_rent.
   b. This exact building's own rental history, when present - especially the same-size unit being repeatedly re-rented. rentSourceUsed = building_history.
   c. The condition-matched rent comp for this apartment's own room count - the generic area comp blends GOOD/SATISFACTORY/TOLERABLE condition units together, which can be a bad match for a specific apartment's actual condition (given above as "Overall condition"). Use the bucket matching that condition when it has a real sample size. rentSourceUsed = condition_matched_comp.
   d. If that condition/room-count bucket is too thin, you may be given the same condition breakdown for an adjacent room count instead - use that only to derive a relative adjustment (e.g. "GOOD-condition units rent for about 12% more than TOLERABLE ones here"), and apply that ratio to whatever baseline you'd otherwise use (building history or the generic comp) - never report that other room count's absolute rent figure as this apartment's own. rentSourceUsed = adjacent_room_count_condition_adjustment.
   e. The generic area-wide comp (all conditions blended) - a city or district-wide comp can be skewed by a large, varied market (e.g. a big city's average mixes cheap suburbs with its expensive center). Use this only when none of the above give a clear basis. rentSourceUsed = generic_area_comp.
   In rentReasoning, be concrete and cite the actual numbers/sources you weighed, including anything that almost changed your mind - this is read for diagnosing which inputs are actually useful, not for the investor-facing narrative.
2. The housing company's own loan's remaining amortization term - Finnish disclosure text sometimes states this directly (an explicit completion date for an ongoing renovation loan, or an explicit repayment plan); otherwise return null and a fallback will be used.
3. Any UPCOMING (not yet reflected in the current price) disclosed renovation's likely effect on this apartment's future debt share, achievable rent, and market value - only when the text gives a real basis (e.g. an explicit per-apartment debt-share euro figure, or a dated, scoped renovation plan). If the text is vague or there's no upcoming renovation mentioned, return zeros rather than guessing.

Be conservative: a wrong estimate here feeds directly into real investment decisions, so when the data doesn't give a clear basis, default to the safer/more generic figure (or null/0 for the renovation estimates) rather than inventing a plausible-sounding number."""


def format_rental_history(active: list, past: list) -> str:
    """Active records have no date field at all (still live, "now" is
    implicit); past ones carry unpublishingTime (ms epoch) - previously
    dropped entirely, which left the LLM unable to tell a 2026 rent from a
    2017 one and treating the whole history as undated/unreliable."""
    if not active and not past:
        return "none available for this building"
    lines = []
    for r in active:
        lines.append(f"- ACTIVE (currently listed): {r.get('area')}m², {r.get('searchPrice')} EUR/mo, {r.get('roomStructure')}")
    sorted_past = sorted(past, key=lambda r: r.get("unpublishingTime") or 0, reverse=True)
    for r in sorted_past[:15]:
        ts = r.get("unpublishingTime")
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m") if ts else "date unknown"
        lines.append(f"- past (delisted {when}): {r.get('area')}m², {r.get('searchPrice')} EUR/mo, {r.get('roomStructure')}")
    return "\n".join(lines)


def _format_condition_estimates(estimates: dict) -> str:
    lines = []
    for condition, est in estimates.items():
        if est is None:
            lines.append(f"- {condition}: no comps found")
        else:
            lines.append(
                f"- {condition}: {est.median_rent:.0f} EUR/month "
                f"(median {est.median_rent_per_sqm:.2f} EUR/sqm, n={est.sample_size}"
                f"{', widened to regional cluster' if est.widened_to_region else ''})"
            )
    return "\n".join(lines)


def format_condition_breakdown(breakdown: dict | None) -> str:
    if not breakdown:
        return "not available"
    parts = [
        f"Rent by condition, this apartment's own room count ({breakdown['roomCount']}):",
        _format_condition_estimates(breakdown["estimates"]),
    ]
    if breakdown.get("fallbackRoomCount"):
        parts += [
            "",
            f"That room count's condition buckets were too thin, so here's the same breakdown "
            f"for an adjacent room count ({breakdown['fallbackRoomCount']}) instead - use this only to "
            f"derive a relative condition adjustment, never as a direct rent figure for this apartment:",
            _format_condition_estimates(breakdown["fallbackEstimates"]),
        ]
    return "\n".join(parts)


def build_user_message(
    listing_details: dict, rent_estimate, scenarios: dict,
    building_rental_history: tuple[list, list] = ([], []),
    condition_rent_breakdown: dict | None = None,
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
        format_condition_breakdown(condition_rent_breakdown),
        "",
        "Baseline DCF scenarios (using the generic comp above, fallback housing-company loan term, no renovation adjustment):",
        json.dumps(scenarios, indent=2),
    ]
    return "\n".join(parts)


def analyze_listing(
    listing_details: dict, rent_estimate, scenarios: dict,
    building_rental_history: tuple[list, list] = ([], []),
    condition_rent_breakdown: dict | None = None,
) -> dict:
    client = anthropic.Anthropic()
    user_message = build_user_message(
        listing_details, rent_estimate, scenarios, building_rental_history, condition_rent_breakdown
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    )
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)
