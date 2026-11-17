import { schnorr } from '@noble/curves/secp256k1.js';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';
import { execSync } from 'child_process';

import { escrowKeys, owedInRelease, type EscrowOrderData } from '../client';
import {
  buildBondRefund,
  buildEscrowRescue,
  buildFundingSweep,
  decryptRecords,
  encryptRecords,
  recordFromOrder,
  blocksLeft,
  type RescueRecord,
} from '../rescue';
import {
  BondContract,
  EscrowContract,
  Transaction,
  fundingScript,
  p2trAddress,
  pubkey,
  xonly,
} from '../taproot';

const coordSk = hexToBytes('07'.repeat(32));
const coordPk = bytesToHex(pubkey(coordSk));
const seller = escrowKeys('seller-token-'.padEnd(40, 's'), 'kilobot', 42);
const buyer = escrowKeys('buyer-token-'.padEnd(40, 'b'), 'kilobot', 42);
const dest = hexToBytes('0014' + '33'.repeat(20));

const record = (role: 'seller' | 'buyer', hrp = 'bcrt'): RescueRecord => {
  const keys = role === 'seller' ? seller : buyer;
  const c = new EscrowContract(
    pubkey(seller.escrow),
    pubkey(buyer.escrow),
    hexToBytes(coordPk),
    20,
    hrp,
  );
  const b = new BondContract(pubkey(keys.bond), hexToBytes(coordPk), 30, hrp);
  return {
    v: 1,
    shortAlias: 'kilobot',
    coordinator: 'kilobot',
    orderId: 42,
    hrp,
    savedAt: 0,
    role,
    escrow: {
      sellerPk: bytesToHex(pubkey(seller.escrow)),
      buyerPk: bytesToHex(pubkey(buyer.escrow)),
      coordinatorPk: coordPk,
      rescueCsv: 20,
      address: c.address,
    },
    bond: { coordinatorPk: coordPk, refundCsv: 30, address: b.address },
  };
};

// Parses a witness transaction and returns its witness stacks (Transaction.parse drops them).
const witnesses = (hex: string): string[][] => {
  const raw = hexToBytes(hex);
  let pos = 4;
  const rd = (k: number): Uint8Array => raw.slice(pos, (pos += k));
  const cs = (): number => {
    const f = rd(1)[0];
    return f < 0xfd ? f : Number(new DataView(rd(f === 0xfd ? 2 : 4).buffer).getUint16(0, true));
  };
  rd(2);
  const nIn = cs();
  for (let i = 0; i < nIn; i++) {
    rd(36);
    rd(cs());
    rd(4);
  }
  const nOut = cs();
  for (let i = 0; i < nOut; i++) {
    rd(8);
    rd(cs());
  }
  const out: string[][] = [];
  for (let i = 0; i < nIn; i++) {
    const stack: string[] = [];
    for (let k = cs(); k > 0; k--) stack.push(bytesToHex(rd(cs())));
    out.push(stack);
  }
  return out;
};

