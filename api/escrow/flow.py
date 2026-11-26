import logging
import math

from django.db import transaction
from django.db.models import F
from datetime import timedelta

from decouple import config
from django.utils import timezone

from api.escrow import service
from api.escrow.service import EscrowError

logger = logging.getLogger(__name__)
from api.escrow.transactions import (
    DUST_P2TR,
    dust_for,
    VB_BOND_COOP,
    VB_OVERHEAD,
    VB_TAPROOT_KEY_INPUT,
    output_vbytes,
)


def is_onchain(order):
    from api.models import Order

    return order.escrow_type == Order.EscrowTypes.ONCHAIN


def escrow_of(order):
    from api.models import OnchainEscrow

    return OnchainEscrow.objects.filter(order=order).first()


def on_contract(order):
    if is_onchain(order):
        service.create_escrow(order)


# 0.9.9: receive addresses of the standard types (Kilombino: "1…" and "3…" too).
ADDRESS_TYPES_TEXT = (
    "this address is not valid (a typo or a missing character?): use a valid address: "
    "legacy (1…), P2SH (3…), segwit (bc1q…) or taproot (bc1p…)"
)


def _spk_for_address(address):
    """The scriptPubKey of a receive address: P2PKH, P2SH, segwit v0 or taproot (0.9.9; until
    0.9.8 only segwit and taproot). Anything else (an unknown witness version, a bare script)
    is refused."""
    info = service.rpc("validateaddress", [address])
    if not info.get("isvalid"):
        raise EscrowError(ADDRESS_TYPES_TEXT)
    spk = bytes.fromhex(info["scriptPubKey"])
    if not is_receive_script(spk):
        raise EscrowError(ADDRESS_TYPES_TEXT)
    return spk


def is_receive_script(spk):
    p2pkh = len(spk) == 25 and spk[:3] == b"\x76\xa9\x14" and spk[23:] == b"\x88\xac"
    p2sh = len(spk) == 23 and spk[:2] == b"\xa9\x14" and spk[22:] == b"\x87"
    v0 = spk[:1] == b"\x00" and len(spk) in (22, 34) and spk[1] == len(spk) - 2
    v1 = spk[:2] == b"\x51\x20" and len(spk) == 34
    return p2pkh or p2sh or v0 or v1


def set_payout_address(order, user, address):
    """The buyer's address for the release; the seller's for a refund."""
    role = service.role_of(order, user)
    escrow = escrow_of(order)
    if escrow is None:
        raise EscrowError("this order has no onchain escrow")
    spk = _spk_for_address(address)
    from api.models import OnchainEscrow

    if escrow.status in (OnchainEscrow.Status.RELEASING, OnchainEscrow.Status.RELEASED, OnchainEscrow.Status.REFUNDED):
        raise EscrowError("the release address can no longer be changed")
    data = dict(escrow.funding_signatures)
    data[f"{role}_payout_spk"] = spk.hex()
    data[f"{role}_payout_address"] = address
    escrow.funding_signatures = data
    escrow.save(update_fields=["funding_signatures", "updated_at"])
    default_bond_return(order, user, spk, address)
    return escrow


def default_bond_return(order, user, spk, address):
    """0.9.7: a trader's payout address is also where its free bond goes back automatically,
    unless it gave a return address of its own."""
    from api.escrow import bonds
    from api.models import OnchainBond

    held = (OnchainBond.Status.FUNDING, OnchainBond.Status.SIGN, OnchainBond.Status.LOCKED)
    for bond in OnchainBond.objects.filter(order=order, owner=user, status__in=held):
        if (bond.auto_return or {}).get("address"):
            continue
        try:
            bonds.set_return_address(bond, spk, address)
        except EscrowError:
            pass


def min_coordinator_fee():
    """Smallest coordinator fee output (rl31-beta13): below dust an output cannot exist, and a
    dropped fee went to the miners while proceeds still counted it (orders #20/#21)."""
    return max(0, config("ESCROW_MIN_COORDINATOR_FEE_SATS", cast=int, default=1000))


# Fee plans of an onchain escrow, fixed when the escrow is created (the order's contract):
# 1 (up to rl31-beta14): the coordinator fee is an output of the deposit, kept on a cancel;
#   hybrid A pays a second fee output in the release, each output at least the minimum.
# 2 (rl31-beta15, Kilombino 17 nov 2026: a collaborative cancel refunds the fee, as with
#   Lightning): the fees stay inside the escrow output and the coordinator (and the optional
#   dev share) is paid only by the release of a completed trade, in ONE output of at least the
#   minimum for the whole order (hybrid A included). A cancel or a "return everything" dispute
#   gives each trader its fee back; a dispute with a winner pays the coordinator as before.
FEE_PLAN = 2


def fee_plan(escrow):
    if escrow is None:
        return FEE_PLAN
    return int((escrow.funding_signatures or {}).get("fee_plan") or 1)


def _order_fee_plan(order):
    if not getattr(order, "pk", None):
        return FEE_PLAN
    return fee_plan(escrow_of(order))


def fees(order, version=None):
    """(seller_fee, buyer_fee) for the onchain escrow, raised so that the coordinator gets at
    least the minimum (charged on the same sides, in the same proportion). Once the deposit is
    planned, the plan's amounts."""
    escrow = escrow_of(order) if getattr(order, "pk", None) else None
    plan = (escrow.funding_signatures or {}).get("plan") if escrow is not None else None
    if plan and "seller_fee" in plan and "buyer_fee" in plan:
        return int(plan["seller_fee"]), int(plan["buyer_fee"])
    version = version or (fee_plan(escrow) if escrow is not None else FEE_PLAN)
    seller_fee, buyer_fee = service.fee_split(order)
    floor = min_coordinator_fee()
    if seller_fee + buyer_fee <= 0:
        return seller_fee, buyer_fee
    if version < 2 and floor > 0 and buyer_fee_at_release(order):
        # Plan 1, hybrid A: two outputs, the seller's fee in the deposit, the buyer's in the release.
        return (max(seller_fee, floor) if seller_fee else 0, max(buyer_fee, floor) if buyer_fee else 0)
    total = seller_fee + buyer_fee
    if floor > 0 and total < floor:
        raised_seller = math.ceil(seller_fee * floor / total)
        seller_fee, buyer_fee = raised_seller, max(0, floor - raised_seller)
    if version >= 2 and seller_fee + buyer_fee < DUST_P2TR:
        return 0, 0  # (only with the minimum off) one output below dust cannot exist: not charged
    return seller_fee, buyer_fee


def fees_held(escrow):
    """Plan 2: the fees each trader left inside the escrow output (given back on a cancel)."""
    held = ((escrow.funding_signatures or {}).get("plan") or {}).get("fees_in_escrow") or {}
    return {"seller": int(held.get("seller") or 0), "buyer": int(held.get("buyer") or 0)}


def charged_fees(escrow, order=None):
    """{'seller': sats, 'buyer': sats}: the coordinator fee each trader really paid."""
    plan = (escrow.funding_signatures or {}).get("plan") or {}
    data = escrow.funding_signatures or {}
    if "seller_fee" in plan:
        seller_fee, buyer_fee = int(plan.get("seller_fee") or 0), int(plan.get("buyer_fee") or 0)
    else:  # plans before rl31-beta9 kept no fees: what the split asked for then
        seller_fee, buyer_fee = service.fee_split(order or escrow.order)
    if fee_plan(escrow) < 2:
        if plan.get("buyer_fee_at_release"):
            buyer_fee = int(data.get("release_coordinator_sats") or 0)
        return {"seller": seller_fee, "buyer": buyer_fee}
    if not escrow.release_txid or data.get("cancel") or escrow.release_kind == escrow.ReleaseKind.RESCUE:
        return {"seller": 0, "buyer": 0}
    return {"seller": seller_fee, "buyer": buyer_fee}


def dev_paid(escrow):
    """Dev share actually paid: in the deposit (plan 1) or in the release (plan 2)."""
    if fee_plan(escrow) < 2:
        return int(escrow.dev_fee_sats or 0)
    return int((escrow.funding_signatures or {}).get("release_dev_sats") or 0)


def recommended_funding(order, escrow, role, from_bond=False):
    """What each trader should send to its funding address: what it owes plus room for its
    share of the deposit's mining fee at a generous rate. from_bond: the buyer's fee comes
    out of its bond instead (no funding input nor change of its own, only its bond input)."""
    seller_fee, buyer_fee = fees(order)
    owed = order.last_satoshis + seller_fee if role == "seller" else buyer_fee
    # Frozen the first time it is shown (rl31-beta12): recomputed from the live fee rate it
    # drifted after the trader paid and the client refused the deposit by 1 sat (order #21).
    key = f"recommended_{role}{'_bond' if from_bond else ''}"
    frozen = (escrow.funding_signatures or {}).get(key) if escrow is not None else None
    if frozen:
        return owed, int(frozen)
    rate = max(service.fee_rate(2) * 2, 2.0)
    # 0.9.9: with the fees kept in the escrow (fee plan 2, every escrow since rl31-beta15) the
    # deposit's only shared output is the escrow itself; counting three made the recommended
    # amount overshoot by ~43 vB at twice the rate (#27: 536 sats left over).
    n_shared = 1 if escrow is not None and fee_plan(escrow) >= 2 else 3
    shared = VB_OVERHEAD + n_shared * output_vbytes(b"\x00" * 34)
    if from_bond:
        vb = VB_BOND_COOP + shared / 2
    else:
        vb = VB_TAPROOT_KEY_INPUT + output_vbytes(b"\x00" * 34) + shared / 2
    # a topped-up bond (rl31-beta18) is one more bond input per extra coin, paid by its owner
    try:
        from api.escrow import bonds as _bonds

        mine = trade_bonds(order).get(role) if not role_is_ln(order, role) else None
        n = len(_bonds.coins_of(mine)) if mine is not None else 0
        # 0.9.3: without from_bond the first bond coin was left out too (its input is in the
        # deposit and its owner pays it): #24 came out 15 sats short at the live rate.
        vb += VB_BOND_COOP * (max(0, n - 1) if from_bond else n)
    except Exception:
        pass
    recommended = owed + math.ceil(vb * rate)
    if escrow is not None and getattr(escrow, "pk", None):
        from api.models import OnchainEscrow

        with transaction.atomic():
            row = OnchainEscrow.objects.select_for_update().get(pk=escrow.pk)
            data = dict(row.funding_signatures or {})
            if key in data:
                return owed, int(data[key])
            data[key] = recommended
            row.funding_signatures = data
            row.save(update_fields=["funding_signatures"])
            escrow.funding_signatures = data
    return owed, recommended


def buyer_fee_from_bond(order, escrow=None):
    """The buyer only pays its onchain bond: its coordinator fee and its share of the deposit's
    mining fee come out of the bond, which is an input of the deposit anyway
    (ESCROW_BUYER_FEE_FROM_BOND, on by default). Kilombino, 16 nov 2026: "que el comprador
    aporte esa minúscula cantidad con el primer pago que ya hizo". Once the deposit is
    planned, the plan says which way it went."""
    from api.models import OnchainBond

    escrow = escrow or escrow_of(order)
    if escrow is not None and escrow.funding_signatures.get("plan"):
        return bool(escrow.funding_signatures["plan"].get("buyer_fee_from_bond"))
    if not config("ESCROW_BUYER_FEE_FROM_BOND", cast=bool, default=True):
        return False
    bond = trade_bonds(order).get("buyer")
    if bond is None or not bond.value or bond.status not in (OnchainBond.Status.LOCKED, OnchainBond.Status.SIGN):
        return False
    _, worst = recommended_funding(order, escrow, "buyer", from_bond=True)
    return bond.value - worst >= DUST_P2TR


def coordinator_fee_spk():
    address = config("ESCROW_COORDINATOR_ADDRESS", default="")
    if not address:
        raise EscrowError("ESCROW_COORDINATOR_ADDRESS is not set")
    return _spk_for_address(address)


def dev_fee(order=None, coordinator_total=0):
    """Onchain dev fund share, out of the coordinator fees (rl31-beta13): ESCROW_DEV_FEE_PERCENT
    of them to ESCROW_DEV_ADDRESS. Off unless both are set; never leaves the coordinator below
    its minimum output (then the coordinator keeps it all)."""
    pct = config("ESCROW_DEV_FEE_PERCENT", cast=float, default=0.0)
    address = config("ESCROW_DEV_ADDRESS", default="")
    if pct <= 0 or not address or coordinator_total <= 0:
        return 0, None
    sats = math.floor(coordinator_total * pct / 100)
    if sats < DUST_P2TR or coordinator_total - sats < min_coordinator_fee():
        return 0, None
    return sats, _spk_for_address(address)


def bond_of(order, role, owner=None):
    """The onchain bond of 'maker' or 'taker' (the taker's is the current taker's, or the given owner's)."""
    from api.models import OnchainBond

    q = OnchainBond.objects.filter(order=order, role=role)
    if role == "taker":
        owner = owner or order.taker
        if owner is None:
            return None
        q = q.filter(owner=owner)
    return q.order_by("-created_at").first()


def trade_bonds(order):
    """{'seller': bond, 'buyer': bond} for the current maker and taker."""
    from api.logics import Logics

    maker, taker = bond_of(order, "maker"), bond_of(order, "taker")
    if Logics.is_seller(order, order.maker):
        return {"seller": maker, "buyer": taker}
    return {"seller": taker, "buyer": maker}


def _advance_bonds(order):
    from api.escrow import bonds
    from api.logics import Logics
    from api.models import OnchainBond, Order, TakeOrder

    if order.status == Order.Status.WFB:
        bond = bond_of(order, "maker")
        if bond:
            bond = bonds.refresh(bond)
            if bond.status == OnchainBond.Status.LOCKED:
                order.log("Onchain maker bond **locked**")
                Logics.publish_order(order)
    elif order.status == Order.Status.PUB:
        # Oldest take first: if several onchain taker bonds lock in the same pass (same block),
        # the robot that took the order first wins; the others lose the race and get their bond back.
        for take_order in TakeOrder.objects.filter(order=order, expires_at__gt=timezone.now()).order_by("id"):
            bond = OnchainBond.objects.filter(order=order, role="taker", take_order=take_order).order_by("-created_at").first()
            if not bond:
                continue
            extend_take_from_mempool(take_order, bond)
            bond = bonds.refresh(bond)
            if bond.status == OnchainBond.Status.LOCKED:
                order.log("Onchain taker bond **locked**")
                Logics.finalize_contract(take_order)
                break


