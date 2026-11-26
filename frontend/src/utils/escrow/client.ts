import {
  individualPk,
  nonceGen,
  partialSigVerify,
  sign as musigSign,
  type Session,
} from './musig2';
import { schnorr } from '@noble/curves/secp256k1.js';

import {
  BondContract,
  EscrowContract,
  Transaction,
  addressToScript,
  bytesToHex,
  fundingScript,
  hexToBytes,
  p2trAddress,
  pubkey,
  signSchnorr,
  tokenKey,
  xonly,
  type TxOut,
} from './taproot';

export interface EscrowKeys {
  escrow: Uint8Array;
  funding: Uint8Array;
  bond: Uint8Array;
}

export interface EscrowOrderData {
  id: number;
  status?: number;
  is_maker?: boolean;
  escrow_type?: string | number;
  escrow_alias?: string;
  escrow_status_code?: number;
  escrow_my_keys_set?: boolean;
  escrow_deposit_signed?: boolean;
  escrow_release_my_nonce?: string;
  escrow_release_my_partial?: boolean;
  /** 0.9.5: we (the seller) can finish the cooperative release alone through our leaf. */
  escrow_release_leaf_ok?: boolean;
  // 0.9.7: automatic return of the bond
  bond_auto_return_address?: string;
  bond_auto_return_signed?: boolean;
  bond_auto_return_tx?: string;
  bond_auto_return_coins?: BondCoin[];
  bond_auto_return_ladder?: string[];
  bond_auto_return_rates?: number[];
  bond_status_code?: number;
  escrow_role?: 'seller' | 'buyer';
  escrow_coordinator_pubkey?: string;
  escrow_seller_pubkey?: string;
  escrow_buyer_pubkey?: string;
  escrow_rescue_csv?: number;
  escrow_address?: string;
  escrow_hrp?: string;
  escrow_sats?: number;
  escrow_output_sats?: number;
  escrow_funding_address?: string;
  escrow_funding_owed?: number;
  escrow_funding_recommended?: number;
  escrow_deposit_tx?: string;
  escrow_deposit_spent?: Array<[number, string]>;
  escrow_deposit_my_inputs?: number[];
  escrow_deposit_my_bond_input?: number | null;
  /** rl31-beta18: every input of our bond in the deposit (a topped-up bond has several). */
  escrow_deposit_my_bond_inputs?: number[];
  escrow_release_tx?: string;
  escrow_release_kind?: string;
  escrow_release_aggnonce?: string;
  escrow_my_payout_address?: string;
  bond_role?: 'maker' | 'taker';
  bond_address?: string;
  bond_required_sats?: number;
  bond_refund_csv?: number;
  bond_coordinator_pubkey?: string;
  bond_hrp?: string;
  bond_txid?: string;
  bond_vout?: number;
  bond_value?: number;
  bond_penalty_tx?: string;
  /** 0.9.12: the penalty's fee ladder (the same penalty at lower rates, ascending; the last
   * rung is the penalty itself) and whether we already signed it. */
  bond_penalty_ladder?: string[];
  bond_penalty_rates?: number[];
  bond_penalty_ladder_signed?: boolean;
  bond_return_tx?: string;
  bond_spend_txid?: string;
  bond_mempool_txid?: string;
  bond_mempool_sats?: number;
  /** rl31-beta19: the fee rate (sat/vB) the bond payment in the mempool pays (the lowest if several). */
  bond_mempool_fee_rate?: number;
  /** rl31-beta19: what a payment from our own wallet should pay (sat/vB); missing = no hint. */
  onchain_fee_hint?: { next_block: number; hour: number };
  /** rl31-beta18: the coins of the bond (several when it was topped up). */
  bond_coins?: BondCoin[];
  bond_return_coins?: BondCoin[];
  /** Waiting for the bond: what is already confirmed at its address and in how many payments it may come. */
  bond_confirmed_sats?: number;
  bond_max_coins?: number;
  /** Coins at the bond address that are not the bond (ours, back with the coordinator's signature). */
  bond_leftovers?: BondCoin[];
  bond_leftover_sats?: number;
  bond_leftover_tx?: string;
  bond_leftover_coins?: BondCoin[];
  bond_leftover_txid?: string;
  bond_leftover_returned_sats?: number;
  /** rl31-beta20: what sending the leftovers back costs now and whether it is worth it, and
   * each return already sent with its state. */
  bond_leftover_fee_sats?: number;
  bond_leftover_worth?: boolean;
  bond_leftover_returns?: Array<{
    txid: string;
    sats: number;
    spent_sats?: number;
    status: 'pending' | 'confirmed';
  }>;
  bond_leftover_returned_confirmed?: boolean;
  /** A freed bond: sats at its address beyond the bond that go back with it. */
  bond_extra_sats?: number;
  escrow_fee_from_bond?: boolean;
  escrow_fee_at_release?: boolean;
  escrow_fee_at_release_sats?: number;
  take_lightning?: boolean;
  taker_lightning?: boolean;
  hybrid_onchain_maker_bond?: boolean;
  escrow_funding_mempool_txid?: string;
  escrow_peer_funding_mempool_txids?: string[]; // 0.9.14-beta7
  escrow_funding_mempool_sats?: number;
  /** 0.9.3: what already confirmed at our funding address (while the deposit is not built). */
  escrow_funding_confirmed_sats?: number;
  escrow_funding_mempool_fee_rate?: number;
  escrow_funding_txid?: string;
  escrow_leftover_sats?: number;
  escrow_sweep_tx?: string;
  escrow_sweep_spent?: Array<[number, string]>;
  escrow_sweep_txid?: string;
  escrow_sweep_sats?: number;
  escrow_sweep_spent_sats?: number;
  escrow_sweep_status?: 'pending' | 'confirmed';
  escrow_leftover_fee_sats?: number;
  escrow_leftover_worth?: boolean;
  escrow_release_txid?: string;
  escrow_waiting_for?: '' | 'you' | 'peer' | 'confirmation' | 'coordinator';
  escrow_waiting_what?: string;
  onchain_summary?: OnchainSummary;
  escrow_release_cancel?: boolean;
  escrow_trade_sats?: number;
  escrow_bond_in_escrow?: { seller: number; buyer: number };
  /** rl31-beta20: funding excess too small for a change output, in the escrow output and
   * back to its owner with its bond (escrows with extra plan 1). */
  escrow_extra_in_escrow?: { seller: number; buyer: number };
  escrow_extra_plan?: number;
  /** 0.9.9: our deposit change goes to our payout/refund address (not our funding address). */
  escrow_change_to_payout?: boolean;
  escrow_release_coordinator_sats?: number;
  /** 2 (rl31-beta15): the coordinator fees ride in the escrow output and only the release of
   * a completed trade pays them; a collaborative cancel gives each trader its fee back. */
  escrow_fee_plan?: number;
  escrow_fees_in_escrow?: { seller: number; buyer: number };
  /** 2 (rl31-beta16): fair rescue. Both traders pre-sign, before the deposit, a rescue valid
   * after the timelock that gives each one its own; 1: the seller alone takes everything. */
  escrow_rescue_plan?: number;
  escrow_rescue_tx?: string;
  escrow_rescue_to_seller?: number;
  escrow_rescue_to_buyer?: number;
  escrow_rescue_sigs?: { seller: string; buyer: string };
  escrow_rescue_my_sig?: boolean;
  escrow_deposit_txid?: string;
}

