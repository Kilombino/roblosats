import {
  amountUnitLabel,
  amountUnitOptions,
  orderPricePerBtc,
  satsEquivalent,
} from '../satOrders';
import { toApiAmount } from '../spamAmounts';

// 0.9.14-beta5 (Kilombino TG 15627): Spamsats as an amount unit for any order, and the
// "(≈ N 丰 sat)" of a fiat or Spamsats amount at the price the order will use.
describe('maker form amount units', () => {
  const dict = { '1': 'USD', '2': 'EUR', '3': 'JPY', '1000': 'BTC' };
  it('Spamsats comes first (right after 丰 sat), then EUR, USD and the rest', () => {
    expect(amountUnitOptions(dict).map(([k]) => k)).toEqual(['1000', '2', '1', '3']);
  });
  it('currency 1000 reads "Spamsats", never "Spamcoin" or BTC', () => {
    expect(amountUnitLabel('1000', 'BTC')).toBe('Spamsats');
    expect(amountUnitLabel(2, 'EUR')).toBe('EUR');
  });
});

describe('(≈ N 丰 sat)', () => {
  it('market price plus premium, or the fixed price', () => {
    expect(orderPricePerBtc(undefined, 100_000, 0)).toBe(100_000);
    expect(orderPricePerBtc(undefined, 100_000, 10)).toBeCloseTo(110_000);
    expect(orderPricePerBtc(undefined, 100_000, -5)).toBeCloseTo(95_000);
    expect(orderPricePerBtc(90_000, 100_000, 10)).toBe(90_000); // fixed ignores premium
    expect(orderPricePerBtc(null, null, 0)).toBeNull();
    expect(orderPricePerBtc(0, 100_000, 0)).toBeNull();
  });
  it('100 EUR at 100,000 EUR/BTC is 100,000 sats; a 10% premium gives fewer sats', () => {
    expect(satsEquivalent(100, orderPricePerBtc(undefined, 100_000, 0))).toBe(100_000);
    expect(satsEquivalent(100, orderPricePerBtc(undefined, 100_000, 10))).toBe(90_909);
  });
  it('Spamsats: 500 Spamsats at 0.0036034 Spamcoin/BTC (the rounded price of #171) ≈ 138,758 sats', () => {
    const units = toApiAmount(500, 1000) ?? 0; // Spamcoin units
    expect(satsEquivalent(units, orderPricePerBtc(undefined, 0.0036034, 0))).toBe(138_758);
  });
  it('nothing without an amount or a price', () => {
    expect(satsEquivalent(0, 100_000)).toBeNull();
    expect(satsEquivalent(100, null)).toBeNull();
  });
});
