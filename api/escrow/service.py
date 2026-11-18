import json
import math

import requests
from decouple import config
from django.db import transaction
from django.utils import timezone

from api.escrow import bip327 as M
from api.escrow.taproot import (
    SIGHASH_ALL_UNIFIED,
    EscrowContract,
    Transaction,
    TxOut,
    derive_secret,
    keypath_witness,
    leaf_witness,
    pubkey,
    schnorr_sign,
    schnorr_verify,
    xonly,
)
from api.escrow.transactions import (
    VB_KEYPATH,
    VB_LEAF_2SIG,
    Utxo,
    rescue_tx,
    funding_descriptor,
    funding_script_pubkey,
    plan_deposit,
    release_tx,
)

ROLES = ("seller", "buyer")


class EscrowError(ValueError):
    pass


def enabled():
    return bool(config("ONCHAIN_ESCROW", cast=bool, default=False) and config("ESCROW_SECRET", default=""))


def network_hrp():
    return {"mainnet": "bc", "testnet": "tb", "regtest": "bcrt"}.get(config("NETWORK", default="mainnet"), "bc")


def coordinator_alias():
    return config("COORDINATOR_ALIAS", default="coordinator").lower()


def coordinator_key(order_id):
    secret = config("ESCROW_SECRET", default="").encode()
    if not secret:
        raise EscrowError("onchain escrow is not configured")
    return derive_secret(secret, coordinator_alias(), order_id, "coordinator")


def rpc(method, params=None, wallet=None):
    url = config("BITCOIND_RPCURL").rstrip("/")
    if wallet:
        url += f"/wallet/{wallet}"
    r = requests.post(
        url,
        auth=(config("BITCOIND_RPCUSER"), config("BITCOIND_RPCPASSWORD")),
        data=json.dumps({"jsonrpc": "1.0", "id": "escrow", "method": method, "params": params or []}),
        timeout=30,
    ).json()
    if r.get("error"):
        raise EscrowError(f"bitcoind {method}: {r['error'].get('message')}")
    return r["result"]


def watch_wallet():
    name = config("ESCROW_WATCH_WALLET", default="roblosats-escrow")
    if name in rpc("listwallets"):
        return name
    try:
        rpc("loadwallet", [name])
    except EscrowError:
        rpc("createwallet", [name, True, True, "", False, True, True])
    return name


def role_of(order, user):
    from api.logics import Logics

    if Logics.is_seller(order, user):
        return "seller"
    if Logics.is_buyer(order, user):
        return "buyer"
    raise EscrowError("you are not a trader in this order")


def contract_for(escrow):
    return EscrowContract(
        bytes.fromhex(escrow.seller_pubkey),
        bytes.fromhex(escrow.buyer_pubkey),
        bytes.fromhex(escrow.coordinator_pubkey),
        escrow.rescue_csv,
        hrp=network_hrp(),
        rescue_plan=rescue_plan(escrow),
    )


RESCUE_PLAN = 2
# rl31-beta20: a funding excess too small for its own change output rides in the escrow and goes
# back to its owner with its bond (escrows created before have no "extra_plan": as before).
EXTRA_PLAN = 1


def extra_plan(escrow):
    return int((escrow.funding_signatures or {}).get("extra_plan") or 0)


def rescue_plan(escrow):
    """1: the seller alone takes the whole escrow after the timelock (escrows up to
    rl31-beta15); 2: both pre-sign a rescue that gives each one its own (rl31-beta16)."""
    return int((escrow.funding_signatures or {}).get("rescue_plan") or 1)


def _pub(hex33):
    try:
        b = bytes.fromhex(hex33)
        xonly(b)
        M.cpoint(b)
    except Exception as e:
        raise EscrowError("invalid public key") from e
    return b


