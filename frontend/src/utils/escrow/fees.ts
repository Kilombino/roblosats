// Onchain coordinator fee as the coordinator charges it since rl31-beta13 (api/escrow/flow.py
// fees()): each side pays its share (maker/taker fee rate, rounded up), and when the total is
// below the coordinator's minimum both shares are raised in the same proportion. A buyer that
// takes with Lightning (hybrid A) pays its fee out of its payout: two separate outputs, each
// one at least the minimum. A seller that takes with Lightning turns the order into a
// Lightning one: plain percentages, no minimum.

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
  if (floor > 0 && seller + buyer > 0 && !lightningSeller) {
    if (takerLightning) {
      // hybrid A: the seller's fee in the deposit, the buyer's in the release
      const s = seller ? Math.max(seller, floor) : 0;
      const b = buyer ? Math.max(buyer, floor) : 0;
      raised = s !== seller || b !== buyer;
      seller = s;
      buyer = b;
    } else if (seller + buyer < floor) {
      const total = seller + buyer;
      const s = Math.ceil((seller * floor) / total);
      buyer = Math.max(0, floor - s);
      seller = s;
      raised = true;
    }
  }
  return {
    sellerSats: seller,
    buyerSats: buyer,
    makerSats: makerIsSeller ? seller : buyer,
    takerSats: makerIsSeller ? buyer : seller,
    raised,
  };
};
