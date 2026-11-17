import math

from django.db import transaction
from django.utils import timezone

from api.escrow import service
from api.escrow.service import EscrowError
from api.escrow.taproot import (
    SIGHASH_ALL_UNIFIED,
    BondContract,
    Transaction,
    TxOut,
    derive_secret,
    leaf_witness,
    pubkey,
    schnorr_sign,
    schnorr_verify,
    xonly,
)
from api.escrow.transactions import DUST_P2TR, BondInput, Utxo, coins_spend_tx

# A bond can be paid in up to this many payments (rl31-beta18: a short first payment is topped
# up with a second one). Each extra coin costs its owner one more bond input in the deposit.
MAX_BOND_COINS = 3


def coordinator_bond_key(bond_order_id, role, owner_pubkey_hex):
    """Per bond: order, role and the owner's bond key (stable for the bond's whole life)."""
    secret = service.config("ESCROW_SECRET", default="").encode()
    if not secret:
        raise EscrowError("onchain escrow is not configured")
    return derive_secret(secret, service.coordinator_alias(), bond_order_id, f"bond-{role}-{owner_pubkey_hex}")


def contract_of(bond):
    return BondContract(
        bytes.fromhex(bond.owner_pubkey),
        bytes.fromhex(bond.coordinator_pubkey),
        bond.refund_csv,
        hrp=service.network_hrp(),
    )


def coins_of(bond):
    """The coins of the bond itself [(txid, vout, sats)]: one, or several when it was topped up."""
    if bond is None:
        return []
    if bond.coins:
        return [(c[0], int(c[1]), int(c[2])) for c in bond.coins]
    if bond.txid:
        return [(bond.txid, bond.vout, bond.value)]
    return []


def _set_coins(bond, coins):
    bond.coins = [list(c) for c in coins]
    bond.txid, bond.vout = coins[0][0], coins[0][1]
    bond.value = sum(c[2] for c in coins)


def bond_input(bond):
    spk = bytes.fromhex(bond.script_pubkey)
    utxos = [Utxo(t, v, sats, spk) for t, v, sats in coins_of(bond)]
    return BondInput(utxos[0], contract_of(bond), utxos[1:])


def confirmed_coins(address, min_conf=1):
    found = service.rpc("listunspent", [min_conf, 9999999, [address]], wallet=service.watch_wallet())
    return sorted((u["txid"], u["vout"], round(u["amount"] * 1e8)) for u in found)


def choose_coins(coins, required):
    """Which confirmed coins at the bond address make the bond: the smallest single coin that
    reaches it (so a short payment followed by a full one locks with the full one and leaves
    the short one free), else the fewest largest coins that together reach it (a top-up), at
    most MAX_BOND_COINS. None while they do not reach it."""
    singles = [c for c in coins if c[2] >= required]
    if singles:
        return [min(singles, key=lambda c: (c[2], c[0], c[1]))]
    picked = []
    for c in sorted(coins, key=lambda c: (-c[2], c[0], c[1]))[:MAX_BOND_COINS]:
        picked.append(c)
        if sum(x[2] for x in picked) >= required:
            return picked
    return None


def _in_flight(bond):
    """Outpoints already spent by a leftover return of ours (maybe still in the mempool)."""
    out = set()
    for item in bond.leftover_txids or []:
        out.update((c[0], int(c[1])) for c in item.get("coins", []))
    return out


def leftovers(bond, confirmed=None):
    """Coins at the bond address that are not the bond (a short payment that was not needed,
    an extra payment, a payment after the bond was spent): they are the owner's. Not while the
    bond is still waiting (they may still make it up) nor when the whole address goes back in
    the bond's return."""
    from api.models import OnchainBond

    S = OnchainBond.Status
    if bond is None or bond.status in (S.FUNDING, S.ABANDONED, S.RETURNABLE):
        return []
    if confirmed is None:
        confirmed = confirmed_coins(bond.address)
    skip = {(c[0], c[1]) for c in coins_of(bond)} | _in_flight(bond)
    skip |= {(c[0], int(c[1])) for c in (bond.return_coins or [])}
    return [c for c in confirmed if (c[0], c[1]) not in skip]


