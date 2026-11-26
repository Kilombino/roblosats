import { checkUpdateWeb, clientVersionString, isNewer, readableNotes } from '../appUpdate';
import { getClientVersion } from '../checkVer';
import packageJson from '../../../package.json';

describe('isNewer (0.9.6 update notice)', () => {
  it('compares versions number by number', () => {
    expect(isNewer('0.9.6', '0.9.5')).toBe(true);
    expect(isNewer('v0.10.0', '0.9.9')).toBe(true);
    expect(isNewer('0.9.5', '0.9.5')).toBe(false);
    expect(isNewer('0.9.4', '0.9.5')).toBe(false);
  });
  it('a final release beats its own betas, not the other way round', () => {
    expect(isNewer('0.9.6', '0.9.6-beta2')).toBe(true);
    expect(isNewer('0.9.6-beta2', '0.9.6')).toBe(false);
  });
});

describe('readableNotes', () => {
  it('drops the SHA-256 block', () => {
    const body = '## Roblosats 0.9.6\n\n### Fixed\n- **a**\n\n### SHA-256\n```\nabc  x.apk\n```\n';
    expect(readableNotes(body)).toBe('Roblosats 0.9.6\n\nFixed\n- a');
  });
});

describe('checkUpdateWeb', () => {
  const answer = (o: object, ok = true): typeof fetch =>
    (async () => ({ ok, json: async () => o })) as unknown as typeof fetch;

  it('newer release', async () => {
    const r = await checkUpdateWeb(
      answer({ tag_name: 'v0.9.6', html_url: 'https://github.com/x', body: 'notes' }),
      '0.9.5',
    );
    expect(r).toEqual({
      status: 'newer',
      version: '0.9.6',
      current: '0.9.5',
      url: 'https://github.com/x',
      notes: 'notes',
      canInstall: false,
    });
  });
  it('up to date', async () => {
    expect((await checkUpdateWeb(answer({ tag_name: 'v0.9.5' }), '0.9.5')).status).toBe('uptodate');
  });
  it('GitHub down or blocked', async () => {
    expect((await checkUpdateWeb(answer({}, false), '0.9.5')).status).toBe('unreachable');
    const boom = (async () => {
      throw new Error('offline');
    }) as unknown as typeof fetch;
    expect((await checkUpdateWeb(boom, '0.9.5')).status).toBe('unreachable');
  });
});

describe('client version with a beta suffix (0.9.14-beta1)', () => {
  it('parses every part as a number, never NaN', () => {
    const { semver } = getClientVersion();
    for (const n of [semver.major, semver.minor, semver.patch]) expect(Number.isNaN(n)).toBe(false);
    expect(semver.patch).toBe(parseInt(packageJson.version.split('.')[2], 10));
  });
  it('keeps the suffix for the update check, so a beta is offered its final', () => {
    expect(clientVersionString()).toBe(packageJson.version);
    if (packageJson.version.includes('-')) {
      expect(isNewer(packageJson.version.split('-')[0], clientVersionString())).toBe(true);
      expect(isNewer('0.9.13', clientVersionString())).toBe(false);
    }
  });
});
