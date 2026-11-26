import React, { useContext, useEffect, useMemo, useRef, useState } from 'react';
import { pendingReturnKey, rememberMyAddress } from '../../utils/myAddresses';
import { systemClient } from '../../services/System';
import { addressErrorText, checkAddress, hrpOfNetwork } from '../../utils/address';
import { useAddressConfirm } from '../Dialogs/AddressConfirm';
import { useTranslation } from 'react-i18next';
import {
  ButtonGroup,
  Slider,
  Switch,
  Tooltip,
  Button,
  Grid,
  Typography,
  TextField,
  Select,
  FormHelperText,
  MenuItem,
  FormControl,
  FormControlLabel,
  InputAdornment,
  InputLabel,
  Box,
  useTheme,
  Collapse,
  IconButton,
  Skeleton,
  CircularProgress,
  ToggleButton,
  ToggleButtonGroup,
} from '@mui/material';

import { type LimitList, defaultMaker, type Order } from '../../models';

import { LocalizationProvider, MobileTimePicker } from '@mui/x-date-pickers';
import { AdapterDateFns } from '@mui/x-date-pickers/AdapterDateFns';
import { StoreTokenDialog, NoRobotDialog, F2fMapDialog, UsedRobotDialog } from '../Dialogs';

import { FlagWithProps } from '../Icons';
import AutocompletePayments from './AutocompletePayments';
import AmountRange from './AmountRange';
import { cleanNumberInput, numberFromInput } from '../../utils/numberInput';
import currencyDict from '../../utils/currencies';
import { amountToString, pn } from '../../utils';
import { onchainFees } from '../../utils/escrow/fees';
import { useBondEstimate } from '../../hooks/useBondEstimate';
import useLegacyMode from '../../hooks/useLegacyMode';

import { SelfImprovement, Lock, DeleteSweep, Edit, Map } from '@mui/icons-material';
import DashboardCustomizeIcon from '@mui/icons-material/DashboardCustomize';
import { LoadingButton } from '@mui/lab';
import { fiatMethods } from '../PaymentMethods';
import { AppContext, type UseAppStoreType } from '../../contexts/AppContext';
import SelectCoordinator from './SelectCoordinator';
import { FederationContext, type UseFederationStoreType } from '../../contexts/FederationContext';
import { GarageContext, type UseGarageStoreType } from '../../contexts/GarageContext';
import { useNavigate } from 'react-router-dom';
import { sha256 } from 'js-sha256';
import AddNewPaymentMethodDialog from '../Dialogs/AddNewPaymentMethodDialog';
import { currencyOptions, currencyLabel } from '../../utils/currencies';
import {
  fixedPricePremium,
  formatOrderPrice,
  formatPricePerBtc,
  fromDisplayPrice,
  priceUnit,
} from '../../utils/fixedPrice';
import {
  SAT_LABEL,
  SAT_OPTION,
  exchangeUnit,
  formatSatRange,
  formatSats,
  formatTotal,
  fromDisplayTotal,
  makerCurrencyOptions,
  amountUnitOptions,
  amountUnitLabel,
  orderPricePerBtc,
  satsEquivalent,
  priceFromTotal,
  totalForSats,
} from '../../utils/satOrders';
import { SPAMCOIN } from '../../utils/currencies';
import { amountScale, formAmountUnit, toApiAmount } from '../../utils/spamAmounts';

/** rl31-beta19: the "丰 sat" mark in the currency selector. */
const SatCoin = (): React.JSX.Element => (
  <span
    aria-hidden
    style={{
      display: 'inline-flex',
      alignItems: 'center',
      justifyContent: 'center',
      width: '1.3em',
      height: '1.3em',
      borderRadius: '50%',
      background: 'radial-gradient(circle at 35% 30%, #ffe08a, #d4a017 70%, #9a7410)',
      color: '#5a3d00',
      fontSize: '0.8em',
      fontWeight: 700,
    }}
  >
    丰
  </span>
);

interface MakerFormProps {
  disableRequest?: boolean;
  collapseAll?: boolean;
  onSubmit?: () => void;
  onReset?: () => void;
  submitButtonLabel?: string;
}

