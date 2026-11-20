import { type Order } from '../../../models';
import { hybridBondStep, isOnchainEscrow, onchainEscrowStep } from '../onchainEscrowSteps';

const order = (o: object): Order => o as unknown as Order;

describe('onchain escrow steps with a Lightning taker (rl31-beta10)', () => {
  it('an onchain taker locks an onchain bond', () => {
    expect(onchainEscrowStep(order({ escrow_type: 'onchain', status: 3, is_maker: false }))).toBe(
      true,
    );
  });

  it('a Lightning taker of an onchain order gets the Lightning bond prompt', () => {
    expect(
      onchainEscrowStep(
        order({ escrow_type: 'onchain', status: 3, is_maker: false, take_lightning: true }),
      ),
    ).toBe(false);
    expect(
      onchainEscrowStep(
        order({ escrow_type: 'onchain', status: 3, is_maker: false, bond_invoice: 'lnbc1' }),
      ),
    ).toBe(false);
  });

  it('case B: the maker takes its onchain bond back once free, in a Lightning order', () => {
    const o = { escrow_type: 0, is_maker: true, hybrid_onchain_maker_bond: true };
    expect(isOnchainEscrow(order(o))).toBe(false);
    expect(hybridBondStep(order({ ...o, status: 14, bond_status_code: 5 }))).toBe(true);
    expect(hybridBondStep(order({ ...o, status: 13, bond_status_code: 2 }))).toBe(false);
    expect(hybridBondStep(order({ ...o, status: 14, bond_status_code: 5, is_maker: false }))).toBe(
      false,
    );
    expect(hybridBondStep(order({ escrow_type: 0, is_maker: true, status: 14 }))).toBe(false);
  });
});

// #26 (0.9.10): a release still being signed after the order finished keeps the onchain step.
describe('release still being signed after the order finished', () => {
  it.each([12, 14, 17, 18])(
    'status %s with the escrow releasing shows the onchain step',
    (status) => {
      expect(
        onchainEscrowStep(order({ escrow_type: 'onchain', status, escrow_status_code: 4 })),
      ).toBe(true);
    },
  );

  it('a released escrow at 14 does not', () => {
    expect(
      onchainEscrowStep(order({ escrow_type: 'onchain', status: 14, escrow_status_code: 5 })),
    ).toBe(false);
  });
});
