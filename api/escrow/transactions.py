import math
from dataclasses import dataclass

from api.escrow.taproot import (
    SEQUENCE_RBF,
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
    bond_inputs: dict = None
    # Every input index of each bond (a topped-up bond has several coins); bond_inputs keeps the first
    bond_input_lists: dict = None
    # What each bond leaves in the escrow output (the buyer's is smaller when its fee came out of it)
    bond_in_escrow: dict = None
    seller_mining: int = 0
    buyer_mining: int = 0
    # rl31-beta20 (extra plan 1): a funding excess too small for a change output of its own,
    # kept in the escrow output and returned to its owner with its bond
    extra_in_escrow: dict = None


class DepositError(ValueError):
    pass


@dataclass
class BondInput:
    """An onchain bond as deposit input(s): one coin, or several that together reach the
    bond (rl31-beta18: a bond can be topped up with a second payment)."""
    utxo: Utxo
    contract: object
    extra: list = None  # more coins of the same bond, after utxo

    @property
    def utxos(self) -> list:
        return [self.utxo] + list(self.extra or [])

    @property
    def value(self) -> int:
        return sum(u.value for u in self.utxos)


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
    seller_bond: "BondInput | None" = None,
    buyer_bond: "BondInput | None" = None,
    buyer_fee_from_bond: bool = False,
    fees_in_escrow: bool = False,
    extra_to_escrow: bool = False,
) -> DepositPlan:
    """Joint deposit: escrow output, coordinator fee, dev fee and each side's change.

    The seller puts in the escrow plus its fee; the buyer its fee. Each side pays the mining
    fee of its own inputs and change, and half of the rest. The dev share comes out of the
    trading fees (it is not charged on top); below dust it stays with the coordinator.

    buyer_fee_from_bond: the buyer sends nothing; its fee and its share of the mining fee come
    out of its bond (already an input), which leaves the rest in the escrow output.

    fees_in_escrow (fee plan 2, rl31-beta15): no coordinator nor dev output; both fees are added
    to the escrow output and the release of a completed trade pays them (a cancel returns them).

    extra_to_escrow (rl31-beta20): every confirmed coin at a funding address is spent; an excess
    worth a change output (at least dust plus the cost of that output at this fee rate) comes
    back as change, a smaller one rides in the escrow output (extra_in_escrow) and goes back to
    its owner with its bond in every release, cancel, dispute and rescue. Only for a trader with
    an onchain bond in the deposit (it always has an output of its own to carry it).
    """
    if not seller_utxos:
        raise DepositError("the seller has not funded yet")
    if buyer_fee_from_bond:
        if buyer_bond is None:
            raise DepositError("the buyer's fee can only come out of an onchain bond")
        buyer_utxos = []
    elif not buyer_utxos and buyer_fee_sats:
        raise DepositError("the buyer has not funded yet")
    if escrow_sats < DUST_P2TR or seller_fee_sats < 0 or buyer_fee_sats < 0:
        raise DepositError("bad amounts")
    if dev_sats < DUST_P2TR or dev_spk is None or fees_in_escrow:
        dev_sats = 0
    held = seller_fee_sats + buyer_fee_sats if fees_in_escrow else 0
    coordinator_sats = 0 if fees_in_escrow else seller_fee_sats + buyer_fee_sats - dev_sats
    if coordinator_sats < 0:
        raise DepositError("the dev share is larger than the fees")

    bond_in_escrow = {r: b.value for r, b in (("seller", seller_bond), ("buyer", buyer_bond)) if b}
    outputs = [TxOut(escrow_sats + held + sum(bond_in_escrow.values()), contract.script_pubkey)]
    if coordinator_sats >= DUST_P2TR:
        outputs.append(TxOut(coordinator_sats, coordinator_spk))
    if dev_sats:
        outputs.append(TxOut(dev_sats, dev_spk))

    shared_vb = VB_OVERHEAD + sum(output_vbytes(o.script_pubkey) for o in outputs)
    seller_in = sum(u.value for u in seller_utxos)
    buyer_in = sum(u.value for u in buyer_utxos)

    def side_fee(n_inputs, change_spk, with_change, bond):
        vb = n_inputs * VB_TAPROOT_KEY_INPUT + (output_vbytes(change_spk) if with_change else 0)
        vb += VB_BOND_COOP * len(bond.utxos) if bond else 0
        return math.ceil((vb + shared_vb / 2) * fee_rate)

    extra_in_escrow = {}

    def change_threshold(change_spk):
        return DUST_P2TR + math.ceil(output_vbytes(change_spk) * fee_rate)

    def change_for(total_in, owed, n_inputs, change_spk, bond=None, role=None):
        fee = side_fee(n_inputs, change_spk, True, bond)
        change = total_in - owed - fee
        keep_extra = extra_to_escrow and bond is not None and role is not None
        if change >= (change_threshold(change_spk) if keep_extra else DUST_P2TR):
            return change, fee
        fee = side_fee(n_inputs, change_spk, False, bond)
        if total_in - owed - fee < 0:
            return None, fee
        if keep_extra and total_in - owed - fee > 0:
            extra_in_escrow[role] = total_in - owed - fee
            return 0, fee
        return 0, total_in - owed

    seller_change, seller_mining = change_for(
        seller_in, escrow_sats + seller_fee_sats, len(seller_utxos), seller_change_spk, seller_bond, "seller"
    )
    if seller_change is None:
        raise DepositError(f"the seller's funding is short by {escrow_sats + seller_fee_sats + seller_mining - seller_in} sats")
    buyer_change, buyer_mining = (0, 0)
    if buyer_fee_from_bond:
        buyer_mining = side_fee(0, buyer_change_spk, False, buyer_bond)
        cost = buyer_fee_sats + buyer_mining
        if buyer_bond.value - cost < DUST_P2TR:
            raise DepositError(f"the buyer's bond is too small for its fee ({cost} sats)")
        bond_in_escrow["buyer"] = buyer_bond.value - cost
        outputs[0] = TxOut(escrow_sats + held + sum(bond_in_escrow.values()), contract.script_pubkey)
    elif buyer_utxos:
        buyer_change, buyer_mining = change_for(
            buyer_in, buyer_fee_sats, len(buyer_utxos), buyer_change_spk, buyer_bond, "buyer"
        )
        if buyer_change is None:
            raise DepositError(f"the buyer's funding is short by {buyer_fee_sats + buyer_mining - buyer_in} sats")
    if extra_in_escrow:
        outputs[0] = TxOut(
            escrow_sats + held + sum(bond_in_escrow.values()) + sum(extra_in_escrow.values()),
            contract.script_pubkey,
        )
    if seller_change:
        outputs.append(TxOut(seller_change, seller_change_spk))
    if buyer_change:
        outputs.append(TxOut(buyer_change, buyer_change_spk))

    utxos = list(seller_utxos) + list(buyer_utxos)
    bond_inputs, bond_input_lists = {}, {}
    for role, bond in (("seller", seller_bond), ("buyer", buyer_bond)):
        if bond:
            bond_inputs[role] = len(utxos)
            bond_input_lists[role] = list(range(len(utxos), len(utxos) + len(bond.utxos)))
            utxos.extend(bond.utxos)
    tx = Transaction([TxIn(u.txid, u.vout) for u in utxos], outputs)
    spent = [TxOut(u.value, u.script_pubkey) for u in utxos]
    mining_fee = sum(s.value for s in spent) - sum(o.value for o in outputs)
    return DepositPlan(
        tx=tx,
        spent=spent,
        escrow_vout=0,
        seller_inputs=list(range(len(seller_utxos))),
        buyer_inputs=list(range(len(seller_utxos), len(seller_utxos) + len(buyer_utxos))),
        seller_pays=seller_in - seller_change,
        buyer_pays=(buyer_fee_sats + buyer_mining) if buyer_fee_from_bond else buyer_in - buyer_change,
        mining_fee=mining_fee,
        bond_inputs=bond_inputs,
        bond_input_lists=bond_input_lists,
        bond_in_escrow=bond_in_escrow,
        seller_mining=seller_mining,
        buyer_mining=buyer_mining,
        extra_in_escrow=extra_in_escrow,
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
    bonds_sats: int = 0,
    extra_sats: int = 0,
    my_extra: int = 0,
) -> None:
    """What a trader's client checks before signing its inputs of the deposit. extra_sats: the
    funding extras of both in the escrow (rl31-beta20); my_extra, ours (it comes back to us)."""
    if not tx.outputs or tx.outputs[0].script_pubkey != contract.script_pubkey:
        raise DepositError("the first output is not the escrow")
    if tx.outputs[0].value != escrow_sats + bonds_sats + extra_sats:
        raise DepositError("the escrow amount is wrong")
    mine_in = sum(spent[i].value for i in my_inputs)
    mine_back = sum(o.value for o in tx.outputs if o.script_pubkey == my_change_spk)
    if mine_in - mine_back - my_extra > my_max_spend:
        raise DepositError("this deposit takes more than agreed from you")
    fee = sum(s.value for s in spent) - sum(o.value for o in tx.outputs)
    if fee < 0:
        raise DepositError("outputs exceed inputs")
    est_vb = VB_OVERHEAD + len(tx.inputs) * VB_BOND_COOP + sum(
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
VB_BOND_COOP = 41 + (1 + 2 * 66 + 71 + 66) / 4
VB_BOND_REFUND = 41 + (1 + 66 + 40 + 66) / 4
VB_LEAF_RESCUE = 41 + (1 + 66 + 40 + 66) / 4


def bond_spend_tx(bond_txid: str, bond_vout: int, bond_sats: int, payout_spk: bytes, fee_rate: float, refund: bool = False, csv: int = 0) -> Transaction:
    """Spends a bond whole to one output: the penalty (to the coordinator) or a return (to the
    owner), through the cooperative leaf, or the owner's refund after the timelock."""
    return coins_spend_tx([(bond_txid, bond_vout, bond_sats)], payout_spk, fee_rate, refund, csv)


def coins_spend_vbytes(n: int, payout_spk: bytes, refund: bool = False) -> int:
    return math.ceil(VB_OVERHEAD + n * (VB_BOND_REFUND if refund else VB_BOND_COOP) + output_vbytes(payout_spk))


def coins_spend_tx(coins: list, payout_spk: bytes, fee_rate: float, refund: bool = False, csv: int = 0) -> Transaction:
    """Spends coins of one bond address [(txid, vout, sats)] whole to one output (rl31-beta18:
    a topped-up bond, or coins left at the bond address that were not the bond)."""
    if not coins:
        raise ValueError("no coins to spend")
    value = sum(c[2] for c in coins) - math.ceil(coins_spend_vbytes(len(coins), payout_spk, refund) * fee_rate)
    if value < DUST_P2TR:
        raise ValueError("the bond is too small for this fee rate")
    seq = csv if refund else SEQUENCE_RBF
    return Transaction([TxIn(t, v, seq) for t, v, _ in coins], [TxOut(value, payout_spk)])



# Fair rescue (rescue plan 2, rl31-beta16): leaf <seller> CHECKSIGVERIFY <buyer> CHECKSIGVERIFY
# <csv> CSV, two signatures, a 75-byte script and a one-step control block.
VB_LEAF_FAIR_RESCUE = 41 + (1 + 2 * 66 + 76 + 66) / 4


def rescue_tx(
    contract: EscrowContract,
    escrow_txid: str,
    escrow_vout: int,
    escrow_sats: int,
    to_seller: int,
    seller_spk: bytes,
    to_buyer: int,
    buyer_spk: bytes,
    fee_rate: float,
) -> Transaction:
    """The rescue both traders pre-sign before the deposit (rescue plan 2): valid only after
    the contract's CSV (input sequence), it gives the seller the trade amount, its bond and its
    held fee, and the buyer its bond and its held fee. Each output pays half the mining fee (the
    seller all of it when the buyer has nothing in the escrow); a buyer output that would end
    below dust is dropped (its few sats go to the miners, never to the other side)."""
    if to_seller + to_buyer != escrow_sats:
        raise ValueError("the rescue must give back the whole escrow")
    outs_spk = [seller_spk] + ([buyer_spk] if to_buyer else [])
    vb = math.ceil(VB_OVERHEAD + VB_LEAF_FAIR_RESCUE + sum(output_vbytes(s) for s in outs_spk))
    fee = math.ceil(vb * fee_rate)
    if to_buyer:
        buyer_part = fee // 2
        seller_part = fee - buyer_part
    else:
        buyer_part, seller_part = 0, fee
    outs = [TxOut(to_seller - seller_part, seller_spk)]
    if to_buyer and to_buyer - buyer_part >= DUST_P2TR:
        outs.append(TxOut(to_buyer - buyer_part, buyer_spk))
    if outs[0].value < DUST_P2TR:
        raise ValueError("the rescue fee leaves the seller below dust")
    return Transaction([TxIn(escrow_txid, escrow_vout, contract.seller_rescue_csv)], outs)