export interface BondCoin {
  txid: string;
  vout: number;
  value: number;
}

export interface OnchainSummary {
  role: 'seller' | 'buyer';
  is_buyer: boolean;
  trade_sats: number;
  received_sats: number;
  bond_sats: number;
  bond_back_sats: number;
  extra_back_sats?: number;
  coordinator_fee_sats: number;
  fee_from_bond: boolean;
  fee_at_release?: boolean;
  lightning_bond?: boolean;
  deposit_mining_sats?: number | null;
  release_mining_sats: number;
  deposit_txid?: string;
  release_txid?: string;
  release_kind?: string;
}

export class EscrowCheckError extends Error {}

const fail = (why: string): never => {
  throw new EscrowCheckError(why);
};

export const escrowKeys = (token: string, coordinator: string, orderId: number): EscrowKeys => ({
  escrow: tokenKey(token, coordinator, orderId, 'escrow'),
  funding: tokenKey(token, coordinator, orderId, 'funding'),
  bond: tokenKey(token, coordinator, orderId, 'bond'),
});

export const publicKeys = (
  keys: EscrowKeys,
): { escrow: string; funding: string; bond: string } => ({
  escrow: bytesToHex(pubkey(keys.escrow)),
  funding: bytesToHex(pubkey(keys.funding)),
  bond: bytesToHex(pubkey(keys.bond)),
});

const spentOf = (data: EscrowOrderData): TxOut[] =>
  (data.escrow_deposit_spent ?? []).map(([v, s]) => ({ value: BigInt(v), script: hexToBytes(s) }));

export const contractOf = (data: EscrowOrderData): EscrowContract => {
  if (!data.escrow_seller_pubkey || !data.escrow_buyer_pubkey || !data.escrow_coordinator_pubkey)
    fail('the escrow keys are not complete yet');
  return new EscrowContract(
    hexToBytes(data.escrow_seller_pubkey!),
    hexToBytes(data.escrow_buyer_pubkey!),
    hexToBytes(data.escrow_coordinator_pubkey!),
    data.escrow_rescue_csv!,
    data.escrow_hrp ?? 'bc',
    data.escrow_rescue_plan ?? 1,
  );
};

