export const ACTION_SIGNATURE_WINDOW = 300;

export function orderActionContent(action: string, orderId: number, shortAlias: string): string {
  return `${action}:${orderId}:${shortAlias}`;
}

export function timedActionContent(action: string, shortAlias: string): string {
  return `${action}:${shortAlias}:${Math.floor(Date.now() / 1000)}`;
}

export function reviewActionContent(pubkey: string): string {
  return `review:${pubkey}`;
}

export async function signAction(
  content: string,
  encPrivKey?: string,
  token?: string,
): Promise<string | undefined> {
  if (!encPrivKey || !token) return undefined;
  // Loaded on first use (openpgp is heavy, and models importing this stay light in tests).
  const { signCleartextMessage } = await import('../pgp');
  return await signCleartextMessage(content, encPrivKey, token);
}

interface SigningRobot {
  token?: string;
  encPrivKey?: string;
  found?: boolean;
  loading?: boolean;
  fetch?: (federation: never) => Promise<unknown>;
}

/**
 * Roblosats 0.9.17: signs as the robot of the coordinator the request goes to, with the key that
 * coordinator holds for it. Until its robot has answered once (found is unknown) the local key may
 * not be the one the coordinator registered first, so it is fetched before signing. Silent: no
 * prompt, the token unlocks the key as for invoices.
 */
export async function signAs(
  robot: SigningRobot | null | undefined,
  federation: unknown,
  content: string,
): Promise<string | undefined> {
  if (!robot) return undefined;
  if (robot.found === undefined && robot.fetch) {
    try {
      await robot.fetch(federation as never);
    } catch {
      // sign with what it has
    }
  }
  try {
    return await signAction(content, robot.encPrivKey, robot.token);
  } catch (e) {
    console.log(e);
    return undefined;
  }
}
