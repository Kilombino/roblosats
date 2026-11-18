import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Grid, TextField } from '@mui/material';

import { type Order } from '../../../models';
import { pn } from '../../../utils';
import {
  EscrowCheckError,
  escrowKeys,
  signBondLeftover,
  type EscrowOrderData,
} from '../../../utils/escrow/client';
import { addressToScript, bytesToHex } from '../../../utils/escrow/taproot';
import { RecoveredLine, type EscrowAction } from './OnchainEscrow';

interface Props {
  order: Order;
  token: string | undefined;
  onAction: (action: EscrowAction, data: object) => void;
}

/** rl31-beta18: coins at our bond address that are not the bond (a short payment we topped up
 * with a full one, an extra payment, a payment after the bond was spent). They are ours: the
 * coordinator co-signs their return to an address of ours at any time, whatever the order is
 * doing (the bond itself stays where it is). Without the coordinator, "Recover without the
 * coordinator" takes them back alone after the bond's timelock. */
export const hasBondLeftover = (order: Order): boolean => {
  const d = order as unknown as EscrowOrderData;
  return (d.bond_leftover_sats ?? 0) > 0 || Boolean(d.bond_leftover_txid);
};

export const BondLeftoverBox = ({ order, token, onAction }: Props): React.JSX.Element | null => {
  const { t } = useTranslation();
  const data = order as unknown as EscrowOrderData;
  const [address, setAddress] = useState<string>(data.escrow_my_payout_address ?? '');
  const [error, setError] = useState<string | null>(null);
  const sent = useRef<Set<string>>(new Set());
  const left = data.bond_leftover_sats ?? 0;

  useEffect(() => {
    if (!token || !data.escrow_alias || !data.bond_leftover_tx || left <= 0 || !address) return;
    let script: Uint8Array;
    try {
      script = addressToScript(address);
    } catch {
      return;
    }
    // A return built earlier for another address of ours: wait for the new one (no error).
    if (!data.bond_leftover_tx.includes(bytesToHex(script))) return;
    const key = `leftover-${data.bond_leftover_tx}`;
    if (sent.current.has(key)) return;
    sent.current.add(key);
    try {
      const keys = escrowKeys(token, data.escrow_alias, order.id);
      onAction('bond_leftover_signatures', {
        signatures: signBondLeftover(data, keys, script, 20_000),
      });
    } catch (e) {
      sent.current.delete(key);
      setError(e instanceof EscrowCheckError ? t(e.message) : String(e));
    }
  }, [data.bond_leftover_tx, address, token]);

  if (left <= 0 && !data.bond_leftover_txid) return null;

  const ask = (): void => {
    try {
      addressToScript(address);
    } catch {
      setError(t('Use a valid segwit or taproot address'));
      return;
    }
    setError(null);
    onAction('bond_leftover_return', { address });
  };

  const fee = data.bond_leftover_fee_sats ?? 0;
  // rl31-beta20: not worth sending back now (the mining fee would eat more than the sats)
  const notWorth = left > 0 && data.bond_leftover_worth === false;
  const returns =
    data.bond_leftover_returns ??
    (data.bond_leftover_txid
      ? [
          {
            txid: data.bond_leftover_txid,
            sats: data.bond_leftover_returned_sats ?? 0,
            status: 'pending' as const,
          },
        ]
      : []);

  return (
    <Grid
      container
      spacing={1}
      sx={{ flexDirection: 'column', alignItems: 'center', padding: 1, width: '100%' }}
    >
      {left > 0 && notWorth ? (
        <Grid>
          <Alert severity='info'>
            <b>{t('Recover your sats')}</b>
            <br />
            {t(
              'Not worth it right now: the mining fee (~{{fee}} sats) would eat more than the {{amount}} sats',
              { fee: pn(fee), amount: pn(left) },
            )}
          </Alert>
        </Grid>
      ) : null}
      {left > 0 && !notWorth ? (
        <>
          <Grid>
            <Alert severity='warning'>
              <b>{t('Recover your sats')}</b>
              <br />
              {t(
                'There are {{amount}} Sats at your bond address that are not part of the bond (a payment that was short, extra or late). They are yours: get them back to an address of yours now. If the coordinator disappears, "Recover without the coordinator" takes them back after the bond timelock.',
                { amount: pn(left) },
              )}
            </Alert>
          </Grid>
          <Grid sx={{ width: '100%' }}>
            <TextField
              size='small'
              fullWidth
              value={address}
              onChange={(e) => setAddress(e.target.value.trim())}
              label={t('Bitcoin address')}
            />
          </Grid>
          <Grid>
            <Button variant='contained' disabled={!address} onClick={ask}>
              {t('Get my sats back')}
            </Button>
          </Grid>
        </>
      ) : null}
      {returns.map((r) => (
        <Grid key={r.txid} sx={{ width: '100%' }}>
          <RecoveredLine sats={r.sats} txid={r.txid} confirmed={r.status === 'confirmed'} />
        </Grid>
      ))}
      {error ? (
        <Grid>
          <Alert severity='error'>{error}</Alert>
        </Grid>
      ) : null}
    </Grid>
  );
};

export default BondLeftoverBox;
