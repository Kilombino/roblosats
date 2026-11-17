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