def create_escrow(order):
    from api.models import OnchainEscrow

    escrow, _ = OnchainEscrow.objects.get_or_create(
        order=order,
        defaults={
            "coordinator_pubkey": pubkey(coordinator_key(order.id)).hex(),
            "rescue_csv": config("ESCROW_RESCUE_CSV", cast=int, default=2016),
            # fixed for the life of the escrow (api/escrow/flow.py FEE_PLAN, RESCUE_PLAN)
            "funding_signatures": {"fee_plan": 2, "rescue_plan": RESCUE_PLAN, "extra_plan": EXTRA_PLAN},
        },
    )
    return escrow


def submit_keys(order, user, escrow_pubkey_hex, funding_pubkey_hex):
    """A trader gives its escrow key and its funding key (both derived from its token)."""
    from api.models import OnchainEscrow

    role = role_of(order, user)
    escrow_pk = _pub(escrow_pubkey_hex)
    funding_pk = _pub(funding_pubkey_hex)
    with transaction.atomic():
        escrow = OnchainEscrow.objects.select_for_update().get(order=order)
        if escrow.status != OnchainEscrow.Status.KEYS:
            raise EscrowError("the keys can no longer be changed")
        setattr(escrow, f"{role}_pubkey", escrow_pk.hex())
        funding = dict(escrow.funding_signatures)
        funding[f"{role}_funding_pubkey"] = funding_pk.hex()
        escrow.funding_signatures = funding
        if escrow.seller_pubkey and escrow.buyer_pubkey:
            c = contract_for(escrow)
            escrow.address = c.address
            escrow.script_pubkey = c.script_pubkey.hex()
        escrow.save()
    if escrow.address:
        watch_funding(escrow)
    return escrow


def funding_spk(escrow, role):
    return funding_script_pubkey(bytes.fromhex(escrow.funding_signatures[f"{role}_funding_pubkey"]))


def watch_funding(escrow):
    wallet = watch_wallet()
    now = int(timezone.now().timestamp()) - 7200
    reqs = [
        {"desc": rpc("getdescriptorinfo", [funding_descriptor(bytes.fromhex(escrow.funding_signatures[f"{r}_funding_pubkey"]))])["descriptor"],
         "timestamp": now, "label": f"roblosats-{escrow.order_id}-{r}"}
        for r in ROLES
    ]
    reqs.append({"desc": rpc("getdescriptorinfo", [f"addr({escrow.address})"])["descriptor"], "timestamp": now,
                 "label": f"roblosats-{escrow.order_id}-escrow"})
    rpc("importdescriptors", [reqs], wallet=wallet)


def funding_utxos(escrow, role, min_conf=1):
    spk = funding_spk(escrow, role)
    addr = rpc("decodescript", [spk.hex()])["address"]
    found = rpc("listunspent", [min_conf, 9999999, [addr]], wallet=watch_wallet())
    return [Utxo(u["txid"], u["vout"], round(u["amount"] * 1e8), spk) for u in found]


def fee_rate(blocks=1):
    try:
        est = rpc("estimatesmartfee", [blocks])
        return max(1.0, est["feerate"] * 1e8 / 1000)
    except (EscrowError, KeyError):
        return config("ESCROW_FALLBACK_FEE_RATE", cast=float, default=5.0)


