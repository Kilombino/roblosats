/**
 * Cross test against the real coordinator (Django + PostgreSQL + a regtest node with the
 * BLAKE2b fork), through the lab harness (/mnt/datos2/roblosats/lab-onchain/py/harness.py).
 * Runs only with ESCROW_E2E_URL set (e.g. http://127.0.0.1:7788). Both robots are played
 * with this client code alone: keys from the token, address checks, bond penalty, deposit and
 * the two MuSig2 rounds of the cooperative release.
 */
import {
  escrowKeys,
  publicKeys,
  signBondPenalty,
  signDeposit,
  releaseNonce,
  releasePartial,
  checkFundingAddress,
  owedInRelease,
  type EscrowOrderData,
} from '../client';
import { addressToScript } from '../taproot';

const URL = process.env.ESCROW_E2E_URL;
const maybe = URL ? test : test.skip;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const call = async (path: string, body: object = {}): Promise<any> => {
  const r = await fetch(`${URL}/${path}`, { method: 'POST', body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(`${path}: ${j.error}`);
  return j;
};

const toChat = async () => {
  const { order_id: id, alias } = await call('setup');
  const tokens = { buyer: 'buyer-token-' + id, seller: 'seller-token-' + id };
  const keys = {
    buyer: escrowKeys(tokens.buyer, alias, id),
    seller: escrowKeys(tokens.seller, alias, id),
  };
  const data = async (user: string): Promise<EscrowOrderData & { status: number }> =>
    await call('data', { user });

  const postBond = async (user: 'buyer' | 'seller'): Promise<void> => {
    await call('action', {
      user,
      name: 'bond_key',
      data: { bond_pubkey: publicKeys(keys[user]).bond },
    });
    let d = await data(user);
    await call('pay', { address: d.bond_address, sats: d.bond_required_sats });
    await call('advance');
    d = await data(user);
    await call('action', {
      user,
      name: 'bond_penalty_signature',
      data: { signature: signBondPenalty(d, keys[user]) },
    });
    await call('advance');
  };

  await postBond('buyer');
  expect((await data('buyer')).status).toBe(1);
  await call('take');
  await postBond('seller');
  expect((await data('buyer')).status).toBe(6);

  const payout: Record<string, string> = {};
  for (const user of ['seller', 'buyer'] as const) {
    const pk = publicKeys(keys[user]);
    await call('action', {
      user,
      name: 'escrow_keys',
      data: { escrow_pubkey: pk.escrow, funding_pubkey: pk.funding },
    });
    payout[user] = (await call('newaddress')).address;
    await call('action', { user, name: 'escrow_address', data: { address: payout[user] } });
  }
  await call('advance');
  expect((await data('buyer')).status).toBe(7);

  // The buyer sends nothing: its fee and mining share come out of its bond.
  const b = await data('buyer');
  expect(b.escrow_fee_from_bond).toBe(true);
  expect(b.escrow_funding_address).toBeUndefined();
  expect(b.escrow_waiting_for).toBe('peer');
  const s = await data('seller');
  expect(s.escrow_waiting_for).toBe('you');
  checkFundingAddress(s, keys.seller);
  await call('pay', { address: s.escrow_funding_address, sats: s.escrow_funding_recommended });
  await call('advance');

  for (const user of ['seller', 'buyer'] as const) {
    const d = await data(user);
    const sig = signDeposit(d, keys[user], d, {
      maxSpendSats: d.escrow_funding_recommended! + (d.bond_value ?? 0),
      maxFeeRate: 50,
    });
    await call('action', { user, name: 'escrow_funding_signatures', data: sig });
  }
  await call('advance', { mine: 1 });
  expect((await data('buyer')).status).toBe(9);
  return { keys, data, payout };
};

// rl31-beta15 (fee plan 2): the fees ride in the escrow output; the client checks every
// cooperative release to the sat with owedInRelease against the real server's data.
const releaseBoth = async (
  keys: Record<'buyer' | 'seller', ReturnType<typeof escrowKeys>>,
  data: (user: string) => Promise<EscrowOrderData & { status: number }>,
  payout: Record<string, string>,
): Promise<void> => {
  const checkFor = async (user: 'buyer' | 'seller') => {
    const d = await data(user);
    const owed = owedInRelease(d);
    expect(owed).not.toBeNull();
    return { d, check: { myScript: addressToScript(payout[user]), minToMe: 0, owed: owed! } };
  };
  const secrets: Record<string, Uint8Array> = {};
  for (const user of ['seller', 'buyer'] as const) {
    const { d, check } = await checkFor(user);
    const n = releaseNonce(d, keys[user], check);
    secrets[user] = n.secnonce;
    await call('action', { user, name: 'escrow_nonce', data: { nonce: n.pubnonce } });
  }
  for (const user of ['seller', 'buyer'] as const) {
    const { d, check } = await checkFor(user);
    const partial = releasePartial(d, keys[user], check, secrets[user]);
    await call('action', { user, name: 'escrow_partial', data: { partial } });
  }
};

maybe(
  'a whole onchain trade driven by the client code',
  async () => {
    const { keys, data, payout } = await toChat();
    expect((await call('confirm', { user: 'buyer' })).result[0]).toBe(true);
    expect((await call('confirm', { user: 'seller' })).result[0]).toBe(true);
    expect((await data('buyer')).status).toBe(13);

    const r = await data('buyer');
    expect(r.escrow_fee_plan).toBe(2);
    const held = r.escrow_fees_in_escrow!;
    await releaseBoth(keys, data, payout);
    const done = await data('buyer');
    expect(done.status).toBe(14);
    const summary = done.onchain_summary!;
    expect(summary.is_buyer).toBe(true);
    expect(summary.fee_from_bond).toBe(true);
    expect(summary.bond_back_sats).toBe(
      30_000 - summary.coordinator_fee_sats - summary.deposit_mining_sats!,
    );
    expect(summary.received_sats).toBe(
      1_000_000 + summary.bond_back_sats - summary.release_mining_sats,
    );
    const seller = (await data('seller')).onchain_summary!;
    expect(seller.role).toBe('seller');
    expect(seller.bond_back_sats).toBe(30_000);
    expect(seller.received_sats).toBe(30_000);
    // the release paid the coordinator both fees in one output
    const tx = await call('rawtx', { txid: done.escrow_release_txid });
    const coord = tx.vout.filter(
      (o: { scriptPubKey: { address: string } }) =>
        o.scriptPubKey.address !== payout.buyer && o.scriptPubKey.address !== payout.seller,
    );
    expect(coord.length).toBe(1);
    expect(Math.round(coord[0].value * 1e8)).toBe(held.seller + held.buyer);
  },
  180_000,
);

maybe(
  'a collaborative cancel gives each trader its coordinator fee back',
  async () => {
    const { keys, data, payout } = await toChat();
    await call('cancel', { user: 'buyer' });
    await call('cancel', { user: 'seller' });
    const r = await data('seller');
    expect(r.escrow_release_cancel).toBe(true);
    const held = r.escrow_fees_in_escrow!;
    expect(held.seller + held.buyer).toBeGreaterThanOrEqual(1000);
    await releaseBoth(keys, data, payout);
    await call('advance');
    const done = await data('seller');
    expect(done.status).toBe(12);
    const tx = await call('rawtx', { txid: done.escrow_release_txid });
    const to = (a: string): number =>
      Math.round(
        tx.vout
          .filter((o: { scriptPubKey: { address: string } }) => o.scriptPubKey.address === a)
          .reduce((x: number, o: { value: number }) => x + o.value, 0) * 1e8,
      );
    expect(tx.vout.length).toBe(2); // nothing for the coordinator
    const bonds = r.escrow_bond_in_escrow!;
    const mining = r.escrow_output_sats! - to(payout.seller) - to(payout.buyer);
    expect(to(payout.seller)).toBe(1_000_000 + bonds.seller + held.seller - mining);
    expect(to(payout.buyer)).toBe(bonds.buyer + held.buyer);
  },
  180_000,
);
