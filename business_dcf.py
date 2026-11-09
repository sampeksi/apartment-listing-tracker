#!/usr/bin/env python3
"""DCF investment calculus, ported from a reference spreadsheet template.

The template was built around one specific deal's bespoke financing (four
separate housing-company loan tranches with different amortization types,
plus a bank loan and a bullet loan). That doesn't generalize to arbitrary
scraped listings, which only ever give us one aggregate housing-company debt
share - so this is a deliberate simplification to two loans:

  - Housing-company debt: the listing's own debt share, at a static rate,
    linear ("tasalyhennys") amortization, over a term estimated per-building
    from its renovation history (not built yet - falls back to a config
    default), with the real Finnish tax treatment that its *principal*
    repayment (not just interest) is deductible against rental income.
  - Bank loan: your own financing of the rest of the purchase price, at
    static rate/term/LTV, linear amortization, principal NOT deductible
    (only interest is - already netted into net yield before tax).

All currency-amount formulas and the tax-loss-carryforward mechanic are
ported faithfully from the template; see the conversation history for the
exact source formulas this was checked against. Two yield metrics are
deliberately distinct, matching standard Finnish property-investment usage:
"Tuotto velattomaan" divides by debt-free price (includes the housing
company's own debt in the base), "Vuokratuotto" divides by selling price
(what you actually pay, excluding that debt share).
"""
from dataclasses import dataclass, field
from pathlib import Path
import json

YEARS = 50


@dataclass
class ListingInputs:
    selling_price: float
    debt_free_price: float
    monthly_rent: float
    monthly_maintenance_charge: float
    city: str
    room_count: str  # e.g. "ONE_ROOM", "TWO_ROOMS" - matches Etuovi's enum
    housing_company_loan_term_years: float | None = None  # None -> use fallback


@dataclass
class YearRow:
    rent: float
    maintenance_charge: float
    repair_reserve: float
    noi: float
    housing_company_interest: float
    bank_interest: float
    bank_service_fee: float
    net_yield_before_tax: float
    housing_company_amortization: float
    bank_amortization: float
    total_amortization: float
    tax: float
    net_yield_after_tax: float
    cash_flow_before_tax: float
    cash_flow_after_tax: float
    cash_flow_after_tax_cum: float
    market_value_change: float
    debt_free_value: float
    total_return: float
    total_return_cash_flow_adjusted: float
    invested_capital_cum: float  # cost-basis, for ROI
    equity_mark_to_market: float  # for ROE
    roi: float
    roe: float
    unlevered_yield: float
    gross_yield: float