def build_deposit(escrow, escrow_sats, seller_fee, buyer_fee, coordinator_spk, dev_sats, dev_spk, bonds=None,
                  buyer_fee_from_bond=False, fees_in_escrow=False):
    """Plan the deposit from the confirmed funding of both traders and store it unsigned.
    bonds: {"seller": OnchainBond, "buyer": OnchainBond} (onchain bonds become inputs).
    buyer_fee_from_bond: the buyer funds nothing, its fee comes out of its bond."""
    from api.escrow import bonds as bond_service
    from api.models import OnchainEscrow

    bonds = bonds or {}

    seller = funding_utxos(escrow, "seller")
    buyer = funding_utxos(escrow, "buyer") if buyer_fee and not buyer_fee_from_bond else []
    plan = plan_deposit(
        seller, buyer, contract_for(escrow), escrow_sats, seller_fee, buyer_fee,
        coordinator_spk, dev_sats, dev_spk, funding_spk(escrow, "seller"), funding_spk(escrow, "buyer"),
        fee_rate(int(config("ESCROW_DEPOSIT_TARGET_BLOCKS", default=2))),
        bond_service.bond_input(bonds["seller"]) if bonds.get("seller") else None,
        bond_service.bond_input(bonds["buyer"]) if bonds.get("buyer") else None,
        buyer_fee_from_bond=buyer_fee_from_bond, fees_in_escrow=fees_in_escrow,
        extra_to_escrow=extra_plan(escrow) >= 1,
    )
    escrow.escrow_sats = plan.tx.outputs[0].value
    dev_paid = sum(o.value for o in plan.tx.outputs[1:] if dev_spk and o.script_pubkey == dev_spk)
    escrow.dev_fee_sats = dev_paid
    # Only what the deposit really pays to the coordinator (rl31-beta13): a fee output below
    # dust would have been dropped and never reached it.
    escrow.coordinator_fee_sats = sum(o.value for o in plan.tx.outputs[1:] if o.script_pubkey == coordinator_spk)
    escrow.funding_tx = plan.tx.serialize().hex()
    sigs = dict(escrow.funding_signatures)
    sigs["plan"] = {
        "spent": [[s.value, s.script_pubkey.hex()] for s in plan.spent],
        "seller_inputs": plan.seller_inputs,
        "buyer_inputs": plan.buyer_inputs,
        "bond_inputs": plan.bond_inputs or {},
        "bond_input_lists": plan.bond_input_lists or {},
        "bond_ids": {r: b.pk for r, b in bonds.items() if b},
        "trade_sats": escrow_sats,
        "bond_in_escrow": plan.bond_in_escrow or {},
        "buyer_fee_from_bond": bool(buyer_fee_from_bond),
        "seller_pays": plan.seller_pays,
        "buyer_pays": plan.buyer_pays,
        "seller_mining": plan.seller_mining,
        "buyer_mining": plan.buyer_mining,
        "seller_fee": seller_fee,
        "buyer_fee": buyer_fee,
        "mining_fee": plan.mining_fee,
        "extra_in_escrow": plan.extra_in_escrow or {},
    }
    if fees_in_escrow:  # plan 2: the fees ride in the escrow output until the release
        sigs["plan"]["fees_in_escrow"] = {"seller": seller_fee, "buyer": buyer_fee}
    escrow.funding_signatures = sigs
    escrow.status = OnchainEscrow.Status.FUNDING
    escrow.save()
    return plan


def stored_deposit(escrow):
    p = escrow.funding_signatures["plan"]
    tx = Transaction.parse(bytes.fromhex(escrow.funding_tx))
    for txin in tx.inputs:
        txin.witness = []
    spent = [TxOut(v, bytes.fromhex(s)) for v, s in p["spent"]]
    return tx, spent, p


def bond_indexes(plan, role):
    """Input indexes of a trader's bond in the deposit (several when it was topped up)."""
    lists = plan.get("bond_input_lists") or {}
    if role in lists:
        return list(lists[role])
    i = (plan.get("bond_inputs") or {}).get(role)
    return [] if i is None else [i]


