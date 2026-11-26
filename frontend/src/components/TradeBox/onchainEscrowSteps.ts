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
  // A cooperative return being signed (collaborative cancel in the chat).
  if ([9, 10, 11, 12].includes(o.status) && o.escrow_status_code === 4) return true;
  if ([14, 17, 18].includes(o.status) && o.escrow_status_code === 4) return true;
  if ([4, 5, 12, 14, 17, 18].includes(o.status) && o.bond_status_code === 5) return true;
  // Sats left on our funding address after the order ended: offer to send them back.
  if (
    [4, 5, 12, 14, 15, 17, 18].includes(o.status) &&
    ((o.escrow_leftover_sats ?? 0) > 0 || o.escrow_sweep_txid)
  )
    return true;
  return false;
};

export const onchainEscrowTitle = (order: Order): string => {
  const o = order as unknown as O;
  if (o.status === 0) return 'Lock your onchain bond to PUBLISH the order';
  if (o.status === 3) return 'Lock your onchain bond to TAKE the order';
  if (o.status === 6)
    return (order as Order).is_buyer ? 'Your release address' : 'Your refund address';
  if (o.status === 7) return 'Fund the onchain escrow';
  if (o.status === 13 || o.escrow_status_code === 4) return 'Releasing the onchain escrow';
  if ((o.escrow_leftover_sats ?? 0) > 0 || o.escrow_sweep_txid) return 'Get your sats back';
  return 'Get your bond back';
};

/** Hybrid B (a Lightning seller took an onchain order) / mixta C (a buyer took a Lightning
 * order with an onchain bond): the order runs over Lightning but one bond is onchain; when it is
 * free its owner takes it back with the onchain prompt. */
export const hybridBondStep = (order: Order): boolean => {
  const o = order as unknown as O & { hybrid_onchain_taker_bond?: boolean };
  const mine =
    (Boolean(o.hybrid_onchain_maker_bond) && o.is_maker) ||
    (Boolean(o.hybrid_onchain_taker_bond) && !o.is_maker);
  return mine && [4, 5, 12, 14, 17, 18].includes(o.status) && o.bond_status_code === 5;
};

/** Mixta C/D (26 nov 2026): this robot is taking a Lightning order with an onchain bond. */
export const lnOnchainTakeStep = (order: Order): boolean => {
  const o = order as unknown as O & { take_onchain?: boolean };
  return o.status === 3 && !o.is_maker && Boolean(o.take_onchain);
};

/** 0.9.14-beta6 (Kilombino TG 15636): after this long with a confirmed bond still unsigned,
 * the screen asks to keep the app open. */
export const SIGN_SLOW_MS = 20_000;

/** The bond is confirmed and the app is signing it: the line to show, and whether the
 * signature is taking too long (sinceMs = when this screen first saw it confirmed). */
export const bondSigningLines = (
  sinceMs: number | null,
  nowMs: number,
): { main: string; slow: string | null } => ({
  main: '✅ Confirmed · ✍️ signing…',
  slow:
    sinceMs !== null && nowMs - sinceMs >= SIGN_SLOW_MS
      ? 'Keep the app open: your signature is missing'
      : null,
});
