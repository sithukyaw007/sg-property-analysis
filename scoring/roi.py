"""ROI calculation for property investments."""

import json
import os
from typing import Optional

from scoring.costs import CostCalculator

# Sensitivity defaults
try:
    from config import (
        ROI_SENSITIVITY_RENT_DELTA_PCT,
        ROI_SENSITIVITY_APPRECIATION_DELTA_PCT,
        ROI_RENT_GROWTH_PER_YEAR,
    )
except ImportError:
    ROI_SENSITIVITY_RENT_DELTA_PCT = 0.10
    ROI_SENSITIVITY_APPRECIATION_DELTA_PCT = 0.015
    ROI_RENT_GROWTH_PER_YEAR = 0.02
from scoring.models import ROIResult
from scoring.rental_estimator import RentalEstimator


# Load cost parameters
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_COST_PARAMS_FILE = os.path.join(_DATA_DIR, "cost_parameters.json")


def _load_appreciation_rate() -> float:
    """Load annual appreciation estimate from config."""
    if os.path.exists(_COST_PARAMS_FILE):
        with open(_COST_PARAMS_FILE) as f:
            params = json.load(f)
            return params.get("annual_appreciation_estimate", 0.02)
    return 0.02


class ROICalculator:
    """
    Calculate ROI for property investments.

    Defaults to the active buyer profile (buyer_profile.py) for ABSD; without
    one, Singapore Citizen, first property (0% ABSD). Hold periods of 5-7 years.
    """

    def __init__(
        self,
        buyer_type: Optional[str] = None,
        property_count: Optional[int] = None,
        annual_appreciation: Optional[float] = None,
        marginal_income_tax_rate: Optional[float] = None,
    ):
        """
        Initialize ROI calculator.

        Args:
            buyer_type: "SC" (Singapore Citizen), "PR", or "Foreigner".
                None = the active buyer profile's (buyer_profile.py), else "SC".
            property_count: 0 for first property, 1 for second, etc.
                None = the active buyer profile's, else 0.
            annual_appreciation: Override default appreciation rate
            marginal_income_tax_rate: Override the marginal income tax rate
                applied to net letting profit (default from
                cost_parameters.json, 0.15)
        """
        # ABSD depends on WHO buys. Defaulting to the profile (not a hardcoded
        # SC) is what makes a PR buyer's 5% first-property ABSD show up in
        # every ROI path without touching call sites. Never affects MMR.
        if buyer_type is None or property_count is None:
            import buyer_profile
            p_type, p_count = buyer_profile.buyer_defaults()
            buyer_type = p_type if buyer_type is None else buyer_type
            property_count = p_count if property_count is None else property_count
        self.buyer_type = buyer_type
        self.property_count = property_count
        self.annual_appreciation = annual_appreciation if annual_appreciation is not None else _load_appreciation_rate()
        self.cost_calc = CostCalculator()
        self.rental_estimator = RentalEstimator()
        # Audit fix (Jun 2026): rental income is taxable — net letting profit
        # (rent minus deductible holding costs and mortgage interest) is taxed
        # at the investor's marginal rate. Untaxed rent overstated after-tax
        # carry 10-24%.
        self.marginal_income_tax_rate = (
            marginal_income_tax_rate
            if marginal_income_tax_rate is not None
            else self.cost_calc.params.get("marginal_income_tax_rate", 0.15)
        )
        self.default_mortgage_rate_pct = self.cost_calc.params.get("mortgage_rate_pct", 3.5)

    def calculate(
        self,
        listing: dict,
        hold_years: int = 5,
        monthly_rent: Optional[float] = None,
        exit_price: Optional[int] = None,
        appreciation_rate: Optional[float] = None,
        agent_rental_months_per_year: Optional[float] = None,
        vacancy_months_per_year: Optional[float] = None,
        ltv: float = 0.0,
        mortgage_rate_pct: Optional[float] = None,
        mortgage_term_years: int = 25,
    ) -> ROIResult:
        """
        Calculate ROI for a property investment.

        Args:
            listing: Listing dictionary with price, sqft, etc.
            hold_years: Investment holding period
            monthly_rent: Override rental estimate
            exit_price: Override exit price estimate
            appreciation_rate: Per-listing appreciation rate (overrides default)
            ltv: Loan-to-value ratio (0 = all-cash, the default — preserves
                the unfinanced outputs exactly). When > 0, returns become
                cash-on-cash: invested cash is downpayment + entry costs,
                mortgage interest is deducted (and is tax-deductible against
                rent), and the remaining loan principal nets off at exit.
            mortgage_rate_pct: Annual mortgage interest rate in percent
                (default from cost_parameters.json, ~3.5). Ignored when ltv=0.
            mortgage_term_years: Amortization term (default 25yr).

        Returns:
            ROIResult with complete ROI breakdown
        """
        purchase_price = listing.get("price", 0)
        sqft = listing.get("sqft", 0)
        beds = listing.get("beds")

        if not purchase_price or not sqft:
            return self._empty_result(hold_years, purchase_price)

        # Estimate rental if not provided
        if monthly_rent is None:
            rental_data = self.rental_estimator.estimate(listing)
            monthly_rent = rental_data["monthly_rent"]

        # Use per-listing appreciation or fall back to default
        rate = appreciation_rate if appreciation_rate is not None else self.annual_appreciation

        # Estimate exit price with appreciation decay
        # v2.2: Appreciation decays slightly each year (mean reversion).
        # This models the reality that high appreciation rates don't sustain
        # indefinitely — they tend to normalize over longer hold periods.
        # Decay factor: 0.97x per year (3% annual decay of the rate itself).
        # Example: 6%/yr → 5.82% yr2 → 5.65% yr3 → 5.48% yr4 → ...
        if exit_price is None:
            APPRECIATION_DECAY_PER_YEAR = 0.97
            compounded_price = float(purchase_price)
            for year in range(hold_years):
                year_rate = rate * (APPRECIATION_DECAY_PER_YEAR ** year)
                compounded_price *= (1 + year_rate)
            exit_price = int(compounded_price)

        # Calculate costs. When vacancy_months_per_year is not given explicitly,
        # the breakdown resolves it from the (beds, price) tier — large/luxury
        # formats let more slowly (see CostCalculator.resolve_vacancy_months).
        cost_breakdown = self.cost_calc.create_cost_breakdown(
            purchase_price=purchase_price,
            sqft=sqft,
            monthly_rent=monthly_rent,
            hold_years=hold_years,
            exit_price=exit_price,
            buyer_type=self.buyer_type,
            property_count=self.property_count,
            agent_rental_months_per_year=agent_rental_months_per_year,
            vacancy_months_per_year=vacancy_months_per_year,
            beds=beds,
        )
        # The vacancy assumption actually used (resolved tier or explicit override),
        # surfaced on the result so callers can show / vary it.
        vacancy_months_used = (
            vacancy_months_per_year
            if vacancy_months_per_year is not None
            else self.cost_calc.resolve_vacancy_months(beds, purchase_price)
        )

        # Entry (transaction) costs — sunk at purchase. Audit #11: these used
        # to inflate only the denominator while never being deducted from the
        # return (exit costs were — the asymmetry was the bug), overstating
        # 5yr ROI ~3.7pp (SC first property) to ~19pp (SC 2nd, 20% ABSD).
        entry_costs = cost_breakdown.bsd + cost_breakdown.absd + cost_breakdown.legal_fee_buy

        # Optional financing (audit structural item). ltv=0 (default) keeps
        # the all-cash path identical: zero loan, zero interest, full price
        # as invested cash.
        ltv = max(0.0, min(0.9, ltv or 0.0))
        loan_amount = int(purchase_price * ltv)
        downpayment = purchase_price - loan_amount
        rate_pct = (
            mortgage_rate_pct if mortgage_rate_pct is not None
            else self.default_mortgage_rate_pct
        )
        interest_by_year, principal_by_year, remaining_principal = self._amortize(
            loan_amount, rate_pct, mortgage_term_years, hold_years
        )
        total_interest = sum(interest_by_year)

        # Annual rental income (gross, year 1)
        annual_rent_gross = monthly_rent * 12

        # Annual holding costs (year 1)
        annual_holding_costs = cost_breakdown.annual_holding

        # Net annual rental income (year 1, before income tax — this is the
        # market-comparable net yield stat)
        annual_rent_net = annual_rent_gross - annual_holding_costs

        # Rental stream over the holding period, year by year.
        # v3.5b: rent compounds at ROI_RENT_GROWTH_PER_YEAR (was held flat,
        # which understated 5-7yr rental income by ~5-7%).
        # Audit fix (Jun 2026): the rent-linked holding costs (property tax,
        # agent fee, vacancy) now scale with the same rent path instead of
        # staying flat, and net letting profit (rent minus deductible holding
        # costs and mortgage interest) is taxed at the marginal income tax
        # rate — untaxed rent overstated after-tax carry 10-24%.
        fixed_costs = cost_breakdown.mcst + cost_breakdown.repairs_insurance
        total_rent_gross = 0.0
        total_holding_costs = 0.0
        total_income_tax = 0.0
        for y in range(hold_years):
            growth = (1 + ROI_RENT_GROWTH_PER_YEAR) ** y
            rent_y = annual_rent_gross * growth
            holding_y = (
                self.cost_calc.calculate_property_tax(rent_y)  # AV tracks rent
                + cost_breakdown.agent_rental_fee * growth
                + cost_breakdown.vacancy_cost * growth
                + fixed_costs
            )
            interest_y = interest_by_year[y] if y < len(interest_by_year) else 0.0
            taxable_y = max(0.0, rent_y - holding_y - interest_y)
            total_rent_gross += rent_y
            total_holding_costs += holding_y
            total_income_tax += taxable_y * self.marginal_income_tax_rate

        # Net rental income over the hold (after holding costs and income tax)
        total_rental_net = total_rent_gross - total_holding_costs - total_income_tax

        # Exit costs
        exit_costs = self.cost_calc.calculate_exit_costs(exit_price, hold_years)
        total_exit_costs = sum(exit_costs.values())

        # Capital gain
        capital_gain = exit_price - purchase_price

        # Total return = profit over all cash put in. Mortgage principal
        # payments are equity transfers (returned via the exit netting), so
        # the financing cost reduces to total interest paid; at exit the
        # remaining principal nets off against the sale proceeds (already
        # captured: capital_gain is on the full price while only the
        # downpayment sits in the denominator).
        total_return = (
            capital_gain + total_rental_net - total_exit_costs
            - entry_costs - total_interest
        )

        # Total investment = cash actually deployed at purchase
        # (all-cash: full price + entry costs == cost_breakdown.total_upfront;
        # financed: downpayment + entry costs -> cash-on-cash ROI).
        total_investment = downpayment + entry_costs

        # ROI calculations
        roi_percent = (total_return / total_investment) * 100 if total_investment > 0 else 0

        # Annualized ROI (geometric mean). With entry costs and interest now
        # inside total_return, final_value is true terminal wealth (exit
        # proceeds net of loan balance + accumulated after-tax rents) — stamp
        # duty is no longer treated as recoverable principal.
        if total_investment > 0 and total_return > -total_investment:
            final_value = total_investment + total_return
            annualized_roi = ((final_value / total_investment) ** (1 / hold_years) - 1) * 100
        else:
            annualized_roi = 0

        # Gross and net yields
        gross_yield = (annual_rent_gross / purchase_price) * 100 if purchase_price > 0 else 0
        net_yield = (annual_rent_net / purchase_price) * 100 if purchase_price > 0 else 0

        result = ROIResult(
            hold_years=hold_years,
            purchase_price=purchase_price,
            estimated_exit_price=exit_price,
            total_rental_income=total_rental_net,
            total_holding_costs=total_holding_costs,
            total_upfront_costs=total_investment,
            total_exit_costs=total_exit_costs,
            gross_rental_yield=round(gross_yield, 2),
            net_rental_yield=round(net_yield, 2),
            capital_gain=capital_gain,
            total_return=round(total_return, 2),
            roi_percent=round(roi_percent, 2),
            annualized_roi=round(annualized_roi, 2),
            monthly_rent_estimate=monthly_rent,
            annual_rent_gross=annual_rent_gross,
            annual_rent_net=annual_rent_net,
            entry_costs=entry_costs,
            total_income_tax=round(total_income_tax, 2),
            marginal_income_tax_rate=self.marginal_income_tax_rate,
            ltv=ltv,
            mortgage_rate_pct=rate_pct if loan_amount > 0 else 0,
            loan_amount=loan_amount,
            downpayment=downpayment,
            total_mortgage_interest=round(total_interest, 2),
            remaining_principal_at_exit=round(remaining_principal, 2),
        )
        # Surface the vacancy assumption actually used. ROIResult is a plain
        # (non-slots) dataclass defined elsewhere; attaching as a dynamic
        # attribute keeps it visible to callers without touching its schema and
        # without affecting dataclass `==` (which compares declared fields only,
        # so ltv=0 stays exactly equal to the all-cash default).
        result.vacancy_months_per_year = round(vacancy_months_used, 4)
        return result

    @staticmethod
    def _amortize(
        loan_amount: int,
        rate_pct: float,
        term_years: int,
        hold_years: int,
    ) -> tuple[list[float], list[float], float]:
        """Standard monthly amortization schedule, aggregated per year.

        Returns (interest_by_year, principal_by_year, remaining_principal)
        for the first `hold_years` years of a `term_years` loan.
        """
        if loan_amount <= 0:
            return [0.0] * hold_years, [0.0] * hold_years, 0.0
        r = (rate_pct / 100.0) / 12.0
        n = max(1, term_years * 12)
        payment = loan_amount / n if r <= 0 else loan_amount * r / (1 - (1 + r) ** (-n))
        balance = float(loan_amount)
        interest_by_year: list[float] = []
        principal_by_year: list[float] = []
        for _ in range(hold_years):
            i_y = p_y = 0.0
            for _m in range(12):
                if balance <= 0:
                    break
                interest = balance * r
                principal = min(payment - interest, balance)
                balance -= principal
                i_y += interest
                p_y += principal
            interest_by_year.append(i_y)
            principal_by_year.append(p_y)
        return interest_by_year, principal_by_year, balance

    def calculate_multiple_periods(
        self,
        listing: dict,
        periods: list[int] = [5, 6, 7],
        monthly_rent: Optional[float] = None,
        appreciation_rate: Optional[float] = None,
    ) -> dict[int, ROIResult]:
        """
        Calculate ROI for multiple holding periods.

        Args:
            listing: Listing dictionary
            periods: List of holding periods in years
            monthly_rent: Override rental estimate
            appreciation_rate: Per-listing appreciation rate

        Returns:
            Dict mapping years -> ROIResult
        """
        results = {}
        for years in periods:
            results[years] = self.calculate(
                listing, years, monthly_rent,
                appreciation_rate=appreciation_rate,
            )
        return results

    def calculate_sensitivity(
        self,
        listing: dict,
        periods: list[int] = [5, 7],
        monthly_rent: Optional[float] = None,
        appreciation_rate: Optional[float] = None,
        rent_delta_pct: Optional[float] = None,
        appreciation_delta_pct: Optional[float] = None,
        ltv: float = 0.0,
        mortgage_rate_pct: Optional[float] = None,
        mortgage_rate_delta_pp: float = 1.0,
        vacancy_delta_months: float = 0.5,
    ) -> dict[str, dict[int, ROIResult]]:
        """
        Calculate ROI sensitivity scenarios.

        - downside/base/upside adjust rent and appreciation rate.
        - vac_low/vac_high vary the vacancy assumption by
          -/+ vacancy_delta_months around the unit's tiered base prior
          (floored at 0). vac_low = optimistic occupancy, vac_high = pessimistic.
        - When financed (ltv > 0) a rate axis is added: rate_up/rate_down move
          the mortgage rate by +/- mortgage_rate_delta_pp.

        The return dict also carries an `assumptions` key (deltas + the resolved
        base/low/high vacancy months) so callers can surface what was varied.
        The per-scenario values remain `{year: ROIResult}` dicts, so existing
        explicit-key access (e.g. sensitivity["downside"][5]) is unaffected.
        """
        if monthly_rent is None:
            rental_data = self.rental_estimator.estimate(listing)
            monthly_rent = rental_data["monthly_rent"]

        base_rate = appreciation_rate if appreciation_rate is not None else self.annual_appreciation
        rent_delta = ROI_SENSITIVITY_RENT_DELTA_PCT if rent_delta_pct is None else rent_delta_pct
        appr_delta = ROI_SENSITIVITY_APPRECIATION_DELTA_PCT if appreciation_delta_pct is None else appreciation_delta_pct
        base_mortgage_rate = (
            mortgage_rate_pct if mortgage_rate_pct is not None
            else self.default_mortgage_rate_pct
        )
        # Unit's tiered vacancy prior is the centre of the vacancy axis.
        base_vacancy = self.cost_calc.resolve_vacancy_months(
            listing.get("beds"), listing.get("price")
        )
        vac_low = max(0.0, base_vacancy - vacancy_delta_months)
        vac_high = base_vacancy + vacancy_delta_months

        scenarios = {
            "downside": {"rent_mult": 1 - rent_delta, "rate_delta": -appr_delta},
            "base": {"rent_mult": 1.0, "rate_delta": 0.0},
            "upside": {"rent_mult": 1 + rent_delta, "rate_delta": appr_delta},
            # Vacancy axis (optimistic / pessimistic occupancy)
            "vac_low": {"rent_mult": 1.0, "rate_delta": 0.0, "vacancy": vac_low},
            "vac_high": {"rent_mult": 1.0, "rate_delta": 0.0, "vacancy": vac_high},
        }
        if ltv > 0:
            scenarios["rate_up"] = {
                "rent_mult": 1.0, "rate_delta": 0.0,
                "mortgage_delta": mortgage_rate_delta_pp,
            }
            scenarios["rate_down"] = {
                "rent_mult": 1.0, "rate_delta": 0.0,
                "mortgage_delta": -mortgage_rate_delta_pp,
            }

        results: dict[str, dict[int, ROIResult]] = {}
        for name, cfg in scenarios.items():
            adj_rent = monthly_rent * cfg["rent_mult"]
            adj_rate = base_rate + cfg["rate_delta"]
            # Keep within reasonable bounds (-5% to +15%/yr)
            adj_rate = max(-0.05, min(0.15, adj_rate))
            adj_mortgage = max(0.0, base_mortgage_rate + cfg.get("mortgage_delta", 0.0))
            adj_vacancy = cfg.get("vacancy")  # None -> calculate() resolves the tier
            results[name] = {}
            for years in periods:
                results[name][years] = self.calculate(
                    listing,
                    hold_years=years,
                    monthly_rent=adj_rent,
                    appreciation_rate=adj_rate,
                    vacancy_months_per_year=adj_vacancy,
                    ltv=ltv,
                    mortgage_rate_pct=adj_mortgage,
                )
        results["assumptions"] = {
            "rent_delta_pct": rent_delta,
            "appreciation_delta_pct": appr_delta,
            "vacancy_delta_months": vacancy_delta_months,
            "vacancy_base_months": round(base_vacancy, 4),
            "vacancy_low_months": round(vac_low, 4),
            "vacancy_high_months": round(vac_high, 4),
        }
        return results

    def _empty_result(self, hold_years: int, purchase_price: int) -> ROIResult:
        """Return empty ROI result for invalid inputs."""
        return ROIResult(
            hold_years=hold_years,
            purchase_price=purchase_price,
            estimated_exit_price=purchase_price,
            total_rental_income=0,
            total_holding_costs=0,
            total_upfront_costs=purchase_price,
            total_exit_costs=0,
            gross_rental_yield=0,
            net_rental_yield=0,
            capital_gain=0,
            total_return=0,
            roi_percent=0,
            annualized_roi=0,
        )


