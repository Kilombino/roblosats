/** 0.9.10: how the bond ended. Once it came back to an address (bond_returned_to), it is
 * returned, never "inside the escrow" or "still locked" any more. */
export const bondStateOf = (s: {
  bond_state?: string;
  bond_returned_to?: string;
}): string | undefined =>
  s.bond_returned_to && ['in_escrow', 'locked', '', undefined].includes(s.bond_state)
    ? 'returned'
    : s.bond_state;

/** 26 nov 2026 (TG 15596): why each reward of the order was credited (all Lightning rewards). */
export const rewardReasonKey = (reason: string): string => {
  switch (reason) {
    case 'trade_amount':
      return 'Trade amount (Lightning reward)';
    case 'escrow_back':
      return 'Your escrow back (Lightning reward)';
    case 'bond_value':
      return "Your bond's value (Lightning reward)";
    case 'other_bond_share':
      return "Part of the other party's bond (Lightning reward)";
    case 'bond_difference':
      return 'Rest of your bond (Lightning reward)';
    case 'swap_surcharge_refund':
      return 'Refund of your onchain swap surcharge, no swap (Lightning reward)';
    default:
      return 'Other reward (Lightning)';
  }
};

/** 26 nov 2026: the explorer page of one transaction (each link uses its own txid). */
export const EXPLORER_TX = 'https://mempool.kilombino.com/tx/';
export const explorerTxUrl = (txid: string): string => `${EXPLORER_TX}${txid}`;