/** The coordinator's escrow address must be the one our own key and the peer's give. */
export const checkEscrowAddress = (data: EscrowOrderData, keys: EscrowKeys): EscrowContract => {
  const c = contractOf(data);
  const mine = bytesToHex(pubkey(keys.escrow));
  const role = data.escrow_role;
  if ((role === 'seller' ? data.escrow_seller_pubkey : data.escrow_buyer_pubkey) !== mine)
    fail('the coordinator does not have our escrow key');
  if (c.address !== data.escrow_address) fail('the escrow address does not match our keys');
  return c;
};

export const checkFundingAddress = (data: EscrowOrderData, keys: EscrowKeys): string => {
  const addr = p2trAddress(xonly(pubkey(keys.funding)), data.escrow_hrp ?? 'bc');
  if (addr !== data.escrow_funding_address) fail('the funding address is not ours');
  return addr;
};

export const bondContractOf = (data: EscrowOrderData, keys: EscrowKeys): BondContract => {
  const c = new BondContract(
    pubkey(keys.bond),
    hexToBytes(data.bond_coordinator_pubkey!),
    data.bond_refund_csv!,
    data.bond_hrp ?? 'bc',
  );
  if (c.address !== data.bond_address) fail('the bond address does not match our key');
  return c;
};

/** The coins of our bond (one, or several when it was topped up). */
export const bondCoinsOf = (data: EscrowOrderData): BondCoin[] =>
  data.bond_coins?.length
    ? data.bond_coins
    : data.bond_txid
      ? [{ txid: data.bond_txid, vout: data.bond_vout!, value: data.bond_value! }]
      : [];

/** Signs, through the cooperative leaf, a transaction that spends exactly [coins] (all at our
 * bond address, in any order) to a single output; one signature per input. */
const signBondCoins = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  txHex: string,
  coins: BondCoin[],
  check: (tx: Transaction, total: bigint) => void,
): string[] => {
  const c = bondContractOf(data, keys);
  const tx = Transaction.parse(txHex);
  const byOutpoint = new Map(coins.map((u) => [`${u.txid}:${u.vout}`, u.value]));
  if (tx.inputs.length !== coins.length || tx.inputs.length === 0)
    fail('this transaction does not spend our bond');
  const spent: TxOut[] = tx.inputs.map((i) => {
    const v = byOutpoint.get(`${i.txid}:${i.vout}`);
    if (v === undefined) fail('this transaction does not spend our bond');
    return { value: BigInt(v!), script: c.scriptPubkey };
  });
  if (new Set(tx.inputs.map((i) => `${i.txid}:${i.vout}`)).size !== tx.inputs.length)
    fail('this transaction does not spend our bond');
  if (tx.outputs.length !== 1) fail('the transaction must have a single output');
  check(
    tx,
    spent.reduce((a, s) => a + s.value, BigInt(0)),
  );
  return tx.inputs.map((_, i) =>
    bytesToHex(
      signSchnorr(keys.bond, tx.sighashUnified(i, spent, c.leafHashes[BondContract.COOPERATIVE])),
    ),
  );
};

/** Signature(s) of the penalty given in advance: it only ever spends this bond's coins. */
export const signBondPenalties = (data: EscrowOrderData, keys: EscrowKeys): string[] =>
  signBondCoins(data, keys, data.bond_penalty_tx!, bondCoinsOf(data), () => {});

export const signBondPenalty = (data: EscrowOrderData, keys: EscrowKeys): string =>
  signBondPenalties(data, keys)[0];

/** 0.9.12: the penalty's fee ladder: every rung spends exactly our bond's coins to the same
 * single output as the top rung (the coordinator's, as the penalty itself), so signing the
 * cheaper ones gives nothing more away; the coordinator broadcasts the one the mempool needs.
 * When the penalty itself is known (SIGN), the top rung must be it. One list per rung. */
export const signBondPenaltyLadder = (data: EscrowOrderData, keys: EscrowKeys): string[][] => {
  const ladder = data.bond_penalty_ladder ?? [];
  if (ladder.length === 0) return [];
  const topHex = ladder[ladder.length - 1];
  if (data.bond_penalty_tx && data.bond_penalty_tx !== topHex)
    fail('the penalty ladder does not end with the penalty');
  const top = Transaction.parse(topHex);
  if (top.outputs.length !== 1) fail('the transaction must have a single output');
  const topScript = bytesToHex(top.outputs[0].script);
  return ladder.map((tx) =>
    signBondCoins(data, keys, tx, bondCoinsOf(data), (t) => {
      if (bytesToHex(t.outputs[0].script) !== topScript)
        fail('a penalty rung does not pay the penalty address');
    }),
  );
};

