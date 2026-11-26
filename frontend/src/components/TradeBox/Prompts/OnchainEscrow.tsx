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
import { ContentCopy, OpenInNew } from '@mui/icons-material';
import QRCode from 'react-qr-code';

import { type Order } from '../../../models';
import { systemClient } from '../../../services/System';
import { explorerTxUrl } from '../../FinalSummary/bondState';
import { pn } from '../../../utils';
import {
  myExtraInEscrow,
  signSweep,
  EscrowCheckError,
  checkFundingAddress,
  escrowKeys,
  publicKeys,
  releaseNonce,
  releasePartial,
  signCooperativeLeaf,
  signBondAutoReturn,
  signBondAutoReturnLadder,
  bondCoinsOf,
  signBondPenalties,
  signBondPenaltyLadder,
  signBondReturns,
  signDeposit,
  signRescue,
  completeRescue,
  signDisputeRelease,
  owedInRelease,
  depositSpendLimit,
  type EscrowKeys,
  type EscrowOrderData,
} from '../../../utils/escrow/client';
import { saveRescue } from '../../../utils/escrow/rescue';
import { isMyAddress, pendingReturnKey, rememberMyAddress } from '../../../utils/myAddresses';
import { type FeeHint, formatRate, lowFeeNeeded, validHint } from '../../../utils/escrow/fees';
import { Transaction, addressToScript, bytesToHex } from '../../../utils/escrow/taproot';
import { DEFAULT_HRP } from '../../../utils/address';
import { useAddressConfirm } from '../../Dialogs/AddressConfirm';
import { bondSigningLines, feeAtReleaseKey, peerFundingSeen } from '../onchainEscrowSteps';

export type EscrowAction =
  | 'bond_key'
  | 'bond_penalty_signature'
  | 'bond_penalty_ladder_signature'
  | 'bond_return'
  | 'bond_return_signature'
  | 'bond_leftover_return'
  | 'bond_leftover_signatures'
  | 'bond_return_address'
  | 'bond_auto_return_signature'
  | 'escrow_keys'
  | 'escrow_address'
  | 'escrow_funding_signatures'
  | 'escrow_rescue_signature'
  | 'escrow_nonce'
  | 'escrow_partial'
  | 'escrow_leaf_signature'
  | 'escrow_release_leaf'
  | 'escrow_sweep'
  | 'escrow_sweep_signatures';

interface Props {
  order: Order;
  token: string | undefined;
  onAction: (action: EscrowAction, data: object, signedAddress?: string) => void;
  onSignedAddress: (address: string, then: (signed: string) => void) => void;
}

const BOND = { FUNDING: 0, SIGN: 1, LOCKED: 2, RETURNABLE: 5, RETURNED: 6, ABANDONED: 7 };
const ESCROW = { KEYS: 0, FUNDING: 1, MEMPOOL: 2, LOCKED: 3, RELEASING: 4 };


/** A transaction of the trade: short txid, copy button and a link to the explorer. */
/** A leftover already sent back (rl31-beta20): done, with its TXID and the explorer. */
export const RecoveredLine = ({
  sats,
  txid,
  confirmed,
}: {
  sats: number;
  txid: string;
  confirmed: boolean;
}): React.JSX.Element => {
  const { t } = useTranslation();
  return (
    <Alert severity='success' icon={false} sx={{ justifyContent: 'center' }}>
      <Typography variant='body2' align='center'>
        <b>{t('✅ Recovered: {{amount}} sats', { amount: pn(sats) })}</b>
      </Typography>
      <Typography variant='caption' align='center' sx={{ display: 'block' }}>
        {confirmed ? t('Confirmed') : t('On its way, waiting for its confirmation')}
      </Typography>
      <TxLine label={t('Your payment')} txid={txid} />
    </Alert>
  );
};

export const TxLine = ({ label, txid }: { label: string; txid: string }): React.JSX.Element => {
  const { t } = useTranslation();
  return (
    <Grid container spacing={0.5} sx={{ flexDirection: 'column', alignItems: 'center' }}>
      <Grid>
        <Typography variant='caption' align='center'>
          {label}
        </Typography>
      </Grid>
      <Grid sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', justifyContent: 'center' }}>
        <Tooltip title={t('Copy to clipboard')} enterTouchDelay={0}>
          <Button
            size='small'
            variant='outlined'
            sx={{ textTransform: 'none' }}
            startIcon={<ContentCopy />}
            onClick={() => systemClient.copyToClipboard(txid)}
          >
            {t('Copy TXID')} {txid.slice(0, 8)}…{txid.slice(-6)}
          </Button>
        </Tooltip>
        <Button
          size='small'
          sx={{ textTransform: 'none' }}
          startIcon={<OpenInNew />}
          href={explorerTxUrl(txid)}
          target='_blank'
          rel='noreferrer'
        >
          {t('View on mempool.kilombino.com')}
        </Button>
      </Grid>
    </Grid>
  );
};

const decimalOf = (lang?: string): string =>
  lang && /^(es|ca|eu|fr|de|it|pt|pl|ru|cs)/.test(lang) ? ',' : '.';

/** rl31-beta19: what to pay from your own wallet, from the coordinator's node (hidden if unknown). */
export const FeeHintNote = ({ hint }: { hint?: FeeHint | null }): React.JSX.Element | null => {
  const { t, i18n } = useTranslation();
  const h = validHint(hint);
  if (h === null) return null;
  const d = decimalOf(i18n.language);
  return (
    <Alert severity='info' sx={{ textAlign: 'left' }}>
      {t(
        'Pay with at least {{next}} sat/vB to confirm in the next block (~{{hour}} sat/vB for ~1 hour).',
        { next: formatRate(h.next_block, d), hour: formatRate(h.hour, d) },
      )}
    </Alert>
  );
};

