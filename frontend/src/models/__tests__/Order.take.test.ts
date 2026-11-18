/**
 * 0.9.3 (18 nov 2026, Kilobot #24): a tester took a 丰 sat onchain order with Lightning,
 * cancelled, and took it again onchain with a new robot. The app kept take_lightning from the
 * first take and showed an empty hold invoice ("Lock 0 Sats", QR of "undefined") instead of the
 * onchain bond, and never sent its bond key.
 */
import Order from '../Order.model';
import { onchainEscrowStep } from '../../components/TradeBox/onchainEscrowSteps';

const r = (o: Order): Record<string, unknown> => o as unknown as Record<string, unknown>;
const page = { id: 24, escrow_type: 'onchain', is_maker: false, sats_denominated: true };

describe('a new take does not inherit an earlier one', () => {
  it('a full page drops take_lightning and the hold invoice the coordinator stopped sending', () => {
    const o = new Order({ id: 24 });
    o.update({
      ...page,
      status: 3,
      take_lightning: true,
      bond_invoice: 'lnbc1',
      bond_satoshis: 30000,
      take_expires_at: 'x',
      bad_request: '',
    });
    expect(onchainEscrowStep(o)).toBe(false);
    o.update({ ...page, status: 1, bad_request: '' }); // after the cancel: the public page
    expect(r(o).take_lightning).toBeUndefined();
    expect(r(o).bond_invoice).toBeUndefined();
    expect(r(o).take_expires_at).toBeUndefined();
    o.update({
      ...page,
      status: 3,
      amount: '1000000',
      exchange_amount: '0.000021',
      bond_role: 'taker',
      bad_request: '',
    });
    expect(onchainEscrowStep(o)).toBe(true);
    expect(o.sats_amount).toBe(1000000);
  });

  it('take() clears what an earlier take left before posting', async () => {
    const o = new Order({ ...page, status: 1, shortAlias: 'kilobot' });
    Object.assign(o, { take_lightning: true, bond_invoice: 'lnbc1', bond_satoshis: 30000 });
    let posted: unknown = null;
    const reply = {
      ...page,
      status: 3,
      amount: '1000000',
      exchange_amount: '0.000021',
      bond_role: 'taker',
      bad_request: '',
    };
    jest.spyOn(o, 'submitAction').mockImplementation(async (_f, _s, action) => {
      posted = action;
      o.update(reply);
      return o;
    });
    await o.take({} as never, {} as never, '', false);
    expect(posted).toMatchObject({ action: 'take' });
    expect((posted as Record<string, unknown>).take_lightning).toBeUndefined();
    expect(r(o).take_lightning).toBeUndefined();
    expect(r(o).bond_invoice).toBeUndefined();
    expect(onchainEscrowStep(o)).toBe(true);
  });

  it('the coordinator saying take_lightning: false is enough for an app that kept it', () => {
    const o = new Order({ id: 24 });
    Object.assign(o, { take_lightning: true });
    o.update({ ...page, status: 3, take_lightning: false, bond_role: 'taker', bad_request: '' });
    expect(onchainEscrowStep(o)).toBe(true);
  });
});

describe('merging one Order into another (Slot.updateSlotFromOrder)', () => {
  it('keeps the sats of a 丰 sat order', () => {
    const fromTake = new Order({ id: 24 });
    fromTake.update({
      ...page,
      status: 3,
      amount: '1000000',
      exchange_amount: '0.000021',
      bad_request: '',
    });
    expect(fromTake.sats_amount).toBe(1000000);
    expect(fromTake.amount).toBe(0.000021);
    const followed = new Order({ id: 24, shortAlias: 'kilobot' });
    followed.update(fromTake);
    expect(followed.sats_amount).toBe(1000000);
    expect(followed.amount).toBe(0.000021);
    followed.update(fromTake);
    expect(followed.sats_amount).toBe(1000000);
  });

  it('a range 丰 sat order keeps its sats range', () => {
    const a = new Order({ id: 30 });
    a.update({
      id: 30,
      status: 1,
      sats_denominated: true,
      has_range: true,
      amount: null,
      min_amount: '50000',
      max_amount: '150000',
      exchange_amount: null,
      exchange_min_amount: '0.0001',
      exchange_max_amount: '0.0003',
    });
    const b = new Order({ id: 30 });
    b.update(a);
    expect(b.sats_min_amount).toBe(50000);
    expect(b.sats_max_amount).toBe(150000);
    expect(b.min_amount).toBe(0.0001);
  });
});

describe('merging an Order drops what the other one no longer has', () => {
  it('drops a stale take_lightning and leftovers', () => {
    const fresh = new Order({ id: 24 });
    fresh.update({
      ...page,
      status: 3,
      amount: '1000000',
      exchange_amount: '0.000021',
      bad_request: '',
    });
    const followed = new Order({ id: 24 });
    Object.assign(followed, { take_lightning: true, bond_invoice: 'lnbc1', bond_leftover_sats: 5 });
    followed.update(fresh);
    const r2 = followed as unknown as Record<string, unknown>;
    expect(r2.take_lightning).toBeUndefined();
    expect(r2.bond_invoice).toBeFalsy();
    expect(r2.bond_leftover_sats).toBeUndefined();
    expect(onchainEscrowStep(followed)).toBe(true);
  });
});
