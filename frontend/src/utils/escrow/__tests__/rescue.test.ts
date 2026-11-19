import { pendingPayments } from '../rescue';
import { schnorr } from '@noble/curves/secp256k1.js';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';
import { execSync } from 'child_process';

import {
  escrowKeys,
  myBondIndexes,
  owedInRelease,
  signBondLeftover,
  signBondAutoReturn,
  signBondPenalties,
  type EscrowOrderData,
} from '../client';
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

describe('bond top-ups and coins at the bond address (rl31-beta18)', () => {
  const bondData = (keys: typeof buyer, extra: Partial<EscrowOrderData> = {}): EscrowOrderData => {
    const b = new BondContract(pubkey(keys.bond), hexToBytes(coordPk), 540, 'bcrt');
    return {
      id: 42,
      escrow_alias: 'kilobot',
      bond_address: b.address,
      bond_coordinator_pubkey: coordPk,
      bond_refund_csv: 540,
      bond_hrp: 'bcrt',
      ...extra,
    };
  };
  const coins = [
    { txid: 'a1'.repeat(32), vout: 0, value: 3300 },
    { txid: 'a2'.repeat(32), vout: 3, value: 330 },
  ];

  it('signs the penalty of a two-coin bond, one signature per coin', () => {
    const b = new BondContract(pubkey(buyer.bond), hexToBytes(coordPk), 540, 'bcrt');
    const pen = new Transaction(
      coins.map((c) => ({ txid: c.txid, vout: c.vout, sequence: 0xfffffffd })),
      [{ value: BigInt(3000), script: dest }],
      2,
      0,
    );
    const d = bondData(buyer, {
      bond_txid: coins[0].txid,
      bond_vout: 0,
      bond_value: 3630,
      bond_coins: coins,
      bond_penalty_tx: bytesToHex(pen.serialize()),
      bond_status_code: 1,
    });
    const sigs = signBondPenalties(d, buyer);
    expect(sigs).toHaveLength(2);
    const spent = coins.map((c) => ({ value: BigInt(c.value), script: b.scriptPubkey }));
    sigs.forEach((s, i) =>
      expect(
        schnorr.verify(
          hexToBytes(s).slice(0, 64),
          pen.sighashUnified(i, spent, b.leafHashes[BondContract.COOPERATIVE]),
          xonly(pubkey(buyer.bond)),
        ),
      ).toBe(true),
    );
    // a penalty that spends something else is refused
    const other = new Transaction(
      [{ txid: 'ff'.repeat(32), vout: 0, sequence: 0xfffffffd }, ...pen.inputs.slice(1)],
      pen.outputs,
      2,
      0,
    );
    expect(() =>
      signBondPenalties({ ...d, bond_penalty_tx: bytesToHex(other.serialize()) }, buyer),
    ).toThrow();
  });

  it('0.9.7: pre-signs the automatic return only for exactly the bond, to our address', () => {
    const b = new BondContract(pubkey(buyer.bond), hexToBytes(coordPk), 540, 'bcrt');
    const ret = new Transaction(
      coins.map((c) => ({ txid: c.txid, vout: c.vout, sequence: 0xfffffffd })),
      [{ value: BigInt(3400), script: dest }],
      2,
      0,
    );
    const d = bondData(buyer, {
      bond_txid: coins[0].txid,
      bond_vout: 0,
      bond_value: 3630,
      bond_coins: coins,
      bond_status_code: 1,
      bond_auto_return_tx: bytesToHex(ret.serialize()),
      bond_auto_return_coins: coins,
    });
    const sigs = signBondAutoReturn(d, buyer, dest, 2000);
    expect(sigs).toHaveLength(2);
    const spent = coins.map((c) => ({ value: BigInt(c.value), script: b.scriptPubkey }));
    sigs.forEach((sg, i) =>
      expect(
        schnorr.verify(
          hexToBytes(sg).slice(0, 64),
          ret.sighashUnified(i, spent, b.leafHashes[BondContract.COOPERATIVE]),
          xonly(pubkey(buyer.bond)),
        ),
      ).toBe(true),
    );
    // to another address, too expensive, or with other coins: refused
    expect(() =>
      signBondAutoReturn(d, buyer, hexToBytes('0014' + '44'.repeat(20)), 2000),
    ).toThrow();
    expect(() => signBondAutoReturn(d, buyer, dest, 100)).toThrow();
    expect(() =>
      signBondAutoReturn({ ...d, bond_auto_return_coins: [coins[0]] }, buyer, dest, 2000),
    ).toThrow();
  });

  it('signs the leftover return only to our address and never with the bond coin', () => {
    const b = new BondContract(pubkey(buyer.bond), hexToBytes(coordPk), 540, 'bcrt');
    const left = { txid: 'b1'.repeat(32), vout: 1, value: 3300 };
    const ret = new Transaction(
      [{ txid: left.txid, vout: left.vout, sequence: 0xfffffffd }],
      [{ value: BigInt(3000), script: dest }],
      2,
      0,
    );
    const d = bondData(buyer, {
      bond_txid: 'c1'.repeat(32),
      bond_vout: 0,
      bond_value: 3330,
      bond_status_code: 2,
      bond_leftover_tx: bytesToHex(ret.serialize()),
      bond_leftover_coins: [left],
    });
    const [sig] = signBondLeftover(d, buyer, dest, 20000);
    expect(
      schnorr.verify(
        hexToBytes(sig).slice(0, 64),
        ret.sighashUnified(
          0,
          [{ value: BigInt(3300), script: b.scriptPubkey }],
          b.leafHashes[BondContract.COOPERATIVE],
        ),
        xonly(pubkey(buyer.bond)),
      ),
    ).toBe(true);
    expect(() => signBondLeftover(d, buyer, hexToBytes('0014' + '44'.repeat(20)), 20000)).toThrow();
    const withBond = {
      ...d,
      bond_leftover_coins: [{ txid: 'c1'.repeat(32), vout: 0, value: 3300 }],
      bond_leftover_tx: bytesToHex(
        new Transaction(
          [{ txid: 'c1'.repeat(32), vout: 0, sequence: 0xfffffffd }],
          [{ value: BigInt(3000), script: dest }],
          2,
          0,
        ).serialize(),
      ),
    };
    expect(() => signBondLeftover(withBond, buyer, dest, 20000)).toThrow();
  });

  it('refunds several coins of the bond address alone, each with the refund leaf', () => {
    const rec = {
      ...record('buyer'),
      bond: { coordinatorPk: coordPk, refundCsv: 540, address: '' },
    };
    const b = new BondContract(pubkey(buyer.bond), hexToBytes(coordPk), 540, 'bcrt');
    rec.bond.address = b.address;
    const built = buildBondRefund(rec, buyer, coins, dest, 2);
    const tx = Transaction.parse(built.hex);
    expect(tx.inputs.map((i) => i.sequence)).toEqual([540, 540]);
    expect(built.sent + built.fee).toBe(3630);
    const spent = coins.map((c) => ({ value: BigInt(c.value), script: b.scriptPubkey }));
    witnesses(built.hex).forEach(([sig, script], i) => {
      expect(script).toBe(bytesToHex(b.leaves[BondContract.REFUND]));
      expect(
        schnorr.verify(
          hexToBytes(sig).slice(0, 64),
          tx.sighashUnified(i, spent, b.leafHashes[BondContract.REFUND]),
          xonly(pubkey(buyer.bond)),
        ),
      ).toBe(true);
    });
  });

  it('signs every bond input of the deposit', () => {
    expect(myBondIndexes({ id: 1, escrow_deposit_my_bond_input: 2 })).toEqual([2]);
    expect(
      myBondIndexes({
        id: 1,
        escrow_deposit_my_bond_input: 2,
        escrow_deposit_my_bond_inputs: [2, 3],
      }),
    ).toEqual([2, 3]);
    expect(myBondIndexes({ id: 1 })).toEqual([]);
  });
});