/** rl31-beta19: the payment seen in the mempool pays less than the mempool needs for ~1 hour. */
export const LowFeeWarning = ({
  rate,
  hint,
}: {
  rate?: number;
  hint?: FeeHint | null;
}): React.JSX.Element | null => {
  const { t, i18n } = useTranslation();
  const needed = lowFeeNeeded(rate, hint);
  if (needed === null || rate === undefined) return null;
  const d = decimalOf(i18n.language);
  return (
    <Alert severity='warning' sx={{ textAlign: 'left' }}>
      {t(
        'Your payment pays {{paid}} sat/vB and the mempool currently needs {{needed}}: speed it up (RBF or CPFP) or it may take a long time to confirm.',
        { paid: formatRate(rate, d), needed: formatRate(needed, d) },
      )}
    </Alert>
  );
};

/** 0.9.12 (#28): both chains use the same addresses; a payment sent from a Spamcoin (SHA-256)
 * wallet lands on the other chain and the coordinator never sees it. Shown with every QR and
 * address the robot has to pay to. */
export const WrongChainWarning = (): React.JSX.Element => {
  const { t } = useTranslation();
  return (
    <Alert severity='warning' data-testid='wrong-chain-warning' sx={{ textAlign: 'left' }}>
      {t(
        'Pay with BTC (BLAKE2b) only — Spamcoin (SHA-256) sent here is NOT seen by the coordinator',
      )}
    </Alert>
  );
};

const PayBox = ({
  address,
  sats,
  label,
  note,
  hint,
}: {
  address: string;
  sats: number;
  label: string;
  note?: string;
  hint?: FeeHint | null;
}): React.JSX.Element => {
  const { t } = useTranslation();
  const btc = (sats / 1e8).toFixed(8);
  const uri = `bitcoin:${address}?amount=${btc}`;
  return (
    <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
      <Grid>
        <Typography variant='body2' align='center'>
          {label}
        </Typography>
      </Grid>
      <Grid>
        <WrongChainWarning />
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
            sx={{ textTransform: 'none' }}
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
      <Grid sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', justifyContent: 'center' }}>
        <Tooltip title={t('Copy to clipboard')} enterTouchDelay={0}>
          <Button
            size='small'
            variant='outlined'
            startIcon={<ContentCopy />}
            onClick={() => systemClient.copyToClipboard(String(sats))}
          >
            {t('Copy amount (Sats)')}
          </Button>
        </Tooltip>
        <Tooltip title={t('Copy to clipboard')} enterTouchDelay={0}>
          <Button
            size='small'
            variant='outlined'
            startIcon={<ContentCopy />}
            onClick={() => systemClient.copyToClipboard(btc)}
          >
            {t('Copy amount (BTC)')}
          </Button>
        </Tooltip>
      </Grid>
      {validHint(hint) ? (
        <Grid>
          <FeeHintNote hint={hint} />
        </Grid>
      ) : null}
      {note ? (
        <Grid>
          <Alert severity='info' sx={{ textAlign: 'left' }}>
            {note}
          </Alert>
        </Grid>
      ) : null}
    </Grid>
  );
};

