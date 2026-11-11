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

maybe(
  'a whole onchain trade driven by the client code',
  async () => {
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

    for (const user of ['seller', 'buyer'] as const) {
      const d = await data(user);
      checkFundingAddress(d, keys[user]);
      await call('pay', { address: d.escrow_funding_address, sats: d.escrow_funding_recommended });
    }
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

    expect((await call('confirm', { user: 'buyer' })).result[0]).toBe(true);
    expect((await call('confirm', { user: 'seller' })).result[0]).toBe(true);
    expect((await data('buyer')).status).toBe(13);

    const checks = {
      buyer: { myScript: addressToScript(payout.buyer), minToMe: 1_000_000 + 30_000 - 5_000 },
      seller: { myScript: addressToScript(payout.seller), minToMe: 30_000 },
    };
    const secrets: Record<string, Uint8Array> = {};
    for (const user of ['seller', 'buyer'] as const) {
      const n = releaseNonce(await data(user), keys[user], checks[user]);
      secrets[user] = n.secnonce;
      await call('action', { user, name: 'escrow_nonce', data: { nonce: n.pubnonce } });
    }
    for (const user of ['seller', 'buyer'] as const) {
      const partial = releasePartial(await data(user), keys[user], checks[user], secrets[user]);
      await call('action', { user, name: 'escrow_partial', data: { partial } });
    }
    expect((await data('buyer')).status).toBe(14);
  },
  180_000,
);
