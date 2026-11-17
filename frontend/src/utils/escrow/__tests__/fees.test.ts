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
