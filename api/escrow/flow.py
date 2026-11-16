import logging
import math
from datetime import timedelta

from decouple import config
from django.utils import timezone

from api.escrow import service
from api.escrow.service import EscrowError

logger = logging.getLogger(__name__)
from api.escrow.transactions import (
    DUST_P2TR,
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


def _spk_for_address(address):
    info = service.rpc("validateaddress", [address])
    if not info.get("isvalid") or not info.get("iswitness"):
        raise EscrowError("use a valid segwit or taproot address")
    return bytes.fromhex(info["scriptPubKey"])


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
    return escrow


def fees(order):
    return service.fee_split(order)


def recommended_funding(order, escrow, role, from_bond=False):
    """What each trader should send to its funding address: what it owes plus room for its
    share of the deposit's mining fee at a generous rate. from_bond: the buyer's fee comes
    out of its bond instead (no funding input nor change of its own, only its bond input)."""
    seller_fee, buyer_fee = fees(order)
    owed = order.last_satoshis + seller_fee if role == "seller" else buyer_fee
    rate = max(service.fee_rate(2) * 2, 2.0)
    shared = VB_OVERHEAD + 3 * output_vbytes(b"\x00" * 34)
    if from_bond:
        vb = VB_BOND_COOP + shared / 2
    else:
        vb = VB_TAPROOT_KEY_INPUT + output_vbytes(b"\x00" * 34) + shared / 2
    return owed, owed + math.ceil(vb * rate)


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


def dev_fee():
    return 0, None


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
        for take_order in TakeOrder.objects.filter(order=order, expires_at__gt=timezone.now()):
            bond = OnchainBond.objects.filter(order=order, role="taker", take_order=take_order).order_by("-created_at").first()
            if not bond:
                continue
            bond = bonds.refresh(bond)
            if bond.status == OnchainBond.Status.LOCKED:
                order.log("Onchain taker bond **locked**")
                Logics.finalize_contract(take_order)
                break


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
            if role_is_ln(order, role):  # hybrid: the taker's bond is a Lightning hold invoice
                return order.taker_bond is not None and order.taker_bond.status == LNPayment.Status.LOCKED
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
            dev_sats, dev_spk = dev_fee()
            try:
                service.build_deposit(
                    escrow, order.last_satoshis, seller_fee, 0 if at_release else buyer_fee, coordinator_fee_spk(),
                    dev_sats, dev_spk, tb, buyer_fee_from_bond=from_bond,
                )
                if at_release:
                    escrow.refresh_from_db()
                    data = dict(escrow.funding_signatures)
                    data["plan"] = {**data["plan"], "buyer_fee": buyer_fee, "buyer_fee_at_release": buyer_fee}
                    escrow.funding_signatures = data
                    escrow.save(update_fields=["funding_signatures"])
                order.log(
                    "Onchain deposit **built** (trade amount + "
                    + ("the onchain bond" if any(role_is_ln(order, r) for r in service.ROLES) else "both bonds")
                    + (", the buyer's fee out of its bond" if from_bond else "")
                    + (", the buyer's fee out of its payout (Lightning bond)" if at_release else "")
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
    if escrow.status == S.RELEASED and order.status == Order.Status.CHA and escrow.funding_signatures.get("cancel"):
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


def _spk(escrow, role):
    hex_ = escrow.funding_signatures.get(f"{role}_payout_spk")
    if not hex_:
        raise EscrowError(f"the {role} has not given a release address yet")
    return bytes.fromhex(hex_)


def seller_confirms(order):
    """Seller confirmed the fiat: fix the cooperative release (buyer: amount + its bond;
    seller: its bond)."""
    from api.escrow.taproot import TxOut
    from api.models import OnchainEscrow, Order

    escrow = escrow_of(order)
    if escrow is None or escrow.status != OnchainEscrow.Status.LOCKED:
        raise EscrowError("the onchain escrow is not locked")
    bonds, trade = _bond_values(escrow)
    fee_later = int(escrow.funding_signatures.get("plan", {}).get("buyer_fee_at_release") or 0)
    if fee_later < DUST_P2TR:  # too small to be an output of its own: not charged
        fee_later = 0
    payouts = [TxOut(trade + bonds["buyer"] - fee_later, _spk(escrow, "buyer"))]
    if bonds["seller"]:
        payouts.append(TxOut(bonds["seller"], _spk(escrow, "seller")))
    if fee_later:
        payouts.append(TxOut(fee_later, coordinator_fee_spk()))
    service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.COOPERATIVE, 0)
    if fee_later:
        data = dict(escrow.funding_signatures)
        data["release_coordinator_sats"] = fee_later
        escrow.funding_signatures = data
        escrow.save(update_fields=["funding_signatures"])
    order.update_status(Order.Status.PAY)
    order.contract_finalization_time = timezone.now()
    order.save(update_fields=["contract_finalization_time"])
    order.log("Seller confirmed the fiat: **cooperative onchain release** started")


def collaborative_cancel(order):
    """Both asked to cancel after the escrow: seller gets amount + its bond, buyer its bond."""
    from api.escrow.taproot import TxOut
    from api.models import OnchainEscrow

    escrow = escrow_of(order)
    if escrow is None or escrow.status != OnchainEscrow.Status.LOCKED:
        raise EscrowError("the onchain escrow is not locked")
    bonds, trade = _bond_values(escrow)
    payouts = [TxOut(trade + bonds["seller"], _spk(escrow, "seller"))]
    if bonds["buyer"]:
        payouts.append(TxOut(bonds["buyer"], _spk(escrow, "buyer")))
    service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.COOPERATIVE, 0)
    if order.taker_lightning:
        from api.logics import Logics

        Logics.return_bond(order.taker_bond)
        order.log("Lightning taker bond **unlocked** (collaborative cancellation)")
    data = dict(escrow.funding_signatures)
    data["cancel"] = True
    escrow.funding_signatures = data
    escrow.save(update_fields=["funding_signatures"])
    order.log("Collaborative cancellation: **onchain return to the seller** started")


