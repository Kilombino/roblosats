import { schnorr, secp256k1 } from '@noble/curves/secp256k1.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes, randomBytes } from '@noble/hashes/utils.js';
import { bech32m } from '@scure/base';

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

export class EscrowContract {
  static SELLER_COORDINATOR = 0;
  static BUYER_COORDINATOR = 1;
  static SELLER_RESCUE = 2;
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
  ) {
    this.leaves = [
      leafPair(sellerPk, coordinatorPk),
      leafPair(buyerPk, coordinatorPk),
      leafCsv(sellerPk, rescueCsv),
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
    this.outputKey = Q.toBytes(true).slice(1);
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

export const signSchnorr = (sk: Uint8Array, msg: Uint8Array): Uint8Array =>
  concat(schnorr.sign(msg, sk, randomBytes(32)), Uint8Array.of(SIGHASH_ALL_UNIFIED));

export const leafHashOf = (contract: EscrowContract | BondContract, index: number): Uint8Array =>
  contract.leafHashes[index];

export const fundingScript = (pk33: Uint8Array): Uint8Array =>
  concat(Uint8Array.of(0x51, 0x20), xonly(pk33));