def submit_funding_signatures(escrow, role, sigs_hex, bond_sig_hex=None):
    """A trader sends one BIP340 signature (0x21) per own funding input of the deposit, plus
    one for its bond input if it has an onchain bond; broadcast when both sides are in."""
    from api.escrow import bonds as bond_service
    from api.models import OnchainBond, OnchainEscrow

    with transaction.atomic():
        escrow = OnchainEscrow.objects.select_for_update().get(pk=escrow.pk)
        if escrow.status != OnchainEscrow.Status.FUNDING:
            raise EscrowError("the deposit is not waiting for signatures")
        if rescue_plan(escrow) >= 2 and not all(
            escrow.funding_signatures.get(f"{r}_rescue_sig") for r in ROLES
        ):
            # Nobody signs the deposit before holding the other's rescue signature: a deposit
            # without the complete rescue would leave its funds to the coordinator's goodwill.
            raise EscrowError("both rescue signatures are needed before the deposit signatures")
        tx, spent, plan = stored_deposit(escrow)
        idx = plan[f"{role}_inputs"]
        if len(sigs_hex) != len(idx):
            raise EscrowError("one signature per input is needed")
        fpk = xonly(bytes.fromhex(escrow.funding_signatures[f"{role}_funding_pubkey"]))
        for i, s in zip(idx, sigs_hex):
            sig = bytes.fromhex(s)
            if len(sig) != 65 or sig[64] != SIGHASH_ALL_UNIFIED:
                raise EscrowError("signatures must be 64 bytes plus SIGHASH_ALL|UNIFIED")
            if not schnorr_verify(tx.sighash_unified(i, spent), fpk, sig[:64]):
                raise EscrowError("a signature does not verify")
        data = dict(escrow.funding_signatures)
        bond_idx = bond_indexes(plan, role)
        if bond_idx:
            bond = OnchainBond.objects.get(pk=plan["bond_ids"][role])
            c = bond_service.contract_of(bond)
            bond_sigs = [bond_sig_hex] if isinstance(bond_sig_hex, str) else list(bond_sig_hex or [])
            if len(bond_sigs) != len(bond_idx):
                raise EscrowError(f"the bond needs one signature per coin ({len(bond_idx)})")
            for i, s in zip(bond_idx, bond_sigs):
                sig = bytes.fromhex(s or "")
                msg = tx.sighash_unified(i, spent, SIGHASH_ALL_UNIFIED, c.leaf_hash(c.COOPERATIVE))
                if len(sig) != 65 or sig[64] != SIGHASH_ALL_UNIFIED or not schnorr_verify(msg, xonly(bytes.fromhex(bond.owner_pubkey)), sig[:64]):
                    raise EscrowError("the bond signature does not verify")
            data[f"{role}_bond_sig"] = bond_sigs[0]
            data[f"{role}_bond_sigs"] = bond_sigs
        data[f"{role}_sigs"] = sigs_hex
        escrow.funding_signatures = data
        complete = all(
            (data.get(f"{r}_sigs") or not plan[f"{r}_inputs"])
            and (not bond_indexes(plan, r) or data.get(f"{r}_bond_sig"))
            for r in ROLES
        )
        if complete:
            for r in ROLES:
                for i, s in zip(plan[f"{r}_inputs"], data.get(f"{r}_sigs", [])):
                    tx.inputs[i].witness = [bytes.fromhex(s)]
            for r in ROLES:
                idx = bond_indexes(plan, r)
                if not idx:
                    continue
                bond = OnchainBond.objects.get(pk=plan["bond_ids"][r])
                c = bond_service.contract_of(bond)
                owner_sigs = data.get(f"{r}_bond_sigs") or [data[f"{r}_bond_sig"]]
                for i, s in zip(idx, owner_sigs):
                    coord = bond_service.coordinator_deposit_signature(bond, tx, spent, i)
                    tx.inputs[i].witness = leaf_witness(c, c.COOPERATIVE, [coord, bytes.fromhex(s)[:64]])
            escrow.funding_tx = tx.serialize().hex()
            escrow.funding_txid = tx.txid()
            escrow.funding_vout = 0
        escrow.save()
    if complete:
        rpc("sendrawtransaction", [escrow.funding_tx])
        escrow.status = OnchainEscrow.Status.MEMPOOL
        escrow.save(update_fields=["status", "updated_at"])
        OnchainBond.objects.filter(pk__in=list(plan.get("bond_ids", {}).values())).update(
            status=OnchainBond.Status.IN_DEPOSIT, spend_txid=escrow.funding_txid
        )
    return escrow


def rescue_fee_rate():
    """Fee rate of the pre-signed rescue, fixed weeks before it can be broadcast: today's rate,
    never below ESCROW_RESCUE_MIN_FEE_RATE (default 2) nor above ESCROW_RESCUE_MAX_FEE_RATE
    (default 20, so the fee stays under the client's MAX_RESCUE_FEE_SATS). Each side can CPFP
    its own output if it is not enough then."""
    low = config("ESCROW_RESCUE_MIN_FEE_RATE", cast=float, default=2.0)
    high = config("ESCROW_RESCUE_MAX_FEE_RATE", cast=float, default=20.0)
    return min(high, max(low, fee_rate(2)))


