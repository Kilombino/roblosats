// Roblosats rl31-beta14: one final summary block for every way an order ends (Lightning,
// onchain or hybrid; buyer or seller; maker or taker). The coordinator computes it
// (api/order_summary.py) and sends it in the order (final_summary) and in /api/pastorders/.
import React from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Box, Button, Chip, Divider, Paper, Tooltip, Typography } from '@mui/material';
import { ContentCopy } from '@mui/icons-material';

import { pn } from '../../utils';
import currencies from '../../utils/currencies';
import { summaryAmountText } from '../../utils/spamAmounts';
import { systemClient } from '../../services/System';
import { TxLine } from '../TradeBox/Prompts/OnchainEscrow';

export interface FinalSummaryData {
  order_id: number;
  status: number;
  outcome: string;
  escrow_type?: 'lightning' | 'onchain' | 'hybrid';
  role?: 'maker' | 'taker';
  is_buyer?: boolean;
  currency?: number | null;
  amount?: string | null;
  payment_method?: string;
  created_at?: string;
  finished_at?: string | null;
  contract?: boolean;
  trade_sats?: number;
  sent_sats?: number;
  received_sats?: number;
  returned_sats?: number;
  bond_kind?: 'lightning' | 'onchain';
  bond_sats?: number;
  bond_state?: string;
  bond_returned_sats?: number;
  /** rl31-beta18: sats at the bond address that were not the bond, still there / sent back */
  bond_extra_sats?: number;
  bond_extra_returned_sats?: number;
  /** rl31-beta20: sats left on the funding address and sent back */
  funding_leftover_returned_sats?: number;
  /** rl31-beta20: funding excess that rode in the escrow and came back with the bond */
  escrow_extra_returned_sats?: number;
  bond_lost_sats?: number;
  bond_lost_reason?: string;
  fee_from_bond?: boolean;
  fee_at_release?: boolean;
  coordinator_fee_sats?: number;
  fee_refunded_sats?: number;
  dev_fee_sats?: number;
  mining_fee_sats?: number;
  routing_fee_sats?: number;
  swap_fee_sats?: number;
  swap_for_buyer_sats?: number;
  swap_paid_by_seller_sats?: number;
  rewards_sats?: number;
  // 0.9.7: status of onchain payouts and releases: mempool | confirmed | queued
  txs?: Array<{ label: string; txid: string; status?: string | null }>;
  hashes?: Array<{ label: string; value: string }>;
  error?: string;
}

type Color = 'success' | 'error' | 'warning' | 'info' | 'default';

/** Short text and colour of how the order ended, for the title and the past orders list. */
export const outcomeOf = (
  s: FinalSummaryData,
  t: (k: string) => string,
): { label: string; color: Color } => {
  const lost = (s.bond_lost_sats ?? 0) > 0;
  switch (s.outcome) {
    case 'success':
      return { label: t('Successful trade'), color: 'success' };
    case 'sending':
      return { label: t('Sending your sats'), color: 'info' };
    case 'routing_failed':
      return { label: t('Lightning payment failed'), color: 'warning' };
    case 'cancelled':
      return {
        label: lost ? t('Cancelled, bond lost') : t('Cancelled'),
        color: lost ? 'error' : 'default',
      };
    case 'collab_cancelled':
      return { label: t('Collaboratively cancelled'), color: 'default' };
    case 'expired':
      return {
        label: lost ? t('Expired, bond lost') : t('Expired'),
        color: lost ? 'error' : 'default',
      };
    case 'dispute_returned':
      return { label: t('Dispute: everything returned'), color: 'default' };
    case 'dispute_won':
      return { label: t('Dispute won'), color: 'success' };
    case 'dispute_lost':
      return { label: t('Dispute lost'), color: 'error' };
    case 'taker_left':
      return { label: t('You left the order, bond lost'), color: 'error' };
    default:
      return { label: t('Finished'), color: 'default' };
  }
};

const lostReason = (reason: string | undefined, t: (k: string) => string): string => {
  switch (reason) {
    case 'cancelled_after_taken':
      return t('you cancelled the order after it was taken');
    case 'escrow_not_locked':
      return t('the seller (you) did not lock the trade escrow in time');
    case 'invoice_not_submitted':
      return t('the buyer (you) did not give the payout invoice or address in time');
    case 'dispute_lost':
      return t('you lost the dispute');
    case 'taker_left':
      return t('you cancelled or ran out of time after taking the order');
    default:
      return t('penalized by the coordinator');
  }
};

const txLabel = (label: string, t: (k: string) => string): string => {
  switch (label) {
    case 'deposit':
      return t('Escrow deposit');
    case 'release':
      return t('Escrow release');
    case 'bond':
      return t('Your bond payment');
    case 'bond_return':
      return t('Your bond returned');
    case 'bond_leftover_return':
      return t('Return of the sats that were not the bond');
    case 'funding_leftover_return':
      return t('Return of the sats left on your funding address');
    case 'bond_penalty':
      return t('Your bond penalized');
    case 'payout':
      return t('Your onchain payout');
    default:
      return label;
  }
};