def resolve_dispute(order, buyer_wins):
    """Winner: amount (if buyer) or amount back (if seller) + its own bond + a reward share of
    the loser's bond; the rest of the loser's bond goes to the coordinator."""
    from api.escrow.taproot import TxOut
    from api.models import OnchainEscrow

    escrow = escrow_of(order)
    winner = "buyer" if buyer_wins else "seller"
    loser = "seller" if buyer_wins else "buyer"
    if escrow.status == OnchainEscrow.Status.RELEASING:
        escrow.status = OnchainEscrow.Status.LOCKED
        escrow.save(update_fields=["status"])
    bonds, trade = _bond_values(escrow)
    reward = int(bonds[loser] * config("SLASHED_BOND_REWARD_SPLIT", cast=float, default=0.5))
    payouts = [TxOut(trade + bonds[winner] + reward, _spk(escrow, winner))]
    if bonds[loser] - reward:
        payouts.append(TxOut(bonds[loser] - reward, coordinator_fee_spk()))
    kind = OnchainEscrow.ReleaseKind.BUYER_WINS if buyer_wins else OnchainEscrow.ReleaseKind.SELLER_WINS
    service.start_release(escrow, payouts, kind, 0)
    data = dict(escrow.funding_signatures)
    data["dispute_coordinator_sats"] = bonds[loser] - reward
    escrow.funding_signatures = data
    escrow.save(update_fields=["funding_signatures"])
    ln_bond_dispute_credit(order, buyer_wins)
    order.log(f"Dispute resolved onchain for the {winner}: waiting for its signature on the release")


def proceeds(order):
    escrow = escrow_of(order)
    if escrow is None:
        return 0
    return escrow.coordinator_fee_sats + int(escrow.funding_signatures.get("release_coordinator_sats") or 0)


def bond_role(order, user):
    from api.models import TakeOrder

    if order.maker == user:
        return "maker", None
    take_order = TakeOrder.objects.filter(order=order, taker=user).order_by("-expires_at").first()
    if order.taker == user or take_order:
        return "taker", take_order
    raise EscrowError("you are not in this order")


