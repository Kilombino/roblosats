import { schnorr, secp256k1 } from '@noble/curves/secp256k1.js';
import { randomBytes } from '@noble/hashes/utils.js';

const Point = secp256k1.Point;
type P = InstanceType<typeof Point>;
const n = Point.Fn.ORDER;
const G = Point.BASE;

export const taggedHash = (tag: string, ...msgs: Uint8Array[]): Uint8Array =>
  schnorr.utils.taggedHash(tag, ...msgs);

export const concat = (...arrs: Uint8Array[]): Uint8Array => {
  const out = new Uint8Array(arrs.reduce((a, b) => a + b.length, 0));
  let off = 0;
  for (const a of arrs) {
    out.set(a, off);
    off += a.length;
  }
  return out;
};

export const toBig = (b: Uint8Array): bigint => {
  let r = BigInt(0);
  for (const x of b) r = (r << BigInt(8)) | BigInt(x);
  return r;
};

export const fromBig = (x: bigint, len = 32): Uint8Array => {
  const out = new Uint8Array(len);
  for (let i = len - 1; i >= 0; i--) {
    out[i] = Number(x & BigInt(0xff));
    x >>= BigInt(8);
  }
  return out;
};

const mod = (a: bigint): bigint => ((a % n) + n) % n;
const equal = (a: Uint8Array, b: Uint8Array): boolean =>
  a.length === b.length && a.every((x, i) => x === b[i]);

const hasEvenY = (p: P): boolean => {
  const { y } = p.toAffine();
  return (y & BigInt(1)) === BigInt(0);
};
const xbytes = (p: P): Uint8Array => p.toBytes(true).slice(1);
const cbytes = (p: P): Uint8Array => p.toBytes(true);
const cbytesExt = (p: P): Uint8Array => (p.is0() ? new Uint8Array(33) : cbytes(p));
const cpoint = (b: Uint8Array): P => Point.fromBytes(b);
const cpointExt = (b: Uint8Array): P => (b.every((x) => x === 0) ? Point.ZERO : cpoint(b));
const mul = (p: P, k: bigint): P => (mod(k) === BigInt(0) ? Point.ZERO : p.multiply(mod(k)));

export interface KeyAggContext {
  Q: P;
  gacc: bigint;
  tacc: bigint;
}

const hashKeys = (pks: Uint8Array[]): Uint8Array => taggedHash('KeyAgg list', concat(...pks));
const secondKey = (pks: Uint8Array[]): Uint8Array => {
  for (const pk of pks.slice(1)) if (!equal(pk, pks[0])) return pk;
  return new Uint8Array(33);
};

export const keyAggCoeff = (pks: Uint8Array[], pk: Uint8Array): bigint => {
  if (equal(pk, secondKey(pks))) return BigInt(1);
  return mod(toBig(taggedHash('KeyAgg coefficient', hashKeys(pks), pk)));
};

export const keyAgg = (pks: Uint8Array[]): KeyAggContext => {
  let Q = Point.ZERO;
  for (const pk of pks) Q = Q.add(mul(cpoint(pk), keyAggCoeff(pks, pk)));
  if (Q.is0()) throw new Error('aggregate key is infinity');
  return { Q, gacc: BigInt(1), tacc: BigInt(0) };
};

export const applyTweak = (
  ctx: KeyAggContext,
  tweak: Uint8Array,
  isXonly: boolean,
): KeyAggContext => {
  if (tweak.length !== 32) throw new Error('tweak must be 32 bytes');
  const g = isXonly && !hasEvenY(ctx.Q) ? n - BigInt(1) : BigInt(1);
  const t = toBig(tweak);
  if (t >= n) throw new Error('tweak out of range');
  const Q = mul(ctx.Q, g).add(mul(G, t));
  if (Q.is0()) throw new Error('tweaked key is infinity');
  return { Q, gacc: mod(g * ctx.gacc), tacc: mod(t + g * ctx.tacc) };
};

export const xonlyAggKey = (ctx: KeyAggContext): Uint8Array => xbytes(ctx.Q);
export const outputParity = (ctx: KeyAggContext): number => (hasEvenY(ctx.Q) ? 0 : 1);

const keyAggAndTweak = (
  pks: Uint8Array[],
  tweaks: Uint8Array[],
  isXonly: boolean[],
): KeyAggContext => {
  let ctx = keyAgg(pks);
  tweaks.forEach((t, i) => {
    ctx = applyTweak(ctx, t, isXonly[i]);
  });
  return ctx;
};

const u32be = (x: number): Uint8Array => fromBig(BigInt(x), 4);
const u64be = (x: number): Uint8Array => fromBig(BigInt(x), 8);

export const nonceGenInternal = (
  randIn: Uint8Array,
  sk: Uint8Array | null,
  pk: Uint8Array,
  aggpk: Uint8Array | null,
  msg: Uint8Array | null,
  extraIn: Uint8Array | null,
): { secnonce: Uint8Array; pubnonce: Uint8Array } => {
  let rand = randIn;
  if (sk) {
    const aux = taggedHash('MuSig/aux', randIn);
    rand = sk.map((x, i) => x ^ aux[i]);
  }
  const agg = aggpk ?? new Uint8Array(0);
  const msgPrefixed = msg ? concat(Uint8Array.of(1), u64be(msg.length), msg) : Uint8Array.of(0);
  const extra = extraIn ?? new Uint8Array(0);
  const k = [0, 1].map((i) =>
    mod(
      toBig(
        taggedHash(
          'MuSig/nonce',
          concat(
            rand,
            Uint8Array.of(pk.length),
            pk,
            Uint8Array.of(agg.length),
            agg,
            msgPrefixed,
            u32be(extra.length),
            extra,
            Uint8Array.of(i),
          ),
        ),
      ),
    ),
  );
  if (k[0] === BigInt(0) || k[1] === BigInt(0)) throw new Error('nonce is zero');
  return {
    secnonce: concat(fromBig(k[0]), fromBig(k[1]), pk),
    pubnonce: concat(cbytes(mul(G, k[0])), cbytes(mul(G, k[1]))),
  };
};

