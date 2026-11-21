import { schnorr, secp256k1 } from '@noble/curves/secp256k1.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes, randomBytes } from '@noble/hashes/utils.js';
import { base58check, bech32, bech32m } from '@scure/base';
import { DEFAULT_HRP, checkAddress } from '../address';

import {
  applyTweak,
  concat,
  fromBig,
  keyAgg,
  outputParity,
  taggedHash,
  toBig,
  xonlyAggKey,
} from './musig2';

export const SIGHASH_ALL_UNIFIED = 0x21;
const LEAF_VERSION = 0xc0;
const OP_CHECKSIG = 0xac;
const OP_CHECKSIGVERIFY = 0xad;
const OP_CSV = 0xb2;
export const SEQUENCE_RBF = 0xfffffffd;
const NUMS_H = hexToBytes('50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0');
const Point = secp256k1.Point;
const n = Point.Fn.ORDER;
const enc = new TextEncoder();

export { bytesToHex, hexToBytes };

export const deriveSecret = (
  secret: Uint8Array,
  coordinator: string,
  orderId: number,
  role: string,
): Uint8Array => {
  const info = enc.encode(`roblosats/escrow/v1/${coordinator}/${orderId}/${role}`);
  for (let attempt = 0; attempt < 256; attempt++) {
    const i = attempt === 0 ? info : concat(info, Uint8Array.of(attempt));
    const sk = hkdf(sha256, secret, new Uint8Array(32), i, 32);
    const x = toBig(sk);
    if (x > BigInt(0) && x < n) return sk;
  }
  throw new Error('could not derive a valid key');
};

export const tokenKey = (
  token: string,
  coordinator: string,
  orderId: number,
  role: string,
): Uint8Array => deriveSecret(enc.encode(token), coordinator.toLowerCase(), orderId, role);

export const pubkey = (sk: Uint8Array): Uint8Array => secp256k1.getPublicKey(sk, true);
export const xonly = (pk33: Uint8Array): Uint8Array => pk33.slice(1);

const compact = (x: number): Uint8Array => {
  if (x < 0xfd) return Uint8Array.of(x);
  if (x <= 0xffff) return Uint8Array.of(0xfd, x & 0xff, x >> 8);
  return concat(Uint8Array.of(0xfe), le(x, 4));
};
const le = (x: number | bigint, len: number): Uint8Array => fromBig(BigInt(x), len).reverse();
const serBytes = (b: Uint8Array): Uint8Array => concat(compact(b.length), b);
const push = (b: Uint8Array): Uint8Array => concat(Uint8Array.of(b.length), b);
const scriptNum = (x: number): Uint8Array => {
  if (x === 0) return Uint8Array.of(0);
  if (x >= 1 && x <= 16) return Uint8Array.of(0x50 + x);
  const bytes: number[] = [];
  let v = x;
  while (v > 0) {
    bytes.push(v & 0xff);
    v >>= 8;
  }
  if (bytes[bytes.length - 1] & 0x80) bytes.push(0);
  return push(Uint8Array.from(bytes));
};

const tapleaf = (script: Uint8Array): Uint8Array =>
  taggedHash('TapLeaf', Uint8Array.of(LEAF_VERSION), serBytes(script));
const tapbranch = (a: Uint8Array, b: Uint8Array): Uint8Array => {
  const [x, y] = bytesToHex(a) < bytesToHex(b) ? [a, b] : [b, a];
  return taggedHash('TapBranch', x, y);
};

export const p2trAddress = (outputKey: Uint8Array, hrp: string): string =>
  bech32m.encode(hrp, [1, ...bech32m.toWords(outputKey)]);

const leafPair = (a: Uint8Array, b: Uint8Array): Uint8Array =>
  concat(
    push(xonly(a)),
    Uint8Array.of(OP_CHECKSIGVERIFY),
    push(xonly(b)),
    Uint8Array.of(OP_CHECKSIG),
  );
const leafCsv = (a: Uint8Array, csv: number): Uint8Array =>
  concat(push(xonly(a)), Uint8Array.of(OP_CHECKSIGVERIFY), scriptNum(csv), Uint8Array.of(OP_CSV));

// Rescue plan 2 (rl31-beta16): <seller> CHECKSIGVERIFY <buyer> CHECKSIGVERIFY <csv> CSV. Spent
// only by the rescue both traders pre-sign before the deposit (each one gets its own).
const leafFairRescue = (s: Uint8Array, b: Uint8Array, csv: number): Uint8Array =>
  concat(
    push(xonly(s)),
    Uint8Array.of(OP_CHECKSIGVERIFY),
    push(xonly(b)),
    Uint8Array.of(OP_CHECKSIGVERIFY),
    scriptNum(csv),
    Uint8Array.of(OP_CSV),
  );

