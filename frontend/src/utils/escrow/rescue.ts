// Rescue without the coordinator (Roblosats, rl31-beta12).
//
// While an onchain order runs, the app keeps on this device what it needs to take its sats back
// on its own if the coordinator disappears: the public data of the contracts (keys, timelocks,
// outpoints). The secret keys are never stored: they come from the robot token, as always.
// What can be recovered alone:
//  - the funding address (rawtr of our funding key): at any time;
//  - our bond while it is not inside the deposit: leaf <owner> CSV after refund_csv blocks;
//  - the escrow, by the seller only: leaf <seller> CSV after rescue_csv blocks from the deposit
//    (rescue plan 1, escrows up to rl31-beta15);
//  - rescue plan 2 (rl31-beta16): the rescue both traders pre-signed before the deposit, kept
//    here complete (both signatures): after rescue_csv blocks either side broadcasts it and
//    each one gets its own (seller: trade + its bond + its fee; buyer: its bond + its fee).
import { xchacha20poly1305 } from '@noble/ciphers/chacha.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, randomBytes } from '@noble/hashes/utils.js';

import { type EscrowKeys, type EscrowOrderData, escrowKeys } from './client';
import {
  BondContract,
  EscrowContract,
  Transaction,
  dustThreshold,
  fundingScript,
  hexToBytes,
  p2trAddress,
  pubkey,
  signSchnorr,
  xonly,
  type TxOut,
} from './taproot';

export interface RescueRecord {
  v: 1;
  shortAlias: string;
  coordinator: string;
  orderId: number;
  hrp: string;
  savedAt: number;
  role?: 'seller' | 'buyer';
  escrow?: {
    sellerPk: string;
    buyerPk: string;
    coordinatorPk: string;
    rescueCsv: number;
    address: string;
    outputSats?: number;
    depositTxid?: string;
    rescuePlan?: number;
    /** Plan 2: the complete pre-signed rescue (hex), its txid and what it pays us. */
    rescueTx?: string;
    rescueTxid?: string;
    rescueToMe?: number;
  };
  bond?: {
    coordinatorPk: string;
    refundCsv: number;
    address: string;
    txid?: string;
    vout?: number;
    value?: number;
  };
  fundingAddress?: string;
}

export interface Utxo {
  txid: string;
  vout: number;
  value: number;
}

export class RescueError extends Error {}

const enc = new TextEncoder();
const storageKey = (token: string): string =>
  `onchain_rescue_${bytesToHex(sha256(enc.encode(`roblosats-rescue-id/${token}`))).slice(0, 16)}`;
const cipherKey = (token: string): Uint8Array =>
  hkdf(sha256, enc.encode(token), new Uint8Array(32), enc.encode('roblosats/rescue/v1'), 32);
const b64 = (b: Uint8Array): string => btoa(String.fromCharCode(...b));
const unb64 = (s: string): Uint8Array => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

export const encryptRecords = (token: string, recs: Record<string, RescueRecord>): string => {
  const nonce = randomBytes(24);
  const ct = xchacha20poly1305(cipherKey(token), nonce).encrypt(enc.encode(JSON.stringify(recs)));
  return `1:${b64(nonce)}:${b64(ct)}`;
};

export const decryptRecords = (token: string, blob?: string): Record<string, RescueRecord> => {
  if (!blob) return {};
  try {
    const [v, n, c] = blob.split(':');
    if (v !== '1') return {};
    const pt = xchacha20poly1305(cipherKey(token), unb64(n)).decrypt(unb64(c));
    return JSON.parse(new TextDecoder().decode(pt)) as Record<string, RescueRecord>;
  } catch {
    return {};
  }
};

/** What the order data tells us that a rescue needs (null when there is nothing yet). */
export interface SignedRescue {
  hex: string;
  txid: string;
  toMe: number;
}

