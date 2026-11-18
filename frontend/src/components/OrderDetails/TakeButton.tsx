import React, { useState, useMemo, useEffect, useContext } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Box,
  Tooltip,
  Grid,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  useTheme,
  Typography,
  FormHelperText,
} from '@mui/material';

import Countdown from 'react-countdown';

import currencies from '../../utils/currencies';

import { type Order, type Info } from '../../models';
import { ConfirmationDialog, OrderDescriptionDialog, UsedRobotDialog } from '../Dialogs';
import { LoadingButton } from '@mui/lab';
import { computeSats, pn } from '../../utils';
import { onchainFees } from '../../utils/escrow/fees';
import { GarageContext, type UseGarageStoreType } from '../../contexts/GarageContext';
import { type UseFederationStoreType, FederationContext } from '../../contexts/FederationContext';
import { useNavigate } from 'react-router-dom';
import { sha256 } from 'js-sha256';
import { UseAppStoreType, AppContext } from '../../contexts/AppContext';
import useLegacyMode from '../../hooks/useLegacyMode';
import { SAT_LABEL, formatTotal } from '../../utils/satOrders';

interface TakeButtonProps {
  currentOrder: Order;
  password?: string;
  info?: Info;
  setCurrentOrder: (currentOrder: Order) => void;
  onClickGenerateRobot?: () => Promise<void>;
}

interface OpenDialogsProps {
  description: boolean;
  confirmation: boolean;
}
const closeAll = { description: false, confirmation: false };

