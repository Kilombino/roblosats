import math
from dataclasses import dataclass

from api.escrow.taproot import (
    EscrowContract,
    Transaction,
    TxIn,
    TxOut,
    with_hashtype,
    xonly,
)

DUST_P2TR = 330
VB_OVERHEAD = 10.5
VB_TAPROOT_KEY_INPUT = 41 + (1 + 1 + 65) / 4
VB_P2TR_OUTPUT = 43.0
VB_OUTPUT_BASE = 9.0


def funding_script_pubkey(pk33: bytes) -> bytes:
    """rawtr(<pk>): a single-key Taproot output with no tweak, spent by the trader alone."""
    return b"\x51\x20" + xonly(pk33)


def funding_descriptor(pk33: bytes) -> str:
    return f"rawtr({xonly(pk33).hex()})"


def output_vbytes(script_pubkey: bytes) -> float:
    return VB_OUTPUT_BASE + len(script_pubkey)


@dataclass
class Utxo:
    txid: str
    vout: int
    value: int
    script_pubkey: bytes


@dataclass
class DepositPlan:
    tx: Transaction
    spent: list[TxOut]
    escrow_vout: int
    seller_inputs: list[int]
    buyer_inputs: list[int]
    seller_pays: int
    buyer_pays: int
    mining_fee: int


class DepositError(ValueError):
    pass


def plan_deposit(
    seller_utxos: list[Utxo],
    buyer_utxos: list[Utxo],
    contract: EscrowContract,
    escrow_sats: int,
    seller_fee_sats: int,
    buyer_fee_sats: int,
    coordinator_spk: bytes,
    dev_sats: int,
    dev_spk: bytes | None,
    seller_change_spk: bytes,
    buyer_change_spk: bytes,
    fee_rate: float,
) -> DepositPlan:
    """Joint deposit: escrow output, coordinator fee, dev fee and each side's change.

    The seller puts in the escrow plus its fee; the buyer its fee. Each side pays the mining
    fee of its own inputs and change, and half of the rest. The dev share comes out of the
    trading fees (it is not charged on top); below dust it stays with the coordinator.
    """
    if not seller_utxos:
        raise DepositError("the seller has not funded yet")
    if not buyer_utxos and buyer_fee_sats:
        raise DepositError("the buyer has not funded yet")
    if escrow_sats < DUST_P2TR or seller_fee_sats < 0 or buyer_fee_sats < 0:
        raise DepositError("bad amounts")
    if dev_sats < DUST_P2TR or dev_spk is None:
        dev_sats = 0
    coordinator_sats = seller_fee_sats + buyer_fee_sats - dev_sats
    if coordinator_sats < 0:
        raise DepositError("the dev share is larger than the fees")

    outputs = [TxOut(escrow_sats, contract.script_pubkey)]
    if coordinator_sats >= DUST_P2TR:
        outputs.append(TxOut(coordinator_sats, coordinator_spk))
    if dev_sats:
        outputs.append(TxOut(dev_sats, dev_spk))

    shared_vb = VB_OVERHEAD + sum(output_vbytes(o.script_pubkey) for o in outputs)
    seller_in = sum(u.value for u in seller_utxos)
    buyer_in = sum(u.value for u in buyer_utxos)

    def side_fee(n_inputs, change_spk, with_change):
        vb = n_inputs * VB_TAPROOT_KEY_INPUT + (output_vbytes(change_spk) if with_change else 0)
        return math.ceil((vb + shared_vb / 2) * fee_rate)

    def change_for(total_in, owed, n_inputs, change_spk):
        fee = side_fee(n_inputs, change_spk, True)
        change = total_in - owed - fee
        if change >= DUST_P2TR:
            return change, fee
        fee = side_fee(n_inputs, change_spk, False)
        if total_in - owed - fee < 0:
            return None, fee
        return 0, total_in - owed

    seller_change, seller_mining = change_for(
        seller_in, escrow_sats + seller_fee_sats, len(seller_utxos), seller_change_spk
    )
    if seller_change is None:
        raise DepositError(f"the seller's funding is short by {escrow_sats + seller_fee_sats + seller_mining - seller_in} sats")
    buyer_change, buyer_mining = (0, 0)
    if buyer_utxos:
        buyer_change, buyer_mining = change_for(
            buyer_in, buyer_fee_sats, len(buyer_utxos), buyer_change_spk
        )
        if buyer_change is None:
            raise DepositError(f"the buyer's funding is short by {buyer_fee_sats + buyer_mining - buyer_in} sats")
    if seller_change:
        outputs.append(TxOut(seller_change, seller_change_spk))
    if buyer_change:
        outputs.append(TxOut(buyer_change, buyer_change_spk))

    utxos = list(seller_utxos) + list(buyer_utxos)
    tx = Transaction([TxIn(u.txid, u.vout) for u in utxos], outputs)
    spent = [TxOut(u.value, u.script_pubkey) for u in utxos]
    mining_fee = sum(s.value for s in spent) - sum(o.value for o in outputs)
    return DepositPlan(
        tx=tx,
        spent=spent,
        escrow_vout=0,
        seller_inputs=list(range(len(seller_utxos))),
        buyer_inputs=list(range(len(seller_utxos), len(utxos))),
        seller_pays=seller_in - seller_change,
        buyer_pays=buyer_in - buyer_change,
        mining_fee=mining_fee,
    )


