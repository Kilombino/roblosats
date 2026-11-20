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
