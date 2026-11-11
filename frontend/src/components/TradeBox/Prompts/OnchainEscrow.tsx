import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Grid,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import { ContentCopy } from '@mui/icons-material';
import QRCode from 'react-qr-code';

import { type Order } from '../../../models';
import { systemClient } from '../../../services/System';
import { pn } from '../../../utils';
import {
  EscrowCheckError,
  checkFundingAddress,
  escrowKeys,
  publicKeys,
  releaseNonce,
  releasePartial,
  signBondPenalty,
  signBondReturn,
  signDeposit,
  signDisputeRelease,
  type EscrowKeys,
  type EscrowOrderData,
} from '../../../utils/escrow/client';
import { addressToScript } from '../../../utils/escrow/taproot';

export type EscrowAction =
  | 'bond_key'
  | 'bond_penalty_signature'
  | 'bond_return'
  | 'bond_return_signature'
  | 'escrow_keys'
  | 'escrow_address'
  | 'escrow_funding_signatures'
  | 'escrow_nonce'
  | 'escrow_partial'
  | 'escrow_leaf_signature';

interface Props {
  order: Order;
  token: string | undefined;
  onAction: (action: EscrowAction, data: object, signedAddress?: string) => void;
  onSignedAddress: (address: string, then: (signed: string) => void) => void;
}

const BOND = { FUNDING: 0, SIGN: 1, LOCKED: 2, RETURNABLE: 5 };
const ESCROW = { KEYS: 0, FUNDING: 1, MEMPOOL: 2, LOCKED: 3, RELEASING: 4 };

const PayBox = ({
  address,
  sats,
  label,
}: {
  address: string;
  sats: number;
  label: string;
}): React.JSX.Element => {
  const { t } = useTranslation();
  const uri = `bitcoin:${address}?amount=${(sats / 1e8).toFixed(8)}`;
  return (
    <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
      <Grid>
        <Typography variant='body2' align='center'>
          {label}
        </Typography>
      </Grid>
      <Grid>
        <Box sx={{ background: '#fff', padding: '0.5em', borderRadius: '0.3em' }}>
          <QRCode value={uri} size={200} />
        </Box>
      </Grid>
      <Grid>
        <Tooltip title={t('Copy to clipboard')} enterTouchDelay={0}>
          <Button
            size='small'
            startIcon={<ContentCopy />}
            onClick={() => systemClient.copyToClipboard(address)}
          >
            {address.slice(0, 12)}…{address.slice(-8)}
          </Button>
        </Tooltip>
      </Grid>
      <Grid>
        <Typography variant='body1' align='center'>
          <b>{pn(sats)} Sats</b>
        </Typography>
      </Grid>
    </Grid>
  );
};