def extend_take_from_mempool(take_order, bond):
    """26 nov 2026 (Kilombino, TG 15573): an onchain take has ESCROW_BOND_SECONDS (3 h) from the
    moment its bond is seen in the mempool to confirm it. The first time the payment shows up,
    the take's deadline is pushed to that moment + 3 h (never brought forward)."""
    from django.core.cache import cache

    from api.models import OnchainBond

    if bond.status != OnchainBond.Status.FUNDING:
        return False
    try:
        pending = _unconfirmed(bond.address)
    except Exception:
        return False
    if not pending or not cache.add(f"onchain-take-mempool:{take_order.pk}", 1, timeout=2 * 24 * 3600):
        return False
    try:  # Kilombino TG 15618: the maker is told once per take that a bond is on its way
        from api import step_alerts

        step_alerts.on_take_in_mempool(bond.order, take_order)
    except Exception as e:
        logger.warning(f"Order {bond.order_id}: take-in-mempool notice failed: {e}")
    until = timezone.now() + timedelta(seconds=config("ESCROW_BOND_SECONDS", cast=int, default=3 * 3600))
    if until > take_order.expires_at:
        take_order.expires_at = until
        take_order.save(update_fields=["expires_at"])
        bond.order.log(
            f"Onchain take of Robot({take_order.taker.robot.id},{take_order.taker}): its bond is in the mempool, "
            f"the take now waits for its confirmation until {until:%Y-%m-%d %H:%M} UTC"
        )
    return True


def pending_onchain_takes(order):
    """Kilombino TG 15618: what the maker of a public order may know of the takes paying an
    onchain bond, without saying who: how many bonds are in the mempool (waiting for their
    1 confirmation), how many are confirmed and being signed, how many are not paid yet."""
    from django.core.cache import cache

    from api.models import OnchainBond

    out = {"mempool": 0, "confirmed": 0, "awaiting_payment": 0, "txs": []}
    now = timezone.now()
    bonds = OnchainBond.objects.filter(
        order=order, role="taker", take_order__isnull=False, take_order__expires_at__gt=now,
        status__in=(OnchainBond.Status.FUNDING, OnchainBond.Status.SIGN),
    ).select_related("take_order")
    seen = set()
    for bond in bonds.order_by("-created_at"):
        if bond.take_order_id in seen:
            continue
        seen.add(bond.take_order_id)
        if bond.status == OnchainBond.Status.SIGN:
            out["confirmed"] += 1
            if bond.txid:
                out["txs"].append({"txid": bond.txid, "stage": "confirmed"})
        elif cache.get(f"onchain-take-mempool:{bond.take_order_id}"):
            out["mempool"] += 1
            # TG 15622: the bond's own funding transaction(s), so the maker can check it
            for txid in _take_mempool_txids(bond):
                out["txs"].append({"txid": txid, "stage": "mempool"})
        else:
            out["awaiting_payment"] += 1
    return out


def _take_mempool_txids(bond):
    """The unconfirmed transactions paying this take bond's address (cached 30 s: the maker's
    app polls the order)."""
    from django.core.cache import cache

    key = f"onchain-take-txids:{bond.pk}"
    txids = cache.get(key)
    if txids is None:
        try:
            txids = sorted({txid for txid, _ in _unconfirmed(bond.address)})
        except Exception as e:
            logger.warning(f"Order {bond.order_id}: take bond mempool txids not read: {e}")
            return []
        cache.set(key, txids, timeout=30)
    return list(txids)


def advance_onchain_takes(order):
    """Mixta C/D (26 nov 2026): a public Lightning order with takes paying an onchain bond:
    their deadlines, mempool notices, and the contract once one of those bonds is locked."""
    _advance_bonds(order)
    try:
        notify_mempool(order, None)
    except Exception as e:
        logger.warning(f"Order {order.id}: onchain take mempool check failed: {e}")
    try:  # 0.9.14-beta6: a confirmed take bond waits for its owner's app to sign
        notify_signatures(order, None)
    except Exception as e:
        logger.warning(f"Order {order.id}: onchain take signature notice failed: {e}")


def advance(order):
    """Moves an onchain-escrow order forward; called from the follow_invoices loop."""
    from api.logics import Logics
    from api.models import OnchainBond, OnchainEscrow, Order
    from api.tasks import send_notification

    _advance_bonds(order)
    order.refresh_from_db()
    escrow = escrow_of(order)
    try:
        notify_mempool(order, escrow)
    except Exception as e:  # a notice never holds the order up
        logger.warning(f"Order {order.id}: onchain mempool check failed: {e}")
    try:
        notify_signatures(order, escrow)
    except Exception as e:
        logger.warning(f"Order {order.id}: onchain signature notice failed: {e}")
    try:  # 0.9.4: funding owed/confirmed, deposit and release in the mempool
        from api import step_alerts

        step_alerts.watch_onchain(order, escrow)
    except Exception as e:
        logger.warning(f"Order {order.id}: onchain step notice failed: {e}")
    if escrow is None:
        return
    S = OnchainEscrow.Status
    if escrow.status == S.KEYS and escrow.address and order.status == Order.Status.WF2:
        data = escrow.funding_signatures
        if data.get("buyer_payout_spk") and data.get("seller_payout_spk"):
            order.update_status(Order.Status.WFE)
            order.expires_at = timezone.now() + timedelta(
                seconds=config("ESCROW_FUNDING_SECONDS", cast=int, default=3 * 3600)
            )
            order.save(update_fields=["expires_at"])
    if escrow.status == S.KEYS and order.status == Order.Status.WFE:
        seller_fee, buyer_fee = fees(order)
        seller_owed, _ = recommended_funding(order, escrow, "seller")
        tb = trade_bonds(order)
        from api.models import LNPayment

        def bond_ready(role):
            if role_is_ln(order, role):  # hybrid A/D: this side's bond is a Lightning hold invoice
                lb = ln_bond(order)
                return lb is not None and lb.status == LNPayment.Status.LOCKED
            b = tb.get(role)
            return b is not None and b.status == OnchainBond.Status.LOCKED

        bonds_ready = all(bond_ready(r) for r in service.ROLES)
        if any(role_is_ln(order, r) for r in service.ROLES):
            tb = {r: (None if role_is_ln(order, r) else b) for r, b in tb.items()}
        at_release = bool(buyer_fee) and buyer_fee_at_release(order)
        from_bond = bool(buyer_fee) and not at_release and buyer_fee_from_bond(order, escrow)
        have_s = sum(u.value for u in service.funding_utxos(escrow, "seller"))
        buyer_ready = from_bond or at_release or not buyer_fee
        if not buyer_ready:
            buyer_owed, _ = recommended_funding(order, escrow, "buyer")
            buyer_ready = sum(u.value for u in service.funding_utxos(escrow, "buyer")) >= buyer_owed
        if bonds_ready and have_s >= seller_owed and buyer_ready:
            in_escrow = fee_plan(escrow) >= 2
            if in_escrow:  # plan 2: no fee output in the deposit, the release pays them
                dev_sats, dev_spk = 0, None
            else:
                dev_sats, dev_spk = dev_fee(order, seller_fee + (0 if at_release else buyer_fee))
            try:
                service.build_deposit(
                    escrow, order.last_satoshis, seller_fee, 0 if at_release else buyer_fee, coordinator_fee_spk(),
                    dev_sats, dev_spk, tb, buyer_fee_from_bond=from_bond, fees_in_escrow=in_escrow,
                )
                if at_release:
                    escrow.refresh_from_db()
                    data = dict(escrow.funding_signatures)
                    data["plan"] = {**data["plan"], "buyer_fee": buyer_fee, "buyer_fee_at_release": buyer_fee}
                    escrow.funding_signatures = data
                    escrow.save(update_fields=["funding_signatures"])
                escrow.refresh_from_db()
                if service.rescue_plan(escrow) >= 2:
                    service.build_rescue(escrow, *_returns(escrow))
                # 0.9.9: both have to sign now: time for both, whoever was slow to fund
                need_sign = [r for r in service.ROLES if waiting_for(order, escrow, r)[0] == "you"]
                give_time(order, escrow, need_sign, "the deposit was built and needs signatures")
                order.log(
                    "Onchain deposit **built** (trade amount + "
                    + ("the onchain bond" if any(role_is_ln(order, r) for r in service.ROLES) else "both bonds")
                    + (", the buyer's fee out of its bond" if from_bond else "")
                    + (", the buyer's fee out of its payout (Lightning bond)" if at_release else "")
                    + (", the fees kept in the escrow until the trade completes" if in_escrow else "")
                    + "), waiting for the traders' signatures"
                )
            except Exception as e:
                order.log(f"Onchain deposit not built yet: {e}", level="WARN")
    if escrow.status == S.MEMPOOL:
        escrow = service.refresh_confirmation(escrow)
        if escrow.status == S.LOCKED and order.status == Order.Status.WFE:
            order.log(f"Onchain escrow **locked** in {escrow.funding_txid}:{escrow.funding_vout}")
            order.update_status(Order.Status.CHA)
            order.expires_at = timezone.now() + timedelta(seconds=order.t_to_expire(Order.Status.CHA))
            order.save(update_fields=["expires_at"])
            send_notification.delay(order_id=order.id, message="fiat_exchange_starts")
    if escrow.status == S.RELEASED and order.status == Order.Status.PAY:
        order.log(f"Onchain escrow **released** to the buyer in {escrow.release_txid}")
        Logics.complete_order(order)
    if escrow.status in (S.RELEASED, S.REFUNDED) and order.status == Order.Status.CHA and escrow.funding_signatures.get("cancel"):
        order.log(f"Onchain escrow **returned** after the collaborative cancellation in {escrow.release_txid}")
        order.update_status(Order.Status.CCA)


def _bond_values(escrow):
    """What each bond left in the escrow output (the buyer's bond minus its fee when the fee
    came out of it) and the trade amount."""
    plan = escrow.funding_signatures.get("plan", {})
    from api.models import OnchainBond

    values = {"seller": 0, "buyer": 0}
    in_escrow = plan.get("bond_in_escrow") or {}
    for role, pk in plan.get("bond_ids", {}).items():
        values[role] = in_escrow[role] if role in in_escrow else OnchainBond.objects.get(pk=pk).value
    return values, plan.get("trade_sats", escrow.escrow_sats - sum(values.values()))


def _extras(escrow):
    """rl31-beta20: what each trader's funding left in the escrow output beyond what it owed
    (too small for a change output): it goes back to it with its bond, whatever happens."""
    ex = (escrow.funding_signatures.get("plan", {}) or {}).get("extra_in_escrow") or {}
    return {"seller": int(ex.get("seller") or 0), "buyer": int(ex.get("buyer") or 0)}


def _spk(escrow, role):
    hex_ = escrow.funding_signatures.get(f"{role}_payout_spk")
    if not hex_:
        raise EscrowError(f"the {role} has not given a release address yet")
    return bytes.fromhex(hex_)


def _fee_outputs(order, escrow, coordinator_sats):
    """Plan 2: the coordinator's output of a release and the dev share out of it."""
    from api.escrow.taproot import TxOut

    if coordinator_sats <= 0:
        return [], 0, 0
    dev_sats, dev_spk = dev_fee(order, coordinator_sats)
    outs = [TxOut(coordinator_sats - dev_sats, coordinator_fee_spk())]
    if dev_sats:
        outs.append(TxOut(dev_sats, dev_spk))
    return outs, coordinator_sats - dev_sats, dev_sats


def _paid_to_coordinator(tx):
    """Sats a release transaction really pays to the coordinator's fee address."""
    spk = coordinator_fee_spk()
    return sum(o.value for o in tx.outputs if o.script_pubkey == spk)


def _record_release(escrow, **values):
    data = dict(escrow.funding_signatures)
    data.update(values)
    escrow.funding_signatures = data
    escrow.save(update_fields=["funding_signatures"])


def seller_leaf(escrow):
    """0.9.5: the seller's app signs the cooperative release alone through its leaf with the
    coordinator, so the release is planned for that witness."""
    return bool(escrow.funding_signatures.get("seller_leaf_release"))


def seller_confirms(order):
    """Seller confirmed the fiat: fix the cooperative release (buyer: amount + its bond;
    seller: its bond; plan 2: the coordinator its fees, out of the escrow)."""
    from api.escrow.taproot import TxOut
    from api.models import OnchainEscrow, Order

    escrow = escrow_of(order)
    if escrow is None or escrow.status != OnchainEscrow.Status.LOCKED:
        raise EscrowError("the onchain escrow is not locked")
    bonds, trade = _bond_values(escrow)
    ex = _extras(escrow)
    fee_later = int(escrow.funding_signatures.get("plan", {}).get("buyer_fee_at_release") or 0)
    if fee_plan(escrow) >= 2:
        held = fees_held(escrow)
        fee_outs, coord_sats, dev_sats = _fee_outputs(order, escrow, held["seller"] + held["buyer"] + fee_later)
        payouts = [TxOut(trade + bonds["buyer"] + ex["buyer"] - fee_later, _spk(escrow, "buyer"))]
        if bonds["seller"] + ex["seller"]:
            payouts.append(TxOut(bonds["seller"] + ex["seller"], _spk(escrow, "seller")))
        payouts += fee_outs
        tx = service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.COOPERATIVE, 0, seller_leaf(escrow))
        _record_release(escrow, release_coordinator_sats=fee_later, release_fee_sats=_paid_to_coordinator(tx),
                        release_dev_sats=dev_sats)
    else:
        if fee_later < DUST_P2TR:  # too small to be an output of its own: not charged
            fee_later = 0
        payouts = [TxOut(trade + bonds["buyer"] + ex["buyer"] - fee_later, _spk(escrow, "buyer"))]
        if bonds["seller"] + ex["seller"]:
            payouts.append(TxOut(bonds["seller"] + ex["seller"], _spk(escrow, "seller")))
        if fee_later:
            payouts.append(TxOut(fee_later, coordinator_fee_spk()))
        service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.COOPERATIVE, 0, seller_leaf(escrow))
        if fee_later:
            _record_release(escrow, release_coordinator_sats=fee_later)
    order.update_status(Order.Status.PAY)
    order.contract_finalization_time = timezone.now()
    order.save(update_fields=["contract_finalization_time"])
    order.log("Seller confirmed the fiat: **cooperative onchain release** started")


