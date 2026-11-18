import { buildReport, pushLog, recentLines, redact } from '../logs';

describe('redact (0.9.6 recent logs)', () => {
  it('removes robot tokens and the Authorization header', () => {
    const token = 'Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb7c';
    expect(redact(`token ${token} end`)).not.toContain(token);
    expect(redact('Authorization: Token 9f8e7d6c5b4a | Public x')).toBe('Authorization: [redacted]');
    expect(redact('{"Authorization":"Token abcdef1234567890"}')).not.toContain('abcdef1234567890');
  });
  it('removes PGP blocks, even cut ones', () => {
    const pgp = '-----BEGIN PGP PRIVATE KEY BLOCK-----\nxyz\n-----END PGP PRIVATE KEY BLOCK-----';
    expect(redact(`a ${pgp} b`)).toBe('a [PGP block redacted] b');
    expect(redact('-----BEGIN PGP MESSAGE-----\nhalf')).toBe('[PGP block redacted]');
  });
  it('removes escrow keys, preimages and txids (hex)', () => {
    const hex = 'a'.repeat(64);
    expect(redact(`key=${hex}`)).toBe('key=[hex redacted]');
    const pub = '02' + 'b'.repeat(64);
    expect(redact(pub)).toBe('[hex redacted]');
  });
  it('removes invoices, nsec and addresses', () => {
    expect(redact('pay lnbc10u1pjk2abcdefghijklmnopqrstuvwxyz0123456789')).toBe('pay [invoice redacted]');
    expect(redact('lnbcrt500u1qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfq')).toBe('[invoice redacted]');
    expect(redact('nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq')).toBe('[nsec redacted]');
    expect(redact('to bc1qdm9jqkl56gxkwyvytqdzw4etxywqvewxu8clsm now')).toBe('to [address redacted] now');
  });
  it('keeps ordinary log lines readable', () => {
    const line = 'Order 24 status 13: escrow RELEASING, waiting for the buyer';
    expect(redact(line)).toBe(line);
  });
});

describe('log capture and report', () => {
  it('stores lines already redacted', () => {
    pushLog('INFO', ['robot token', 'Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8Nn']);
    const last = recentLines().slice(-1)[0];
    expect(last).toContain('INFO robot token [secret redacted]');
  });
  it('the report has the crash and both logs, redacted', () => {
    const r = buildReport('v0.9.6', {
      logs: `11-18 21:00 D Robosats: invoice lntb1u1pjxyzxyzxyzxyzxyzxyzxyzxyz`,
      crash: `java.lang.IllegalStateException at x\nkey ${'c'.repeat(64)}`,
      version: '0.9.6',
      android: '15',
      device: 'Google Pixel',
    });
    expect(r).toContain('Roblosats v0.9.6 (app 0.9.6)');
    expect(r).toContain('Android 15 · Google Pixel');
    expect(r).toContain('IllegalStateException');
    expect(r).toContain('[invoice redacted]');
    expect(r).toContain('[hex redacted]');
    expect(r).not.toContain('c'.repeat(64));
  });
});