const TakeButton = ({
  password,
  currentOrder,
  setCurrentOrder,
  info,
  onClickGenerateRobot = async () => {},
}: TakeButtonProps): React.JSX.Element => {
  const { t } = useTranslation();
  const theme = useTheme();
  const navigate = useNavigate();
  const { navigateToPage } = useContext<UseAppStoreType>(AppContext);
  const { garage } = useContext<UseGarageStoreType>(GarageContext);
  const { federation } = useContext<UseFederationStoreType>(FederationContext);
  const { slotUpdatedAt } = useContext<UseAppStoreType>(AppContext);
  const { isLegacyMode, legacyDisabledTooltip } = useLegacyMode();

  const [takeAmount, setTakeAmount] = useState<string>('');
  const [badRequest, setBadRequest] = useState<string>('');
  const [loadingTake, setLoadingTake] = useState<boolean>(false);
  const [open, setOpen] = useState<OpenDialogsProps>(closeAll);
  const [satoshis, setSatoshis] = useState<string>('');
  const [openUsedRobotDialog, setOpenUsedRobotDialog] = useState<boolean>(false);
  const [usedRobotHasActiveOrder, setUsedRobotHasActiveOrder] = useState<boolean>(false);
  const [changingRobot, setChangingRobot] = useState<boolean>(false);
  // Roblosats hybrid: an onchain order may be taken with Lightning instead.
  const isOnchainOrder =
    (currentOrder as unknown as { escrow_type?: number | string })?.escrow_type === 1 ||
    (currentOrder as unknown as { escrow_type?: number | string })?.escrow_type === 'onchain';
  const [takeWith, setTakeWith] = useState<'onchain' | 'lightning'>('onchain');
  // The maker's order type: 0 = the maker buys (the taker sells), 1 = the maker sells.
  const takerSells = currentOrder?.type === 0;
  // rl31-beta19: a "丰 sat" order is taken in sats
  const satOrder = Boolean(currentOrder?.sats_denominated);
  const toOrderUnits = (v: string): number =>
    satOrder ? Number(v) : currentOrder?.currency === 1000 ? Number(v) / 100000000 : Number(v);
  const rangeMin = satOrder
    ? Number(currentOrder?.sats_min_amount)
    : Number(currentOrder?.min_amount);
  const rangeMax = satOrder
    ? Number(currentOrder?.sats_max_amount)
    : Number(currentOrder?.max_amount);

  // rl31-beta14: what this robot will pay the coordinator, for the way it takes the order
  // (onchain: at least the coordinator's minimum; Lightning seller: a Lightning order, plain %).
  const takeFeeText = (): string => {
    if (!isOnchainOrder || !currentOrder || !info) return '';
    let tradeSats = currentOrder.satoshis_now;
    if (satOrder && currentOrder.has_range && Number(takeAmount) > 0) {
      tradeSats = Math.round(Number(takeAmount));
    } else if (currentOrder.has_range && Number(takeAmount) > 0 && currentOrder.max_amount > 0) {
      tradeSats = Math.round((tradeSats * Number(takeAmount)) / Number(currentOrder.max_amount));
    }
    if (!tradeSats) return '';
    // /api/info has it when onchain is open to everybody, /api/robot/ for an allowed robot
    const minFee =
      info.onchain_min_fee_sats ??
      garage.getSlot()?.getRobot(currentOrder.shortAlias)?.onchainEscrow?.minFeeSats ??
      0;
    const fees = onchainFees({
      tradeSats,
      makerFeeRate: info.maker_fee ?? 0,
      takerFeeRate: info.taker_fee ?? 0,
      makerIsSeller: currentOrder.type === 1,
      minFeeSats: minFee,
      takerLightning: takeWith === 'lightning',
    });
    const fee = pn(fees.takerSats);
    if (takeWith === 'lightning' && takerSells) {
      return t('Your coordinator fee: {{fee}} Sats', { fee });
    }
    return minFee > 0
      ? t(
          'Your onchain coordinator fee: {{fee}} Sats (the order pays at least {{min}} Sats in total, shared by both traders)',
          {
            fee,
            min: pn(minFee),
          },
        )
      : t('Your onchain coordinator fee: {{fee}} Sats', { fee });
  };

  const takeWithChoice = (): React.JSX.Element | null => {
    if (!isOnchainOrder) return null;
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, mb: 1 }}>
        <Typography variant='body2'>{t('Take this onchain order with')}</Typography>
        <ToggleButtonGroup
          size='small'
          exclusive
          value={takeWith}
          onChange={(_e, v: 'onchain' | 'lightning' | null) => {
            if (v) setTakeWith(v);
          }}
        >
          <ToggleButton value='onchain'>{t('⛓️ Onchain')}</ToggleButton>
          <ToggleButton value='lightning'>{t('⚡ Lightning')}</ToggleButton>
        </ToggleButtonGroup>
        {takeFeeText() ? (
          <Typography variant='caption' color='text.secondary' align='center'>
            {takeFeeText()}
          </Typography>
        ) : null}
        {takeWith === 'lightning' ? (
          <Alert severity='warning' sx={{ maxWidth: '30em' }}>
            {takerSells
              ? t(
                  'With Lightning your bond and the trade escrow are Lightning hold invoices. The buyer asked for onchain: the coordinator pays it onchain with its swap (the buyer pays that swap fee and the mining fee).',
                )
              : t(
                  'With Lightning your bond is a Lightning hold invoice and you do not need to send anything onchain. The seller still locks the sats onchain and you receive them onchain; your coordinator fee comes out of what you receive.',
                )}
          </Alert>
        ) : null}
      </Box>
    );
  };

  const satoshisNow = (): string | undefined => {
    if (currentOrder === null) return;

    const tradeFee = info?.taker_fee ?? 0;
    const defaultRoutingBudget = 0.001;
    const btcNow = currentOrder.satoshis_now / 100000000;
    const rate =
      currentOrder.amount != null ? currentOrder.amount / btcNow : currentOrder.max_amount / btcNow;
    const amount = satOrder
      ? (Number(takeAmount) / 100000000) * rate
      : currentOrder.currency === 1000
        ? Number(takeAmount) / 100000000
        : Number(takeAmount);
    const satoshis = computeSats({
      amount,
      routingBudget: currentOrder.is_buyer ? defaultRoutingBudget : 0,
      fee: tradeFee,
      rate,
    });
    return satoshis;
  };

  useEffect(() => {
    setSatoshis(satoshisNow() ?? '');
  }, [slotUpdatedAt, takeAmount, info]);

  const currencyCode: string = satOrder
    ? SAT_LABEL
    : currentOrder?.currency === 1000
      ? 'Sats'
      : currencies[`${Number(currentOrder?.currency)}`];

  interface countdownTakeOrderRendererProps {
    seconds: number;
    completed: boolean;
  }

  const countdownTakeOrderRenderer = function ({
    seconds,
    completed,
  }: countdownTakeOrderRendererProps): React.JSX.Element {
    if (isNaN(seconds) || completed) {
      return takeOrderButton();
    } else {
      return (
        <Tooltip enterTouchDelay={0} title={t('Wait until you can take an order')}>
          <Grid container sx={{ width: '100%', justifyContent: 'center', padding: 1 }}>
            <LoadingButton
              loading={loadingTake}
              disabled={true}
              variant='outlined'
              color='primary'
              size='large'
            >
              {t('Take Order')}
            </LoadingButton>
          </Grid>
        </Tooltip>
      );
    }
  };

  const handleTakeAmountChange = function (e: React.ChangeEvent<HTMLInputElement>): void {
    if (e.target.value !== '' && e.target.value != null) {
      setTakeAmount(`${parseFloat(e.target.value)}`);
    } else {
      setTakeAmount(e.target.value);
    }
  };

  const amountHelperText = useMemo(() => {
    if (currentOrder === null) return;

    const amount = toOrderUnits(takeAmount);
    if (amount < rangeMin && takeAmount !== '') {
      return t('Too low');
    } else if (amount > rangeMax && takeAmount !== '') {
      return t('Too high');
    } else if (satOrder && takeAmount !== '' && currentOrder.satoshis_now > 0) {
      // what is paid in exchange for these sats (the price per sat is the same along the range)
      const max = Number(currentOrder.max_amount);
      const total = rangeMax > 0 ? (max * amount) / rangeMax : 0;
      return total > 0 ? `↔ ${formatTotal(total, currentOrder.currency)}` : null;
    } else {
      return null;
    }
  }, [slotUpdatedAt, takeAmount]);

  const proceedToTakeFlow = function (): void {
    if (currentOrder?.description && currentOrder?.description !== '') {
      setOpen({ description: true, confirmation: false });
    } else {
      setOpen({ description: false, confirmation: true });
    }
  };

  const onTakeOrderClicked = function (): void {
    const slot = garage.getSlot();
    if (slot && !slot.isReusable()) {
      setUsedRobotHasActiveOrder(Boolean(slot.activeOrder));
      setOpenUsedRobotDialog(true);
      return;
    }
    proceedToTakeFlow();
  };

  const handleChangeRobotAndTake = (): void => {
    setChangingRobot(true);
    const currentIndex = garage.getCurrentAccountIndex();
    void garage
      .findNextUnusedAccount(federation, currentIndex + 1)
      .then(async (nextIndex) => {
        await garage.setAccountIndex(federation, nextIndex);
        garage.publishAccountRecovery(federation, nextIndex);
      })
      .then(() => {
        setOpenUsedRobotDialog(false);
        takeOrder();
      })
      .catch(() => {
        setBadRequest('Could not switch to a new robot. Please try again.');
        setOpenUsedRobotDialog(false);
      })
      .finally(() => {
        setChangingRobot(false);
      });
  };

  const invalidTakeAmount = useMemo(() => {
    const amount = toOrderUnits(takeAmount);
    return amount < rangeMin || amount > rangeMax || takeAmount === '' || takeAmount == null;
  }, [takeAmount, slotUpdatedAt]);

  const takeOrderButton = function (): React.JSX.Element {
    if (currentOrder?.has_range) {
      return (
        <Box
          sx={{
            padding: '0.5em',
            backgroundColor: 'background.paper',
            border: '1px solid',
            borderRadius: '4px',
            borderColor: theme.palette.mode === 'dark' ? '#434343' : '#c4c4c4',
            '&:hover': {
              borderColor: theme.palette.mode === 'dark' ? '#ffffff' : '#2f2f2f',
            },
          }}
        >
          <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
            <Box
              sx={{
                display: 'flex',
                flexDirection: 'row',
                alignItems: 'flex-start',
                justifyContent: 'space-evenly',
                width: '100%',
              }}
            >
              <Tooltip
                placement='top'
                enterTouchDelay={500}
                enterDelay={700}
                enterNextDelay={2000}
                title={t('Enter amount of fiat to exchange for bitcoin')}
                sx={{ flex: 1 }}
              >
                <TextField
                  error={takeAmount === '' ? false : invalidTakeAmount}
                  helperText={amountHelperText}
                  label={t('Amount {{currencyCode}}', { currencyCode })}
                  size='small'
                  type='number'
                  required={true}
                  value={takeAmount}
                  sx={{ flex: 1 }}
                  slotProps={{
                    htmlInput: {
                      min: satOrder ? rangeMin : currentOrder?.min_amount,
                      max: satOrder ? rangeMax : currentOrder?.max_amount,
                      style: { textAlign: 'center' },
                    },
                  }}
                  onChange={handleTakeAmountChange}
                />
              </Tooltip>
              <div
                style={{
                  display: invalidTakeAmount ? '' : 'none',
                  flex: 1,
                }}
              >
                <Tooltip
                  placement='top'
                  enterTouchDelay={0}
                  enterDelay={500}
                  enterNextDelay={1200}
                  title={t('You must specify an amount first')}
                >
                  <div style={{ width: '100%' }}>
                    <LoadingButton
                      loading={loadingTake}
                      sx={{ height: '2.8em', width: '100%', whiteSpace: 'nowrap' }}
                      variant='outlined'
                      color='primary'
                      disabled={true}
                    >
                      {t('Take Order')}
                    </LoadingButton>
                  </div>
                </Tooltip>
              </div>
              <div
                style={{
                  display: invalidTakeAmount ? 'none' : '',
                  flex: 1,
                }}
              >
                <Tooltip title={isLegacyMode ? legacyDisabledTooltip : ''} placement='top'>
                  <span>
                    <LoadingButton
                      loading={loadingTake}
                      sx={{ height: '2.8em', width: '100%', whiteSpace: 'nowrap' }}
                      variant='outlined'
                      color='primary'
                      disabled={isLegacyMode}
                      onClick={onTakeOrderClicked}
                    >
                      {t('Take Order')}
                    </LoadingButton>
                  </span>
                </Tooltip>
              </div>
            </Box>
            {satoshis !== '0' && satoshis !== '' && !invalidTakeAmount ? (
              <FormHelperText sx={{ position: 'relative', top: '0.15em' }}>
                {currentOrder?.type === 1
                  ? t('You will receive {{satoshis}} Sats (Approx)', { satoshis })
                  : t('You will send {{satoshis}} Sats (Approx)', { satoshis })}
              </FormHelperText>
            ) : null}
          </Box>
        </Box>
      );
    } else {
      return (
        <Box
          style={{
            display: 'flex',
            justifyContent: 'center',
            position: 'relative',
            bottom: '0.25em',
          }}
        >
          <Tooltip title={isLegacyMode ? legacyDisabledTooltip : ''} placement='top'>
            <span>
              <LoadingButton
                loading={loadingTake}
                sx={{ height: '2.71em' }}
                variant='outlined'
                color='primary'
                size='large'
                disabled={isLegacyMode}
                onClick={onTakeOrderClicked}
              >
                {t('Take Order')}
              </LoadingButton>
            </span>
          </Tooltip>
        </Box>
      );
    }
  };

  const takeOrder = function (): void {
    const slot = garage.getSlot();

    if (currentOrder === null || slot === null) return;

    if (password) currentOrder.password = sha256(password);

    setLoadingTake(true);

    slot
      .takeOrder(federation, currentOrder, takeAmount, isOnchainOrder && takeWith === 'lightning')
      .then((order) => {
        if (order?.bad_request !== undefined) {
          setBadRequest(order.bad_request);
        } else {
          setBadRequest('');
          setCurrentOrder(order);
          navigateToPage(`order/${order.shortAlias}/${order.id}`, navigate);
        }
        setLoadingTake(false);
      })
      .catch(() => {
        setBadRequest('Request error');
      });
  };

  return (
    <Box>
      {takeWithChoice()}
      <Countdown
        date={new Date(currentOrder?.penalty ?? '')}
        renderer={countdownTakeOrderRenderer}
      />
      {badRequest !== '' ? (
        <Box style={{ padding: '0.5em' }}>
          <Typography align='center' color='secondary'>
            {t(badRequest)}
          </Typography>
        </Box>
      ) : (
        <></>
      )}

      <OrderDescriptionDialog
        open={open.description}
        onClose={() => setOpen({ ...open, description: false })}
        onClickBack={() => setOpen({ ...open, description: false })}
        onClickDone={() => setOpen({ ...open, description: false, confirmation: true })}
        order={currentOrder}
      />

      <ConfirmationDialog
        open={open.confirmation}
        onClose={() => {
          setOpen({ ...open, confirmation: false });
        }}
        onClickDone={() => {
          takeOrder();
          setLoadingTake(true);
          setOpen(closeAll);
        }}
        hasRobot={Boolean(garage.getSlot()?.hashId)}
        onClickGenerateRobot={onClickGenerateRobot}
      />

      <UsedRobotDialog
        open={openUsedRobotDialog}
        action='take'
        hasActiveOrder={usedRobotHasActiveOrder}
        isRangeOrder={Boolean(currentOrder?.has_range)}
        isLegacyMode={isLegacyMode}
        onClose={() => setOpenUsedRobotDialog(false)}
        onChangeRobot={handleChangeRobotAndTake}
        loading={changingRobot}
      />
    </Box>
  );
};

export default TakeButton;
