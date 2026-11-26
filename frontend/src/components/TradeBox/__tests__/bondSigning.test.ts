import { SIGN_SLOW_MS, bondSigningLines } from '../onchainEscrowSteps';
import { pendingTakeNotice, pendingTakeTxs } from '../pendingTakes';
import { formatOrderPrice } from '../../../utils/fixedPrice';
import { type Order } from '../../../models';
import en from '../../../../static/locales/en.json';
import es from '../../../../static/locales/es.json';
import ca from '../../../../static/locales/ca.json';

const dicts = [en, es, ca] as Array<Record<string, string>>;

// 0.9.14-beta6 (Kilombino TG 15636/15637)
describe('a confirmed bond waiting for the app to sign', () => {
  it('says confirmed and signing, then asks to keep the app open after ~20 s', () => {
    expect(bondSigningLines(1_000, 1_000).main).toBe('✅ Confirmed · ✍️ signing…');
    expect(bondSigningLines(1_000, 1_000 + SIGN_SLOW_MS - 1).slow).toBeNull();
    expect(bondSigningLines(1_000, 1_000 + SIGN_SLOW_MS).slow).toBe('Keep the app open: your signature is missing');
    expect(bondSigningLines(null, 99_999).slow).toBeNull();
  });
  it('has its texts in en, es and ca', () => {
    const l = bondSigningLines(0, SIGN_SLOW_MS);
    for (const d of dicts) {
      expect(d[l.main]).toBeTruthy();
      expect(d[l.slow!]).toBeTruthy();
    }
  });
});

describe("the maker's notice: confirmed, waiting for the taker's signature", () => {
  const order = { status: 1, is_maker: true, pending_onchain_takes: { mempool: 0, confirmed: 1, txs: [{ txid: 'cd'.repeat(32), stage: 'confirmed' }] } } as unknown as Order;
  it('says so in the notice and in the transaction line, in three languages', () => {
    const notice = pendingTakeNotice(order)!;
    const tx = pendingTakeTxs(order)[0];
    expect(notice.key).toMatch(/waiting for the taker's signature/);
    expect(tx.labelKey).toMatch(/waiting for the taker's signature/);
    for (const d of dicts) {
      expect(d[notice.key]).toBeTruthy();
      expect(d[tx.labelKey]).toBeTruthy();
    }
  });
});

describe('maker form current price in Spamsats orders', () => {
  it('reads as the order page pair (BTC/Spamcoin)', () => {
    expect(formatOrderPrice(0.0036569, 1000)).toBe('0.0036569 BTC/Spamcoin');
  });
});