def bond_action(order, user, name, data):
    """Onchain bond actions (views: action=bond_*): key, penalty signature, return."""
    from api.escrow import bonds

    role, take_order = bond_role(order, user)
    if name == "bond_key":
        bonds.open_bond(order, user, role, data["bond_pubkey"], take_order if role == "taker" else None)
        return
    bond = bond_of(order, role, owner=user)
    if bond is None:
        raise EscrowError("send your bond key first")
    if name == "bond_penalty_signature":
        bonds.submit_penalty_signature(bond, data["signature"])
    elif name == "bond_return":
        bonds.return_tx(bond, _spk_for_address(data["address"]))
    elif name == "bond_return_signature":
        bonds.submit_return_signature(bond, data["signature"])
    else:
        raise EscrowError("unknown bond action")


def action(order, user, name, data):
    """Trader actions on the onchain escrow (views: action=escrow_*)."""
    if name.startswith("bond_"):
        return bond_action(order, user, name, data)
    role = service.role_of(order, user)
    escrow = escrow_of(order)
    if escrow is None:
        raise EscrowError("this order has no onchain escrow")
    if name == "escrow_keys":
        service.submit_keys(order, user, data["escrow_pubkey"], data["funding_pubkey"])
    elif name == "escrow_address":
        set_payout_address(order, user, data["address"])
    elif name == "escrow_funding_signatures":
        service.submit_funding_signatures(escrow, role, data["signatures"], data.get("bond_signature"))
    elif name == "escrow_nonce":
        service.submit_release_nonce(escrow, role, data["nonce"])
    elif name == "escrow_partial":
        service.submit_release_partial(escrow, role, data["partial"])
        escrow.refresh_from_db()
        advance(order)
    elif name == "escrow_leaf_signature":
        service.submit_leaf_signature(escrow, role, data["signature"])
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
            pending = _unconfirmed(my_bond.address)
            if pending:
                bond_data["bond_mempool_txid"] = pending[0][0]
                bond_data["bond_mempool_sats"] = sum(v for _, v in pending)
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
    }
    fpk = escrow.funding_signatures.get(f"{role}_funding_pubkey")
    at_release = role == "buyer" and bool(fees(order)[1]) and buyer_fee_at_release(order)
    from_bond = role == "buyer" and bool(fees(order)[1]) and not at_release and buyer_fee_from_bond(order, escrow)
    if at_release:
        # Hybrid A: Lightning bond, nothing to send onchain; the fee comes out of the payout.
        data["escrow_fee_at_release"] = True
        data["escrow_fee_at_release_sats"] = fees(order)[1]
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
            pending = _unconfirmed(data["escrow_funding_address"])
            if pending:
                data["escrow_funding_mempool_txid"] = pending[0][0]
                data["escrow_funding_mempool_sats"] = sum(v for _, v in pending)
    who, what = waiting_for(order, escrow, role)
    data["escrow_waiting_for"] = who or ""
    data["escrow_waiting_what"] = what or ""
    if escrow.funding_tx and escrow.status == escrow.Status.FUNDING:
        tx, spent, plan = service.stored_deposit(escrow)
        data["escrow_deposit_tx"] = escrow.funding_tx
        data["escrow_deposit_spent"] = plan["spent"]
        data["escrow_deposit_my_inputs"] = plan[f"{role}_inputs"]
        data["escrow_deposit_my_bond_input"] = plan.get("bond_inputs", {}).get(role)
        data["escrow_deposit_signed"] = bool(escrow.funding_signatures.get(f"{role}_sigs"))
    if escrow.funding_txid:
        data["escrow_funding_txid"] = escrow.funding_txid
    if escrow.status == escrow.Status.RELEASING:
        data["escrow_release_tx"] = escrow.release_tx
        data["escrow_release_kind"] = escrow.get_release_kind_display()
        data["escrow_release_fee_rate"] = escrow.release_fee_rate
        data["escrow_release_my_nonce"] = escrow.release_nonces.get(role, "")
        data["escrow_release_aggnonce"] = service.aggregate_nonce(escrow) or ""
        data["escrow_release_my_partial"] = bool(escrow.release_partial_sigs.get(role))
    if escrow.release_txid:
        data["escrow_release_txid"] = escrow.release_txid
        summary = trade_summary(order, user)
        if summary:
            data["onchain_summary"] = summary
    return data


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
        for txid, sats in _unconfirmed(bond.address):
            seen.append({"what": "bond", "role": bond.role, "owner": bond.owner_id, "txid": txid, "sats": sats})
    escrow = escrow or escrow_of(order)
    if escrow is not None and escrow.status == OnchainEscrow.Status.KEYS and escrow.address:
        for role in service.ROLES:
            if not escrow.funding_signatures.get(f"{role}_funding_pubkey"):
                continue
            user = order.maker if role_user_is_maker(order, role) else order.taker
            for txid, sats in _unconfirmed(_funding_address(escrow, role)):
                seen.append({"what": "funding", "role": role, "owner": user.id if user else None, "txid": txid, "sats": sats})
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
            send_notification.delay(
                order_id=order.id, message=f"onchain_mempool:{item['owner']}:{item['what']}:{item['sats']}"
            )


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
            plan.get("bond_inputs", {}).get(r) is None or data.get(f"{r}_bond_sig"))
        if not signed(role):
            return "you", "deposit_signature"
        if not signed(peer):
            return "peer", "deposit_signature"
        return "coordinator", "deposit"
    if escrow.status == S.MEMPOOL:
        return "confirmation", "deposit"
    if escrow.status == S.RELEASING:
        if escrow.release_kind != escrow.ReleaseKind.COOPERATIVE:
            return "coordinator", "release"
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
    seller_fee, buyer_fee = (plan["seller_fee"], plan["buyer_fee"]) if "seller_fee" in plan else fees(order)
    tx = Transaction.parse(bytes.fromhex(escrow.release_tx))
    my_spk = escrow.funding_signatures.get(f"{role}_payout_spk", "")
    received = sum(o.value for o in tx.outputs if o.script_pubkey.hex() == my_spk)
    release_mining = escrow.escrow_sats - sum(o.value for o in tx.outputs)
    # payouts[0] paid the release's mining fee: the buyer when released, the seller when returned
    payer = "buyer" if escrow.status == escrow.Status.RELEASED and not escrow.funding_signatures.get("cancel") else "seller"
    from api.models import OnchainBond

    bond_ids = plan.get("bond_ids", {})
    bond_sats = OnchainBond.objects.get(pk=bond_ids[role]).value if role in bond_ids else 0
    ln_bond = role_is_ln(order, role)
    if ln_bond and order.taker_bond is not None:
        bond_sats = order.taker_bond.num_satoshis
    return {
        "lightning_bond": ln_bond,
        "fee_at_release": role == "buyer" and bool(escrow.funding_signatures.get("release_coordinator_sats")),
        "role": role,
        "is_buyer": role == "buyer",
        "trade_sats": trade,
        "received_sats": received,
        "bond_sats": bond_sats,
        "bond_back_sats": bond_sats if ln_bond else bonds.get(role, 0),
        "coordinator_fee_sats": seller_fee if role == "seller" else buyer_fee,
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
        if not escrow.funding_signatures.get("seller_sigs"):
            return "seller"
        return "buyer"
    seller_fee, buyer_fee = fees(order)
    seller_owed, _ = recommended_funding(order, escrow, "seller")
    if sum(u.value for u in service.funding_utxos(escrow, "seller", 0)) < seller_owed:
        return "seller"
    return "buyer"


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
    order.update_status(Order.Status.EXP)
    order.expiry_reason = Order.ExpiryReasons.NESCRO
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


def bonds_locked(order):
    from api.models import OnchainBond

    from api.models import LNPayment

    held = (OnchainBond.Status.LOCKED, OnchainBond.Status.IN_DEPOSIT)
    maker = bond_of(order, "maker")
    taker = bond_of(order, "taker")
    if order.taker_lightning:
        return bool(maker and maker.status in held), bool(
            order.taker_bond and order.taker_bond.status == LNPayment.Status.LOCKED
        )
    return bool(maker and maker.status in held), bool(taker and taker.status in held)


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
        rate = order.currency.exchange_rate
        sizes = [Logics.calc_sats(order.min_amount, rate, order.premium), Logics.calc_sats(order.max_amount, rate, order.premium)]
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


def role_is_ln(order, role):
    """True when the trader 'role' (seller/buyer) of this onchain order has a Lightning bond."""
    return bool(order.taker_lightning) and order.taker_id is not None and taker_role(order) == role


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
    return is_bond_proxy(order.maker_bond)


def proxy_settle(proxy):
    """Settling the proxy = penalizing the onchain bond. True only the first time."""
    from api.escrow import bonds
    from api.models import LNPayment

    proxy.refresh_from_db(fields=["status"])
    if proxy.status == LNPayment.Status.SETLED:
        return False
    bond = proxied_bond(proxy)
    try:
        bonds.penalize(bond)
    except EscrowError as e:
        if bond is not None:
            bond.order.log(f"Onchain maker bond penalty could not be broadcast: {e}", level="ERROR")
        return False
    proxy.status = LNPayment.Status.SETLED
    proxy.save(update_fields=["status"])
    return True


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


def to_lightning(order):
    """Case B: the seller took with Lightning. The order goes on as a Lightning order; the
    maker's onchain bond is represented by a proxy LNPayment in order.maker_bond."""
    from django.contrib.auth.models import User

    from api.models import LNPayment, OnchainBond, Order

    bond = bond_of(order, "maker")
    if bond is None or bond.status != OnchainBond.Status.LOCKED:
        raise EscrowError("the maker's onchain bond is not locked")
    proxy, _ = LNPayment.objects.update_or_create(
        payment_hash=f"{PROXY_PREFIX}{bond.pk}",
        defaults=dict(
            concept=LNPayment.Concepts.MAKEBOND,
            type=LNPayment.Types.HOLD,
            status=LNPayment.Status.LOCKED,
            sender=order.maker,
            receiver=User.objects.filter(username=config("ESCROW_USERNAME", default="admin")).first(),
            num_satoshis=bond.value,
            description=f"Onchain maker bond of order {order.id} (proxy)",
            created_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30),
        ),
    )
    order.maker_bond = proxy
    order.escrow_type = Order.EscrowTypes.LIGHTNING
    order.log(
        f"Taken with Lightning by the seller: the order goes on as a **Lightning order**; the maker's "
        f"onchain bond ({bond.value} Sats) is held through proxy LNPayment({proxy.payment_hash})"
    )