def load_assumptions(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_assumptions(assumptions: dict, city: str, room_count: str) -> dict:
    """Region overrides win for the fields they specify; everything else
    (and any city/room_count combo with no override) falls back to global."""
    resolved = dict(assumptions["global"])
    for override in assumptions.get("regionOverrides", []):
        if override["city"] == city and override["roomCount"] == room_count:
            resolved.update({
                k: v for k, v in override.items()
                if k not in ("city", "areaWeighting", "roomCount")
            })
            break
    return resolved


def run_dcf(listing: ListingInputs, assumptions: dict) -> list[YearRow]:
    a = resolve_assumptions(assumptions, listing.city, listing.room_count)

    housing_company_debt0 = max(0.0, listing.debt_free_price - listing.selling_price)
    housing_company_term = (
        listing.housing_company_loan_term_years
        or a["housingCompanyLoanTermFallbackYears"]
    )
    bank_loan_base = listing.selling_price  # bankLoanToValueBase == "sellingPrice"
    bank_loan0 = bank_loan_base * a["bankLoanToValue"]

    housing_company_installment = (
        housing_company_debt0 / housing_company_term if housing_company_term > 0 else 0.0
    )
    bank_installment = bank_loan0 / a["bankLoanTermYears"] if a["bankLoanTermYears"] > 0 else 0.0

    rows: list[YearRow] = []
    rent = 0.0
    maintenance = 0.0
    repair_reserve = 0.0
    debt_free_value = listing.debt_free_price
    cum_notional_tax = 0.0
    cash_flow_after_tax_cum = 0.0
    invested_capital = None  # set after year 1's equity requirement is known
    equity0 = None

    for year in range(1, YEARS + 1):
        if year == 1:
            rent = listing.monthly_rent * a["occupancyRate"] * 12
            maintenance = listing.monthly_maintenance_charge * 12
            repair_reserve = listing.monthly_rent  # template default: ~1 month's rent/yr
        else:
            rent = rent * (1 + a["rentGrowth"])
            maintenance = maintenance * (1 + a["maintenanceChargeGrowth"])
            repair_reserve = repair_reserve * (1 + a["repairReserveGrowth"])

        noi = rent - maintenance - repair_reserve

        hc_balance_start = max(0.0, housing_company_debt0 - (year - 1) * housing_company_installment)
        hc_interest = hc_balance_start * a["housingCompanyLoanRate"]
        hc_amortization = min(hc_balance_start, housing_company_installment)

        bank_balance_start = max(0.0, bank_loan0 - (year - 1) * bank_installment)
        bank_interest = bank_balance_start * a["bankLoanRate"]
        bank_amortization = min(bank_balance_start, bank_installment)
        bank_service_fee = a["bankLoanServiceFeeMonthly"] * 12 if bank_balance_start > 0 else 0.0

        net_yield_before_tax = noi - hc_interest - bank_interest - bank_service_fee

        notional_tax = (net_yield_before_tax - hc_amortization) * a["taxRate"]
        tax = max(0.0, notional_tax + min(0.0, cum_notional_tax))
        cum_notional_tax += notional_tax

        net_yield_after_tax = net_yield_before_tax - tax

        total_amortization = hc_amortization + bank_amortization
        cash_flow_before_tax = net_yield_before_tax - total_amortization
        cash_flow_after_tax = cash_flow_before_tax - tax
        cash_flow_after_tax_cum += cash_flow_after_tax

        market_value_change = debt_free_value * a["marketPriceChange"]
        debt_free_value += market_value_change

        total_return = (
            cash_flow_after_tax
            + (total_amortization + market_value_change) * a["collateralValueReturnShare"]
        )
        total_return_cash_flow_adjusted = total_return + min(0.0, cash_flow_after_tax)

        if year == 1:
            equity0 = (
                listing.selling_price - bank_loan0
                + listing.debt_free_price * a["transferTaxRate"]
                + a["closingCostFixed"]
            )
            invested_capital = equity0
        else:
            invested_capital += abs(min(0.0, rows[-1].cash_flow_after_tax))

        remaining_debt = hc_balance_start - hc_amortization + bank_balance_start - bank_amortization
        equity_mark_to_market = debt_free_value - remaining_debt

        roi = total_return / invested_capital if invested_capital else 0.0
        roe = total_return / equity_mark_to_market if equity_mark_to_market else 0.0
        unlevered_yield = noi / debt_free_value
        gross_yield = noi / listing.selling_price if listing.selling_price else 0.0

        rows.append(YearRow(
            rent=rent, maintenance_charge=maintenance, repair_reserve=repair_reserve,
            noi=noi, housing_company_interest=hc_interest, bank_interest=bank_interest,
            bank_service_fee=bank_service_fee, net_yield_before_tax=net_yield_before_tax,
            housing_company_amortization=hc_amortization, bank_amortization=bank_amortization,
            total_amortization=total_amortization, tax=tax,
            net_yield_after_tax=net_yield_after_tax,
            cash_flow_before_tax=cash_flow_before_tax, cash_flow_after_tax=cash_flow_after_tax,
            cash_flow_after_tax_cum=cash_flow_after_tax_cum,
            market_value_change=market_value_change, debt_free_value=debt_free_value,
            total_return=total_return,
            total_return_cash_flow_adjusted=total_return_cash_flow_adjusted,
            invested_capital_cum=invested_capital, equity_mark_to_market=equity_mark_to_market,
            roi=roi, roe=roe, unlevered_yield=unlevered_yield, gross_yield=gross_yield,
        ))

    return rows


def summarize(listing: ListingInputs, rows: list[YearRow]) -> dict:
    """The 8 headline metrics, mostly as year-1 snapshots (what an investor
    sees "if I bought today"), plus the one genuinely new derived metric."""
    y1 = rows[0]
    break_even_year = next(
        (i + 1 for i, r in enumerate(rows) if r.cash_flow_after_tax >= 0), None
    )
    appreciation_yield = (
        y1.market_value_change / listing.selling_price if listing.selling_price else 0.0
    )
    return {
        "unleveredYield": y1.unlevered_yield,
        "grossYield": y1.gross_yield,
        "roe": y1.roe,
        "totalYield": y1.gross_yield + appreciation_yield,
        "netYieldBeforeTax": y1.net_yield_before_tax,
        "netYieldAfterTax": y1.net_yield_after_tax,
        "cashFlowBeforeTax": y1.cash_flow_before_tax,
        "cashFlowAfterTaxYear1": y1.cash_flow_after_tax,
        "cashFlowBreakEvenYear": break_even_year,
        "totalReturnCashFlowAdjusted": y1.total_return_cash_flow_adjusted,
    }