const paysOnlyUs =
  (myScript: Uint8Array, maxFeeSats: number, what: string) =>
  (tx: Transaction, total: bigint): void => {
    if (bytesToHex(tx.outputs[0].script) !== bytesToHex(myScript))
      fail(`the ${what} does not pay our address`);
    if (total - tx.outputs[0].value > BigInt(maxFeeSats)) fail(`the ${what} fee is too high`);
  };

/** Return of a freed bond (and anything else at its address): everything (minus the fee) to
 * our own address. One signature per coin. */
export const signBondReturns = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  myScript: Uint8Array,
  maxFeeSats: number,
): string[] =>
  signBondCoins(
    data,
    keys,
    data.bond_return_tx!,
    data.bond_return_coins?.length ? data.bond_return_coins : bondCoinsOf(data),
    paysOnlyUs(myScript, maxFeeSats, 'return'),
  );

export const signBondReturn = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  myScript: Uint8Array,
  maxFeeSats: number,
): string => signBondReturns(data, keys, myScript, maxFeeSats)[0];

/** 0.9.7: the automatic return of our bond, signed in advance (with the penalty or when the
 * return address arrives): all its coins to OUR return address, at most maxFeeSats in fees.
 * The coordinator may only broadcast it once the bond is free. */
export const signBondAutoReturn = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  myScript: Uint8Array,
  maxFeeSats: number,
): string[] => {
  const coins = data.bond_auto_return_coins ?? [];
  const bond = bondCoinsOf(data);
  // exactly the bond's coins, nothing else
  const key = (u: BondCoin): string => `${u.txid}:${u.vout}:${u.value}`;
  if (coins.length !== bond.length || coins.some((u) => !bond.map(key).includes(key(u))))
    fail('the automatic return does not spend exactly our bond');
  return signBondCoins(
    data,
    keys,
    data.bond_auto_return_tx!,
    coins,
    paysOnlyUs(myScript, maxFeeSats, 'automatic return'),
  );
};

/** 0.9.11: the fee ladder of the automatic return (the same return at lower rates, ascending;
 * the last rung is bond_auto_return_tx). Every rung is checked like the top one. */
export const signBondAutoReturnLadder = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  myScript: Uint8Array,
  maxFeeSats: number,
): string[][] =>
  (data.bond_auto_return_ladder ?? []).map((tx) =>
    signBondAutoReturn({ ...data, bond_auto_return_tx: tx }, keys, myScript, maxFeeSats),
  );

/** rl31-beta18: coins at our bond address that are not the bond, back to our address. */
export const signBondLeftover = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  myScript: Uint8Array,
  maxFeeSats: number,
): string[] => {
  const coins = data.bond_leftover_coins ?? [];
  const bond = new Set(bondCoinsOf(data).map((u) => `${u.txid}:${u.vout}`));
  // never sign away the bond itself through this path
  if (
    [1, 2].includes(data.bond_status_code ?? -1) &&
    coins.some((u) => bond.has(`${u.txid}:${u.vout}`))
  )
    fail('this transaction spends our bond');
  return signBondCoins(
    data,
    keys,
    data.bond_leftover_tx!,
    coins,
    paysOnlyUs(myScript, maxFeeSats, 'return'),
  );
};

/** Most the deposit may take from us: the recommended funding (which the coordinator
 * recomputes from the live fee rate, so it drifts by a few sats after we paid: order #21 on
 * 17 nov 2026 failed by 1 sat) plus a small tolerance, plus our bond. Never unlimited. */
export const depositSpendLimit = (data: EscrowOrderData): number => {
  const recommended = data.escrow_funding_recommended ?? 0;
  const tolerance = Math.max(546, Math.ceil(recommended * 0.01));
  return recommended + tolerance + (data.bond_value ?? 0);
};

export interface DepositLimits {
  maxSpendSats: number;
  maxFeeRate: number;
}

/** rl31-beta20: our funding extra in the escrow (0 when none or an older coordinator). */
export const myExtraInEscrow = (data: EscrowOrderData): number => {
  const ex = data.escrow_extra_in_escrow;
  if (!ex) return 0;
  return (data.escrow_role === 'buyer' ? ex.buyer : ex.seller) ?? 0;
};

/** The largest extra a deposit can leave in the escrow: below dust plus a change output's cost
 * it would have been change instead (with rounding room). */
export const maxExtraSats = (maxFeeRate: number): number =>
  330 + 2 * Math.ceil(43 * maxFeeRate) + 2;

/** Scripts where the deposit may send our change: our funding address and (0.9.9) our
 * payout/refund address, which this app gave. */
