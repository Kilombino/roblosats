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
from api.escrow.transactions import BondInput, Utxo, bond_spend_tx


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


def bond_input(bond):
    return BondInput(
        Utxo(bond.txid, bond.vout, bond.value, bytes.fromhex(bond.script_pubkey)), contract_of(bond)
    )


def required_sats(order, take_order=None):
    sats = take_order.last_satoshis if take_order is not None and take_order.last_satoshis else order.last_satoshis
    return int(sats * order.bond_size / 100)


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
    found = service.rpc("listunspent", [min_conf, 9999999, [bond.address]], wallet=service.watch_wallet())
    found = [u for u in found if round(u["amount"] * 1e8) >= bond.required_sats]
    if not found:
        return bond
    u = max(found, key=lambda x: x["amount"])
    bond.txid, bond.vout, bond.value = u["txid"], u["vout"], round(u["amount"] * 1e8)
    from api.escrow.flow import coordinator_fee_spk

    tx = bond_spend_tx(bond.txid, bond.vout, bond.value, coordinator_fee_spk(),
                       service.config("ESCROW_PENALTY_FEE_RATE", cast=float, default=10.0))
    bond.penalty_tx = tx.serialize().hex()
    bond.status = OnchainBond.Status.SIGN
    bond.save()
    return bond


def penalty_message(bond):
    c = contract_of(bond)
    tx = Transaction.parse(bytes.fromhex(bond.penalty_tx))
    return tx, tx.sighash_unified(0, [TxOut(bond.value, c.script_pubkey)], SIGHASH_ALL_UNIFIED, c.leaf_hash(c.COOPERATIVE))


def submit_penalty_signature(bond, sig_hex):
    """The owner's signature of the penalty, given in advance: the bond is then LOCKED."""
    from api.models import OnchainBond

    with transaction.atomic():
        bond = OnchainBond.objects.select_for_update().get(pk=bond.pk)
        if bond.status != OnchainBond.Status.SIGN:
            raise EscrowError("the bond is not waiting for the penalty signature")
        _, msg = penalty_message(bond)
        sig = bytes.fromhex(sig_hex)
        if len(sig) != 65 or sig[64] != SIGHASH_ALL_UNIFIED or not schnorr_verify(msg, xonly(bytes.fromhex(bond.owner_pubkey)), sig[:64]):
            raise EscrowError("the penalty signature does not verify")
        bond.penalty_owner_sig = sig_hex
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
    c = contract_of(bond)
    tx, msg = penalty_message(bond)
    owner = bytes.fromhex(bond.penalty_owner_sig)[:64]
    tx.inputs[0].witness = leaf_witness(c, c.COOPERATIVE, [schnorr_sign(_coordinator_key_of(bond), msg), owner])
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
    tx = bond_spend_tx(bond.txid, bond.vout, bond.value, payout_spk, service.fee_rate(2))
    bond.return_tx = tx.serialize().hex()
    bond.save(update_fields=["return_tx", "updated_at"])
    return tx


def submit_return_signature(bond, sig_hex):
    """The owner signs the return the server built; the coordinator adds its own and broadcasts."""
    from api.models import OnchainBond

    if bond.status != OnchainBond.Status.RETURNABLE or not bond.return_tx:
        raise EscrowError("ask for the return first")
    c = contract_of(bond)
    tx = Transaction.parse(bytes.fromhex(bond.return_tx))
    msg = tx.sighash_unified(0, [TxOut(bond.value, c.script_pubkey)], SIGHASH_ALL_UNIFIED, c.leaf_hash(c.COOPERATIVE))
    sig = bytes.fromhex(sig_hex)
    if len(sig) != 65 or sig[64] != SIGHASH_ALL_UNIFIED or not schnorr_verify(msg, xonly(bytes.fromhex(bond.owner_pubkey)), sig[:64]):
        raise EscrowError("the return signature does not verify")
    tx.inputs[0].witness = leaf_witness(c, c.COOPERATIVE, [schnorr_sign(_coordinator_key_of(bond), msg), sig[:64]])
    service.rpc("sendrawtransaction", [tx.serialize().hex()])
    bond.spend_txid = tx.txid()
    bond.status = OnchainBond.Status.RETURNED
    bond.save(update_fields=["spend_txid", "status", "updated_at"])
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
    }
    if bond.txid:
        data.update({"bond_txid": bond.txid, "bond_vout": bond.vout, "bond_value": bond.value})
    if bond.status == bond.Status.SIGN:
        data["bond_penalty_tx"] = bond.penalty_tx
    if bond.return_tx and bond.status == bond.Status.RETURNABLE:
        data["bond_return_tx"] = bond.return_tx
    if bond.spend_txid:
        data["bond_spend_txid"] = bond.spend_txid
    return data
