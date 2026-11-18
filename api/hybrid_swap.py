"""
Roblosats rl31-beta21: in hybrid B the seller pays the buyer's swap to onchain.

Hybrid B: the buyer made an onchain order and a seller took it with Lightning, so the order
runs over Lightning and the buyer only gets onchain through the coordinator's swap. That swap
exists because of the seller's choice, so the seller pays it: when its escrow hold invoice is
made, the estimated swap cost (swap fee at the current rate + mining at the suggested rate for
the payout, with a margin) is added on top of the escrow. The buyer then receives onchain the
same as in a pure onchain order: the trade amount minus only its own coordinator-fee share.
When the order completes, whatever the swap did not use (all of it if the buyer took
Lightning) goes back to the seller as rewards.

A pure Lightning order whose buyer picks an onchain payout keeps the upstream rule (the buyer
pays its swap). Orders whose escrow was made before this (Kilobot #23) have no surcharge.
"""
import math
import logging

from decouple import config

logger = logging.getLogger("api.hybrid_swap")

PAYOUT_VBYTES = 280  # same as the upstream swap
SWAP_MARGIN = 1.10  # the swap rate can move between the escrow and the payout
MINING_MARGIN = 1.50  # and so can the mempool


def _fee_split():
    from api.logics import FEE, MAKER_FEE_SPLIT

    return FEE, MAKER_FEE_SPLIT


def buyer_payout_sats(order, last_satoshis=None):
    """What the buyer gets before any swap: the trade amount minus its coordinator-fee share."""
    FEE, MAKER_FEE_SPLIT = _fee_split()
    sats = order.last_satoshis if last_satoshis is None else last_satoshis
    buyer_is_maker = order.type == order.Types.BUY
    fraction = FEE * MAKER_FEE_SPLIT if buyer_is_maker else FEE * (1 - MAKER_FEE_SPLIT)
    return round(sats - sats * fraction)


def swap_possible(payout_sats):
    """The coordinator would offer the buyer an onchain payout of this size (hybrid B limits)."""
    if config("DISABLE_ONCHAIN", cast=bool, default=True):
        return False
    lo = min(
        config("MIN_SWAP_AMOUNT", cast=int, default=20_000),
        config("ESCROW_MIN_SATS", cast=int, default=config("MIN_ORDER_SIZE", cast=int, default=20000)),
    )
    hi = config("MAX_SWAP_AMOUNT", cast=int, default=500_000)
    return lo <= payout_sats <= hi


def current_rates():
    """(swap fee %, suggested mining sat/vB) as the buyer's payout would get them now."""
    from api.logics import Logics
    from api.utils import get_minning_fee
    from control.models import BalanceLog

    balance = BalanceLog.objects.order_by("-time").first()
    if balance is None:
        balance = BalanceLog.objects.create()
    swap_rate = float(Logics.compute_swap_fee_rate(balance))
    mining = float(get_minning_fee("suggested", 100_000))
    mining = min(1000.0, max(2.05, mining))
    return swap_rate, mining


def quote(swap_rate, mining_rate):
    """What the client needs to estimate the surcharge for any amount: sats * percent/100 + mining."""
    return {
        "swap_fee_percent": round(swap_rate * SWAP_MARGIN, 4),
        "mining_fee_sats": int(math.ceil(mining_rate * PAYOUT_VBYTES * MINING_MARGIN)),
        "swap_fee_rate": round(swap_rate, 2),
        "mining_fee_rate": round(mining_rate, 2),
    }


def surcharge_for(payout_sats, swap_rate, mining_rate):
    q = quote(swap_rate, mining_rate)
    return int(math.ceil(payout_sats * q["swap_fee_percent"] / 100 + q["mining_fee_sats"] - 1e-9))


def applies(order):
    """Hybrid B with the seller as the Lightning taker."""
    from api.escrow import flow as escrow_flow

    return bool(
        order.taker_lightning
        and escrow_flow.has_proxy(order)
        and order.type == order.Types.BUY  # the maker buys, the taker sells
    )