export const recordFromOrder = (
  data: EscrowOrderData,
  shortAlias: string,
  prev?: RescueRecord,
  rescue?: SignedRescue,
): RescueRecord | null => {
  const coordinator = data.escrow_alias;
  if (!coordinator) return null;
  const rec: RescueRecord = {
    ...(prev ?? {}),
    v: 1,
    shortAlias,
    coordinator,
    orderId: data.id,
    hrp: data.escrow_hrp ?? data.bond_hrp ?? prev?.hrp ?? 'bc',
    savedAt: Date.now(),
  };
  if (data.escrow_role) rec.role = data.escrow_role;
  if (data.escrow_seller_pubkey && data.escrow_buyer_pubkey && data.escrow_coordinator_pubkey) {
    rec.escrow = {
      ...(prev?.escrow ?? {}),
      sellerPk: data.escrow_seller_pubkey,
      buyerPk: data.escrow_buyer_pubkey,
      coordinatorPk: data.escrow_coordinator_pubkey,
      rescueCsv: data.escrow_rescue_csv ?? 0,
      address: data.escrow_address ?? '',
    };
    if (data.escrow_output_sats) rec.escrow.outputSats = data.escrow_output_sats;
    if (data.escrow_funding_txid) rec.escrow.depositTxid = data.escrow_funding_txid;
    if (data.escrow_rescue_plan) rec.escrow.rescuePlan = data.escrow_rescue_plan;
    if (rescue) {
      rec.escrow.rescueTx = rescue.hex;
      rec.escrow.rescueTxid = rescue.txid;
      rec.escrow.rescueToMe = rescue.toMe;
      if (data.escrow_deposit_txid) rec.escrow.depositTxid = data.escrow_deposit_txid;
    }
  }
  if (data.bond_address && data.bond_coordinator_pubkey && data.bond_refund_csv) {
    rec.bond = {
      ...(prev?.bond ?? {}),
      coordinatorPk: data.bond_coordinator_pubkey,
      refundCsv: data.bond_refund_csv,
      address: data.bond_address,
    };
    if (data.bond_txid) {
      rec.bond.txid = data.bond_txid;
      rec.bond.vout = data.bond_vout;
      rec.bond.value = data.bond_value;
    }
  }
  if (data.escrow_funding_address) rec.fundingAddress = data.escrow_funding_address;
  if (!rec.escrow && !rec.bond && !rec.fundingAddress) return null;
  return rec;
};

export const recordKey = (shortAlias: string, orderId: number): string =>
  `${shortAlias}/${orderId}`;

interface Storage {
  getItem: (key: string) => Promise<string | undefined>;
  setItem: (key: string, value: string) => void;
}

/** Keep (merge) the rescue data of one order of this robot. Returns true when it changed. */
export const saveRescue = async (
  storage: Storage,
  token: string,
  data: EscrowOrderData,
  shortAlias: string,
  rescue?: SignedRescue,
): Promise<boolean> => {
  const key = storageKey(token);
  const recs = decryptRecords(token, await storage.getItem(key));
  const k = recordKey(shortAlias, data.id);
  const rec = recordFromOrder(data, shortAlias, recs[k], rescue);
  if (!rec) return false;
  const strip = (r?: RescueRecord): string => JSON.stringify({ ...(r ?? {}), savedAt: 0 });
  if (strip(rec) === strip(recs[k])) return false;
  recs[k] = rec;
  storage.setItem(key, encryptRecords(token, recs));
  return true;
};

export const loadRescues = async (storage: Storage, token: string): Promise<RescueRecord[]> =>
  Object.values(decryptRecords(token, await storage.getItem(storageKey(token)))).sort(
    (a, b) => b.orderId - a.orderId,
  );

export const keysOf = (token: string, rec: RescueRecord): EscrowKeys =>
  escrowKeys(token, rec.coordinator, rec.orderId);

const check = (ok: boolean, why: string): void => {
  if (!ok) throw new RescueError(why);
};

export const escrowContractOf = (rec: RescueRecord): EscrowContract => {
  const e = rec.escrow!;
  const c = new EscrowContract(
    hexToBytes(e.sellerPk),
    hexToBytes(e.buyerPk),
    hexToBytes(e.coordinatorPk),
    e.rescueCsv,
    rec.hrp,
    e.rescuePlan ?? 1,
  );
  check(!e.address || c.address === e.address, 'the stored escrow does not match its keys');
  return c;
};