describe('rescue without the coordinator (rl31-beta12)', () => {
  it('keeps the records encrypted with the token', () => {
    const recs = { 'kilobot/42': record('seller') };
    const blob = encryptRecords('tok', recs);
    expect(blob).not.toContain('kilobot');
    expect(decryptRecords('tok', blob)).toEqual(recs);
    expect(decryptRecords('other', blob)).toEqual({});
  });

  it('builds the record from the order data', () => {
    const r = record('seller');
    const data: EscrowOrderData = {
      id: 42,
      escrow_alias: 'kilobot',
      escrow_hrp: 'bcrt',
      escrow_role: 'seller',
      escrow_seller_pubkey: r.escrow!.sellerPk,
      escrow_buyer_pubkey: r.escrow!.buyerPk,
      escrow_coordinator_pubkey: coordPk,
      escrow_rescue_csv: 20,
      escrow_address: r.escrow!.address,
      escrow_output_sats: 120000,
      escrow_funding_txid: 'cd'.repeat(32),
    };
    const rec = recordFromOrder(data, 'kilobot');
    expect(rec?.escrow?.depositTxid).toBe('cd'.repeat(32));
    expect(rec?.escrow?.outputSats).toBe(120000);
  });

  it('the seller signs the rescue leaf with a valid signature', () => {
    const rec = record('seller');
    const c = new EscrowContract(
      pubkey(seller.escrow),
      pubkey(buyer.escrow),
      hexToBytes(coordPk),
      20,
      'bcrt',
    );
    const utxo = { txid: 'ab'.repeat(32), vout: 0, value: 120000 };
    const built = buildEscrowRescue(rec, seller, utxo, dest, 2);
    const tx = Transaction.parse(built.hex);
    expect(tx.inputs[0].sequence).toBe(20);
    expect(Number(tx.outputs[0].value)).toBe(built.sent);
    const [sig, script, cb] = witnesses(built.hex)[0];
    expect(script).toBe(bytesToHex(c.leaves[2]));
    expect(cb).toBe(bytesToHex(c.controlBlock(2)));
    const msg = tx.sighashUnified(
      0,
      [{ value: BigInt(120000), script: c.scriptPubkey }],
      c.leafHashes[2],
    );
    expect(schnorr.verify(hexToBytes(sig).slice(0, 64), msg, xonly(pubkey(seller.escrow)))).toBe(
      true,
    );
  });

  it('the buyer cannot use the seller rescue', () => {
    const utxo = { txid: 'ab'.repeat(32), vout: 0, value: 120000 };
    expect(() => buildEscrowRescue(record('buyer'), buyer, utxo, dest, 2)).toThrow();
  });

  it('bond refund and funding sweep', () => {
    const rec = record('buyer');
    const b = buildBondRefund(
      rec,
      buyer,
      { txid: 'ef'.repeat(32), vout: 1, value: 15000 },
      dest,
      2,
    );
    expect(Transaction.parse(b.hex).inputs[0].sequence).toBe(30);
    const s = buildFundingSweep(
      rec,
      buyer,
      [
        { txid: '01'.repeat(32), vout: 0, value: 10000 },
        { txid: '02'.repeat(32), vout: 1, value: 5000 },
      ],
      dest,
      2,
    );
    const tx = Transaction.parse(s.hex);
    expect(tx.inputs).toHaveLength(2);
    const spent = [10000, 5000].map((v) => ({
      value: BigInt(v),
      script: fundingScript(pubkey(buyer.funding)),
    }));
    const w = witnesses(s.hex);
    for (let i = 0; i < 2; i++)
      expect(
        schnorr.verify(
          hexToBytes(w[i][0]).slice(0, 64),
          tx.sighashUnified(i, spent),
          xonly(pubkey(buyer.funding)),
        ),
      ).toBe(true);
  });

  it('counts the blocks still to wait', () => {
    expect(blocksLeft(100, 20, 110)).toBe(9);
    expect(blocksLeft(100, 20, 119)).toBe(0);
    expect(blocksLeft(null, 20, 119)).toBeNull();
  });
});

describe('cooperative releases are checked to the sat (rl31-beta12)', () => {
  const base = (role: 'seller' | 'buyer', cancel: boolean): EscrowOrderData => ({
    id: 1,
    escrow_role: role,
    escrow_release_kind: 'Seller and buyer (key path)',
    escrow_release_cancel: cancel,
    escrow_trade_sats: 100000,
    escrow_sats: 100000,
    escrow_bond_in_escrow: { seller: 15000, buyer: 14000 },
    escrow_output_sats: 129000,
    bond_txid: 'aa'.repeat(32),
    bond_value: role === 'seller' ? 15000 : 15000,
    escrow_fee_from_bond: role === 'buyer',
    escrow_funding_recommended: 1500,
  });
  it('collaborative cancel: buyer owed exactly its bond, seller the rest less the fee', () => {
    expect(owedInRelease(base('buyer', true))).toEqual({ due: 14000, paysFee: false });
    expect(owedInRelease(base('seller', true))).toEqual({ due: 115000, paysFee: true });
  });
  it('trade release: seller exactly its bond, buyer the rest less the fee', () => {
    expect(owedInRelease(base('seller', false))).toEqual({ due: 15000, paysFee: false });
    expect(owedInRelease(base('buyer', false))).toEqual({ due: 114000, paysFee: true });
  });
  it('refuses amounts that do not add up or shrink our bond', () => {
    expect(() => owedInRelease({ ...base('buyer', true), escrow_output_sats: 130000 })).toThrow();
    expect(() =>
      owedInRelease({
        ...base('buyer', true),
        escrow_bond_in_escrow: { seller: 21500, buyer: 7500 },
      }),
    ).toThrow();
    expect(() =>
      owedInRelease({
        ...base('seller', true),
        escrow_trade_sats: 90000,
        escrow_bond_in_escrow: { seller: 25000, buyer: 14000 },
      }),
    ).toThrow();
  });
  it('dispute releases are not checked here', () => {
    expect(
      owedInRelease({ ...base('buyer', false), escrow_release_kind: 'Buyer and coordinator' }),
    ).toBeNull();
  });
});

