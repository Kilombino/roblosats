// Onchain coordinator fee as the coordinator charges it since rl31-beta15 (api/escrow/flow.py
// fees(), fee plan 2): each side pays its share (maker/taker fee rate, rounded up), and when the
// total is below the coordinator's minimum both shares are raised in the same proportion. The
// minimum is for the whole order, also when the buyer takes with Lightning (hybrid A: one
// coordinator output in the release). A seller that takes with Lightning turns the order into
// a Lightning one: plain percentages, no minimum. The fee is only paid when the trade
// completes: a collaborative cancel returns it.

export interface OnchainFeeInput {
  tradeSats: number;
  makerFeeRate: number; // e.g. 0.00025
  takerFeeRate: number;
  makerIsSeller: boolean;
  minFeeSats: number;
  takerLightning?: boolean;
}

export interface OnchainFees {
  makerSats: number;
  takerSats: number;
  sellerSats: number;
  buyerSats: number;
  raised: boolean; // the minimum applied
}

export const onchainFees = ({
  tradeSats,
  makerFeeRate,
  takerFeeRate,
  makerIsSeller,
  minFeeSats,
  takerLightning = false,
}: OnchainFeeInput): OnchainFees => {
  const makerFee = Math.ceil(tradeSats * makerFeeRate);
  const takerFee = Math.ceil(tradeSats * takerFeeRate);
  let seller = makerIsSeller ? makerFee : takerFee;
  let buyer = makerIsSeller ? takerFee : makerFee;
  let raised = false;
  const floor = Math.max(0, Math.floor(minFeeSats || 0));
  const lightningSeller = takerLightning && makerIsSeller === false;
  if (floor > 0 && seller + buyer > 0 && !lightningSeller && seller + buyer < floor) {
    const total = seller + buyer;
    const s = Math.ceil((seller * floor) / total);
    buyer = Math.max(0, floor - s);
    seller = s;
    raised = true;
  }
  // (only with the minimum off) one fee output below dust cannot exist: not charged
  if (!lightningSeller && seller + buyer > 0 && seller + buyer < 330) {
    seller = 0;
    buyer = 0;
  }
  return {
    sellerSats: seller,
    buyerSats: buyer,
    makerSats: makerIsSeller ? seller : buyer,
    takerSats: makerIsSeller ? buyer : seller,
    raised,
  };
};

// rl31-beta19: the fee rates a payment from the trader's own wallet should pay (from the
// coordinator, onchain_fee_hint), and whether a payment seen in the mempool pays too little.
export interface FeeHint {
  next_block: number; // sat/vB to confirm in the next block
  hour: number; // sat/vB to confirm in about an hour
}

/** A usable hint, or null (missing or nonsense: the app hides it). */
export const validHint = (hint?: Partial<FeeHint> | null): FeeHint | null => {
  if (!hint) return null;
  const { next_block: next, hour } = hint;
  if (typeof next !== 'number' || typeof hour !== 'number') return null;
  if (!(next > 0) || !(hour > 0) || !isFinite(next) || !isFinite(hour)) return null;
  return { next_block: next, hour: Math.min(hour, next) };
};

/** 2 → "2", 0.5 → "0.5" (or "0,5" with a comma locale), at most one decimal. */
export const formatRate = (rate: number, decimal = '.'): string => {
  const r = Math.round(rate * 10) / 10;
  return (Number.isInteger(r) ? String(r) : r.toFixed(1)).replace('.', decimal);
};

/** The rate needed for about an hour when the payment pays less, else null. */
export const lowFeeNeeded = (
  rate?: number | null,
  hint?: Partial<FeeHint> | null,
): number | null => {
  const h = validHint(hint);
  if (h === null || typeof rate !== 'number' || !isFinite(rate)) return null;
  return rate < h.hour ? h.hour : null;
};