const hashLabel = (label: string, t: (k: string) => string): string => {
  switch (label) {
    case 'bond_hash':
      return t('Bond payment hash');
    case 'escrow_hash':
      return t('Escrow payment hash');
    case 'payout_hash':
      return t('Payout payment hash');
    case 'payout_preimage':
      return t('Payout preimage');
    default:
      return label;
  }
};

const Row = ({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}): React.JSX.Element => (
  <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}>
    <Typography variant='body2' color='text.secondary'>
      {label}
    </Typography>
    <Typography variant='body2' color={color} sx={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
      <b>{value}</b>
    </Typography>
  </Box>
);

const sats = (n: number | undefined): string => `${pn(Math.round(n ?? 0))} Sats`;

const FinalSummary = ({
  summary,
  title = true,
  satsDenominated,
}: {
  summary: FinalSummaryData;
  title?: boolean;
  satsDenominated?: boolean; // 0.9.1: a "丰 sat" order (known on the order page)
}): React.JSX.Element => {
  const { t } = useTranslation();
  const s = summary;
  const outcome = outcomeOf(s, t);
  const escrow =
    s.escrow_type === 'onchain'
      ? t('Onchain')
      : s.escrow_type === 'hybrid'
        ? t('Onchain + Lightning')
        : t('Lightning');
  const currencyCode = s.currency != null ? (currencies[`${s.currency}`] ?? '') : '';
  const role = `${s.is_buyer ? t('Buyer role') : t('Seller role')} · ${s.role === 'maker' ? t('Maker role') : t('Taker role')}`;
  const bondLabel =
    s.bond_kind === 'onchain' ? t('Your bond (onchain)') : t('Your bond (Lightning)');
  const lost = (s.bond_lost_sats ?? 0) > 0;
  let bondState = '';
  switch (s.bond_state) {
    case 'returned':
      bondState =
        (s.bond_returned_sats ?? 0) > 0 && (s.bond_returned_sats ?? 0) < (s.bond_sats ?? 0)
          ? `${t('Returned')}: ${sats(s.bond_returned_sats)}`
          : t('Returned in full');
      break;
    case 'lost':
      bondState = `${t('Lost')}: ${sats(s.bond_lost_sats)}`;
      break;
    case 'recoverable':
      bondState = t('Free: take it back from the order page');
      break;
    case 'locked':
      bondState = t('Still locked');
      break;
    case 'in_escrow':
      bondState = t('Inside the escrow');
      break;
    case 'rewards':
      bondState = t('Returned as rewards');
      break;
    case 'not_locked':
      bondState = t('Never locked');
      break;
    default:
      bondState = '';
  }
  const fee = s.coordinator_fee_sats ?? 0;
  const feeLabel = s.fee_from_bond
    ? t('Coordinator fee (out of your bond)')
    : s.fee_at_release
      ? t('Coordinator fee (out of what you received)')
      : t('Coordinator fee');

  if (s.error) {
    return (
      <Alert severity='warning'>
        {t('Order')} #{s.order_id}: {s.error}
      </Alert>
    );
  }

  return (
    <Paper elevation={0} variant='outlined' sx={{ padding: 1.5, width: '100%' }}>
      {title ? (
        <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 1 }}>
          <Typography variant='subtitle1'>
            <b>
              {t('Summary of order')} #{s.order_id}
            </b>
          </Typography>
          <Chip size='small' label={outcome.label} color={outcome.color} />
        </Box>
      ) : null}
      <Row label={t('Your role')} value={role} />
      <Row label={t('Escrow')} value={escrow} />
      {s.amount && Number(s.amount) > 0 ? (
        <Row
          label={t('Amount')}
          value={`${summaryAmountText(s, currencyCode, satsDenominated, pn)}${s.payment_method ? ` · ${s.payment_method}` : ''}`}
        />
      ) : null}
      {s.contract && (s.trade_sats ?? 0) > 0 ? (
        <Row label={t('Trade amount')} value={sats(s.trade_sats)} />
      ) : null}
      {(s.sent_sats ?? 0) > 0 ? <Row label={t('You sent')} value={sats(s.sent_sats)} /> : null}
      {(s.received_sats ?? 0) > 0 ? (
        <Row label={t('You received')} value={sats(s.received_sats)} color='success.main' />
      ) : null}
      {(s.returned_sats ?? 0) > 0 ? (
        <Row label={t('Returned to you')} value={sats(s.returned_sats)} />
      ) : null}
      <Divider sx={{ my: 0.75 }} />
      {(s.bond_sats ?? 0) > 0 ? <Row label={bondLabel} value={sats(s.bond_sats)} /> : null}
      {bondState ? (
        <Row
          label={t('Bond')}
          value={bondState}
          color={lost ? 'error.main' : s.bond_state === 'recoverable' ? 'warning.main' : undefined}
        />
      ) : null}
      {(s.bond_extra_sats ?? 0) > 0 ? (
        <Row
          label={t('Recover your sats')}
          value={t(
            '{{amount}} at your bond address that were not the bond: get them back from the order page',
            {
              amount: sats(s.bond_extra_sats),
            },
          )}
          color='warning.main'
        />
      ) : null}
      {(s.bond_extra_returned_sats ?? 0) > 0 ? (
        <Row label={t('Not the bond, returned')} value={sats(s.bond_extra_returned_sats)} />
      ) : null}
      {(s.escrow_extra_returned_sats ?? 0) > 0 ? (
        <Row
          label={t('Extra returned with your bond')}
          value={`+${sats(s.escrow_extra_returned_sats)}`}
        />
      ) : null}
      {(s.funding_leftover_returned_sats ?? 0) > 0 ? (
        <Row
          label={t('Left on your funding address, returned')}
          value={sats(s.funding_leftover_returned_sats)}
        />
      ) : null}
      {lost ? (
        <Typography variant='caption' color='error.main' sx={{ display: 'block' }}>
          {t('Why')}: {lostReason(s.bond_lost_reason, t)}
        </Typography>
      ) : null}
      {s.fee_from_bond && s.bond_state === 'returned' ? (
        <Typography variant='caption' color='text.secondary' sx={{ display: 'block' }}>
          {t('Your fee and your share of the deposit mining fee came out of your bond.')}
        </Typography>
      ) : null}
      {(s.rewards_sats ?? 0) > 0 ? (
        <Row
          label={t('Rewards for this order')}
          value={sats(s.rewards_sats)}
          color='success.main'
        />
      ) : null}
      <Divider sx={{ my: 0.75 }} />
      {fee > 0 || ['success', 'sending'].includes(s.outcome) ? (
        <Row label={feeLabel} value={sats(fee)} />
      ) : null}
      {(s.dev_fee_sats ?? 0) > 0 ? (
        <Row label={t('of which to the development fund')} value={sats(s.dev_fee_sats)} />
      ) : null}
      {(s.fee_refunded_sats ?? 0) > 0 ? (
        <Row label={t('Coordinator fee (returned)')} value={sats(s.fee_refunded_sats)} />
      ) : null}
      {(s.mining_fee_sats ?? 0) > 0 ? (
        <Row label={t('Mining fees you paid')} value={sats(s.mining_fee_sats)} />
      ) : null}
      {(s.routing_fee_sats ?? 0) > 0 ? (
        <Row label={t('Lightning routing budget')} value={sats(s.routing_fee_sats)} />
      ) : null}
      {(s.swap_fee_sats ?? 0) > 0 ? (
        <Row label={t('Onchain swap fee')} value={sats(s.swap_fee_sats)} />
      ) : null}
      {(s.swap_for_buyer_sats ?? 0) > 0 ? (
        <Row label={t("Buyer's swap to onchain you paid")} value={sats(s.swap_for_buyer_sats)} />
      ) : null}
      {(s.swap_paid_by_seller_sats ?? 0) > 0 ? (
        <Row
          label={t('Swap to onchain paid by the seller')}
          value={sats(s.swap_paid_by_seller_sats)}
        />
      ) : null}
      {(s.txs ?? []).map((tx) => (
        <Box key={tx.txid || tx.label} sx={{ mt: 1 }}>
          {tx.txid ? (
            <TxLine label={txLabel(tx.label, t)} txid={tx.txid} />
          ) : (
            <Typography variant='caption' component='p' align='center'>
              {txLabel(tx.label, t)}
            </Typography>
          )}
          {tx.status ? (
            <Typography
              variant='caption'
              component='p'
              align='center'
              color={tx.status === 'confirmed' ? 'success.main' : 'text.secondary'}
              data-testid='tx-status'
            >
              {tx.status === 'confirmed'
                ? t('Confirmed')
                : tx.status === 'mempool'
                  ? t('In the mempool, waiting for a block')
                  : t('Queued: the coordinator sends it shortly')}
            </Typography>
          ) : null}
        </Box>
      ))}
      {(s.hashes ?? []).map((h) => (
        <Box
          key={h.label + h.value}
          sx={{ mt: 1, display: 'flex', flexDirection: 'column', alignItems: 'center' }}
        >
          <Typography variant='caption'>{hashLabel(h.label, t)}</Typography>
          <Tooltip title={t('Copy to clipboard')} enterTouchDelay={0}>
            <Button
              size='small'
              variant='outlined'
              sx={{ textTransform: 'none' }}
              startIcon={<ContentCopy />}
              onClick={() => {
                systemClient.copyToClipboard(h.value);
              }}
            >
              {h.value.slice(0, 10)}…{h.value.slice(-8)}
            </Button>
          </Tooltip>
        </Box>
      ))}
    </Paper>
  );
};

export default FinalSummary;
