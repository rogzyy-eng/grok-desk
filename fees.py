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


# ---- PAPER price impact (constant-product pool, x*y=k). The quote side of a pool is ~ liquidity/2. ----
def buy_impact(spend_usd: float, price: float, liq_usd):
    """-> (avg fill price, impact %). Spending dq into quote reserve Q fills at price × (1 + dq/Q)."""
    if not liq_usd or liq_usd <= 0 or not price:
        return price, 0.0
    f = float(spend_usd) / (float(liq_usd) / 2.0)
    return price * (1 + f), round(f * 100, 4)


def sell_impact(qty: float, price: float, liq_usd):
    """-> (gross USD out, impact %). Selling tokens worth v at spot into quote reserve Q returns v / (1 + v/Q)."""
    v = float(qty or 0) * float(price or 0)
    if liq_usd is None or v <= 0:
        return v, 0.0
    if liq_usd <= 0:
        return 0.0, 100.0          # pool gone: nothing comes back
    f = v / (float(liq_usd) / 2.0)
    return v / (1 + f), round(f / (1 + f) * 100, 4)
