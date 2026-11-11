import math
from datetime import timedelta

from decouple import config
from django.utils import timezone

from api.escrow import service
from api.escrow.service import EscrowError
from api.escrow.transactions import (
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


def recommended_funding(order, escrow, role):
    """What each trader should send to its funding address: what it owes plus room for its
    share of the deposit's mining fee at a generous rate."""
    seller_fee, buyer_fee = fees(order)
    owed = order.last_satoshis + seller_fee if role == "seller" else buyer_fee
    rate = max(service.fee_rate(2) * 2, 2.0)
    shared = VB_OVERHEAD + 3 * output_vbytes(b"\x00" * 34)
    vb = VB_TAPROOT_KEY_INPUT + output_vbytes(b"\x00" * 34) + shared / 2
    return owed, owed + math.ceil(vb * rate)


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
        buyer_owed, _ = recommended_funding(order, escrow, "buyer")
        have_s = sum(u.value for u in service.funding_utxos(escrow, "seller"))
        have_b = sum(u.value for u in service.funding_utxos(escrow, "buyer"))
        tb = trade_bonds(order)
        bonds_ready = all(b is not None and b.status == OnchainBond.Status.LOCKED for b in tb.values())
        if bonds_ready and have_s >= seller_owed and (have_b >= buyer_owed or not buyer_fee):
            dev_sats, dev_spk = dev_fee()
            try:
                service.build_deposit(
                    escrow, order.last_satoshis, seller_fee, buyer_fee, coordinator_fee_spk(), dev_sats, dev_spk, tb
                )
                order.log("Onchain deposit **built** (trade amount + both bonds), waiting for the traders' signatures")
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
    plan = escrow.funding_signatures.get("plan", {})
    from api.models import OnchainBond

    values = {"seller": 0, "buyer": 0}
    for role, pk in plan.get("bond_ids", {}).items():
        values[role] = OnchainBond.objects.get(pk=pk).value
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
    payouts = [TxOut(trade + bonds["buyer"], _spk(escrow, "buyer"))]
    if bonds["seller"]:
        payouts.append(TxOut(bonds["seller"], _spk(escrow, "seller")))
    service.start_release(escrow, payouts, OnchainEscrow.ReleaseKind.COOPERATIVE, 0)
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
    order.log(f"Dispute resolved onchain for the {winner}: waiting for its signature on the release")


def proceeds(order):
    escrow = escrow_of(order)
    return escrow.coordinator_fee_sats if escrow else 0


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

    bond_data = {}
    try:
        brole, _ = bond_role(order, user)
        bond_data = bonds.bond_data(bond_of(order, brole, owner=user))
        bond_data["bond_role"] = brole
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
        "escrow_rescue_csv": escrow.rescue_csv,
        "escrow_address": escrow.address,
        "escrow_sats": order.last_satoshis,
        "escrow_my_keys_set": bool(getattr(escrow, f"{role}_pubkey")),
        "escrow_peer_keys_set": bool(getattr(escrow, ("buyer" if role == "seller" else "seller") + "_pubkey")),
        "escrow_my_payout_address": escrow.funding_signatures.get(f"{role}_payout_address", ""),
    }
    fpk = escrow.funding_signatures.get(f"{role}_funding_pubkey")
    if fpk and escrow.address:
        owed, recommended = recommended_funding(order, escrow, role)
        data["escrow_funding_address"] = service.rpc(
            "decodescript", [service.funding_spk(escrow, role).hex()]
        )["address"]
        data["escrow_funding_owed"] = owed
        data["escrow_funding_recommended"] = recommended
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
    return data


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
    from api.escrow import bonds

    seller_is_maker = Logics.is_seller(order, order.maker)
    at_fault_is_maker = (fault == "seller") == seller_is_maker
    loser = bond_of(order, "maker" if at_fault_is_maker else "taker")
    winner = bond_of(order, "taker" if at_fault_is_maker else "maker")
    try:
        bonds.penalize(loser)
    except EscrowError as e:
        order.log(f"Onchain penalty could not be broadcast: {e}", level="ERROR")
    bonds.release(winner)
    order.update_status(Order.Status.EXP)
    order.expiry_reason = Order.ExpiryReasons.NESCRO
    order.save(update_fields=["expiry_reason"])
    order.log(f"Onchain escrow not completed in time by the {fault}: their bond was **penalized**, the other is free to return")
    return True


def cancel_before_escrow(order, canceller):
    """Maker or taker cancels in WF2/WFE: its onchain bond is penalized, the other is freed
    (if the taker cancels, the maker's bond keeps backing the order, which goes back to the book)."""
    from api.escrow import bonds
    from api.logics import Logics
    from api.models import OnchainEscrow, Order

    lost = bond_of(order, canceller)
    bonds.penalize(lost)
    escrow = escrow_of(order)
    if escrow and escrow.status in (OnchainEscrow.Status.KEYS, OnchainEscrow.Status.FUNDING):
        escrow.delete()
    if canceller == "maker":
        bonds.release(bond_of(order, "taker"))
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

    held = (OnchainBond.Status.LOCKED, OnchainBond.Status.IN_DEPOSIT)
    maker = bond_of(order, "maker")
    taker = bond_of(order, "taker")
    return bool(maker and maker.status in held), bool(taker and taker.status in held)