export class EscrowContract {
  static SELLER_COORDINATOR = 0;
  static BUYER_COORDINATOR = 1;
  static SELLER_RESCUE = 2;
  static RESCUE = 2;
  leaves: Uint8Array[];
  leafHashes: Uint8Array[];
  merkleRoot: Uint8Array;
  internalKey: Uint8Array;
  tweak: Uint8Array;
  outputKey: Uint8Array;
  parity: number;

  constructor(
    public sellerPk: Uint8Array,
    public buyerPk: Uint8Array,
    public coordinatorPk: Uint8Array,
    public rescueCsv: number,
    public hrp = 'bc',
    /** 1: the seller alone after the timelock (escrows up to rl31-beta15); 2: fair rescue. */
    public rescuePlan = 1,
  ) {
    this.leaves = [
      leafPair(sellerPk, coordinatorPk),
      leafPair(buyerPk, coordinatorPk),
      rescuePlan >= 2 ? leafFairRescue(sellerPk, buyerPk, rescueCsv) : leafCsv(sellerPk, rescueCsv),
    ];
    this.leafHashes = this.leaves.map(tapleaf);
    this.merkleRoot = tapbranch(
      tapbranch(this.leafHashes[0], this.leafHashes[1]),
      this.leafHashes[2],
    );
    const ctx = keyAgg(this.musigPubkeys());
    this.internalKey = xonlyAggKey(ctx);
    this.tweak = taggedHash('TapTweak', this.internalKey, this.merkleRoot);
    const tweaked = applyTweak(ctx, this.tweak, true);
    this.outputKey = xonlyAggKey(tweaked);
    this.parity = outputParity(tweaked);
  }

  musigPubkeys(): Uint8Array[] {
    return [this.sellerPk, this.buyerPk];
  }

  /** Control block to spend leaf [index] (tree ((0, 1), 2)). */
  controlBlock(index: number): Uint8Array {
    const [h0, h1, h2] = this.leafHashes;
    const path = index === 2 ? [tapbranch(h0, h1)] : index === 0 ? [h1, h2] : [h0, h2];
    return concat(Uint8Array.of(LEAF_VERSION | this.parity), this.internalKey, ...path);
  }

  get scriptPubkey(): Uint8Array {
    return concat(Uint8Array.of(0x51, 0x20), this.outputKey);
  }

  get address(): string {
    return p2trAddress(this.outputKey, this.hrp);
  }
}

export class BondContract {
  static COOPERATIVE = 0;
  static REFUND = 1;
  leaves: Uint8Array[];
  leafHashes: Uint8Array[];
  outputKey: Uint8Array;
  parity = 0;

  constructor(
    public ownerPk: Uint8Array,
    public coordinatorPk: Uint8Array,
    public refundCsv: number,
    public hrp = 'bc',
  ) {
    this.leaves = [leafPair(ownerPk, coordinatorPk), leafCsv(ownerPk, refundCsv)];
    this.leafHashes = this.leaves.map(tapleaf);
    const root = tapbranch(this.leafHashes[0], this.leafHashes[1]);
    const P = schnorr.utils.lift_x(toBig(NUMS_H));
    const t = toBig(taggedHash('TapTweak', NUMS_H, root));
    const Q = P.add(Point.BASE.multiply(t));
    const q = Q.toBytes(true);
    this.outputKey = q.slice(1);
    this.parity = q[0] === 3 ? 1 : 0;
  }

  /** Control block to spend leaf [index] (tree (0, 1), internal key NUMS H). */
  controlBlock(index: number): Uint8Array {
    return concat(
      Uint8Array.of(LEAF_VERSION | this.parity),
      NUMS_H,
      this.leafHashes[index === 0 ? 1 : 0],
    );
  }

  get scriptPubkey(): Uint8Array {
    return concat(Uint8Array.of(0x51, 0x20), this.outputKey);
  }

  get address(): string {
    return p2trAddress(this.outputKey, this.hrp);
  }
}

export interface TxIn {
  txid: string;
  vout: number;
  sequence: number;
}
export interface TxOut {
  value: bigint;
  script: Uint8Array;
}

export class Transaction {
  constructor(
    public inputs: TxIn[],
    public outputs: TxOut[],
    public version = 2,
    public locktime = 0,
  ) {}