export const OnchainEscrowPrompt = ({
  order,
  token,
  onAction,
  onSignedAddress,
}: Props): React.JSX.Element => {
  const { t } = useTranslation();
  const data = order as unknown as EscrowOrderData;
  const [error, setError] = useState<string | null>(null);
  const [address, setAddress] = useState<string>('');
  const sent = useRef<Set<string>>(new Set());
  const secnonce = useRef<Uint8Array | null>(null);

  const keys: EscrowKeys | null =
    token && data.escrow_alias ? escrowKeys(token, data.escrow_alias, order.id) : null;

  const once = (key: string, fn: () => void): void => {
    if (sent.current.has(key)) return;
    sent.current.add(key);
    try {
      fn();
    } catch (e) {
      sent.current.delete(key);
      setError(e instanceof EscrowCheckError ? t(e.message) : String(e));
    }
  };

  useEffect(() => {
    if (!keys) return;
    setError(null);
    const pk = publicKeys(keys);
    if ((order.status === 0 || order.status === 3) && data.bond_status_code === undefined)
      once('bond_key', () => onAction('bond_key', { bond_pubkey: pk.bond }));
    if (data.bond_status_code === BOND.SIGN && data.bond_penalty_tx)
      once(`penalty-${data.bond_txid}`, () =>
        onAction('bond_penalty_signature', { signature: signBondPenalty(data, keys) }),
      );
    if (data.escrow_status_code === ESCROW.KEYS && data.escrow_my_keys_set === false)
      once('escrow_keys', () =>
        onAction('escrow_keys', { escrow_pubkey: pk.escrow, funding_pubkey: pk.funding }),
      );
    if (
      data.escrow_status_code === ESCROW.FUNDING &&
      data.escrow_deposit_tx &&
      !data.escrow_deposit_signed
    )
      once(`deposit-${data.escrow_deposit_tx.slice(0, 64)}`, () =>
        onAction(
          'escrow_funding_signatures',
          signDeposit(data, keys, data, {
            maxSpendSats: (data.escrow_funding_recommended ?? 0) + (data.bond_value ?? 0),
            maxFeeRate: 100,
          }),
        ),
      );
    if (
      data.escrow_status_code === ESCROW.RELEASING &&
      data.escrow_release_tx &&
      data.escrow_my_payout_address
    ) {
      const myScript = addressToScript(data.escrow_my_payout_address);
      const isBuyer = data.escrow_role === 'buyer';
      const cooperative = (data.escrow_release_kind ?? '').startsWith('Seller and buyer');
      const minToMe = cooperative
        ? isBuyer
          ? Math.floor((data.escrow_sats ?? 0) * 0.98)
          : Math.floor((data.bond_value ?? 0) * 0.98)
        : Math.floor((data.escrow_sats ?? 0) * 0.98);
      const check = { myScript, minToMe };
      if (cooperative) {
        if (!data.escrow_release_my_nonce)
          once(`nonce-${data.escrow_release_tx.slice(0, 64)}`, () => {
            const n = releaseNonce(data, keys, check);
            secnonce.current = n.secnonce;
            onAction('escrow_nonce', { nonce: n.pubnonce });
          });
        else if (
          data.escrow_release_aggnonce &&
          !data.escrow_release_my_partial &&
          secnonce.current
        )
          once(`partial-${data.escrow_release_aggnonce}`, () => {
            const partial = releasePartial(data, keys, check, secnonce.current!);
            secnonce.current = null;
            onAction('escrow_partial', { partial });
          });
        else if (
          data.escrow_release_my_nonce &&
          !secnonce.current &&
          !data.escrow_release_my_partial
        )
          once(`renonce-${data.escrow_release_tx.slice(0, 64)}`, () => {
            const n = releaseNonce(data, keys, check);
            secnonce.current = n.secnonce;
            onAction('escrow_nonce', { nonce: n.pubnonce });
          });
      } else {
        once(`leaf-${data.escrow_release_tx.slice(0, 64)}`, () =>
          onAction('escrow_leaf_signature', { signature: signDisputeRelease(data, keys, check) }),
        );
      }
    }
    if (data.bond_status_code === BOND.RETURNABLE && data.bond_return_tx) {
      const target = address || data.escrow_my_payout_address;
      if (target)
        once(`return-${data.bond_return_tx.slice(0, 64)}`, () =>
          onAction('bond_return_signature', {
            signature: signBondReturn(data, keys, addressToScript(target), 20_000),
          }),
        );
    }
  }, [
    order.status,
    data.bond_status_code,
    data.escrow_status_code,
    data.escrow_deposit_tx,
    data.escrow_release_tx,
    data.escrow_release_aggnonce,
    data.bond_return_tx,
  ]);

  const submitAddress = (name: 'escrow_address' | 'bond_return'): void => {
    try {
      addressToScript(address);
    } catch {
      setError(t('Use a valid segwit or taproot address'));
      return;
    }
    if (name === 'escrow_address')
      onSignedAddress(address, (signed) => onAction('escrow_address', {}, signed));
    else onAction('bond_return', { address });
  };

  if (!keys) return <CircularProgress />;

  const waiting = (text: string): React.JSX.Element => (
    <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
      <Grid>
        <Typography variant='body2' align='center'>
          {text}
        </Typography>
      </Grid>
      <Grid>
        <CircularProgress />
      </Grid>
    </Grid>
  );

  let body: React.JSX.Element = waiting(t('Waiting for the coordinator…'));

  if ((order.status === 0 || order.status === 3) && data.bond_status_code !== undefined) {
    if (data.bond_status_code === BOND.FUNDING && data.bond_address)
      body = (
        <PayBox
          address={data.bond_address}
          sats={data.bond_required_sats ?? 0}
          label={t(
            'Send exactly this amount from any wallet. It is your bond: nobody can move it alone, it comes back to you after the trade (or after about 3 days in any case), and you lose it only if you cancel or do not complete your part in time.',
          )}
        />
      );
    else body = waiting(t('Bond received, waiting for 1 confirmation…'));
  } else if (
    order.status === 6 &&
    data.escrow_status_code === ESCROW.KEYS &&
    !data.escrow_my_payout_address
  ) {
    body = (
      <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
        <Grid>
          <Typography variant='body2' align='center'>
            {data.escrow_role === 'buyer'
              ? t('Bitcoin address where you will receive the sats (and your bond back).')
              : t('Bitcoin address for your bond back (and a refund if the trade is cancelled).')}
          </Typography>
        </Grid>
        <Grid>
          <TextField
            size='small'
            fullWidth
            value={address}
            onChange={(e) => setAddress(e.target.value.trim())}
            label={t('Bitcoin address')}
          />
        </Grid>
        <Grid>
          <Button
            variant='contained'
            disabled={!address}
            onClick={() => submitAddress('escrow_address')}
          >
            {t('Submit')}
          </Button>
        </Grid>
      </Grid>
    );
  } else if (
    order.status === 7 &&
    data.escrow_status_code === ESCROW.KEYS &&
    data.escrow_funding_address
  ) {
    try {
      checkFundingAddress(data, keys);
      body = (
        <PayBox
          address={data.escrow_funding_address}
          sats={data.escrow_funding_recommended ?? 0}
          label={
            data.escrow_role === 'seller'
              ? t(
                  'Send this amount to your funding address: the trade amount, your fee and the mining fee. Anything left over comes back to you in the deposit.',
                )
              : t(
                  'Send this amount to your funding address: your fee and the mining fee. Anything left over comes back to you.',
                )
          }
        />
      );
    } catch (e) {
      body = <Alert severity='error'>{String(e)}</Alert>;
    }
  } else if (data.escrow_status_code === ESCROW.FUNDING) {
    body = waiting(
      data.escrow_deposit_signed
        ? t('Deposit signed. Waiting for your peer…')
        : t('Checking and signing the deposit…'),
    );
  } else if (data.escrow_status_code === ESCROW.MEMPOOL) {
    body = waiting(t('Deposit sent. Waiting for 1 confirmation…'));
  } else if (data.escrow_status_code === ESCROW.RELEASING) {
    body = waiting(t('Signing the release together with your peer…'));
  } else if (data.bond_status_code === BOND.RETURNABLE) {
    body = (
      <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
        <Grid>
          <Typography variant='body2' align='center'>
            {t('Your bond is free. Get it back to an address of yours.')}
          </Typography>
        </Grid>
        <Grid>
          <TextField
            size='small'
            fullWidth
            value={address}
            onChange={(e) => setAddress(e.target.value.trim())}
            label={t('Bitcoin address')}
          />
        </Grid>
        <Grid>
          <Button
            variant='contained'
            disabled={!address}
            onClick={() => submitAddress('bond_return')}
          >
            {t('Get my bond back')}
          </Button>
        </Grid>
      </Grid>
    );
  }

  return (
    <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center', padding: 1 }}>
      <Grid>{body}</Grid>
      {error ? (
        <Grid>
          <Alert severity='error'>{error}</Alert>
        </Grid>
      ) : null}
    </Grid>
  );
};

export default OnchainEscrowPrompt;
