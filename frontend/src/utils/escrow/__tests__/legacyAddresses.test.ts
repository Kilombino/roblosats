/**
 * 0.9.9: legacy receive addresses (Kilombino: "1…" and "3…" too), their dust thresholds, the
 * deposit change counted at our payout address, and the texts that say which types are taken.
 */
import { addressToScript, bytesToHex, dustThreshold, validOnchainAddress } from '../taproot';
import { myChangeScripts, type EscrowOrderData } from '../client';
import es from '../../../../static/locales/es.json';
import ca from '../../../../static/locales/ca.json';
import en from '../../../../static/locales/en.json';

const hex = (a: string): string => bytesToHex(addressToScript(a));

describe('receive addresses', () => {
  it('P2PKH and P2SH, mainnet and regtest', () => {
    expect(hex('1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2')).toBe(
      '76a91477bff20c60e522dfaa3350c39b030a5d004e839a88ac',
    );
    expect(hex('3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy')).toBe(
      'a914b472a266d0bd89c13706a4132ccfb16f7c3b9fcb87',
    );
    expect(hex('mvv7Xc2rHoG6PxZaWnDeiaNBRFYSQTKZgT')).toBe(
      '76a914a8eb2966fb60bee43d6c646d88979eb155429eeb88ac',
    );
  });

  it('segwit and taproot as before', () => {
    expect(hex('bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4')).toBe(
      '0014751e76e8199196d454941c45d1b3a323f1433bd6',
    );
    expect(
      validOnchainAddress('bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0'),
    ).toBe(true);
  });

  it('refuses a bad checksum or something else', () => {
    expect(validOnchainAddress('1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3')).toBe(false);
    expect(validOnchainAddress('hello')).toBe(false);
    expect(validOnchainAddress('')).toBe(false);
  });
});

describe('dust per script type (node at 3000 sat/kvB)', () => {
  it('matches Bitcoin Core, never below 330', () => {
    expect(dustThreshold(addressToScript('1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2'))).toBe(546);
    expect(dustThreshold(addressToScript('3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy'))).toBe(540);
    expect(dustThreshold(addressToScript('bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4'))).toBe(330);
  });
});

describe('deposit change at our payout address', () => {
  it('counts our payout script as ours', () => {
    const keys = {
      escrow: new Uint8Array(32).fill(1),
      funding: new Uint8Array(32).fill(2),
      bond: new Uint8Array(32).fill(3),
    };
    const data = {
      escrow_my_payout_address: '1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2',
    } as unknown as EscrowOrderData;
    const mine = myChangeScripts(data, keys);
    expect(mine.has('76a91477bff20c60e522dfaa3350c39b030a5d004e839a88ac')).toBe(true);
    expect(mine.size).toBe(2); // and our funding address
  });
});

describe('texts', () => {
  const keys = [
    'Use a valid address: legacy (1…), P2SH (3…), segwit (bc1q…) or taproot (bc1p…)',
    'Accepted: 1…, 3…, bc1q… or bc1p…',
    'Pay {{amount}} Sats. Anything left over comes back by itself to your address {{address}} in the deposit (a few sats below the dust limit go to the miners). If you send less, send the rest to the same address before the order expires.',
  ];
  it.each(keys)('%s is in English, Spanish and Catalan', (k) => {
    const e = en as Record<string, string>;
    const s = es as Record<string, string>;
    const c = ca as Record<string, string>;
    expect(e[k]).toBe(k);
    expect(s[k] && s[k] !== k).toBeTruthy();
    expect(c[k] && c[k] !== k).toBeTruthy();
  });
});
