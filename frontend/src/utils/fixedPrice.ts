/**
 * Roblosats rl31-beta17: FIXED PRICE orders.
 *
 * The maker sets the price per BTC in the order's currency (fixed_price): fiat, or Spamcoin
 * for currency 1000 (shown to people as Spamsats per BTC = Spamcoin × 100,000,000). The sats
 * are amount / fixed_price and never depend on the coordinator's market price; the premium
 * against the market is only informative, and only when there is a market price.
 */
import { pn } from './prettyNumbers';
import currencies from './currencies';

export const SPAMCOIN_CODE = 1000;
const SUITS_PER_COIN = 100_000_000;

interface Priced {
  fixed_price?: number | string | null;
  premium?: number | string | null;
  currency?: number | null;
}

export const fixedPriceOf = (order: Priced | null | undefined): number | null => {
  const price = Number(order?.fixed_price ?? 0);
  return Number.isFinite(price) && price > 0 ? price : null;
};

export const isFixedPrice = (order: Priced | null | undefined): boolean =>
  fixedPriceOf(order) !== null;

/** The fixed price against the market price, in % (2 decimals); null without a market price. */
export const fixedPricePremium = (
  fixedPrice: number,
  marketPrice?: number | null,
): number | null => {
  if (!marketPrice || !(marketPrice > 0) || !(fixedPrice > 0)) return null;
  return Math.round((fixedPrice / marketPrice - 1) * 10000) / 100;
};

/** Price per BTC of a book order: its fixed price, or market × (1 + premium). */
export const orderPrice = (order: Priced, marketPrice?: number | null): number | null => {
  const fixed = fixedPriceOf(order);
  if (fixed !== null) return fixed;
  if (!marketPrice || !(marketPrice > 0)) return null;
  return marketPrice * (1 + parseFloat(String(order.premium ?? '0')) / 100);
};

/** Premium of a book order: its own, or (fixed price) against the market; null if unknown. */
export const orderPremium = (order: Priced, marketPrice?: number | null): number | null => {
  const fixed = fixedPriceOf(order);
  if (fixed !== null) return fixedPricePremium(fixed, marketPrice);
  return parseFloat(String(order.premium ?? '0'));
};

/** What people type and read: Spamsats per BTC for currency 1000, else the currency. */
export const priceUnit = (currency: number | null | undefined): string =>
  currency === SPAMCOIN_CODE ? 'Spamsats' : (currencies[String(currency ?? 1)] ?? '');

export const toDisplayPrice = (price: number, currency: number | null | undefined): number =>
  currency === SPAMCOIN_CODE ? price * SUITS_PER_COIN : price;

export const fromDisplayPrice = (price: number, currency: number | null | undefined): number =>
  currency === SPAMCOIN_CODE ? price / SUITS_PER_COIN : price;

export const formatPricePerBtc = (price: number, currency: number | null | undefined): string => {
  const shown = toDisplayPrice(price, currency);
  const rounded = shown >= 100 ? Math.round(shown) : parseFloat(shown.toPrecision(6));
  return `${pn(rounded)} ${priceUnit(currency)}/BTC`;
};

/**
 * A price as an exchange pair, for showing an order (26 nov 2026): with Spamcoin it is
 * "0.0036428 BTC/Spamcoin" (1 BTC of ours = 0.0036428 Spamcoin), as Neoxa and NonKYC quote it;
 * other currencies keep formatPricePerBtc. `price` is in the order's currency per BTC.
 */
export const formatOrderPrice = (price: number, currency: number | null | undefined): string => {
  if (currency !== SPAMCOIN_CODE) return formatPricePerBtc(price, currency);
  const shown = price >= 1 ? parseFloat(price.toFixed(4)) : parseFloat(price.toPrecision(5));
  return `${shown} BTC/Spamcoin`;
};

/** Sats for an amount at a fixed price (amount and price in the order's currency). */
export const fixedPriceSats = (amount: number, fixedPrice: number): number =>
  fixedPrice > 0 ? Math.round((amount / fixedPrice) * 100_000_000) : 0;