  static parse(hex: string): Transaction {
    const raw = hexToBytes(hex);
    let pos = 0;
    const take = (k: number): Uint8Array => {
      if (pos + k > raw.length) throw new Error('truncated transaction');
      const out = raw.slice(pos, pos + k);
      pos += k;
      return out;
    };
    const readLe = (k: number): bigint => toBig(take(k).reverse());
    const readCompact = (): number => {
      const f = take(1)[0];
      if (f < 0xfd) return f;
      return Number(readLe(f === 0xfd ? 2 : f === 0xfe ? 4 : 8));
    };
    const version = Number(readLe(4));
    const segwit = raw[pos] === 0 && raw[pos + 1] === 1;
    if (segwit) take(2);
    const inputs: TxIn[] = [];
    for (let i = readCompact(); i > 0; i--) {
      const txid = bytesToHex(take(32).reverse());
      const vout = Number(readLe(4));
      if (readCompact() !== 0) throw new Error('unexpected scriptSig');
      inputs.push({ txid, vout, sequence: Number(readLe(4)) });
    }
    const outputs: TxOut[] = [];
    for (let i = readCompact(); i > 0; i--) {
      const value = readLe(8);
      outputs.push({ value, script: take(readCompact()) });
    }
    if (segwit) for (const _ of inputs) for (let k = readCompact(); k > 0; k--) take(readCompact());
    const locktime = Number(readLe(4));
    if (pos !== raw.length) throw new Error('trailing bytes');
    return new Transaction(inputs, outputs, version, locktime);
  }

  /** Raw transaction; with [witnesses] (one stack per input) in the segwit format. */
  serialize(witnesses?: Uint8Array[][]): Uint8Array {
    const ins = this.inputs.map((i) =>
      concat(this.outpoint(i), Uint8Array.of(0), le(i.sequence, 4)),
    );
    const body = concat(
      compact(this.inputs.length),
      ...ins,
      compact(this.outputs.length),
      ...this.outputs.map((o) => this.serOut(o)),
    );
    if (!witnesses) return concat(le(this.version, 4), body, le(this.locktime, 4));
    const wit = witnesses.map((stack) => concat(compact(stack.length), ...stack.map(serBytes)));
    return concat(le(this.version, 4), Uint8Array.of(0, 1), body, ...wit, le(this.locktime, 4));
  }

  txid(): string {
    return bytesToHex(sha256(sha256(this.serialize())).reverse());
  }

  private outpoint(i: TxIn): Uint8Array {
    return concat(hexToBytes(i.txid).reverse(), le(i.vout, 4));
  }

  private serOut(o: TxOut): Uint8Array {
    return concat(le(o.value, 8), serBytes(o.script));
  }

  sighashUnified(index: number, spent: TxOut[], leafHash?: Uint8Array): Uint8Array {
    if (spent.length !== this.inputs.length) throw new Error('one spent output per input');
    const shaPrevouts = sha256(concat(...this.inputs.map((i) => this.outpoint(i))));
    const shaAmounts = sha256(concat(...spent.map((s) => le(s.value, 8))));
    const shaScripts = sha256(concat(...spent.map((s) => serBytes(s.script))));
    const shaSeq = sha256(concat(...this.inputs.map((i) => le(i.sequence, 4))));
    const shaOuts = sha256(concat(...this.outputs.map((o) => this.serOut(o))));
    const parts = [
      Uint8Array.of(0, SIGHASH_ALL_UNIFIED),
      le(this.version, 4),
      le(this.locktime, 4),
      Uint8Array.of(0),
      shaPrevouts,
      shaAmounts,
      shaScripts,
      shaSeq,
      shaOuts,
      Uint8Array.of(leafHash ? 3 : 2),
      le(index, 4),
      Uint8Array.of(0),
    ];
    if (leafHash) parts.push(leafHash, Uint8Array.of(0), le(0xffffffff, 4));
    return taggedHash('UnifiedSighash', concat(...parts));
  }
}

/** 0.9.12: the plain BIP341 signature hash (SIGHASH_DEFAULT), as the SHA-256 chain ("Spamchain")
 * validates it: used only by the rescue tool to take back coins that were sent to one of our
 * addresses on that chain by mistake. Same fields as sighashUnified without the unified ones. */
export const sighashTaproot = (
  tx: Transaction,
  index: number,
  spent: TxOut[],
  leafHash?: Uint8Array,
): Uint8Array => {
  if (spent.length !== tx.inputs.length) throw new Error('one spent output per input');
  const outpoint = (i: TxIn): Uint8Array => concat(hexToBytes(i.txid).reverse(), le(i.vout, 4));
  const parts = [
    Uint8Array.of(0, 0), // epoch 0, SIGHASH_DEFAULT
    le(tx.version, 4),
    le(tx.locktime, 4),
    sha256(concat(...tx.inputs.map(outpoint))),
    sha256(concat(...spent.map((s) => le(s.value, 8)))),
    sha256(concat(...spent.map((s) => serBytes(s.script)))),
    sha256(concat(...tx.inputs.map((i) => le(i.sequence, 4)))),
    sha256(concat(...tx.outputs.map((o) => concat(le(o.value, 8), serBytes(o.script))))),
    Uint8Array.of(leafHash ? 2 : 0), // spend_type: ext_flag 1 for a script path, no annex
    le(index, 4),
  ];
  if (leafHash) parts.push(leafHash, Uint8Array.of(0), le(0xffffffff, 4));
  return taggedHash('TapSighash', concat(...parts));
};