export const myChangeScripts = (data: EscrowOrderData, keys: EscrowKeys): Set<string> => {
  const out = new Set<string>([bytesToHex(fundingScript(pubkey(keys.funding)))]);
  if (data.escrow_my_payout_address) {
    try {
      out.add(bytesToHex(addressToScript(data.escrow_my_payout_address)));
    } catch {
      // not an address we can read: only the funding address counts
    }
  }
  return out;
};

/** Checks the joint deposit and signs our funding inputs (and our bond input). */
export const signDeposit = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  bondData: EscrowOrderData | null,
  limits: DepositLimits,
): { signatures: string[]; bond_signature?: string; bond_signatures?: string[] } => {
  const c = checkEscrowAddress(data, keys);
  const tx = Transaction.parse(data.escrow_deposit_tx!);
  const spent = spentOf(data);
  if (spent.length !== tx.inputs.length) fail('inputs and spent outputs do not match');
  const myFunding = fundingScript(pubkey(keys.funding));
  const mine = data.escrow_deposit_my_inputs ?? [];
  const bondIndexes = myBondIndexes(data);
  let bondValue = BigInt(0);
  let bond: BondContract | null = null;
  if (bondIndexes.length) {
    if (!bondData) fail('missing our bond');
    bond = bondContractOf(bondData!, keys);
    for (const bi of bondIndexes) {
      if (bi < 0 || bi >= spent.length || mine.includes(bi)) fail('the bond input is not our bond');
      if (bytesToHex(spent[bi].script) !== bytesToHex(bond.scriptPubkey))
        fail('the bond input is not our bond');
      bondValue += spent[bi].value;
    }
    if (new Set(bondIndexes).size !== bondIndexes.length) fail('the bond input is not our bond');
  }
  for (const i of mine)
    if (bytesToHex(spent[i].script) !== bytesToHex(myFunding))
      fail('an input marked as ours is not our funding');
  if (bytesToHex(tx.outputs[0].script) !== bytesToHex(c.scriptPubkey))
    fail('the first output is not the escrow');
  if (tx.outputs[0].value < BigInt(data.escrow_sats!) + bondValue)
    fail('the escrow output is too small');
  const totalIn = spent.reduce((a, s) => a + s.value, BigInt(0));
  const totalOut = tx.outputs.reduce((a, o) => a + o.value, BigInt(0));
  if (totalOut > totalIn) fail('outputs exceed inputs');
  const myIn = mine.reduce((a, i) => a + spent[i].value, BigInt(0)) + bondValue;
  const backTo = myChangeScripts(data, keys);
  const myBack = tx.outputs
    .slice(1)
    .filter((o) => backTo.has(bytesToHex(o.script)))
    .reduce((a, o) => a + o.value, BigInt(0));
  // rl31-beta20: our funding excess too small for a change output rides in the escrow and
  // comes back with our bond (the release and the rescue are checked to give it back). Never
  // more than a change output would have been worth at the most fee rate we accept.
  const myExtra = BigInt(myExtraInEscrow(data));
  if (myExtra < BigInt(0) || myExtra > BigInt(maxExtraSats(limits.maxFeeRate)))
    fail('this deposit takes more than agreed from us');
  if (myExtra > BigInt(0) && tx.outputs[0].value < BigInt(data.escrow_sats!) + bondValue + myExtra)
    fail('the escrow output is too small');
  if (myIn - myBack - myExtra > BigInt(limits.maxSpendSats))
    fail('this deposit takes more than agreed from us');
  const vbytes =
    11 + tx.inputs.length * 110 + tx.outputs.reduce((a, o) => a + 9 + o.script.length, 0);
  if (Number(totalIn - totalOut) > vbytes * limits.maxFeeRate) fail('the mining fee is too high');
  const signatures = mine.map((i) =>
    bytesToHex(signSchnorr(keys.funding, tx.sighashUnified(i, spent))),
  );
  const out: { signatures: string[]; bond_signature?: string; bond_signatures?: string[] } = {
    signatures,
  };
  if (bond && bondIndexes.length) {
    out.bond_signatures = bondIndexes.map((bi) =>
      bytesToHex(
        signSchnorr(
          keys.bond,
          tx.sighashUnified(bi, spent, bond!.leafHashes[BondContract.COOPERATIVE]),
        ),
      ),
    );
    out.bond_signature = out.bond_signatures[0];
  }
  return out;
};

/** Our bond's input indexes in the deposit (several when the bond was topped up). */
export const myBondIndexes = (data: EscrowOrderData): number[] => {
  if (data.escrow_deposit_my_bond_inputs?.length) return data.escrow_deposit_my_bond_inputs;
  const bi = data.escrow_deposit_my_bond_input;
  return bi !== null && bi !== undefined ? [bi] : [];
};

