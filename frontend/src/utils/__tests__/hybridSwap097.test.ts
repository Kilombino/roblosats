import {
  MIN_ONCHAIN_PAYOUT_SATS,
  MIN_PAYOUT_FEE_RATE,
  maxFeeRateForMinimum,
  onchainPayout,
  sellerMiningCover,
  validPayoutFeeRate,
} from '../hybridSwap';

// Order #25 (hybrid B): payout 99,980 sats, swap fee 3.12 %, the seller paid 4,296 sats.
const tx = { invoiceAmount: 99_980, swapFeeRate: 3.12, paidBySeller: 4_296 };

describe('0.9.7 onchain payout limits', () => {
  it('the highest fee rate still leaves the 20,000-sat minimum', () => {
    const max = maxFeeRateForMinimum(tx);
    const at = onchainPayout({ ...tx, miningFeeSats: max * 280 });
    const above = onchainPayout({ ...tx, miningFeeSats: (max + 1) * 280 });
    expect(at.received).toBeGreaterThanOrEqual(MIN_ONCHAIN_PAYOUT_SATS);
    expect(above.received).toBeLessThan(MIN_ONCHAIN_PAYOUT_SATS);
    expect(max).toBe(289);
  });
  it('500 sat/vB, as in the screenshot, would leave a negative amount', () => {
    expect(onchainPayout({ ...tx, miningFeeSats: 500 * 280 }).received).toBe(-38_844);
  });
  it('what the seller still covers after the swap fee', () => {
    const c = sellerMiningCover(tx);
    expect(c.sats).toBe(1_176);
    expect(c.rate).toBe(4.2);
  });
  it('without a seller payment the buyer pays everything', () => {
    const plain = { invoiceAmount: 99_980, swapFeeRate: 3.12, paidBySeller: 0 };
    expect(maxFeeRateForMinimum(plain)).toBe(Math.floor((99_980 - 20_000 - 3_119.376) / 280));
    expect(sellerMiningCover(plain).sats).toBe(0);
  });
});

// 0.9.10: the swap payout's lowest fee rate is 1 sat/vB (it was a fixed 2), like the coordinator.
describe('payout fee rate limits', () => {
  it('accepts from 1 sat/vB up to the maximum', () => {
    expect(MIN_PAYOUT_FEE_RATE).toBe(1);
    expect(validPayoutFeeRate(1, 30)).toBe(true);
    expect(validPayoutFeeRate(1.5, 30)).toBe(true);
    expect(validPayoutFeeRate(30, 30)).toBe(true);
  });

  it('refuses empty, below 1 or above the maximum', () => {
    expect(validPayoutFeeRate(null, 30)).toBe(false);
    expect(validPayoutFeeRate(0.9, 30)).toBe(false);
    expect(validPayoutFeeRate(0, 30)).toBe(false);
    expect(validPayoutFeeRate(31, 30)).toBe(false);
    expect(validPayoutFeeRate(NaN, 30)).toBe(false);
    expect(validPayoutFeeRate(1, 0)).toBe(false);
  });
});
