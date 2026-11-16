import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';
import { escrowKeys, signSweep, type EscrowOrderData } from '../client';
import { fundingScript, pubkey } from '../taproot';

// Plain (non-witness) serialization, enough for Transaction.parse.
const le = (n: number, k: number): string => {
  const b = new Uint8Array(k);
  let v = BigInt(n);
  for (let i = 0; i < k; i++) {
    b[i] = Number(v & BigInt(255));
    v >>= BigInt(8);
  }
  return bytesToHex(b);
};
const txHex = (ins: Array<[string, number]>, outs: Array<[number, Uint8Array]>): string =>
  le(2, 4) +
  le(ins.length, 1) +
  ins.map(([t, v]) => bytesToHex(hexToBytes(t).reverse()) + le(v, 4) + '00' + 'fdffffff').join('') +
  le(outs.length, 1) +
  outs.map(([v, s]) => le(v, 8) + le(s.length, 1) + bytesToHex(s)).join('') +
  le(0, 4);

describe('funding sweep (QA 16 nov 2026): the client only signs a sweep of its own coins to itself', () => {
  const keys = escrowKeys('t'.repeat(36), 'kilobot', 7);
  const mine = fundingScript(pubkey(keys.funding));
  const me = hexToBytes('5120' + '11'.repeat(32));
  const thief = hexToBytes('5120' + '22'.repeat(32));
  const txid = 'ab'.repeat(32);
  const data = (outs: Array<[number, Uint8Array]>, spentScript = mine): EscrowOrderData => ({
    escrow_sweep_tx: txHex([[txid, 0]], outs),
    escrow_sweep_spent: [[30000, bytesToHex(spentScript)]],
  });
  it('signs a sweep to our address', () => {
    expect(signSweep(data([[29000, me]]), keys, me, 2000)).toHaveLength(1);
  });
  it('refuses a sweep that pays someone else', () => {
    expect(() => signSweep(data([[29000, thief]]), keys, me, 2000)).toThrow();
    expect(() => signSweep(data([[15000, me], [14000, thief]]), keys, me, 2000)).toThrow();
  });
  it('refuses a sweep with a big fee', () => {
    expect(() => signSweep(data([[20000, me]]), keys, me, 2000)).toThrow();
  });
  it('refuses to sign coins that are not on our funding address', () => {
    expect(() => signSweep(data([[29000, me]], thief), keys, me, 2000)).toThrow();
  });
});
