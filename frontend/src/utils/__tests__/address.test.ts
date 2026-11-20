/**
 * 0.9.10: full check of the onchain addresses the user types (Kilombino: a typo, a missing
 * character or an address of another network must never be sent).
 */
import { base58check, bech32, bech32m } from '@scure/base';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex } from '@noble/hashes/utils.js';

import {
  addressErrorText,
  checkAddress,
  groupAddress,
  hrpOfNetwork,
  type AddressError,
} from '../address';
import { validOnchainAddress } from '../escrow/taproot';

const b58 = base58check(sha256);

const scriptOf = (a: string, hrp = 'bc'): string => {
  const r = checkAddress(a, hrp);
  if (!r.ok) throw new Error(`${a}: ${r.error}`);
  return bytesToHex(r.script);
};
const errorOf = (a: string, hrp = 'bc'): AddressError | null => {
  const r = checkAddress(a, hrp);
  return r.ok ? null : r.error;
};

/** The same witness program for another HRP (a valid address of another network). */
const reHrp = (a: string, hrp: string): string => {
  const lower = a.toLowerCase() as `${string}1${string}`;
  const isM = !bech32.decodeUnsafe(lower);
  const enc = isM ? bech32m : bech32;
  return enc.encode(hrp, enc.decode(lower).words);
};
/** The same hash with another base58 version byte. */
const reVersion = (a: string, version: number): string => {
  const raw = b58.decode(a);
  return b58.encode(Uint8Array.of(version, ...raw.slice(1)));
};
const missing = (a: string): string => a.slice(0, 10) + a.slice(11);
const swapped = (a: string): string => {
  for (let i = 8; i < a.length - 1; i++) {
    if (a[i] !== a[i + 1]) return a.slice(0, i) + a[i + 1] + a[i] + a.slice(i + 2);
  }
  throw new Error('no pair to swap');
};

const P2PKH = '1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2';
const P2SH = '3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy';
const P2WPKH = 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4';
const P2WSH = 'bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3';
const P2TR = 'bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0';

describe('real mainnet addresses', () => {
  it.each([
    [P2PKH, 'p2pkh', '76a91477bff20c60e522dfaa3350c39b030a5d004e839a88ac'],
    [P2SH, 'p2sh', 'a914b472a266d0bd89c13706a4132ccfb16f7c3b9fcb87'],
    [P2WPKH, 'p2wpkh', '0014751e76e8199196d454941c45d1b3a323f1433bd6'],
    [P2WSH, 'p2wsh', '00201863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262'],
    [P2TR, 'p2tr', '512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798'],
  ])('%s is a valid %s', (address, type, script) => {
    const r = checkAddress(address, 'bc');
    expect(r).toMatchObject({ ok: true, type, address });
    expect(scriptOf(address)).toBe(script);
    expect(validOnchainAddress(address)).toBe(true);
    expect(validOnchainAddress(address, 'tb')).toBe(false);
  });

  it.each([P2PKH, P2SH, P2WPKH, P2WSH, P2TR])('%s with a missing character is a typo', (a) => {
    expect(errorOf(missing(a))).toBe('typo');
  });

  it.each([P2PKH, P2SH, P2WPKH, P2WSH, P2TR])('%s with two characters swapped is a typo', (a) => {
    expect(errorOf(swapped(a))).toBe('typo');
  });

  it.each([P2WPKH, P2WSH, P2TR])('%s on testnet or regtest is another network', (a) => {
    expect(errorOf(reHrp(a, 'tb'))).toBe('network');
    expect(errorOf(reHrp(a, 'bcrt'))).toBe('network');
    // and the other way round: valid on its own network only
    expect(checkAddress(reHrp(a, 'tb'), 'tb').ok).toBe(true);
    expect(checkAddress(reHrp(a, 'bcrt'), 'bcrt').ok).toBe(true);
    expect(errorOf(a, 'tb')).toBe('network');
  });

  it('legacy addresses of testnet/regtest are another network', () => {
    const tPkh = reVersion(P2PKH, 0x6f);
    const tSh = reVersion(P2SH, 0xc4);
    expect(tPkh[0]).toMatch(/[mn]/);
    expect(tSh[0]).toBe('2');
    expect(errorOf(tPkh)).toBe('network');
    expect(errorOf(tSh)).toBe('network');
    expect(errorOf('mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn')).toBe('network');
    expect(errorOf('2MzQwSSnBHWHqSAqtTVQ6v47XtaisrJa1Vc')).toBe('network');
    expect(checkAddress('mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn', 'tb')).toMatchObject({
      ok: true,
      type: 'p2pkh',
    });
    expect(checkAddress('2MzQwSSnBHWHqSAqtTVQ6v47XtaisrJa1Vc', 'bcrt')).toMatchObject({
      ok: true,
      type: 'p2sh',
    });
    expect(errorOf(P2PKH, 'tb')).toBe('network');
    expect(errorOf(P2SH, 'bcrt')).toBe('network');
  });

  it('other base58 version bytes (another coin) are not accepted', () => {
    expect(errorOf(reVersion(P2PKH, 0x30))).toBe('type'); // Litecoin L…
  });

  it('bech32m on a v0 program and bech32 on a v1 program are typos', () => {
    const v0 = bech32.decode(P2WPKH).words;
    const v1 = bech32m.decode(P2TR as `${string}1${string}`).words;
    expect(errorOf(bech32m.encode('bc', v0))).toBe('typo');
    expect(errorOf(bech32.encode('bc', v1))).toBe('typo');
  });

  it('mixed case is refused, all upper case is fine and sent in lower case', () => {
    const mixed = P2WPKH.slice(0, 10) + P2WPKH.slice(10).toUpperCase();
    expect(errorOf(mixed)).toBe('case');
    expect(errorOf('bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqZk5jj0')).toBe('case');
    expect(checkAddress(P2WPKH.toUpperCase())).toMatchObject({ ok: true, address: P2WPKH });
  });

  it('empty, spaces and garbage', () => {
    expect(errorOf('')).toBe('empty');
    expect(errorOf('   ')).toBe('empty');
    expect(errorOf('hello')).toBe('typo');
    expect(errorOf('bitcoin:' + P2WPKH)).toBe('typo');
    expect(checkAddress(`  ${P2TR}\n`)).toMatchObject({ ok: true, address: P2TR });
  });
});

