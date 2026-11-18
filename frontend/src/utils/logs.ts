// 0.9.6: "Recent logs" (Settings): what the app logged lately and its last crash, to send when
// something breaks. Everything goes through redact() before it is shown, copied or shared.

const MAX_LINES = 600;
const lines: string[] = [];
let installed = false;

const REDACTIONS: Array<[RegExp, string]> = [
  // PGP armored blocks (robot keys, signed messages)
  [/-----BEGIN PGP [A-Z ]+-----[\s\S]*?-----END PGP [A-Z ]+-----/g, '[PGP block redacted]'],
  // a PGP block cut by the log (no END line)
  [/-----BEGIN PGP [A-Z ]+-----[\s\S]*/g, '[PGP block redacted]'],
  // Authorization headers: robot token hash and keys
  [/(Authorization["']?\s*[:=]\s*["']?)[^"'\n]+/gi, '$1[redacted]'],
  [/\bToken\s+[A-Za-z0-9+/=_\-|.:;!@#$%^&*()~]{8,}/g, 'Token [redacted]'],
  // Nostr secret keys
  [/\bnsec1[02-9ac-hj-np-z]{20,}\b/gi, '[nsec redacted]'],
  // Lightning invoices and offers (any network)
  [/\bln(?:bc|tb|bcrt|tbs|sb|o)[0-9a-z]{20,}\b/gi, '[invoice redacted]'],
  // onchain addresses (bech32)
  [/\b(?:bc|tb|bcrt)1[02-9ac-hj-np-z]{20,}\b/gi, '[address redacted]'],
  // hex keys, secrets, preimages, txids (32 bytes or more)
  [/\b[0-9a-fA-F]{64,}\b/g, '[hex redacted]'],
  // tokens and other long base62/base64 secrets (robot tokens are 36+ characters)
  [/\b[A-Za-z0-9+/_-]{32,}={0,2}/g, '[secret redacted]'],
];

/** The text with tokens, keys, PGP blocks, invoices and addresses removed. */
export const redact = (text: string): string =>
  REDACTIONS.reduce((acc, [re, by]) => acc.replace(re, by), text);

const stamp = (): string => new Date().toISOString().slice(11, 23);

const asText = (args: unknown[]): string =>
  args
    .map((a) => {
      if (typeof a === 'string') return a;
      if (a instanceof Error) return `${a.name}: ${a.message}`;
      try {
        return JSON.stringify(a);
      } catch {
        return String(a);
      }
    })
    .join(' ');

export const pushLog = (level: string, args: unknown[]): void => {
  // stored already redacted: nothing secret stays in memory for longer than the call
  lines.push(`${stamp()} ${level} ${redact(asText(args)).slice(0, 2000)}`);
  if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
};

const remember = (key: string, value: string): void => {
  try {
    localStorage.setItem(key, value);
  } catch {
    // storage may be off (private window)
  }
};

/** Keeps the last console lines and the last JS error (both redacted). Once. */
export const installLogCapture = (): void => {
  if (installed || typeof window === 'undefined') return;
  installed = true;
  for (const level of ['log', 'info', 'warn', 'error'] as const) {
    const original = console[level].bind(console);
    console[level] = (...args: unknown[]) => {
      pushLog(level.toUpperCase(), args);
      original(...args);
    };
  }
  window.addEventListener('error', (e) => {
    const text = redact(
      `${new Date().toISOString()} ${e.message} at ${e.filename}:${e.lineno}\n${(e.error as Error | undefined)?.stack ?? ''}`,
    );
    pushLog('CRASH', [text]);
    remember('last_js_error', text);
  });
  window.addEventListener('unhandledrejection', (e) => {
    const reason = e.reason as Error | string | undefined;
    const text = redact(
      `${new Date().toISOString()} unhandled rejection: ${typeof reason === 'string' ? reason : `${reason?.message ?? ''}\n${reason?.stack ?? ''}`}`,
    );
    pushLog('CRASH', [text]);
    remember('last_js_error', text);
  });
};

export const recentLines = (): string[] => [...lines];

export const lastJsError = (): string => {
  try {
    return localStorage.getItem('last_js_error') ?? '';
  } catch {
    return '';
  }
};

export interface NativeLogs {
  logs?: string;
  crash?: string;
  version?: string;
  android?: string;
  device?: string;
}

/** The whole report, redacted. */
export const buildReport = (clientVersion: string, native: NativeLogs | null): string => {
  const parts = [
    `Roblosats ${clientVersion}${native?.version ? ` (app ${native.version})` : ''}`,
    native?.android
      ? `Android ${native.android} · ${native.device ?? ''}`
      : `Browser: ${navigator.userAgent}`,
    '',
    '== Last crash ==',
    native?.crash && native.crash.trim() !== '' ? native.crash : lastJsError() || '(none)',
    '',
    '== App (web) log ==',
    ...recentLines(),
  ];
  if (native?.logs) parts.push('', '== Android log ==', native.logs);
  return redact(parts.join('\n'));
};