maybe(
  'a short payment and a top-up at the bond address come back alone after 540 blocks (rl31-beta18)',
  () => {
    const cli = (a: string): string => execSync(`${BCLI} ${a}`, { encoding: 'utf8' }).trim();
    const mine = (n: number): void => {
      while (n > 0) {
        cli(`generatetoaddress ${Math.min(n, 200)} ${cli('-rpcwallet=miner getnewaddress')}`);
        n -= 200;
      }
    };
    const b = new BondContract(pubkey(buyer.bond), hexToBytes(coordPk), 540, 'bcrt');
    const fund = (sats: number): { txid: string; vout: number; value: number } => {
      const txid = cli(
        `-rpcwallet=miner -named sendtoaddress address=${b.address} amount=${(sats / 1e8).toFixed(8)} fee_rate=2`,
      );
      const t = JSON.parse(cli(`getrawtransaction ${txid} true`));
      const vout = t.vout.findIndex(
        (o: { scriptPubKey: { address: string } }) => o.scriptPubKey.address === b.address,
      );
      return { txid, vout, value: sats };
    };
    const destAddr = cli('-rpcwallet=miner getnewaddress refund bech32m');
    const destScript = hexToBytes(
      JSON.parse(cli(`-rpcwallet=miner getaddressinfo ${destAddr}`)).scriptPubKey,
    );
    const rec = {
      ...record('buyer'),
      bond: { coordinatorPk: coordPk, refundCsv: 540, address: b.address },
    };
    const us = [fund(3300), fund(330)];
    mine(1);
    const built = buildBondRefund(rec, buyer, us, destScript, 2);
    const accept = (hex: string): { allowed: boolean; 'reject-reason'?: string } =>
      JSON.parse(cli(`testmempoolaccept '["${hex}"]'`))[0];
    mine(538); // 539 confirmations: one short
    expect(accept(built.hex)['reject-reason']).toMatch(/non-BIP68-final|sequence/i);
    mine(1);
    expect(accept(built.hex)).toMatchObject({ allowed: true });
    const txid = cli(`sendrawtransaction ${built.hex}`);
    mine(1);
    const t = JSON.parse(cli(`getrawtransaction ${txid} true`));
    expect(t.confirmations).toBeGreaterThanOrEqual(1);
    const got = t.vout
      .filter(
        (o: { scriptPubKey: { hex: string } }) => o.scriptPubKey.hex === bytesToHex(destScript),
      )
      .reduce((a: number, o: { value: number }) => a + Math.round(o.value * 1e8), 0);
    expect(got).toBe(3630 - built.fee);
    expect(got).toBe(built.sent);
    expect(Math.ceil(t.vsize * 2)).toBeLessThanOrEqual(built.fee);
  },
  300000,
);

