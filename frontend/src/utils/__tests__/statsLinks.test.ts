import { commitUrl, nodexNodeUrl, runningCommit } from '../statsLinks';

// 0.9.15 (Kilombino TG 15665): Stats for Nerds links
describe('coordinator stats links', () => {
  it("links the coordinator's node to its Nodex page", () => {
    const pk = '0222d36d7b634ead665c982145f6e4507ad65c4f369902ab3ca11e379d2139dc60';
    expect(nodexNodeUrl(pk)).toBe(`https://nodex.kilombino.com/node/${pk}`);
  });
  it('links the running commit to Kilombino/roblosats, or nothing for the placeholder', () => {
    const sha = '1389a6fd0123456789abcdef0123456789abcdef';
    expect(runningCommit(sha)).toBe(sha);
    expect(runningCommit(`${sha}\n`)).toBe(sha);
    expect(commitUrl(sha)).toBe(`https://github.com/Kilombino/roblosats/commit/${sha}`);
    expect(runningCommit('00000000000000000000 dev')).toBeNull();
    expect(runningCommit('')).toBeNull();
    expect(runningCommit(undefined)).toBeNull();
  });
});