export const nonceGen = (
  sk: Uint8Array,
  pk: Uint8Array,
  aggpk: Uint8Array,
  msg: Uint8Array,
  extraIn: Uint8Array | null = null,
): { secnonce: Uint8Array; pubnonce: Uint8Array } =>
  nonceGenInternal(randomBytes(32), sk, pk, aggpk, msg, extraIn);

export const nonceAgg = (pubnonces: Uint8Array[]): Uint8Array => {
  const parts = [0, 1].map((j) => {
    let R = Point.ZERO;
    for (const pn of pubnonces) R = R.add(cpoint(pn.slice(33 * j, 33 * (j + 1))));
    return cbytesExt(R);
  });
  return concat(parts[0], parts[1]);
};

export interface Session {
  aggnonce: Uint8Array;
  pubkeys: Uint8Array[];
  tweaks: Uint8Array[];
  isXonly: boolean[];
  msg: Uint8Array;
}

const sessionValues = (s: Session) => {
  const ctx = keyAggAndTweak(s.pubkeys, s.tweaks, s.isXonly);
  const b = mod(toBig(taggedHash('MuSig/noncecoef', s.aggnonce, xbytes(ctx.Q), s.msg)));
  const R1 = cpointExt(s.aggnonce.slice(0, 33));
  const R2 = cpointExt(s.aggnonce.slice(33, 66));
  const Rp = R1.add(mul(R2, b));
  const R = Rp.is0() ? G : Rp;
  const e = mod(toBig(taggedHash('BIP0340/challenge', xbytes(R), xbytes(ctx.Q), s.msg)));
  return { ctx, b, R, e };
};

export const partialSigVerify = (
  psig: Uint8Array,
  pubnonces: Uint8Array[],
  s: Session,
  i: number,
): boolean => {
  const sess = { ...s, aggnonce: nonceAgg(pubnonces) };
  return partialSigVerifyInternal(psig, pubnonces[i], s.pubkeys[i], sess);
};

export const partialSigVerifyInternal = (
  psig: Uint8Array,
  pubnonce: Uint8Array,
  pk: Uint8Array,
  s: Session,
): boolean => {
  const { ctx, b, R, e } = sessionValues(s);
  const sv = toBig(psig);
  if (sv >= n) return false;
  const Rs1 = cpoint(pubnonce.slice(0, 33));
  const Rs2 = cpoint(pubnonce.slice(33, 66));
  const Rep = Rs1.add(mul(Rs2, b));
  const Re = hasEvenY(R) ? Rep : Rep.negate();
  const a = keyAggCoeff(s.pubkeys, pk);
  const g = hasEvenY(ctx.Q) ? BigInt(1) : n - BigInt(1);
  const gp = mod(g * ctx.gacc);
  const lhs = mul(G, sv);
  const rhs = Re.add(mul(cpoint(pk), mod(e * a * gp)));
  return lhs.equals(rhs);
};

export const sign = (secnonce: Uint8Array, sk: Uint8Array, s: Session): Uint8Array => {
  const { ctx, b, R, e } = sessionValues(s);
  const k1p = toBig(secnonce.slice(0, 32));
  const k2p = toBig(secnonce.slice(32, 64));
  if (k1p === BigInt(0) || k2p === BigInt(0) || k1p >= n || k2p >= n)
    throw new Error('invalid or reused secnonce');
  secnonce.fill(0, 0, 64);
  const k1 = hasEvenY(R) ? k1p : n - k1p;
  const k2 = hasEvenY(R) ? k2p : n - k2p;
  const dp = toBig(sk);
  if (dp === BigInt(0) || dp >= n) throw new Error('invalid secret key');
  const pk = cbytes(mul(G, dp));
  if (!equal(pk, secnonce.slice(64, 97))) throw new Error('public key does not match the nonce');
  const a = keyAggCoeff(s.pubkeys, pk);
  const g = hasEvenY(ctx.Q) ? BigInt(1) : n - BigInt(1);
  const d = mod(g * ctx.gacc * dp);
  const sv = mod(k1 + b * k2 + e * a * d);
  const psig = fromBig(sv);
  const pubnonce = concat(cbytes(mul(G, k1p)), cbytes(mul(G, k2p)));
  if (!partialSigVerifyInternal(psig, pubnonce, pk, s))
    throw new Error('partial signature does not verify');
  return psig;
};

export const partialSigAgg = (psigs: Uint8Array[], s: Session): Uint8Array => {
  const { ctx, R, e } = sessionValues(s);
  let sv = BigInt(0);
  for (const p of psigs) {
    const x = toBig(p);
    if (x >= n) throw new Error('invalid partial signature');
    sv = mod(sv + x);
  }
  const g = hasEvenY(ctx.Q) ? BigInt(1) : n - BigInt(1);
  sv = mod(sv + e * g * ctx.tacc);
  return concat(xbytes(R), fromBig(sv));
};

export const individualPk = (sk: Uint8Array): Uint8Array => cbytes(mul(G, toBig(sk)));