// rl31-beta19: what is still unconfirmed at an address shows as pending in the rescue

describe('pendingPayments', () => {
  it('counts unconfirmed payments (by transaction) and their sats', () => {
    // order #22: 3,300 confirmed, 3,330 in the mempool
    const us = [
      { txid: 'a'.repeat(64), vout: 0, value: 3300, height: 100 },
      { txid: 'b'.repeat(64), vout: 1, value: 3330, height: null },
    ];
    expect(pendingPayments(us)).toEqual({ count: 1, sats: 3330, utxos: [us[1]] });
    const two = [...us, { txid: 'c'.repeat(64), vout: 0, value: 500, height: null }];
    expect(pendingPayments(two).count).toBe(2);
    expect(pendingPayments(two).sats).toBe(3830);
    expect(pendingPayments([us[0]])).toEqual({ count: 0, sats: 0, utxos: [] });
  });
});

// rl31-beta20 (Kilobot #22: 331 sats at ~4.7 sat/vB): a leftover the miners would eat is not
// pushed; the coordinator uses the same rule.
describe('uneconomic leftovers', () => {
  it('recoveryFee and worthRecovering', () => {
    const { recoveryFee, worthRecovering } = jest.requireActual('../rescue');
    const fee = recoveryFee('funding', 1, 4.7);
    expect(fee).toBe(Math.ceil((11 + 58 + 43) * 4.7));
    expect(worthRecovering(331, fee)).toBe(false);
    expect(worthRecovering(3300, fee)).toBe(true);
    expect(worthRecovering(2 * fee, fee)).toBe(true);
    expect(worthRecovering(2 * fee - 1, fee)).toBe(false);
    expect(worthRecovering(339, 10)).toBe(false);
    expect(recoveryFee('bond', 2, 2)).toBeGreaterThan(recoveryFee('bond', 1, 2));
  });
});
