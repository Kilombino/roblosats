import { MAX_STATEMENT, defaultDispute, statementWithChat } from '../Prompts/disputeStatement';

// 26 nov 2026 (Kilombino TG 15581/15584): the chat is attached by default; a long one goes apart.
describe('dispute statement with the chat', () => {
  it('attaches the chat by default', () => {
    expect(defaultDispute.attachLogs).toBe(true);
  });

  it('a short chat rides inside the statement, as every coordinator reads it', () => {
    const r = statementWithChat('my statement', [{ index: 1, plainTextMessage: 'hi' }], true);
    expect(r.chatLog).toBeUndefined();
    expect(JSON.parse(r.statement)).toEqual({
      statement: 'my statement',
      messages: [{ index: 1, plainTextMessage: 'hi' }],
    });
  });

  it('a long chat goes apart and the statement stays plain', () => {
    const big = Array.from({ length: 2000 }, (_, i) => ({
      index: i,
      plainTextMessage: 'x'.repeat(40),
    }));
    const r = statementWithChat('my statement', big, true);
    expect(JSON.stringify({ statement: 'my statement', messages: big }).length).toBeGreaterThan(
      MAX_STATEMENT,
    );
    expect(r.statement).toBe('my statement');
    expect(JSON.parse(r.chatLog ?? '[]')).toHaveLength(2000);
  });

  it('unticked: only the statement', () => {
    expect(statementWithChat('s', [{ index: 1 }], false)).toEqual({ statement: 's' });
  });
});
