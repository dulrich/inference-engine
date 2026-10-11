// Default Command Center link for AppHeader, as a pure function of the page location.

/** Command Center's gateway host label — pinned in `gateway.hosts` (plans/wireguard-tls-gateway.md). */
export const GATEWAY_CENTER_LABEL = "center";

/** LAN / direct-port Command Center port. */
export const COMMAND_CENTER_PORT = 4730;

export interface HomeLocation {
  protocol: string;
  hostname: string;
  port: string;
}

function isIpLiteral(hostname: string): boolean {
  return /^\d{1,3}(\.\d{1,3}){3}$/.test(hostname) || hostname.includes(":") || hostname.startsWith("[");
}

/**
 * Served through the TLS gateway (default port, `<tool>.<parent>` name) →
 * `<protocol>//center.<parent>/`; otherwise `<protocol>//<hostname>:4730/`.
 */
export function defaultHomeHrefFor({ protocol, hostname, port }: HomeLocation): string {
  if (port === "" && hostname !== "localhost" && !isIpLiteral(hostname)) {
    const labels = hostname.split(".");
    if (labels.length >= 3 && labels.every((l) => l !== "")) {
      return `${protocol}//${GATEWAY_CENTER_LABEL}.${labels.slice(1).join(".")}/`;
    }
  }
  return `${protocol}//${hostname}:${COMMAND_CENTER_PORT}/`;
}
