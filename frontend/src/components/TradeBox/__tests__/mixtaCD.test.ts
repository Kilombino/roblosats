import { type Order } from '../../../models';
import { hybridBondStep, isOnchainEscrow, lnOnchainTakeStep } from '../onchainEscrowSteps';
import { defaultPayoutTab } from '../payoutOptions';

// 26 nov 2026, mixta C/D: a Lightning order taken with an onchain bond.
const order = (o: object): Order => o as unknown as Order;

describe('mixta C/D: Lightning order taken with an onchain bond', () => {
  it('the onchain take of a Lightning order shows the onchain bond prompt', () => {
    const o = { escrow_type: 0, status: 3, is_maker: false, take_onchain: true };
    expect(isOnchainEscrow(order(o))).toBe(false);
    expect(lnOnchainTakeStep(order(o))).toBe(true);
  });

  it('a Lightning take of a Lightning order keeps the hold invoice', () => {
    expect(lnOnchainTakeStep(order({ escrow_type: 0, status: 3, is_maker: false }))).toBe(false);
    expect(
      lnOnchainTakeStep(order({ escrow_type: 0, status: 3, is_maker: true, take_onchain: true })),
    ).toBe(false);
  });

  it('mixta C: the taker takes its onchain bond back once free', () => {
    const o = { escrow_type: 0, is_maker: false, hybrid_onchain_taker_bond: true };
    expect(hybridBondStep(order({ ...o, status: 14, bond_status_code: 5 }))).toBe(true);
    expect(hybridBondStep(order({ ...o, status: 9, bond_status_code: 2 }))).toBe(false);
    // the maker of a mixta C order has a Lightning bond: nothing onchain to take back
    expect(hybridBondStep(order({ ...o, is_maker: true, status: 14, bond_status_code: 5 }))).toBe(
      false,
    );
  });

  it('mixta C: the onchain-bond buyer is offered the onchain payout first', () => {
    const o = {
      status: 6,
      currency: 2,
      is_maker: false,
      is_buyer: true,
      swap_allowed: true,
      hybrid_onchain_taker_bond: true,
    };
    expect(defaultPayoutTab(order(o))).toBe('onchain');
    expect(defaultPayoutTab(order({ ...o, swap_allowed: false }))).toBe('lightning');
    expect(defaultPayoutTab(order({ ...o, hybrid_onchain_taker_bond: false }))).toBe('lightning');
  });
});