export const bondContractOfRecord = (rec: RescueRecord, keys: EscrowKeys): BondContract => {
  const b = rec.bond!;
  const c = new BondContract(pubkey(keys.bond), hexToBytes(b.coordinatorPk), b.refundCsv, rec.hrp);
  check(c.address === b.address, 'the stored bond does not match our key');
  return c;
};

export const fundingAddressOf = (rec: RescueRecord, keys: EscrowKeys): string =>
  p2trAddress(xonly(pubkey(keys.funding)), rec.hrp);

// Virtual sizes (rounded up) of what we build: one output to a P2TR/P2WSH-sized script.
const VB_OUT = 43;
const VB_BASE = 11;
const VB_LEAF_CSV_IN = 41 + Math.ceil((1 + 66 + 1 + 38 + 1 + 65) / 4); // sig, script, cb
const VB_ESCROW_LEAF_IN = 41 + Math.ceil((1 + 66 + 1 + 38 + 1 + 65) / 4);
const VB_KEY_IN = 58;

const feeFor = (vbytes: number, rate: number): bigint => BigInt(Math.ceil(vbytes * rate));

const singleOutput = (inputs: Utxo[], seq: number, dest: Uint8Array, value: bigint): Transaction =>
  new Transaction(
    inputs.map((u) => ({ txid: u.txid, vout: u.vout, sequence: seq })),
    [{ value, script: dest }],
    2,
    0,
  );

export interface BuiltRescue {
  hex: string;
  txid: string;
  fee: number;
  sent: number;
}

/** The seller takes the whole escrow alone through leaf 2 once rescue_csv blocks passed. */
export const buildEscrowRescue = (
  rec: RescueRecord,
  keys: EscrowKeys,
  utxo: Utxo,
  dest: Uint8Array,
  feeRate: number,
): BuiltRescue => {
  check(Boolean(rec.escrow), 'no escrow stored for this order');
  const c = escrowContractOf(rec);
  check(
    bytesToHex(pubkey(keys.escrow)) === rec.escrow!.sellerPk,
    'only the seller can rescue the escrow alone',
  );
  check((rec.escrow!.rescuePlan ?? 1) < 2, 'this escrow is rescued with the pre-signed rescue');
  const fee = feeFor(VB_BASE + VB_ESCROW_LEAF_IN + VB_OUT, feeRate);
  const value = BigInt(utxo.value) - fee;
  check(value >= BigInt(dustThreshold(dest)), 'the escrow does not cover the mining fee');
  const tx = singleOutput([utxo], c.rescueCsv, dest, value);
  const spent: TxOut[] = [{ value: BigInt(utxo.value), script: c.scriptPubkey }];
  const sig = signSchnorr(
    keys.escrow,
    tx.sighashUnified(0, spent, c.leafHashes[EscrowContract.SELLER_RESCUE]),
  );
  const hex = bytesToHex(
    tx.serialize([[sig, c.leaves[EscrowContract.SELLER_RESCUE], c.controlBlock(2)]]),
  );
  return { hex, txid: tx.txid(), fee: Number(fee), sent: Number(value) };
};

/** Plan 2: the complete pre-signed rescue kept on this device (nothing to sign, no fee to
 * choose: it was fixed before the deposit). */
export const storedFairRescue = (rec: RescueRecord): BuiltRescue => {
  const e = rec.escrow;
  check(Boolean(e?.rescueTx), 'the complete rescue is not saved on this device');
  const c = escrowContractOf(rec);
  const tx = Transaction.parse(e!.rescueTx!);
  check(
    tx.inputs.length === 1 &&
      (!e!.depositTxid || tx.inputs[0].txid === e!.depositTxid) &&
      tx.inputs[0].sequence === c.rescueCsv,
    'the saved rescue does not spend this escrow',
  );
  const out = tx.outputs.reduce((a, o) => a + Number(o.value), 0);
  return {
    hex: e!.rescueTx!,
    txid: tx.txid(),
    fee: e!.outputSats ? e!.outputSats - out : 0,
    sent: e!.rescueToMe ?? 0,
  };
};

/** Our bond back alone through its refund leaf once refund_csv blocks passed: every coin at
 * the bond address we pass (the bond, a top-up, or sats that were never the bond, rl31-beta18),
 * each with its own timelock already passed. */
