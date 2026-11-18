/**
 * rl31-beta20 (18 nov 2026, Kilobot #22): "Recover your sats" stayed on screen after the
 * leftover went back, because Order.update (Object.assign) kept bond_leftover_sats from an
 * older page. A full order page now drops the coordinator keys it no longer sends.
 */
import Order from '../Order.model';

describe('Order.update drops stale coordinator keys', () => {
  it('removes onchain keys the next full page no longer has', () => {
    const o = new Order({ id: 22 });
    o.update({
      id: 22,
      status: 9,
      bond_leftover_sats: 3300,
      bond_leftovers: [{}],
      escrow_role: 'buyer',
    });
    expect((o as unknown as Record<string, unknown>).bond_leftover_sats).toBe(3300);
    o.update({ id: 22, status: 9, bond_leftover_txid: 'ab', escrow_role: 'buyer' });
    const r = o as unknown as Record<string, unknown>;
    expect(r.bond_leftover_sats).toBeUndefined();
    expect(r.bond_leftovers).toBeUndefined();
    expect(r.bond_leftover_txid).toBe('ab');
    expect(r.escrow_role).toBe('buyer');
  });

  it('keeps everything on an error reply or a partial update', () => {
    const o = new Order({ id: 22 });
    o.update({ id: 22, status: 9, bond_leftover_sats: 3300 });
    o.update({ bad_request: 'nope' });
    o.update({ id: 22, status: 9, bad_request: 'nope' });
    expect((o as unknown as Record<string, unknown>).bond_leftover_sats).toBe(3300);
  });

  it('never drops what the app set itself', () => {
    const o = new Order({ id: 22 });
    (o as unknown as Record<string, unknown>).escrow_local_note = 'x';
    o.update({ id: 22, status: 9 });
    o.update({ id: 22, status: 10 });
    expect((o as unknown as Record<string, unknown>).escrow_local_note).toBe('x');
  });
});
