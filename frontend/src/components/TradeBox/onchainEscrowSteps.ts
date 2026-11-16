import { type Order } from '../../models';
import { type EscrowOrderData } from '../../utils/escrow/client';

type O = EscrowOrderData & { status: number; is_maker: boolean };

export const isOnchainEscrow = (order: Order): boolean => {
  const e = (order as unknown as O).escrow_type;
  return e === 'onchain' || e === 1;
};

/** True when the trade box should show the onchain escrow step instead of the Lightning one. */
export const onchainEscrowStep = (order: Order): boolean => {
  const o = order as unknown as O;
  if (o.status === 0 && o.is_maker) return true;
  // Taken with Lightning (Roblosats hybrid): the taker bond is a Lightning hold invoice.
  if (o.status === 3 && !o.is_maker) return !(o.take_lightning || (order as Order).bond_invoice);
  if (o.status === 6 || o.status === 7) return true;
  if (o.status === 13) return true;
  if ([17, 18].includes(o.status) && o.escrow_status_code === 4) return true;
  if ([4, 5, 12, 14, 17, 18].includes(o.status) && o.bond_status_code === 5) return true;
  return false;
};

export const onchainEscrowTitle = (order: Order): string => {
  const o = order as unknown as O;
  if (o.status === 0) return 'Lock your onchain bond to PUBLISH the order';
  if (o.status === 3) return 'Lock your onchain bond to TAKE the order';
  if (o.status === 6) return 'Your release address';
  if (o.status === 7) return 'Fund the onchain escrow';
  if (o.status === 13 || o.escrow_status_code === 4) return 'Releasing the onchain escrow';
  return 'Get your bond back';
};

/** Hybrid (a Lightning seller took an onchain order): the order runs over Lightning but the
 * maker's bond is onchain; when it is free the maker takes it back with the onchain prompt. */
export const hybridBondStep = (order: Order): boolean => {
  const o = order as unknown as O;
  return (
    Boolean(o.hybrid_onchain_maker_bond) &&
    o.is_maker &&
    [4, 5, 12, 14, 17, 18].includes(o.status) &&
    o.bond_status_code === 5
  );
};