def _returns(escrow):
    """What a cancel gives back: the seller the trade amount, its bond and (plan 2) its fee; the
    buyer its bond and (plan 2) its fee."""
    bonds, trade = _bond_values(escrow)
    held = fees_held(escrow)
    ex = _extras(escrow)
    return (trade + bonds["seller"] + held["seller"] + ex["seller"],
            bonds["buyer"] + held["buyer"] + ex["buyer"])


def collaborative_cancel(order):
    """Both asked to cancel after the escrow: seller gets amount + its bond, buyer its bond
    (plan 2: each one its coordinator fee too, nothing is charged)."""
    from api.escrow.taproot import TxOut
    from api.models import OnchainEscrow

    escrow = escrow_of(order)
    if escrow is None or escrow.status != OnchainEscrow.Status.LOCKED:
        raise EscrowError("the onchain escrow is not locked")
    to_seller, to_buyer = _returns(escrow)
    payouts = [TxOut(to_seller, _spk(escrow, "seller"))]
    if to_buyer:
        payouts.append(TxOut(to_buyer, _spk(escrow, "buyer")))
    service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.COOPERATIVE, 0, seller_leaf(escrow))
    if ln_bond(order) is not None:
        from api.logics import Logics

        Logics.return_bond(ln_bond(order))
        order.log(f"Lightning {ln_side(order)} bond **unlocked** (collaborative cancellation)")
    _record_release(escrow, cancel=True, release_coordinator_sats=0, release_fee_sats=0, release_dev_sats=0)
    order.log("Collaborative cancellation: **onchain return to the seller** started"
              + (" (coordinator fees refunded)" if fee_plan(escrow) >= 2 else ""))


def return_split(escrow):
    """0.9.9-1: who gets what in a "return everything" dispute release, as [(who, sats)] with
    who 'seller'/'buyer'; the first output pays the mining fee. Shared by return_everything and
    the admin dispute page's preview, so what the page says is what the release does. The
    buyer's part below the dust of its address goes to the seller's output (it cannot be an
    output of its own); without a buyer address that part can only be returned if it is 0."""
    to_seller, to_buyer = _returns(escrow)
    outs = [["seller", to_seller]]
    if to_buyer:
        buyer_spk = escrow.funding_signatures.get("buyer_payout_spk")
        if buyer_spk and to_buyer >= dust_for(bytes.fromhex(buyer_spk)):
            outs.append(["buyer", to_buyer])
        elif buyer_spk:
            outs[0][1] += to_buyer
        else:
            raise EscrowError("the buyer has not given a release address yet")
    return [tuple(o) for o in outs]


def return_everything(order):
    """Dispute solved with no winner: the seller gets the trade amount and its bond back, the
    buyer its bond (what is left of it in the escrow), plan 2: each its fee too, as a
    collaborative cancel; through the seller's leaf with the coordinator, so it works even if
    the buyer is gone. A Lightning taker bond (hybrid A) goes back too: unlocked while still
    held, credited as rewards when it was already settled (it is settled when the dispute
    opens; until 0.9.9-1 the settled bond was simply kept)."""
    from api.escrow.taproot import TxOut
    from api.models import OnchainEscrow

    escrow = escrow_of(order)
    if escrow.status == OnchainEscrow.Status.RELEASING:
        escrow.status = OnchainEscrow.Status.LOCKED
        escrow.save(update_fields=["status"])
    payouts = [TxOut(sats, _spk(escrow, who)) for who, sats in return_split(escrow)]
    service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.SELLER_WINS, 0)
    _record_release(escrow, cancel=True, release_coordinator_sats=0, release_fee_sats=0, release_dev_sats=0)
    if ln_bond(order) is not None:
        ln_bond_return_everything(order)
    order.log("Dispute solved with **everything returned**: waiting for the seller's signature on the return")


def ln_bond_return_everything(order):
    """Hybrid A/D, "return everything": the Lightning bond goes back to its owner."""
    from api.logics import Logics, ledger_credit
    from api.models import LNPayment

    side = ln_side(order)
    bond = ln_bond(order)
    bond.refresh_from_db(fields=["status"])
    if bond.status != LNPayment.Status.SETLED:
        Logics.return_bond(bond)
        bond.refresh_from_db(fields=["status"])
        if bond.status != LNPayment.Status.SETLED:
            order.log(f"Lightning {side} bond **unlocked** (dispute: everything returned)")
            return
    ledger_credit(side_user(order, side).robot, bond.num_satoshis, "dispute_bond_back",
                  f"dispute_bond_back:{order.id}:{bond.payment_hash}", order_id=order.id)
    order.log(f"Dispute solved with everything returned: the Lightning {side} bond of {bond.num_satoshis} Sats "
              "is credited back as rewards")


def dispute_split(order, escrow, buyer_wins):
    """0.9.9-1: who gets what in a dispute release with a winner, as ([(who, sats)], info); who
    is 'buyer'/'seller'/'coordinator'/'dev' and the first output (the winner) pays the mining
    fee. Shared by resolve_dispute and the admin dispute page's preview. info: winner, loser,
    reward (share of the loser's bond for the winner), penalty (what the coordinator keeps of
    the loser's bond), fees (coordinator fees held in the escrow), dev, dev_spk."""
    winner = "buyer" if buyer_wins else "seller"
    loser = "seller" if buyer_wins else "buyer"
    bonds, trade = _bond_values(escrow)
    reward = int(bonds[loser] * config("SLASHED_BOND_REWARD_SPLIT", cast=float, default=0.5))
    ex = _extras(escrow)
    outs = [(winner, trade + bonds[winner] + reward + ex[winner])]
    penalty = bonds[loser] - reward
    # rl31-beta20: the loser's funding extra was never part of its bond: it goes back to it
    # (below dust it cannot be an output of its own and goes with the penalty)
    loser_spk = escrow.funding_signatures.get(f"{loser}_payout_spk")
    if loser_spk and ex[loser] >= dust_for(bytes.fromhex(loser_spk)):
        outs.append((loser, ex[loser]))
    else:
        penalty += ex[loser]
    held = fees_held(escrow)
    fee_sats = held["seller"] + held["buyer"]
    dev_sats, dev_spk = 0, None
    if fee_plan(escrow) >= 2 and fee_sats:
        dev_sats, dev_spk = dev_fee(order, fee_sats)
        if penalty + fee_sats - dev_sats:
            outs.append(("coordinator", penalty + fee_sats - dev_sats))
        if dev_sats:
            outs.append(("dev", dev_sats))
    elif penalty:
        outs.append(("coordinator", penalty))
    info = {"winner": winner, "loser": loser, "reward": reward, "penalty": penalty,
            "fees": fee_sats if fee_plan(escrow) >= 2 else 0, "dev": dev_sats, "dev_spk": dev_spk,
            "bonds": bonds, "trade": trade, "extras": ex}
    return outs, info


def resolve_dispute(order, buyer_wins):
    """Winner: amount (if buyer) or amount back (if seller) + its own bond + a reward share of
    the loser's bond; the rest of the loser's bond goes to the coordinator (plan 2: with the
    fees held in the escrow, as when they were paid in the deposit)."""
    from api.escrow.taproot import TxOut
    from api.models import OnchainEscrow

    escrow = escrow_of(order)
    if escrow.status == OnchainEscrow.Status.RELEASING:
        escrow.status = OnchainEscrow.Status.LOCKED
        escrow.save(update_fields=["status"])
    outs, info = dispute_split(order, escrow, buyer_wins)
    spks = {"coordinator": coordinator_fee_spk if any(w == "coordinator" for w, _ in outs) else None}
    payouts = []
    for who, sats in outs:
        if who == "coordinator":
            spk = spks["coordinator"]()
        elif who == "dev":
            spk = info["dev_spk"]
        else:
            spk = _spk(escrow, who)
        payouts.append(TxOut(sats, spk))
    kind = OnchainEscrow.ReleaseKind.BUYER_WINS if buyer_wins else OnchainEscrow.ReleaseKind.SELLER_WINS
    service.start_release(escrow, payouts, kind, 0)
    extra = {"dispute_coordinator_sats": info["penalty"]}
    if fee_plan(escrow) >= 2:
        fee_sats = info["fees"]
        extra.update(release_coordinator_sats=0, release_fee_sats=fee_sats - info["dev"], release_dev_sats=info["dev"])
    _record_release(escrow, **extra)
    ln_bond_dispute_credit(order, buyer_wins)
    order.log(f"Dispute resolved onchain for the {info['winner']}: waiting for its signature on the release")


def penalize_split(order, escrow, guilty):
    """0.9.10: who gets what when a dispute is solved by penalizing 'seller' or 'buyer' with no
    trade, as [(who, sats)] plus info. Nobody gets trade sats they did not pay for: the seller
    gets its escrow amount back, each trader its fee (no trade, nothing charged) and its funding
    extra; the innocent side its bond plus the reward share of the guilty bond
    (SLASHED_BOND_REWARD_SPLIT, as when a dispute has a winner); the coordinator the rest of the
    guilty bond. The guilty side's extra or fee below its dust goes with the penalty.
    The release goes through the SELLER's leaf (the seller always gets the trade amount back, so
    its app signs, as for "return everything"); the coordinator's penalty output pays the mining
    fee when there is one (info["fee_payer"]), else the seller's."""
    innocent = "buyer" if guilty == "seller" else "seller"
    bonds, trade = _bond_values(escrow)
    held = fees_held(escrow)
    ex = _extras(escrow)
    reward = int(bonds[guilty] * config("SLASHED_BOND_REWARD_SPLIT", cast=float, default=0.5))
    penalty = bonds[guilty] - reward
    got = {
        "seller": trade + held["seller"] + ex["seller"],
        "buyer": held["buyer"] + ex["buyer"],
    }
    got[innocent] += bonds[innocent] + reward
    outs = [["seller", got["seller"]]]
    if got["buyer"]:
        spk = escrow.funding_signatures.get("buyer_payout_spk")
        if spk and got["buyer"] >= dust_for(bytes.fromhex(spk)):
            outs.append(["buyer", got["buyer"]])
        elif guilty == "buyer":
            penalty += got["buyer"]
        elif not spk:
            raise EscrowError("the buyer has not given a release address yet")
        else:  # the innocent buyer's part below its dust (it had no onchain bond): to the seller
            outs[0][1] += got["buyer"]
    fee_payer = 0
    if penalty:
        outs.append(["coordinator", penalty])
        fee_payer = len(outs) - 1  # penalize_dispute falls back to the seller if it cannot pay
    info = {"guilty": guilty, "innocent": innocent, "reward": reward, "penalty": penalty,
            "fee_payer": fee_payer, "bonds": bonds, "trade": trade, "extras": ex, "held": held}
    return [tuple(o) for o in outs], info


def penalize_dispute(order, guilty):
    """0.9.10: Lightning's "penalize maker/taker (no trade)" for an onchain escrow."""
    from api.escrow.taproot import TxOut
    from api.models import OnchainEscrow

    escrow = escrow_of(order)
    if escrow.status == OnchainEscrow.Status.RELEASING:
        escrow.status = OnchainEscrow.Status.LOCKED
        escrow.save(update_fields=["status"])
    outs, info = penalize_split(order, escrow, guilty)
    payouts = [TxOut(sats, coordinator_fee_spk() if who == "coordinator" else _spk(escrow, who)) for who, sats in outs]
    try:
        service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.SELLER_WINS, info["fee_payer"])
    except ValueError:  # the penalty cannot pay the mining fee and stay an output: the seller pays
        info["fee_payer"] = 0
        service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.SELLER_WINS, 0)
    _record_release(escrow, release_fee_payer=outs[info["fee_payer"]][0])
    _record_release(escrow, cancel=True, dispute_outcome=f"penalize_{guilty}", dispute_coordinator_sats=info["penalty"],
                    dispute_reward_sats=info["reward"], release_coordinator_sats=0, release_fee_sats=0, release_dev_sats=0)
    # hybrid A: the Lightning taker bond was settled when the dispute opened
    ln_bond_dispute_credit(order, buyer_wins=info["innocent"] == "buyer")
    order.log(f"Dispute solved onchain by **penalizing the {guilty}** (no trade): waiting for the seller's "
              "signature on the release")


def completed_split(order, escrow):
    """0.9.10: a dispute solved as a completed trade, the release a cooperative one would have
    been: the buyer the trade amount and its bond (less a hybrid A fee taken at release), the
    seller its bond, the coordinator its fees (and the dev share). [(who, sats)] plus info; the
    buyer's output pays the mining fee. Goes through the BUYER's leaf (the buyer gets the trade
    amount, so its app signs; the seller may be gone)."""
    bonds, trade = _bond_values(escrow)
    ex = _extras(escrow)
    fee_later = int(escrow.funding_signatures.get("plan", {}).get("buyer_fee_at_release") or 0)
    dev_sats, dev_spk, coord = 0, None, 0
    if fee_plan(escrow) >= 2:
        held = fees_held(escrow)
        total = held["seller"] + held["buyer"] + fee_later
        if total > 0:
            dev_sats, dev_spk = dev_fee(order, total)
            coord = total - dev_sats
    else:
        if fee_later < DUST_P2TR:
            fee_later = 0
        coord = fee_later
    outs = [("buyer", trade + bonds["buyer"] + ex["buyer"] - fee_later)]
    if bonds["seller"] + ex["seller"]:
        outs.append(("seller", bonds["seller"] + ex["seller"]))
    if coord:
        outs.append(("coordinator", coord))
    if dev_sats:
        outs.append(("dev", dev_sats))
    return outs, {"fee_later": fee_later, "coordinator": coord, "dev": dev_sats, "dev_spk": dev_spk,
                  "bonds": bonds, "trade": trade, "extras": ex}


