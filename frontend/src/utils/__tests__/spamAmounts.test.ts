import {
  amountScale,
  formAmountUnit,
  isSpamAmountCurrency,
  summaryAmountText,
  toApiAmount,
  toFormAmount,
} from '../spamAmounts';

describe('Spamcoin amounts in the maker form (0.9.1)', () => {
  it('only currency 1000 is typed in Spamsats', () => {
    expect(isSpamAmountCurrency(1000)).toBe(true);
    expect(isSpamAmountCurrency(1)).toBe(false);
    expect(isSpamAmountCurrency(2)).toBe(false);
    expect(isSpamAmountCurrency(null)).toBe(false);
    expect(amountScale(1000)).toBe(100_000_000);
    expect(amountScale(2)).toBe(1);
  });

  it('Spamsats go to the API as Spamcoin with 8 decimals (same shape as older orders)', () => {
    expect(toApiAmount(28_000, 1000)).toBe(0.00028);
    expect(toApiAmount(10_000, 1000)).toBe(0.0001);
    expect(toApiAmount(1, 1000)).toBe(0.00000001);
    expect(toApiAmount(123_456_789, 1000)).toBe(1.23456789);
    // whole Spamsats only
    expect(toApiAmount(28_000.6, 1000)).toBe(0.00028001);
    expect(toApiAmount(null, 1000)).toBeNull();
  });

  it('other currencies are untouched', () => {
    expect(toApiAmount(12.5, 2)).toBe(12.5);
    expect(toFormAmount(12.5, 2)).toBe(12.5);
    expect(toFormAmount('0.35', 1)).toBe(0.35);
  });

  it('API Spamcoin amounts come back as whole Spamsats (editing, renewing)', () => {
    expect(toFormAmount('0.00028000', 1000)).toBe(28_000);
    expect(toFormAmount(0.0001, 1000)).toBe(10_000);
    expect(toFormAmount(0.00338624, 1000)).toBe(338_624);
    expect(toFormAmount(null, 1000)).toBeNull();
    expect(toFormAmount('', 1000)).toBeNull();
    for (const s of [1, 999, 50_000, 101_000, 6_000_000]) {
      expect(toFormAmount(toApiAmount(s, 1000), 1000)).toBe(s);
    }
  });

  it('labels', () => {
    expect(formAmountUnit(1000, 'BTC')).toBe('Spamsats');
    expect(formAmountUnit(2, 'EUR')).toBe('EUR');
  });
});

describe('final summary amount (0.9.1)', () => {
  const fmt = (n: number): string => n.toLocaleString('en-US');
  it('plain Spamcoin orders read in Spamsats', () => {
    expect(
      summaryAmountText(
        { currency: 1000, amount: '0.00028000', contract: true, trade_sats: 82_000 },
        'BTC',
        false,
        fmt,
      ),
    ).toBe('28,000 Spamsats');
    expect(summaryAmountText({ currency: 1000, amount: '0.00010000' }, 'BTC', undefined, fmt)).toBe(
      '10,000 Spamsats',
    );
  });
  it('丰 sat orders read in sats (from the order page or guessed in past orders)', () => {
    expect(summaryAmountText({ currency: 1000, amount: '50000.00000000' }, 'BTC', true, fmt)).toBe(
      '50,000 丰 sat',
    );
    expect(
      summaryAmountText({ currency: 1000, amount: '50000.00000000' }, 'BTC', undefined, fmt),
    ).toBe('50,000 丰 sat');
    expect(
      summaryAmountText(
        { currency: 2, amount: '25000', contract: true, trade_sats: 25_000 },
        'EUR',
        undefined,
        fmt,
      ),
    ).toBe('25,000 丰 sat');
  });
  it('fiat orders as before', () => {
    expect(
      summaryAmountText(
        { currency: 1, amount: '0.35', contract: true, trade_sats: 160_000 },
        'USD',
        false,
        fmt,
      ),
    ).toBe('0.35 USD');
    expect(summaryAmountText({ currency: 2, amount: '200' }, 'EUR', undefined, fmt)).toBe(
      '200 EUR',
    );
  });
});