def check_deposit(
    tx: Transaction,
    spent: list[TxOut],
    contract: EscrowContract,
    escrow_sats: int,
    my_inputs: list[int],
    my_max_spend: int,
    my_change_spk: bytes,
    max_fee_rate: float,
) -> None:
    """What a trader's client checks before signing its inputs of the deposit."""
    if not tx.outputs or tx.outputs[0].script_pubkey != contract.script_pubkey:
        raise DepositError("the first output is not the escrow")
    if tx.outputs[0].value != escrow_sats:
        raise DepositError("the escrow amount is wrong")
    mine_in = sum(spent[i].value for i in my_inputs)
    mine_back = sum(o.value for o in tx.outputs if o.script_pubkey == my_change_spk)
    if mine_in - mine_back > my_max_spend:
        raise DepositError("this deposit takes more than agreed from you")
    fee = sum(s.value for s in spent) - sum(o.value for o in tx.outputs)
    if fee < 0:
        raise DepositError("outputs exceed inputs")
    est_vb = VB_OVERHEAD + len(tx.inputs) * VB_TAPROOT_KEY_INPUT + sum(
        output_vbytes(o.script_pubkey) for o in tx.outputs
    )
    if fee > est_vb * max_fee_rate:
        raise DepositError("the mining fee is too high")


def sign_funding_inputs(tx: Transaction, spent: list[TxOut], indexes: list[int], sign) -> None:
    """sign(msg32) -> 64-byte BIP340 signature with the trader's funding key (rawtr, no tweak)."""
    for i in indexes:
        tx.inputs[i].witness = [with_hashtype(sign(tx.sighash_unified(i, spent)))]


def release_tx(
    contract: EscrowContract,
    escrow_txid: str,
    escrow_vout: int,
    escrow_sats: int,
    payouts: list[TxOut],
    kind_vbytes: float,
    fee_rate: float,
    fee_payer: int = 0,
    sequence: int | None = None,
) -> Transaction:
    """Release of the escrow. payouts[fee_payer] pays the mining fee; outputs below dust are dropped."""
    vb = math.ceil(VB_OVERHEAD + kind_vbytes + sum(output_vbytes(p.script_pubkey) for p in payouts))
    fee = math.ceil(vb * fee_rate)
    if sum(p.value for p in payouts) != escrow_sats:
        raise ValueError("the payouts must add up to the escrow")
    outs = [TxOut(p.value, p.script_pubkey) for p in payouts]
    outs[fee_payer].value -= fee
    if outs[fee_payer].value < DUST_P2TR:
        raise ValueError("the fee leaves the payer below dust")
    outs = [o for o in outs if o.value >= DUST_P2TR]
    txin = TxIn(escrow_txid, escrow_vout)
    if sequence is not None:
        txin.sequence = sequence
    return Transaction([txin], outs)


VB_KEYPATH = VB_TAPROOT_KEY_INPUT
VB_LEAF_2SIG = 41 + (1 + 2 * 66 + 71 + 98) / 4
VB_LEAF_RESCUE = 41 + (1 + 66 + 40 + 66) / 4