def complete_dispute(order):
    """0.9.10: Lightning's "successful trade" for an onchain escrow. The order goes to PAY and
    advance() completes it when the release is broadcast (as after a cooperative release)."""
    from api.escrow.taproot import TxOut
    from api.models import OnchainEscrow, Order

    escrow = escrow_of(order)
    if escrow.status == OnchainEscrow.Status.RELEASING:
        escrow.status = OnchainEscrow.Status.LOCKED
        escrow.save(update_fields=["status"])
    outs, info = completed_split(order, escrow)
    payouts = []
    for who, sats in outs:
        spk = coordinator_fee_spk() if who == "coordinator" else info["dev_spk"] if who == "dev" else _spk(escrow, who)
        payouts.append(TxOut(sats, spk))
    tx = service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.BUYER_WINS, 0)
    values = {"dispute_outcome": "completed", "release_coordinator_sats": info["fee_later"]}
    if fee_plan(escrow) >= 2:
        values.update(release_fee_sats=_paid_to_coordinator(tx), release_dev_sats=info["dev"])
    _record_release(escrow, **values)
    if ln_bond(order) is not None:  # hybrid A/D: both bonds back
        ln_bond_return_everything(order)
    order.update_status(Order.Status.PAY)
    order.contract_finalization_time = timezone.now()
    order.save(update_fields=["contract_finalization_time"])
    order.log("Dispute solved as a **completed trade**: waiting for the buyer's signature on the release")


def dispute_outcome(escrow):
    """'penalize_seller' / 'penalize_buyer' / 'completed' for the 0.9.10 dispute releases, else ''."""
    return str((escrow.funding_signatures or {}).get("dispute_outcome") or "") if escrow is not None else ""


def proceeds(order):
    """Coordinator fees really paid on chain by a completed trade: the deposit's fee output
    (plan 1) plus the release's (hybrid A in plan 1; every fee in plan 2)."""
    escrow = escrow_of(order)
    if escrow is None:
        return 0
    data = escrow.funding_signatures or {}
    if fee_plan(escrow) >= 2:
        if data.get("cancel") or not escrow.release_tx:
            return 0
        return int(data.get("release_fee_sats") or 0)
    return escrow.coordinator_fee_sats + int(data.get("release_coordinator_sats") or 0)


def bond_role(order, user):
    from api.models import TakeOrder

    if order.maker == user:
        return "maker", None
    take_order = TakeOrder.objects.filter(order=order, taker=user).order_by("-expires_at").first()
    if order.taker == user or take_order:
        return "taker", take_order
    # 0.9.8: a take that is over (lost the race, expired) still owns its bond here
    if lost_take_bond(order, user) is not None:
        return "taker", None
    raise EscrowError("you are not in this order")


def bond_action(order, user, name, data):
    """Onchain bond actions (views: action=bond_*): key, penalty signature, return."""
    from api.escrow import bonds

    role, take_order = bond_role(order, user)
    if name == "bond_key":
        from django.utils import timezone

        # A Lightning take pays its bond with a hold invoice: no onchain bond for it (0.9.3).
        if role == "taker" and take_order is not None and take_order.lightning and order.taker != user \
                and take_order.expires_at > timezone.now():
            raise EscrowError("this take pays its bond with Lightning")
        # Lightning order: only a take made with an onchain bond (mixta C/D) gets one; the maker
        # of a Lightning order has its hold invoice.
        if not is_onchain(order):
            if role == "maker" or take_order is None or not getattr(take_order, "onchain", False) \
                    or order.taker == user:
                raise EscrowError("this order pays its bonds with Lightning")
        # 0.9.8: no new bond for a take that is over (another taker locked first, or it expired)
        if role == "taker" and order.taker != user and (
            take_order is None or take_order.expires_at <= timezone.now()
        ):
            raise EscrowError("your take of this order is over")
        bond = bonds.open_bond(order, user, role, data["bond_pubkey"], take_order if role == "taker" else None)
        # 0.9.7: optional return address for the automatic return of the bond
        if data.get("return_address"):
            bonds.set_return_address(bond, _spk_for_address(data["return_address"]), data["return_address"])
        return
    bond = bond_of(order, role, owner=user)
    if bond is None:
        raise EscrowError("send your bond key first")
    if name == "bond_penalty_signature":
        bonds.submit_penalty_signature(bond, data.get("signatures") or data["signature"], data.get("ladder"))
    elif name == "bond_penalty_ladder_signature":
        # 0.9.12: the penalty's fee ladder of a bond that is already locked
        bonds.submit_penalty_ladder_signatures(bond, data["ladder"])
    elif name == "bond_return":
        bonds.return_tx(bond, _spk_for_address(data["address"]))
    elif name == "bond_return_signature":
        bonds.submit_return_signature(bond, data.get("signatures") or data["signature"])
    elif name == "bond_leftover_return":
        bonds.leftover_return_tx(bond, _spk_for_address(data["address"]))
    elif name == "bond_leftover_signatures":
        bonds.submit_leftover_signatures(bond, data["signatures"])
    elif name == "bond_return_address":
        # 0.9.7: where the bond goes back automatically (given later: e.g. the payout address)
        bonds.set_return_address(bond, _spk_for_address(data["address"]), data["address"])
    elif name == "bond_auto_return_signature":
        bonds.submit_auto_return_signature(bond, data.get("signatures") or data["signature"], data.get("ladder"))
    else:
        raise EscrowError("unknown bond action")


FAIR_DEADLINE_ACTIONS = ("escrow_keys", "escrow_address", "escrow_rescue_signature", "escrow_funding_signatures")


def action(order, user, name, data):
    """Trader actions on the onchain escrow (views: action=escrow_*)."""
    if name.startswith("bond_"):
        return bond_action(order, user, name, data)
    role = service.role_of(order, user)
    escrow = escrow_of(order)
    if escrow is None:
        raise EscrowError("this order has no onchain escrow")
    _escrow_action(order, user, role, escrow, name, data)
    if name in FAIR_DEADLINE_ACTIONS:
        # 0.9.9: this trader finished its part: its peer gets at least an hour for its own
        try:
            escrow.refresh_from_db()
            order.refresh_from_db()
            give_time(order, escrow, peer_still_owes(order, escrow, role), f"the {role} did its part ({name})")
        except Exception as e:  # never fails the trader's action
            logger.warning(f"Order {order.id}: fair deadline not applied: {e}")


def _escrow_action(order, user, role, escrow, name, data):
    if name == "escrow_keys":
        service.submit_keys(order, user, data["escrow_pubkey"], data["funding_pubkey"],
                            bool(data.get("leaf_release")), bool(data.get("change_to_payout")))
    elif name == "escrow_address":
        set_payout_address(order, user, data["address"])
    elif name == "escrow_rescue_signature":
        service.submit_rescue_signature(escrow, role, data["signature"])
    elif name == "escrow_funding_signatures":
        service.submit_funding_signatures(escrow, role, data["signatures"], data.get("bond_signatures") or data.get("bond_signature"))
    elif name == "escrow_nonce":
        service.submit_release_nonce(escrow, role, data["nonce"])
    elif name == "escrow_partial":
        service.submit_release_partial(escrow, role, data["partial"])
        escrow.refresh_from_db()
        advance(order)
    elif name == "escrow_leaf_signature":
        service.submit_leaf_signature(escrow, role, data["signature"])
    elif name == "escrow_release_leaf":
        # 0.9.5: the seller alone (with the coordinator) finishes the cooperative release
        service.submit_cooperative_leaf(escrow, role, data["signature"])
        escrow.refresh_from_db()
        advance(order)
    elif name == "escrow_sweep":
        sweep_tx(order, escrow, role, _spk_for_address(data["address"]))
    elif name == "escrow_sweep_signatures":
        submit_sweep(order, escrow, role, data["signatures"])
    else:
        raise EscrowError("unknown escrow action")


def order_data(order, user):
    """What a trader's client needs to follow and sign its onchain bond and the escrow."""
    from api.escrow import bonds

    from api.models import OnchainBond

    bond_data = {"escrow_alias": service.coordinator_alias(), "escrow_hrp": service.network_hrp()}
    try:
        brole, _ = bond_role(order, user)
        my_bond = bond_of(order, brole, owner=user)
        bond_data.update(bonds.bond_data(my_bond))
        bond_data["bond_role"] = brole
        if my_bond is not None and my_bond.status == OnchainBond.Status.FUNDING:
            hint = fee_hint()
            if hint:
                bond_data["onchain_fee_hint"] = hint
            pending = _unconfirmed(my_bond.address)
            if pending:
                bond_data["bond_mempool_txid"] = pending[0][0]
                bond_data["bond_mempool_sats"] = sum(v for _, v in pending)
                rate = _min_fee_rate(pending)
                if rate is not None:
                    bond_data["bond_mempool_fee_rate"] = rate
    except EscrowError:
        pass
    escrow = escrow_of(order)
    if escrow is None:
        return bond_data
    role = service.role_of(order, user)
    data = {**bond_data,
        "escrow_type": "onchain",
        "escrow_status": escrow.get_status_display(),
        "escrow_status_code": escrow.status,
        "escrow_role": role,
        "escrow_coordinator_pubkey": escrow.coordinator_pubkey,
        "escrow_seller_pubkey": escrow.seller_pubkey,
        "escrow_buyer_pubkey": escrow.buyer_pubkey,
        "escrow_hrp": service.network_hrp(),
        "escrow_alias": service.coordinator_alias(),
        "escrow_rescue_csv": escrow.rescue_csv,
        "escrow_address": escrow.address,
        "escrow_sats": order.last_satoshis,
        "escrow_output_sats": escrow.escrow_sats,
        "escrow_my_keys_set": bool(getattr(escrow, f"{role}_pubkey")),
        "escrow_peer_keys_set": bool(getattr(escrow, ("buyer" if role == "seller" else "seller") + "_pubkey")),
        "escrow_my_payout_address": escrow.funding_signatures.get(f"{role}_payout_address", ""),
        # rl31-beta15: 2 = the coordinator fee stays in the escrow and is paid by the release
        # of a completed trade only (a cancel returns it); 1 = paid in the deposit.
        "escrow_fee_plan": fee_plan(escrow),
    }
    if escrow.funding_signatures.get("plan"):
        data["escrow_fees_in_escrow"] = fees_held(escrow)
        # rl31-beta20: funding extras that ride in the escrow and come back with each bond
        data["escrow_extra_in_escrow"] = _extras(escrow)
    data["escrow_extra_plan"] = service.extra_plan(escrow)
    # 0.9.9: whatever the deposit does not use comes back by itself to our payout address
    data["escrow_change_to_payout"] = role in service.change_to_payout_roles(escrow)
    fpk = escrow.funding_signatures.get(f"{role}_funding_pubkey")
    at_release = role == "buyer" and bool(fees(order)[1]) and buyer_fee_at_release(order)
    from_bond = role == "buyer" and bool(fees(order)[1]) and not at_release and buyer_fee_from_bond(order, escrow)
    # Hybrid A: the buyer's fee is an output of the release. Both clients check the release to
    # the sat, so both need it (17 nov 2026 QA: the seller's app refused to sign the release,
    # "the release charges a fee we did not agree", because only the buyer was told).
    if bool(fees(order)[1]) and buyer_fee_at_release(order):
        planned = int(escrow.funding_signatures.get("plan", {}).get("buyer_fee_at_release") or 0)
        data["escrow_fee_at_release_sats"] = planned or fees(order)[1]
    if at_release:
        # Hybrid A: Lightning bond, nothing to send onchain; the fee comes out of the payout.
        data["escrow_fee_at_release"] = True
    elif from_bond:
        # Nothing to send: the buyer's fee and mining share come out of its bond. The client
        # still needs the most the deposit may take from it to check the deposit.
        owed, recommended = recommended_funding(order, escrow, role, from_bond=True)
        data["escrow_fee_from_bond"] = True
        data["escrow_funding_owed"] = owed
        data["escrow_funding_recommended"] = recommended
    elif fpk and escrow.address:
        owed, recommended = recommended_funding(order, escrow, role)
        data["escrow_funding_address"] = _funding_address(escrow, role)
        data["escrow_funding_owed"] = owed
        data["escrow_funding_recommended"] = recommended
        if escrow.status == escrow.Status.KEYS:
            # 0.9.3: what already confirmed at the funding address, so the app says "payment
            # confirmed, building the deposit" instead of asking for the payment again (#24)
            try:
                data["escrow_funding_confirmed_sats"] = sum(u.value for u in service.funding_utxos(escrow, role))
            except Exception:
                pass
            hint = fee_hint()
            if hint:
                data["onchain_fee_hint"] = hint
            pending = _unconfirmed(data["escrow_funding_address"])
            if pending:
                data["escrow_funding_mempool_txid"] = pending[0][0]
                data["escrow_funding_mempool_sats"] = sum(v for _, v in pending)
                rate = _min_fee_rate(pending)
                if rate is not None:
                    data["escrow_funding_mempool_fee_rate"] = rate
    who, what = waiting_for(order, escrow, role)
    data["escrow_waiting_for"] = who or ""
    data["escrow_waiting_what"] = what or ""
    # 0.9.14-beta7 (Kilombino TG 15645, #38): whoever waits for the other trader's onchain
    # payment sees it (copy / mempool link) once it is in the mempool
    peer = "buyer" if role == "seller" else "seller"
    if escrow.status == escrow.Status.KEYS and escrow.funding_signatures.get(f"{peer}_funding_pubkey"):
        try:
            pending = _unconfirmed(_funding_address(escrow, peer))
            if pending:
                data["escrow_peer_funding_mempool_txids"] = sorted({txid for txid, _ in pending})
        except Exception as e:
            logger.warning(f"Order {order.id}: the peer's funding payment not read: {e}")
    if escrow.funding_tx and escrow.status == escrow.Status.FUNDING:
        tx, spent, plan = service.stored_deposit(escrow)
        data["escrow_deposit_tx"] = escrow.funding_tx
        data["escrow_deposit_spent"] = plan["spent"]
        data["escrow_deposit_my_inputs"] = plan[f"{role}_inputs"]
        data["escrow_deposit_my_bond_input"] = plan.get("bond_inputs", {}).get(role)
        data["escrow_deposit_my_bond_inputs"] = service.bond_indexes(plan, role)
        data["escrow_deposit_signed"] = bool(escrow.funding_signatures.get(f"{role}_sigs"))
    # Rescue plan 2 (rl31-beta16): the rescue both traders pre-sign before the deposit. The
    # client stores the complete rescue on the device and only then signs the deposit.
    data["escrow_rescue_plan"] = service.rescue_plan(escrow)
    rescue = escrow.funding_signatures.get("rescue")
    if rescue:
        data["escrow_rescue_tx"] = rescue["tx"]
        data["escrow_rescue_to_seller"] = rescue["to_seller"]
        data["escrow_rescue_to_buyer"] = rescue["to_buyer"]
        data["escrow_rescue_sigs"] = {
            r: escrow.funding_signatures.get(f"{r}_rescue_sig", "") for r in service.ROLES
        }
        data["escrow_rescue_my_sig"] = bool(escrow.funding_signatures.get(f"{role}_rescue_sig"))
        if escrow.funding_tx:
            data["escrow_deposit_txid"] = service.Transaction.parse(bytes.fromhex(escrow.funding_tx)).txid()
            if "escrow_deposit_tx" not in data:
                # later on (or on another device) the client can still check and keep the rescue
                plan = escrow.funding_signatures.get("plan", {})
                data["escrow_deposit_tx"] = escrow.funding_tx
                data["escrow_deposit_spent"] = plan.get("spent", [])
                data["escrow_deposit_my_inputs"] = plan.get(f"{role}_inputs", [])
                data["escrow_deposit_my_bond_input"] = plan.get("bond_inputs", {}).get(role)
                data["escrow_deposit_my_bond_inputs"] = service.bond_indexes(plan, role)
    if escrow.funding_txid:
        data["escrow_funding_txid"] = escrow.funding_txid
    if escrow.status == escrow.Status.RELEASING:
        data["escrow_release_tx"] = escrow.release_tx
        data["escrow_release_kind"] = escrow.get_release_kind_display()
        # A cooperative release that returns the escrow (collaborative cancel): the client
        # checks it against what a return owes it, not against a trade release.
        data["escrow_release_cancel"] = bool(escrow.funding_signatures.get("cancel"))
        # What each side is owed, so a client checks the release to the sat (rl31-beta12): the
        # trade amount, what each bond left in the escrow and the coordinator fee taken at release.
        bonds_in, trade_in = _bond_values(escrow)
        data["escrow_trade_sats"] = trade_in
        data["escrow_bond_in_escrow"] = bonds_in
        data["escrow_release_coordinator_sats"] = int(escrow.funding_signatures.get("release_coordinator_sats") or 0)
        data["escrow_release_fee_rate"] = escrow.release_fee_rate
        data["escrow_release_my_nonce"] = escrow.release_nonces.get(role, "")
        data["escrow_release_aggnonce"] = service.aggregate_nonce(escrow) or ""
        data["escrow_release_my_partial"] = bool(escrow.release_partial_sigs.get(role))
        # signing round: a new one starts when a trader had to send a new nonce
        data["escrow_release_round"] = int(escrow.funding_signatures.get("release_round") or 0)
        # 0.9.5: the seller can finish a cooperative release alone (with the coordinator)
        data["escrow_release_leaf_ok"] = role == "seller" and service.leaf_release_ok(escrow)
    if escrow.release_txid:
        data["escrow_release_txid"] = escrow.release_txid
        summary = trade_summary(order, user)
        if summary:
            data["onchain_summary"] = summary
    try:
        left = funding_leftover(order, escrow, role)
    except EscrowError:
        left = []
    if left:
        data["escrow_leftover_sats"] = sum(u.value for u in left)
        if not data.get("escrow_funding_address"):
            data["escrow_funding_address"] = _funding_address(escrow, role)
    sw = escrow.funding_signatures.get(f"sweep_{role}")
    if sw and left:
        data["escrow_sweep_tx"] = sw["tx"]
        data["escrow_sweep_spent"] = sw["spent"]
    if left:
        from api.escrow import bonds

        try:
            fee = sweep_cost(len(left))
            data["escrow_leftover_fee_sats"] = fee
            data["escrow_leftover_worth"] = bonds.worth_recovering(data["escrow_leftover_sats"], fee)
        except Exception:
            pass
    if escrow.funding_signatures.get(f"sweep_{role}_txid"):
        data.update(sweep_info(escrow, role))
    return data