// https://github.com/bitcoin/bips/blob/master/bip-0350.mediawiki#test-vectors-for-v0-v16-native-segregated-witness-addresses
describe('BIP-350 vectors', () => {
  it.each([
    [
      'BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4',
      'bc',
      '0014751e76e8199196d454941c45d1b3a323f1433bd6',
    ],
    [
      'tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7',
      'tb',
      '00201863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262',
    ],
    [
      'tb1qqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesrxh6hy',
      'tb',
      '0020000000c4a5cad46221b2a187905e5266362b99d5e91c6ce24d165dab93e86433',
    ],
    [
      'tb1pqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesf3hn0c',
      'tb',
      '5120000000c4a5cad46221b2a187905e5266362b99d5e91c6ce24d165dab93e86433',
    ],
    [
      'bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0',
      'bc',
      '512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798',
    ],
  ])('valid and accepted: %s', (address, hrp, script) => {
    expect(scriptOf(address, hrp)).toBe(script);
  });

  // Valid by BIP-350, but witness versions/sizes nobody here pays to.
  it.each([
    ['bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7kt5nd6y', 'bc'],
    ['BC1SW50QGDZ25J', 'bc'],
    ['bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs', 'bc'],
  ])('valid but not accepted: %s', (address, hrp) => {
    expect(errorOf(address, hrp)).toBe('type');
  });

  it.each([
    ['tc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq5zuyut', 'network'],
    ['bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd', 'typo'],
    ['tb1z0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqglt7rf', 'typo'],
    ['BC1S0XLXVLHEMJA6C4DQV22UAPCTQUPFHLXM9H8Z3K2E72Q4K9HCZ7VQ54WELL', 'typo'],
    ['bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh', 'typo'],
    ['tb1q0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq24jc47', 'typo'],
    ['bc1p38j9r5y49hruaue7wxjce0updqjuyyx0kh56v8s25huc6995vvpql3jow4', 'typo'],
    ['BC130XLXVLHEMJA6C4DQV22UAPCTQUPFHLXM9H8Z3K2E72Q4K9HCZ7VQ7ZWS8R', 'typo'],
    ['bc1pw5dgrnzv', 'typo'],
    ['bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7v8n0nx0muaewav253zgeav', 'typo'],
    ['BC1QR508D6QEJXTDG4Y5R3ZARVARYV98GJ9P', 'typo'],
    ['tb1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq47Zagq', 'case'],
    ['bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7v07qwwzcrf', 'typo'],
    ['tb1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vpggkg4j', 'typo'],
    ['bc1gmk9yu', 'typo'],
  ])('invalid: %s (%s)', (address, error) => {
    const hrp = address.toLowerCase().startsWith('tb1') ? 'tb' : 'bc';
    expect(errorOf(address, hrp)).toBe(error);
  });
});