const MakerForm = ({
  disableRequest = false,
  collapseAll = false,
  onSubmit = () => {},
  onReset = () => {},
  submitButtonLabel = 'Create Order',
}: MakerFormProps): React.JSX.Element => {
  const { fav, setFav, settings, navigateToPage } = useContext<UseAppStoreType>(AppContext);
  const { federation } = useContext<UseFederationStoreType>(FederationContext);
  const { federationUpdatedAt } = useContext<UseAppStoreType>(AppContext);
  const { maker, setMaker, garage } = useContext<UseGarageStoreType>(GarageContext);

  const { t } = useTranslation();
  const theme = useTheme();
  const navigate = useNavigate();

  const [badRequest, setBadRequest] = useState<string | null>(null);
  const [amountLimits, setAmountLimits] = useState<number[]>([1, 1000]);
  const [currentPrice, setCurrentPrice] = useState<number>();
  const [currencyCode, setCurrencyCode] = useState<string>('USD');

  const [addNewPaymentMethodOpen, setAddNewPaymentMethodOpen] = useState<boolean>(false);
  const [hasCustomPaymentMethod, setHasCustomPaymentMethod] = useState<boolean>(false);
  const [openNoRobot, setOpenNoRobot] = useState<boolean>(false);
  const [openStoreToken, setOpenStoreToken] = useState<boolean>(false);
  const [openUsedRobotDialog, setOpenUsedRobotDialog] = useState<boolean>(false);
  const [usedRobotHasActiveOrder, setUsedRobotHasActiveOrder] = useState<boolean>(false);
  const [openWorldmap, setOpenWorldmap] = useState<boolean>(false);
  const [bondReturn, setBondReturn] = useState<string>('');
  // 0.9.10: fully checked for the coordinators' network and confirmed before the order is made
  const bondReturnHrp = hrpOfNetwork(federation.network);
  const bondReturnCheck = bondReturn === '' ? null : checkAddress(bondReturn, bondReturnHrp);
  const bondReturnError = bondReturnCheck !== null && !bondReturnCheck.ok;
  const confirmedBondReturn = useRef<string>('');
  const { confirmAddress, addressDialog } = useAddressConfirm();
  const [submittingRequest, setSubmittingRequest] = useState<boolean>(false);
  const [changingRobot, setChangingRobot] = useState<boolean>(false);
  const [amountRangeEnabled, setAmountRangeEnabled] = useState<boolean>(true);
  const [hasRangeError, setHasRangeError] = useState<boolean>(false);
  const [openPublicDuration, setOpenPublicDuration] = useState<boolean>(false);
  const [openEscrowTimer, setOpenEscrowTimer] = useState<boolean>(false);
  const [limits, setLimits] = useState<LimitList>({});

  const amountSafeThresholds = [1.03, 0.98];
  // Onchain escrow: offered to everybody by /api/info, or only to this robot by /api/robot/
  const robotEscrow = garage.getSlot()?.getRobot(maker.coordinator)?.onchainEscrow ?? null;
  const onchainMinFee =
    robotEscrow?.minFeeSats ??
    federation.getCoordinator(maker.coordinator)?.info?.onchain_min_fee_sats ??
    0;
  const onchainAvailable = Boolean(
    federation.getCoordinator(maker.coordinator)?.info?.onchain_escrow || robotEscrow,
  );

  // Roblosats rl31-beta17: FIXED PRICE. The maker types the price per BTC (the currency, or
  // Spamsats for Spamcoin); the order carries it in the order's currency (fixed_price) and
  // the sats are amount / price: no market price needed. vs market is only informative.
  // Only coordinators that announce it: any other would silently make a market order of it.
  const fixedAvailable = Boolean(federation.getCoordinator(maker.coordinator)?.info?.fixed_price);
  // Roblosats rl31-beta19: "丰 sat" orders, first and default where the coordinator offers them.
  // The amount is in sats; fav.currency is what is paid in exchange (EUR when none is chosen).
  const satsAvailable = Boolean(federation.getCoordinator(maker.coordinator)?.info?.sats_orders);
  const satMode = maker.satsMode && satsAvailable;
  const payCurrency = fav.currency === 0 ? 2 : fav.currency;
  const fixedMode = maker.priceMode === 'fixed' && fixedAvailable && !satMode;
  const priceCurrency = satMode ? payCurrency : fav.currency === 0 ? 1 : fav.currency;
  const fixedPriceUnits =
    fixedMode && maker.fixedPrice && maker.fixedPrice > 0
      ? fromDisplayPrice(maker.fixedPrice, priceCurrency)
      : null;
  const marketPriceRaw =
    limits?.[priceCurrency]?.price ??
    federation.getCoordinator(maker.coordinator)?.limits?.[priceCurrency]?.price;
  const marketPrice = marketPriceRaw && marketPriceRaw > 0 ? marketPriceRaw : null;
  const fixedVsMarket =
    fixedPriceUnits !== null ? fixedPricePremium(fixedPriceUnits, marketPrice) : null;
  const effectivePrice = fixedMode ? (fixedPriceUnits ?? undefined) : currentPrice;

  // sat order: the price per BTC from the total in exchange (for the max of a range, so the
  // price per sat is the same along it), or market + premium
  const satRef = maker.advancedOptions && amountRangeEnabled ? maker.maxAmount : maker.amount;
  const satTotalMode = satMode && maker.satPricing === 'total';
  const satPriceUnits = ((): number | null => {
    if (!satMode) return null;
    if (satTotalMode) {
      if (!maker.exchangeTotal || !(maker.exchangeTotal > 0) || !satRef) return null;
      const price = priceFromTotal(fromDisplayTotal(maker.exchangeTotal, payCurrency), satRef);
      return price === null ? null : Number(price.toFixed(8));
    }
    return marketPrice === null ? null : marketPrice * (1 + (maker.premium ?? 0) / 100);
  })();
  const satVsMarket =
    satTotalMode && satPriceUnits !== null ? fixedPricePremium(satPriceUnits, marketPrice) : null;
  // the fixed-like checks: a fixed price, or a sat order with its total in exchange
  const fixedLike = fixedMode || satTotalMode;
  const fixedLikeUnits = satMode ? satPriceUnits : fixedPriceUnits;

  // Roblosats 0.9.1: Spamcoin (1000) is an ordinary currency here, never upstream's swap mode.
  // Its amounts are typed in whole Spamsats; the API gets Spamcoin with 8 decimals.
  const spamMode = !satMode && fav.currency === SPAMCOIN;
  // 0.9.7: a 丰 sat order paid only with Spamcoin methods is paid in Spamcoin (Spamsats): its pay
  // currency follows the methods and is locked (Kilombino, 18 nov 2026: a 丰 sat order with
  // "Spamcoin Lightning" still offered EUR). 0.9.17 (TG 15740): an order whose amount is in a
  // fiat currency keeps it, and Spamcoin can be combined with other methods (SEPA, Bizum…); the
  // order page shows what the fiat amount is worth in Spamcoin sats.
  const onlySpamcoinMethods =
    maker.paymentMethods.length > 0 && maker.paymentMethods.every((m) => /spamcoin/i.test(m));
  const scale = satMode ? 1 : amountScale(fav.currency);
  const amountUnit = formAmountUnit(satMode ? null : fav.currency, currencyCode);
  const formToUnits = (v: number | null | undefined): number => (v ?? 0) / scale;

  // Amount limits of a sat order: the coordinator's limits in sats, as they are
  const satAmountLimits = (): number[] => {
    const coordinator = federation.getCoordinator(maker.coordinator);
    const info = coordinator?.info;
    let minSats = info?.min_order_size ?? 20000;
    let maxSats = info?.max_order_size ?? 250000;
    const sizeLimit = coordinator?.size_limit ?? 0;
    if (sizeLimit > 0) maxSats = Math.min(maxSats, sizeLimit);
    if (maker.onchainEscrow && robotEscrow) {
      minSats = robotEscrow.minSats;
      maxSats = Math.min(maxSats, robotEscrow.maxSats);
    }
    return [minSats, maxSats];
  };

  // Amount limits of a fixed-price order: the coordinator's limits in sats at the maker's price
  const fixedAmountLimits = (): number[] | null => {
    if (fixedPriceUnits === null) return null;
    const coordinator = federation.getCoordinator(maker.coordinator);
    const info = coordinator?.info;
    let minSats = info?.min_order_size ?? 20000;
    let maxSats = info?.max_order_size ?? 250000;
    const sizeLimit = coordinator?.size_limit ?? 0;
    if (sizeLimit > 0) maxSats = Math.min(maxSats, sizeLimit);
    if (maker.onchainEscrow && robotEscrow) {
      minSats = robotEscrow.minSats;
      maxSats = Math.min(maxSats, robotEscrow.maxSats);
    }
    return [
      (minSats / 100000000) * fixedPriceUnits * amountSafeThresholds[0] * scale,
      (maxSats / 100000000) * fixedPriceUnits * amountSafeThresholds[1] * scale,
    ];
  };

  useEffect(() => {
    setCurrencyCode(currencyDict[String(fav.currency === 0 ? 1 : fav.currency)]);
  }, [federationUpdatedAt]);

  useEffect(() => {
    if (maker.priceMode === 'fixed' && !fixedAvailable) {
      setMaker((maker) => ({ ...maker, priceMode: 'market' }));
    }
  }, [maker.coordinator, fixedAvailable, federationUpdatedAt]);

  useEffect(() => {
    if (satMode) {
      setAmountLimits(satAmountLimits());
    } else if (fixedMode) {
      const fixedLimits = fixedAmountLimits();
      if (fixedLimits) setAmountLimits(fixedLimits);
    } else if (Object.keys(limits).length > 0) {
      updateAmountLimits(limits, fav.currency, maker.premium ?? 0);
    }
  }, [
    satMode,
    maker.priceMode,
    maker.fixedPrice,
    fav.currency,
    maker.coordinator,
    maker.onchainEscrow,
    robotEscrow?.minSats,
    robotEscrow?.maxSats,
    federationUpdatedAt,
  ]);

  useEffect(() => {
    updateCoordinatorInfo();
  }, [maker.coordinator]);

  useEffect(() => {
    if (maker.onchainEscrow && !onchainAvailable) {
      setMaker((maker) => ({ ...maker, onchainEscrow: false }));
      return;
    }
    if (Object.keys(limits).length > 0) {
      updateAmountLimits(limits, fav.currency, maker.premium ?? 0);
    }
  }, [maker.onchainEscrow, onchainAvailable, robotEscrow?.minSats, robotEscrow?.maxSats]);

  const updateCoordinatorInfo = (): void => {
    if (maker.coordinator != null) {
      const slot = garage.getSlot();
      slot?.fetchActiveOrder(federation);

      const coordinator = federation.getCoordinator(maker.coordinator);
      if (!coordinator) return;
      const existingLimits = coordinator.limits;
      if (existingLimits && Object.keys(existingLimits).length > 0) {
        // Limits already loaded by loadCoordinatorData() — update local state directly.
        updateAmountLimits(existingLimits, fav.currency, maker.premium ?? 0);
        updateCurrentPrice(existingLimits, fav.currency, maker.premium ?? 0);
        setLimits(existingLimits);
      } else {
        // Fallback: coordinator was unreachable at startup — try again now.
        coordinator.loadLimits(() => {
          const newLimits = coordinator.limits;
          if (newLimits && Object.keys(newLimits).length !== 0) {
            updateAmountLimits(newLimits, fav.currency, maker.premium ?? 0);
            updateCurrentPrice(newLimits, fav.currency, maker.premium ?? 0);
            setLimits(newLimits);
          }
        });
      }
    }
  };
  useEffect(() => {
    // Set the default coordinator only once discovery has settled: at that
    // point the lottery order is final and removed coordinators are gone.
    if (federation.federationListLoaded) {
      setMaker((maker) => ({ ...maker, coordinator: federation.getCoordinatorsAlias()[0] }));
    }
  }, [federation.federationListLoaded]);

  const updateAmountLimits = function (
    limitList: LimitList,
    currency: number,
    premium: number,
  ): void {
    if (satMode) {
      setAmountLimits(satAmountLimits());
      return;
    }
    if (maker.priceMode === 'fixed') {
      const fixedLimits = fixedAmountLimits();
      if (fixedLimits) setAmountLimits(fixedLimits);
      return;
    }
    const index = currency === 0 ? 1 : currency;
    // A coordinator whose limits did not load (offline, slow over Tor) has no entry: keep the
    // current limits instead of throwing (17 nov 2026 QA: the form picked such a coordinator).
    if (!limitList?.[index]) return;
    let minAmountLimit: number = limitList[index].min_amount * (1 + premium / 100);
    let maxAmountLimit: number = limitList[index].max_amount * (1 + premium / 100);

    const coordinatorSizeLimit =
      ((federation.getCoordinator(maker.coordinator)?.size_limit ?? 0) / 100000000) *
      limitList[index].price;
    maxAmountLimit = Math.min(coordinatorSizeLimit, maxAmountLimit);

    if (maker.onchainEscrow && robotEscrow) {
      const price = limitList[index].price * (1 + premium / 100);
      minAmountLimit = (robotEscrow.minSats / 100000000) * price;
      maxAmountLimit = Math.min(maxAmountLimit, (robotEscrow.maxSats / 100000000) * price);
    }

    // apply thresholds to ensure good request
    minAmountLimit = minAmountLimit * amountSafeThresholds[0];
    maxAmountLimit = maxAmountLimit * amountSafeThresholds[1];
    // 0.9.1: Spamcoin limits in Spamsats, as the amount is typed
    const limitScale = amountScale(index);
    setAmountLimits([minAmountLimit * limitScale, maxAmountLimit * limitScale]);
  };

  const updateCurrentPrice = function (
    limitsList: LimitList,
    currency: number,
    premium: number,
  ): void {
    const index = currency === 0 ? 1 : currency;
    if (!limitsList?.[index]) return;
    const price = limitsList[index].price * (1 + premium / 100);

    setCurrentPrice(parseFloat(Number(price).toPrecision(5)));
  };

  const handleCurrencyChange = function (newCurrency: number): void {
    if (newCurrency === SAT_OPTION) {
      // "丰 sat": amounts in sats, the currency becomes what is paid in exchange
      setMaker((maker) => ({
        ...maker,
        satsMode: true,
        amount: null,
        minAmount: null,
        maxAmount: null,
        exchangeTotal: null,
      }));
      setAmountLimits(satAmountLimits());
      return;
    }
    if (satMode) {
      setMaker((maker) => ({
        ...maker,
        satsMode: false,
        amount: null,
        minAmount: null,
        maxAmount: null,
      }));
    } else if (amountScale(newCurrency) !== amountScale(fav.currency === 0 ? 1 : fav.currency)) {
      // 0.9.1: to or from Spamcoin (typed in Spamsats) an amount means something else
      setMaker((maker) => ({ ...maker, amount: null, minAmount: null, maxAmount: null }));
    }
    const currencyCode: string = currencyDict[String(newCurrency)];
    setCurrencyCode(currencyCode);
    setFav((prev) => {
      // 0.9.1: Spamcoin keeps the normal form (no swap mode in Roblosats)
      return {
        ...prev,
        currency: newCurrency,
        mode: 'fiat',
      };
    });
    if (maker.priceMode === 'fixed') {
      // a price in the old currency means nothing in the new one
      setMaker((maker) => ({ ...maker, fixedPrice: null }));
      updateCurrentPrice(limits, newCurrency, maker.premium ?? 0);
      return;
    }
    updateAmountLimits(limits, newCurrency, maker.premium ?? 0);
    updateCurrentPrice(limits, newCurrency, maker.premium ?? 0);

    if (makerHasAmountRange && limits[newCurrency]) {
      const newScale = amountScale(newCurrency);
      const minAmount = parseFloat(
        Number(limits[newCurrency].min_amount * newScale).toPrecision(2),
      );
      const maxAmount = parseFloat(
        Number(limits[newCurrency].max_amount * newScale).toPrecision(2),
      );
      if (
        parseFloat(String(maker.minAmount ?? 0)) < minAmount ||
        parseFloat(String(maker.minAmount ?? 0)) > maxAmount ||
        parseFloat(String(maker.maxAmount ?? 0)) > maxAmount ||
        parseFloat(String(maker.maxAmount ?? 0)) < minAmount
      ) {
        setMaker((maker) => ({
          ...maker,
          minAmount: parseFloat((maxAmount * 0.25).toPrecision(2)),
          maxAmount: parseFloat((maxAmount * 0.75).toPrecision(2)),
        }));
      }
    }
  };

  const handlePayCurrencyChange = (newCurrency: number): void => {
    setCurrencyCode(currencyDict[String(newCurrency)]);
    setFav((prev) => ({
      ...prev,
      currency: newCurrency,
      mode: 'fiat',
    }));
    setMaker((maker) => ({ ...maker, exchangeTotal: null }));
    updateCurrentPrice(limits, newCurrency, maker.premium ?? 0);
  };

  useEffect(() => {
    if (satMode && onlySpamcoinMethods && payCurrency !== SPAMCOIN) {
      handlePayCurrencyChange(SPAMCOIN);
    }
  }, [onlySpamcoinMethods, satMode, payCurrency]);

  const makerHasAmountRange = useMemo(() => {
    return maker.advancedOptions && amountRangeEnabled;
  }, [maker.advancedOptions, amountRangeEnabled]);

  const handlePaymentMethodChange = function (paymentArray: string[]): void {
    let str = '';
    const arrayLength = paymentArray.length;
    let includesCash = false;

    for (let i = 0; i < arrayLength; i++) {
      str += paymentArray[i] + ' ';
      if (paymentArray[i].includes('F2F')) {
        includesCash = true;
        if (i === arrayLength - 1) {
          setOpenWorldmap(true);
        }
      }
    }
    const paymentMethodText = str.slice(0, -1);

    setMaker((maker) => {
      return {
        ...maker,
        paymentMethods: paymentArray,
        paymentMethodsText: paymentMethodText,
        badPaymentMethod: paymentMethodText.length > 50,
        latitude: includesCash ? maker.latitude : null,
        longitude: includesCash ? maker.longitude : null,
      };
    });
  };

  const handlePremiumChange: React.ChangeEventHandler<HTMLInputElement | HTMLTextAreaElement> =
    function ({ target }): void {
      const value = cleanNumberInput(target);
      const max = 999;
      const min = -100;
      const newPremium = Math.floor(Number(value) * Math.pow(10, 2)) / Math.pow(10, 2);
      let premium: number = isNaN(newPremium) ? 0 : newPremium;
      let badPremiumText: string = '';
      if (newPremium > 999) {
        badPremiumText = t('Must be less than {{max}}%', { max });
        premium = 999;
      } else if (newPremium <= -100) {
        badPremiumText = t('Must be more than {{min}}%', { min });
        premium = -99.99;
      }
      updateCurrentPrice(limits, fav.currency, premium);
      updateAmountLimits(limits, fav.currency, premium);
      setMaker({
        ...maker,
        premium: isNaN(newPremium) || value === '' ? null : premium,
        badPremiumText,
      });
    };

  const handlePriceModeChange = (_e: React.MouseEvent<HTMLElement>, mode: string | null): void => {
    if (mode !== 'market' && mode !== 'fixed') return;
    setMaker((maker) => ({ ...maker, priceMode: mode }));
  };

  const handleFixedPriceChange: React.ChangeEventHandler<HTMLInputElement | HTMLTextAreaElement> =
    function ({ target }): void {
      const price = numberFromInput(target);
      setMaker((maker) => ({ ...maker, fixedPrice: price }));
    };

  // 0.9.7: a 丰 sat order with Spamcoin-only methods is always paid in Spamcoin (currency 1000),
  // whatever a stale selector says (Kilobot #25: "Spamcoin Lightning" at a 15% premium went out
  // as EUR). An order whose amount is in fiat keeps its currency (0.9.17).
  const buildOrderAttributes = (): object => {
    const attrs = buildOrderAttributesRaw() as Record<string, unknown>;
    return satMode && onlySpamcoinMethods ? { ...attrs, currency: SPAMCOIN } : attrs;
  };

  const buildOrderAttributesRaw = (): object =>
    satMode
      ? {
          ...baseOrderAttributes(),
          currency: payCurrency,
          amount: makerHasAmountRange ? null : Math.round(maker.amount ?? 0),
          min_amount: makerHasAmountRange ? Math.round(maker.minAmount ?? 0) : null,
          max_amount: makerHasAmountRange ? Math.round(maker.maxAmount ?? 0) : null,
          premium: satTotalMode || !maker.premium ? 0 : maker.premium,
          fixed_price: satTotalMode ? satPriceUnits : null,
          sats_denominated: true,
        }
      : baseOrderAttributes();

  const baseOrderAttributes = (): object => ({
    type: fav.type === 0 ? 1 : 0,
    currency: fav.currency === 0 ? 1 : fav.currency,
    // 0.9.1: Spamsats typed → Spamcoin for the API (other currencies as they are)
    amount: makerHasAmountRange ? null : toApiAmount(maker.amount, fav.currency),
    has_range: makerHasAmountRange,
    min_amount: makerHasAmountRange ? toApiAmount(maker.minAmount, fav.currency) : null,
    max_amount: makerHasAmountRange ? toApiAmount(maker.maxAmount, fav.currency) : null,
    payment_method: maker.paymentMethodsText === '' ? 'not specified' : maker.paymentMethodsText,
    premium: fixedMode || !maker.premium ? 0 : maker.premium,
    satoshis: null,
    fixed_price: fixedMode ? fixedPriceUnits : null,
    public_duration: maker.publicDuration,
    escrow_duration: maker.escrowDuration,
    bond_size: maker.bondSize,
    latitude: maker.latitude,
    longitude: maker.longitude,
    shortAlias: maker.coordinator,
    password: maker.password ? sha256(maker.password) : null,
    description: maker.description ? maker.description : null,
    escrow_type: maker.onchainEscrow ? 1 : 0,
  });

  const submitOrder = (): void => {
    void garage
      .makeOrderWithRecovery(federation, buildOrderAttributes())
      .then((order: Order) => {
        if (order.id) {
          // 0.9.7: the bond's return address goes with its bond key, from the order page
          const token = garage.getSlot()?.token;
          if (token && maker.onchainEscrow && bondReturnCheck?.ok) {
            systemClient.setItem(pendingReturnKey(token), bondReturnCheck.address);
            void rememberMyAddress(bondReturnCheck.address);
          }
          navigateToPage(`order/${order.shortAlias}/${order.id}`, navigate);
          clearMaker();
        } else if (order?.bad_request) {
          setBadRequest(order?.bad_request);
        }
        setSubmittingRequest(false);
      })
      .catch(() => {
        setBadRequest('Request error');
        setSubmittingRequest(false);
      });
  };

  const handleCreateOrder = function (): void {
    const slot = garage.getSlot();

    if (
      !disableRequest &&
      maker.onchainEscrow &&
      bondReturnCheck?.ok &&
      confirmedBondReturn.current !== bondReturn
    ) {
      setOpenStoreToken(false);
      confirmAddress({
        address: bondReturn,
        hrp: bondReturnHrp,
        message: t('Your bond will come back to:'),
        onConfirm: () => {
          confirmedBondReturn.current = bondReturn;
          handleCreateOrder();
        },
      });
      return;
    }

    if (!disableRequest && maker.coordinator && slot) {
      setSubmittingRequest(true);

      const slotHasBeenUsed = Boolean(slot.activeOrder ?? slot.lastOrder);
      if (garage.garageKey && slotHasBeenUsed && !slot.isReusable()) {
        setSubmittingRequest(false);
        setOpenStoreToken(false);
        setUsedRobotHasActiveOrder(Boolean(slot.activeOrder));
        setOpenUsedRobotDialog(true);
        return;
      }

      submitOrder();
    }
    setOpenStoreToken(false);
  };

  const handleChangeRobotAndCreate = (): void => {
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
        setSubmittingRequest(true);
        submitOrder();
      })
      .catch(() => {
        setBadRequest('Could not switch to a new robot. Please try again.');
        setOpenUsedRobotDialog(false);
      })
      .finally(() => {
        setChangingRobot(false);
      });
  };

  const handleChangePublicDuration = function (date: Date): void {
    const d = new Date(date);
    const hours: number = d.getHours();
    const minutes: number = d.getMinutes();

    const totalSecs: number = hours * 60 * 60 + minutes * 60;

    setMaker({
      ...maker,
      publicExpiryTime: date,
      publicDuration: totalSecs,
    });
  };

  const handlePasswordChange = function (event: React.ChangeEvent<HTMLInputElement>): void {
    setMaker({
      ...maker,
      password: event.target.value,
    });
  };

  const handleDescriptionChange = function (event: React.ChangeEvent<HTMLInputElement>): void {
    setMaker({
      ...maker,
      description: event.target.value,
      badDescription: event.target.value.length > 240,
    });
  };

  const handleChangeEscrowDuration = function (date: Date): void {
    const d = new Date(date);
    const hours: number = d.getHours();
    const minutes: number = d.getMinutes();

    const totalSecs: number = hours * 60 * 60 + minutes * 60;

    setMaker({
      ...maker,
      escrowExpiryTime: date,
      escrowDuration: totalSecs,
    });
  };

  const handleClickAdvanced = function (): void {
    if (maker.advancedOptions) {
      setMaker({ ...maker, advancedOptions: false });
    } else {
      resetRange(true);
    }
  };

  const handleAddNewPaymentMethod: (newPaymentMethod: string) => void = (newPaymentMethod) => {
    handlePaymentMethodChange([...maker.paymentMethods, newPaymentMethod]);
    setAddNewPaymentMethodOpen(false);
  };

  const resetRange = function (advancedOptions: boolean): void {
    const index = fav.currency === 0 ? 1 : fav.currency;
    // fixed price, or no market price: from the form's own limits
    const maxLimit =
      fixedMode || !(limits[index]?.max_amount > 0)
        ? amountLimits[1] / amountSafeThresholds[1]
        : limits[index].max_amount * scale;
    const minAmount =
      maker.amount !== null
        ? parseFloat((maker.amount / 2).toPrecision(2))
        : parseFloat(Number(maxLimit * 0.25).toPrecision(2));
    const maxAmount =
      maker.amount !== null ? maker.amount : parseFloat(Number(maxLimit * 0.75).toPrecision(2));

    setMaker({
      ...maker,
      advancedOptions,
      minAmount,
      maxAmount,
    });
  };

  const handleClickAmountRangeEnabled = function (
    _e: React.ChangeEvent<HTMLInputElement>,
    checked: boolean,
  ): void {
    setAmountRangeEnabled(checked);
  };

  const { isLegacyMode, legacyDisabledTooltip } = useLegacyMode();

  // rl31-beta20: computed on every render (a memo with missing dependencies kept CREATE ORDER
  // disabled after picking a payment method until something else re-rendered the form)
  const disableSubmit: boolean = Boolean(
    !federation.federationListLoaded ||
    isLegacyMode ||
    fav.type == null ||
    (!makerHasAmountRange &&
      maker.amount &&
      (maker.amount < amountLimits[0] || maker.amount > amountLimits[1])) ||
    maker.badPaymentMethod ||
    (maker.amount == null &&
      (!makerHasAmountRange ||
        (!fixedLike && !satMode && (Object.keys(limits)?.length ?? 0) < 1))) ||
    (makerHasAmountRange && hasRangeError) ||
    // rl31-beta20: a cleared range field is empty, i.e. required
    (makerHasAmountRange && (maker.minAmount == null || maker.maxAmount == null)) ||
    (!makerHasAmountRange && maker.amount != null && maker.amount <= 0) ||
    (fixedLike && fixedLikeUnits === null) ||
    (satMode && !satTotalMode && marketPrice === null) ||
    (!fixedLike && maker.badPremiumText !== '') ||
    (!fixedLike &&
      Object.keys(federation.getCoordinator(maker.coordinator)?.limits ?? {}).length === 0) ||
    (fixedLike && !federation.getCoordinator(maker.coordinator)?.info) ||
    (!fixedLike && maker.premium !== null && typeof maker.premium !== 'number') ||
    maker.paymentMethods.length === 0 ||
    maker.badDescription ||
    (maker.onchainEscrow && bondReturnError), // 0.9.7: fix or empty the return address
  );

  const clearMaker = function (): void {
    setFav((prev) => {
      return { ...prev, type: null, currency: 0, mode: 'fiat' };
    });
    setMaker(defaultMaker);
    handleCurrencyChange(0);
    handlePaymentMethodChange([]);
  };

  const handleAddLocation = (pos: [number, number]): void => {
    if (pos?.length === 2) {
      setMaker((maker) => {
        return {
          ...maker,
          latitude: parseFloat(pos[0].toPrecision(6)),
          longitude: parseFloat(pos[1].toPrecision(6)),
        };
      });
      const cashMethod = maker.paymentMethods.find((method) => method === 'cash');
      if (cashMethod !== null) {
        const newMethods = maker.paymentMethods;
        const cash = fiatMethods.find((method) => method.icon === 'cash');
        if (cash) {
          newMethods.unshift(cash.name);
          handlePaymentMethodChange(newMethods);
        }
      }
    }
  };

  const currencyFormatter = new Intl.NumberFormat(settings.language);

  const getDisabledMessage = () => {
    if (!federation.federationListLoaded) {
      return t('Loading coordinator list...');
    }
    if (isLegacyMode) {
      return legacyDisabledTooltip;
    }
    if (satTotalMode && satPriceUnits === null) {
      return t('Please enter what you want in exchange.');
    }
    if (fixedMode && fixedPriceUnits === null) {
      return t('Please enter your price per BTC.');
    }
    if (!fixedLike && (satMode ? marketPrice === null : currentPrice === undefined)) {
      return t('The Bitcoin price is not synchronized.');
    }
    if (fav.type == null) {
      return t('Please select if you want to buy or sell.');
    }
    if (
      !makerHasAmountRange &&
      maker.amount &&
      (maker.amount < amountLimits[0] || maker.amount > amountLimits[1])
    ) {
      return t('The amount is outside the allowed limits.');
    }
    if (maker.badPaymentMethod) {
      return t('The payment method is not valid.');
    }
    if (maker.amount == null && (!makerHasAmountRange || (Object.keys(limits)?.length ?? 0) < 1)) {
      return t('Please enter an amount.');
    }
    if (makerHasAmountRange && hasRangeError) {
      return t('The amount range is not valid.');
    }
    if (makerHasAmountRange && (maker.minAmount == null || maker.maxAmount == null)) {
      return t('Please enter an amount.');
    }
    if (!makerHasAmountRange && maker.amount && maker.amount <= 0) {
      return t('The amount must be greater than 0.');
    }
    if (!fixedLike && maker.badPremiumText !== '') {
      return t('The premium is not valid.');
    }
    if (
      !fixedLike &&
      Object.keys(federation.getCoordinator(maker.coordinator)?.limits ?? {}).length === 0
    ) {
      return t('The coordinator limits are not available.');
    }
    if (!fixedLike && typeof maker.premium !== 'number') {
      return t('The premium must be a number.');
    }
    if (maker.paymentMethods.length === 0) {
      return t('Please select a payment method.');
    }
    if (maker.badDescription) {
      return t('The description is not valid.');
    }
    return t('You must fill the form correctly');
  };

  const bondAmount = useBondEstimate({
    maker,
    fav,
    federation,
    currentPrice: satMode
      ? 100_000_000
      : effectivePrice !== undefined
        ? effectivePrice * scale
        : undefined,
    federationUpdatedAt: federationUpdatedAt ? Date.parse(federationUpdatedAt) : 0,
    amountRangeEnabled,
    satMode,
  });

  // rl31-beta14: the maker's onchain fee as the coordinator will charge it (minimum included),
  // from the trade amount behind the bond estimate (the largest amount of a range).
  const makerOnchainFee = ((): number | null => {
    const info = federation.getCoordinator(maker.coordinator)?.info;
    const bondPct = maker.bondSize ?? info?.bond_size ?? 3;
    if (!info || bondAmount === null || !bondPct || !(Number(bondAmount) > 0)) return null;
    const tradeSats = Math.round((Number(bondAmount) * 100) / bondPct);
    return onchainFees({
      tradeSats,
      makerFeeRate: info.maker_fee ?? 0,
      takerFeeRate: info.taker_fee ?? 0,
      makerIsSeller: fav.type === 0, // the order type is the opposite of the book tab (line ~285)
      minFeeSats: onchainMinFee,
    }).makerSats;
  })();

  // 0.9.14-beta5 (TG 15627): the 丰 sat side of a fiat or Spamsats amount, live, at the price
  // the order will use (fixed, or market + premium)
  const equivalentPrice = orderPricePerBtc(
    fixedMode ? fixedPriceUnits : undefined,
    marketPrice,
    maker.premium,
  );
  const satsEquivalentText = ((): string | null => {
    if (satMode || (fixedMode && fixedPriceUnits === null)) return null;
    if (makerHasAmountRange) {
      const a = satsEquivalent(formToUnits(maker.minAmount), equivalentPrice);
      const b = satsEquivalent(formToUnits(maker.maxAmount), equivalentPrice);
      return a !== null && b !== null ? formatSatRange(a, b) : null;
    }
    const n = satsEquivalent(formToUnits(maker.amount), equivalentPrice);
    return n === null ? null : formatSats(n);
  })();

  const SummaryText = (): React.JSX.Element => {
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        {satMode ? (
          <Typography
            component='h2'
            variant='subtitle2'
            align='center'
            color={disableSubmit ? 'text.secondary' : 'text.primary'}
          >
            {t(
              fav.type == null
                ? 'Order of {{sats}}'
                : fav.type === 1
                  ? 'Buy {{sats}}'
                  : 'Sell {{sats}}',
              {
                sats: makerHasAmountRange
                  ? formatSatRange(maker.minAmount ?? 0, maker.maxAmount ?? 0)
                  : formatSats(maker.amount ?? 0),
              },
            )}
            {satPriceUnits !== null
              ? ` ↔ ${
                  makerHasAmountRange
                    ? `${formatTotal(totalForSats(maker.minAmount ?? 0, satPriceUnits), payCurrency).split(' ')[0]}-${formatTotal(totalForSats(maker.maxAmount ?? 0, satPriceUnits), payCurrency)}`
                    : formatTotal(totalForSats(maker.amount ?? 0, satPriceUnits), payCurrency)
                }`
              : ''}
            {!satTotalMode
              ? (maker.premium ?? 0) === 0
                ? t(' at market price')
                : (maker.premium ?? 0) > 0
                  ? t(' at a {{premium}}% premium', { premium: maker.premium })
                  : t(' at a {{discount}}% discount', { discount: -(maker.premium ?? 0) })
              : ''}
          </Typography>
        ) : null}
        <Typography
          component='h2'
          variant='subtitle2'
          align='center'
          color={disableSubmit ? 'text.secondary' : 'text.primary'}
          sx={{ display: satMode ? 'none' : undefined }}
        >
          {fav.type == null
            ? t('Order for ')
            : fav.type === 1
              ? t('Buy BTC for ')
              : t('Sell BTC for ')}
          {amountToString(
            String(maker.amount ?? 0),
            makerHasAmountRange,
            maker.minAmount ?? 0,
            maker.maxAmount ?? 0,
          )}
          {' ' + amountUnit}
          {satsEquivalentText ? ` (≈ ${satsEquivalentText})` : ''}
          {fixedMode
            ? fixedPriceUnits !== null
              ? t(' at a fixed price of {{price}}', {
                  price: formatPricePerBtc(fixedPriceUnits, priceCurrency),
                })
              : ''
            : (maker.premium ?? 0) === 0
              ? t(' at market price')
              : (maker.premium ?? 0) > 0
                ? t(' at a {{premium}}% premium', { premium: maker.premium })
                : t(' at a {{discount}}% discount', { discount: -(maker.premium ?? 0) })}
        </Typography>
        {bondAmount !== null && (
          <Typography variant='caption' color='text.secondary' sx={{ mt: 0.5 }}>
            {t('Estimated Bond')}:{' '}
            {pn(
              maker.onchainEscrow && robotEscrow?.minBondSats
                ? Math.max(Number(bondAmount), robotEscrow.minBondSats)
                : Number(bondAmount),
            )}{' '}
            Sats
          </Typography>
        )}
        {maker.onchainEscrow && onchainMinFee ? (
          <Typography variant='caption' color='text.secondary'>
            {makerOnchainFee !== null
              ? t(
                  'Your onchain coordinator fee: {{fee}} Sats (the order pays at least {{min}} Sats in total, shared by both traders)',
                  {
                    fee: pn(makerOnchainFee),
                    min: pn(onchainMinFee),
                  },
                )
              : t('Onchain coordinator fee: at least {{sats}} Sats', { sats: pn(onchainMinFee) })}
          </Typography>
        ) : null}
      </Box>
    );
  };

  return (
    <Box>
      <NoRobotDialog
        open={openNoRobot}
        onClose={() => {
          setOpenNoRobot(false);
        }}
        onClickGenerateRobot={() =>
          garage.generateRobot(federation).then(() => {
            setOpenNoRobot(false);
            setOpenStoreToken(true);
          })
        }
      />
      {addressDialog}
      <StoreTokenDialog
        open={openStoreToken}
        onClose={() => {
          setOpenStoreToken(false);
        }}
        onClickBack={() => {
          setOpenStoreToken(false);
        }}
        onClickDone={handleCreateOrder}
      />
      <F2fMapDialog
        interactive
        latitude={maker.latitude}
        longitude={maker.longitude}
        open={openWorldmap}
        message={t(
          'To protect your privacy, the exact location you pin will be slightly randomized.',
        )}
        orderType={fav?.type ?? 0}
        onClose={(pos?: [number, number]) => {
          if (pos != null) handleAddLocation(pos);
          setOpenWorldmap(false);
        }}
        zoom={maker.latitude === 0 && maker.longitude === 0 ? 2 : 6}
      />
      <UsedRobotDialog
        open={openUsedRobotDialog}
        action='make'
        hasActiveOrder={usedRobotHasActiveOrder}
        onClose={() => setOpenUsedRobotDialog(false)}
        onChangeRobot={handleChangeRobotAndCreate}
        loading={changingRobot}
      />
      <Collapse in={!collapseAll}>
        <Grid container spacing={0} sx={{ maxHeight: '1em', justifyContent: 'space-between' }}>
          <Grid>
            <IconButton
              sx={{
                width: '1.3em',
                height: '1.3em',
                position: 'relative',
                bottom: '0.2em',
                right: '0.2em',
                color: 'text.secondary',
              }}
              onClick={clearMaker}
            >
              <Tooltip
                placement='top'
                enterTouchDelay={500}
                enterDelay={700}
                enterNextDelay={2000}
                title={t('Clear form')}
              >
                <DeleteSweep sx={{ width: '1em', height: '1em' }} />
              </Tooltip>
            </IconButton>
          </Grid>
          <Grid>
            <Tooltip enterTouchDelay={0} placement='top' title={t('Enable advanced options')}>
              <div
                style={{
                  display: 'flex',
                  width: '4em',
                  height: '1.1em',
                }}
              >
                <Switch
                  size='small'
                  disabled={!federation.federationListLoaded || Object.keys(limits).length === 0}
                  checked={maker.advancedOptions}
                  onChange={handleClickAdvanced}
                />
                {!federation.federationListLoaded || Object.keys(limits).length === 0 ? (
                  <CircularProgress size={15} style={{ marginLeft: 7 }} />
                ) : (
                  <SelfImprovement sx={{ color: 'text.secondary' }} />
                )}
              </div>
            </Tooltip>
          </Grid>
        </Grid>
      </Collapse>

      <Collapse in={!collapseAll}>
        <Grid container spacing={1} sx={{ alignItems: 'center', justifyContent: 'center' }}>
          <Grid>
            <Grid
              container
              direction='row'
              sx={{ alignItems: 'stretch', justifyContent: 'center' }}
            >
              {/* 0.9.1: no "Swap?" box: currency 1000 is Spamcoin in Roblosats */}
              <Grid>
                <FormControl component='fieldset'>
                  <FormHelperText sx={{ textAlign: 'center' }}>
                    {`${t('Buy or Sell Bitcoin?')} *`}
                  </FormHelperText>
                  <div style={{ textAlign: 'center' }}>
                    <ButtonGroup size='large'>
                      <Box
                        sx={{
                          boxShadow: fav.type === 1 ? 0 : 3,
                          display: 'inline-block',
                          borderBottomLeftRadius: 4,
                          borderTopLeftRadius: 4,
                        }}
                      >
                        <Button
                          size={maker.advancedOptions ? 'small' : 'large'}
                          variant='contained'
                          onClick={() => {
                            setFav((prev) => {
                              return {
                                ...prev,
                                type: 1,
                              };
                            });
                          }}
                          disableElevation={fav.type === 1}
                          sx={{
                            backgroundColor:
                              fav.type === 1 ? 'primary.main' : theme.palette.background.paper,
                            color:
                              fav.type === 1 ? theme.palette.background.paper : 'text.secondary',
                            ':hover': {
                              color: theme.palette.background.paper,
                              backgroundColor: 'primary.main',
                            },
                          }}
                        >
                          {t('Buy')}
                        </Button>
                      </Box>
                      <Box
                        sx={{
                          boxShadow: fav.type === 0 ? 0 : 3,
                          display: 'inline-block',
                          borderBottomRightRadius: 4,
                          borderTopRightRadius: 4,
                        }}
                      >
                        <Button
                          size={maker.advancedOptions ? 'small' : 'large'}
                          variant='contained'
                          onClick={() => {
                            setFav((prev) => {
                              return {
                                ...prev,
                                type: 0,
                              };
                            });
                          }}
                          color='secondary'
                          sx={{
                            boxShadow: 3,
                            backgroundColor:
                              fav.type === 0 ? 'secondary.main' : theme.palette.background.paper,
                            color: fav.type === 0 ? 'background.secondary' : 'text.secondary',
                            ':hover': {
                              color: theme.palette.background.paper,
                              backgroundColor: 'secondary.main',
                            },
                          }}
                        >
                          {t('Sell')}
                        </Button>
                      </Box>
                    </ButtonGroup>
                  </div>
                </FormControl>
              </Grid>
            </Grid>
          </Grid>

          <Grid sx={{ width: '100%' }}>
            <Collapse in={maker.advancedOptions}>
              <FormControlLabel
                control={
                  <Switch
                    size='small'
                    disabled={!maker.advancedOptions}
                    checked={amountRangeEnabled}
                    onChange={handleClickAmountRangeEnabled}
                  />
                }
                sx={{
                  paddingLeft: '1em',
                  color: 'text.secondary',
                  marginTop: '-0.5em',
                  paddingBottom: '0.5em',
                }}
                label={amountRangeEnabled ? t('Amount Range') : t('Exact Amount')}
              />
            </Collapse>
            <Collapse in={makerHasAmountRange}>
              <AmountRange
                satMode={satMode}
                satsAvailable={satsAvailable}
                currency={fav.currency}
                currencyCode={satMode ? SAT_LABEL : amountUnit}
                integerAmounts={spamMode}
                handleCurrencyChange={handleCurrencyChange}
                amountLimits={amountLimits}
                amountSafeThresholds={amountSafeThresholds}
                setHasRangeError={setHasRangeError}
              />
            </Collapse>
            <Collapse in={!makerHasAmountRange}>
              <Grid container>
                <Grid sx={{ width: '50%' }}>
                  <Tooltip
                    placement='top'
                    enterTouchDelay={500}
                    enterDelay={700}
                    enterNextDelay={2000}
                    title={t('Amount of fiat to exchange for bitcoin')}
                  >
                    <TextField
                      fullWidth
                      disabled={makerHasAmountRange}
                      variant={makerHasAmountRange ? 'filled' : 'outlined'}
                      error={
                        maker.amount !== null &&
                        (maker.amount < amountLimits[0] || maker.amount > amountLimits[1])
                      }
                      helperText={
                        maker.amount && maker.amount < amountLimits[0]
                          ? t('Must be more than {{minAmount}}', {
                              minAmount: pn(parseFloat(amountLimits[0].toPrecision(2))),
                            })
                          : maker.amount && maker.amount > amountLimits[1]
                            ? t('Must be less than {{maxAmount}}', {
                                maxAmount: pn(parseFloat(amountLimits[1].toPrecision(2))),
                              })
                            : null
                      }
                      label={
                        satMode ? t('Amount ({{unit}})', { unit: SAT_LABEL }) : t('Amount') // 0.9.1: Spamcoin's unit (Spamsats) is in the selector
                      }
                      required={true}
                      value={maker.amount ?? ''}
                      type='number'
                      slotProps={{
                        htmlInput: { inputMode: satMode || spamMode ? 'numeric' : 'decimal' },
                      }}
                      onChange={(e) => {
                        const value = numberFromInput(e.target);
                        // whole Spamsats only
                        const amount = spamMode && value !== null ? Math.round(value) : value;
                        setMaker({ ...maker, amount });
                      }}
                    />
                  </Tooltip>
                </Grid>

                <Grid sx={{ width: '50%' }}>
                  <Select
                    fullWidth
                    sx={{
                      backgroundColor: theme.palette.background.paper,
                      borderRadius: '4px',
                    }}
                    required={true}
                    inputProps={{
                      style: { textAlign: 'center' },
                    }}
                    value={satMode ? SAT_OPTION : fav.currency === 0 ? 1 : fav.currency}
                    // 0.9.1: Spamcoin shows the unit the amount is typed in (Spamsats)
                    renderValue={
                      spamMode
                        ? () => (
                            <div
                              style={{
                                display: 'flex',
                                alignItems: 'center',
                                whiteSpace: 'nowrap',
                              }}
                            >
                              <FlagWithProps code={currencyDict[String(SPAMCOIN)]} />
                              <span style={{ marginLeft: '0.15em', fontSize: '0.88em' }}>
                                Spamsats
                              </span>
                            </div>
                          )
                        : undefined
                    }
                    onChange={(e) => {
                      handleCurrencyChange(Number(e.target.value));
                    }}
                  >
                    {satsAvailable ? (
                      <MenuItem key='sat' value={SAT_OPTION}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.3em' }}>
                          <SatCoin /> {SAT_LABEL}
                        </div>
                      </MenuItem>
                    ) : null}
                    {(satsAvailable ? amountUnitOptions : currencyOptions)(currencyDict).map(
                      ([key, value]) => (
                        <MenuItem key={key} value={parseInt(key)}>
                          <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                            <FlagWithProps code={value} />
                            {' ' + amountUnitLabel(key, value)}
                          </div>
                        </MenuItem>
                      ),
                    )}
                  </Select>
                </Grid>
              </Grid>
            </Collapse>
          </Grid>
          <Grid sx={{ width: '100%' }}>
            <Grid sx={{ width: '100%', display: 'flex', flexDirection: 'row' }}>
              <Grid sx={{ width: '80%' }}>
                <AutocompletePayments
                  paymentMethods={maker.paymentMethods}
                  paymentMethodsText={maker.paymentMethodsText}
                  setHasCustomPaymentMethod={setHasCustomPaymentMethod}
                  onAutocompleteChange={handlePaymentMethodChange}
                  optionsType='fiat'
                  error={maker.badPaymentMethod}
                  label={`${
                    satMode
                      ? payCurrency === SPAMCOIN
                        ? t('Spamcoin payment method(s)')
                        : t('Payment method(s)')
                      : spamMode
                        ? t('Spamcoin payment method(s)')
                        : t('Fiat Payment Method(s)')
                  } *`}
                  tooltipTitle={t(
                    'Enter your preferred fiat payment methods. Fast methods are highly recommended.',
                  )}
                  addNewButtonText={t('Add New')}
                  isFilter={false}
                  multiple={true}
                />
              </Grid>
              <Grid sx={{ width: '20%' }}>
                <Tooltip
                  placement='top'
                  enterTouchDelay={300}
                  enterDelay={700}
                  enterNextDelay={2000}
                  title={t('Add custom payment method')}
                >
                  <Button
                    color='inherit'
                    variant={hasCustomPaymentMethod ? 'contained' : 'outlined'}
                    fullWidth
                    sx={{
                      minHeight: '3.3em',
                      minWidth: 0,
                      padding: 0,
                      justifyContent: 'center',
                      fontWeight: 'normal',
                      textTransform: 'none',
                      backgroundColor: theme.palette.background.paper,
                      color: theme.palette.text.secondary,
                      borderColor: theme.palette.text.disabled,
                    }}
                    onClick={() => setAddNewPaymentMethodOpen(true)}
                  >
                    <DashboardCustomizeIcon
                      sx={{ width: '1em', height: '1em' }}
                      color={hasCustomPaymentMethod ? 'primary' : 'inherit'}
                    />
                  </Button>
                </Tooltip>
              </Grid>
            </Grid>
            {maker.badPaymentMethod && (
              <FormHelperText error={true}>
                {t('Must be shorter than 65 characters')}
              </FormHelperText>
            )}
          </Grid>

          {/* 0.9.7: no face-to-face location when the only methods are Spamcoin (online) */}
          {!spamMode && !onlySpamcoinMethods && (
            <Grid sx={{ width: '100%' }}>
              <Tooltip enterTouchDelay={0} title={t('Add geolocation for a face to face trade')}>
                <Button
                  size='large'
                  fullWidth={true}
                  color='inherit'
                  variant='outlined'
                  sx={{
                    justifyContent: 'flex-start',
                    fontWeight: 'normal',
                    textTransform: 'none',
                    backgroundColor: theme.palette.background.paper,
                    color: theme.palette.text.secondary,
                    borderColor: theme.palette.text.disabled,
                  }}
                  onClick={() => {
                    setOpenWorldmap(true);
                  }}
                >
                  {t('Face to Face Location')}
                  <Map style={{ paddingLeft: 5 }} />
                </Button>
              </Tooltip>
            </Grid>
          )}

          {satMode ? (
            <Grid sx={{ width: '100%' }}>
              <ToggleButtonGroup
                fullWidth
                exclusive
                size='small'
                color='primary'
                value={maker.satPricing}
                onChange={(_e, v: string | null) => {
                  if (v === 'total' || v === 'market')
                    setMaker((maker) => ({ ...maker, satPricing: v }));
                }}
                sx={{ mb: 1, backgroundColor: theme.palette.background.paper }}
              >
                <ToggleButton value='total' sx={{ textTransform: 'none' }}>
                  {t('Fixed')}
                </ToggleButton>
                <ToggleButton value='market' sx={{ textTransform: 'none' }}>
                  {t('Premium over market')}
                </ToggleButton>
              </ToggleButtonGroup>
              {!satTotalMode && !onlySpamcoinMethods ? (
                // Premium mode: the payment unit on its own row, labelled, so the premium
                // field never reads like "100 EUR" (Kilombino, 18 nov 2026)
                <FormControl fullWidth sx={{ mb: 1.5 }}>
                  <InputLabel id='sat-pay-unit'>{t('Paid in')}</InputLabel>
                  <Select
                    labelId='sat-pay-unit'
                    label={t('Paid in')}
                    fullWidth
                    sx={{ backgroundColor: theme.palette.background.paper, borderRadius: '4px' }}
                    value={payCurrency}
                    renderValue={(v) => exchangeUnit(Number(v))}
                    onChange={(e) => {
                      handlePayCurrencyChange(Number(e.target.value));
                    }}
                  >
                    {makerCurrencyOptions(currencyDict).map(([key, value]) => (
                      <MenuItem key={key} value={parseInt(key)}>
                        <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                          <FlagWithProps code={value} />
                          {' ' +
                            (Number(key) === SPAMCOIN
                              ? exchangeUnit(SPAMCOIN)
                              : currencyLabel(value))}
                        </div>
                      </MenuItem>
                    ))}
                  </Select>
                </FormControl>
              ) : null}
              <Grid container sx={{ width: '100%', flexWrap: 'nowrap' }}>
                <Grid sx={{ width: satTotalMode ? '60%' : '100%' }}>
                  {satTotalMode ? (
                    <TextField
                      fullWidth
                      label={`${
                        makerHasAmountRange
                          ? t('In exchange for the maximum ({{unit}})', {
                              unit: exchangeUnit(payCurrency),
                            })
                          : t('In exchange for ({{unit}})', { unit: exchangeUnit(payCurrency) })
                      } *`}
                      type='number'
                      value={maker.exchangeTotal ?? ''}
                      helperText={
                        satPriceUnits === null
                          ? t('The total you want for your sats, whatever the market does')
                          : `${formatPricePerBtc(satPriceUnits, payCurrency)}${
                              makerHasAmountRange && maker.minAmount
                                ? ` · ${t('for the minimum: {{total}}', {
                                    total: formatTotal(
                                      totalForSats(maker.minAmount, satPriceUnits),
                                      payCurrency,
                                    ),
                                  })}`
                                : ''
                            }${
                              satVsMarket !== null
                                ? ` · ${satVsMarket > 0 ? '+' : ''}${satVsMarket}% ${t('vs market')}`
                                : ''
                            }`
                      }
                      slotProps={{
                        htmlInput: {
                          inputMode: 'decimal',
                          min: 0,
                          style: {
                            textAlign: 'center',
                            backgroundColor: theme.palette.background.paper,
                            borderRadius: '4px',
                          },
                        },
                      }}
                      onChange={(e) => {
                        const v = numberFromInput(e.target);
                        setMaker((maker) => ({ ...maker, exchangeTotal: v }));
                      }}
                    />
                  ) : (
                    <TextField
                      fullWidth
                      error={maker.badPremiumText !== ''}
                      helperText={
                        maker.badPremiumText !== ''
                          ? maker.badPremiumText
                          : marketPrice === null
                            ? t('No market price for this currency right now')
                            : satPriceUnits !== null
                              ? formatPricePerBtc(satPriceUnits, payCurrency)
                              : null
                      }
                      label={`${t('Premium over Market (%)')} *`}
                      type='number'
                      value={maker.premium ?? ''}
                      slotProps={{
                        input: {
                          endAdornment: <InputAdornment position='end'>%</InputAdornment>,
                        },
                        htmlInput: {
                          inputMode: 'decimal',
                          min: -100,
                          max: 999,
                          style: {
                            textAlign: 'center',
                            backgroundColor: theme.palette.background.paper,
                            borderRadius: '4px',
                          },
                        },
                      }}
                      onChange={handlePremiumChange}
                    />
                  )}
                </Grid>
                {satTotalMode ? (
                  <Grid sx={{ width: '40%' }}>
                    <Select
                      fullWidth
                      disabled={onlySpamcoinMethods}
                      sx={{ backgroundColor: theme.palette.background.paper, borderRadius: '4px' }}
                      value={payCurrency}
                      renderValue={(v) => exchangeUnit(Number(v))}
                      onChange={(e) => {
                        handlePayCurrencyChange(Number(e.target.value));
                      }}
                    >
                      {makerCurrencyOptions(currencyDict).map(([key, value]) => (
                        <MenuItem key={key} value={parseInt(key)}>
                          <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                            <FlagWithProps code={value} />
                            {' ' +
                              (Number(key) === SPAMCOIN
                                ? exchangeUnit(SPAMCOIN)
                                : currencyLabel(value))}
                          </div>
                        </MenuItem>
                      ))}
                    </Select>
                  </Grid>
                ) : null}
              </Grid>
            </Grid>
          ) : null}
          <Grid sx={{ width: '100%', display: satMode ? 'none' : undefined }}>
            <ToggleButtonGroup
              fullWidth
              exclusive
              size='small'
              color='primary'
              value={maker.priceMode}
              onChange={handlePriceModeChange}
              sx={{ mb: 1, backgroundColor: theme.palette.background.paper }}
            >
              <ToggleButton value='market' sx={{ textTransform: 'none' }}>
                {t('Market price + premium')}
              </ToggleButton>
              <ToggleButton value='fixed' disabled={!fixedAvailable} sx={{ textTransform: 'none' }}>
                {t('Fixed price')}
              </ToggleButton>
            </ToggleButtonGroup>
          </Grid>
          <Collapse in={fixedMode} sx={{ width: '100%' }}>
            <Grid sx={{ width: '100%' }}>
              <TextField
                fullWidth
                label={`${t('Price per BTC ({{unit}})', { unit: priceUnit(priceCurrency) })} *`}
                type='number'
                value={maker.fixedPrice ?? ''}
                helperText={
                  fixedPriceUnits === null
                    ? t('Your price does not move with the market: sats = amount / price')
                    : fixedVsMarket !== null
                      ? t('{{premium}}% vs market price (only for information)', {
                          premium: `${fixedVsMarket > 0 ? '+' : ''}${fixedVsMarket}`,
                        })
                      : t('No market price to compare right now: your price is used as it is')
                }
                slotProps={{
                  htmlInput: {
                    inputMode: 'decimal',
                    min: 0,
                    style: {
                      textAlign: 'center',
                      backgroundColor: theme.palette.background.paper,
                      borderRadius: '4px',
                    },
                  },
                }}
                onChange={handleFixedPriceChange}
              />
            </Grid>
          </Collapse>
          <Grid sx={{ width: '100%', display: fixedMode || satMode ? 'none' : undefined }}>
            <TextField
              fullWidth
              error={maker.badPremiumText !== ''}
              helperText={maker.badPremiumText === '' ? null : maker.badPremiumText}
              label={`${t('Premium over Market (%)')} *`}
              type='number'
              value={maker.premium ?? ''}
              slotProps={{
                htmlInput: {
                  inputMode: 'decimal',
                  min: -100,
                  max: 999,
                  style: {
                    textAlign: 'center',
                    backgroundColor: theme.palette.background.paper,
                    borderRadius: '4px',
                  },
                },
              }}
              onChange={handlePremiumChange}
            />
          </Grid>
          <Collapse in={maker.advancedOptions} sx={{ width: '100%' }}>
            <Grid sx={{ width: '100%' }}>
              <Tooltip
                placement='top'
                enterTouchDelay={300}
                enterDelay={700}
                enterNextDelay={2000}
                title={t('You can add more details about your order.')}
              >
                <TextField
                  fullWidth
                  label={`${t('Description')}`}
                  value={maker.description ?? ''}
                  style={{ marginBottom: 8 }}
                  slotProps={{
                    htmlInput: {
                      style: {
                        textAlign: 'center',
                        backgroundColor: theme.palette.background.paper,
                        borderRadius: 4,
                      },
                    },
                  }}
                  onChange={handleDescriptionChange}
                />
              </Tooltip>
            </Grid>

            {maker.badDescription && (
              <FormHelperText error={true}>
                {t('Must be equal to or shorter than 240 characters')}
              </FormHelperText>
            )}

            <Grid sx={{ width: '100%' }}>
              <Tooltip
                placement='top'
                enterTouchDelay={300}
                enterDelay={700}
                enterNextDelay={2000}
                title={t(
                  'You can optionally set a password so that only the robots you share it with can take this order.',
                )}
              >
                <TextField
                  fullWidth
                  label={`${t('Password')}`}
                  type='password'
                  value={maker.password ?? ''}
                  style={{ marginBottom: 8 }}
                  slotProps={{
                    htmlInput: {
                      style: {
                        textAlign: 'center',
                        backgroundColor: theme.palette.background.paper,
                        borderRadius: 4,
                      },
                    },
                  }}
                  onChange={handlePasswordChange}
                />
              </Tooltip>
            </Grid>

            <Grid sx={{ width: '100%' }}>
              <LocalizationProvider dateAdapter={AdapterDateFns}>
                <MobileTimePicker
                  open={openPublicDuration}
                  onClose={() => setOpenPublicDuration(false)}
                  ampm={false}
                  localeText={{ toolbarTitle: t('Public order length') }}
                  openTo='hours'
                  views={['hours', 'minutes']}
                  slotProps={{
                    textField: {
                      fullWidth: true,
                      onClick: () => setOpenPublicDuration(true),
                      slotProps: {
                        input: {
                          style: {
                            backgroundColor: theme.palette.background.paper,
                            borderRadius: '4px',
                            marginBottom: 8,
                          },
                        },
                      },
                    },
                  }}
                  label={t('Public Duration (HH:mm)')}
                  value={maker.publicExpiryTime}
                  onChange={(value: unknown) => {
                    if (value instanceof Date) handleChangePublicDuration(value);
                  }}
                  minTime={new Date(0, 0, 0, 0, 10)}
                  maxTime={new Date(0, 0, 0, 23, 59)}
                />
              </LocalizationProvider>
            </Grid>

            <Grid sx={{ width: '100%' }}>
              <LocalizationProvider dateAdapter={AdapterDateFns}>
                <MobileTimePicker
                  ampm={false}
                  open={openEscrowTimer}
                  onClose={() => setOpenEscrowTimer(false)}
                  localeText={{ toolbarTitle: t('Escrow/invoice step length') }}
                  openTo='hours'
                  views={['hours', 'minutes']}
                  format='HH:mm'
                  slotProps={{
                    textField: {
                      fullWidth: true,
                      onClick: () => setOpenEscrowTimer(true),
                      slotProps: {
                        input: {
                          style: {
                            backgroundColor: theme.palette.background.paper,
                            borderRadius: '4px',
                            marginBottom: 8,
                          },
                        },
                      },
                    },
                  }}
                  label={t('Escrow/Invoice Timer (HH:mm)')}
                  value={maker.escrowExpiryTime}
                  onChange={(value: unknown) => {
                    if (value instanceof Date) handleChangeEscrowDuration(value);
                  }}
                  minTime={new Date(0, 0, 0, 1, 0)}
                  maxTime={new Date(0, 0, 0, 10, 0)}
                />
              </LocalizationProvider>
            </Grid>

            <Grid sx={{ width: '100%', marginBottom: '8px' }}>
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
                <Grid
                  container
                  spacing={0.5}
                  sx={{ alignItems: 'center', flexDirection: 'column' }}
                >
                  <Grid
                    sx={{
                      width: '100%',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexWrap: 'wrap',
                    }}
                  >
                    <Tooltip
                      enterDelay={800}
                      enterTouchDelay={0}
                      placement='top'
                      title={t('Set the skin-in-the-game, increase for higher safety assurance')}
                    >
                      <Typography
                        variant='caption'
                        sx={{
                          color: 'text.secondary',
                          display: 'flex',
                          flexWrap: 'wrap',
                        }}
                      >
                        {t('Fidelity Bond Size')} <Lock sx={{ height: '0.8em', width: '0.8em' }} />
                      </Typography>
                    </Tooltip>
                  </Grid>
                  <Grid sx={{ width: 'calc(100% - 2em)' }}>
                    <Slider
                      sx={{ width: '100%', align: 'center' }}
                      aria-label='Bond Size (%)'
                      defaultValue={3}
                      value={maker.bondSize}
                      valueLabelDisplay='auto'
                      valueLabelFormat={(x: number) => x + '%'}
                      step={0.25}
                      marks={[
                        { value: 2, label: '2%' },
                        { value: 5, label: '5%' },
                        { value: 10, label: '10%' },
                        { value: 15, label: '15%' },
                      ]}
                      min={2}
                      max={15}
                      onChange={(_e, newValue) => {
                        setMaker({
                          ...maker,
                          bondSize: Array.isArray(newValue) ? newValue[0] : newValue,
                        });
                      }}
                    />
                  </Grid>
                </Grid>
              </Box>
            </Grid>
          </Collapse>
        </Grid>
      </Collapse>

      <SelectCoordinator
        coordinatorAlias={maker.coordinator}
        setCoordinatorAlias={(coordinatorAlias) => {
          setMaker((maker) => {
            return { ...maker, coordinator: coordinatorAlias };
          });
        }}
      />

      {onchainAvailable ? (
        <Tooltip
          enterTouchDelay={0}
          title={t(
            'Bonds and escrow on chain, without Lightning: nobody can move them alone, the trade takes longer (1 confirmation for each bond and for the deposit) and the coordinator fee is only paid when the trade is completed (a cancellation gives it back).',
          )}
        >
          <FormControlLabel
            sx={{ marginLeft: 0 }}
            control={
              <Switch
                checked={maker.onchainEscrow}
                onChange={(e) => {
                  setMaker({ ...maker, onchainEscrow: e.target.checked });
                }}
              />
            }
            label={t('Onchain escrow (no Lightning)')}
          />
        </Tooltip>
      ) : null}

      {/* 0.9.7: where the onchain bond goes back by itself once it is free (optional) */}
      {maker.onchainEscrow && onchainAvailable ? (
        <TextField
          fullWidth
          size='small'
          sx={{ mt: 1 }}
          label={t('Bond return address (optional)')}
          helperText={
            bondReturnCheck && !bondReturnCheck.ok
              ? addressErrorText(bondReturnCheck.error, t, bondReturnHrp)
              : t(
                  'Your bond comes back here by itself when it is free (1…, 3…, bc1q… or bc1p… addresses). Empty: your payout address.',
                )
          }
          error={bondReturnError}
          value={bondReturn}
          onChange={(e) => {
            setBondReturn(e.target.value.trim());
          }}
          slotProps={{ htmlInput: { 'data-testid': 'bond-return-address' } }}
        />
      ) : null}

      <Grid container sx={{ alignItems: 'center', flexDirection: 'column' }}>
        <Grid sx={{ marginBottom: '8px', marginTop: '8px' }}>
          <SummaryText />
        </Grid>

        <Grid>
          <Grid
            container
            direction='row'
            spacing={1}
            sx={{ alignItems: 'center', justifyItems: 'center' }}
          >
            <Grid>
              {/* conditions to disable the make button */}
              {disableSubmit ? (
                <Tooltip enterTouchDelay={0} title={getDisabledMessage()}>
                  <div>
                    <Button disabled color='primary' variant='contained' size='large'>
                      {t(submitButtonLabel)}
                    </Button>
                  </div>
                </Tooltip>
              ) : (
                <LoadingButton
                  loading={submittingRequest}
                  color='primary'
                  size='large'
                  variant='contained'
                  onClick={() => {
                    if (disableRequest) {
                      onSubmit();
                    } else if (garage.getSlot()?.hashId) {
                      setOpenStoreToken(true);
                    } else {
                      setOpenNoRobot(true);
                    }
                  }}
                >
                  {t(submitButtonLabel)}
                </LoadingButton>
              )}
            </Grid>
            {collapseAll ? (
              <Grid>
                <Collapse in={collapseAll} orientation='vertical'>
                  <IconButton onClick={onReset}>
                    <Tooltip
                      placement='top'
                      enterTouchDelay={500}
                      enterDelay={700}
                      enterNextDelay={2000}
                      title={t('Edit order')}
                    >
                      <Edit sx={{ width: '1.5em', height: '1.5em' }} />
                    </Tooltip>
                  </IconButton>
                </Collapse>
              </Grid>
            ) : null}
          </Grid>
        </Grid>

        <Grid>
          <Typography align='center' component='h2' variant='subtitle2' color='secondary'>
            {badRequest}
          </Typography>
        </Grid>

        <Tooltip
          placement='top'
          enterTouchDelay={0}
          enterDelay={1000}
          enterNextDelay={2000}
          title={
            fixedMode
              ? t('Your fixed price. It does not move with the market.')
              : t("Your order's current exchange rate. Rate will move with the market.")
          }
        >
          {satMode ? (
            <Typography align='center' variant='caption' color='text.secondary'>
              {satPriceUnits !== null
                ? `${satTotalMode ? t('Fixed price') : t('Order current rate:')} ${formatPricePerBtc(satPriceUnits, payCurrency)}`
                : '-'}
            </Typography>
          ) : fixedMode ? (
            <Typography align='center' variant='caption' color='text.secondary'>
              {`${t('Fixed price')}: ${fixedPriceUnits !== null ? formatPricePerBtc(fixedPriceUnits, priceCurrency) : '-'}`}
              {fixedVsMarket !== null
                ? ` (${fixedVsMarket > 0 ? '+' : ''}${fixedVsMarket}% ${t('vs market')})`
                : marketPrice === null
                  ? ` (${t('no market price to compare')})`
                  : ''}
            </Typography>
          ) : currentPrice ? (
            <Typography align='center' variant='caption' color='text.secondary'>
              {spamMode
                ? `${t('Order current rate:')} ${
                    marketPrice !== null
                      ? // 0.9.14-beta6: the pair as the order page shows it ("0.0036569 BTC/Spamcoin")
                        formatOrderPrice(marketPrice * (1 + (maker.premium ?? 0) / 100), SPAMCOIN)
                      : '-'
                  }`
                : `${t('Order current rate:')} ${currentPrice ? currencyFormatter.format(currentPrice) : '-'} ${currencyCode}/BTC`}
            </Typography>
          ) : (
            <Typography
              align='center'
              variant='caption'
              color='text.secondary'
              sx={{ display: 'flex', flexDirection: 'row', whiteSpace: 'nowrap', gap: '0.3em' }}
            >
              {t('Order current rate:')}
              <Skeleton width='4.5em' />
              {spamMode ? 'BTC/Spamcoin' : `${currencyCode}/BTC`}
            </Typography>
          )}
        </Tooltip>
      </Grid>
      <AddNewPaymentMethodDialog
        open={addNewPaymentMethodOpen}
        onClose={() => setAddNewPaymentMethodOpen(false)}
        onConfirm={handleAddNewPaymentMethod}
      />
    </Box>
  );
};

export default MakerForm;