FEE_HINT_CACHE = "onchain-fee-hint"
NEXT_BLOCK_TARGET = 2  # bitcoind treats a target of 1 as 2
HOUR_TARGET = 6


def _sat_vb(btc_per_kvb):
    return float(btc_per_kvb) * 1e5


def _round_up(rate):
    """Fee rates go up to the next tenth of a sat/vB (this chain relays below 1 sat/vB)."""
    return math.ceil(round(float(rate), 6) * 10) / 10


def _node_fee_rates():
    floor = 0.0
    try:
        info = service.rpc("getmempoolinfo")
        floor = max(_sat_vb(info.get("mempoolminfee") or 0), _sat_vb(info.get("minrelaytxfee") or 0))
    except Exception:
        pass
    rates = []
    for target in (NEXT_BLOCK_TARGET, HOUR_TARGET):
        est = service.rpc("estimatesmartfee", [target])
        if not est.get("feerate"):
            raise EscrowError("estimatesmartfee: no estimate")
        rates.append(max(_sat_vb(est["feerate"]), floor))
    return rates


def _mempool_fee_rates():
    from api.mempool import _mempool_fees_with_hard_timeout

    data = _mempool_fees_with_hard_timeout()
    return [float(data["fastestFee"]), float(data.get("hourFee") or data["fastestFee"])]


def fee_hint():
    """What a payment from the trader's own wallet should pay, in sat/vB: {"next_block": X,
    "hour": Y} (Kilombino, 17 nov 2026). From the coordinator's node (estimatesmartfee), else
    from the mempool instance of this chain; None when neither answers (the app hides the hint).
    Cached for a minute, failures too, so a slow mempool never slows the order page."""
    from django.core.cache import cache

    cached = cache.get(FEE_HINT_CACHE)
    if cached is not None:
        return cached or None
    hint = {}
    for source in (_node_fee_rates, _mempool_fee_rates):
        try:
            nxt, hour = source()
            if nxt > 0 and hour > 0:
                nxt, hour = _round_up(nxt), _round_up(hour)
                hint = {"next_block": nxt, "hour": min(hour, nxt)}
                break
        except Exception:
            continue
    cache.set(FEE_HINT_CACHE, hint, timeout=60)
    return hint or None


def tx_fee_rate(txid):
    """The fee rate a transaction in the node's mempool pays, in sat/vB, counting its unconfirmed
    parents (they drag it down) and children (a CPFP bumps it). None if the node does not have it."""
    try:
        e = service.rpc("getmempoolentry", [txid])
    except Exception:
        return None
    try:
        f = e["fees"]
        rate = float(f["base"]) * 1e8 / int(e["vsize"])
        if e.get("ancestorsize"):
            rate = min(rate, float(f["ancestor"]) * 1e8 / int(e["ancestorsize"]))
        if int(e.get("descendantsize") or 0) > int(e["vsize"]):
            rate = max(rate, float(f["descendant"]) * 1e8 / int(e["descendantsize"]))
        return math.floor(rate * 10) / 10
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def low_fee(rate, hint=None):
    """The rate needed for about an hour when a payment pays less than it, else None."""
    hint = hint if hint is not None else fee_hint()
    if rate is None or not hint:
        return None
    return hint["hour"] if rate < hint["hour"] else None


def _min_fee_rate(pending):
    rates = [r for r in (tx_fee_rate(txid) for txid, _ in pending) if r is not None]
    return min(rates) if rates else None


def _unconfirmed(address):
    """Unconfirmed outputs paying an address the watch-only wallet follows: [(txid, sats)]."""
    found = service.rpc("listunspent", [0, 0, [address]], wallet=service.watch_wallet())
    return [(u["txid"], round(u["amount"] * 1e8)) for u in found]


def _funding_address(escrow, role):
    return service.rpc("decodescript", [service.funding_spk(escrow, role).hex()])["address"]


def mempool_seen(order, escrow=None):
    """Payments of this order seen in the node's mempool and not confirmed yet:
    [{"what": "bond"|"funding", "role": maker/taker or seller/buyer, "owner": user id,
      "txid": ..., "sats": ...}]. Bonds still waiting for their confirmation, and the funding
    addresses while the deposit is not built."""
    from api.models import OnchainBond, OnchainEscrow

    seen = []
    for bond in OnchainBond.objects.filter(order=order, status=OnchainBond.Status.FUNDING):
        pending = _unconfirmed(bond.address)
        if not pending:
            continue
        # rl31-beta18: whether this payment, with what is already there, reaches the bond
        from api.escrow import bonds as _bonds

        try:
            have = sum(c[2] for c in _bonds.confirmed_coins(bond.address)) + sum(v for _, v in pending)
        except EscrowError:
            have = None
        for txid, sats in pending:
            seen.append({"what": "bond", "role": bond.role, "owner": bond.owner_id, "txid": txid, "sats": sats,
                         "short": None if have is None else max(0, bond.required_sats - have),
                         "fee_rate": tx_fee_rate(txid)})
    escrow = escrow or escrow_of(order)
    if escrow is not None and escrow.status == OnchainEscrow.Status.KEYS and escrow.address:
        for role in service.ROLES:
            if not escrow.funding_signatures.get(f"{role}_funding_pubkey"):
                continue
            user = order.maker if role_user_is_maker(order, role) else order.taker
            for txid, sats in _unconfirmed(_funding_address(escrow, role)):
                seen.append({"what": "funding", "role": role, "owner": user.id if user else None, "txid": txid, "sats": sats,
                             "fee_rate": tx_fee_rate(txid)})
    return seen


def role_user_is_maker(order, role):
    from api.logics import Logics

    return (role == "seller") == Logics.is_seller(order, order.maker)


def notify_mempool(order, escrow=None):
    """Tell the payer, once per transaction, that its payment is in the mempool and waits for
    1 confirmation (Kilombino, 16 nov 2026)."""
    from django.core.cache import cache

    from api.tasks import send_notification

    for item in mempool_seen(order, escrow):
        key = f"onchain-mempool:{order.id}:{item['what']}:{item['role']}:{item['txid']}"
        if not cache.add(key, 1, timeout=7 * 24 * 3600):
            continue
        order.log(f"Onchain {item['role']} {item['what']} of {item['sats']} Sats **seen in the mempool** ({item['txid']})")
        if item["owner"]:
            send_notification.delay(order_id=order.id, message=mempool_message(item))


SIGN_NOTICE_AFTER = 90  # seconds a signature may be pending before the robot is told
# signatures asked right after a confirmation: told at once (0.9.14-beta6)
SIGN_NOW = ("bond_signature", "take_bond_signature")


def _trader(order, role):
    from api.models import Order

    maker_role = "seller" if order.type == Order.Types.SELL else "buyer"
    return order.maker if role == maker_role else order.taker


def pending_signatures(order, escrow=None):
    """[(user, what)] for every signature the coordinator is waiting for from a robot's app
    (0.9.3): its bond penalty signature, the rescue, the deposit, the release."""
    from api.models import OnchainBond

    out = []
    for bond in OnchainBond.objects.filter(order=order, status=OnchainBond.Status.SIGN):
        # 0.9.14-beta6: a take's bond is told apart (its notice says the order is not won yet)
        out.append((bond.owner, "take_bond_signature" if bond.role == "taker" else "bond_signature"))
    if escrow is not None:
        for role in service.ROLES:
            who, what = waiting_for(order, escrow, role)
            if who == "you" and what and what.endswith("_signature"):
                user = _trader(order, role)
                if user is not None:
                    out.append((user, what))
    return out


def notify_signatures(order, escrow=None):
    """The apps sign by themselves, but only while the order is open: a robot whose signature
    is still pending after SIGN_NOTICE_AFTER seconds is told once to open the order (0.9.3)."""
    import time

    from django.core.cache import cache

    from api.tasks import send_notification

    now = int(time.time())
    for user, what in pending_signatures(order, escrow):
        step = f"{order.id}:{user.id}:{what}:{getattr(escrow, 'status', '')}:{(escrow.funding_signatures or {}).get('release_round', 0) if escrow else 0}"
        cache.add(f"onchain-sign-seen:{step}", now, timeout=24 * 3600)
        seen = cache.get(f"onchain-sign-seen:{step}") or now
        # 0.9.14-beta6 (Kilombino TG 15636): a bond that just got its confirmation is told at
        # once (an onchain take is won by whoever signs first); the rest after the grace period.
        # One notice per step either way (the same "told" key).
        immediate = what in SIGN_NOW
        if not immediate and now - int(seen) < SIGN_NOTICE_AFTER:
            continue
        if cache.add(f"onchain-sign-told:{step}", 1, timeout=7 * 24 * 3600):
            order.log(f"Onchain {what} of Robot({user.robot.id}) pending: robot told to open the order")
            suffix = ":confirmed" if immediate else ""
            send_notification.delay(order_id=order.id, message=f"onchain_sign:{user.id}:{what}{suffix}")


def mempool_message(item, hint=None):
    """onchain_mempool:<user id>:<bond|funding>:<sats>[:<sats missing for the bond, or empty>
    [:<fee rate it pays>:<fee rate needed for about an hour>]] (the last two only when it pays
    less than that, rl31-beta19)."""
    short = item.get("short")
    msg = f"onchain_mempool:{item['owner']}:{item['what']}:{item['sats']}"
    needed = low_fee(item.get("fee_rate"), hint)
    if needed is not None:
        return msg + f":{'' if short is None else short}:{item['fee_rate']:g}:{needed:g}"
    return msg + ("" if short is None else f":{short}")


