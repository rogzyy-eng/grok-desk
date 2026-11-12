"""fees.py — FOMO fee model (STEP 11). Fees apply on EACH side, minus the referral discount. Gas = 0."""
MIN_TRADE = 5.0
TIER1_MAX = 47.50
TIER2_MAX = 190.0
FLAT_FEE = 0.95
GAS = 0.0


class TicketTooSmall(ValueError):
    pass


def side_fee(amount_usd: float, ref_discount: float = 0.10) -> float:
    a = float(amount_usd)
    if a < MIN_TRADE:
        raise TicketTooSmall(f"${a:.2f} is below the ${MIN_TRADE:.2f} minimum")
    if a <= TIER1_MAX:
        fee = a * 0.02
    elif a <= TIER2_MAX:
        fee = FLAT_FEE
    else:
        fee = a * 0.005
    return round(fee * (1.0 - ref_discount) + GAS, 6)


def round_trip_fee(entry_usd: float, exit_usd: float, ref_discount: float = 0.10) -> float:
    return round(side_fee(entry_usd, ref_discount) + side_fee(max(exit_usd, MIN_TRADE), ref_discount), 6)