/** Most the pre-signed rescue may spend on mining (it pays a margin over today's rate). */
export const MAX_RESCUE_FEE_SATS = 5000;

/** The fair rescue (plan 2): spends exactly the escrow output of this deposit, only after the
 * timelock, and pays us at least what we put into the escrow less our share of the deposit
 * and rescue mining fees. Checked before signing it and again before keeping it. */
export const checkRescue = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  bondData: EscrowOrderData | null,
): { tx: Transaction; c: EscrowContract; msg: Uint8Array; spent: TxOut[] } => {
  if ((data.escrow_rescue_plan ?? 1) < 2 || !data.escrow_rescue_tx)
    fail('there is no rescue to sign');
  const c = checkEscrowAddress(data, keys);
  const deposit = Transaction.parse(data.escrow_deposit_tx!);
  if (bytesToHex(deposit.outputs[0].script) !== bytesToHex(c.scriptPubkey))
    fail('the first output is not the escrow');
  const escrowValue = deposit.outputs[0].value;
  const tx = Transaction.parse(data.escrow_rescue_tx!);
  if (
    tx.inputs.length !== 1 ||
    tx.inputs[0].txid !== deposit.txid() ||
    tx.inputs[0].vout !== 0 ||
    tx.inputs[0].sequence !== c.rescueCsv ||
    tx.version !== 2 ||
    tx.locktime !== 0
  )
    fail('the rescue does not spend this escrow after the timelock');
  const out = tx.outputs.reduce((a, o) => a + o.value, BigInt(0));
  if (out > escrowValue || escrowValue - out > BigInt(MAX_RESCUE_FEE_SATS))
    fail('the rescue mining fee is too high');
  if (!data.escrow_my_payout_address) fail('we have no payout address yet');
  const mine = bytesToHex(addressToScript(data.escrow_my_payout_address!));
  const toMe = tx.outputs
    .filter((o) => bytesToHex(o.script) === mine)
    .reduce((a, o) => a + o.value, BigInt(0));
  // What we put into the escrow: our funding inputs and our bond, less our change.
  const spent = spentOf(data);
  const myFunding = bytesToHex(fundingScript(pubkey(keys.funding)));
  let myIn = (data.escrow_deposit_my_inputs ?? []).reduce((a, i) => a + spent[i].value, BigInt(0));
  const bis = myBondIndexes(data);
  if (bis.length) {
    const bond = bondContractOf(bondData ?? data, keys);
    for (const bi of bis) {
      if (bytesToHex(spent[bi].script) !== bytesToHex(bond.scriptPubkey))
        fail('the bond input is not our bond');
      myIn += spent[bi].value;
    }
  }
  const backTo = myChangeScripts(data, keys);
  backTo.add(myFunding);
  const myBack = deposit.outputs
    .slice(1)
    .filter((o) => backTo.has(bytesToHex(o.script)))
    .reduce((a, o) => a + o.value, BigInt(0));
  const totalIn = spent.reduce((a, x) => a + x.value, BigInt(0));
  const depositFee = totalIn - deposit.outputs.reduce((a, o) => a + o.value, BigInt(0));
  const owed = myIn - myBack - depositFee - (escrowValue - out);
  if (owed > BigInt(0) && toMe < owed) fail('the rescue does not give us back what we put in');
  const escrowSpentOut: TxOut[] = [{ value: escrowValue, script: c.scriptPubkey }];
  const msg = tx.sighashUnified(0, escrowSpentOut, c.leafHashes[EscrowContract.RESCUE]);
  return { tx, c, msg, spent: escrowSpentOut };
};

export const signRescue = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  bondData: EscrowOrderData | null,
): string => bytesToHex(signSchnorr(keys.escrow, checkRescue(data, keys, bondData).msg));

/** The complete rescue (both signatures, both checked), ready to broadcast after the
 * timelock without the coordinator. */
export const completeRescue = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  bondData: EscrowOrderData | null,
): { hex: string; txid: string; toMe: number } => {
  const { tx, c, msg } = checkRescue(data, keys, bondData);
  const sigs = data.escrow_rescue_sigs;
  if (!sigs?.seller || !sigs?.buyer) fail('the rescue is not signed by both yet');
  const sig = (role: 'seller' | 'buyer'): Uint8Array => {
    const raw = hexToBytes(sigs![role]);
    const pk = role === 'seller' ? c.sellerPk : c.buyerPk;
    if (raw.length !== 65 || raw[64] !== 0x21 || !schnorr.verify(raw.slice(0, 64), msg, xonly(pk)))
      fail('a rescue signature does not verify');
    return raw;
  };
  const witness = [sig('buyer'), sig('seller'), c.leaves[EscrowContract.RESCUE], c.controlBlock(2)];
  const mine = bytesToHex(addressToScript(data.escrow_my_payout_address!));
  const toMe = tx.outputs
    .filter((o) => bytesToHex(o.script) === mine)
    .reduce((a, o) => a + Number(o.value), 0);
  return { hex: bytesToHex(tx.serialize([witness])), txid: tx.txid(), toMe };
};

