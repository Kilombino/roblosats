import {
  SAT_LABEL,
  applySatsTag,
  bookSats,
  exchangeUnit,
  formatTotal,
  fromDisplayTotal,
  makerCurrencyOptions,
  priceFromTotal,
  totalForSats,
} from '../satOrders';
import Order from '../../models/Order.model';

// rl31-beta19: "丰 sat" orders (same cases as api/tests/test_roblosats_sat_orders.py)
describe('sat orders', () => {
  it('lists 丰 sat first in the form, then EUR, USD and the rest in their order', () => {
    const keys = makerCurrencyOptions({ '1': 'USD', '2': 'EUR', '3': 'JPY', '1000': 'BTC' }).map(
      ([k]) => k,
    );
    expect(keys).toEqual(['2', '1', '1000', '3']);
    expect(SAT_LABEL).toBe('丰 sat');
  });

  it('prices from the total in exchange, Spamsats for Spamcoin', () => {
    expect(exchangeUnit(1000)).toBe('Spamsats');
    expect(exchangeUnit(2)).toBe('EUR');
    // 25,000 sat for 50,000 Spamsats = 2 Spamcoin per BTC
    const price = priceFromTotal(fromDisplayTotal(50_000, 1000), 25_000);
    expect(price).toBe(2);
    expect(formatTotal(totalForSats(25_000, price ?? 0), 1000)).toMatch(/^50.000 Spamsats$/);
    // a range keeps the price per sat: 45 EUR for 90,000 → 15 EUR for 30,000
    const eur = priceFromTotal(45, 90_000) ?? 0;
    expect(totalForSats(30_000, eur)).toBeCloseTo(15, 8);
    expect(priceFromTotal(0, 1)).toBeNull();
  });

  it('reads the Nostr sats tag and the book shape', () => {
    const o = applySatsTag({ has_range: true } as Record<string, unknown>, [
      'sats',
      '30000',
      '90000',
    ]);
    expect(o.sats_denominated).toBe(true);
    expect(bookSats(o)).toEqual([30000, 90000]);
    expect(bookSats({ sats_denominated: true, has_range: false, sats_amount: '25000' })).toEqual([
      25000,
    ]);
    expect(bookSats({ has_range: false, sats_amount: '25000' })).toBeNull();
  });

  it('keeps the sats of an order page and shows the currency in amount', () => {
    const order = new Order({});
    order.update({
      sats_denominated: true,
      has_range: true,
      amount: null,
      min_amount: '30000.00000000',
      max_amount: '90000.00000000',
      exchange_amount: null,
      exchange_min_amount: '15',
      exchange_max_amount: '45',
    });
    expect([order.sats_min_amount, order.sats_max_amount]).toEqual([30000, 90000]);
    expect([order.min_amount, order.max_amount]).toEqual([15, 45]);
    // a normal order is untouched
    const plain = new Order({});
    plain.update({ amount: '100', min_amount: null });
    expect(plain.amount).toBe('100');
  });
});
