import { bookExchangeText } from '../satOrders';
import { paymentMethodNames } from '../../components/PaymentMethods/MethodList';

// Roblosats 0.9.2: the book shows what a "丰 sat" order is paid with next to the method
describe('book: what a 丰 sat order is paid with', () => {
  // the live Kilobot order: 1,000,000 丰 sat for 2,100 Spamsats at a fixed price
  const fixed = {
    sats_denominated: true,
    sats_amount: 1000000,
    has_range: false,
    amount: '0.000021',
    currency: 1000,
    fixed_price: '0.00210000',
    premium: '-38.81',
  };

  it('a fixed price: the fixed total, no ≈, whatever the market does', () => {
    expect(bookExchangeText(fixed, 0.0034)).toMatch(/^2[.,]100 Spamsats$/);
    expect(bookExchangeText(fixed, null)).toMatch(/^2[.,]100 Spamsats$/);
    expect(bookExchangeText({ ...fixed, currency: 2, amount: '15', fixed_price: '1500' })).toBe(
      '15 EUR',
    );
  });

  it('market price + premium: today’s estimate with ≈', () => {
    const market = { ...fixed, fixed_price: null, premium: '5', amount: '0.00002' };
    // 1,000,000 sat at 0.002 × 1.05 Spamcoin/BTC = 2,100 Spamsats
    expect(bookExchangeText(market, 0.002)).toMatch(/^≈ 2[.,]100 Spamsats$/);
    // no market price yet: the book's own figure, still an estimate
    expect(bookExchangeText(market, null)).toMatch(/^≈ 2[.,]000 Spamsats$/);
  });

  it('a range, and orders that are not 丰 sat', () => {
    const range = {
      sats_denominated: true,
      has_range: true,
      sats_min_amount: 30000,
      sats_max_amount: 90000,
      min_amount: '15',
      max_amount: '45',
      currency: 2,
      fixed_price: '50000',
    };
    expect(bookExchangeText(range)).toBe('15-45 EUR');
    expect(bookExchangeText({ ...fixed, sats_denominated: false })).toBeNull();
    expect(bookExchangeText({ ...fixed, amount: '0', fixed_price: null }, null)).toBeNull();
  });
});

describe('payment method names', () => {
  it('lists the methods once each, then the custom text', () => {
    expect(paymentMethodNames('Spamcoin Lightning Spamcoin On-Chain')).toEqual([
      'Spamcoin On-Chain',
      'Spamcoin Lightning',
    ]);
    expect(paymentMethodNames('Bizum my own bank')).toEqual(['Bizum', 'my own bank']);
    expect(paymentMethodNames('')).toEqual([]);
  });
});