export interface ReleaseCheck {
  myScript: Uint8Array;
  minToMe: number;
  /** When set, the release must pay us exactly this (less the mining fee if we pay it). */
  owed?: { due: number; paysFee: boolean };
}

// Most the release may leave to miners when we pay it: ~200 vB at 25 sat/vB.
export const MAX_RELEASE_FEE_SATS = 5000;

/** What a cooperative release (trade or collaborative cancel) owes us, to the sat, from the
 * amounts the coordinator states, after checking them against what we know ourselves. Null when
 * the coordinator does not state them (older servers) or for dispute releases. */
export const owedInRelease = (data: EscrowOrderData): { due: number; paysFee: boolean } | null => {
  const bonds = data.escrow_bond_in_escrow;
  const trade = data.escrow_trade_sats;
  if (
    !bonds ||
    trade === undefined ||
    !(data.escrow_release_kind ?? '').startsWith('Seller and buyer')
  )
    return null;
  const role = data.escrow_role;
  // fee plan 2: each trader's fee is inside the escrow output until the release
  const held =
    (data.escrow_fee_plan ?? 1) >= 2
      ? (data.escrow_fees_in_escrow ?? { seller: 0, buyer: 0 })
      : { seller: 0, buyer: 0 };
  if (held.seller < 0 || held.buyer < 0)
    fail('the amounts of the release do not add up to the escrow');
  const ex = data.escrow_extra_in_escrow ?? { seller: 0, buyer: 0 };
  if (ex.seller < 0 || ex.buyer < 0) fail('the amounts of the release do not add up to the escrow');
  if (
    trade + bonds.seller + bonds.buyer + held.seller + held.buyer + ex.seller + ex.buyer !==
    data.escrow_output_sats
  )
    fail('the amounts of the release do not add up to the escrow');
  const myHeld = role === 'seller' ? held.seller : held.buyer;
  // our fee inside the escrow is what we agreed to pay: what we owed beyond the trade amount
  if ((data.escrow_fee_plan ?? 1) >= 2 && data.escrow_funding_owed !== undefined) {
    const agreed =
      role === 'seller'
        ? data.escrow_funding_owed - (data.escrow_sats ?? trade)
        : data.escrow_funding_owed;
    if (myHeld !== agreed) fail('the release does not give our fee back');
  }
  if (role === 'buyer' && data.escrow_fee_at_release && held.buyer !== 0)
    fail('the amounts of the release do not add up to the escrow');
  if (data.escrow_sats !== undefined && trade !== data.escrow_sats)
    fail('the release does not keep the trade amount');
  const myBond = role === 'seller' ? bonds.seller : bonds.buyer;
  if (data.bond_value !== undefined && data.bond_txid) {
    if (myBond > data.bond_value) fail('the release counts more bond than we posted');
    const mayLose = data.escrow_fee_from_bond ? (data.escrow_funding_recommended ?? 0) : 0;
    if (myBond < data.bond_value - mayLose) fail('the release does not give our bond back');
    if (data.escrow_fee_from_bond && myBond + myHeld > data.bond_value)
      fail('the release counts more bond than we posted');
  }
  const atRelease = data.escrow_release_coordinator_sats ?? 0;
  if (atRelease && atRelease !== (data.escrow_fee_at_release_sats ?? -1))
    fail('the release charges a fee we did not agree');
  // our funding extra (rl31-beta20) comes back with our bond, whatever the release is
  if (data.escrow_release_cancel)
    return role === 'seller'
      ? { due: trade + bonds.seller + held.seller + ex.seller, paysFee: true }
      : { due: bonds.buyer + held.buyer + ex.buyer, paysFee: false };
  return role === 'buyer'
    ? { due: trade + bonds.buyer + ex.buyer - atRelease, paysFee: true }
    : { due: bonds.seller + ex.seller, paysFee: false };
};

const checkRelease = (
  data: EscrowOrderData,
  check: ReleaseCheck,
): { tx: Transaction; c: EscrowContract } => {
  const c = contractOf(data);
  const tx = Transaction.parse(data.escrow_release_tx!);
  const toMe = tx.outputs
    .filter((o) => bytesToHex(o.script) === bytesToHex(check.myScript))
    .reduce((a, o) => a + o.value, BigInt(0));
  if (check.owed) {
    const out = tx.outputs.reduce((a, o) => a + o.value, BigInt(0));
    const fee = BigInt(data.escrow_output_sats!) - out;
    if (fee < BigInt(0) || fee > BigInt(MAX_RELEASE_FEE_SATS))
      fail('the release mining fee is too high');
    const expect = BigInt(check.owed.due) - (check.owed.paysFee ? fee : BigInt(0));
    if (toMe !== expect) fail('the release does not pay us exactly what we are owed');
    return { tx, c };
  }
  if (toMe < BigInt(check.minToMe)) fail('the release pays us less than agreed');
  return { tx, c };
};

