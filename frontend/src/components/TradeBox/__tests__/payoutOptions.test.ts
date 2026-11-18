import { type Order } from '../../../models';
import { defaultPayoutTab, isHybridLightningTake, onchainPayoutAllowed } from '../payoutOptions';

// Kilobot #23 as its maker (the buyer) sees it: a 丰 sat order paid in Spamsats (currency 1000),
// made onchain and taken by a Lightning seller (hybrid B).
const order23 = (over: Record<string, unknown> = {}): Order =>
  ({
    id: 23,
    status: 6,
    currency: 1000,
    sats_denominated: true,
    is_maker: true,
    is_buyer: true,
    swap_allowed: true,
    swap_fee_rate: 3.18,
    suggested_mining_fee_rate: 3,
    hybrid_onchain_maker_bond: true,
    taker_lightning: true,
    ...over,
  }) as unknown as Order;

describe('buyer payout options', () => {
  it('Spamcoin (currency 1000) is not a swap: onchain payout follows swap_allowed', () => {
    expect(onchainPayoutAllowed(order23())).toBe(true);
    expect(onchainPayoutAllowed(order23({ swap_allowed: false }))).toBe(false);
  });

  it('hybrid B: the buyer made it onchain, so the payout defaults to onchain', () => {
    expect(isHybridLightningTake(order23())).toBe(true);
    expect(defaultPayoutTab(order23())).toBe('onchain');
  });

  it('only taker_lightning is enough to spot hybrid B', () => {
    expect(defaultPayoutTab(order23({ hybrid_onchain_maker_bond: undefined }))).toBe('onchain');
  });

  it('a plain Lightning order keeps Lightning first', () => {
    expect(
      defaultPayoutTab(order23({ hybrid_onchain_maker_bond: false, taker_lightning: false })),
    ).toBe('lightning');
  });

  it('a buyer who took with Lightning chose Lightning', () => {
    expect(defaultPayoutTab(order23({ is_maker: false }))).toBe('lightning');
  });

  it('without swaps at the coordinator it stays on Lightning', () => {
    expect(defaultPayoutTab(order23({ swap_allowed: false }))).toBe('lightning');
  });
});
