/**
 * Roblosats 0.9.4: does the open order need something from this robot right now? The text
 * (an i18n key) says what. Drives the "action needed" banner, the sound/vibration and the
 * flashing tab title; the coordinator sends the same steps as notifications.
 */

export interface ActionOrder {
  status: number;
  is_maker?: boolean;
  is_taker?: boolean;
  is_buyer?: boolean;
  is_seller?: boolean;
  escrow_type?: string | number;
  bond_invoice?: string;
  bond_address?: string;
  take_lightning?: boolean;
  pending_cancel?: boolean;
  statement_submitted?: boolean;
  escrow_status_code?: number;
  bond_status_code?: number;
  escrow_waiting_for?: string;
  escrow_waiting_what?: string;
  escrow_leftover_sats?: number;
  escrow_sweep_txid?: string;
  invoice_amount?: number;
}

const BOND_SIGN = 1;
const BOND_RETURNABLE = 5;
const ESCROW_KEYS = 0;

const isOnchain = (o: ActionOrder): boolean => o.escrow_type === 'onchain' || o.escrow_type === 1;

export const actionNeeded = (o: ActionOrder | null | undefined): string | null => {
  if (!o) return null;
  const onchain = isOnchain(o);
  const st = o.status;
  const what = o.escrow_waiting_what ?? '';
  const mine = o.escrow_waiting_for === 'you';

  // the app signs by itself, but only while the order is open
  if (onchain && mine && what.endsWith('_signature'))
    return 'Keep this order open: your app has to sign';
  if (onchain && o.bond_status_code === BOND_SIGN && (st === 0 || st === 1 || st === 3))
    return 'Keep this order open: your app has to sign';

  if (o.pending_cancel && [8, 9, 10].includes(st))
    return 'Your counterpart asked to cancel by agreement: accept it or keep trading';

  switch (st) {
    case 0:
      return o.is_maker ? 'Lock your bond to publish the order' : null;
    case 1:
    case 3:
      if (o.is_maker) return null;
      return o.bond_invoice || o.bond_address || st === 3
        ? 'Lock your bond to take the order'
        : null;
    case 6:
    case 7:
    case 8:
      if (onchain) {
        if (st === 6 || o.escrow_status_code === ESCROW_KEYS) {
          if (mine && (what === 'address' || what === 'keys'))
            return o.is_buyer
              ? 'Give the address that will receive the sats'
              : 'Give your refund address';
          if (mine && what === 'funding')
            return o.is_seller
              ? 'Fund the onchain escrow'
              : 'Send your fee to your funding address';
        }
        return null;
      }
      if (o.is_seller && (st === 6 || st === 7)) return 'Lock the trade escrow';
      if (o.is_buyer && (st === 6 || st === 8)) return 'Send your payout invoice';
      return null;
    case 9:
      return o.is_buyer ? 'Send the payment and press "Confirm sent"' : null;
    case 10:
      return o.is_seller ? 'Check your account: confirm only when the money has arrived' : null;
    case 11:
      return o.statement_submitted ? null : 'Send your dispute statement';
    case 15:
      return o.is_buyer ? 'Send a new invoice' : null;
    default:
      break;
  }
  if (
    [4, 5, 12, 14, 15, 17, 18].includes(st) &&
    (o.bond_status_code === BOND_RETURNABLE ||
      ((o.escrow_leftover_sats ?? 0) > 0 && !o.escrow_sweep_txid))
  )
    return 'Get your sats back';
  return null;
};

export default actionNeeded;
