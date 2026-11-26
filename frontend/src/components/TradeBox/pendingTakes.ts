import { type Order } from '../../models';

/** Kilombino TG 15618: what the maker of a public order knows of the takes paying an onchain
 * bond (counts only, never who). */
export interface PendingOnchainTakes {
  mempool?: number;
  confirmed?: number;
  awaiting_payment?: number;
}

export interface PendingTakeNotice {
  key: string;
  count: number;
}

/** The notice for the maker's "Your order is public" screen, or null when no onchain take
 * bond is in the mempool or confirmed yet (unpaid takes are not announced: many never pay). */
export const pendingTakeNotice = (order: Order): PendingTakeNotice | null => {
  const p = (order as unknown as { pending_onchain_takes?: PendingOnchainTakes | null })
    .pending_onchain_takes;
  if (!p || order.status !== 1 || !order.is_maker) return null;
  const confirmed = Number(p.confirmed ?? 0);
  const mempool = Number(p.mempool ?? 0);
  if (confirmed > 0)
    return {
      key: 'A robot\'s onchain bond to take your order is confirmed: the order is about to be taken. Get ready.',
      count: confirmed,
    };
  if (mempool === 1)
    return {
      key: 'A robot is taking your order with an onchain bond: seen in the mempool, waiting for 1 confirmation. Get ready.',
      count: 1,
    };
  if (mempool > 1)
    return {
      key: '{{n}} robots are taking your order with an onchain bond: seen in the mempool, waiting for 1 confirmation. The first to confirm takes it. Get ready.',
      count: mempool,
    };
  return null;
};
