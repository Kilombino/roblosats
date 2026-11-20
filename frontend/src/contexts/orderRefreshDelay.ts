import { onchainStillPending } from '../models/Slot.model';
import type Order from '../models/Order.model';

export const defaultDelay = 5000;

// Refresh delays (ms) according to Order status
const statusToDelay = [
  3000, // 'Waiting for maker bond'
  35000, // 'Public'
  180000, // 'Paused'
  3000, // 'Waiting for taker bond'
  999999, // 'Cancelled'
  999999, // 'Expired'
  8000, // 'Waiting for trade collateral and buyer invoice'
  8000, // 'Waiting only for seller trade collateral'
  8000, // 'Waiting only for buyer invoice'
  10000, // 'Sending fiat - In chatroom'
  10000, // 'Fiat sent - In chatroom'
  100000, // 'In dispute'
  999999, // 'Collaboratively cancelled'
  10000, // 'Sending satoshis to buyer'
  60000, // 'Sucessful trade'
  30000, // 'Failed lightning network routing'
  300000, // 'Wait for dispute resolution'
  300000, // 'Maker lost dispute'
  300000, // 'Taker lost dispute'
];

/** How long to wait before refreshing the active order. #26 (0.9.10): a finished onchain order
 * whose release is still on its way (or that waits for our signature) keeps the normal pace. */
export const activeOrderDelay = (order: Order): number => {
  if (onchainStillPending(order)) return 10000;
  return order.status >= 0 && order.status <= 18 ? statusToDelay[order.status] : defaultDelay;
};
