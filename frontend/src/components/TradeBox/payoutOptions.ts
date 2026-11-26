import { type Order } from '../../models';

/**
 * Roblosats: the buyer's payout options.
 *
 * Upstream RoboSats never offers an onchain payout for currency 1000 (its "BTC" swap
 * pseudo-currency). In Roblosats 1000 is Spamcoin, an ordinary payment currency, so the
 * onchain payout depends only on the coordinator (swap_allowed).
 */
export const onchainPayoutAllowed = (order: Order): boolean => Boolean(order.swap_allowed);

/** An onchain order taken with Lightning (hybrid B): the buyer chose onchain when making it. */
export const isHybridLightningTake = (order: Order): boolean => {
  const o = order as unknown as {
    hybrid_onchain_maker_bond?: boolean;
    taker_lightning?: boolean;
  };
  return Boolean(o.hybrid_onchain_maker_bond || o.taker_lightning);
};

/** Mixta C (26 nov 2026): the buyer took this Lightning order with an onchain bond. */
export const isOnchainBondTake = (order: Order): boolean =>
  Boolean((order as unknown as { hybrid_onchain_taker_bond?: boolean }).hybrid_onchain_taker_bond);

export const defaultPayoutTab = (order: Order): 'lightning' | 'onchain' =>
  onchainPayoutAllowed(order) &&
  ((isHybridLightningTake(order) && order.is_maker) ||
    (isOnchainBondTake(order) && !order.is_maker))
    ? 'onchain'
    : 'lightning';