def build_rescue(escrow, to_seller, to_buyer):
    """Rescue plan 2: fix the rescue transaction right after the deposit is planned (its txid
    is known before any signature). Both traders sign it (leaf 2) before the deposit."""
    tx = Transaction.parse(bytes.fromhex(escrow.funding_tx))
    c = contract_for(escrow)
    data = dict(escrow.funding_signatures)
    rtx = rescue_tx(
        c, tx.txid(), 0, escrow.escrow_sats, to_seller, bytes.fromhex(data["seller_payout_spk"]),
        to_buyer, bytes.fromhex(data["buyer_payout_spk"]), rescue_fee_rate(),
    )
    data["rescue"] = {"tx": rtx.serialize().hex(), "to_seller": to_seller, "to_buyer": to_buyer}
    for r in ROLES:
        data.pop(f"{r}_rescue_sig", None)
    escrow.funding_signatures = data
    escrow.save(update_fields=["funding_signatures", "updated_at"])
    return rtx


def rescue_message(escrow):
    c = contract_for(escrow)
    tx = Transaction.parse(bytes.fromhex(escrow.funding_signatures["rescue"]["tx"]))
    msg = tx.sighash_unified(0, [TxOut(escrow.escrow_sats, c.script_pubkey)], SIGHASH_ALL_UNIFIED,
                             c.leaf_hash(c.RESCUE))
    return c, tx, msg


def signed_rescue(escrow):
    """The complete rescue (both signatures in the witness), or None."""
    data = escrow.funding_signatures or {}
    if not data.get("rescue") or not all(data.get(f"{r}_rescue_sig") for r in ROLES):
        return None
    c, tx, _ = rescue_message(escrow)
    sigs = [bytes.fromhex(data[f"{r}_rescue_sig"])[:64] for r in ("buyer", "seller")]
    tx.inputs[0].witness = leaf_witness(c, c.RESCUE, sigs)
    return tx


def submit_rescue_signature(escrow, role, sig_hex):
    """A trader's signature (0x21, leaf 2) on the pre-signed rescue."""
    from api.models import OnchainEscrow

    with transaction.atomic():
        escrow = OnchainEscrow.objects.select_for_update().get(pk=escrow.pk)
        if rescue_plan(escrow) < 2 or escrow.status != OnchainEscrow.Status.FUNDING:
            raise EscrowError("the rescue is not waiting for signatures")
        if not escrow.funding_signatures.get("rescue"):
            raise EscrowError("the rescue is not ready yet")
        _, _, msg = rescue_message(escrow)
        sig = bytes.fromhex(sig_hex)
        pk = xonly(bytes.fromhex(getattr(escrow, f"{role}_pubkey")))
        if len(sig) != 65 or sig[64] != SIGHASH_ALL_UNIFIED or not schnorr_verify(msg, pk, sig[:64]):
            raise EscrowError("the rescue signature does not verify")
        data = dict(escrow.funding_signatures)
        data[f"{role}_rescue_sig"] = sig_hex
        escrow.funding_signatures = data
        escrow.save(update_fields=["funding_signatures", "updated_at"])
    return escrow


def refresh_confirmation(escrow, min_conf=1):
    from api.models import OnchainEscrow

    if escrow.status != OnchainEscrow.Status.MEMPOOL:
        return escrow
    info = rpc("gettxout", [escrow.funding_txid, escrow.funding_vout, True])
    if info and info["confirmations"] >= min_conf:
        escrow.confirmed_height = rpc("getblockcount") - info["confirmations"] + 1
        escrow.status = OnchainEscrow.Status.LOCKED
        escrow.save(update_fields=["status", "confirmed_height", "updated_at"])
    return escrow


