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
  - Bank loan: tops up whatever the housing-company debt doesn't already
    cover of the target LTV (applied to debt-free price, not selling price),
    floored at 0. Carries a capitalized arrangement fee that's repaid with
    the loan but accrues no interest of its own (tracked as a separate,
    non-interest-bearing balance alongside the loan's principal). Principal
    is NOT tax-deductible (only interest is, already netted into net yield
    before tax); immediate reserves (a separate, smaller one-off cost) are
    paid from equity instead, not financed.

The headline metrics are pulled from the template's own summary dashboard
(not a year-1 snapshot of the year-by-year grid, which is a different,
easily-confused thing): six of the eight are IRR over the full 50-year cash
flow stream, not simple ratios. Two different initial outlays are used -
"total cost" (debt-free price + transfer tax + arrangement fee + reserves)
for the unlevered metrics, "equity" (debt-free price x (1 - LTV) + transfer
tax + reserves - i.e. actual cash in, excluding the financed arrangement
fee) for the levered ones - confirmed algebraically equivalent to the
template's own Kokonaishinta/Sijoitettava pääoma cells. Only "Tuotto
velattomaan" is a plain ratio, using raw monthly rent/maintenance inputs
(not year-1 grown values) over total cost. Market value (used for
appreciation and total-return metrics) grows from total cost, not bare
debt-free price - also per the template.
"""
from dataclasses import dataclass
from pathlib import Path
import json

YEARS = 50


def npv(rate: float, cash_flows: list[float]) -> float:
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cash_flows))


def irr(cash_flows: list[float], guess: float = 0.1, tol: float = 1e-7, max_iter: int = 100) -> float | None:
    """Pure-Python IRR (Newton's method, falling back to bisection). Returns
    None if no root is found (e.g. the series never turns cash-positive)."""
    rate = guess
    for _ in range(max_iter):
        f = npv(rate, cash_flows)
        d = sum(-t * cf / (1 + rate) ** (t + 1) for t, cf in enumerate(cash_flows) if t > 0)
        if abs(d) < 1e-12:
            break
        new_rate = rate - f / d
        if not (-0.999 < new_rate < 100):
            break  # Newton diverged - fall through to bisection instead
        if abs(new_rate - rate) < tol:
            return new_rate
        rate = new_rate

    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo, cash_flows), npv(hi, cash_flows)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid, cash_flows)
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


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
    noi: float
    net_yield_before_tax: float
    net_yield_after_tax: float
    cash_flow_before_tax: float
    cash_flow_after_tax: float
    market_value_change: float
    total_return_cash_flow_adjusted: float


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


@dataclass
class DCFResult:
    rows: list[YearRow]
    total_cost: float  # unlevered base ("Kokonaishinta")
    equity: float       # levered base ("Sijoitettava pääoma")


def run_dcf(listing: ListingInputs, assumptions: dict, ltv: float) -> DCFResult:
    a = resolve_assumptions(assumptions, listing.city, listing.room_count)

    housing_company_debt0 = max(0.0, listing.debt_free_price - listing.selling_price)
    housing_company_term = (
        listing.housing_company_loan_term_years
        or a["housingCompanyLoanTermFallbackYears"]
    )
    # LTV applies to the whole debt-free price; the bank loan tops up
    # whatever the housing-company debt doesn't already cover of that target
    bank_loan0 = max(0.0, listing.debt_free_price * ltv - housing_company_debt0)
    # capitalized into the bank loan (repaid with it, no interest charged on
    # it) only when there's actually a bank loan - not paid from equity
    arrangement_fee = a["bankLoanArrangementFee"] if bank_loan0 > 0 else 0.0
    reserves = a["immediateReservesWithLoan"] if bank_loan0 > 0 else a["immediateReservesNoLoan"]

    housing_company_installment = (
        housing_company_debt0 / housing_company_term if housing_company_term > 0 else 0.0
    )
    bank_term = a["bankLoanTermYears"]
    bank_principal_installment = bank_loan0 / bank_term if bank_term > 0 else 0.0
    bank_fee_installment = arrangement_fee / bank_term if bank_term > 0 else 0.0

    transfer_tax = listing.debt_free_price * a["transferTaxRate"]
    total_cost = listing.debt_free_price + transfer_tax + arrangement_fee + reserves
    equity = listing.debt_free_price * (1 - ltv) + transfer_tax + reserves

    rows: list[YearRow] = []
    rent = 0.0
    maintenance = 0.0
    repair_reserve = 0.0
    debt_free_value = total_cost  # template grows market value from Kokonaishinta, not debt-free price
    cum_notional_tax = 0.0

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

        bank_principal_balance_start = max(0.0, bank_loan0 - (year - 1) * bank_principal_installment)
        bank_fee_balance_start = max(0.0, arrangement_fee - (year - 1) * bank_fee_installment)
        bank_interest = bank_principal_balance_start * a["bankLoanRate"]  # fee balance accrues no interest
        bank_amortization = (
            min(bank_principal_balance_start, bank_principal_installment)
            + min(bank_fee_balance_start, bank_fee_installment)
        )
        bank_service_fee = (
            a["bankLoanServiceFeeMonthly"] * 12
            if (bank_principal_balance_start + bank_fee_balance_start) > 0 else 0.0
        )

        net_yield_before_tax = noi - hc_interest - bank_interest - bank_service_fee

        notional_tax = (net_yield_before_tax - hc_amortization) * a["taxRate"]
        tax = max(0.0, notional_tax + min(0.0, cum_notional_tax))
        cum_notional_tax += notional_tax

        net_yield_after_tax = net_yield_before_tax - tax

        total_amortization = hc_amortization + bank_amortization
        cash_flow_before_tax = net_yield_before_tax - total_amortization
        cash_flow_after_tax = cash_flow_before_tax - tax

        market_value_change = debt_free_value * a["marketPriceChange"]
        debt_free_value += market_value_change

        total_return = (
            cash_flow_after_tax
            + (total_amortization + market_value_change) * a["collateralValueReturnShare"]
        )
        total_return_cash_flow_adjusted = total_return + min(0.0, cash_flow_after_tax)

        rows.append(YearRow(
            noi=noi,
            net_yield_before_tax=net_yield_before_tax,
            net_yield_after_tax=net_yield_after_tax,
            cash_flow_before_tax=cash_flow_before_tax,
            cash_flow_after_tax=cash_flow_after_tax,
            market_value_change=market_value_change,
            total_return_cash_flow_adjusted=total_return_cash_flow_adjusted,
        ))

    return DCFResult(rows=rows, total_cost=total_cost, equity=equity)


def summarize(listing: ListingInputs, assumptions: dict, result: DCFResult) -> dict:
    """The dashboard's headline metrics. Six of eight are IRR over the full
    50-year stream (unlevered ones against -total_cost, levered ones against
    -equity); only "unleveredYield" is a plain ratio."""
    a = resolve_assumptions(assumptions, listing.city, listing.room_count)
    rows = result.rows

    def irr_series(values: list[float], base: float) -> float | None:
        return irr([-base] + values)

    noi_series = [r.noi for r in rows]
    noi_plus_appreciation = [r.noi + r.market_value_change for r in rows]
    net_yield_before_tax_series = [r.net_yield_before_tax for r in rows]
    net_yield_after_tax_series = [r.net_yield_after_tax for r in rows]
    cash_flow_before_tax_series = [r.cash_flow_before_tax for r in rows]
    cash_flow_after_tax_series = [r.cash_flow_after_tax for r in rows]
    total_return_adj_series = [r.total_return_cash_flow_adjusted for r in rows]

    break_even_year = next(
        (i + 1 for i, r in enumerate(rows) if r.cash_flow_after_tax >= 0), None
    )

    return {
        # the one plain ratio - raw monthly inputs over total cost, not NOI
        "unleveredYield": (
            (listing.monthly_rent - listing.monthly_maintenance_charge) * 12 / result.total_cost
            if result.total_cost else 0.0
        ),
        "grossYield": irr_series(noi_series, result.total_cost),
        "totalYield": irr_series(noi_plus_appreciation, result.total_cost),
        "netYieldBeforeTax": irr_series(net_yield_before_tax_series, result.equity),
        "netYieldAfterTax": irr_series(net_yield_after_tax_series, result.equity),
        "cashFlowBeforeTax": irr_series(cash_flow_before_tax_series, result.equity),
        "cashFlowAfterTax": irr_series(cash_flow_after_tax_series, result.equity),
        "cashFlowAfterTaxYear1": rows[0].cash_flow_after_tax,
        "cashFlowBreakEvenYear": break_even_year,
        "totalReturnCashFlowAdjusted": irr_series(total_return_adj_series, result.equity),
    }


def run_scenarios(listing: ListingInputs, assumptions: dict) -> dict[str, dict]:
    """Run every configured LTV scenario (e.g. fully leveraged vs. balanced
    vs. conservative) so the equity-based metrics can be compared side by
    side - the unlevered ones (unleveredYield/grossYield/totalYield) don't
    vary by LTV and will come out ~identical across scenarios; that's
    expected, not a bug."""
    scenarios = assumptions.get("ltvScenarios", {"default": 0.70})
    return {
        name: summarize(listing, assumptions, run_dcf(listing, assumptions, ltv))
        for name, ltv in scenarios.items()
    }
