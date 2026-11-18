import React, { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Grid, Typography, TextField, List, Divider, ListItemText, ListItem } from '@mui/material';
import { type Order } from '../../../models';
import { LoadingButton } from '@mui/lab';
import { pn } from '../../../utils';
import {
  MIN_ONCHAIN_PAYOUT_SATS,
  PAYOUT_VBYTES,
  maxFeeRateForMinimum,
  onchainPayout,
  sellerMiningCover,
} from '../../../utils/hybridSwap';
import { numberFromInput } from '../../../utils/numberInput';

export interface OnchainForm {
  address: string;
  // 0.9.7: null while the field is empty (it used to keep a "0" that could not be erased)
  miningFee: number | null;
  badAddress: string;
}

export const defaultOnchain: OnchainForm = {
  address: '',
  miningFee: 10,
  badAddress: '',
};

interface OnchainPayoutFormProps {
  order: Order;
  loading: boolean;
  onchain: OnchainForm;
  setOnchain: (state: OnchainForm) => void;
  onClickSubmit: () => void;
}

export const OnchainPayoutForm = ({
  order,
  loading,
  onClickSubmit,
  onchain,
  setOnchain,
}: OnchainPayoutFormProps): React.JSX.Element => {
  const { t } = useTranslation();

  const minMiningFee = 2;
  const costPerVByte = PAYOUT_VBYTES;

  const handleMiningFeeChange = (e: React.ChangeEvent<HTMLInputElement>): void => {
    setOnchain({ ...onchain, miningFee: numberFromInput(e.target) });
  };

  useEffect(() => {
    setOnchain({ ...onchain, miningFee: order.suggested_mining_fee_rate });
  }, []);

  // rl31-beta21, hybrid B: the seller already paid this swap (up to this much)
  const paidBySeller = Number(
    (order as unknown as { swap_paid_by_seller_sats?: number }).swap_paid_by_seller_sats ?? 0,
  );
  const feeRate = onchain.miningFee ?? 0;
  const miningFeeSats = Math.floor(Math.max(minMiningFee, feeRate) * costPerVByte);
  const swapTx = {
    invoiceAmount: order.invoice_amount,
    swapFeeRate: Number(order.swap_fee_rate),
    paidBySeller,
  };
  const payout = onchainPayout({ ...swapTx, miningFeeSats });
  const bySeller = paidBySeller > 0;
  // 0.9.7: the coordinator refuses a payout under 20,000 Sats: cap the fee before that
  const maxMiningFee = Math.min(500, maxFeeRateForMinimum(swapTx));
  const cover = sellerMiningCover(swapTx);
  const emptyFee = onchain.miningFee === null;
  const belowMinimum = payout.received < MIN_ONCHAIN_PAYOUT_SATS;
  const invalidFee = emptyFee || feeRate < minMiningFee || feeRate > maxMiningFee || belowMinimum;

  return (
    <Grid
      container

      spacing={0.5}
      sx={{ alignItems: 'center', justifyContent: 'flex-start', flexDirection: 'column' }}
    >
      <List dense={true}>
        <ListItem>
          <Typography variant='body2'>
            {t('RoboSats coordinator will do a swap and send the Sats to your onchain address.')}
          </Typography>
        </ListItem>

        <ListItem>
          <Typography variant='body2'>
            <b>
              {bySeller
                ? payout.covered < payout.cost
                  ? t(
                      'No swap fee for you: the seller pays the swap to onchain. The seller pays mining up to {{rate}} sat/vB ({{covered}} Sats); anything above that ({{excess}} Sats) comes out of what you receive.',
                      {
                        rate: cover.rate,
                        covered: pn(cover.sats),
                        excess: pn(payout.cost - payout.covered),
                      },
                    )
                  : t('No swap fee for you: the seller pays the swap to onchain.')
                : t(
                    'You (the buyer) pay the swap fee and the mining fee: both are deducted from what you receive.',
                  )}
            </b>
          </Typography>
        </ListItem>

        <Divider />

        <ListItem>
          <ListItemText
            primary={`${pn(Math.floor((order.invoice_amount * order.swap_fee_rate) / 100))} Sats (${Number(
              Number(order.swap_fee_rate).toFixed(2),
            )}%)`}
            secondary={bySeller ? t('Swap fee (paid by the seller)') : t('Swap fee')}
          />
        </ListItem>

        <Divider />

        <ListItem>
          <ListItemText
            primary={`${pn(miningFeeSats)} Sats (${Math.max(minMiningFee, feeRate)} Sats/vByte)`}
            secondary={
              bySeller
                ? t('Mining fee (paid by the seller up to {{rate}} sat/vB)', { rate: cover.rate })
                : t('Mining fee')
            }
          />
        </ListItem>

        <Divider />

        <ListItem>
          <ListItemText
            primary={<b>{pn(Math.max(0, payout.received)) + ' Sats'}</b>}
            secondary={t('Final amount you will receive')}
          />
        </ListItem>
      </List>

      <Grid>
        <Grid
          container
          direction='row'
          spacing={0}
          sx={{ alignItems: 'flex-start', justifyContent: 'center' }}
        >
          <Grid size={7}>
            <TextField
              error={onchain.badAddress !== ''}
              helperText={onchain.badAddress !== '' ? t(onchain.badAddress) : ''}
              label={t('Bitcoin Address')}
              required
              value={onchain.address}
              fullWidth
              maxRows={1}
              onChange={(e) => {
                setOnchain({ ...onchain, address: e.target.value });
              }}
            />
          </Grid>
          <Grid size={5}>
            <TextField
              error={invalidFee}
              helperText={t('Max {{max}} sat/vB', { max: maxMiningFee })}
              label={t('Mining Fee')}
              required
              fullWidth={true}
              value={onchain.miningFee ?? ''}
              type='number'
              slotProps={{
                htmlInput: {
                  max: maxMiningFee,
                  min: minMiningFee,
                  style: { textAlign: 'center' },
                },
              }}
              onChange={(e) => handleMiningFeeChange(e as React.ChangeEvent<HTMLInputElement>)}
            />
          </Grid>
        </Grid>
      </Grid>

      {!emptyFee && (belowMinimum || feeRate > maxMiningFee) ? (
        <Grid sx={{ maxWidth: '26em', mt: 1 }}>
          <Typography variant='body2' color='error' align='center'>
            {t(
              'With this fee you would receive less than {{min}} Sats, the least an onchain payout can be. Use at most {{max}} sat/vB.',
              { min: pn(MIN_ONCHAIN_PAYOUT_SATS), max: maxMiningFee },
            )}
          </Typography>
        </Grid>
      ) : null}

      <Grid style={{ marginTop: 16 }}>
        <LoadingButton
          loading={loading}
          onClick={onClickSubmit}
          disabled={invalidFee}
          variant='outlined'
          color='primary'
          size='large'
        >
          {t('Submit')}
        </LoadingButton>
      </Grid>
    </Grid>
  );
};

export default OnchainPayoutForm;