def to_onchain(order):
    """Case B order back in the book (the Lightning seller failed): onchain again."""
    from api.models import Order

    if has_proxy(order):
        order.escrow_type = Order.EscrowTypes.ONCHAIN
        order.maker_bond = None
        order.log("Back in the book as an **onchain** order (the maker's onchain bond still backs it)")
    order.taker_lightning = False


def penalize_side(order, side):
    """side: 'maker' or 'taker'. Lightning taker bond: settled; onchain bond: penalty broadcast."""
    from api.escrow import bonds
    from api.logics import Logics

    if side == "taker" and order.taker_lightning:
        return Logics.settle_bond(order.taker_bond)
    return bonds.penalize(bond_of(order, side))


def free_side(order, side):
    from api.escrow import bonds
    from api.logics import Logics

    if side == "taker" and order.taker_lightning:
        return Logics.return_bond(order.taker_bond)
    return bonds.release(bond_of(order, side))


def ln_bond_dispute_credit(order, buyer_wins):
    """Case A dispute: the Lightning taker bond was settled when the dispute opened. Winner
    taker: its bond back as rewards. Loser taker: the winner gets the usual reward share."""
    from api.logics import ledger_credit

    if not order.taker_lightning or order.taker_bond is None:
        return
    taker_wins = (taker_role(order) == "buyer") == buyer_wins
    sats = order.taker_bond.num_satoshis
    if taker_wins:
        ledger_credit(order.taker.robot, sats, "dispute_bond_back",
                      f"dispute_bond_back:{order.id}:{order.taker_bond.payment_hash}", order_id=order.id)
        order.log(f"Dispute won by the Lightning taker: its bond of {sats} Sats is credited back as rewards")
    else:
        reward = int(sats * config("SLASHED_BOND_REWARD_SPLIT", cast=float, default=0.5))
        ledger_credit(order.maker.robot, reward, "slash_reward",
                      f"slash_reward:{order.id}:{order.taker_bond.payment_hash}", order_id=order.id)
        order.log(f"Dispute lost by the Lightning taker: the maker is rewarded {reward} Sats of its bond")