def take_quote(order):
    """For the take screen of an onchain BUY order: what taking it with Lightning would cost
    the seller for the swap (None if the swap would not be offered)."""
    if config("DISABLE_ONCHAIN", cast=bool, default=True):
        return None
    from django.core.cache import cache

    # every public page of an onchain BUY order asks: price it at most once a minute
    rates = cache.get("hybrid_swap_rates")
    if rates is None:
        try:
            rates = current_rates()
        except Exception as e:
            logger.warning(f"take quote for order {order.id}: {e}")
            return None
        cache.set("hybrid_swap_rates", rates, 60)
    swap_rate, mining = rates
    q = quote(swap_rate, mining)
    q["min_payout_sats"] = min(
        config("MIN_SWAP_AMOUNT", cast=int, default=20_000),
        config("ESCROW_MIN_SATS", cast=int, default=config("MIN_ORDER_SIZE", cast=int, default=20000)),
    )
    q["max_payout_sats"] = config("MAX_SWAP_AMOUNT", cast=int, default=500_000)
    return q


def fix_surcharge(order):
    """Called right before the seller's escrow hold invoice is made (once)."""
    if order.trade_escrow_id or order.swap_surcharge_sats or not applies(order):
        return order.swap_surcharge_sats
    payout = buyer_payout_sats(order)
    if not swap_possible(payout):
        order.log("Hybrid B: no swap possible for this amount, no swap surcharge for the seller")
        return 0
    try:
        swap_rate, mining = current_rates()
    except Exception as e:
        order.log(f"Hybrid B: could not price the swap ({e}); no surcharge", level="WARN")
        return 0
    order.swap_surcharge_sats = surcharge_for(payout, swap_rate, mining)
    order.save(update_fields=["swap_surcharge_sats"])
    order.log(
        f"Hybrid B: the seller pays the buyer's swap: {order.swap_surcharge_sats} Sats on top of the "
        f"escrow ({swap_rate:.2f}% x{SWAP_MARGIN} of {payout} + {mining} sat/vB x {PAYOUT_VBYTES} vB x{MINING_MARGIN})"
    )
    return order.swap_surcharge_sats


def swap_cost(tx):
    """Swap fee + mining of an onchain payout, in sats (rounded up)."""
    return int(
        math.ceil(float(tx.num_satoshis) * float(tx.swap_fee_rate) / 100 + float(tx.mining_fee_sats) - 1e-9)
    )


def covered_by_seller(order, tx=None):
    """How much of the buyer's swap the seller's surcharge pays."""
    tx = tx if tx is not None else order.payout_tx
    if not order.swap_surcharge_sats or tx is None:
        return 0
    return min(int(order.swap_surcharge_sats), swap_cost(tx))


def sent_satoshis(order, tx):
    """Onchain sats for the buyer: its payout minus only the part of the swap the seller's
    surcharge does not cover (e.g. a mining fee above the one priced)."""
    uncovered = swap_cost(tx) - covered_by_seller(order, tx)
    return int(float(tx.num_satoshis) - uncovered)


def refund_due(order):
    if not order.swap_surcharge_sats:
        return 0
    used = covered_by_seller(order) if order.is_swap else 0
    return max(0, int(order.swap_surcharge_sats) - used)


def refund_seller(order):
    """At completion: what the swap did not use goes back to the seller as rewards (once)."""
    from api.logics import Logics, ledger_credit

    due = refund_due(order)
    if due <= 0 or order.trade_escrow is None:
        return 0
    seller = order.trade_escrow.sender
    if ledger_credit(
        seller.robot, due, "swap_surcharge_back", f"swap_surcharge_back:{order.id}", order.id,
        "unused part of the swap the seller paid for the buyer",
    ):
        order.swap_surcharge_refund_sats = due
        order.save(update_fields=["swap_surcharge_refund_sats"])
        order.log(
            f"Hybrid B: {due} Sats of the seller's swap surcharge were not used and went back to "
            f"Robot({seller.robot.id},{seller.username}) as rewards"
        )
    return due


def refund_all_to_seller(order, reason):
    """Disputes won by the buyer: it is paid in rewards (Lightning), there is no swap."""
    from api.logics import ledger_credit

    if not order.swap_surcharge_sats or order.trade_escrow is None:
        return 0
    seller = order.trade_escrow.sender
    due = int(order.swap_surcharge_sats)
    if ledger_credit(
        seller.robot, due, "swap_surcharge_back", f"swap_surcharge_back:{order.id}", order.id, reason
    ):
        order.swap_surcharge_refund_sats = due
        order.save(update_fields=["swap_surcharge_refund_sats"])
    return due
