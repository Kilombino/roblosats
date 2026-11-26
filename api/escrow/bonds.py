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
from api.escrow.transactions import DUST_P2TR, BondInput, Utxo, coins_spend_tx, coins_spend_vbytes, dust_for

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


def penalty_fee_rate(value, n_coins, payout_spk):
    """Fee rate (sat/vB) of a new penalty, fixed when the bond locks (rl31-beta22).

    Until beta21 every penalty paid ESCROW_PENALTY_FEE_RATE (10 sat/vB): 1,500 of the 3,330
    sats of a minimum bond went to the miners (order #23). Now that rate is a ceiling and the
    fee is also held to ESCROW_PENALTY_MAX_FEE_PCT of the bond (10 %), never below
    ESCROW_PENALTY_MIN_FEE_RATE (1 sat/vB, above the node's relay minimum). A penalty that
    turns out too cheap when it is broadcast is still the coordinator's to speed up: its only
    output is the coordinator's, so it can be CPFP'd from there."""
    from api.escrow.transactions import coins_spend_vbytes

    top = service.config("ESCROW_PENALTY_FEE_RATE", cast=float, default=10.0)
    pct = service.config("ESCROW_PENALTY_MAX_FEE_PCT", cast=float, default=10.0)
    floor = service.config("ESCROW_PENALTY_MIN_FEE_RATE", cast=float, default=1.0)
    vb = coins_spend_vbytes(max(1, n_coins), payout_spk)
    cap_fee = math.floor(value * pct / 100)
    capped = math.floor(cap_fee * 100 / vb) / 100  # 2 decimals
    if math.ceil(vb * capped) > cap_fee:  # float rounding: never a sat above the cap
        capped = round(capped - 0.01, 2)
    return min(top, max(floor, capped))


def penalty_sats(bond):
    """What the bond's penalty pays the coordinator (its only output), 0 without a penalty.
    0.9.12: the rung that was broadcast (the latest one after a bump), else the top penalty."""
    if bond is None or not bond.penalty_tx:
        return 0
    tx_hex = bond.penalty_tx
    lad = getattr(bond, "penalty_ladder", None) or {}
    sent = lad.get("sent") or {}
    try:
        if sent and sent.get("txid") == bond.spend_txid:
            tx_hex = lad["rungs"][int(sent["index"])]["tx"]
    except (KeyError, IndexError, ValueError, TypeError):
        tx_hex = bond.penalty_tx
    try:
        tx = Transaction.parse(bytes.fromhex(tx_hex))
    except Exception:
        return 0
    return int(sum(o.value for o in tx.outputs))


