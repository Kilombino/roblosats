/**
 * 0.9.8: an onchain taker waiting for its confirmation lost the race (another taker's bond
 * locked first). The coordinator now answers its order page with only its bond ("lost_take")
 * instead of a 403: the app follows it until the bond is settled, then lets the order go.
 */
import Order from '../Order.model';
import Slot from '../Slot.model';
import { actionNeeded } from '../../utils/actionNeeded';

jest.mock('../../services/System', () => ({
  systemClient: { getItem: jest.fn(), setItem: jest.fn(), deleteItem: jest.fn() },
}));

const lost = {
  id: 7,
  status: 6,
  escrow_type: 'onchain',
  is_maker: false,
  is_taker: false,
  is_participant: false,
  bad_request: '',
  lost_take: true,
  lost_take_taken: true,
  bond_role: 'taker',
  bond_address: 'bcrt1pbond',
};

const slotFollowing = (): Slot => {
  const slot = new Slot('token', ['test'], {}, jest.fn());
  slot.activeOrder = new Order({ id: 7, shortAlias: 'test', status: 3, is_taker: true });
  return slot;
};

describe('a lost take', () => {
  it('stays the active order while its bond is not settled', () => {
    const slot = slotFollowing();
    const page = new Order({ shortAlias: 'test' });
    page.update({ ...lost, lost_take_done: false, bond_status_code: 7, bond_mempool_txid: 'ab' });
    slot.updateSlotFromOrder(page);
    expect(slot.activeOrder?.lost_take).toBe(true);
    expect(slot.activeOrder?.is_taker).toBe(false);
  });

  it('is let go once the bond is settled', () => {
    const slot = slotFollowing();
    const page = new Order({ shortAlias: 'test' });
    page.update({ ...lost, lost_take_done: true, bond_status_code: 6 });
    slot.updateSlotFromOrder(page);
    expect(slot.activeOrder).toBeNull();
    expect(slot.lastOrder?.id).toBe(7);
  });

  it('a later normal page drops the lost_take flags', () => {
    const o = new Order({ id: 7 });
    o.update({ ...lost, lost_take_done: false, bond_status_code: 5 });
    o.update({ id: 7, status: 3, is_taker: true, is_participant: true, bad_request: '' });
    expect(o.lost_take).toBeUndefined();
    expect(o.lost_take_taken).toBeUndefined();
  });

  it('asks for action only when the bond can be taken back', () => {
    expect(actionNeeded({ ...lost, bond_status_code: 5 })).toBe('Get your sats back');
    expect(actionNeeded({ ...lost, bond_status_code: 7 })).toBeNull();
    // never "Lock your bond to take the order" on a public order it lost
    expect(actionNeeded({ ...lost, status: 1, bond_status_code: 7 })).toBeNull();
  });
});
