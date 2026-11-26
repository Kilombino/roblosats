import { bondStateOf } from '../bondState';

// 0.9.10: the final summary says where the bond came back, and never "inside the escrow" then.
describe('bond state in the final summary', () => {
  it('a bond that came back to an address is returned', () => {
    expect(bondStateOf({ bond_state: 'in_escrow', bond_returned_to: 'bc1qxyz' })).toBe('returned');
    expect(bondStateOf({ bond_state: 'locked', bond_returned_to: 'bc1qxyz' })).toBe('returned');
    expect(bondStateOf({ bond_returned_to: 'bc1qxyz' })).toBe('returned');
  });

  it('otherwise the coordinator state stays', () => {
    expect(bondStateOf({ bond_state: 'in_escrow', bond_returned_to: '' })).toBe('in_escrow');
    expect(bondStateOf({ bond_state: 'in_escrow' })).toBe('in_escrow');
    expect(bondStateOf({ bond_state: 'lost', bond_returned_to: 'bc1qxyz' })).toBe('lost');
    expect(bondStateOf({ bond_state: 'rewards', bond_returned_to: 'bc1qxyz' })).toBe('rewards');
  });
});

// 26 nov 2026 (#29): rewards by reason, the bond credited back, and one explorer link per txid.
import { explorerTxUrl, rewardReasonKey } from '../bondState';

describe('final summary: rewards, credited bond and explorer links', () => {
  it('a bond penalized but credited back keeps its own state', () => {
    expect(bondStateOf({ bond_state: 'credited' })).toBe('credited');
  });

  it('every reward reason says it is a Lightning reward', () => {
    for (const r of [
      'trade_amount',
      'escrow_back',
      'bond_value',
      'other_bond_share',
      'bond_difference',
      'swap_surcharge_refund',
      'whatever',
    ]) {
      expect(rewardReasonKey(r)).toMatch(/Lightning/);
    }
    expect(rewardReasonKey('swap_surcharge_refund')).toMatch(/swap surcharge/);
  });

  it('each transaction links to its own txid (the penalty is not the bond payment)', () => {
    const bond = '93c66f8f81565ef4d410df48b0dc9c3fca504f11f37e7909f94f069b6fb9edba';
    const penalty = 'da4bc3bca4890c523c7ebc2ab7bd3a7513f36a96d93649df1a5230b66a33fef0';
    expect(explorerTxUrl(bond)).toBe(`https://mempool.kilombino.com/tx/${bond}`);
    expect(explorerTxUrl(penalty)).toBe(`https://mempool.kilombino.com/tx/${penalty}`);
    expect(explorerTxUrl(penalty)).not.toBe(explorerTxUrl(bond));
  });
});