def penalty_floor_sats(bond):
    """0.9.12: what the coordinator gets from the bond's penalty AT LEAST: the top penalty's
    output (a cheaper rung may still be replaced by it). Rewards and proceeds are counted on it,
    as in 0.9.11, so a bump never leaves a reward larger than what the coordinator got."""
    if bond is None or not bond.penalty_tx:
        return 0
    try:
        tx = Transaction.parse(bytes.fromhex(bond.penalty_tx))
    except Exception:
        return 0
    return int(sum(o.value for o in tx.outputs))


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
    # 0.9.12: a regtest lab may shorten the timelock to test what happens near it (never on a
    # real network: the owner could then take its bond back before the trade ends)
    lab_csv = service.config("ESCROW_LAB_BOND_CSV", cast=int, default=0)
    if lab_csv > 0 and service.config("NETWORK", default="mainnet") == "regtest":
        csv = lab_csv
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

    rate = penalty_fee_rate(sum(c[2] for c in coins), len(coins), coordinator_fee_spk())
    try:
        tx = coins_spend_tx(coins, coordinator_fee_spk(), rate)
    except ValueError:
        # A bond paid before the floor existed (or a mempool rate change): a cheaper penalty
        # beats an order stuck forever on a confirmed bond.
        vb = coins_spend_vbytes(len(coins), coordinator_fee_spk())
        tx = coins_spend_tx(coins, coordinator_fee_spk(), max(1.0, (bond.value - DUST_P2TR) // vb))
    bond.penalty_tx = tx.serialize().hex()
    bond.status = OnchainBond.Status.SIGN
    bond.penalty_ladder = {}
    bond.save()
    _build_penalty_ladder(bond)  # 0.9.12: the cheaper penalties, signed with the top one
    _build_auto_return(bond)  # 0.9.7: signed together with the penalty
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


def submit_penalty_signature(bond, sigs, ladder_sigs=None):
    """The owner's signature(s) of the penalty, given in advance: the bond is then LOCKED.
    0.9.12: ladder_sigs, one list per rung of the penalty's fee ladder (optional: an app that
    signs only the top penalty keeps the 0.9.11 behaviour)."""
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
        if ladder_sigs:
            _check_penalty_ladder(bond, sigs, ladder_sigs)
        bond.status = OnchainBond.Status.LOCKED
        bond.save()
    return bond


# 0.9.12: the penalty's fee LADDER (PENALTY_LADDER_PLAN 1). Until 0.9.11 the owner signed one
# penalty at ESCROW_PENALTY_FEE_RATE (a ceiling, 10 sat/vB, held to 10 % of the bond): 1,500 of a
# 30,000-sat bond went to the miners (#29) while the mempool took 1 sat/vB. Now the owner also
# signs the same penalty at lower rates (ESCROW_PENALTY_RATES, default "1,3", below the top
# rate); the coordinator broadcasts the cheapest one the node expects to confirm within
# ESCROW_PENALTY_TARGET_BLOCKS and, if it is still unconfirmed after ESCROW_PENALTY_BUMP_SECONDS
# (or the estimate rose above it), replaces it with the next one (same inputs, RBF). The top rung
# is penalty_tx, so an app that signs only it (older apps) keeps the old behaviour. The rungs pay
# the coordinator's output only (as the penalty does), so the owner gives nothing more.
PENALTY_LADDER_PLAN = 1


def penalty_rates(top):
    """The lower rungs of the penalty: ESCROW_PENALTY_RATES below the top rate, ascending."""
    raw = str(service.config("ESCROW_PENALTY_RATES", default="1,3"))
    rates = set()
    for part in raw.split(","):
        try:
            r = round(float(part.strip()), 2)
        except ValueError:
            continue
        if 1.0 <= r < top - 1e-9:
            rates.add(r)
    return sorted(rates)


def _tx_rate(tx, coins):
    """Fee rate (sat/vB, 2 decimals) of a transaction spending these bond coins to one output."""
    from api.escrow.transactions import coins_spend_vbytes

    fee = sum(c[2] for c in coins) - sum(o.value for o in tx.outputs)
    return round(fee / coins_spend_vbytes(len(coins), tx.outputs[0].script_pubkey), 2)


def _build_penalty_ladder(bond):
    """The cheaper penalties to sign, once the penalty is fixed (SIGN, or LOCKED for a bond
    locked by an older coordinator or app: a new app may still sign them)."""
    from api.models import OnchainBond

    if bond.status not in (OnchainBond.Status.SIGN, OnchainBond.Status.LOCKED) or not bond.penalty_tx:
        return bond
    if (bond.penalty_ladder or {}).get("rungs"):
        return bond
    coins = coins_of(bond)
    if not coins:
        return bond
    top = Transaction.parse(bytes.fromhex(bond.penalty_tx))
    if len(top.outputs) != 1:
        return bond
    spk = top.outputs[0].script_pubkey  # the penalty's own output: the same coordinator address
    top_rate = _tx_rate(top, coins)
    rungs = []
    for r in penalty_rates(top_rate):
        try:
            rungs.append({"rate": r, "tx": coins_spend_tx(coins, spk, r).serialize().hex()})
        except ValueError:
            continue
    if not rungs:
        return bond
    rungs.append({"rate": top_rate, "tx": bond.penalty_tx})
    bond.penalty_ladder = {"plan": PENALTY_LADDER_PLAN, "rungs": rungs}
    bond.save(update_fields=["penalty_ladder", "updated_at"])
    return bond


def _check_penalty_ladder(bond, sigs, ladder_sigs):
    """Checks and stores the owner's signatures of the penalty's rungs (bond already locked
    for update by the caller). Each rung spends exactly the penalty's coins to the penalty's
    only output; the top rung is penalty_tx, signed by sigs."""
    lad = dict(bond.penalty_ladder or {})
    rungs = lad.get("rungs") or []
    if not rungs:
        raise EscrowError("this bond has no penalty ladder to sign")
    top = Transaction.parse(bytes.fromhex(bond.penalty_tx))
    got = []
    for i, rung in enumerate(rungs):
        rs = _as_list(ladder_sigs[i]) if i < len(ladder_sigs) and ladder_sigs[i] else None
        if rung["tx"] == bond.penalty_tx:
            rs = sigs
        elif rs:
            tx = Transaction.parse(bytes.fromhex(rung["tx"]))
            if len(tx.outputs) != 1 or tx.outputs[0].script_pubkey != top.outputs[0].script_pubkey:
                raise EscrowError("a penalty rung does not pay the penalty's address")
            _check_owner_sigs(bond, _coin_messages(bond, tx, coins_of(bond)), rs, "penalty")
        got.append(rs)
    lad["sigs"] = got
    bond.penalty_ladder = lad


def submit_penalty_ladder_signatures(bond, ladder_sigs):
    """0.9.12: a bond locked before its app signed the ladder (an older coordinator or app):
    the rungs can be signed later, while the bond is still locked."""
    from api.models import OnchainBond

    with transaction.atomic():
        bond = OnchainBond.objects.select_for_update().get(pk=bond.pk)
        if bond.status != OnchainBond.Status.LOCKED:
            raise EscrowError("the bond is not locked")
        if (bond.penalty_ladder or {}).get("sigs"):
            return bond
        _build_penalty_ladder(bond)
        _check_penalty_ladder(bond, bond.penalty_owner_sigs or [bond.penalty_owner_sig], ladder_sigs)
        bond.save(update_fields=["penalty_ladder", "updated_at"])
    return bond


def signed_penalty_rungs(bond):
    """[(index, rate, tx_hex, sigs)] of the penalties the owner signed, ascending by rate. The
    top one (penalty_tx) is always there, signed by penalty_owner_sigs."""
    lad = bond.penalty_ladder or {}
    top_sigs = bond.penalty_owner_sigs or ([bond.penalty_owner_sig] if bond.penalty_owner_sig else [])
    rungs = lad.get("rungs") or []
    lsigs = lad.get("sigs") or []
    out = []
    for i, rung in enumerate(rungs):
        if rung.get("tx") == bond.penalty_tx:
            continue
        sigs = lsigs[i] if i < len(lsigs) else None
        if sigs:
            out.append((i, float(rung.get("rate") or 0), rung["tx"], sigs))
    top_rate = next((float(r.get("rate") or 0) for r in rungs if r.get("tx") == bond.penalty_tx), None)
    if top_rate is None:
        try:
            top_rate = _tx_rate(Transaction.parse(bytes.fromhex(bond.penalty_tx)), coins_of(bond))
        except Exception:
            top_rate = 0.0
    top_index = next((i for i, r in enumerate(rungs) if r.get("tx") == bond.penalty_tx), len(rungs))
    if top_sigs:
        out.append((top_index, top_rate, bond.penalty_tx, top_sigs))
    return out


def penalty_target_rate():
    """The rate the node expects to confirm within ESCROW_PENALTY_TARGET_BLOCKS."""
    try:
        return service.fee_rate(service.config("ESCROW_PENALTY_TARGET_BLOCKS", cast=int, default=6))
    except Exception:
        return service.config("ESCROW_FALLBACK_FEE_RATE", cast=float, default=5.0)


def _send_penalty_rung(bond, rung):
    i, rate, tx_hex, sigs = rung
    tx = Transaction.parse(bytes.fromhex(tx_hex))
    msgs = _coin_messages(bond, tx, coins_of(bond))
    _cosign(bond, tx, msgs, sigs)
    service.rpc("sendrawtransaction", [tx.serialize().hex()])
    return tx


# 0.9.7: automatic return of a free bond (AUTO_RETURN_PLAN 1). The owner gives a return address
# (at make/take, or its payout address later) and signs, when it signs the penalty, the return
# of exactly the bond's coins to that address at the penalty's fee-rate rule. The coordinator
# broadcasts it only from release(), i.e. once the bond can no longer be penalized nor go into a
# deposit. Anything else (no address, an old app, coins that changed, a node that refuses it)
# falls back to the manual "Get my bond back".
AUTO_RETURN_PLAN = 2
# 0.9.11 (plan 2): a fee LADDER. Plan 1 signed one return at the penalty's rate (up to 10 sat/vB),
# so with the mempool at 1 sat/vB a bond paid ten times the fee it needed (a user paid 1,530
# sats). Plan 2 also pre-signs the same return at lower rates (ESCROW_AUTO_RETURN_RATES, default
# "1,3", below the penalty's rate); the coordinator broadcasts the cheapest one the node expects
# to confirm within ESCROW_AUTO_RETURN_TARGET_BLOCKS and, if it is still unconfirmed after
# ESCROW_AUTO_RETURN_BUMP_SECONDS (or the estimate rose above it), replaces it with the next one
# (same inputs, RBF). The top rung is plan 1's transaction, so an app that signs only it (older
# apps) keeps plan 1's behaviour. Bonds signed under plan 1 keep their single return.


def set_return_address(bond, payout_spk, address):
    """The owner's return address. Replaces an earlier one only while nothing is signed."""
    from api.models import OnchainBond

    with transaction.atomic():
        bond = OnchainBond.objects.select_for_update().get(pk=bond.pk)
        if bond.status not in (OnchainBond.Status.FUNDING, OnchainBond.Status.SIGN, OnchainBond.Status.LOCKED):
            raise EscrowError("the return address can only be set while the bond is held")
        ar = dict(bond.auto_return or {})
        if ar.get("sigs"):
            if ar.get("spk") == payout_spk.hex():
                return bond
            raise EscrowError("the automatic return is already signed to another address")
        ar.update({"plan": AUTO_RETURN_PLAN, "address": address, "spk": payout_spk.hex()})
        ar.pop("tx", None)
        bond.auto_return = ar
        bond.save(update_fields=["auto_return", "updated_at"])
        _build_auto_return(bond)
    return bond


def _build_auto_return(bond):
    """The return to sign, once the bond's coins are fixed (SIGN or LOCKED) and an address set."""
    from api.models import OnchainBond

    ar = dict(bond.auto_return or {})
    if not ar.get("spk") or ar.get("tx") or bond.status not in (OnchainBond.Status.SIGN, OnchainBond.Status.LOCKED):
        return bond
    coins = coins_of(bond)
    if not coins:
        return bond
    spk = bytes.fromhex(ar["spk"])
    from api.escrow.transactions import coins_spend_vbytes

    rate = penalty_fee_rate(sum(c[2] for c in coins), len(coins), spk)
    try:
        tx = coins_spend_tx(coins, spk, rate)
    except ValueError:
        vb = coins_spend_vbytes(len(coins), spk)
        try:
            tx = coins_spend_tx(coins, spk, max(1.0, (bond.value - dust_for(spk)) // vb))
        except ValueError:
            return bond  # too small to return automatically: manual
    ar.update({"tx": tx.serialize().hex(), "rate": rate, "coins": [list(c) for c in coins]})
    if int(ar.get("plan") or 1) >= 2:
        ar["ladder"] = _ladder(coins, spk, rate, tx)
    bond.auto_return = ar
    bond.save(update_fields=["auto_return", "updated_at"])
    return bond


def ladder_rates(top):
    """The lower rungs: ESCROW_AUTO_RETURN_RATES below the top rate, ascending, no repeats."""
    raw = str(service.config("ESCROW_AUTO_RETURN_RATES", default="1,3"))
    rates = set()
    for part in raw.split(","):
        try:
            r = round(float(part.strip()), 2)
        except ValueError:
            continue
        if 1.0 <= r < top:
            rates.add(r)
    return sorted(rates)


def _ladder(coins, spk, top_rate, top_tx):
    """[{rate, tx}] ascending, the last one the top (plan 1's) transaction."""
    rungs = []
    for r in ladder_rates(top_rate):
        try:
            rungs.append({"rate": r, "tx": coins_spend_tx(coins, spk, r).serialize().hex()})
        except ValueError:
            continue
    rungs.append({"rate": top_rate, "tx": top_tx.serialize().hex()})
    return rungs


def _rung_message(bond, tx_hex):
    ar = bond.auto_return or {}
    tx = Transaction.parse(bytes.fromhex(tx_hex))
    return tx, _coin_messages(bond, tx, [tuple(c) for c in ar.get("coins") or coins_of(bond)])


def signed_rungs(bond):
    """[(index, rate, tx_hex, sigs)] of the rungs the owner signed, ascending by rate. The top
    rung (plan 1's transaction) is signed by ar["sigs"]."""
    ar = bond.auto_return or {}
    ladder = ar.get("ladder") or [{"rate": ar.get("rate"), "tx": ar.get("tx")}]
    lsigs = ar.get("ladder_sigs") or []
    out = []
    for i, rung in enumerate(ladder):
        sigs = ar.get("sigs") if rung.get("tx") == ar.get("tx") else (lsigs[i] if i < len(lsigs) else None)
        if sigs and rung.get("tx"):
            out.append((i, float(rung.get("rate") or 0), rung["tx"], sigs))
    return out


def auto_return_message(bond):
    ar = bond.auto_return or {}
    tx = Transaction.parse(bytes.fromhex(ar["tx"]))
    return tx, _coin_messages(bond, tx, [tuple(c) for c in ar.get("coins") or coins_of(bond)])


def submit_auto_return_signature(bond, sigs, ladder_sigs=None):
    """The owner's signature(s) of its automatic return (one per coin); 0.9.11: ladder_sigs, one
    list per rung of the fee ladder (the top rung is the same transaction as sigs)."""
    from api.models import OnchainBond

    sigs = _as_list(sigs)
    with transaction.atomic():
        bond = OnchainBond.objects.select_for_update().get(pk=bond.pk)
        ar = dict(bond.auto_return or {})
        if not ar.get("tx"):
            raise EscrowError("there is no automatic return to sign")
        if bond.status not in (OnchainBond.Status.SIGN, OnchainBond.Status.LOCKED):
            raise EscrowError("the bond is not held any more")
        tx, msgs = auto_return_message(bond)
        # it pays the owner's address, whole, and nothing else
        if len(tx.outputs) != 1 or tx.outputs[0].script_pubkey.hex() != ar.get("spk"):
            raise EscrowError("the automatic return does not pay the return address")
        _check_owner_sigs(bond, msgs, sigs, "automatic return")
        ar["sigs"] = sigs
        # 0.9.11 (plan 2): the cheaper rungs, each checked like the top one
        if ladder_sigs and ar.get("ladder"):
            got = []
            for i, rung in enumerate(ar["ladder"]):
                rs = _as_list(ladder_sigs[i]) if i < len(ladder_sigs) and ladder_sigs[i] else None
                if rung["tx"] == ar["tx"]:
                    rs = sigs
                elif rs:
                    rtx, rmsgs = _rung_message(bond, rung["tx"])
                    if len(rtx.outputs) != 1 or rtx.outputs[0].script_pubkey.hex() != ar.get("spk"):
                        raise EscrowError("the automatic return does not pay the return address")
                    _check_owner_sigs(bond, rmsgs, rs, "automatic return")
                got.append(rs)
            ar["ladder_sigs"] = got
        bond.auto_return = ar
        bond.save(update_fields=["auto_return", "updated_at"])
    return bond


def target_rate():
    """The rate the node expects to confirm within ESCROW_AUTO_RETURN_TARGET_BLOCKS."""
    try:
        return service.fee_rate(service.config("ESCROW_AUTO_RETURN_TARGET_BLOCKS", cast=int, default=6))
    except Exception:  # a node that does not answer: the fallback, as fee_rate does
        return service.config("ESCROW_FALLBACK_FEE_RATE", cast=float, default=5.0)


def choose_rung(rungs, rate):
    """The cheapest signed rung at or above the expected rate, else the most expensive one."""
    for rung in rungs:
        if rung[1] + 1e-9 >= rate:
            return rung
    return rungs[-1] if rungs else None


def _send_rung(bond, rung):
    i, rate, tx_hex, sigs = rung
    tx, msgs = _rung_message(bond, tx_hex)
    ar = bond.auto_return or {}
    if len(tx.outputs) != 1 or tx.outputs[0].script_pubkey.hex() != ar.get("spk"):
        raise EscrowError("the signed return does not pay the return address")
    _cosign(bond, tx, msgs, sigs)
    service.rpc("sendrawtransaction", [tx.serialize().hex()])
    return tx


def _broadcast_auto_return(bond):
    """From release() only: the bond is free. True when the signed return went out. 0.9.11: the
    cheapest signed rung the mempool needs; if the node refuses it, the next one up."""
    import time

    from api.models import OnchainBond

    ar = bond.auto_return or {}
    if bond.status != OnchainBond.Status.RETURNABLE or not ar.get("sigs") or not ar.get("tx"):
        return False
    try:
        if [list(c) for c in coins_of(bond)] != [list(c) for c in ar.get("coins") or []]:
            raise EscrowError("the bond's coins changed since the return was signed")
        rungs = signed_rungs(bond)
        first = choose_rung(rungs, target_rate()) if len(rungs) > 1 else rungs[0]
        tx, sent, error = None, None, None
        for rung in [r for r in rungs if r[1] >= first[1]]:
            try:
                tx = _send_rung(bond, rung)
                sent = rung
                break
            except Exception as e:  # too cheap for the node now: the next rung
                error = e
        if tx is None:
            raise error or EscrowError("no signed return")
    except Exception as e:
        bond.order.log(f"Onchain {bond.role} bond: automatic return not sent ({e}); the owner takes it back by hand", level="WARN")
        _alert_bond_manual(bond, e)
        return False
    ar = dict(bond.auto_return)
    ar["sent"] = {"index": sent[0], "rate": sent[1], "txid": tx.txid(), "at": int(time.time())}
    bond.auto_return = ar
    bond.spend_txid = tx.txid()
    bond.status = OnchainBond.Status.RETURNED
    bond.save(update_fields=["auto_return", "spend_txid", "status", "updated_at"])
    bond.order.log(f"Onchain {bond.role} bond **returned automatically** to {ar.get('address')} in {bond.spend_txid}"
                   f" ({sent[1]:g} sat/vB)")
    return True


def bump_auto_return(bond, now=None):
    """0.9.11: an automatic return still unconfirmed after ESCROW_AUTO_RETURN_BUMP_SECONDS, or
    below what the node now expects, is replaced by the next signed rung (RBF). True if bumped."""
    import time

    from api.models import OnchainBond

    ar = dict(bond.auto_return or {})
    sent = ar.get("sent")
    if bond.status != OnchainBond.Status.RETURNED or not sent or ar.get("confirmed"):
        return False
    status = tx_status(sent["txid"])
    if status == "confirmed":
        ar["confirmed"] = True
        bond.auto_return = ar
        bond.save(update_fields=["auto_return", "updated_at"])
        return False
    now = int(now if now is not None else time.time())
    waited = now - int(sent.get("at") or now)
    later = [r for r in signed_rungs(bond) if r[1] > float(sent["rate"])]
    if not later:
        return False
    stale = waited >= service.config("ESCROW_AUTO_RETURN_BUMP_SECONDS", cast=int, default=7200)
    if not stale and target_rate() <= float(sent["rate"]) + 1e-6:  # float noise of the estimate
        return False
    nxt = later[0]
    try:
        tx = _send_rung(bond, nxt)
    except Exception as e:
        bond.order.log(f"Onchain {bond.role} bond: automatic return not bumped ({e})", level="WARN")
        return False
    ar["sent"] = {"index": nxt[0], "rate": nxt[1], "txid": tx.txid(), "at": now, "replaced": sent["txid"]}
    bond.auto_return = ar
    bond.spend_txid = tx.txid()
    bond.save(update_fields=["auto_return", "spend_txid", "updated_at"])
    bond.order.log(f"Onchain {bond.role} bond: automatic return **bumped** to {nxt[1]:g} sat/vB in {bond.spend_txid}")
    return True


def bump_auto_returns():
    """From the follow_invoices loop: every automatic return that is still in the mempool."""
    from api.models import OnchainBond

    n = 0
    for bond in OnchainBond.objects.filter(status=OnchainBond.Status.RETURNED, auto_return__has_key="sent"):
        try:
            n += bool(bump_auto_return(bond))
        except Exception:
            continue
    return n


def _alert_bond_manual(bond, error):
    """The coordinator's Telegram alert (roblosats_admin), when it is installed and wants it."""
    try:
        from django.apps import apps

        if apps.is_installed("roblosats_admin"):
            from roblosats_admin import alerts

            alerts.queue("bond_manual", bond.order_id, {"role": bond.role, "reason": str(error)[:200]})
    except Exception:  # noqa: BLE001 (an alert never stops a release)
        pass


def _coordinator_key_of(bond):
    key = coordinator_bond_key(bond.order_id, bond.role, bond.owner_pubkey)
    if pubkey(key).hex() != bond.coordinator_pubkey:
        raise EscrowError("the coordinator key of this bond does not match ESCROW_SECRET")
    return key


def penalize(bond, reason=""):
    """Broadcast the penalty the owner signed in advance, with the coordinator's signature.
    0.9.12: the cheapest signed rung the mempool needs; if the node refuses it, the next one up
    (the top one is the penalty of 0.9.11). Raises EscrowError when none goes out."""
    import time

    from api.models import OnchainBond

    if bond is None or bond.status != OnchainBond.Status.LOCKED:
        return False
    rungs = signed_penalty_rungs(bond)
    if not rungs:
        raise EscrowError("the bond's penalty is not signed")
    first = choose_rung(rungs, penalty_target_rate()) if len(rungs) > 1 else rungs[0]
    tx, sent, error = None, None, None
    for rung in [r for r in rungs if r[1] >= first[1]]:
        try:
            tx = _send_penalty_rung(bond, rung)
            sent = rung
            break
        except Exception as e:  # too cheap for the node now: the next rung
            error = e
    if tx is None:
        raise error if isinstance(error, EscrowError) else EscrowError(str(error or "no signed penalty"))
    lad = dict(bond.penalty_ladder or {})
    lad["sent"] = {"index": sent[0], "rate": sent[1], "txid": tx.txid(), "at": int(time.time())}
    bond.penalty_ladder = lad
    bond.spend_txid = tx.txid()
    bond.status = OnchainBond.Status.PENALIZED
    bond.save(update_fields=["penalty_ladder", "spend_txid", "status", "updated_at"])
    bond.order.log(f"Onchain {bond.role} bond **penalized** in {bond.spend_txid} ({sent[1]:g} sat/vB)"
                   + (f": {reason}" if reason else ""))
    return True


def bump_penalty(bond, now=None):
    """0.9.12: a penalty still unconfirmed after ESCROW_PENALTY_BUMP_SECONDS, or below what the
    node now expects, is replaced by the next signed rung (RBF). True if bumped."""
    import time

    from api.models import OnchainBond

    lad = dict(bond.penalty_ladder or {})
    sent = lad.get("sent")
    if bond.status != OnchainBond.Status.PENALIZED or not sent or lad.get("confirmed"):
        return False
    if tx_status(sent["txid"]) == "confirmed":
        lad["confirmed"] = True
        bond.penalty_ladder = lad
        bond.save(update_fields=["penalty_ladder", "updated_at"])
        return False
    now = int(now if now is not None else time.time())
    waited = now - int(sent.get("at") or now)
    later = [r for r in signed_penalty_rungs(bond) if r[1] > float(sent["rate"])]
    if not later:
        return False
    stale = waited >= service.config("ESCROW_PENALTY_BUMP_SECONDS", cast=int, default=7200)
    if not stale and penalty_target_rate() <= float(sent["rate"]) + 1e-6:
        return False
    nxt = later[0]
    try:
        tx = _send_penalty_rung(bond, nxt)
    except Exception as e:
        bond.order.log(f"Onchain {bond.role} bond: penalty not bumped ({e})", level="WARN")
        return False
    lad["sent"] = {"index": nxt[0], "rate": nxt[1], "txid": tx.txid(), "at": now, "replaced": sent["txid"]}
    bond.penalty_ladder = lad
    bond.spend_txid = tx.txid()
    bond.save(update_fields=["penalty_ladder", "spend_txid", "updated_at"])
    bond.order.log(f"Onchain {bond.role} bond: penalty **bumped** to {nxt[1]:g} sat/vB in {bond.spend_txid}")
    return True


def bump_penalties():
    """From the follow_invoices loop: every penalty of the ladder that is still in the mempool."""
    from api.models import OnchainBond

    n = 0
    for bond in OnchainBond.objects.filter(status=OnchainBond.Status.PENALIZED, penalty_ladder__has_key="sent"):
        try:
            n += bool(bump_penalty(bond))
        except Exception:
            continue
    return n


# 0.9.12: an onchain bond is no longer penalized when a dispute opens (a Lightning hold invoice
# must be settled then, or its HTLC may force-close the channel; a bond on chain can wait). It
# stays locked until the dispute is decided: penalized if its owner loses, freed (and returned by
# the signed return) if it wins. Its owner can take it back alone once the bond's timelock
# (refund_csv, counted from the bond's confirmation) is over, so a dispute that runs that long
# would let a loser walk away with it: ESCROW_DISPUTE_BOND_SAFETY_BLOCKS (144) before that, the
# coordinator penalizes it anyway; a winner then gets its value back as a reward.
def bond_coins_state(bond):
    """(unspent, confirmations): whether every coin of the bond is still unspent (also in the
    mempool) and the confirmations of its OLDEST coin (the first one its owner could take back
    alone). (False, None) once any coin is spent; (True, 0) while unconfirmed."""
    oldest = 0
    for txid, vout, _sats in coins_of(bond):
        out = service.rpc("gettxout", [txid, int(vout), True])
        if not out:
            return False, None
        oldest = max(oldest, int(out.get("confirmations") or 0))
    return True, oldest


def blocks_to_refund(bond):
    """Blocks left before the owner can take the bond back alone (0 = it can now), or None when
    the bond is no longer where it was."""
    unspent, conf = bond_coins_state(bond)
    if not unspent:
        return None
    return max(0, int(bond.refund_csv) - int(conf))


def safety_blocks(bond=None):
    """Blocks before the timelock at which a disputed bond is penalized anyway: the setting, but
    never more than a quarter of the bond's own timelock (a short one would otherwise be
    penalized the moment its dispute opens), and at least 6."""
    blocks = service.config("ESCROW_DISPUTE_BOND_SAFETY_BLOCKS", cast=int, default=144)
    if bond is not None and bond.refund_csv:
        blocks = min(blocks, max(6, int(bond.refund_csv) // 4))
    return blocks


def taken_back(bond, why):
    """The bond's coins were spent by somebody else (its owner, alone, after the timelock):
    nothing is left to penalize or to return."""
    from api.models import OnchainBond

    bond.status = OnchainBond.Status.RETURNED
    bond.save(update_fields=["status", "updated_at"])
    bond.order.log(f"Onchain {bond.role} bond is **no longer at its address** ({why}): its owner took it "
                   "back alone after the timelock; nothing to penalize or return", level="WARN")
    _alert_safety(bond, "taken_back")
    return bond


def _alert_safety(bond, what, left=None):
    try:
        from django.apps import apps

        if apps.is_installed("roblosats_admin"):
            from roblosats_admin import alerts

            alerts.queue("onchain", bond.order_id, {
                "kind": f"bond_{what}", "role": bond.role, "sats": int(bond.value or 0),
                "height": "" if left is None else int(left), "txid": bond.spend_txid or "",
            })
    except Exception:  # noqa: BLE001 (an alert never stops the guard)
        pass


_guard = {"at": 0.0}


def guard_disputed_bonds(force=False):
    """From the follow_invoices loop (at most once a minute): every onchain bond still locked in
    a disputed order whose timelock is close is penalized now (see above); one already spent by
    its owner is marked as taken back. Returns the number of bonds acted on."""
    import time

    from api.logics import order_lock
    from api.models import OnchainBond, Order

    now = time.monotonic()
    if not force and now - _guard["at"] < 60:
        return 0
    _guard["at"] = now
    n = 0
    ids = list(OnchainBond.objects.filter(
        status=OnchainBond.Status.LOCKED, order__is_disputed=True,
        order__status__in=[Order.Status.DIS, Order.Status.WFR],
    ).values_list("pk", "order_id"))
    for pk, order_id in ids:
        try:
            with order_lock(order_id):
                bond = OnchainBond.objects.select_related("order").get(pk=pk)
                if bond.status != OnchainBond.Status.LOCKED or bond.order.status not in (
                    Order.Status.DIS, Order.Status.WFR
                ):
                    continue
                n += bool(guard_bond(bond))
        except Exception as e:  # the node down: next pass
            import logging

            logging.getLogger(__name__).warning(f"dispute bond guard, bond {pk}: {e}")
    return n


def guard_bond(bond):
    """One locked bond of an open dispute: penalized when fewer than safety_blocks() remain."""
    left = blocks_to_refund(bond)
    if left is None:
        taken_back(bond, "spent while the dispute was open")
        return True
    if left >= safety_blocks(bond):
        return False
    reason = (f"the dispute is still open and only {left} blocks remain before its owner can take it "
              "back alone (safety)")
    penalize_now(bond, reason)
    _alert_safety(bond, "safety_penalty", left)
    return True


def penalize_now(bond, reason=""):
    """Penalizes a locked bond through the order's own path (the hybrid proxy, so its LNPayment
    is settled too). If the penalty cannot go out because the coins are gone, the bond is marked
    as taken back. True when the penalty went out."""
    from api.escrow import flow

    order = bond.order
    ln = order.maker_bond if bond.role == "maker" else order.taker_bond  # mixta C: the taker's proxy
    proxy = ln if flow.is_bond_proxy(ln) else None
    try:
        if proxy is not None and flow.proxied_bond(proxy) and flow.proxied_bond(proxy).pk == bond.pk:
            ok = flow.proxy_settle(proxy, reason=reason, raise_errors=True)
        else:
            ok = penalize(bond, reason)
        return bool(ok)
    except EscrowError as e:
        bond.refresh_from_db()
        try:
            unspent, _ = bond_coins_state(bond)
        except Exception:
            unspent = True
        if not unspent:
            taken_back(bond, f"the penalty was refused: {e}")
        else:
            order.log(f"Onchain {bond.role} bond penalty could not be broadcast: {e}", level="ERROR")
        return False


def release(bond):
    """The bond is no longer at stake: its owner may take it back (with the coordinator's
    signature now, or alone after the timelock)."""
    from api.models import OnchainBond

    if bond is None:
        return False
    if bond.status == OnchainBond.Status.LOCKED:
        bond.status = OnchainBond.Status.RETURNABLE
        bond.save(update_fields=["status", "updated_at"])
        _broadcast_auto_return(bond)  # 0.9.7: the return the owner signed in advance
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


# rl31-beta20: a leftover is only worth sending back when what arrives is at least the dust limit
# and at least what the miners take (331 sats at ~4.7 sat/vB cost ~530 in mining: not worth it).
P2TR_SPK = b"\x51\x20" + b"\x00" * 32


def worth_recovering(sats, fee):
    return sats - fee >= max(DUST_P2TR, fee)


def leftover_cost(n_coins, rate=None, refund=False):
    """Mining fee of sending n coins of a bond address back to one (taproot) output."""
    rate = service.fee_rate(2) if rate is None else rate
    return math.ceil(coins_spend_vbytes(n_coins, P2TR_SPK, refund) * rate)


def tx_status(txid):
    """'confirmed' once a transaction is in a block, 'pending' while not (or unknown)."""
    try:
        tx = service.rpc("getrawtransaction", [txid, True])
        return "confirmed" if int(tx.get("confirmations") or 0) > 0 else "pending"
    except Exception:
        return "pending"


def leftover_return_tx(bond, payout_spk):
    """Coins at the bond address that are not the bond go back to an address of the owner's,
    with the coordinator's signature, at any time (the bond itself stays where it is)."""
    coins = leftovers(bond)
    if not coins:
        raise EscrowError("there is nothing else at your bond address")
    rate = service.fee_rate(2)
    total = sum(c[2] for c in coins)
    if not worth_recovering(total, leftover_cost(len(coins), rate)):
        raise EscrowError("not worth it right now: the mining fee would eat more than these sats")
    try:
        tx = coins_spend_tx(coins, payout_spk, rate)
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
    # 0.9.12: the penalty's fee ladder (ascending rates; the last rung is the penalty itself), to
    # sign with the penalty (SIGN) or later while the bond is locked
    if bond.status in (bond.Status.SIGN, bond.Status.LOCKED) and bond.penalty_tx:
        lad = getattr(bond, "penalty_ladder", None) or {}
        if bond.status == bond.Status.LOCKED and not lad.get("rungs"):
            try:
                _build_penalty_ladder(bond)
                lad = bond.penalty_ladder or {}
            except Exception:
                lad = {}
        if lad.get("rungs"):
            data["bond_penalty_ladder_signed"] = bool(lad.get("sigs"))
            if not lad.get("sigs"):
                data["bond_penalty_ladder"] = [r["tx"] for r in lad["rungs"]]
                data["bond_penalty_rates"] = [r["rate"] for r in lad["rungs"]]
    # 0.9.7: the automatic return (address, the transaction to sign, whether it is signed)
    ar = getattr(bond, "auto_return", None) or {}
    if ar.get("plan"):
        data["bond_auto_return_address"] = ar.get("address", "")
        data["bond_auto_return_signed"] = bool(ar.get("sigs"))
        if ar.get("tx") and not ar.get("sigs") and bond.status in (bond.Status.SIGN, bond.Status.LOCKED):
            data["bond_auto_return_tx"] = ar["tx"]
            data["bond_auto_return_coins"] = [
                {"txid": c[0], "vout": int(c[1]), "value": int(c[2])} for c in ar.get("coins") or []
            ]
            # 0.9.11: the fee ladder (ascending rates; the last rung is bond_auto_return_tx)
            if ar.get("ladder"):
                data["bond_auto_return_ladder"] = [r["tx"] for r in ar["ladder"]]
                data["bond_auto_return_rates"] = [r["rate"] for r in ar["ladder"]]
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
            try:
                fee = leftover_cost(len(lo))
                data["bond_leftover_fee_sats"] = fee
                data["bond_leftover_worth"] = worth_recovering(data["bond_leftover_sats"], fee)
            except Exception:
                pass
            if bond.leftover_tx:
                data["bond_leftover_tx"] = bond.leftover_tx
                data["bond_leftover_coins"] = [
                    {"txid": c[0], "vout": int(c[1]), "value": int(c[2])} for c in bond.leftover_coins
                ]
    if bond.leftover_txids:
        data["bond_leftover_txid"] = bond.leftover_txids[-1]["txid"]
        data["bond_leftover_returned_sats"] = sum(int(i.get("sats", 0)) for i in bond.leftover_txids)
        # rl31-beta20: each return with its state, so the app shows them as done (pending or
        # confirmed) also after a reload, in past orders and in the final summary
        data["bond_leftover_returns"] = [
            {
                "txid": i["txid"],
                "sats": int(i.get("sats", 0)),
                "spent_sats": sum(int(c[2]) for c in i.get("coins", [])),
                "status": tx_status(i["txid"]),
            }
            for i in bond.leftover_txids
        ]
        data["bond_leftover_returned_confirmed"] = all(
            r["status"] == "confirmed" for r in data["bond_leftover_returns"]
        )
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