def min_bond_sats():
    """The smallest onchain bond the coordinator can still take as a penalty: the penalty
    spend's mining fee at ESCROW_PENALTY_FEE_RATE plus a dust-free output, with room for a
    pricier mempool (16 nov 2026, QA: a 3 % bond of a small order, 1,811 sats, was accepted,
    paid and then stuck, because the penalty needed 1,830). ESCROW_MIN_BOND_SATS overrides."""
    from api.escrow.flow import coordinator_fee_spk
    from api.escrow.transactions import VB_BOND_COOP, VB_OVERHEAD, output_vbytes

    floor = service.config("ESCROW_MIN_BOND_SATS", cast=int, default=0)
    if floor:
        return floor
    rate = service.config("ESCROW_PENALTY_FEE_RATE", cast=float, default=10.0)
    try:
        spk = coordinator_fee_spk()
    except Exception:
        spk = b"\x00" * 34
    vb = math.ceil(VB_OVERHEAD + VB_BOND_COOP + output_vbytes(spk))
    return 2 * math.ceil(vb * rate) + DUST_P2TR


def required_sats(order, take_order=None):
    sats = take_order.last_satoshis if take_order is not None and take_order.last_satoshis else order.last_satoshis
    return max(int(sats * order.bond_size / 100), min_bond_sats())


def open_bond(order, user, role, owner_pubkey_hex, take_order=None):
    """The owner gives its bond key (derived from its token); the bond address is fixed."""
    from api.logics import Logics
    from api.models import OnchainBond

    owner_pk = service._pub(owner_pubkey_hex)
    existing = OnchainBond.objects.filter(order=order, owner=user, role=role, take_order=take_order).exclude(
        status__in=[OnchainBond.Status.ABANDONED, OnchainBond.Status.RETURNED, OnchainBond.Status.RETURNABLE]
    ).first()
    if existing:
        if existing.owner_pubkey != owner_pk.hex():
            raise EscrowError("this bond already has another key")
        return existing
    ckey = coordinator_bond_key(order.id, role, owner_pk.hex())
    csv = max(144, Logics.compute_cltv_expiry_blocks(order, f"{role}_bond"))
    c = BondContract(owner_pk, pubkey(ckey), min(csv, 0xFFFE), hrp=service.network_hrp())
    bond = OnchainBond.objects.create(
        order=order,
        take_order=take_order,
        owner=user,
        role=role,
        owner_pubkey=owner_pk.hex(),
        coordinator_pubkey=pubkey(ckey).hex(),
        refund_csv=c.refund_csv,
        address=c.address,
        script_pubkey=c.script_pubkey.hex(),
        required_sats=required_sats(order, take_order),
    )
    desc = service.rpc("getdescriptorinfo", [f"addr({c.address})"])["descriptor"]
    service.rpc(
        "importdescriptors",
        [[{"desc": desc, "timestamp": int(timezone.now().timestamp()) - 7200, "label": f"roblosats-{order.id}-bond-{role}"}]],
        wallet=service.watch_wallet(),
    )
    order.log(f"Onchain {role} bond address {c.address} for {bond.required_sats} Sats")
    return bond