def waiting_for(order, escrow, role):
    """Who the onchain escrow is waiting for, as the trader 'role' sees it: 'you', 'peer'
    (the other trader), 'confirmation' (a block), 'coordinator' or None. Plus what for."""
    from api.models import OnchainEscrow, Order

    S = OnchainEscrow.Status
    peer = "buyer" if role == "seller" else "seller"
    data = escrow.funding_signatures
    if escrow.status == S.KEYS:
        if not getattr(escrow, f"{role}_pubkey"):
            return "you", "keys"
        if not data.get(f"{role}_payout_spk"):
            return "you", "address"
        if not getattr(escrow, f"{peer}_pubkey"):
            return "peer", "keys"
        if not data.get(f"{peer}_payout_spk"):
            return "peer", "address"
        if order.status == Order.Status.WFE:
            from_bond = buyer_fee_from_bond(order, escrow) or buyer_fee_at_release(order)
            owed = {r: recommended_funding(order, escrow, r)[0] for r in service.ROLES}
            funded = {r: sum(u.value for u in service.funding_utxos(escrow, r, 0)) for r in service.ROLES}
            need = ["seller"] + ([] if from_bond or not fees(order)[1] else ["buyer"])
            missing = [r for r in need if funded[r] < owed[r]]
            if role in missing:
                return "you", "funding"
            if peer in missing:
                return "peer", "funding"
            confirmed = {r: sum(u.value for u in service.funding_utxos(escrow, r, 1)) for r in need}
            if any(confirmed[r] < owed[r] for r in need):
                return "confirmation", "funding"
        return "coordinator", "deposit"
    if escrow.status == S.FUNDING:
        plan = data.get("plan", {})
        signed = lambda r: (data.get(f"{r}_sigs") or not plan.get(f"{r}_inputs")) and (
            not service.bond_indexes(plan, r) or data.get(f"{r}_bond_sig"))
        if service.rescue_plan(escrow) >= 2:
            if not data.get(f"{role}_rescue_sig"):
                return "you", "rescue_signature"
            if not data.get(f"{peer}_rescue_sig"):
                return "peer", "rescue_signature"
        if not signed(role):
            return "you", "deposit_signature"
        if not signed(peer):
            return "peer", "deposit_signature"
        return "coordinator", "deposit"
    if escrow.status == S.MEMPOOL:
        return "confirmation", "deposit"
    if escrow.status == S.RELEASING:
        # 0.9.9-1: a dispute release (a winner, or "return everything" = the seller's leaf) waits
        # for ONE trader's signature, not for the coordinator: the other one only waits (#26: the
        # buyer saw "signing the release together with your peer" with a spinner)
        if escrow.release_kind in (escrow.ReleaseKind.BUYER_WINS, escrow.ReleaseKind.SELLER_WINS):
            signer = "buyer" if escrow.release_kind == escrow.ReleaseKind.BUYER_WINS else "seller"
            return ("you" if role == signer else "peer"), "release_signature"
        if escrow.release_kind != escrow.ReleaseKind.COOPERATIVE:
            return "coordinator", "release"
        # 0.9.5: the seller's app finishes it alone through its leaf with the coordinator
        if role == "seller" and seller_leaf(escrow) and service.leaf_release_ok(escrow):
            return "you", "release_signature"
        if role == "buyer" and seller_leaf(escrow) and service.leaf_release_ok(escrow):
            return "peer", "release_signature"
        if role not in escrow.release_nonces or role not in escrow.release_partial_sigs and service.aggregate_nonce(escrow):
            return "you", "release_signature"
        if peer not in escrow.release_nonces or peer not in escrow.release_partial_sigs:
            return "peer", "release_signature"
        return "coordinator", "release"
    return None, None


def trade_summary(order, user):
    """What an onchain trade left to each side, from the deposit plan and the release."""
    from api.escrow.taproot import Transaction

    escrow = escrow_of(order)
    if escrow is None or not escrow.release_txid:
        return None
    role = service.role_of(order, user)
    plan = escrow.funding_signatures.get("plan", {})
    bonds, trade = _bond_values(escrow)
    tx = Transaction.parse(bytes.fromhex(escrow.release_tx))
    my_spk = escrow.funding_signatures.get(f"{role}_payout_spk", "")
    received = sum(o.value for o in tx.outputs if o.script_pubkey.hex() == my_spk)
    release_mining = escrow.escrow_sats - sum(o.value for o in tx.outputs)
    # payouts[0] paid the release's mining fee: the buyer when released, the seller when returned
    payer = "buyer" if escrow.status == escrow.Status.RELEASED and not escrow.funding_signatures.get("cancel") else "seller"  # REFUNDED/cancel: the seller paid
    from api.models import OnchainBond

    bond_ids = plan.get("bond_ids", {})
    bond_sats = OnchainBond.objects.get(pk=bond_ids[role]).value if role in bond_ids else 0
    is_ln = role_is_ln(order, role)
    if is_ln and ln_bond(order) is not None:
        bond_sats = ln_bond(order).num_satoshis
    return {
        "lightning_bond": is_ln,
        "fee_at_release": role == "buyer" and bool(escrow.funding_signatures.get("release_coordinator_sats")),
        "fee_refunded_sats": fees_held(escrow)[role] if escrow.funding_signatures.get("cancel") else 0,
        "role": role,
        "is_buyer": role == "buyer",
        "trade_sats": trade,
        "received_sats": received,
        "bond_sats": bond_sats,
        "bond_back_sats": bond_sats if is_ln else bonds.get(role, 0),
        # rl31-beta20: a funding excess that rode in the escrow, back with the bond (in received)
        "extra_back_sats": _extras(escrow)[role],
        "coordinator_fee_sats": charged_fees(escrow, order)[role],
        "fee_from_bond": role == "buyer" and bool(plan.get("buyer_fee_from_bond")),
        "deposit_mining_sats": plan.get(f"{role}_mining"),
        "release_mining_sats": release_mining if role == payer else 0,
        "deposit_txid": escrow.funding_txid,
        "release_txid": escrow.release_txid,
        "release_kind": escrow.get_release_kind_display(),
    }


def funding_fault(order):
    """Who is holding up an onchain escrow that ran out of time: 'seller', 'buyer' or None."""
    from api.models import OnchainEscrow

    escrow = escrow_of(order)
    S = OnchainEscrow.Status
    if escrow is None:
        return "seller"
    if escrow.status in (S.MEMPOOL, S.LOCKED):
        return None
    if not escrow.seller_pubkey:
        return "seller"
    if not escrow.buyer_pubkey or not escrow.funding_signatures.get("buyer_payout_spk"):
        return "buyer"
    if escrow.status == S.FUNDING:
        if service.rescue_plan(escrow) >= 2:
            for r in service.ROLES:  # the rescue is signed before the deposit
                if not escrow.funding_signatures.get(f"{r}_rescue_sig"):
                    return r
        if not escrow.funding_signatures.get("seller_sigs"):
            return "seller"
        return "buyer"
    seller_fee, buyer_fee = fees(order)
    seller_owed, _ = recommended_funding(order, escrow, "seller")
    if sum(u.value for u in service.funding_utxos(escrow, "seller", 0)) < seller_owed:
        return "seller"
    return "buyer"


def give_time(order, escrow, roles, reason):
    """0.9.9, fair deadline: the onchain setup (keys, address, rescue, deposit) has one deadline
    for both traders. When one finishes its part, or the coordinator asks both for signatures,
    the other must not be left with the minutes the first one did not use and then take the
    blame (Kilobot #26). The deadline is pushed back so that 'roles' have at least
    ESCROW_PEER_STEP_SECONDS (1 h) from now, at most ESCROW_MAX_DEADLINE_EXTENSIONS times per
    order (each extension at most that long): nobody can stall the order forever."""
    from api.models import OnchainEscrow, Order

    if order.status not in (Order.Status.WF2, Order.Status.WFE) or escrow is None:
        return False
    now = timezone.now()
    if order.expires_at is None or order.expires_at <= now:
        return False  # already expired: the expiry decides
    need = timedelta(seconds=config("ESCROW_PEER_STEP_SECONDS", cast=int, default=3600))
    if order.expires_at >= now + need:
        return False
    if not roles:
        return False
    cap = config("ESCROW_MAX_DEADLINE_EXTENSIONS", cast=int, default=6)
    with transaction.atomic():
        row = OnchainEscrow.objects.select_for_update().get(pk=escrow.pk)
        data = dict(row.funding_signatures or {})
        done = int(data.get("deadline_extensions") or 0)
        if done >= cap:
            return False
        data["deadline_extensions"] = done + 1
        row.funding_signatures = data
        row.save(update_fields=["funding_signatures"])
        escrow.funding_signatures = data
    old = order.expires_at
    order.expires_at = now + need
    order.save(update_fields=["expires_at"])
    order.log(
        f"Fair deadline ({done + 1}/{cap}): {reason}; the {' and '.join(roles)} now "
        f"{'have' if len(roles) > 1 else 'has'} until {order.expires_at:%H:%M} UTC (was {old:%H:%M})"
    )
    return True


def peer_still_owes(order, escrow, role):
    """After 'role' acted: [peer] when 'role' has nothing left to do in the onchain setup and
    its peer still has (keys, address or a signature); else []."""
    peer = "buyer" if role == "seller" else "seller"
    try:
        mine, _ = waiting_for(order, escrow, role)
        theirs, what = waiting_for(order, escrow, peer)
    except Exception:
        return []
    if mine == "you" or theirs != "you" or what == "funding":
        return []
    return [peer]


def expire_funding(order):
    """Expiry of an onchain-escrow order before the escrow is confirmed. The trader at fault
    loses its bond and the other gets the usual reward; funds already sent to a funding
    address stay with their owner, who can spend them back with its own key."""
    from api.logics import Logics
    from api.models import OnchainEscrow, Order

    fault = funding_fault(order)
    if fault is None:
        order.expires_at = timezone.now() + timedelta(hours=1)
        order.save(update_fields=["expires_at"])
        order.log("Onchain deposit broadcast but not confirmed yet: expiry pushed back one hour")
        return True
    escrow = escrow_of(order)
    if escrow and escrow.status in (OnchainEscrow.Status.KEYS, OnchainEscrow.Status.FUNDING):
        escrow.status = OnchainEscrow.Status.ABANDONED
        escrow.save(update_fields=["status", "updated_at"])

    seller_is_maker = Logics.is_seller(order, order.maker)
    at_fault_is_maker = (fault == "seller") == seller_is_maker
    try:
        penalize_side(order, "maker" if at_fault_is_maker else "taker")
    except EscrowError as e:
        order.log(f"Onchain penalty could not be broadcast: {e}", level="ERROR")
    free_side(order, "taker" if at_fault_is_maker else "maker")
    credit_penalty(order, "maker" if at_fault_is_maker else "taker")
    order.update_status(Order.Status.EXP)
    # The buyer is at fault when it did not give its keys / payout address (17 nov 2026 QA:
    # the buyer's app said "Escrow not locked" while the seller had done its part).
    order.expiry_reason = Order.ExpiryReasons.NESCRO if fault == "seller" else Order.ExpiryReasons.NINVOI
    order.save(update_fields=["expiry_reason"])
    order.log(f"Onchain escrow not completed in time by the {fault}: their bond was **penalized**, the other is free to return")
    return True


def cancel_before_escrow(order, canceller):
    """Maker or taker cancels in WF2/WFE: its onchain bond is penalized, the other is freed
    (if the taker cancels, the maker's bond keeps backing the order, which goes back to the book)."""
    from api.logics import Logics
    from api.models import OnchainEscrow, Order

    penalize_side(order, canceller)
    escrow = escrow_of(order)
    if escrow and escrow.status in (OnchainEscrow.Status.KEYS, OnchainEscrow.Status.FUNDING):
        escrow.delete()
    credit_penalty(order, canceller)  # before publishing: that clears the taker
    if canceller == "maker":
        free_side(order, "taker")
        order.update_status(Order.Status.UCA)
        order.log("Maker cancelled before the onchain escrow: maker bond **penalized**, taker bond free to return")
    else:
        Logics.publish_order(order)
        order.log("Taker cancelled before the onchain escrow: taker bond **penalized**, order back in the book")
    return True


def release_bond(order, role, owner=None):
    from api.escrow import bonds

    return bonds.release(bond_of(order, role, owner))


# Bonds of a take that is over: nothing more can happen to them (0.9.8).
_BOND_DONE = (4, 5, 6, 7)  # PENALIZED, RETURNABLE, RETURNED, ABANDONED


def release_take_bond(take_order):
    """Frees the onchain bond(s) of a pending take that did not become the contract (it expired,
    or another taker locked first). Looked up by the take itself, not by the order's escrow
    type: a seller taking with Lightning turns the order into a Lightning order (case B), and
    the bonds of the other takers must still be freed (0.9.8). A bond already confirmed is
    picked up first, so it is free to return at once; one still unconfirmed becomes ABANDONED
    and recover_abandoned() frees it when it confirms."""
    from api.escrow import bonds
    from api.models import OnchainBond

    freed = []
    for bond in OnchainBond.objects.filter(take_order=take_order).exclude(status__in=_BOND_DONE):
        if bond.status == OnchainBond.Status.FUNDING:
            try:
                bond = bonds.refresh(bond)
            except Exception as e:  # the node down: recover_abandoned() catches it later
                logger.warning(f"Order {bond.order_id}: could not look at take bond {bond.pk}: {e}")
        if bonds.release(bond):
            freed.append(bond)
    return freed


def lose_race(order, winner):
    """0.9.8: another taker's bond locked first (a Lightning bond locks in seconds, an onchain
    one needs a confirmation). Every other pending take with an onchain bond is over now: its
    bond is freed (never penalized), the take can no longer win and its robot is told."""
    from api.models import OnchainBond, TakeOrder
    from api.tasks import send_notification

    losers = TakeOrder.objects.filter(order=order).exclude(pk=winner.pk)
    for take in losers:
        if not OnchainBond.objects.filter(take_order=take).exists():
            continue  # a Lightning pretaker: its hold invoice is cancelled if it ever locks
        freed = release_take_bond(take)
        take.expires_at = timezone.now()
        take.save(update_fields=["expires_at"])
        order.log(
            f"Another taker locked first: the onchain take of Robot({take.taker.robot.id},{take.taker}) "
            f"is over, its bond is **free** ({len(freed)} freed, never penalized)"
        )
        send_notification.delay(order_id=order.id, message=f"take_lost:{take.taker_id}")


def lost_take_bond(order, user):
    """The onchain bond of a robot whose take of this order is over (it lost the race or its
    take expired) while it is not the maker nor the taker: what it can still follow and take
    back from the order page. None when there is nothing of the kind."""
    from api.models import OnchainBond, TakeOrder

    if user is None or not user.is_authenticated:
        return None
    if order.maker_id == user.id or order.taker_id == user.id:
        return None
    if TakeOrder.objects.filter(order=order, taker=user, expires_at__gt=timezone.now()).exists():
        return None
    bond = OnchainBond.objects.filter(order=order, role="taker", owner=user).order_by("-created_at").first()
    if bond is None or bond.status not in _BOND_DONE + (OnchainBond.Status.FUNDING, OnchainBond.Status.SIGN):
        return None
    return bond