const escrowSpent = (data: EscrowOrderData, c: EscrowContract): TxOut[] => [
  { value: BigInt(data.escrow_output_sats!), script: c.scriptPubkey },
];

const releaseSession = (
  data: EscrowOrderData,
  tx: Transaction,
  c: EscrowContract,
  aggnonce: Uint8Array,
): Session => ({
  aggnonce,
  pubkeys: c.musigPubkeys(),
  tweaks: [c.tweak],
  isXonly: [true],
  msg: tx.sighashUnified(0, escrowSpent(data, c)),
});

/** Round 1 of the cooperative release. Keep the secret nonce in memory only. */
export const releaseNonce = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  check: ReleaseCheck,
): { secnonce: Uint8Array; pubnonce: string } => {
  const { tx, c } = checkRelease(data, check);
  const msg = tx.sighashUnified(0, escrowSpent(data, c));
  const n = nonceGen(keys.escrow, individualPk(keys.escrow), c.outputKey, msg);
  return { secnonce: n.secnonce, pubnonce: bytesToHex(n.pubnonce) };
};

/** Round 2: our partial signature over the same release, once both nonces are in. */
export const releasePartial = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  check: ReleaseCheck,
  secnonce: Uint8Array,
): string => {
  const { tx, c } = checkRelease(data, check);
  const session = releaseSession(data, tx, c, hexToBytes(data.escrow_release_aggnonce!));
  return bytesToHex(musigSign(secnonce, keys.escrow, session));
};

/** Dispute won: our signature on the leaf with the coordinator. */
export const signDisputeRelease = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  check: ReleaseCheck,
): string => {
  const { tx, c } = checkRelease(data, check);
  const leaf =
    data.escrow_role === 'buyer'
      ? EscrowContract.BUYER_COORDINATOR
      : EscrowContract.SELLER_COORDINATOR;
  const msg = tx.sighashUnified(0, escrowSpent(data, c), c.leafHashes[leaf]);
  return bytesToHex(signSchnorr(keys.escrow, msg));
};

/** 0.9.5: the seller signs the cooperative release (trade or collaborative cancel) alone, on
 * its leaf with the coordinator: one signature, no nonces, the buyer signs nothing. The release
 * is checked exactly as for the two-round signing. */
export const signCooperativeLeaf = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  check: ReleaseCheck,
): string => {
  if (data.escrow_role !== 'seller') fail('only the seller signs the release through its leaf');
  if (!(data.escrow_release_kind ?? '').startsWith('Seller and buyer'))
    fail('this is not a cooperative release');
  const { tx, c } = checkRelease(data, check);
  const msg = tx.sighashUnified(
    0,
    escrowSpent(data, c),
    c.leafHashes[EscrowContract.SELLER_COORDINATOR],
  );
  return bytesToHex(signSchnorr(keys.escrow, msg));
};

export { partialSigVerify };

/** Sweep of what is left on our own funding address: every input must be our funding, the only
 * output our address, and the fee at most maxFeeSats. One signature per input. */
export const signSweep = (
  data: EscrowOrderData,
  keys: EscrowKeys,
  myScript: Uint8Array,
  maxFeeSats: number,
): string[] => {
  const tx = Transaction.parse(data.escrow_sweep_tx!);
  const spent: TxOut[] = (data.escrow_sweep_spent ?? []).map(([v, spk]) => ({
    value: BigInt(v),
    script: hexToBytes(spk),
  }));
  if (spent.length !== tx.inputs.length) fail('inputs and spent outputs do not match');
  const myFunding = bytesToHex(fundingScript(pubkey(keys.funding)));
  for (const sp of spent)
    if (bytesToHex(sp.script) !== myFunding) fail('an input is not our funding');
  if (tx.outputs.length !== 1 || bytesToHex(tx.outputs[0].script) !== bytesToHex(myScript))
    fail('the sweep does not pay our address');
  const totalIn = spent.reduce((a, sp) => a + sp.value, BigInt(0));
  if (totalIn - tx.outputs[0].value > BigInt(maxFeeSats)) fail('the sweep fee is too high');
  return tx.inputs.map((_, i) =>
    bytesToHex(signSchnorr(keys.funding, tx.sighashUnified(i, spent))),
  );
};
