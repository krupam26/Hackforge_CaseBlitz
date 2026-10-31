const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

export function secureTransportIssue(apiUrl: string, pageProtocol: string, pageHostname: string): string | null {
  let api: URL;
  try { api = new URL(apiUrl); }
  catch { return "Telemetry API URL is invalid; configure VITE_TRUSTMESH_API_URL."; }
  const localPage = LOCAL_HOSTS.has(pageHostname) || pageHostname.endsWith(".localhost");
  const localApi = LOCAL_HOSTS.has(api.hostname) || api.hostname.endsWith(".localhost");
  if (api.protocol !== "https:" && !(api.protocol === "http:" && localApi)) {
    return "Telemetry upload blocked because the API does not use HTTPS.";
  }
  if (pageProtocol !== "https:" && !localPage) {
    return "Telemetry upload blocked because this app is not served over HTTPS.";
  }
  return null;
}

export function retryDelayMs(attempts: number): number {
  const exponent = Math.min(Math.max(0, Math.floor(attempts)), 8);
  return Math.min(300_000, 1_000 * 2 ** exponent);
}

export function shouldRecordStoppedEvent(lastMovingAt: number | null, speedMetersPerSecond: number | null, now: number, stillForMs = 30_000): boolean {
  return lastMovingAt !== null && Number.isFinite(lastMovingAt) && Number.isFinite(speedMetersPerSecond) &&
    speedMetersPerSecond! >= 0 && speedMetersPerSecond! <= 0.5 && now >= lastMovingAt && now - lastMovingAt >= stillForMs;
}
