/**
 * 0.9.10: what the user types as an onchain address, checked in full before anything is sent:
 * the checksum (bech32 BIP-173 for segwit v0, bech32m BIP-350 for taproot and later versions,
 * base58check for 1…/3…), the network (bech32 HRP of the coordinator: "bc" mainnet, "tb"
 * testnet, "bcrt" regtest; base58 version bytes 0x00/0x05 mainnet, 0x6f/0xc4 test/regtest) and
 * the types this app and the coordinator pay (P2PKH, P2SH, P2WPKH, P2WSH, P2TR).
 */
import { base58check, bech32, bech32m } from '@scure/base';
import { sha256 } from '@noble/hashes/sha2.js';

export type AddressType = 'p2pkh' | 'p2sh' | 'p2wpkh' | 'p2wsh' | 'p2tr';

/** empty; typo (checksum, length, a character that cannot be there); case (mixed upper and
 * lower case); network (valid, but for another network); type (valid, but a kind we don't pay). */
export type AddressError = 'empty' | 'typo' | 'case' | 'network' | 'type';

export type AddressCheck =
  | { ok: true; type: AddressType; script: Uint8Array; address: string }
  | { ok: false; error: AddressError };

export const DEFAULT_HRP = 'bc';

const KNOWN_HRPS = ['bc', 'tb', 'bcrt'];

/** The bech32 HRP of a network name ("mainnet" when unknown). */
export const hrpOfNetwork = (network?: string | null): string =>
  network === 'testnet' ? 'tb' : network === 'regtest' ? 'bcrt' : DEFAULT_HRP;

const b58 = base58check(sha256);
const BASE58 = /^[1-9A-HJ-NP-Za-km-z]+$/;

const concat = (...parts: Uint8Array[]): Uint8Array => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let i = 0;
  for (const p of parts) {
    out.set(p, i);
    i += p.length;
  }
  return out;
};

const fail = (error: AddressError): AddressCheck => ({ ok: false, error });

const checkBase58 = (address: string, hrp: string): AddressCheck => {
  let raw: Uint8Array;
  try {
    raw = b58.decode(address);
  } catch {
    return fail('typo');
  }
  if (raw.length !== 21) return fail('typo');
  const mainnet = hrp === 'bc';
  const [p2pkh, p2sh] = mainnet ? [0x00, 0x05] : [0x6f, 0xc4];
  const [otherPkh, otherSh] = mainnet ? [0x6f, 0xc4] : [0x00, 0x05];
  const hash = raw.slice(1);
  if (raw[0] === p2pkh)
    return {
      ok: true,
      type: 'p2pkh',
      address,
      script: concat(Uint8Array.of(0x76, 0xa9, 0x14), hash, Uint8Array.of(0x88, 0xac)),
    };
  if (raw[0] === p2sh)
    return {
      ok: true,
      type: 'p2sh',
      address,
      script: concat(Uint8Array.of(0xa9, 0x14), hash, Uint8Array.of(0x87)),
    };
  if (raw[0] === otherPkh || raw[0] === otherSh) return fail('network');
  return fail('type');
};

const checkBech32 = (address: string, hrp: string): AddressCheck => {
  if (address !== address.toLowerCase() && address !== address.toUpperCase()) return fail('case');
  const lower = address.toLowerCase();
  let decoded = bech32.decodeUnsafe(lower);
  let isM = false;
  if (!decoded) {
    decoded = bech32m.decodeUnsafe(lower);
    isM = true;
  }
  if (!decoded) return fail('typo');
  const { prefix, words } = decoded;
  // BIP-173: HRP characters in the US-ASCII range 33-126
  if (![...prefix].every((c) => c.charCodeAt(0) >= 33 && c.charCodeAt(0) <= 126))
    return fail('typo');
  if (words.length === 0) return fail('typo');
  const [version, ...data] = words;
  if (version > 16) return fail('typo');
  const program = bech32.fromWordsUnsafe(data);
  if (!program) return fail('typo');
  // BIP-141 / BIP-173 / BIP-350: program size, v0 sizes and which checksum each version takes
  if (program.length < 2 || program.length > 40) return fail('typo');
  if (version === 0 && program.length !== 20 && program.length !== 32) return fail('typo');
  if ((version === 0) === isM) return fail('typo');
  if (prefix !== hrp) return fail('network');
  const p = Uint8Array.from(program);
  if (version === 0)
    return {
      ok: true,
      type: p.length === 20 ? 'p2wpkh' : 'p2wsh',
      address: lower,
      script: concat(Uint8Array.of(0, p.length), p),
    };
  if (version === 1 && p.length === 32)
    return { ok: true, type: 'p2tr', address: lower, script: concat(Uint8Array.of(0x51, 0x20), p) };
  return fail('type');
};

/** Checks an address typed by the user for the coordinator's network (bech32 HRP). On success
 * `address` is the form to send (bech32 in lower case) and `script` its output script. */
export const checkAddress = (input: string, hrp: string = DEFAULT_HRP): AddressCheck => {
  const address = (input ?? '').trim();
  if (address === '') return fail('empty');
  const lower = address.toLowerCase();
  const sep = lower.lastIndexOf('1');
  const prefix = sep > 0 ? lower.slice(0, sep) : '';
  const looksBech32 =
    KNOWN_HRPS.some((h) => lower.startsWith(`${h}1`)) ||
    lower.startsWith(`${hrp}1`) ||
    (/^[a-z]{1,83}$/.test(prefix) && !BASE58.test(address));
  if (looksBech32) return checkBech32(address, hrp);
  if (BASE58.test(address)) return checkBase58(address, hrp);
  return fail('typo');
};

/** Groups of 4 characters separated by spaces, to read an address aloud and compare it. */
export const groupAddress = (address: string): string => (address.match(/.{1,4}/g) ?? []).join(' ');

/** The examples of what is accepted on a network: legacy, P2SH, segwit, taproot. */
export const acceptedExamples = (
  hrp: string = DEFAULT_HRP,
): { p2pkh: string; p2sh: string; hrp: string } =>
  hrp === 'bc' ? { p2pkh: '1…', p2sh: '3…', hrp } : { p2pkh: 'm…, n…', p2sh: '2…', hrp };

/** The text that tells the user what is wrong with the address. */
export const addressErrorText = (
  error: AddressError,
  t: (key: string, options?: Record<string, unknown>) => string,
  hrp: string = DEFAULT_HRP,
): string => {
  const ex = acceptedExamples(hrp);
  switch (error) {
    case 'empty':
      return t('Write a Bitcoin address first');
    case 'typo':
      return t(
        'This address has a typo or a missing character. Check it again, or better copy and paste it from your wallet.',
      );
    case 'case':
      return t(
        'This address mixes capital and small letters. Copy and paste it exactly as your wallet shows it.',
      );
    case 'network':
      return t(
        'This address is for another network. This coordinator only takes addresses of its own network: {{hrp}}1…, {{p2pkh}} or {{p2sh}}',
        ex,
      );
    case 'type':
      return t(
        'This kind of address is not accepted. Use legacy ({{p2pkh}}), P2SH ({{p2sh}}), segwit ({{hrp}}1q…) or taproot ({{hrp}}1p…).',
        ex,
      );
  }
};
