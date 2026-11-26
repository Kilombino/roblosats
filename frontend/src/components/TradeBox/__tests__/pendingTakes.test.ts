import { pendingTakeNotice, pendingTakeTxs } from '../pendingTakes';
import { explorerTxUrl } from '../../FinalSummary/bondState';
import { type Order } from '../../../models';
import en from '../../../../static/locales/en.json';
import es from '../../../../static/locales/es.json';
import ca from '../../../../static/locales/ca.json';

const order = (o: object): Order => ({ status: 1, is_maker: true, ...o }) as unknown as Order;

describe('pending onchain takes on the maker\'s public order (TG 15618)', () => {
  it('says nothing without a bond in the mempool', () => {
    expect(pendingTakeNotice(order({}))).toBeNull();
    expect(pendingTakeNotice(order({ pending_onchain_takes: { mempool: 0, confirmed: 0, awaiting_payment: 3 } }))).toBeNull();
  });
  it('one take in the mempool, several, and a confirmed one', () => {
    expect(pendingTakeNotice(order({ pending_onchain_takes: { mempool: 1 } }))?.key).toMatch(/^A robot is taking your order/);
    const many = pendingTakeNotice(order({ pending_onchain_takes: { mempool: 3 } }));
    expect(many?.count).toBe(3);
    expect(many?.key).toMatch(/^\{\{n\}\} robots/);
    expect(pendingTakeNotice(order({ pending_onchain_takes: { mempool: 2, confirmed: 1 } }))?.key).toMatch(/is confirmed/);
  });
  it('only for the maker of a public order', () => {
    const p = { pending_onchain_takes: { mempool: 1 } };
    expect(pendingTakeNotice(order({ ...p, is_maker: false }))).toBeNull();
    expect(pendingTakeNotice(order({ ...p, status: 3 }))).toBeNull();
  });
  it('every text is in en, es and ca', () => {
    for (const p of [{ mempool: 1 }, { mempool: 2 }, { confirmed: 1 }]) {
      const key = pendingTakeNotice(order({ pending_onchain_takes: p }))!.key;
      for (const d of [en, es, ca] as Array<Record<string, string>>) expect(d[key]).toBeTruthy();
    }
  });
});

describe('the bond transactions of the pending takes (TG 15622)', () => {
  const a = 'aa'.repeat(32);
  const b = 'bb'.repeat(32);
  it('one line per txid, confirmed first, each linking its own transaction', () => {
    const txs = pendingTakeTxs(
      order({
        pending_onchain_takes: {
          mempool: 1,
          confirmed: 1,
          txs: [
            { txid: a, stage: 'mempool' },
            { txid: b, stage: 'confirmed' },
            { txid: a, stage: 'mempool' },
            { txid: 'not-a-txid', stage: 'mempool' },
          ],
        },
      }),
    );
    expect(txs.map((x) => x.txid)).toEqual([b, a]);
    expect(txs.map((x) => explorerTxUrl(x.txid))).toEqual([explorerTxUrl(b), explorerTxUrl(a)]);
    expect(explorerTxUrl(a).endsWith(a)).toBe(true);
    for (const x of txs) for (const d of [en, es, ca] as Array<Record<string, string>>) expect(d[x.labelKey]).toBeTruthy();
  });
  it('nothing for others or without txs', () => {
    expect(pendingTakeTxs(order({ pending_onchain_takes: { mempool: 1 } }))).toEqual([]);
    expect(pendingTakeTxs(order({ is_maker: false, pending_onchain_takes: { txs: [{ txid: a, stage: 'mempool' }] } }))).toEqual([]);
  });
});
