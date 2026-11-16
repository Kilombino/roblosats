import { type Order } from '../../models';
import { isOnchainEscrow } from './onchainEscrowSteps';

/** Onchain escrow: the same five phases for maker and taker, buyer and seller
 * (Kilombino, 16 nov 2026: one side read 2/4 and the other 3/5 at the same moment). */
const onchainStep = function (order: Order): string {
  const s = order.status;
  let x: number | null = null;
  if ([0, 1, 2, 3].includes(s))
    x = 1; // bond
  else if (s === 6)
    x = 2; // release address
  else if ([7, 8].includes(s))
    x = 3; // deposit
  else if ([9, 10].includes(s))
    x = 4; // trade
  else if (s === 13) x = 5; // release
  return x != null ? `(${x}/5)` : '';
};

const stepXofY = function (order: Order): string {
  if (isOnchainEscrow(order)) return onchainStep(order);
  // set y value
  let x: number | null = null;
  let y: number | null = null;

  if (order.is_maker) {
    y = 5;
  } else if (order.is_taker) {
    y = 4;
  }

  // set x values
  if (order.is_maker) {
    if (order.status === 0) {
      x = 1;
    } else if ([1, 2, 3].includes(order.status)) {
      x = 2;
    } else if ([6, 7, 8].includes(order.status)) {
      x = 3;
    } else if (order.status === 9) {
      x = 4;
    } else if (order.status === 10) {
      x = 5;
    }
  } else if (order.is_taker) {
    if (order.status === 3) {
      x = 1;
    } else if ([6, 7, 8].includes(order.status)) {
      x = 2;
    } else if (order.status === 9) {
      x = 3;
    } else if (order.status === 10) {
      x = 4;
    }
  }

  // Return "(x/y)"
  if (x != null && y != null) {
    return `(${x}/${y})`;
  } else {
    return '';
  }
};

export default stepXofY;
