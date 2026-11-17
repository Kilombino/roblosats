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
