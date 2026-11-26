import { type Order } from '../../models';

/** 0.9.14 (Kilombino TG 15656, #34): once the buyer has a payout, what it receives exactly:
 * its Lightning invoice (the trade minus the routing budget it chose) or its onchain payout.
 * Null before a payout exists (the order details keep the approximate amount). */
export const exactReceive = (
  order: Order,
): { sats: number; routingBudget: number; onchain: boolean } | null => {
  const o = order as unknown as {
    payout_sats?: number;
    payout_routing_budget_sats?: number;
    payout_layer?: string;
    is_buyer?: boolean;
  };
  const sats = Number(o.payout_sats ?? 0);
  if (!o.is_buyer || !(sats > 0)) return null;
  return {
    sats,
    routingBudget: Math.max(0, Number(o.payout_routing_budget_sats ?? 0)),
    onchain: o.payout_layer === 'onchain',
  };
};