// https://github.com/bitcoin/bips/blob/master/bip-0173.mediawiki#test-vectors
describe('BIP-173 vectors', () => {
  it.each([
    ['tc1qw508d6qejxtdg4y5r3zarvary0c5xw7kg3g4ty', 'network'],
    ['bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5', 'typo'],
    ['BC13W508D6QEJXTDG4Y5R3ZARVARY0C5XW7KN40WF2', 'typo'],
    ['bc1rw5uspcuh', 'typo'],
    ['bc10w508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7kw5rljs90', 'typo'],
    ['BC1QR508D6QEJXTDG4Y5R3ZARVARYV98GJ9P', 'typo'],
    ['tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sL5k7', 'case'],
    ['bc1zw508d6qejxtdg4y5r3zarvaryvqyzf3du', 'typo'],
    ['tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3pjxtptv', 'typo'],
    ['bc1gmk9yu', 'typo'],
    // invalid bech32 strings
    [
      'an84characterslonghumanreadablepartthatcontainsthenumber1andtheexcludedcharactersbio1569pvx',
      'typo',
    ],
    ['pzry9x0s0muk', 'typo'],
    ['1pzry9x0s0muk', 'typo'],
    ['x1b4n0q5v', 'typo'],
    ['li1dgmt3', 'typo'],
    ['A1G7SGD8', 'typo'],
    ['10a06t8', 'typo'],
    ['1qzzfhee', 'typo'],
  ])('invalid: %s (%s)', (address, error) => {
    const hrp = address.toLowerCase().startsWith('tb1') ? 'tb' : 'bc';
    expect(errorOf(address, hrp)).toBe(error);
  });

  it('bech32 v1+ addresses of BIP-173 are invalid after BIP-350', () => {
    expect(
      errorOf('bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7k7grplx'),
    ).toBe('typo');
    expect(errorOf('BC1SW50QA3JX3S')).toBe('typo');
    expect(errorOf('bc1zw508d6qejxtdg4y5r3zarvaryvg6kdaj')).toBe('typo');
  });
});

describe('helpers', () => {
  it('groups of four for reading', () => {
    expect(groupAddress(P2WPKH)).toBe('bc1q w508 d6qe jxtd g4y5 r3za rvar y0c5 xw7k v8f3 t4');
    expect(groupAddress('')).toBe('');
  });

  it('network names', () => {
    expect(hrpOfNetwork('mainnet')).toBe('bc');
    expect(hrpOfNetwork(undefined)).toBe('bc');
    expect(hrpOfNetwork('testnet')).toBe('tb');
    expect(hrpOfNetwork('regtest')).toBe('bcrt');
  });

  it('one text per error, with the examples of the network', () => {
    const t = (k: string, o?: Record<string, unknown>): string =>
      k.replace(/{{(\w+)}}/g, (_m, n: string) => String(o?.[n]));
    const errors: AddressError[] = ['empty', 'typo', 'case', 'network', 'type'];
    const texts = errors.map((e) => addressErrorText(e, t));
    expect(new Set(texts).size).toBe(errors.length);
    expect(addressErrorText('type', t)).toContain('bc1p…');
    expect(addressErrorText('type', t, 'tb')).toContain('tb1q…');
    expect(addressErrorText('network', t, 'tb')).toContain('2…');
  });
});

describe('texts (0.9.10)', () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const load = (l: string): Record<string, string> => require(`../../../static/locales/${l}.json`);
  const keys = [
    'Write a Bitcoin address first',
    'This address has a typo or a missing character. Check it again, or better copy and paste it from your wallet.',
    'This address mixes capital and small letters. Copy and paste it exactly as your wallet shows it.',
    'This address is for another network. This coordinator only takes addresses of its own network: {{hrp}}1…, {{p2pkh}} or {{p2sh}}',
    'This kind of address is not accepted. Use legacy ({{p2pkh}}), P2SH ({{p2sh}}), segwit ({{hrp}}1q…) or taproot ({{hrp}}1p…).',
    'Check the address',
    'Compare it with the address in your wallet, character by character. Sats sent to a wrong address cannot be recovered.',
    'Yes, use this address',
    'Your payout (and your bond back) will go to:',
    'Your bond back (and your refund if the trade is cancelled) will go to:',
    'Your bond of {{amount}} Sats will come back to:',
    'Your bond will come back to:',
    'Your {{amount}} Sats will go back to:',
    'Your sats will go back to:',
    'The rescue will send {{amount}} Sats (minus the mining fee) to:',
    'Your payout of {{amount}} Sats will go to:',
    'Min {{min}}, max {{max}} sat/vB',
    'Bond returned to',
  ];
  it.each(keys)('%s is in every locale, translated in Spanish and Catalan', (k) => {
    for (const l of ['en', 'fr', 'de', 'zh-SI']) expect(load(l)[k]).toBe(k);
    for (const l of ['es', 'ca']) {
      const v = load(l)[k];
      expect(v && v !== k).toBeTruthy();
      // the same placeholders
      expect((v.match(/{{\w+}}/g) ?? []).sort()).toEqual((k.match(/{{\w+}}/g) ?? []).sort());
    }
  });
});
