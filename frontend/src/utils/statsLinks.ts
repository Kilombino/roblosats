// Roblosats 0.9.15 (Kilombino TG 15665): the "Stats for Nerds" links of a coordinator.

/** The coordinator's Lightning node on Nodex, the BLAKE2b Lightning explorer. */
export const nodexNodeUrl = (nodeId: string): string =>
  `https://nodex.kilombino.com/node/${encodeURIComponent(nodeId)}`;

/** The coordinator's running commit, or null when its image does not say (an all-zero
 * placeholder, "dev", or anything that is not a commit hash). */
export const runningCommit = (hash: string | undefined | null): string | null => {
  const sha = (hash ?? '').trim().split(/\s+/)[0] ?? '';
  if (!/^[0-9a-f]{7,40}$/i.test(sha) || /^0+$/.test(sha)) return null;
  return sha.toLowerCase();
};

export const commitUrl = (sha: string): string =>
  `https://github.com/Kilombino/roblosats/commit/${sha}`;
