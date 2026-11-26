import { type Order } from '../../models';

/** Kilombino TG 15618: what the maker of a public order knows of the takes paying an onchain
 * bond (counts only, never who). */
export interface PendingOnchainTakes {
  mempool?: number;
  confirmed?: number;
  awaiting_payment?: number;
  txs?: Array<{ txid?: string; stage?: string }>;
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
      key: 'A robot\'s onchain bond to take your order is confirmed, waiting for the taker\'s signature: the order is about to be taken. Get ready.',
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

export interface PendingTakeTx {
  txid: string;
  labelKey: string;
}

const pending = (order: Order): PendingOnchainTakes | null =>
  (order as unknown as { pending_onchain_takes?: PendingOnchainTakes | null }).pending_onchain_takes ??
  null;

/** TG 15622: one line per bond transaction the coordinator sees (each with its own txid, cf.
 * the #29 link bug), confirmed ones first. */
export const pendingTakeTxs = (order: Order): PendingTakeTx[] => {
  const p = pending(order);
  if (!p || order.status !== 1 || !order.is_maker) return [];
  const seen = new Set<string>();
  const out: PendingTakeTx[] = [];
  for (const stage of ['confirmed', 'mempool']) {
    for (const tx of p.txs ?? []) {
      if (tx?.stage !== stage || typeof tx.txid !== 'string' || !/^[0-9a-f]{64}$/i.test(tx.txid))
        continue;
      if (seen.has(tx.txid)) continue;
      seen.add(tx.txid);
      out.push({
        txid: tx.txid,
        labelKey:
          stage === 'confirmed'
            ? "Taker bond (confirmed, waiting for the taker's signature)"
            : 'Taker bond (in the mempool, waiting for 1 confirmation)',
      });
    }
  }
  return out;
};
