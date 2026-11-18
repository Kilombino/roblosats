/**
 * Roblosats 0.9.1: Spamcoin (currency 1000) in the maker form.
 *
 * Upstream RoboSats uses currency 1000 as its "BTC" swap pseudo-currency and switches the
 * maker form to swap mode for it. In Roblosats 1000 is Spamcoin, an ordinary payment unit:
 * the form keeps BUY/SELL and people write the amount in Spamsats (whole numbers), while the
 * API keeps the amount in Spamcoin with up to 8 decimals (e.g. 0.00028 for 28,000 Spamsats),
 * as every Spamcoin order before.
 */
import { SPAMCOIN } from './currencies';

const SPAMSATS_PER_COIN = 100_000_000;

/** A plain Spamcoin order (not a "丰 sat" order): its amounts are typed in Spamsats. */
export const isSpamAmountCurrency = (currency: number | null | undefined): boolean =>
  currency === SPAMCOIN;

/** How many form units make one API unit: 100,000,000 for Spamcoin, else 1. */
export const amountScale = (currency: number | null | undefined): number =>
  isSpamAmountCurrency(currency) ? SPAMSATS_PER_COIN : 1;

/** An API amount (order's currency) → as the form shows it (Spamsats for Spamcoin). */
export const toFormAmount = (
  units: number | string | null | undefined,
  currency: number | null | undefined,
): number | null => {
  if (units === null || units === undefined || units === '') return null;
  const n = Number(units);
  if (!Number.isFinite(n)) return null;
  return isSpamAmountCurrency(currency) ? Math.round(n * SPAMSATS_PER_COIN) : n;
};

/** A form amount → the API amount (Spamcoin with 8 decimals for currency 1000). */
export const toApiAmount = (
  shown: number | null | undefined,
  currency: number | null | undefined,
): number | null => {
  if (shown === null || shown === undefined || !Number.isFinite(shown)) return null;
  if (!isSpamAmountCurrency(currency)) return shown;
  return Number((Math.round(shown) / SPAMSATS_PER_COIN).toFixed(8));
};

/** The unit people read next to a form amount: Spamsats for Spamcoin, else the code. */
export const formAmountUnit = (currency: number | null | undefined, code: string): string =>
  isSpamAmountCurrency(currency) ? 'Spamsats' : code;

interface SummaryAmount {
  currency?: number | null;
  amount?: string | null;
  contract?: boolean;
  trade_sats?: number;
  sats_denominated?: boolean;
}

/**
 * The amount line of a final summary. Spamcoin reads in Spamsats; a "丰 sat" order carries its
 * sats as the amount (its currency is what was paid in exchange), known from the order page or,
 * in the past orders list, from the amount matching the trade sats (a Spamcoin amount of a whole
 * coin or more cannot be a plain Spamcoin order either).
 */
export const summaryAmountText = (
  s: SummaryAmount,
  currencyCode: string,
  satsDenominated?: boolean,
  format: (n: number) => string = (n) => String(n),
): string => {
  const n = Number(s.amount ?? 0);
  const satOrder =
    satsDenominated ??
    s.sats_denominated ??
    ((Boolean(s.contract) && (s.trade_sats ?? 0) > 0 && Math.round(n) === s.trade_sats) ||
      (isSpamAmountCurrency(s.currency) && n >= 1));
  if (satOrder) return `${format(Math.round(n))} 丰 sat`;
  if (isSpamAmountCurrency(s.currency))
    return `${format(toFormAmount(n, s.currency) ?? 0)} Spamsats`;
  return `${format(n)} ${currencyCode}`;
};
