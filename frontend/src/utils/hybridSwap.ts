/**
 * Roblosats rl31-beta21, hybrid B: a seller who takes an onchain BUY order with Lightning pays
 * the buyer's swap to onchain. The coordinator sends `lightning_take_swap` on the take screen
 * (swap % with its margin and the mining sats for the payout) and, once the escrow is made,
 * `swap_surcharge_sats` (seller) / `swap_paid_by_seller_sats` (buyer's payout).
 */
export interface LightningTakeSwap {
  swap_fee_percent: number;
  mining_fee_sats: number;
  swap_fee_rate?: number;
  mining_fee_rate?: number;
  min_payout_sats?: number;
  max_payout_sats?: number;
}

/** The estimated swap the seller would pay for a trade of `tradeSats`, or null if no swap. */
export const lightningTakeSwapSats = (
  quote: LightningTakeSwap | null | undefined,
  tradeSats: number,
): number | null => {
  if (!quote || !(tradeSats > 0)) return null;
  if (quote.min_payout_sats != null && tradeSats < quote.min_payout_sats) return null;
  if (quote.max_payout_sats != null && tradeSats > quote.max_payout_sats) return null;
  // (minus a hair so float noise such as 2200.0000000000005 does not add a sat)
  return Math.ceil(
    (tradeSats * Number(quote.swap_fee_percent)) / 100 + Number(quote.mining_fee_sats) - 1e-9,
  );
};

export interface SwapTx {
  invoiceAmount: number;
  swapFeeRate: number;
  miningFeeSats: number;
  paidBySeller: number;
}

/** Onchain payout as the coordinator computes it: the swap fee and mining come out of the
 * payout except what the seller already paid (hybrid B). */
export const onchainPayout = ({
  invoiceAmount,
  swapFeeRate,
  miningFeeSats,
  paidBySeller,
}: SwapTx): { swapFee: number; cost: number; covered: number; received: number } => {
  const swapFee = (invoiceAmount * swapFeeRate) / 100;
  const cost =
    paidBySeller > 0 ? Math.ceil(swapFee + miningFeeSats - 1e-9) : swapFee + miningFeeSats;
  const covered = Math.min(Math.max(0, paidBySeller), cost);
  return { swapFee, cost, covered, received: Math.floor(invoiceAmount - (cost - covered)) };
};

/** 0.9.7: the coordinator refuses an onchain payout that leaves less than this (error 4003). */
export const MIN_ONCHAIN_PAYOUT_SATS = 20_000;
/** 0.9.10: the lowest mining fee rate (sat/vB) for an onchain swap payout; the coordinators
 * enforce PAYOUT_MIN_FEE_RATE, default 1 (it was a fixed 2). */
export const MIN_PAYOUT_FEE_RATE = 1;
/** vbytes the coordinator counts for the payout transaction (tx.mining_fee_sats = rate × 280). */
export const PAYOUT_VBYTES = 280;

/** 0.9.7: the highest mining fee rate (sat/vB, whole) that still leaves the minimum payout. */
export const maxFeeRateForMinimum = ({
  invoiceAmount,
  swapFeeRate,
  paidBySeller,
  minReceived = MIN_ONCHAIN_PAYOUT_SATS,
}: Omit<SwapTx, 'miningFeeSats'> & { minReceived?: number }): number => {
  const swapFee = (invoiceAmount * swapFeeRate) / 100;
  const room = invoiceAmount - minReceived + Math.max(0, paidBySeller) - swapFee;
  return Math.max(0, Math.floor(room / PAYOUT_VBYTES));
};

/** 0.9.7 (hybrid B): the mining the seller's payment still covers after the swap fee, in sats
 * and as a fee rate. */
export const sellerMiningCover = ({
  invoiceAmount,
  swapFeeRate,
  paidBySeller,
}: Omit<SwapTx, 'miningFeeSats'>): { sats: number; rate: number } => {
  const swapFee = (invoiceAmount * swapFeeRate) / 100;
  const sats = Math.max(0, Math.floor(paidBySeller - swapFee));
  return { sats, rate: Math.floor((sats / PAYOUT_VBYTES) * 100) / 100 };
};

/** 0.9.10: a payout fee rate the form may send: given, at least MIN_PAYOUT_FEE_RATE and at most
 * `max` (the highest rate that still leaves the minimum payout, capped at 500). */
export const validPayoutFeeRate = (rate: number | null | undefined, max: number): boolean =>
  rate != null && Number.isFinite(rate) && rate >= MIN_PAYOUT_FEE_RATE && rate <= max;