def lost_take_view(order, user):
    """0.9.8: what the order page shows to a robot that lost the race for this order (or whose
    onchain take expired) while its bond is not settled: only its bond, never the order's
    trade data. None: the usual view (e.g. a public order it may take again)."""
    from api.escrow import bonds
    from api.models import OnchainBond, Order

    bond = lost_take_bond(order, user)
    if bond is None:
        return None
    S = OnchainBond.Status
    pending = []
    if bond.status in (S.ABANDONED, S.FUNDING):
        try:
            pending = _unconfirmed(bond.address)
        except Exception:
            pending = []
    public = order.status in (Order.Status.PUB, Order.Status.PAU)
    if bond.status == S.RETURNABLE:
        done = False
    elif bond.status in (S.ABANDONED, S.FUNDING, S.SIGN):
        done = not pending and bond.status == S.ABANDONED
        if public and done:
            return None  # never paid: just the public order again
    else:  # RETURNED, PENALIZED (never happens to a lost take)
        if public:
            return None
        done = True
    data = {
        "id": order.id,
        "status": order.status,
        "type": order.type,
        "currency": order.currency_id,
        "escrow_type": "onchain",
        "is_maker": False,
        "is_taker": False,
        "is_participant": False,
        "is_buyer": False,
        "is_seller": False,
        "bad_request": "",
        "lost_take": True,
        "lost_take_taken": order.taker_id is not None,
        "lost_take_done": done,
        "escrow_alias": service.coordinator_alias(),
        "escrow_hrp": service.network_hrp(),
        "bond_role": "taker",
        **bonds.bond_data(bond),
    }
    if pending:
        data["bond_mempool_txid"] = pending[0][0]
        data["bond_mempool_sats"] = sum(v for _, v in pending)
    return data


def bonds_locked(order):
    from api.models import OnchainBond

    from api.models import LNPayment

    held = (OnchainBond.Status.LOCKED, OnchainBond.Status.IN_DEPOSIT)
    side = ln_side(order)

    def locked(s):
        if s == side:
            b = order.maker_bond if s == "maker" else order.taker_bond
            return bool(b and b.status == LNPayment.Status.LOCKED)
        b = bond_of(order, s)
        return bool(b and b.status in held)

    return locked("maker"), locked("taker")


def allowed_list():
    """ESCROW_ALLOWED_ROBOTS: comma separated robot nicknames, robot hash ids or
    robot nostr keys (hex or npub). Empty means everyone."""
    return [x.strip() for x in config("ESCROW_ALLOWED_ROBOTS", default="").split(",") if x.strip()]


def restricted():
    """While a list of robots is set, onchain escrow stays hidden from everybody
    else: not announced in /api/info, not in the public book, not on Nostr."""
    return bool(allowed_list())


def _nostr_hex(value):
    if not value.startswith("npub1"):
        return value.lower()
    try:
        from nostr_sdk import PublicKey

        return PublicKey.parse(value).to_hex()
    except Exception:
        return value


def robot_allowed(user):
    allowed = allowed_list()
    if not allowed:
        return True
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    robot = getattr(user, "robot", None)
    names = {user.username}
    if robot is not None:
        if robot.hash_id:
            names.add(robot.hash_id.lower())
        if robot.nostr_pubkey:
            names.add(robot.nostr_pubkey.lower())
    return any(x in names or _nostr_hex(x) in names for x in allowed)


def onchain_size_error(order):
    """Onchain orders use their own size limits: ESCROW_MIN_SATS (default
    MIN_ORDER_SIZE) to ESCROW_MAX_SATS (default MAX_ORDER_SIZE)."""
    from api.logics import Logics

    low = config("ESCROW_MIN_SATS", cast=int, default=config("MIN_ORDER_SIZE", cast=int, default=20000))
    high = config("ESCROW_MAX_SATS", cast=int, default=0) or config("MAX_ORDER_SIZE", cast=int, default=250000)
    if order.has_range:
        # fixed-price orders: from the maker's price, no exchange rate needed (rl31-beta17)
        sizes = [Logics.sats_for_amount(order, order.min_amount), Logics.sats_for_amount(order, order.max_amount)]
    else:
        sizes = [order.t0_satoshis]
    if min(sizes) < low or max(sizes) > high:
        return f"Onchain escrow orders must be between {low} and {high} Sats on this coordinator for now"
    return None


# --- Hybrid: a Lightning taker on an onchain order (Roblosats, rl31-beta10) ----------------
#
# Kilombino, 16 nov 2026: "aunque la orden quiera onchain la fianza se pueda hacer lightning e
# incluso el pago lightning avisando de que se carga la comisión del coordinador".
#
# When taking an onchain order the taker may choose Lightning:
#  A) taker = BUYER: its bond is a Lightning hold invoice; the seller (maker) still funds the
#     onchain escrow, the buyer still gets the sats onchain. Its coordinator fee comes out of
#     its payout in the release (it has no onchain bond to take it from, and should not need
#     onchain coins to buy).
#  B) taker = SELLER: its bond AND its escrow are Lightning hold invoices: the order turns into
#     a Lightning order. The maker's onchain bond stays behind a proxy LNPayment (order.maker_bond)
#     so the whole Lightning machinery works unchanged: settling the proxy penalizes the onchain
#     bond, returning it frees it. The buyer (maker) is paid onchain by the coordinator with the
#     usual on-the-fly swap (swap fee + mining shown before it gives its address), or by Lightning
#     if it prefers.
#  C) taker = BUYER receiving by Lightning from the onchain escrow: NOT in this round
#     (ln_payout_enabled() is False): the buyer of case A receives onchain.

PROXY_PREFIX = "onchain-bond-"


def lightning_taker_enabled():
    return config("ESCROW_LIGHTNING_TAKER", cast=bool, default=True)


def ln_payout_enabled():
    """Case C (buyer of an onchain escrow paid by Lightning through the coordinator). Not
    implemented yet: kept off whatever the configuration says."""
    return False


def taker_role(order):
    """'seller' or 'buyer' for whoever takes this order (the opposite of the maker)."""
    from api.logics import Logics

    return "buyer" if Logics.is_seller(order, order.maker) else "seller"


def ln_side(order):
    """'maker'/'taker': the trader of this onchain order whose bond is a Lightning hold invoice
    (hybrid A: the taker; mixta D, 26 nov 2026: the maker of a Lightning order taken by an
    onchain seller), or None."""
    if order.taker_lightning and order.taker_id is not None:
        return "taker"
    if getattr(order, "maker_lightning", False):
        return "maker"
    return None


def ln_bond(order):
    """The Lightning hold-invoice bond of ln_side(order), or None."""
    side = ln_side(order)
    if side == "taker":
        return order.taker_bond
    if side == "maker":
        return order.maker_bond
    return None


def side_user(order, side):
    return order.taker if side == "taker" else order.maker


def side_role(order, side):
    """'seller'/'buyer' of the maker or the taker."""
    t = taker_role(order)
    if side == "taker":
        return t
    return "seller" if t == "buyer" else "buyer"


def role_is_ln(order, role):
    """True when the trader 'role' (seller/buyer) of this onchain order has a Lightning bond."""
    side = ln_side(order)
    return side is not None and side_role(order, side) == role


def buyer_fee_at_release(order):
    """Case A: the buyer has no onchain bond nor funding: its fee comes out of its payout."""
    return role_is_ln(order, "buyer")


def is_bond_proxy(bond):
    return bond is not None and str(getattr(bond, "payment_hash", "") or "").startswith(PROXY_PREFIX)


def proxied_bond(proxy):
    from api.models import OnchainBond

    try:
        return OnchainBond.objects.get(pk=int(str(proxy.payment_hash)[len(PROXY_PREFIX):]))
    except (OnchainBond.DoesNotExist, ValueError):
        return None


def has_proxy(order):
    """Hybrid B: the maker's onchain bond behind a proxy (a Lightning order since the take)."""
    return is_bond_proxy(order.maker_bond)


def has_taker_proxy(order):
    """Mixta C: the buyer took a Lightning order with an onchain bond, behind a proxy."""
    return is_bond_proxy(order.taker_bond)


def has_any_proxy(order):
    return has_proxy(order) or has_taker_proxy(order)


def user_proxy(order, user):
    """The proxy of this robot's onchain bond in this Lightning order, or None."""
    if user is None:
        return None
    if order.maker_id == user.id and has_proxy(order):
        return order.maker_bond
    if order.taker_id == user.id and has_taker_proxy(order):
        return order.taker_bond
    return None


def make_proxy(order, side, bond):
    """A proxy LNPayment for the locked onchain bond of 'maker'/'taker' so the Lightning
    machinery runs unchanged on it (settle = penalty, return/cancel = free)."""
    from django.contrib.auth.models import User

    from api.models import LNPayment

    proxy, _ = LNPayment.objects.update_or_create(
        payment_hash=f"{PROXY_PREFIX}{bond.pk}",
        defaults=dict(
            concept=LNPayment.Concepts.MAKEBOND if side == "maker" else LNPayment.Concepts.TAKEBOND,
            type=LNPayment.Types.HOLD,
            status=LNPayment.Status.LOCKED,
            sender=bond.owner,
            receiver=User.objects.filter(username=config("ESCROW_USERNAME", default="admin")).first(),
            num_satoshis=bond.value,
            description=f"Onchain {side} bond of order {order.id} (proxy)",
            created_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30),
        ),
    )
    return proxy


def proxy_settle(proxy, reason="", raise_errors=False):
    """Settling the proxy = penalizing the onchain bond. True only the first time."""
    from api.escrow import bonds
    from api.models import LNPayment

    proxy.refresh_from_db(fields=["status"])
    if proxy.status == LNPayment.Status.SETLED:
        return False
    bond = proxied_bond(proxy)
    try:
        bonds.penalize(bond, reason)
    except EscrowError as e:
        if raise_errors:
            raise
        if bond is not None:
            bond.order.log(f"Onchain {bond.role} bond penalty could not be broadcast: {e}", level="ERROR")
        return False
    proxy.status = LNPayment.Status.SETLED
    proxy.save(update_fields=["status"])
    return True


# --- 0.9.12: the onchain maker bond of a hybrid B order in a dispute -----------------------
#
# Kilombino, 21 nov 2026 (TG 14873): an onchain bond is no longer penalized when the dispute
# opens (order #29: the maker's 30,000-sat bond was penalized at 10 sat/vB the moment the chat
# expired). It stays locked behind its proxy until the dispute is decided: the winner gets it
# back on chain (the return it signed in advance, from the fee ladder) and is NOT credited its
# value; the loser's bond is penalized then. A bond penalized earlier (opened under 0.9.11, or by
# the timelock safety guard) is credited back to a winning maker as rewards, as before.

def dispute_bond_sats(order, side, wins):
    """The sats a dispute decision credits for the bond of 'maker'/'taker', and the onchain
    side effect for a hybrid B maker bond: freed (wins) or penalized (loses) on chain. A
    Lightning bond (settled when the dispute opened) is credited whole when its owner wins."""
    from api.escrow import bonds
    from api.models import LNPayment, OnchainBond

    ln = order.maker_bond if side == "maker" else order.taker_bond
    if ln is None:
        return 0
    if not is_bond_proxy(ln):
        return ln.num_satoshis if wins else 0
    bond = proxied_bond(ln)
    if bond is None:
        return ln.num_satoshis if wins else 0
    S = OnchainBond.Status
    if bond.status == S.LOCKED:
        if wins:
            # still at stake on chain: it goes straight back to its owner (signed return, cheapest
            # rung the mempool needs; else the owner's "Get my bond back")
            proxy_return(ln, LNPayment.Status.RETNED)
            order.log(f"Dispute: the {side}'s onchain bond is **freed** and goes back to its owner on chain "
                      "(not credited as rewards)")
            return 0
        bonds.penalize_now(bond, "the dispute was lost")
        return 0
    if bond.status == S.PENALIZED:
        if wins:
            order.log(f"Dispute: the {side}'s onchain bond had been penalized before the decision: its value "
                      f"({ln.num_satoshis} Sats) is credited back as rewards", level="WARN")
            return ln.num_satoshis
        return 0
    # RETURNABLE / RETURNED: it is (or went) back with its owner already
    if not wins and bond.status == S.RETURNED:
        order.log(f"Dispute lost by the {side}, but its onchain bond had already gone back to it on chain: "
                  "nothing to penalize", level="WARN")
    return 0


def proxy_still_locked(order):
    """Hybrid B: the maker's onchain bond is still at stake (not penalized when the dispute opened)."""
    from api.models import OnchainBond

    if not has_proxy(order):
        return False
    bond = proxied_bond(order.maker_bond)
    return bond is not None and bond.status == OnchainBond.Status.LOCKED


def proxy_return(proxy, status):
    """Returning or cancelling the proxy = the onchain bond is free to go back to its owner."""
    from api.escrow import bonds

    bonds.release(proxied_bond(proxy))
    proxy.status = status
    proxy.save(update_fields=["status"])
    return True


def proxy_is_penalized(proxy):
    from api.models import OnchainBond

    bond = proxied_bond(proxy)
    return bond is not None and bond.status == OnchainBond.Status.PENALIZED


def proxy_penalty_sats(proxy):
    """What the penalty of the onchain bond behind this proxy pays the coordinator."""
    from api.escrow import bonds

    if not proxy_is_penalized(proxy):
        return 0
    return bonds.penalty_floor_sats(proxied_bond(proxy))  # 0.9.12: what it pays at least


def to_lightning(order):
    """Case B: the seller took with Lightning. The order goes on as a Lightning order; the
    maker's onchain bond is represented by a proxy LNPayment in order.maker_bond."""
    from api.models import OnchainBond, Order

    bond = bond_of(order, "maker")
    if bond is None or bond.status != OnchainBond.Status.LOCKED:
        raise EscrowError("the maker's onchain bond is not locked")
    proxy = make_proxy(order, "maker", bond)
    order.maker_bond = proxy
    order.escrow_type = Order.EscrowTypes.LIGHTNING
    order.log(
        f"Taken with Lightning by the seller: the order goes on as a **Lightning order**; the maker's "
        f"onchain bond ({bond.value} Sats) is held through proxy LNPayment({proxy.payment_hash})"
    )


def to_onchain(order):
    """Back in the book (publish_order). Case B (the Lightning seller failed): onchain again.
    Mixta D (the onchain seller failed): a Lightning order again, the maker's Lightning bond
    still backs it. Mixta C needs nothing: publish_order drops the taker's proxy bond."""
    from api.models import OnchainEscrow, Order

    if has_proxy(order):
        order.escrow_type = Order.EscrowTypes.ONCHAIN
        order.maker_bond = None
        order.log("Back in the book as an **onchain** order (the maker's onchain bond still backs it)")
    if getattr(order, "maker_lightning", False):
        OnchainEscrow.objects.filter(
            order=order, status__in=[OnchainEscrow.Status.KEYS, OnchainEscrow.Status.FUNDING, OnchainEscrow.Status.ABANDONED]
        ).delete()
        order.escrow_type = Order.EscrowTypes.LIGHTNING
        order.maker_lightning = False
        order.log("Back in the book as a **Lightning** order (the maker's Lightning bond still backs it)")
    order.taker_lightning = False