def _release_tx(escrow, payouts, kind, fee_payer=0):
    rate = fee_rate(1)
    vb = VB_KEYPATH if kind == escrow.ReleaseKind.COOPERATIVE else VB_LEAF_2SIG
    if isinstance(payouts, (bytes, bytearray)):
        payouts = [TxOut(escrow.escrow_sats, bytes(payouts))]
    tx = release_tx(contract_for(escrow), escrow.funding_txid, escrow.funding_vout, escrow.escrow_sats,
                    payouts, vb, rate, fee_payer)
    return tx, rate


def start_release(escrow, payouts, kind, fee_payer=0):
    """Fix the release transaction. payouts: list of TxOut adding up to the escrow output (or a
    single script that takes it all); payouts[fee_payer] pays the next-block mining fee."""
    from api.models import OnchainEscrow

    if escrow.status != OnchainEscrow.Status.LOCKED:
        raise EscrowError("the escrow is not locked")
    tx, rate = _release_tx(escrow, payouts, kind, fee_payer)
    escrow.release_kind = kind
    escrow.release_tx = tx.serialize().hex()
    escrow.release_fee_rate = rate
    escrow.release_nonces = {}
    escrow.release_partial_sigs = {}
    escrow.status = OnchainEscrow.Status.RELEASING
    escrow.save()
    return tx


def release_message(escrow):
    c = contract_for(escrow)
    tx = Transaction.parse(bytes.fromhex(escrow.release_tx))
    for txin in tx.inputs:
        txin.witness = []
    leaf = None
    if escrow.release_kind == escrow.ReleaseKind.BUYER_WINS:
        leaf = c.leaf_hash(c.BUYER_COORDINATOR)
    elif escrow.release_kind == escrow.ReleaseKind.SELLER_WINS:
        leaf = c.leaf_hash(c.SELLER_COORDINATOR)
    return tx, tx.sighash_unified(0, [TxOut(escrow.escrow_sats, c.script_pubkey)], SIGHASH_ALL_UNIFIED, leaf)


def submit_release_nonce(escrow, role, pubnonce_hex):
    from api.models import OnchainEscrow

    with transaction.atomic():
        escrow = OnchainEscrow.objects.select_for_update().get(pk=escrow.pk)
        if escrow.status != OnchainEscrow.Status.RELEASING or escrow.release_kind != escrow.ReleaseKind.COOPERATIVE:
            raise EscrowError("not waiting for release nonces")
        nonce = bytes.fromhex(pubnonce_hex)
        if len(nonce) != 66:
            raise EscrowError("a MuSig2 public nonce is 66 bytes")
        nonces = dict(escrow.release_nonces)
        if role in nonces and nonces[role] != pubnonce_hex:
            if role in escrow.release_partial_sigs:
                raise EscrowError("the nonce can no longer change: your partial signature was already given")
            # This trader lost its secret nonce (the app restarted between its nonce and its
            # partial signature). Whatever the other gave was made for the old aggregate nonce:
            # a new signing round for both (17 nov 2026 QA: the peer had already signed, the
            # new nonce was refused and the release could never finish).
            nonces = {}
            escrow.release_partial_sigs = {}
            data = dict(escrow.funding_signatures)
            data["release_round"] = int(data.get("release_round") or 0) + 1
            escrow.funding_signatures = data
        nonces[role] = pubnonce_hex
        escrow.release_nonces = nonces
        escrow.save(update_fields=["release_nonces", "release_partial_sigs", "funding_signatures", "updated_at"])
    return escrow


def aggregate_nonce(escrow):
    if not all(r in escrow.release_nonces for r in ROLES):
        return None
    return M.nonce_agg([bytes.fromhex(escrow.release_nonces[r]) for r in ROLES]).hex()