def quick_roi_estimate(
    price: int,
    sqft: float,
    hold_years: int = 5,
    annual_appreciation: float = 0.02,
) -> dict:
    """
    Quick ROI estimate without full listing data.

    Args:
        price: Purchase price
        sqft: Floor area in square feet
        hold_years: Holding period
        annual_appreciation: Annual price appreciation rate

    Returns:
        Dict with estimated ROI metrics
    """
    # Rough estimates
    rent_psf = 3.5  # Market average
    monthly_rent = sqft * rent_psf
    annual_rent = monthly_rent * 12

    # Simplified cost estimates
    bsd = CostCalculator().calculate_bsd(price)
    # v2.2: Apply appreciation decay (same as full calculator)
    compounded = float(price)
    for yr in range(hold_years):
        compounded *= (1 + annual_appreciation * (0.97 ** yr))
    exit_price = int(compounded)

    # Annual costs (simplified)
    annual_costs = sqft * 0.35 * 12  # MCST
    # Vacancy (0.75 flat mid-tier prior) + agent (0.5). This quick path has no
    # beds, so it uses the flat fallback the full calculator falls back to when
    # (beds, price) can't tier the vacancy (resolve_vacancy_months).
    annual_costs += monthly_rent * 1.25
    annual_costs += 1800  # Repairs/insurance
    annual_costs += annual_rent * 0.15  # Property tax estimate

    net_annual = annual_rent - annual_costs
    # Marginal income tax on net letting profit (matches full calculator)
    tax_rate = CostCalculator().params.get("marginal_income_tax_rate", 0.15)
    net_annual -= max(0.0, net_annual) * tax_rate
    total_net_rental = net_annual * hold_years

    capital_gain = exit_price - price
    exit_costs = int(exit_price * 0.02) + 3000  # Agent + legal
    entry_costs = bsd + 3500  # BSD + legal (sunk at purchase — audit #11)

    total_return = capital_gain + total_net_rental - exit_costs - entry_costs
    total_investment = price + entry_costs

    roi = (total_return / total_investment) * 100
    if roi > -100:
        annualized = ((1 + roi / 100) ** (1 / hold_years) - 1) * 100
    else:
        annualized = 0

    return {
        "purchase_price": price,
        "exit_price": exit_price,
        "monthly_rent": round(monthly_rent),
        "gross_yield_pct": round((annual_rent / price) * 100, 2),
        "capital_gain": capital_gain,
        "total_return": round(total_return),
        "roi_pct": round(roi, 2),
        "annualized_roi_pct": round(annualized, 2),
    }
