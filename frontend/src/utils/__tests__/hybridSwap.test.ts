import { lightningTakeSwapSats, onchainPayout } from '../hybridSwap';

const quote = {
  swap_fee_percent: 2.2,
  mining_fee_sats: 1260,
  min_payout_sats: 20_000,
  max_payout_sats: 6_000_000,
};

describe('hybrid B: the seller pays the swap', () => {
  it('estimates the swap for the take screen', () => {
    expect(lightningTakeSwapSats(quote, 100_000)).toBe(2200 + 1260);
  });
  it('no swap out of the coordinator range or without a quote', () => {
    expect(lightningTakeSwapSats(quote, 10_000)).toBeNull();
    expect(lightningTakeSwapSats(quote, 7_000_000)).toBeNull();
    expect(lightningTakeSwapSats(undefined, 100_000)).toBeNull();
  });
  it('the buyer receives everything when the seller covers the swap', () => {
    const r = onchainPayout({
      invoiceAmount: 49_990,
      swapFeeRate: 3.18,
      miningFeeSats: 840,
      paidBySeller: 3_500,
    });
    expect(r.received).toBe(49_990);
    expect(r.covered).toBe(r.cost);
  });
  it('a higher mining fee than priced comes out of the buyer', () => {
    const r = onchainPayout({
      invoiceAmount: 100_000,
      swapFeeRate: 2,
      miningFeeSats: 5_600,
      paidBySeller: 3_460,
    });
    expect(r.received).toBe(100_000 - (2_000 + 5_600 - 3_460));
  });
  it('upstream rule without a surcharge (buyer pays)', () => {
    const r = onchainPayout({
      invoiceAmount: 49_990,
      swapFeeRate: 3.18,
      miningFeeSats: 840,
      paidBySeller: 0,
    });
    expect(r.received).toBe(Math.floor(49_990 - 49_990 * 0.0318 - 840));
  });
});
