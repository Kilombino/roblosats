import { schnorr } from '@noble/curves/secp256k1.js';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';

import {
  escrowKeys,
  owedInRelease,
  publicKeys,
  signCooperativeLeaf,
  type EscrowOrderData,
} from '../client';
import { EscrowContract, Transaction } from '../taproot';

// 0.9.5: the seller alone signs the cooperative release on leaf 0 (seller + coordinator).
const keys = {
  seller: escrowKeys('token-del-vendedor', 'kilobot', 24),
  buyer: escrowKeys('token-del-comprador', 'kilobot', 24),
  coord: escrowKeys('coordinador', 'kilobot', 24),
};
const SELLER_SPK = hexToBytes('0014' + 'a1'.repeat(20));
const BUYER_SPK = hexToBytes('5120' + 'b2'.repeat(32));

const data = (over: Partial<EscrowOrderData> = {}): EscrowOrderData => {
  const base = {
    id: 24,
    escrow_role: 'seller',
    escrow_status_code: 4,
    escrow_release_kind: 'Seller and buyer (key path)',
    escrow_seller_pubkey: publicKeys(keys.seller).escrow,
    escrow_buyer_pubkey: publicKeys(keys.buyer).escrow,
    escrow_coordinator_pubkey: publicKeys(keys.coord).escrow,
    escrow_rescue_csv: 2016,
    escrow_rescue_plan: 2,
    escrow_hrp: 'bc',
    escrow_trade_sats: 1_000_000,
    escrow_sats: 1_000_000,
    escrow_bond_in_escrow: { seller: 30_000, buyer: 30_000 },
    escrow_fee_plan: 2,
    escrow_fees_in_escrow: { seller: 0, buyer: 0 },
    escrow_output_sats: 1_060_000,
    escrow_release_coordinator_sats: 0,
    escrow_release_leaf_ok: true,
    ...over,
  } as EscrowOrderData;
  const tx = new Transaction(
    [{ txid: '11'.repeat(32), vout: 0, sequence: 0xfffffffd }],
    [
      { value: BigInt(1_029_500), script: BUYER_SPK }, // the buyer pays the mining fee
      { value: BigInt(30_000), script: SELLER_SPK },
    ],
  );
  return { ...base, escrow_release_tx: bytesToHex(tx.serialize()) } as EscrowOrderData;
};

const check = (d: EscrowOrderData) => ({
  myScript: SELLER_SPK,
  minToMe: 0,
  owed: owedInRelease(d)!,
});

describe('signCooperativeLeaf (0.9.5)', () => {
  test('the seller signs leaf 0 of the release it checked', () => {
    const d = data();
    const sig = hexToBytes(signCooperativeLeaf(d, keys.seller, check(d)));
    expect(sig.length).toBe(65);
    expect(sig[64]).toBe(0x21);
    const c = new EscrowContract(
      hexToBytes(d.escrow_seller_pubkey!),
      hexToBytes(d.escrow_buyer_pubkey!),
      hexToBytes(d.escrow_coordinator_pubkey!),
      2016,
      'bc',
      2,
    );
    const tx = Transaction.parse(d.escrow_release_tx!);
    const spent = [{ value: BigInt(1_060_000), script: c.scriptPubkey }];
    const msg = tx.sighashUnified(0, spent, c.leafHashes[EscrowContract.SELLER_COORDINATOR]);
    const sellerX = hexToBytes(d.escrow_seller_pubkey!).slice(1);
    expect(schnorr.verify(sig.slice(0, 64), msg, sellerX)).toBe(true);
    // not valid for the key path nor the buyer's leaf
    expect(schnorr.verify(sig.slice(0, 64), tx.sighashUnified(0, spent), sellerX)).toBe(false);
    expect(
      schnorr.verify(
        sig.slice(0, 64),
        tx.sighashUnified(0, spent, c.leafHashes[EscrowContract.BUYER_COORDINATOR]),
        sellerX,
      ),
    ).toBe(false);
  });

  test('a release that does not pay our bond back is refused', () => {
    const d = data({ escrow_bond_in_escrow: { seller: 31_000, buyer: 29_000 } });
    expect(() => signCooperativeLeaf(d, keys.seller, check(d))).toThrow(
      'the release does not pay us exactly what we are owed',
    );
  });

  test('only the seller, only a cooperative release', () => {
    const d = data();
    expect(() => signCooperativeLeaf({ ...d, escrow_role: 'buyer' }, keys.buyer, check(d))).toThrow(
      'only the seller signs the release through its leaf',
    );
    expect(() =>
      signCooperativeLeaf(
        { ...d, escrow_release_kind: 'Buyer and coordinator' },
        keys.seller,
        check(d),
      ),
    ).toThrow('this is not a cooperative release');
  });
});
