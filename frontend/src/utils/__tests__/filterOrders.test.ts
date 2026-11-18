import filterOrders from '../filterOrders';
import { type Federation, type PublicOrder, type Favorites } from '../../models';

// 17 nov 2026: the onchain orders a robot gets apart (not in federation.book) were dropped
// by the filtered book.
const order = (id: number, type: number): PublicOrder =>
  ({
    id,
    type,
    currency: 1,
    premium: '0',
    payment_method: 'Spamcoin Lightning',
    amount: '0.2',
    min_amount: null,
    max_amount: null,
    coordinatorShortAlias: 'kilobot',
  }) as unknown as PublicOrder;

const federation = {
  book: { kilobot1: order(1, 1) },
  getCoordinators: () => [{ shortAlias: 'kilobot', enabled: true }],
  getCoordinator: () => ({ federated: true }),
} as unknown as Federation;

const fav = { type: null, currency: 0, mode: 'swap', coordinator: 'any' } as unknown as Favorites;

describe('filterOrders', () => {
  it('filters the federation book by default', () => {
    expect(filterOrders({ federation, baseFilter: fav }).map((o) => o.id)).toEqual([1]);
  });
  it('filters a given list (book + onchain orders) when passed', () => {
    const orders = [order(1, 1), order(21, 0)];
    expect(filterOrders({ federation, baseFilter: fav, orders }).map((o) => o.id)).toEqual([1, 21]);
    const buys = filterOrders({ federation, baseFilter: { ...fav, type: 0 }, orders });
    expect(buys.map((o) => o.id)).toEqual([21]);
  });
});

// 18 nov 2026: Kilobot #23 (a "丰 sat" order paid in Spamsats, currency 1000 = Spamcoin) was
// hidden in the default book (mode 'fiat', currency ANY): upstream drops currency 1000 there.
describe('filterOrders: Spamcoin orders (currency 1000)', () => {
  const spam = (id: number, extra: Partial<PublicOrder> = {}): PublicOrder =>
    ({ ...order(id, 0), currency: 1000, amount: '0.0005', ...extra }) as unknown as PublicOrder;
  const orders = [
    order(1, 1),
    spam(23, { sats_denominated: true } as Partial<PublicOrder>),
    spam(24),
  ];
  const def = { type: null, currency: 0, mode: 'fiat', coordinator: 'any' } as unknown as Favorites;

  it('shows Spamcoin and 丰 sat orders in the default book (fiat mode, ANY currency)', () => {
    expect(filterOrders({ federation, baseFilter: def, orders }).map((o) => o.id)).toEqual([
      1, 23, 24,
    ]);
  });
  it('still filters by Spamcoin when it is picked as the currency', () => {
    const only = filterOrders({ federation, baseFilter: { ...def, currency: 1000 }, orders });
    expect(only.map((o) => o.id)).toEqual([23, 24]);
    const usd = filterOrders({ federation, baseFilter: { ...def, currency: 1 }, orders });
    expect(usd.map((o) => o.id)).toEqual([1]);
  });
  it('shows them with a type filter too', () => {
    const sells = filterOrders({ federation, baseFilter: { ...def, type: 0 }, orders });
    expect(sells.map((o) => o.id)).toEqual([23, 24]);
  });
});