def submit_release_partial(escrow, role, psig_hex):
    """MuSig2 partial signature for the cooperative release; broadcast once both verify."""
    from api.models import OnchainEscrow

    with transaction.atomic():
        escrow = OnchainEscrow.objects.select_for_update().get(pk=escrow.pk)
        agg = aggregate_nonce(escrow)
        if escrow.status != OnchainEscrow.Status.RELEASING or agg is None:
            raise EscrowError("not waiting for partial signatures")
        c = contract_for(escrow)
        tx, msg = release_message(escrow)
        pks, tweaks, isx = c.musig_pubkeys(), [c.tweak], [True]
        pubnonces = [bytes.fromhex(escrow.release_nonces[r]) for r in ROLES]
        psig = bytes.fromhex(psig_hex)
        if not M.partial_sig_verify(psig, pubnonces, pks, tweaks, isx, msg, ROLES.index(role)):
            raise EscrowError("the partial signature does not verify")
        psigs = dict(escrow.release_partial_sigs)
        psigs[role] = psig_hex
        escrow.release_partial_sigs = psigs
        done = all(r in psigs for r in ROLES)
        if done:
            ctx = M.SessionContext(bytes.fromhex(agg), pks, tweaks, isx, msg)
            sig = M.partial_sig_agg([bytes.fromhex(psigs[r]) for r in ROLES], ctx)
            if not schnorr_verify(msg, c.output_key, sig):
                raise EscrowError("the aggregate signature does not verify")
            tx.inputs[0].witness = keypath_witness(sig)
            escrow.release_tx = tx.serialize().hex()
            escrow.release_txid = tx.txid()
        escrow.save()
    if done:
        rpc("sendrawtransaction", [escrow.release_tx])
        # A collaborative cancel gives the escrow back to the seller: say so (rl31-beta12).
        cancel = bool(escrow.funding_signatures.get("cancel"))
        escrow.status = OnchainEscrow.Status.REFUNDED if cancel else OnchainEscrow.Status.RELEASED
        escrow.save(update_fields=["status", "updated_at"])
    return escrow


def submit_leaf_signature(escrow, role, sig_hex):
    """Dispute release: the winner signs the leaf; the coordinator adds its signature and broadcasts."""
    from api.models import OnchainEscrow

    kind = escrow.release_kind
    expected = "buyer" if kind == escrow.ReleaseKind.BUYER_WINS else "seller"
    if escrow.status != OnchainEscrow.Status.RELEASING or kind not in (escrow.ReleaseKind.BUYER_WINS, escrow.ReleaseKind.SELLER_WINS):
        raise EscrowError("not waiting for a dispute signature")
    if role != expected:
        raise EscrowError("only the winner signs this release")
    c = contract_for(escrow)
    tx, msg = release_message(escrow)
    sig = bytes.fromhex(sig_hex)
    winner_pk = xonly(bytes.fromhex(escrow.buyer_pubkey if role == "buyer" else escrow.seller_pubkey))
    if len(sig) != 65 or sig[64] != SIGHASH_ALL_UNIFIED or not schnorr_verify(msg, winner_pk, sig[:64]):
        raise EscrowError("the signature does not verify")
    index = c.BUYER_COORDINATOR if role == "buyer" else c.SELLER_COORDINATOR
    coord = schnorr_sign(coordinator_key(escrow.order_id), msg)
    tx.inputs[0].witness = leaf_witness(c, index, [coord, sig[:64]])
    escrow.release_tx = tx.serialize().hex()
    escrow.release_txid = tx.txid()
    escrow.save()
    rpc("sendrawtransaction", [escrow.release_tx])
    escrow.status = OnchainEscrow.Status.RELEASED if role == "buyer" else OnchainEscrow.Status.REFUNDED
    escrow.save(update_fields=["status", "updated_at"])
    return escrow


def fee_split(order):
    from api.logics import FEE, MAKER_FEE_SPLIT, Logics

    amount = order.last_satoshis
    maker_fee = math.ceil(amount * FEE * MAKER_FEE_SPLIT)
    taker_fee = math.ceil(amount * FEE * (1 - MAKER_FEE_SPLIT))
    seller_is_maker = Logics.is_seller(order, order.maker)
    return (maker_fee, taker_fee) if seller_is_maker else (taker_fee, maker_fee)

