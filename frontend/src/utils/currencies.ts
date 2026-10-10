/**
 * Typed accessor for static/assets/currencies.json.
 *
 * currencies.json uses integer keys (as strings) indexing into the currency list.
 * `resolveJsonModule` gives the JSON a literal-key type — every numeric lookup
 * requires a cast at the call site.  Import from here instead to get a single
 * properly-typed `Record<string, string>` without scattering inline casts.
 */
import rawCurrencies from '../../static/assets/currencies.json';

const currencies: Record<string, string> = rawCurrencies as unknown as Record<string, string>;

export default currencies;

/**
 * Roblosats: the currency lists as shown in the selectors. Code 1000 ("BTC", the swap
 * currency) is the SHA-256 chain's coin here, Spamcoin, paid on-chain or over its
 * Lightning: it goes first and reads "Spamcoin".
 */
export const SPAMCOIN = 1000;
export const currencyOptions = (dict: Record<string, string>): Array<[string, string]> => {
  const entries = Object.entries(dict);
  const spam = entries.filter(([key]) => Number(key) === SPAMCOIN);
  return [...spam, ...entries.filter(([key]) => Number(key) !== SPAMCOIN)];
};
export const currencyLabel = (code: string): string => (code === 'BTC' ? 'Spamcoin' : code);
