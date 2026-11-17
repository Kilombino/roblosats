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

  it('hybrid A: two outputs, each at least the minimum', () => {
    const f = onchainFees({ ...base, tradeSats: 80_000, makerIsSeller: true, takerLightning: true });
    expect([f.sellerSats, f.buyerSats]).toEqual([1000, 1000]);
  });

  it('hybrid B (Lightning seller): plain percentages', () => {
    const f = onchainFees({ ...base, tradeSats: 80_000, makerIsSeller: false, takerLightning: true });
    expect(f.takerSats).toBe(137);
    expect(f.raised).toBe(false);
  });

  it('no minimum', () => {
    const f = onchainFees({ ...base, minFeeSats: 0, tradeSats: 80_000, makerIsSeller: true });
    expect([f.sellerSats, f.buyerSats]).toEqual([16, 137]);
  });
});
