/** 0.9.11 (Clearnet mode): the host names of the coordinators' clearnet addresses, any network.
 * The native side reaches only these directly; everything else keeps going through Tor. */
interface WithOrigins {
  mainnet?: { clearnet?: unknown };
  testnet?: { clearnet?: unknown };
}

export const clearnetHosts = (federation: { getCoordinators: () => WithOrigins[] }): string[] => {
  const hosts = new Set<string>();
  federation.getCoordinators().forEach((c) => {
    [c.mainnet?.clearnet, c.testnet?.clearnet].forEach((u) => {
      if (!u) return;
      try {
        const host = new URL(String(u)).hostname.toLowerCase();
        if (!host.endsWith('.onion')) hosts.add(host);
      } catch {
        // not an address
      }
    });
  });
  return Array.from(hosts).sort();
};

export default clearnetHosts;