def onchain_take_enabled():
    """Mixta C/D (26 nov 2026): a Lightning order may be taken with an onchain bond."""
    return service.enabled() and config("ESCROW_ONCHAIN_TAKER", cast=bool, default=True)


def mixta_d_window(order):
    """Mixta D (Kilombino TG 15602): the maker's Lightning hold-invoice bond must outlive the
    onchain flow it ends up backing. (ok, seconds left, seconds needed). Left: its CLTV at the
    fastest block rate expected, minus its age; needed: keys/addresses (WF2) + the onchain
    funding window + the fiat exchange + ESCROW_D_RELEASE_MARGIN (6 h) for the release."""
    from api.logics import BLOCK_TIME, MAX_MINING_NETWORK_SPEEDUP_EXPECTED
    from api.models import Order

    bond = order.maker_bond
    if bond is None or not bond.cltv_expiry or is_bond_proxy(bond):
        return True, None, None
    left = bond.cltv_expiry * BLOCK_TIME * 60 / max(1.0, MAX_MINING_NETWORK_SPEEDUP_EXPECTED)
    left -= (timezone.now() - bond.created_at).total_seconds()
    needed = (
        order.t_to_expire(Order.Status.WF2)
        + config("ESCROW_FUNDING_SECONDS", cast=int, default=3 * 3600)
        + order.t_to_expire(Order.Status.CHA)
        + config("ESCROW_D_RELEASE_MARGIN", cast=int, default=6 * 3600)
    )
    return left >= needed, int(left), int(needed)


def onchain_take_contract(order, take_order):
    """finalize_contract of a Lightning order taken with an onchain bond (now LOCKED).
    Mixta C (the taker is the buyer): the order stays a Lightning order; the taker's onchain
    bond is held through a proxy LNPayment in order.taker_bond. Mixta D (the taker is the
    seller): the order becomes an onchain-escrow order (the seller funds the escrow onchain)
    while the maker keeps its Lightning hold-invoice bond (order.maker_lightning)."""
    from api.logics import Logics
    from api.models import OnchainBond, Order

    bond = OnchainBond.objects.filter(order=order, role="taker", take_order=take_order).order_by("-created_at").first()
    if bond is None or bond.status != OnchainBond.Status.LOCKED:
        raise EscrowError("the taker's onchain bond is not locked")
    if Logics.is_seller(order, take_order.taker):
        order.taker_bond = None
        order.escrow_type = Order.EscrowTypes.ONCHAIN
        order.maker_lightning = True
        order.log(
            f"Taken with an onchain bond by the seller (**mixta D**): the order goes on as an **onchain-escrow** "
            f"order; the maker keeps its Lightning bond ({order.maker_bond.num_satoshis if order.maker_bond else 0} Sats)"
        )
    else:
        order.taker_bond = make_proxy(order, "taker", bond)
        order.log(
            f"Taken with an onchain bond by the buyer (**mixta C**): the order goes on as a Lightning order; the "
            f"taker's onchain bond ({bond.value} Sats) is held through proxy LNPayment({order.taker_bond.payment_hash})"
        )


def hybrid_kind(order):
    """'A', 'B', 'C', 'D' or '' (pure Lightning or pure onchain)."""
    if getattr(order, "maker_lightning", False):
        return "D"
    if is_onchain(order) and order.taker_lightning and order.taker_id is not None:
        return "A"
    if has_proxy(order):
        return "B"
    if has_taker_proxy(order):
        return "C"
    return ""


def penalize_side(order, side):
    """side: 'maker' or 'taker'. Lightning taker bond: settled; onchain bond: penalty broadcast."""
    from api.escrow import bonds
    from api.logics import Logics

    if side == ln_side(order):
        return Logics.settle_bond(order.taker_bond if side == "taker" else order.maker_bond)
    return bonds.penalize(bond_of(order, side))


def free_side(order, side):
    from api.escrow import bonds
    from api.logics import Logics

    if side == ln_side(order):
        return Logics.return_bond(order.taker_bond if side == "taker" else order.maker_bond)
    return bonds.release(bond_of(order, side))


def ln_bond_dispute_credit(order, buyer_wins):
    """Case A/D dispute: the Lightning bond was settled when the dispute opened. Its owner wins:
    its bond back as rewards. Its owner loses: the winner gets the usual reward share."""
    from api.logics import ledger_credit

    side = ln_side(order)
    bond = ln_bond(order)
    if bond is None:
        return
    other = "maker" if side == "taker" else "taker"
    owner_wins = (side_role(order, side) == "buyer") == buyer_wins
    sats = bond.num_satoshis
    if owner_wins:
        ledger_credit(side_user(order, side).robot, sats, "dispute_bond_back",
                      f"dispute_bond_back:{order.id}:{bond.payment_hash}", order_id=order.id)
        order.log(f"Dispute won by the Lightning {side}: its bond of {sats} Sats is credited back as rewards")
    else:
        reward = int(sats * config("SLASHED_BOND_REWARD_SPLIT", cast=float, default=0.5))
        ledger_credit(side_user(order, other).robot, reward, "slash_reward",
                      f"slash_reward:{order.id}:{bond.payment_hash}", order_id=order.id)
        order.log(f"Dispute lost by the Lightning {side}: the {other} is rewarded {reward} Sats of its bond")


def _slash_side(order, side):
    """The bond of 'maker'/'taker' as {user, staked, received, key}: staked = what it put up,
    received = what the coordinator got from it if it was slashed (a settled Lightning bond
    whole; a penalized onchain bond its penalty output, the rest went to the miners)."""
    from api.escrow import bonds
    from api.models import LNPayment, OnchainBond

    if side == ln_side(order):
        b = order.taker_bond if side == "taker" else order.maker_bond
        if b is None:
            return None
        b.refresh_from_db(fields=["status"])
        got = b.num_satoshis if b.status == LNPayment.Status.SETLED else 0
        return {"user": b.sender, "staked": b.num_satoshis, "received": got, "key": b.payment_hash}
    b = bond_of(order, side)
    if b is None:
        return None
    # 0.9.12: what the penalty pays AT LEAST (a cheaper rung of its ladder may still be bumped)
    got = bonds.penalty_floor_sats(b) if b.status == OnchainBond.Status.PENALIZED else 0
    # same key as the hybrid B proxy, so one onchain bond is never rewarded twice
    return {"user": b.owner, "staked": b.value, "received": got, "key": f"{PROXY_PREFIX}{b.pk}"}


def credit_penalty(order, at_fault):
    """rl31-beta22: an onchain (or hybrid A) order ended by a fault before the escrow: the
    trader at fault ('maker'/'taker') lost its bond. Until beta21 nobody was rewarded and the
    proceeds ignored the penalty. Now, as in a Lightning order: the other trader gets
    SLASHED_BOND_REWARD_SPLIT of what the coordinator really got (at most of its own bond,
    as upstream caps it); the coordinator's proceeds are the rest. Nothing goes back to the
    trader at fault. Must run before the order goes back to the book (the taker is cleared)."""
    from api.logics import ledger_credit
    from api.models import Order
    from api.tasks import send_devfund_donation

    innocent = "taker" if at_fault == "maker" else "maker"
    fault, other = _slash_side(order, at_fault), _slash_side(order, innocent)
    if fault is None or fault["received"] <= 0:
        order.log(f"Reward for the {at_fault}'s slashed bond NOT CREDITED: the bond was not penalized", level="WARN")
        return False
    if other is None or other["user"] is None or other["user"].pk == fault["user"].pk:
        order.log(f"Reward for the {at_fault}'s slashed bond NOT CREDITED: no other trader with a bond", level="WARN")
        return False
    split = config("SLASHED_BOND_REWARD_SPLIT", cast=float, default=0.5)
    reward = int(min(fault["received"], other["staked"]) * split)
    robot = other["user"].robot
    if not ledger_credit(robot, reward, "slash_reward", f"slash_reward:{order.id}:{fault['key']}", order_id=order.id):
        order.log(f"Slashed bond {fault['key']} was already rewarded: DUPLICATE CREDIT REFUSED", level="WARN")
        return False
    new_proceeds = int(fault["received"] - reward)
    Order.objects.filter(pk=order.pk).update(proceeds=F("proceeds") + new_proceeds)
    order.refresh_from_db(fields=["proceeds"])
    transaction.on_commit(lambda: send_devfund_donation.delay(order.id, new_proceeds, "slashed bond"))
    order.log(
        f"Robot({robot.id},{other['user'].username}) was rewarded {reward} Sats of the {at_fault}'s "
        f"slashed bond (the coordinator got {fault['received']} Sats from it)"
    )
    return True


def min_bond_sats():
    """The smallest onchain bond (see bonds.min_bond_sats)."""
    from api.escrow import bonds

    return bonds.min_bond_sats()



# Leftovers on a funding address (Roblosats, 16 nov 2026 QA): what a trader sent to its own
# funding address and the deposit never took (order expired or cancelled first, or sent after
# the deposit) is its own, under its own key. The coordinator builds a sweep to an address of
# the owner's; the owner's client checks it and signs it; the coordinator broadcasts it.
SWEEP_DONE_STATUSES = (4, 5, 12, 14, 15, 17, 18)


def funding_leftover(order, escrow, role):
    if escrow is None or not escrow.funding_signatures.get(f"{role}_funding_pubkey"):
        return []
    from api.models import OnchainEscrow

    deposit_pending = escrow.status in (OnchainEscrow.Status.KEYS, OnchainEscrow.Status.FUNDING, OnchainEscrow.Status.MEMPOOL)
    if deposit_pending and order.status not in SWEEP_DONE_STATUSES:
        return []
    utxos = service.funding_utxos(escrow, role)
    if escrow.funding_txid:  # never offer what the deposit spent
        try:
            dep = service.rpc("getrawtransaction", [escrow.funding_txid, True])
            used = {(v["txid"], v["vout"]) for v in dep["vin"]}
            utxos = [u for u in utxos if (u.txid, u.vout) not in used]
        except EscrowError:
            pass
    return utxos


def sweep_cost(n_utxos, rate=None):
    """Mining fee of sending n coins of a funding address back to one (taproot) output."""
    from api.escrow import bonds

    rate = service.fee_rate(2) if rate is None else rate
    return math.ceil(math.ceil(VB_OVERHEAD + n_utxos * VB_TAPROOT_KEY_INPUT + output_vbytes(bonds.P2TR_SPK)) * rate)


def sweep_info(escrow, role):
    """A sent-back funding leftover (rl31-beta20): txid, what arrives, what it spent and whether
    it is confirmed. Sweeps before rl31-beta20 kept only the txid: read the rest from the node."""
    from api.escrow import bonds

    fs = escrow.funding_signatures
    txid = fs[f"sweep_{role}_txid"]
    out = {"escrow_sweep_txid": txid, "escrow_sweep_status": bonds.tx_status(txid)}
    sats, spent = fs.get(f"sweep_{role}_sats"), fs.get(f"sweep_{role}_spent_sats")
    if sats is None:
        try:
            tx = service.rpc("getrawtransaction", [txid, True])
            sats = round(sum(float(v["value"]) for v in tx["vout"]) * 1e8)
        except Exception:
            sats = None
    if sats is not None:
        out["escrow_sweep_sats"] = int(sats)
    if spent is not None:
        out["escrow_sweep_spent_sats"] = int(spent)
    return out


def sweep_tx(order, escrow, role, payout_spk):
    from api.escrow import bonds
    from api.escrow.taproot import Transaction, TxIn, TxOut

    utxos = funding_leftover(order, escrow, role)
    if not utxos:
        raise EscrowError("there is nothing left on your funding address")
    total = sum(u.value for u in utxos)
    vb = math.ceil(VB_OVERHEAD + len(utxos) * VB_TAPROOT_KEY_INPUT + output_vbytes(payout_spk))
    value = total - math.ceil(vb * service.fee_rate(2))
    if value < dust_for(payout_spk) or not bonds.worth_recovering(total, total - value):
        raise EscrowError("not worth it right now: the mining fee would eat more than these sats")
    tx = Transaction([TxIn(u.txid, u.vout) for u in utxos], [TxOut(value, payout_spk)])
    data = dict(escrow.funding_signatures)
    data[f"sweep_{role}"] = {"tx": tx.serialize().hex(), "spent": [[u.value, u.script_pubkey.hex()] for u in utxos]}
    escrow.funding_signatures = data
    escrow.save(update_fields=["funding_signatures"])
    return tx


def submit_sweep(order, escrow, role, sigs_hex):
    from api.escrow.taproot import SIGHASH_ALL_UNIFIED, Transaction, TxOut, schnorr_verify, xonly

    sw = escrow.funding_signatures.get(f"sweep_{role}")
    if not sw:
        raise EscrowError("ask for the sweep first")
    tx = Transaction.parse(bytes.fromhex(sw["tx"]))
    spent = [TxOut(v, bytes.fromhex(spk)) for v, spk in sw["spent"]]
    if len(sigs_hex) != len(tx.inputs):
        raise EscrowError("one signature per input is needed")
    fpk = xonly(bytes.fromhex(escrow.funding_signatures[f"{role}_funding_pubkey"]))
    for i, s in enumerate(sigs_hex):
        sig = bytes.fromhex(s)
        if len(sig) != 65 or sig[64] != SIGHASH_ALL_UNIFIED or not schnorr_verify(tx.sighash_unified(i, spent), fpk, sig[:64]):
            raise EscrowError("a signature does not verify")
        tx.inputs[i].witness = [sig]
    service.rpc("sendrawtransaction", [tx.serialize().hex()])
    data = dict(escrow.funding_signatures)
    data[f"sweep_{role}_txid"] = tx.txid()
    data[f"sweep_{role}_sats"] = sum(o.value for o in tx.outputs)
    data[f"sweep_{role}_spent_sats"] = sum(o.value for o in spent)
    data.pop(f"sweep_{role}", None)
    escrow.funding_signatures = data
    escrow.save(update_fields=["funding_signatures"])
    order.log(f"Onchain funding leftover of the {role} **sent back** in {tx.txid()}")