/** What the release pays to our own address (0 if we cannot tell). */
const releaseToMe = (data: EscrowOrderData): number => {
  try {
    if (!data.escrow_release_tx || !data.escrow_my_payout_address) return 0;
    const mine = bytesToHex(addressToScript(data.escrow_my_payout_address));
    return Transaction.parse(data.escrow_release_tx)
      .outputs.filter((o) => bytesToHex(o.script) === mine)
      .reduce((a, o) => a + Number(o.value), 0);
  } catch {
    return 0;
  }
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
  const { confirmAddress, addressDialog } = useAddressConfirm();

  const keys: EscrowKeys | null =
    token && data.escrow_alias ? escrowKeys(token, data.escrow_alias, order.id) : null;

  // Keep on this device what a rescue without the coordinator needs (public data only).
  useEffect(() => {
    if (!token || !data.escrow_alias) return;
    // Fair rescue: once both signed, the complete rescue goes with it (also on a device that
    // joins later: the coordinator still hands the signed parts out while it is up).
    let rescue: ReturnType<typeof completeRescue> | undefined;
    if (
      keys &&
      (data.escrow_rescue_plan ?? 1) >= 2 &&
      data.escrow_rescue_sigs?.seller &&
      data.escrow_rescue_sigs?.buyer
    ) {
      try {
        rescue = completeRescue(data, keys, data);
      } catch {
        rescue = undefined;
      }
    }
    void saveRescue(systemClient, token, data, order.shortAlias, rescue).catch(() => {});
  }, [
    token,
    data.escrow_rescue_plan,
    data.escrow_address,
    data.escrow_funding_txid,
    data.escrow_output_sats,
    data.escrow_funding_address,
    data.bond_address,
    data.bond_txid,
    data.escrow_rescue_sigs?.seller,
    data.escrow_rescue_sigs?.buyer,
  ]);

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
      once('bond_key', () => {
        // 0.9.7: the return address typed when making/taking (optional)
        if (!token) {
          onAction('bond_key', { bond_pubkey: pk.bond });
          return;
        }
        void systemClient.getItem(pendingReturnKey(token)).then((ret) => {
          onAction(
            'bond_key',
            ret ? { bond_pubkey: pk.bond, return_address: ret } : { bond_pubkey: pk.bond },
          );
          if (ret) systemClient.deleteItem(pendingReturnKey(token));
        });
      });
    // 0.9.7: pre-sign the automatic return of the bond, only to an address this app gave
    if (
      data.bond_auto_return_tx &&
      !data.bond_auto_return_signed &&
      [BOND.SIGN, BOND.LOCKED].includes(data.bond_status_code ?? -1)
    )
      once(`autoreturn-${data.bond_auto_return_tx}`, () => {
        const addr = data.bond_auto_return_address ?? '';
        void isMyAddress(addr).then((mine) => {
          if (!mine) return;
          try {
            const value = bondCoinsOf(data).reduce((a, u) => a + u.value, 0);
            const maxFee = Math.max(2_000, Math.floor(value * 0.1));
            const signatures = signBondAutoReturn(data, keys, addressToScript(addr), maxFee);
            // 0.9.11: also the cheaper rungs, so the coordinator sends the one the mempool needs
            const ladder = data.bond_auto_return_ladder?.length
              ? signBondAutoReturnLadder(data, keys, addressToScript(addr), maxFee)
              : undefined;
            onAction(
              'bond_auto_return_signature',
              ladder ? { signatures, ladder } : { signatures },
            );
          } catch (e) {
            setError(e instanceof EscrowCheckError ? t(e.message) : String(e));
          }
        });
      });
    if (data.bond_status_code === BOND.SIGN && data.bond_penalty_tx)
      once(`penalty-${data.bond_penalty_tx}`, () => {
        // one signature per coin (a topped-up bond has several, rl31-beta18)
        const signatures = signBondPenalties(data, keys);
        // 0.9.12: also the cheaper rungs, so a penalty never pays more mining than it needs
        let ladder: string[][] | undefined;
        try {
          ladder = data.bond_penalty_ladder?.length ? signBondPenaltyLadder(data, keys) : undefined;
        } catch (e) {
          ladder = undefined; // a ladder that does not check: the penalty alone, as before
        }
        onAction(
          'bond_penalty_signature',
          ladder
            ? { signature: signatures[0], signatures, ladder }
            : { signature: signatures[0], signatures },
        );
      });
    // 0.9.12: a bond locked before the ladder existed: its cheaper penalties, signed now
    if (
      data.bond_status_code === BOND.LOCKED &&
      data.bond_penalty_ladder?.length &&
      data.bond_penalty_ladder_signed === false
    )
      once(`penalty-ladder-${data.bond_penalty_ladder.join('')}`, () => {
        try {
          onAction('bond_penalty_ladder_signature', { ladder: signBondPenaltyLadder(data, keys) });
        } catch (e) {
          // nothing to do: the coordinator keeps the penalty signed before
        }
      });
    if (data.escrow_status_code === ESCROW.KEYS && data.escrow_my_keys_set === false)
      once('escrow_keys', () =>
        onAction('escrow_keys', {
          escrow_pubkey: pk.escrow,
          funding_pubkey: pk.funding,
          // 0.9.5: as seller, this app finishes a cooperative release alone (one signature)
          leaf_release: true,
          // 0.9.9: our deposit change may go to our payout address (we check it there)
          change_to_payout: true,
        }),
      );
    const fairRescue = (data.escrow_rescue_plan ?? 1) >= 2;
    if (
      fairRescue &&
      data.escrow_status_code === ESCROW.FUNDING &&
      data.escrow_rescue_tx &&
      !data.escrow_rescue_my_sig
    )
      // rl31-beta16: first the pre-signed rescue (each one gets its own after the timelock)
      once(`rescue-${data.escrow_rescue_tx}`, () =>
        onAction('escrow_rescue_signature', { signature: signRescue(data, keys, data) }),
      );
    const rescueReady =
      !fairRescue || Boolean(data.escrow_rescue_sigs?.seller && data.escrow_rescue_sigs?.buyer);
    if (
      data.escrow_status_code === ESCROW.FUNDING &&
      data.escrow_deposit_tx &&
      !data.escrow_deposit_signed &&
      rescueReady &&
      token
    )
      once(`deposit-${data.escrow_deposit_tx}`, () => {
        const deposit = signDeposit(data, keys, data, {
          maxSpendSats: depositSpendLimit(data),
          maxFeeRate: 100,
        });
        if (!fairRescue) {
          onAction('escrow_funding_signatures', deposit);
          return;
        }
        // Never sign the deposit before the complete rescue (both signatures, checked) is
        // saved on this device: without it our sats would depend on the coordinator.
        const rescue = completeRescue(data, keys, data);
        void saveRescue(systemClient, token, data, order.shortAlias, rescue)
          .then(() => {
            onAction('escrow_funding_signatures', deposit);
          })
          .catch((e) => {
            sent.current.delete(`deposit-${data.escrow_deposit_tx}`);
            setError(String(e));
          });
      });
    if (
      data.escrow_status_code === ESCROW.RELEASING &&
      data.escrow_release_tx &&
      data.escrow_my_payout_address
    ) {
      const myScript = addressToScript(data.escrow_my_payout_address);
      const isBuyer = data.escrow_role === 'buyer';
      const cooperative = (data.escrow_release_kind ?? '').startsWith('Seller and buyer');
      // A collaborative cancel returns the escrow: the seller gets the trade amount back and
      // the buyer only its bond (less its fee if the fee came out of it).
      const cancel = Boolean((data as { escrow_release_cancel?: boolean }).escrow_release_cancel);
      const minToMe = cooperative
        ? cancel
          ? isBuyer
            ? Math.floor((data.bond_value ?? 0) * 0.5)
            : Math.floor((data.escrow_sats ?? 0) * 0.98)
          : isBuyer
            ? Math.floor((data.escrow_sats ?? 0) * 0.98)
            : Math.floor((data.bond_value ?? 0) * 0.98)
        : Math.floor((data.escrow_sats ?? 0) * 0.98);
      // rl31-beta12: a cooperative release (trade or collaborative cancel) is checked to the sat.
      let owed: { due: number; paysFee: boolean } | undefined;
      let refused = false;
      try {
        owed = owedInRelease(data) ?? undefined;
      } catch (e) {
        refused = true;
        setError(e instanceof EscrowCheckError ? t(e.message) : String(e));
      }
      const check = { myScript, minToMe, owed };
      if (refused) {
        // never sign a release whose amounts do not add up
      } else if (cooperative && !isBuyer && data.escrow_release_leaf_ok) {
        // 0.9.5: one signature on our leaf with the coordinator finishes the release, also one
        // started with the two-round signing (any nonce we sent is simply not used).
        once(`coopleaf-${data.escrow_release_tx}`, () =>
          onAction('escrow_release_leaf', { signature: signCooperativeLeaf(data, keys, check) }),
        );
      } else if (cooperative) {
        // a new signing round when a trader had to send a new nonce (lost secret nonce)
        const round = (data as { escrow_release_round?: number }).escrow_release_round ?? 0;
        if (!data.escrow_release_my_nonce)
          once(`nonce-${data.escrow_release_tx}-${round}`, () => {
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
          once(`renonce-${data.escrow_release_tx}-${round}`, () => {
            const n = releaseNonce(data, keys, check);
            secnonce.current = n.secnonce;
            onAction('escrow_nonce', { nonce: n.pubnonce });
          });
      } else if (
        (data.escrow_release_kind ?? '').toLowerCase().startsWith(isBuyer ? 'buyer' : 'seller')
      ) {
        // A dispute release goes through the winner's leaf: only the winner signs it.
        once(`leaf-${data.escrow_release_tx}`, () =>
          onAction('escrow_leaf_signature', { signature: signDisputeRelease(data, keys, check) }),
        );
      }
    }
    if (data.escrow_sweep_tx && (data.escrow_leftover_sats ?? 0) > 0) {
      const target = address || data.escrow_my_payout_address;
      // A sweep built earlier for another address of ours: wait for a new one (no error).
      if (target && data.escrow_sweep_tx.includes(bytesToHex(addressToScript(target))))
        once(`sweep-${data.escrow_sweep_tx}`, () =>
          onAction('escrow_sweep_signatures', {
            signatures: signSweep(data, keys, addressToScript(target), 20_000),
          }),
        );
    }
    if (data.bond_status_code === BOND.RETURNABLE && data.bond_return_tx) {
      const target = address || data.escrow_my_payout_address;
      if (target && data.bond_return_tx.includes(bytesToHex(addressToScript(target))))
        once(`return-${data.bond_return_tx}`, () => {
          const signatures = signBondReturns(data, keys, addressToScript(target), 20_000);
          onAction('bond_return_signature', { signature: signatures[0], signatures });
        });
    }
  }, [
    order.status,
    data.bond_status_code,
    data.bond_penalty_tx,
    data.bond_penalty_ladder_signed,
    data.escrow_status_code,
    data.escrow_deposit_tx,
    data.escrow_rescue_tx,
    data.escrow_rescue_my_sig,
    data.escrow_rescue_sigs?.seller,
    data.escrow_rescue_sigs?.buyer,
    data.escrow_release_tx,
    data.escrow_release_aggnonce,
    data.escrow_release_my_nonce,
    data.escrow_release_my_partial,
    data.escrow_release_leaf_ok,
    data.bond_return_tx,
    data.escrow_sweep_tx,
    address,
  ]);

  // 0.9.10: fully checked for the coordinator's network and confirmed by the user first
  const submitAddress = (name: 'escrow_address' | 'bond_return' | 'escrow_sweep'): void => {
    const bondSats = (data.bond_value ?? 0) + (data.bond_extra_sats ?? 0);
    const leftover = data.escrow_leftover_sats ?? 0;
    const message =
      name === 'escrow_address'
        ? data.escrow_role === 'buyer'
          ? t('Your payout (and your bond back) will go to:')
          : t('Your bond back (and your refund if the trade is cancelled) will go to:')
        : name === 'bond_return'
          ? bondSats > 0
            ? t('Your bond of {{amount}} Sats will come back to:', { amount: pn(bondSats) })
            : t('Your bond will come back to:')
          : leftover > 0
            ? t('Your {{amount}} Sats will go back to:', { amount: pn(leftover) })
            : t('Your sats will go back to:');
    const problem = confirmAddress({
      address,
      hrp: data.escrow_hrp ?? data.bond_hrp ?? DEFAULT_HRP,
      message,
      onConfirm: (checked) => {
        setError(null);
        setAddress(checked);
        void rememberMyAddress(checked);
        if (name === 'escrow_address')
          onSignedAddress(checked, (signed) => onAction('escrow_address', {}, signed));
        else onAction(name, { address: checked });
      },
    });
    if (problem) setError(problem);
  };

  // 0.9.3: the app signs the onchain steps by itself, but only while this order is open in the
  // foreground. Which step is pending (or about to be, right after a payment confirms).
  const fundingConfirmed =
    order.status === 7 &&
    data.escrow_status_code === ESCROW.KEYS &&
    (data.escrow_funding_confirmed_sats ?? 0) > 0 &&
    (data.escrow_funding_confirmed_sats ?? 0) >= (data.escrow_funding_owed ?? Infinity);
  const signPhase: 'bond' | 'deposit' | 'release' | null =
    data.bond_status_code === BOND.SIGN && (order.status === 0 || order.status === 3)
      ? 'bond'
      : data.escrow_status_code === ESCROW.FUNDING || fundingConfirmed
        ? 'deposit'
        : data.escrow_status_code === ESCROW.RELEASING &&
            data.escrow_waiting_what === 'release_signature'
          ? 'release'
          : null;
  // 0.9.14-beta6: since when this screen sees the bond confirmed and waiting for our signature
  const bondSigning = data.bond_status_code === BOND.SIGN && (order.status === 0 || order.status === 3);
  const [signSince, setSignSince] = useState<number | null>(null);
  const [signNow, setSignNow] = useState<number>(Date.now());
  useEffect(() => {
    if (!bondSigning) {
      setSignSince(null);
      return;
    }
    setSignSince((v) => v ?? Date.now());
    const timer = setInterval(() => setSignNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, [bondSigning]);
  useEffect(() => {
    if (!signPhase) return;
    let lock: { release: () => Promise<void> } | null = null;
    window.AndroidAppRobosats?.keepScreenOn?.(true);
    const nav = navigator as unknown as {
      wakeLock?: { request: (k: 'screen') => Promise<{ release: () => Promise<void> }> };
    };
    nav.wakeLock
      ?.request('screen')
      .then((l) => {
        lock = l;
      })
      .catch(() => {});
    return () => {
      window.AndroidAppRobosats?.keepScreenOn?.(false);
      void lock?.release().catch(() => {});
    };
  }, [signPhase]);

  if (!keys) return <CircularProgress />;

  const isBuyer = data.escrow_role === 'buyer';
  const peer = isBuyer ? t('the seller') : t('the buyer');

  const waiting = (text: string, extra?: React.JSX.Element | null): React.JSX.Element => (
    <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
      <Grid>
        <Typography variant='body2' align='center'>
          {text}
        </Typography>
      </Grid>
      <Grid>
        <CircularProgress />
      </Grid>
      {extra ? <Grid>{extra}</Grid> : null}
    </Grid>
  );

  // Who the escrow is waiting for, said in words (the coordinator only when it really is).
  const waitingText = (): string => {
    const who = data.escrow_waiting_for;
    const what = data.escrow_waiting_what;
    if (who === 'peer') {
      if (what === 'keys') return t('Waiting for {{peer}} to join the onchain escrow…', { peer });
      if (what === 'address')
        return t('Waiting for {{peer}} to give their onchain address…', { peer });
      if (what === 'funding') return t('Waiting for the deposit of {{peer}}…', { peer });
      if (what === 'deposit_signature')
        return t('Waiting for {{peer}} to sign the deposit…', { peer });
      if (what === 'rescue_signature')
        return t('Waiting for {{peer}} to sign the rescue without the coordinator…', { peer });
      if (what === 'release_signature')
        return t('Waiting for {{peer}} to sign the release…', { peer });
    }
    if (who === 'confirmation') {
      if (what === 'funding') return t('Payment received, waiting for 1 confirmation…');
      return t('Deposit sent. Waiting for 1 confirmation…');
    }
    return t('Waiting for the coordinator…');
  };

  // 0.9.3: what the trader must do for the automatic signing to happen, and who is missing.
  const keepOpenNotice = (): React.JSX.Element | null => {
    if (!signPhase) return null;
    const who = data.escrow_waiting_for;
    const what = data.escrow_waiting_what ?? '';
    const missing =
      signPhase === 'bond'
        ? t('Still missing: the signature of your app.')
        : who === 'you' && what.endsWith('_signature')
          ? t('Still missing: the signature of your app.')
          : who === 'peer' && what.endsWith('_signature')
            ? t('Still missing: the signature of {{peer}}.', { peer })
            : null;
    const text =
      signPhase === 'bond'
        ? t('Keep this order open in the foreground until your bond is signed.')
        : signPhase === 'release'
          ? t('Keep this order open in the foreground until the release is signed.')
          : t('Keep this order open in the foreground until the deposit is signed.');
    return (
      <Alert severity='warning'>
        {text}
        {missing ? ` ${missing}` : ''}
      </Alert>
    );
  };

  // 0.9.8: while an onchain taker waits for its confirmation, a faster taker can still win
  const raceNotice = (): React.JSX.Element | null =>
    order.status === 3 ? (
      <Alert severity='warning' data-testid='take-race-notice'>
        {t(
          'Until your bond has 1 confirmation, another robot can still take this order first (a Lightning bond locks in seconds). If that happens you lose nothing: your bond comes back to you (by itself if you gave a return address, otherwise with "Get my bond back" on this order) and you are told. If your bond confirms first, the order is yours and you are notified right away.',
        )}
      </Alert>
    ) : null;

  const afterPayingNotice = (what: 'bond' | 'deposit'): React.JSX.Element => (
    <Alert severity='info'>
      {what === 'bond'
        ? t(
            'After paying, keep this order open on this device: your app signs your bond by itself.',
          )
        : t(
            'After paying, keep this order open on this device: your app signs the rescue and the deposit by itself.',
          )}
    </Alert>
  );

  const seenInMempool = (txid?: string, rate?: number): React.JSX.Element | null =>
    txid ? (
      <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
        <Grid>
          <Alert severity='success'>
            {t('📥 Payment seen in the mempool, waiting for 1 confirmation.')}
          </Alert>
        </Grid>
        <Grid>
          <TxLine label={t('Your payment')} txid={txid} />
        </Grid>
        {lowFeeNeeded(rate, data.onchain_fee_hint) !== null ? (
          <Grid>
            <LowFeeWarning rate={rate} hint={data.onchain_fee_hint} />
          </Grid>
        ) : null}
      </Grid>
    ) : null;

  const deadlineRaw =
    order.status === 3
      ? (order as unknown as { take_expires_at?: string }).take_expires_at
      : order.expires_at;
  const bondDeadline = deadlineRaw ? new Date(deadlineRaw) : null;
  let body: React.JSX.Element = waiting(waitingText());

  // rl31-beta18: the bond can come in more than one payment (up to bond_max_coins); what is
  // already confirmed at its address counts.
  const bondRequired = data.bond_required_sats ?? 0;
  const bondConfirmed = data.bond_confirmed_sats ?? 0;
  const bondPending = data.bond_mempool_sats ?? 0;
  const bondMissing = Math.max(0, bondRequired - bondConfirmed);
  const bondTopUps = (data.bond_max_coins ?? 0) > 1;
  if ((order.status === 0 || order.status === 3) && data.bond_status_code !== undefined) {
    if (
      data.bond_status_code === BOND.FUNDING &&
      data.bond_mempool_txid &&
      (bondPending >= bondRequired || (bondTopUps && bondPending + bondConfirmed >= bondRequired))
    )
      body = waiting(
        t('Bond seen in the mempool, waiting for 1 confirmation…'),
        <>
          <TxLine label={t('Your bond')} txid={data.bond_mempool_txid} />
          <LowFeeWarning rate={data.bond_mempool_fee_rate} hint={data.onchain_fee_hint} />
          {afterPayingNotice('bond')}
          {raceNotice()}
        </>,
      );
    else if (data.bond_status_code === BOND.FUNDING && data.bond_address)
      body = (
        <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
          <Grid>
            {bondTopUps && bondConfirmed > 0 ? (
              <Alert severity='warning' sx={{ mb: 1 }}>
                {t(
                  'You sent {{sent}} Sats, less than the bond ({{bond}}). Send the {{missing}} Sats missing to the same address and they add up (at least {{min}} Sats per payment). If you send more, the extra comes back to you.',
                  {
                    sent: pn(bondConfirmed),
                    bond: pn(bondRequired),
                    missing: pn(bondMissing),
                    min: pn(330),
                  },
                )}
              </Alert>
            ) : null}
            <PayBox
              hint={data.onchain_fee_hint}
              address={data.bond_address}
              sats={bondTopUps && bondConfirmed > 0 ? Math.max(bondMissing, 330) : bondRequired}
              label={t(
                'Send exactly this amount from any wallet. It is your bond: nobody can move it alone, it comes back to you after the trade (or after about {{days}} days in any case), and you lose it only if you cancel or do not complete your part in time.',
                { days: Math.max(1, Math.round(((data.bond_refund_csv ?? 432) * 10) / 1440)) },
              )}
              note={
                bondTopUps
                  ? t(
                      'Send exactly {{amount}} Sats. If you send less by mistake, send the rest to the same address (up to {{n}} payments in total): they add up. Anything that is not needed comes back to you.',
                      { amount: pn(bondRequired), n: data.bond_max_coins ?? 3 },
                    )
                  : t(
                      'Send exactly {{amount}} Sats in one payment. Less than that is not taken as your bond.',
                      { amount: pn(bondRequired) },
                    )
              }
            />
          </Grid>
          {bondDeadline ? (
            <Grid>
              <Alert severity='info'>
                {t(
                  order.status === 3
                    ? 'Send it before {{time}} or the order goes on without you.'
                    : 'Send it before {{time}} or the order expires.',
                  {
                    time: bondDeadline.toLocaleTimeString([], {
                      hour: '2-digit',
                      minute: '2-digit',
                    }),
                  },
                )}
              </Alert>
            </Grid>
          ) : null}
          <Grid>{afterPayingNotice('bond')}</Grid>
          {raceNotice() ? <Grid>{raceNotice()}</Grid> : null}
          {data.bond_mempool_txid ? (
            <Grid>
              <Alert severity='warning'>
                {bondTopUps
                  ? t(
                      'A payment of {{amount}} Sats is in the mempool. Together with what you already sent it does not reach the bond yet: {{missing}} Sats missing.',
                      {
                        amount: pn(bondPending),
                        missing: pn(Math.max(0, bondRequired - bondConfirmed - bondPending)),
                      },
                    )
                  : t(
                      'A payment of {{amount}} Sats is in the mempool, but it is less than the bond.',
                      {
                        amount: pn(data.bond_mempool_sats ?? 0),
                      },
                    )}
              </Alert>
            </Grid>
          ) : null}
          {data.bond_mempool_txid &&
          lowFeeNeeded(data.bond_mempool_fee_rate, data.onchain_fee_hint) !== null ? (
            <Grid>
              <LowFeeWarning rate={data.bond_mempool_fee_rate} hint={data.onchain_fee_hint} />
            </Grid>
          ) : null}
        </Grid>
      );
    else if (bondSigning) {
      // 0.9.14-beta6: confirmed; the app signs it now (an onchain take is won on signing)
      const lines = bondSigningLines(signSince, signNow);
      body = waiting(
        t(lines.main),
        <>
          {data.bond_txid ? <TxLine label={t('Your bond')} txid={data.bond_txid} /> : null}
          {lines.slow ? (
            <Alert severity='warning' sx={{ mt: 1 }}>
              {t(lines.slow)}
            </Alert>
          ) : null}
        </>,
      );
    } else
      body = waiting(
        t('Bond received, waiting for 1 confirmation…'),
        <>
          {data.bond_txid ? <TxLine label={t('Your bond')} txid={data.bond_txid} /> : null}
          {afterPayingNotice('bond')}
        </>,
      );
  } else if (
    order.status === 6 &&
    data.escrow_status_code === ESCROW.KEYS &&
    !data.escrow_my_payout_address
  ) {
    body = (
      <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
        <Grid>
          <Typography variant='body2' align='center'>
            {isBuyer
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
            helperText={t('Accepted: 1…, 3…, bc1q… or bc1p…')}
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
    isBuyer &&
    data.escrow_fee_at_release
  ) {
    body = waiting(
      waitingText(),
      <Alert severity='info'>
        {t(feeAtReleaseKey(Boolean(order.is_maker)), {
          fee: pn(data.escrow_fee_at_release_sats ?? 0),
        })}
      </Alert>,
    );
  } else if (
    order.status === 7 &&
    data.escrow_status_code === ESCROW.KEYS &&
    isBuyer &&
    data.escrow_fee_from_bond
  ) {
    body = waiting(
      waitingText(),
      <Alert severity='info'>
        {t(
          'You do not need to send anything else: your fee ({{fee}} Sats) and your part of the mining fee come out of your bond, and the rest of the bond comes back to you with the trade.',
          { fee: pn(data.escrow_funding_owed ?? 0) },
        )}
      </Alert>,
    );
  } else if (
    order.status === 7 &&
    data.escrow_status_code === ESCROW.KEYS &&
    data.escrow_funding_address
  ) {
    const seen = data.escrow_funding_mempool_txid;
    const enough = (data.escrow_funding_mempool_sats ?? 0) >= (data.escrow_funding_owed ?? 0);
    try {
      if (!fundingConfirmed) checkFundingAddress(data, keys);
      body = fundingConfirmed ? (
        waiting(t('✅ Payment confirmed. Building the deposit… Your app will sign it by itself.'))
      ) : seen && enough ? (
        waiting(
          waitingText(),
          <>
            {seenInMempool(seen, data.escrow_funding_mempool_fee_rate)}
            {afterPayingNotice('deposit')}
          </>,
        )
      ) : (
        <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
          <Grid>
            <PayBox
              hint={data.onchain_fee_hint}
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
              note={
                data.escrow_change_to_payout && data.escrow_my_payout_address
                  ? t(
                      'Pay {{amount}} Sats. Anything left over comes back by itself to your address {{address}} in the deposit (a few sats below the dust limit go to the miners). If you send less, send the rest to the same address before the order expires.',
                      {
                        amount: pn(data.escrow_funding_recommended ?? 0),
                        address: data.escrow_my_payout_address,
                      },
                    )
                  : t(
                      'Send exactly {{amount}} Sats. If you send more, the change comes back to you in the deposit; if you send less, send the rest to the same address before the order expires.',
                      { amount: pn(data.escrow_funding_recommended ?? 0) },
                    )
              }
            />
          </Grid>
          <Grid>{afterPayingNotice('deposit')}</Grid>
          {seen ? (
            <Grid>
              <Alert severity='warning'>
                {t(
                  'Seen in the mempool: {{amount}} Sats. It is not enough yet: send the rest to the same address.',
                  { amount: pn(data.escrow_funding_mempool_sats ?? 0) },
                )}
              </Alert>
            </Grid>
          ) : null}
          {seen &&
          lowFeeNeeded(data.escrow_funding_mempool_fee_rate, data.onchain_fee_hint) !== null ? (
            <Grid>
              <LowFeeWarning
                rate={data.escrow_funding_mempool_fee_rate}
                hint={data.onchain_fee_hint}
              />
            </Grid>
          ) : null}
        </Grid>
      );
    } catch (e) {
      body = <Alert severity='error'>{String(e)}</Alert>;
    }
  } else if (data.escrow_status_code === ESCROW.FUNDING) {
    body = waiting(
      data.escrow_deposit_signed
        ? t(
            '✍️ Your app checked the deposit (the trade amount, the escrow contract and your change) and signed it automatically. Waiting for {{peer}} to sign…',
            { peer },
          )
        : t('Checking and signing the deposit…'),
    );
  } else if (data.escrow_status_code === ESCROW.MEMPOOL) {
    body = waiting(
      t(
        '✍️ Your app checked and signed the deposit automatically. Deposit sent: waiting for 1 confirmation…',
      ),
      data.escrow_funding_txid ? (
        <TxLine label={t('Escrow deposit')} txid={data.escrow_funding_txid} />
      ) : null,
    );
  } else if (data.escrow_status_code === ESCROW.RELEASING) {
    const toMe = releaseToMe(data);
    // Only say "signed" when our signature was really sent: in the two-round signing the first
    // round is just a nonce, and our app has to sign once more after the peer's nonce (#24).
    body = waiting(
      data.escrow_release_leaf_ok
        ? t('✍️ Your app is checking the release (you receive {{amount}} Sats) and signing it…', {
            amount: pn(toMe),
          })
        : data.escrow_release_my_partial
          ? t(
              '✍️ Your app checked the release (you receive {{amount}} Sats) and signed it automatically. Waiting for {{peer}}…',
              { amount: pn(toMe), peer },
            )
          : data.escrow_release_my_nonce && data.escrow_waiting_for === 'peer'
            ? t(
                '✍️ Your app checked the release (you receive {{amount}} Sats) and sent its part. When {{peer}} signs, open this order again so your app can sign it too.',
                { amount: pn(toMe), peer },
              )
            : data.escrow_waiting_for === 'peer'
              ? t('Waiting for {{peer}} to sign the release…', { peer })
              : t('Signing the release together with your peer…'),
    );
  } else if (data.escrow_release_txid && order.status === 13) {
    body = waiting(
      t('Release sent. Finishing the order…'),
      <TxLine label={t('Escrow release')} txid={data.escrow_release_txid} />,
    );
  } else if (data.bond_status_code === BOND.RETURNABLE) {
    body = (
      <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
        <Grid>
          <Typography variant='body2' align='center'>
            {t('Your bond is free. Get it back to an address of yours.')}
          </Typography>
          {(data.bond_extra_sats ?? 0) > 0 ? (
            <Typography variant='caption' align='center' sx={{ display: 'block' }}>
              {t(
                'The other {{amount}} Sats at your bond address that were not the bond go back with it.',
                { amount: pn(data.bond_extra_sats ?? 0) },
              )}
            </Typography>
          ) : null}
        </Grid>
        <Grid>
          <TextField
            size='small'
            fullWidth
            value={address}
            onChange={(e) => setAddress(e.target.value.trim())}
            label={t('Bitcoin address')}
            helperText={t('Accepted: 1…, 3…, bc1q… or bc1p…')}
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
  } else if (
    (data.escrow_leftover_sats ?? 0) > 0 &&
    ![9, 10, 11, 13].includes(order.status) &&
    data.escrow_leftover_worth === false
  ) {
    // rl31-beta20: not worth sending back now (the mining fee would eat more than the sats)
    body = (
      <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
        <Grid>
          <Alert severity='info'>
            {t(
              'Not worth it right now: the mining fee (~{{fee}} sats) would eat more than the {{amount}} sats',
              {
                fee: pn(data.escrow_leftover_fee_sats ?? 0),
                amount: pn(data.escrow_leftover_sats ?? 0),
              },
            )}
          </Alert>
        </Grid>
        {data.escrow_sweep_txid ? (
          <Grid sx={{ width: '100%' }}>
            <RecoveredLine
              sats={data.escrow_sweep_sats ?? 0}
              txid={data.escrow_sweep_txid}
              confirmed={data.escrow_sweep_status === 'confirmed'}
            />
          </Grid>
        ) : null}
      </Grid>
    );
  } else if ((data.escrow_leftover_sats ?? 0) > 0 && ![9, 10, 11, 13].includes(order.status)) {
    body = (
      <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
        <Grid>
          <Typography variant='body2' align='center'>
            {t(
              'You have {{amount}} Sats left on your funding address that the deposit did not take. They are yours: get them back to an address of yours.',
              { amount: pn(data.escrow_leftover_sats ?? 0) },
            )}
          </Typography>
        </Grid>
        <Grid>
          <TextField
            size='small'
            fullWidth
            value={address}
            onChange={(e) => setAddress(e.target.value.trim())}
            label={t('Bitcoin address')}
            helperText={t('Accepted: 1…, 3…, bc1q… or bc1p…')}
          />
        </Grid>
        <Grid>
          <Button
            variant='contained'
            disabled={!address}
            onClick={() => submitAddress('escrow_sweep')}
          >
            {t('Get my sats back')}
          </Button>
        </Grid>
      </Grid>
    );
  } else if (data.escrow_sweep_txid && data.bond_status_code !== BOND.RETURNABLE) {
    body = (
      <RecoveredLine
        sats={data.escrow_sweep_sats ?? 0}
        txid={data.escrow_sweep_txid}
        confirmed={data.escrow_sweep_status === 'confirmed'}
      />
    );
  }

  // 0.9.14-beta7 (#38): the other trader's funding payment, once it is in the mempool
  const peerSeen =
    data.escrow_status_code === ESCROW.KEYS ? peerFundingSeen(data, isBuyer, order.status) : null;

  return (
    <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center', padding: 1 }}>
      <Grid>{body}</Grid>
      {peerSeen ? (
        <Grid>
          <Alert severity='info' sx={{ mb: 0.5 }}>
            {t(peerSeen.key)}
          </Alert>
          {peerSeen.txids.map((txid) => (
            <TxLine key={txid} label={t("Peer's payment")} txid={txid} />
          ))}
        </Grid>
      ) : null}
      {signPhase ? <Grid>{keepOpenNotice()}</Grid> : null}
      {myExtraInEscrow(data) > 0 && data.escrow_deposit_txid ? (
        <Grid>
          <Typography variant='caption' align='center' sx={{ display: 'block' }}>
            {t('+{{amount}} sats extra returned with your bond', {
              amount: pn(myExtraInEscrow(data)),
            })}
          </Typography>
        </Grid>
      ) : null}
      {/* 0.9.7: the bond goes back by itself once it is free */}
      {data.bond_auto_return_signed &&
      [BOND.SIGN, BOND.LOCKED].includes(data.bond_status_code ?? -1) &&
      data.bond_auto_return_address ? (
        <Grid>
          <Typography
            variant='caption'
            align='center'
            color='text.secondary'
            sx={{ display: 'block', wordBreak: 'break-all' }}
            data-testid='bond-auto-return'
          >
            {t('When your bond is free, it goes back by itself to {{address}}.', {
              address: data.bond_auto_return_address,
            })}
          </Typography>
        </Grid>
      ) : null}
      {error ? (
        <Grid>
          <Alert severity='error'>{error}</Alert>
        </Grid>
      ) : null}
      {addressDialog}
    </Grid>
  );
};

/** 0.9.8: this robot's onchain take of the order is over (another taker locked first, or its
 * take expired). The coordinator shows only its bond: follow it and take it back. */
export const LostTakePrompt = (props: Props): React.JSX.Element => {
  const { t } = useTranslation();
  const data = props.order as unknown as EscrowOrderData & {
    lost_take_taken?: boolean;
    bond_spend_txid?: string;
  };
  const code = data.bond_status_code ?? -1;
  let detail: React.JSX.Element;
  if (code === BOND.RETURNABLE) {
    detail = <OnchainEscrowPrompt {...props} />;
  } else if (code === BOND.RETURNED) {
    detail = (
      <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
        <Grid>
          <Typography variant='body2' align='center'>
            {t('Your bond went back to you.')}
          </Typography>
        </Grid>
        {data.bond_spend_txid ? (
          <Grid>
            <TxLine label={t('Bond return')} txid={data.bond_spend_txid} />
          </Grid>
        ) : null}
      </Grid>
    );
  } else if (data.bond_mempool_txid || code === BOND.SIGN || data.bond_txid) {
    detail = (
      <Grid container spacing={1} sx={{ flexDirection: 'column', alignItems: 'center' }}>
        <Grid>
          <Typography variant='body2' align='center'>
            {t(
              'Your bond payment is waiting for 1 confirmation. Then you can get it back here: keep this order open.',
            )}
          </Typography>
        </Grid>
        <Grid>
          <CircularProgress />
        </Grid>
        {data.bond_mempool_txid ? (
          <Grid>
            <TxLine label={t('Your bond')} txid={data.bond_mempool_txid} />
          </Grid>
        ) : null}
      </Grid>
    );
  } else {
    detail = (
      <Typography variant='body2' align='center'>
        {t('You had not sent your bond, so you lose nothing. Do not send it now.')}
      </Typography>
    );
  }
  return (
    <Grid
      container
      spacing={1}
      sx={{ flexDirection: 'column', alignItems: 'center', padding: 1 }}
      data-testid='lost-take'
    >
      <Grid>
        <Alert severity='info'>
          {data.lost_take_taken
            ? t(
                'Another robot took this order before you: its bond locked first (a Lightning bond locks in seconds, an onchain bond needs a confirmation).',
              )
            : t('Your take of this order is over.')}{' '}
          {t('Your bond was never at stake: it is not penalized.')}
        </Alert>
      </Grid>
      <Grid>{detail}</Grid>
    </Grid>
  );
};

export default OnchainEscrowPrompt;