export const buildBondRefund = (
  rec: RescueRecord,
  keys: EscrowKeys,
  utxos: Utxo | Utxo[],
  dest: Uint8Array,
  feeRate: number,
): BuiltRescue => {
  check(Boolean(rec.bond), 'no bond stored for this order');
  const coins = Array.isArray(utxos) ? utxos : [utxos];
  check(coins.length > 0, 'nothing at the bond address');
  const c = bondContractOfRecord(rec, keys);
  const total = coins.reduce((a, u) => a + BigInt(u.value), BigInt(0));
  const fee = feeFor(VB_BASE + VB_LEAF_CSV_IN * coins.length + VB_OUT, feeRate);
  const value = total - fee;
  check(value >= BigInt(dustThreshold(dest)), 'the bond does not cover the mining fee');
  const tx = singleOutput(coins, c.refundCsv, dest, value);
  const spent: TxOut[] = coins.map((u) => ({ value: BigInt(u.value), script: c.scriptPubkey }));
  const witnesses = coins.map((_, i) => [
    signSchnorr(keys.bond, tx.sighashUnified(i, spent, c.leafHashes[BondContract.REFUND])),
    c.leaves[BondContract.REFUND],
    c.controlBlock(BondContract.REFUND),
  ]);
  const hex = bytesToHex(tx.serialize(witnesses));
  return { hex, txid: tx.txid(), fee: Number(fee), sent: Number(value) };
};

/** Everything on our funding address (key path, no timelock). */
export const buildFundingSweep = (
  rec: RescueRecord,
  keys: EscrowKeys,
  utxos: Utxo[],
  dest: Uint8Array,
  feeRate: number,
): BuiltRescue => {
  check(utxos.length > 0, 'nothing on the funding address');
  const script = fundingScript(pubkey(keys.funding));
  const total = utxos.reduce((a, u) => a + BigInt(u.value), BigInt(0));
  const fee = feeFor(VB_BASE + VB_KEY_IN * utxos.length + VB_OUT, feeRate);
  const value = total - fee;
  check(value >= BigInt(dustThreshold(dest)), 'the funding address does not cover the mining fee');
  const tx = singleOutput(utxos, 0xfffffffd, dest, value);
  const spent: TxOut[] = utxos.map((u) => ({ value: BigInt(u.value), script }));
  const witnesses = utxos.map((_, i) => [signSchnorr(keys.funding, tx.sighashUnified(i, spent))]);
  return {
    hex: bytesToHex(tx.serialize(witnesses)),
    txid: tx.txid(),
    fee: Number(fee),
    sent: Number(value),
  };
};

/** rl31-beta20: the mining fee of taking coins back alone, as the builders above charge it. */
export const recoveryFee = (
  kind: 'funding' | 'bond' | 'escrow',
  inputs: number,
  feeRate: number,
): number =>
  Number(
    feeFor(
      VB_BASE +
        (kind === 'funding' ? VB_KEY_IN : kind === 'bond' ? VB_LEAF_CSV_IN : VB_ESCROW_LEAF_IN) *
          Math.max(1, inputs) +
        VB_OUT,
      feeRate,
    ),
  );

export const DUST_SATS = 330;

/** A leftover is worth sending back when what arrives is above the dust limit and at least
 * what the miners take (the coordinator uses the same rule, api/escrow/bonds.py). */
export const worthRecovering = (sats: number, fee: number): boolean =>
  sats - fee >= Math.max(DUST_SATS, fee);

/** Blocks still to wait for a CSV of [csv] on an output confirmed at [confHeight]. */
export const blocksLeft = (confHeight: number | null, csv: number, tip: number): number | null => {
  if (!confHeight) return null;
  return Math.max(0, confHeight + csv - 1 - tip);
};

/** rl31-beta19: payments at an address not confirmed yet (shown as pending in the rescue). */
export const pendingPayments = (
  utxos: Array<Utxo & { height: number | null }>,
): { count: number; sats: number; utxos: Utxo[] } => {
  const p = utxos.filter((u) => u.height === null);
  const txids = new Set(p.map((u) => u.txid));
  return { count: txids.size, sats: p.reduce((a, u) => a + u.value, 0), utxos: p };
};