// Real regtest check (lab node with BLAKE2b): RESCUE_REGTEST=1 and the lab's bitcoin-cli.
const BCLI = process.env.RESCUE_REGTEST ? '/mnt/datos2/roblosats/qa-lab/bcli' : '';
const maybe = BCLI ? test : test.skip;
maybe(
  'the three rescues are accepted by a real BLAKE2b regtest node',
  () => {
    const cli = (a: string): string => execSync(`${BCLI} ${a}`, { encoding: 'utf8' }).trim();
    const mine = (n: number): void => {
      cli(`generatetoaddress ${n} ${cli('-rpcwallet=miner getnewaddress')}`);
    };
    const fund = (addr: string, sats: number): { txid: string; vout: number } => {
      const txid = cli(
        `-rpcwallet=miner -named sendtoaddress address=${addr} amount=${(sats / 1e8).toFixed(8)} fee_rate=2`,
      );
      const t = JSON.parse(cli(`getrawtransaction ${txid} true`));
      const vout = t.vout.findIndex(
        (o: { scriptPubKey: { address: string } }) => o.scriptPubKey.address === addr,
      );
      return { txid, vout };
    };
    const destAddr = cli('-rpcwallet=miner getnewaddress rescue bech32m');
    const destScript = hexToBytes(
      JSON.parse(cli(`-rpcwallet=miner getaddressinfo ${destAddr}`)).scriptPubKey,
    );
    const rec = record('seller');
    const c = new EscrowContract(
      pubkey(seller.escrow),
      pubkey(buyer.escrow),
      hexToBytes(coordPk),
      20,
      'bcrt',
    );
    const b = new BondContract(pubkey(seller.bond), hexToBytes(coordPk), 30, 'bcrt');
    const faddr = p2trAddress(xonly(pubkey(seller.funding)), 'bcrt');
    const e = fund(c.address, 120000);
    const bd = fund(b.address, 15000);
    const fd = fund(faddr, 9000);
    mine(1);
    const accept = (hex: string): { allowed: boolean; 'reject-reason'?: string } =>
      JSON.parse(cli(`testmempoolaccept '["${hex}"]'`))[0];
    // funding: any time
    const s = buildFundingSweep(rec, seller, [{ ...fd, value: 9000 }], destScript, 2);
    expect(accept(s.hex)).toMatchObject({ allowed: true });
    // timelocks not reached yet
    const er = buildEscrowRescue(rec, seller, { ...e, value: 120000 }, destScript, 2);
    expect(accept(er.hex)['reject-reason']).toMatch(/non-BIP68-final|sequence/i);
    mine(19);
    expect(accept(er.hex)).toMatchObject({ allowed: true });
    cli(`sendrawtransaction ${er.hex}`);
    mine(10);
    const br = buildBondRefund(rec, seller, { ...bd, value: 15000 }, destScript, 2);
    expect(accept(br.hex)).toMatchObject({ allowed: true });
    cli(`sendrawtransaction ${br.hex}`);
    cli(`sendrawtransaction ${s.hex}`);
    mine(1);
  },
  120000,
);

describe('deposit spend limit (17 nov 2026, order #21)', () => {
  it('tolerates the recommendation drifting a few sats after we paid', () => {
    const { depositSpendLimit } = require('../client');
    const limit = depositSpendLimit({
      id: 21,
      escrow_funding_recommended: 90993,
      bond_value: 13538,
    });
    expect(90994 + 13538).toBeLessThanOrEqual(limit);
    expect(limit).toBe(90993 + 910 + 13538);
    expect(depositSpendLimit({ id: 1, escrow_funding_recommended: 20000, bond_value: 3330 })).toBe(
      20000 + 546 + 3330,
    );
  });
});
