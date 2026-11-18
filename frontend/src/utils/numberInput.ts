// Numeric form inputs (rl31-beta20, also an upstream RoboSats bug): an input cleared by the user
// must hold '' (null in the state), never Number('') = 0, which left a "0" that could not be
// erased and turned typing into "01". Leading zeros are stripped as the user types.

type NumberField = HTMLInputElement | HTMLTextAreaElement;

/** The input's text without leading zeros ("01" → "1", "-05" → "-5", "0.5" stays), also
 * written back to the field (a number input keeps "01" on screen otherwise). */
export const cleanNumberInput = (el: NumberField): string => {
  const raw = el.value ?? '';
  const v = raw.replace(/^(-?)0+(?=\d)/, '$1');
  if (v !== raw) {
    try {
      el.value = v;
    } catch {
      // some test doubles have a read-only value
    }
  }
  return v;
};

/** null for an empty or non-numeric text, else the number. */
export const parseNumberInput = (raw: string): number | null => {
  const s = (raw ?? '').trim();
  if (s === '' || s === '-' || s === '.') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};

/** Both: what a numeric onChange stores (null when cleared). */
export const numberFromInput = (el: NumberField): number | null =>
  parseNumberInput(cleanNumberInput(el));
