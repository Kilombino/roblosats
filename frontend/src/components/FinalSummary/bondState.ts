/** 0.9.10: how the bond ended. Once it came back to an address (bond_returned_to), it is
 * returned, never "inside the escrow" or "still locked" any more. */
export const bondStateOf = (s: {
  bond_state?: string;
  bond_returned_to?: string;
}): string | undefined =>
  s.bond_returned_to && ['in_escrow', 'locked', '', undefined].includes(s.bond_state)
    ? 'returned'
    : s.bond_state;
