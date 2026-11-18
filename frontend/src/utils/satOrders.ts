/**
 * Roblosats rl31-beta19: "丰 sat" orders.
 *
 * The amount (or range) is in sats of BTC (BLAKE2b); the order's currency is what is paid in
 * exchange. The maker writes the total it wants in exchange in its own unit (Spamsats for
 * Spamcoin, else the currency), so the price per BTC is total / sats and stays the same along
 * a range; optionally the price is the market price + premium.
 *
 * Shapes: the book (Nostr and /api/book) carries amounts in the payment currency (as every
 * NIP-69 client reads them) and the sats in sats_amount / sats_min_amount / sats_max_amount;
 * the order page carries amounts in sats and exchange_amount / exchange_min_amount /
 * exchange_max_amount in the currency.
 */
import { pn } from './prettyNumbers';
import currencies, { SPAMCOIN, currencyOptions, currencyLabel } from './currencies';

export const SAT_LABEL = '丰 sat';
export const SAT_SYMBOL = '丰';
/** The value the maker-form currency selector uses for "丰 sat". */
export const SAT_OPTION = -1;
const SATS_PER_COIN = 100_000_000;

/** Maker form: EUR, USD, then the others in their usual order (Spamcoin first). */
export const makerCurrencyOptions = (dict: Record<string, string>): Array<[string, string]> => {
  const all = currencyOptions(dict);
  const first = ['2', '1'].map((k) => all.find(([key]) => key === k)).filter(Boolean) as Array<
    [string, string]
  >;
  return [...first, ...all.filter(([key]) => key !== '1' && key !== '2')];
};

/** The unit people use for what is paid in exchange: Spamsats for Spamcoin, else the currency. */
export const exchangeUnit = (currency: number | null | undefined): string =>
  currency === SPAMCOIN ? 'Spamsats' : currencyLabel(currencies[String(currency ?? 1)] ?? '');

/** A total in the order's currency → as people read it (Spamsats for Spamcoin). */
export const toDisplayTotal = (units: number, currency: number | null | undefined): number =>
  currency === SPAMCOIN ? units * SATS_PER_COIN : units;

export const fromDisplayTotal = (shown: number, currency: number | null | undefined): number =>
  currency === SPAMCOIN ? shown / SATS_PER_COIN : shown;

/** Price per BTC (order's currency) from a total paid in exchange for some sats. */
export const priceFromTotal = (totalUnits: number, sats: number): number | null =>
  totalUnits > 0 && sats > 0 ? (totalUnits * SATS_PER_COIN) / sats : null;

/** What is paid in exchange for some sats at a price per BTC (order's currency). */
export const totalForSats = (sats: number, pricePerBtc: number): number =>
  (sats / SATS_PER_COIN) * pricePerBtc;

export const formatSats = (sats: number | string): string =>
  `${pn(Math.round(Number(sats)))} ${SAT_LABEL}`;

export const formatSatRange = (min: number | string, max: number | string): string =>
  `${pn(Math.round(Number(min)))}-${pn(Math.round(Number(max)))} ${SAT_LABEL}`;

/** A total in the order's currency as people read it, e.g. "50.000 Spamsats" or "12,5 EUR". */
export const formatTotal = (units: number, currency: number | null | undefined): string => {
  const shown = toDisplayTotal(units, currency);
  const rounded = currency === SPAMCOIN ? Math.round(shown) : Math.round(shown * 100) / 100;
  return `${pn(rounded)} ${exchangeUnit(currency)}`;
};

export interface SatFields {
  sats_denominated?: boolean;
  sats_amount?: number | string | null;
  sats_min_amount?: number | string | null;
  sats_max_amount?: number | string | null;
}

export const isSatOrder = (o: SatFields | null | undefined): boolean =>
  Boolean(o?.sats_denominated);

/** The sats of a book order: [one amount] or [min, max]; null if it is not a sat order. */
export const bookSats = (o: SatFields & { has_range?: boolean }): number[] | null => {
  if (!isSatOrder(o)) return null;
  const vals = o.has_range ? [o.sats_min_amount, o.sats_max_amount] : [o.sats_amount];
  const nums = vals.map((v) => Number(v ?? NaN));
  return nums.every((n) => Number.isFinite(n) && n > 0) ? nums : null;
};

/** Nostr: ["sats", <sats>] or ["sats", <min>, <max>] marks a sat order (fa = what is paid). */
export const applySatsTag = <T extends SatFields>(order: T, tag: string[]): T => {
  if (tag[0] !== 'sats' || !tag[1]) return order;
  order.sats_denominated = true;
  if (tag[2]) {
    order.sats_min_amount = tag[1];
    order.sats_max_amount = tag[2];
  } else {
    order.sats_amount = tag[1];
  }
  return order;
};
