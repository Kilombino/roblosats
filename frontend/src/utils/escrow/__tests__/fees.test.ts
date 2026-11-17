import { formatRate, lowFeeNeeded, validHint } from '../fees';
import { onchainFees } from '../fees';

// Same cases as api/tests/test_escrow_fees.py (rl31-beta13 fee floor).
describe('onchainFees', () => {
  const base = { makerFeeRate: 0.0001995, takerFeeRate: 0.0017005, minFeeSats: 1000 };

  it('raises small fees to the minimum in proportion', () => {
    const f = onchainFees({ ...base, tradeSats: 80_000, makerIsSeller: true });
    expect(f.sellerSats + f.buyerSats).toBe(1000);
    expect(f.buyerSats).toBeGreaterThan(f.sellerSats);
    expect(f.makerSats).toBe(f.sellerSats);
    expect(f.raised).toBe(true);
  });

  it('leaves big fees untouched', () => {
    const f = onchainFees({ ...base, tradeSats: 10_000_000, makerIsSeller: false });
    expect(f.makerSats).toBe(1995);
    expect(f.takerSats).toBe(17005);
    expect(f.raised).toBe(false);
  });

  it('hybrid A: one minimum for the whole order (rl31-beta15)', () => {
    const f = onchainFees({
      ...base,
      tradeSats: 80_000,
      makerIsSeller: true,
      takerLightning: true,
    });
    expect(f.sellerSats + f.buyerSats).toBe(1000);
    expect(f).toEqual(onchainFees({ ...base, tradeSats: 80_000, makerIsSeller: true }));
  });

  it('hybrid B (Lightning seller): plain percentages', () => {
    const f = onchainFees({
      ...base,
      tradeSats: 80_000,
      makerIsSeller: false,
      takerLightning: true,
    });
    expect(f.takerSats).toBe(137);
    expect(f.raised).toBe(false);
  });

  it('no minimum', () => {
    const f = onchainFees({ ...base, minFeeSats: 0, tradeSats: 80_000, makerIsSeller: true });
    expect([f.sellerSats, f.buyerSats]).toEqual([0, 0]); // below dust: not charged (beta15)
    const g = onchainFees({ ...base, minFeeSats: 0, tradeSats: 1_000_000, makerIsSeller: true });
    expect([g.sellerSats, g.buyerSats]).toEqual([200, 1701]);
  });
});

// rl31-beta19: fee hint on the payment screens and the low-fee warning

describe('fee hint', () => {
  it('takes a sane hint and hides a missing or broken one', () => {
    expect(validHint({ next_block: 3, hour: 1.2 })).toEqual({ next_block: 3, hour: 1.2 });
    expect(validHint({ next_block: 2, hour: 5 })).toEqual({ next_block: 2, hour: 2 });
    expect(validHint(undefined)).toBeNull();
    expect(validHint(null)).toBeNull();
    expect(validHint({ next_block: 0, hour: 1 })).toBeNull();
    expect(validHint({ next_block: 2 })).toBeNull();
    expect(validHint({ next_block: NaN, hour: 1 })).toBeNull();
  });

  it('formats rates with at most one decimal', () => {
    expect(formatRate(2)).toBe('2');
    expect(formatRate(0.5)).toBe('0.5');
    expect(formatRate(1.25)).toBe('1.3');
    expect(formatRate(2.5, ',')).toBe('2,5');
  });

  it('warns only when the payment pays less than the rate for about an hour', () => {
    const hint = { next_block: 5, hour: 2.5 };
    expect(lowFeeNeeded(1, hint)).toBe(2.5);
    expect(lowFeeNeeded(2.5, hint)).toBeNull();
    expect(lowFeeNeeded(4, hint)).toBeNull();
    expect(lowFeeNeeded(undefined, hint)).toBeNull();
    expect(lowFeeNeeded(1, undefined)).toBeNull();
  });
});