/** 0.9.12: a 64-byte BIP340 signature (SIGHASH_DEFAULT: no hash type byte), for sighashTaproot. */
export const signSchnorrDefault = (sk: Uint8Array, msg: Uint8Array): Uint8Array =>
  schnorr.sign(msg, sk, randomBytes(32));

export const signSchnorr = (sk: Uint8Array, msg: Uint8Array): Uint8Array =>
  concat(schnorr.sign(msg, sk, randomBytes(32)), Uint8Array.of(SIGHASH_ALL_UNIFIED));

export const leafHashOf = (contract: EscrowContract | BondContract, index: number): Uint8Array =>
  contract.leafHashes[index];

export const fundingScript = (pk33: Uint8Array): Uint8Array =>
  concat(Uint8Array.of(0x51, 0x20), xonly(pk33));

// 0.9.9: legacy receive addresses. Version bytes: mainnet P2PKH 0x00 ("1…"), P2SH 0x05
// ("3…"); testnet/regtest 0x6f/0xc4 (the coordinator checks the network when it gets it).
const b58 = base58check(sha256);
const P2PKH_VERSIONS = [0x00, 0x6f];
const P2SH_VERSIONS = [0x05, 0xc4];

const base58Script = (address: string): Uint8Array | null => {
  let raw: Uint8Array;
  try {
    raw = b58.decode(address);
  } catch {
    return null;
  }
  if (raw.length !== 21) return null;
  const hash = raw.slice(1);
  if (P2PKH_VERSIONS.includes(raw[0]))
    return concat(Uint8Array.of(0x76, 0xa9, 0x14), hash, Uint8Array.of(0x88, 0xac));
  if (P2SH_VERSIONS.includes(raw[0]))
    return concat(Uint8Array.of(0xa9, 0x14), hash, Uint8Array.of(0x87));
  return null;
};

export const addressToScript = (address: string): Uint8Array => {
  const legacy = /^[1-9A-HJ-NP-Za-km-z]{25,35}$/.test(address) ? base58Script(address) : null;
  if (legacy) return legacy;
  const lower = address.toLowerCase() as `${string}1${string}`;
  let decoded: { words: number[] };
  let isM = false;
  try {
    decoded = bech32.decode(lower);
  } catch {
    decoded = bech32m.decode(lower);
    isM = true;
  }
  const [version, ...words] = decoded.words;
  const program = Uint8Array.from(bech32.fromWords(words));
  if (version === 0 && !isM && (program.length === 20 || program.length === 32))
    return concat(Uint8Array.of(0, program.length), program);
  if (version === 1 && isM && program.length === 32)
    return concat(Uint8Array.of(0x51, 0x20), program);
  throw new Error('only legacy (1…), P2SH (3…), segwit v0 and taproot addresses');
};

/** 0.9.9: the node's dust threshold for an output to this script (Bitcoin Core's rule: the
 * output plus the input that would spend it, 67 bytes for a witness program, 148 otherwise, at
 * the dust relay fee; the coordinators' nodes run 3000 sat/kvB), never below 330 like the
 * coordinator (P2PKH 546, P2SH 540, segwit and taproot 330). */
export const dustThreshold = (script: Uint8Array, relayFee = 3000): number => {
  const witness =
    script.length >= 4 &&
    script.length <= 42 &&
    (script[0] === 0 || (script[0] >= 0x51 && script[0] <= 0x60)) &&
    script[1] === script.length - 2;
  const size = 8 + 1 + script.length + (witness ? 67 : 148);
  return Math.max(330, Math.floor((size * relayFee) / 1000));
};

/** A receive address this app can pay: legacy 1…, P2SH 3… (0.9.9), segwit v0 bc1q… and taproot
 * bc1p…, 0.9.10: fully checked (checksum, bech32 vs bech32m, case) for the coordinator's
 * network (`hrp`: "bc" mainnet, "tb" testnet, "bcrt" regtest), see utils/address.ts. */
export const validOnchainAddress = (address: string, hrp: string = DEFAULT_HRP): boolean =>
  checkAddress(address, hrp).ok;
