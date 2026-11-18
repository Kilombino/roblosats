import { maxExtraSats, myExtraInEscrow, owedInRelease, type EscrowOrderData } from '../client';

// rl31-beta15 fee plan 2: the fees ride in the escrow output; a collaborative cancel gives each
// trader its fee back, a completed trade pays them to the coordinator in the release.
const base = (over: Partial<EscrowOrderData>): EscrowOrderData =>
  ({
    escrow_release_kind: 'Seller and buyer (cooperative)',
    escrow_trade_sats: 100_000,
    escrow_sats: 100_000,
    escrow_bond_in_escrow: { seller: 3_000, buyer: 1_700 },
    escrow_fee_plan: 2,
    escrow_fees_in_escrow: { seller: 1_000, buyer: 1_000 },
    escrow_output_sats: 100_000 + 3_000 + 1_700 + 2_000,
    escrow_release_coordinator_sats: 0,
    ...over,
  }) as EscrowOrderData;

describe('owedInRelease, fee plan 2', () => {
  it('trade: the buyer gets trade + bond, the seller its bond', () => {
    expect(owedInRelease(base({ escrow_role: 'buyer' }))).toEqual({ due: 101_700, paysFee: true });
    expect(owedInRelease(base({ escrow_role: 'seller' }))).toEqual({ due: 3_000, paysFee: false });
  });

  it('cancel: each one gets its fee back', () => {
    const cancel = { escrow_release_cancel: true };
    expect(owedInRelease(base({ ...cancel, escrow_role: 'seller' }))).toEqual({
      due: 104_000,
      paysFee: true,
    });
    expect(owedInRelease(base({ ...cancel, escrow_role: 'buyer' }))).toEqual({
      due: 2_700,
      paysFee: false,
    });
  });

  it('refuses amounts that do not add up to the escrow', () => {
    expect(() =>
      owedInRelease(
        base({ escrow_role: 'seller', escrow_fees_in_escrow: { seller: 0, buyer: 1_000 } }),
      ),
    ).toThrow('the amounts of the release do not add up to the escrow');
  });

  it('refuses a fee held that is not what we agreed', () => {
    expect(() =>
      owedInRelease(
        base({ escrow_role: 'seller', escrow_release_cancel: true, escrow_funding_owed: 100_900 }),
      ),
    ).toThrow('the release does not give our fee back');
    expect(
      owedInRelease(
        base({ escrow_role: 'seller', escrow_release_cancel: true, escrow_funding_owed: 101_000 }),
      ),
    ).toEqual({ due: 104_000, paysFee: true });
  });

  it('hybrid A: the buyer fee comes out of the payout, nothing of the buyer is held', () => {
    const d = base({
      escrow_role: 'buyer',
      escrow_fee_at_release: true,
      escrow_fee_at_release_sats: 1_000,
      escrow_release_coordinator_sats: 1_000,
      escrow_bond_in_escrow: { seller: 3_000, buyer: 0 },
      escrow_fees_in_escrow: { seller: 1_000, buyer: 0 },
      escrow_output_sats: 104_000,
    });
    expect(owedInRelease(d)).toEqual({ due: 99_000, paysFee: true });
  });

  it('plan 1 escrows (beta14) keep their rules', () => {
    const d = base({
      escrow_role: 'seller',
      escrow_release_cancel: true,
      escrow_fee_plan: undefined,
      escrow_fees_in_escrow: undefined,
      escrow_output_sats: 104_700,
    });
    expect(owedInRelease(d)).toEqual({ due: 103_000, paysFee: true });
  });
});

// rl31-beta20: a funding excess too small for a change output rides in the escrow and goes back
// to its owner with its bond, in the trade release and in a collaborative cancel.
describe('owedInRelease, funding extras in the escrow', () => {
  const ex = (over: Partial<EscrowOrderData>): EscrowOrderData =>
    base({
      escrow_extra_in_escrow: { seller: 331, buyer: 400 },
      escrow_output_sats: 100_000 + 3_000 + 1_700 + 2_000 + 731,
      ...over,
    });

  it('trade: each gets its extra with its bond', () => {
    expect(owedInRelease(ex({ escrow_role: 'buyer' }))).toEqual({ due: 102_100, paysFee: true });
    expect(owedInRelease(ex({ escrow_role: 'seller' }))).toEqual({ due: 3_331, paysFee: false });
  });

  it('cancel: each gets its extra too', () => {
    const cancel = { escrow_release_cancel: true };
    expect(owedInRelease(ex({ ...cancel, escrow_role: 'seller' }))).toEqual({
      due: 104_331,
      paysFee: true,
    });
    expect(owedInRelease(ex({ ...cancel, escrow_role: 'buyer' }))).toEqual({
      due: 3_100,
      paysFee: false,
    });
  });

  it('refuses extras that do not add up to the escrow', () => {
    expect(() => owedInRelease(ex({ escrow_role: 'seller', escrow_output_sats: 106_700 }))).toThrow(
      'the amounts of the release do not add up to the escrow',
    );
    expect(() =>
      owedInRelease(
        ex({ escrow_role: 'seller', escrow_extra_in_escrow: { seller: -1, buyer: 732 } }),
      ),
    ).toThrow('the amounts of the release do not add up to the escrow');
  });
});

describe('our funding extra (rl31-beta20)', () => {
  it('is read for our role and bounded by what a change output would be worth', () => {
    const d = base({ escrow_role: 'buyer', escrow_extra_in_escrow: { seller: 331, buyer: 7 } });
    expect(myExtraInEscrow(d)).toBe(7);
    expect(myExtraInEscrow(base({ escrow_role: 'seller' }))).toBe(0);
    expect(maxExtraSats(50)).toBe(330 + 2 * 2150 + 2);
  });
});
