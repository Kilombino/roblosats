import {
  fixedPriceOf,
  fixedPricePremium,
  fixedPriceSats,
  formatPricePerBtc,
  fromDisplayPrice,
  isFixedPrice,
  orderPremium,
  orderPrice,
  toDisplayPrice,
} from '../fixedPrice';

describe('fixed price (rl31-beta17)', () => {
  it('detects fixed-price orders (strings from the API, numbers from Nostr)', () => {
    expect(isFixedPrice({ fixed_price: '50000.00000000' })).toBe(true);
    expect(isFixedPrice({ fixed_price: 50000 })).toBe(true);
    expect(isFixedPrice({ fixed_price: null })).toBe(false);
    expect(isFixedPrice({ fixed_price: '0' })).toBe(false);
    expect(isFixedPrice({})).toBe(false);
    expect(fixedPriceOf({ fixed_price: 'abc' })).toBeNull();
  });

  it('price and premium of book orders, with and without a market price', () => {
    const fixed = { fixed_price: '55000', premium: '0' };
    const market = { fixed_price: null, premium: '5' };
    expect(orderPrice(fixed, 50000)).toBe(55000);
    expect(orderPrice(fixed, 0)).toBe(55000); // no market price needed
    expect(orderPremium(fixed, 50000)).toBe(10);
    expect(orderPremium(fixed, undefined)).toBeNull();
    expect(orderPrice(market, 50000)).toBeCloseTo(52500);
    expect(orderPrice(market, 0)).toBeNull();
    expect(orderPremium(market, 0)).toBe(5);
    expect(fixedPricePremium(49000, 50000)).toBe(-2);
  });

  it('sats = amount / price', () => {
    expect(fixedPriceSats(100, 50000)).toBe(200000);
    expect(fixedPriceSats(60, 40000)).toBe(150000);
    expect(fixedPriceSats(60, 0)).toBe(0);
  });

  it('Spamcoin (1000) is typed and shown in Spamsats per BTC', () => {
    expect(fromDisplayPrice(500000, 1000)).toBeCloseTo(0.005);
    expect(toDisplayPrice(0.005, 1000)).toBeCloseTo(500000);
    expect(fromDisplayPrice(50000, 2)).toBe(50000);
    expect(formatPricePerBtc(0.005, 1000)).toMatch(/Spamsats\/BTC$/);
    expect(formatPricePerBtc(50000, 2)).toMatch(/EUR\/BTC$/);
  });
});