def refresh(bond, min_conf=1):
    """FUNDING → SIGN once a big enough output is confirmed; the penalty is then fixed."""
    from api.models import OnchainBond

    if bond.status != OnchainBond.Status.FUNDING:
        return bond
    coins = choose_coins(confirmed_coins(bond.address, min_conf), bond.required_sats)
    if not coins:
        return bond
    _set_coins(bond, coins)
    from api.escrow.flow import coordinator_fee_spk
    from api.escrow.transactions import coins_spend_vbytes

    rate = service.config("ESCROW_PENALTY_FEE_RATE", cast=float, default=10.0)
    try:
        tx = coins_spend_tx(coins, coordinator_fee_spk(), rate)
    except ValueError:
        # A bond paid before the floor existed (or a mempool rate change): a cheaper penalty
        # beats an order stuck forever on a confirmed bond.
        vb = coins_spend_vbytes(len(coins), coordinator_fee_spk())
        tx = coins_spend_tx(coins, coordinator_fee_spk(), max(1.0, (bond.value - DUST_P2TR) // vb))
    bond.penalty_tx = tx.serialize().hex()
    bond.status = OnchainBond.Status.SIGN
    bond.save()
    return bond


def _coin_messages(bond, tx, coins):
    """Cooperative-leaf sighashes of a transaction spending exactly these coins of the bond
    address, input by input (coins in the order of the inputs)."""
    c = contract_of(bond)
    by_outpoint = {(t, v): sats for t, v, sats in coins}
    try:
        spent = [TxOut(by_outpoint[(i.txid, i.vout)], c.script_pubkey) for i in tx.inputs]
    except KeyError:
        raise EscrowError("the transaction spends a coin that is not at this bond address")
    leaf = c.leaf_hash(c.COOPERATIVE)
    return [tx.sighash_unified(i, spent, SIGHASH_ALL_UNIFIED, leaf) for i in range(len(tx.inputs))]


def _as_list(sigs):
    if isinstance(sigs, str):
        return [sigs]
    return list(sigs or [])


def _check_owner_sigs(bond, msgs, sigs, what):
    if len(sigs) != len(msgs):
        raise EscrowError(f"the {what} needs one signature per coin ({len(msgs)})")
    owner = xonly(bytes.fromhex(bond.owner_pubkey))
    for m, s in zip(msgs, sigs):
        sig = bytes.fromhex(s)
        if len(sig) != 65 or sig[64] != SIGHASH_ALL_UNIFIED or not schnorr_verify(m, owner, sig[:64]):
            raise EscrowError(f"the {what} signature does not verify")


def _cosign(bond, tx, msgs, sigs):
    c = contract_of(bond)
    key = _coordinator_key_of(bond)
    for i, (m, s) in enumerate(zip(msgs, sigs)):
        tx.inputs[i].witness = leaf_witness(c, c.COOPERATIVE, [schnorr_sign(key, m), bytes.fromhex(s)[:64]])


def penalty_message(bond):
    """The penalty and its sighash(es): one per coin of the bond."""
    tx = Transaction.parse(bytes.fromhex(bond.penalty_tx))
    msgs = _coin_messages(bond, tx, coins_of(bond))
    return tx, msgs


def submit_penalty_signature(bond, sigs):
    """The owner's signature(s) of the penalty, given in advance: the bond is then LOCKED."""
    from api.models import OnchainBond

    sigs = _as_list(sigs)
    with transaction.atomic():
        bond = OnchainBond.objects.select_for_update().get(pk=bond.pk)
        if bond.status != OnchainBond.Status.SIGN:
            raise EscrowError("the bond is not waiting for the penalty signature")
        _, msgs = penalty_message(bond)
        _check_owner_sigs(bond, msgs, sigs, "penalty")
        bond.penalty_owner_sig = sigs[0]
        bond.penalty_owner_sigs = sigs
        bond.status = OnchainBond.Status.LOCKED
        bond.save()
    return bond


def _coordinator_key_of(bond):
    key = coordinator_bond_key(bond.order_id, bond.role, bond.owner_pubkey)
    if pubkey(key).hex() != bond.coordinator_pubkey:
        raise EscrowError("the coordinator key of this bond does not match ESCROW_SECRET")
    return key


def penalize(bond):
    """Broadcast the penalty the owner signed in advance, with the coordinator's signature."""
    from api.models import OnchainBond

    if bond is None or bond.status != OnchainBond.Status.LOCKED:
        return False
    tx, msgs = penalty_message(bond)
    _cosign(bond, tx, msgs, bond.penalty_owner_sigs or [bond.penalty_owner_sig])
    service.rpc("sendrawtransaction", [tx.serialize().hex()])
    bond.spend_txid = tx.txid()
    bond.status = OnchainBond.Status.PENALIZED
    bond.save(update_fields=["spend_txid", "status", "updated_at"])
    bond.order.log(f"Onchain {bond.role} bond **penalized** in {bond.spend_txid}")
    return True


def release(bond):
    """The bond is no longer at stake: its owner may take it back (with the coordinator's
    signature now, or alone after the timelock)."""
    from api.models import OnchainBond

    if bond is None:
        return False
    if bond.status == OnchainBond.Status.LOCKED:
        bond.status = OnchainBond.Status.RETURNABLE
        bond.save(update_fields=["status", "updated_at"])
        return True
    if bond.status in (OnchainBond.Status.FUNDING, OnchainBond.Status.SIGN):
        bond.status = OnchainBond.Status.RETURNABLE if bond.txid else OnchainBond.Status.ABANDONED
        bond.save(update_fields=["status", "updated_at"])
        return True
    return False


def return_tx(bond, payout_spk):
    from api.models import OnchainBond

    if bond.status != OnchainBond.Status.RETURNABLE or not bond.txid:
        raise EscrowError("this bond cannot be returned now")
    # The bond and anything else confirmed at its address go back together (rl31-beta18).
    mine = coins_of(bond)
    known = {(c[0], c[1]) for c in mine} | _in_flight(bond)
    try:
        extra = [c for c in confirmed_coins(bond.address) if (c[0], c[1]) not in known]
    except EscrowError:
        extra = []
    coins = mine + extra
    try:
        tx = coins_spend_tx(coins, payout_spk, service.fee_rate(2))
    except ValueError as e:
        raise EscrowError(str(e))
    bond.return_tx = tx.serialize().hex()
    bond.return_coins = [list(c) for c in coins]
    bond.save(update_fields=["return_tx", "return_coins", "updated_at"])
    return tx


def return_coins_of(bond):
    if bond.return_coins:
        return [(c[0], int(c[1]), int(c[2])) for c in bond.return_coins]
    return coins_of(bond)


def submit_return_signature(bond, sigs):
    """The owner signs the return the server built; the coordinator adds its own and broadcasts."""
    from api.models import OnchainBond

    if bond.status != OnchainBond.Status.RETURNABLE or not bond.return_tx:
        raise EscrowError("ask for the return first")
    tx = Transaction.parse(bytes.fromhex(bond.return_tx))
    msgs = _coin_messages(bond, tx, return_coins_of(bond))
    sigs = _as_list(sigs)
    _check_owner_sigs(bond, msgs, sigs, "return")
    _cosign(bond, tx, msgs, sigs)
    service.rpc("sendrawtransaction", [tx.serialize().hex()])
    bond.spend_txid = tx.txid()
    bond.status = OnchainBond.Status.RETURNED
    bond.save(update_fields=["spend_txid", "status", "updated_at"])
    return bond


def leftover_return_tx(bond, payout_spk):
    """Coins at the bond address that are not the bond go back to an address of the owner's,
    with the coordinator's signature, at any time (the bond itself stays where it is)."""
    coins = leftovers(bond)
    if not coins:
        raise EscrowError("there is nothing else at your bond address")
    try:
        tx = coins_spend_tx(coins, payout_spk, service.fee_rate(2))
    except ValueError:
        raise EscrowError("what is left at your bond address is too small to send back")
    bond.leftover_tx = tx.serialize().hex()
    bond.leftover_coins = [list(c) for c in coins]
    bond.save(update_fields=["leftover_tx", "leftover_coins", "updated_at"])
    return tx


def submit_leftover_signatures(bond, sigs):
    from api.models import OnchainBond

    with transaction.atomic():
        bond = OnchainBond.objects.select_for_update().get(pk=bond.pk)
        if not bond.leftover_tx:
            raise EscrowError("ask for the return of the leftover first")
        coins = [(c[0], int(c[1]), int(c[2])) for c in bond.leftover_coins]
        still = {(c[0], c[1]) for c in leftovers(bond)}
        if any((c[0], c[1]) not in still for c in coins):
            bond.leftover_tx, bond.leftover_coins = "", []
            bond.save(update_fields=["leftover_tx", "leftover_coins", "updated_at"])
            raise EscrowError("the coins at your bond address changed: ask again")
        tx = Transaction.parse(bytes.fromhex(bond.leftover_tx))
        msgs = _coin_messages(bond, tx, coins)
        sigs = _as_list(sigs)
        _check_owner_sigs(bond, msgs, sigs, "leftover return")
        _cosign(bond, tx, msgs, sigs)
        service.rpc("sendrawtransaction", [tx.serialize().hex()])
        bond.leftover_txids = list(bond.leftover_txids or []) + [
            {"txid": tx.txid(), "coins": [list(c) for c in coins], "sats": tx.outputs[0].value}
        ]
        bond.leftover_tx, bond.leftover_coins = "", []
        bond.save(update_fields=["leftover_txids", "leftover_tx", "leftover_coins", "updated_at"])
    bond.order.log(
        f"Onchain {bond.role} bond address: {sum(c[2] for c in coins)} Sats that were not the bond "
        f"**sent back** to its owner in {tx.txid()}"
    )
    return bond


def coordinator_deposit_signature(bond, tx, spent, index):
    c = contract_of(bond)
    msg = tx.sighash_unified(index, spent, SIGHASH_ALL_UNIFIED, c.leaf_hash(c.COOPERATIVE))
    return schnorr_sign(_coordinator_key_of(bond), msg)


def bond_data(bond):
    if bond is None:
        return {}
    data = {
        "bond_onchain": True,
        "bond_status": bond.get_status_display(),
        "bond_status_code": bond.status,
        "bond_address": bond.address,
        "bond_required_sats": bond.required_sats,
        "bond_refund_csv": bond.refund_csv,
        "bond_coordinator_pubkey": bond.coordinator_pubkey,
        "bond_hrp": service.network_hrp(),
        "escrow_alias": service.coordinator_alias(),
    }
    coins = coins_of(bond)
    if bond.txid:
        data.update({"bond_txid": bond.txid, "bond_vout": bond.vout, "bond_value": bond.value})
        data["bond_coins"] = [{"txid": t, "vout": v, "value": s} for t, v, s in coins]
    if bond.status == bond.Status.SIGN:
        data["bond_penalty_tx"] = bond.penalty_tx
    if bond.return_tx and bond.status == bond.Status.RETURNABLE:
        data["bond_return_tx"] = bond.return_tx
        data["bond_return_coins"] = [{"txid": t, "vout": v, "value": s} for t, v, s in return_coins_of(bond)]
    if bond.spend_txid:
        data["bond_spend_txid"] = bond.spend_txid
    # rl31-beta18: top-ups and coins at the bond address that are not the bond
    try:
        confirmed = confirmed_coins(bond.address)
    except Exception:
        confirmed = None
    if confirmed is not None:
        if bond.status == bond.Status.FUNDING:
            data["bond_confirmed_sats"] = sum(c[2] for c in confirmed)
            data["bond_max_coins"] = MAX_BOND_COINS
        elif bond.status == bond.Status.RETURNABLE:
            mine = {(c[0], c[1]) for c in coins} | _in_flight(bond)
            extra = sum(c[2] for c in confirmed if (c[0], c[1]) not in mine)
            if extra:
                data["bond_extra_sats"] = extra
        lo = leftovers(bond, confirmed)
        if lo:
            data["bond_leftovers"] = [{"txid": t, "vout": v, "value": s} for t, v, s in lo]
            data["bond_leftover_sats"] = sum(c[2] for c in lo)
            if bond.leftover_tx:
                data["bond_leftover_tx"] = bond.leftover_tx
                data["bond_leftover_coins"] = [
                    {"txid": c[0], "vout": int(c[1]), "value": int(c[2])} for c in bond.leftover_coins
                ]
    if bond.leftover_txids:
        data["bond_leftover_txid"] = bond.leftover_txids[-1]["txid"]
        data["bond_leftover_returned_sats"] = sum(int(i.get("sats", 0)) for i in bond.leftover_txids)
    return data


def recover_abandoned(days=30):
    """A bond paid after its order was gone (expired or cancelled while it was still unpaid)
    is never taken: it becomes free for its owner to take back with the coordinator's signature
    (16 nov 2026, QA: a late maker bond stayed "Never funded" with no way back but the
    timelock). One listunspent for all of them."""
    from datetime import timedelta

    from api.models import OnchainBond

    since = timezone.now() - timedelta(days=days)
    bonds = list(OnchainBond.objects.filter(status=OnchainBond.Status.ABANDONED, created_at__gte=since))
    if not bonds:
        return 0
    found = service.rpc("listunspent", [1, 9999999, [b.address for b in bonds]], wallet=service.watch_wallet())
    by_addr = {}
    for u in found:
        by_addr.setdefault(u["address"], []).append(u)
    n = 0
    for b in bonds:
        us = by_addr.get(b.address)
        if not us:
            continue
        # every coin there (a short payment, a top-up) goes back with the return
        _set_coins(b, sorted((u["txid"], u["vout"], round(u["amount"] * 1e8)) for u in us))
        b.status = OnchainBond.Status.RETURNABLE
        b.save(update_fields=["txid", "vout", "value", "coins", "status", "updated_at"])
        b.order.log(f"Onchain {b.role} bond paid after the order was gone: **free to return** to its owner")
        n += 1
    return n
