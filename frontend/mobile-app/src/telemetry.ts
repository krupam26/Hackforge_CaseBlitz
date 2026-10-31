/** AsliKaun browser telemetry. Collects only after explicit in-app consent and while online. */
import { resampleAt50Hz, sensorIntervalMs } from "./sensorProcessing";
import { retryDelayMs, secureTransportIssue, shouldRecordStoppedEvent } from "./telemetryPolicy";
type TouchPathPoint = { x: number; y: number; pressure: number; t_offset_ms: number };
type TouchEventSummary = {
  type: "tap" | "swipe";
  timestamp: string;
  duration_ms: number;
  distance_px: number;
  interval_ms: number | null;
  t_offset_ms: number;
  pressure?: number;
  path?: TouchPathPoint[];
};
type IngestResponse = { driver_id?: string; decision?: string; quadrant?: string; model_status?: string; [key: string]: unknown };
type Sample = { at: number; value: number };
type EncryptedPayload = { iv: ArrayBuffer; ciphertext: ArrayBuffer };
type QueuedBatch = { batch_id: string; payload: EncryptedPayload; attempts: number; created_at: string; next_attempt_at: number };
export type QueueHealth = { pending: number; retrying: number; oldest: string | null; nextRetryAt: number | null };
const API_BASE = (
  import.meta.env.VITE_ASLIKAUN_API_URL ||
  import.meta.env.VITE_TRUSTMESH_API_URL ||
  (typeof window !== "undefined" && window.location.origin ? window.location.origin : "http://127.0.0.1:8000")
).replace(/\/$/, "");
const DRIVER_ID = import.meta.env.VITE_ASLIKAUN_DRIVER_ID || import.meta.env.VITE_TRUSTMESH_DRIVER_ID || "rider-001";
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
const LOCAL_DEV = typeof window !== "undefined" && (LOCAL_HOSTS.has(window.location.hostname) || window.location.hostname.endsWith(".localhost"));
const DRIVER_TOKEN = import.meta.env.VITE_ASLIKAUN_DRIVER_TOKEN || import.meta.env.VITE_TRUSTMESH_DRIVER_TOKEN || (LOCAL_DEV ? "demo-driver-token-2026" : "");
const ZONE_STORAGE_KEY = "aslikaun_coarse_zone_v1";
const DB_NAME = "aslikaun-telemetry-v1";
const DB_STORE = "outbox";
const GRID_MS = 20;
type GpsPoint = { lat: number; lng: number; t: string };
const state = {
  online: false, listenersStarted: false, accel: [] as Sample[], gyro: [] as Sample[], touches: [] as TouchEventSummary[],
  pendingEvents: [] as { event_type: string; timestamp: string }[],
  pointerStart: null as { at: number; x: number; y: number; pressure: number } | null,
  pointerPath: [] as TouchPathPoint[],
  lastTouchAt: null as number | null, sessionStartedAt: Date.now(), offerAt: null as number | null,
  acceptLatencyMs: null as number | null, zone: "zone-unavailable", watchId: null as number | null,
  heartbeat: null as ReturnType<typeof setInterval> | null,
  // Precision mode: raw GPS points + full touch gesture paths. OFF by
  // default — only enabled after the driver explicitly opts in via the
  // in-app consent prompt (see App.tsx's handleAvailabilityChange),
  // recorded server-side via POST /v1/drivers/{id}/consent. When off,
  // behavior is unchanged from before: coarse zone only, no raw path.
  precisionEnabled: false,
  gps: [] as GpsPoint[],
  zoneTimestamp: null as string | null, lastMovingAt: null as number | null, stoppedEventReported: false,
  batteryLevel: null as number | null, charging: false, sampleIntervalMs: GRID_MS, lastAccelAt: 0, lastGyroAt: 0,
  retryTimer: null as ReturnType<typeof setTimeout> | null,
  sensorWatchdog: null as ReturnType<typeof setInterval> | null,
  battery: null as (EventTarget & { level: number; charging: boolean }) | null,
  batteryHandler: null as (() => void) | null, elevatedRisk: false,
};

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(DB_STORE)) request.result.createObjectStore(DB_STORE, { keyPath: "batch_id" });
      if (!request.result.objectStoreNames.contains("meta")) request.result.createObjectStore("meta");
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function storeRequest<T>(name: string, mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await openDatabase();
  try { return await new Promise((resolve, reject) => { const tx = db.transaction(name, mode); const request = work(tx.objectStore(name)); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); tx.onabort = () => reject(tx.error); }); }
  finally { db.close(); }
}
async function encryptionKey(): Promise<CryptoKey> {
  const saved = await storeRequest<CryptoKey | undefined>("meta", "readonly", (store) => store.get("queue-key"));
  if (saved) return saved;
  const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  const db = await openDatabase();
  try { await new Promise<void>((resolve, reject) => { const tx = db.transaction("meta", "readwrite"); tx.objectStore("meta").put(key, "queue-key"); tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error); }); }
  finally { db.close(); }
  return key;
}
async function encryptPayload(payload: Record<string, unknown>): Promise<EncryptedPayload> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await encryptionKey(), new TextEncoder().encode(JSON.stringify(payload)));
  return { iv: iv.buffer, ciphertext };
}
async function decryptPayload(payload: EncryptedPayload): Promise<Record<string, unknown>> {
  const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv: new Uint8Array(payload.iv) }, await encryptionKey(), payload.ciphertext);
  return JSON.parse(new TextDecoder().decode(plaintext)) as Record<string, unknown>;
}
function transportError(): string | null { return secureTransportIssue(API_BASE, window.location.protocol, window.location.hostname); }
function credentialError(): string | null { return DRIVER_TOKEN ? null : "Driver API token is not configured for this deployment."; }

/** Call once the driver has explicitly granted (or not) precision
 * tracking consent. Does NOT itself call the consent API — App.tsx
 * does that as part of the same user action, this just flips what
 * telemetry.ts captures/sends. */
export function setPrecisionMode(enabled: boolean) {
  state.precisionEnabled = enabled;
}

/** Records the driver's consent decision with the backend
 * (POST/DELETE equivalent via /v1/drivers/{id}/consent). Call this
 * from the same user gesture that shows the consent prompt, before
 * enabling precision capture. */
export async function recordPrecisionConsent(granted: boolean): Promise<boolean> {
  const issue = transportError() || credentialError(); if (issue) { status("security", false, issue); return false; }
  const path = granted ? "consent" : "consent/revoke";
  try {
    const response = await fetch(`${API_BASE}/v1/drivers/${DRIVER_ID}/${path}`, {
      method: "POST", headers: { "X-Driver-Token": DRIVER_TOKEN },
    });
    return response.ok;
  } catch (error) {
    console.error("[AsliKaun] Failed to record consent", error);
    return false;
  }
}
function status(signal: string, available: boolean, detail?: string) {
  window.dispatchEvent(new CustomEvent("aslikaun:signal-status", { detail: { signal, available, detail } }));
  window.dispatchEvent(new CustomEvent("trustmesh:signal-status", { detail: { signal, available, detail } }));
}
function pushLimited(list: Sample[], value: number, at: number, max = 1500) { if (Number.isFinite(value) && Number.isFinite(at)) { list.push({ at, value: Number(value.toFixed(3)) }); if (list.length > max) list.shift(); } }
function updateSamplingPolicy() {
  const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
  state.sampleIntervalMs = sensorIntervalMs({ hidden, batteryLevel: state.batteryLevel, charging: state.charging, elevatedRisk: state.elevatedRisk });
  status("sampling", true, `Motion target: ${Math.round(1000 / state.sampleIntervalMs)} Hz${hidden ? "; background collection paused" : ""}${state.batteryLevel === null ? "; battery status unavailable" : `; battery ${Math.round(state.batteryLevel * 100)}%`}`);
}
async function startBatteryMonitoring() {
  const api = navigator as Navigator & { getBattery?: () => Promise<EventTarget & { level: number; charging: boolean }> };
  if (!api.getBattery) { state.batteryLevel = null; status("battery", false, "Battery status is unavailable in this browser."); status("thermal", false, "Browser does not expose a reliable thermal-state API."); updateSamplingPolicy(); return; }
  try {
    const battery = await api.getBattery(); state.battery = battery;
    const update = () => { state.batteryLevel = battery.level; state.charging = battery.charging; updateSamplingPolicy(); };
    state.batteryHandler = update;
    battery.addEventListener("levelchange", update); battery.addEventListener("chargingchange", update); update();
    status("battery", true, "Battery-aware sensor sampling enabled.");
    status("thermal", false, "Browser does not expose a reliable thermal-state API.");
  } catch { state.batteryLevel = null; status("battery", false, "Battery status could not be read."); status("thermal", false, "Browser does not expose a reliable thermal-state API."); updateSamplingPolicy(); }
}
function stopBatteryMonitoring() {
  if (state.battery && state.batteryHandler) { state.battery.removeEventListener("levelchange", state.batteryHandler); state.battery.removeEventListener("chargingchange", state.batteryHandler); }
  state.battery = null; state.batteryHandler = null; state.batteryLevel = null; state.elevatedRisk = false; state.sampleIntervalMs = GRID_MS;
}
function startSensorWatchdog() {
  if (state.sensorWatchdog) return;
  state.sensorWatchdog = setInterval(() => {
    if (!state.online || document.visibilityState !== "visible") return;
    if (!state.lastAccelAt || Date.now() - state.lastAccelAt > 30_000) status("motion", false, "No recent motion samples; sensor may be unavailable or permission may have changed.");
  }, 15_000);
}
function makeDeviceId() {
  const key = "aslikaun_device_id_v1";
  let id = localStorage.getItem(key) || localStorage.getItem("trustmesh_device_id_v1");
  if (!id) { id = `web-${crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`; localStorage.setItem(key, id); }
  return id;
}
// Rounded to ~1 km-ish grid; exact coordinates never leave the browser.
function coarseZone(latitude: number, longitude: number) { return `grid-${latitude.toFixed(2)}-${longitude.toFixed(2)}`; }
async function requestMotionPermission() {
  const motion = DeviceMotionEvent as typeof DeviceMotionEvent & { requestPermission?: () => Promise<"granted" | "denied"> };
  if (typeof DeviceMotionEvent === "undefined") { status("motion", false, "Device motion is not supported in this browser."); return; }
  try {
    if (typeof motion.requestPermission === "function") {
      const result = await motion.requestPermission();
      if (result !== "granted") { status("motion", false, "Motion permission was not granted."); return; }
    }
    status("motion", true, "Motion collection enabled where sensor data is available.");
  } catch { status("motion", false, "Motion permission was unavailable or denied."); }
}
function startLocation() {
  if (!navigator.geolocation) { status("gps", false, "Geolocation is unsupported."); return; }
  if (state.watchId !== null) navigator.geolocation.clearWatch(state.watchId);
  state.watchId = navigator.geolocation.watchPosition((position) => {
    const nextZone = coarseZone(position.coords.latitude, position.coords.longitude);
    const changed = nextZone !== state.zone;
    state.zone = nextZone; localStorage.setItem(ZONE_STORAGE_KEY, state.zone);
    state.zoneTimestamp = new Date(position.timestamp).toISOString();
    if (Number.isFinite(position.coords.speed) && position.coords.speed !== null) {
      const speed = position.coords.speed; const now = position.timestamp || Date.now();
      if (speed > 0.5) { state.lastMovingAt = now; state.stoppedEventReported = false; }
      else if (!state.stoppedEventReported && shouldRecordStoppedEvent(state.lastMovingAt, speed, now)) {
        state.pendingEvents.push({ event_type: "driver_stop_detected", timestamp: new Date(now).toISOString() }); state.stoppedEventReported = true;
        if (state.online) void sendSession();
      }
    }
    if (state.precisionEnabled) {
      state.gps.push({ lat: position.coords.latitude, lng: position.coords.longitude, t: new Date().toISOString() });
      if (state.gps.length > 500) state.gps.shift();
      status("gps", true, `Precise location recorded${position.coords.accuracy ? ` (accuracy ~${Math.round(position.coords.accuracy)} m)` : ""}, per your consent. Purged after 30 days.`);
    } else {
      status("gps", true, `Coarse zone updated${position.coords.accuracy ? ` (accuracy reported: ~${Math.round(position.coords.accuracy)} m)` : ""}. Exact coordinates are not uploaded.`);
    }
    if (changed && state.online) void sendSession();
  }, (error) => {
    state.zone = localStorage.getItem(ZONE_STORAGE_KEY) || "zone-unavailable";
    status("gps", false, error.code === 1 ? "Location permission denied; GPS zone unavailable." : "Location unavailable; GPS zone unavailable.");
  }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 });
}
function startListeners() {
  if (state.listenersStarted || typeof window === "undefined") return;
  state.listenersStarted = true;
  window.addEventListener("devicemotion", (event) => {
    if (!state.online || document.visibilityState !== "visible" || !event.accelerationIncludingGravity) return;
    const a = event.accelerationIncludingGravity; if (a.x == null || a.y == null || a.z == null) return;
    const at = Date.now(); if (at - state.lastAccelAt < state.sampleIntervalMs) return; state.lastAccelAt = at;
    pushLimited(state.accel, Math.sqrt(a.x ** 2 + a.y ** 2 + a.z ** 2), at); status("motion", true, "Accelerometer samples received.");
  }, { passive: true });
  window.addEventListener("deviceorientation", (event) => {
    if (!state.online || document.visibilityState !== "visible" || event.beta == null || event.gamma == null) return;
    const at = Date.now(); if (at - state.lastGyroAt < state.sampleIntervalMs) return; state.lastGyroAt = at;
    pushLimited(state.gyro, (Math.abs(event.beta) + Math.abs(event.gamma)) / 180, at);
  }, { passive: true });
  // PointerEvent exposes pressure (0..1) on supported touch hardware. Mouse pressure is not treated as biometric evidence.
  window.addEventListener("pointerdown", (event) => {
    if (!state.online || event.pointerType !== "touch") return;
    state.pointerStart = { at: performance.now(), x: event.clientX, y: event.clientY, pressure: event.pressure || 0 };
    state.pointerPath = state.precisionEnabled
      ? [{ x: event.clientX, y: event.clientY, pressure: event.pressure || 0.5, t_offset_ms: 0 }]
      : [];
  }, { passive: true });
  window.addEventListener("pointermove", (event) => {
    if (!state.online || !state.precisionEnabled || event.pointerType !== "touch" || !state.pointerStart) return;
    if (state.pointerPath.length >= 30) return; // bounded, same cap as the legacy SDK
    state.pointerPath.push({
      x: event.clientX, y: event.clientY, pressure: event.pressure || 0.5,
      t_offset_ms: Math.round(performance.now() - state.pointerStart.at),
    });
  }, { passive: true });
  window.addEventListener("pointerup", (event) => {
    if (!state.online || event.pointerType !== "touch" || !state.pointerStart) return;
    const start = state.pointerStart; const duration = Math.max(1, performance.now() - start.at);
    const distance = Math.round(Math.hypot(event.clientX - start.x, event.clientY - start.y)); const endedAt = Date.now();
    const pressureValues = [start.pressure, event.pressure].filter((p) => Number.isFinite(p) && p > 0);
    const item: TouchEventSummary = { type: distance > 15 ? "swipe" : "tap", timestamp: new Date(endedAt).toISOString(), duration_ms: Math.round(duration), distance_px: distance,
      interval_ms: state.lastTouchAt == null ? null : endedAt - state.lastTouchAt,
      t_offset_ms: Math.max(0, endedAt - state.sessionStartedAt) };
    if (pressureValues.length) item.pressure = Number((pressureValues.reduce((a, b) => a + b, 0) / pressureValues.length).toFixed(3));
    if (state.precisionEnabled && state.pointerPath.length >= 2) {
      state.pointerPath.push({ x: event.clientX, y: event.clientY, pressure: event.pressure || 0.5, t_offset_ms: Math.round(duration) });
      item.path = state.pointerPath;
    }
    state.touches.push(item); if (state.touches.length > 80) state.touches.shift(); state.lastTouchAt = endedAt; state.pointerStart = null; state.pointerPath = [];
    status("touch", true, `Touch gesture recorded${item.pressure !== undefined ? " with supported pressure data" : "; pressure not exposed by this device/browser"}.`);
  }, { passive: true });
  window.addEventListener("pointercancel", () => { state.pointerStart = null; state.pointerPath = []; }, { passive: true });
  window.addEventListener("online", () => { status("network", true, "Connection restored; flushing saved telemetry."); void flushQueue(); });
  window.addEventListener("offline", () => status("network", false, "Offline; telemetry batches remain saved on this device."));
  document.addEventListener("visibilitychange", () => {
    updateSamplingPolicy();
    if (document.visibilityState === "visible" && state.online) { startLocation(); void flushQueue(); }
    else if (state.online) { if (state.watchId !== null) navigator.geolocation?.clearWatch(state.watchId); state.watchId = null; void sendSession(); }
  });
  window.addEventListener("pagehide", () => { if (state.online) void sendSession(); });
}
export function recordOrderOffer() { state.offerAt = Date.now(); }
export function recordOrderAccept() {
  if (state.offerAt != null) { state.acceptLatencyMs = Math.max(0, Date.now() - state.offerAt); state.offerAt = null; }
  state.pendingEvents.push({ event_type: "delivery_order_accepted", timestamp: new Date().toISOString() });
  return sendSession();
}
export async function goOnlineAndIngest(precisionEnabled = false): Promise<IngestResponse | null> {
  // Called from the driver's explicit Go Online action so browser permission prompts can appear.
  state.precisionEnabled = precisionEnabled;
  await requestMotionPermission(); await startBatteryMonitoring(); startLocation(); startListeners(); startSensorWatchdog(); state.online = true;
  state.pendingEvents.push({ event_type: "driver_session_resumed", timestamp: new Date().toISOString() });
  if (state.heartbeat) clearInterval(state.heartbeat);
  state.heartbeat = setInterval(() => { if (state.online) void sendSession(); }, 30000);
  return sendSession();
}
export function goOffline() {
  if (state.online) { state.pendingEvents.push({ event_type: "driver_session_paused", timestamp: new Date().toISOString() }); void sendSession(); }
  state.online = false; if (state.watchId !== null && navigator.geolocation) navigator.geolocation.clearWatch(state.watchId);
  state.watchId = null; if (state.heartbeat) clearInterval(state.heartbeat); state.heartbeat = null;
  if (state.sensorWatchdog) clearInterval(state.sensorWatchdog); state.sensorWatchdog = null;
  stopBatteryMonitoring();
  status("collection", false, "Telemetry paused while offline.");
}
export type ChallengeStatus = { driver_id: string; challenge_pending: boolean; decision?: string; reasons?: string[] };

/** Poll whether the backend has escalated this driver to a mid-trip
 * safe-pause identity challenge. Call on a timer while online. */
export async function getChallengeStatus(): Promise<ChallengeStatus | null> {
  const issue = transportError() || credentialError(); if (issue) { status("security", false, issue); return null; }
  try {
    const response = await fetch(`${API_BASE}/v1/drivers/${DRIVER_ID}/challenge-status`, { headers: { "X-Driver-Token": DRIVER_TOKEN } });
    if (!response.ok) return null;
    return await response.json() as ChallengeStatus;
  } catch {
    return null;
  }
}

/** Submit a selfie (base64, no data: URI prefix) in response to a
 * pending challenge. Backend checks it against the driver's enrolled
 * face and auto-clears on a clear match; anything else is left for
 * human review, never auto-banned. */
export async function respondToChallenge(imageBase64: string): Promise<{ outcome: string; similarity?: number; reason?: string } | null> {
  const issue = transportError() || credentialError(); if (issue) { status("security", false, issue); return null; }
  try {
    const response = await fetch(`${API_BASE}/v1/drivers/${DRIVER_ID}/challenge/respond`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Driver-Token": DRIVER_TOKEN },
      body: JSON.stringify({ image_base64: imageBase64 }),
    });
    return await response.json();
  } catch (error) {
    console.error("[AsliKaun] Failed to submit challenge response", error);
    return null;
  }
}

async function saveBatch(payload: Record<string, unknown>) {
  const batch: QueuedBatch = { batch_id: crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`, payload: await encryptPayload(payload), attempts: 0, created_at: new Date().toISOString(), next_attempt_at: Date.now() };
  await storeRequest("outbox", "readwrite", (store) => store.put(batch));
}
let activeFlush: Promise<IngestResponse | null> | null = null;
export function flushQueue(): Promise<IngestResponse | null> {
  if (activeFlush) return activeFlush;
  activeFlush = flushQueueOnce().finally(() => { activeFlush = null; });
  return activeFlush;
}
export async function getQueueHealth(): Promise<QueueHealth> {
  try {
    const batches = await storeRequest<QueuedBatch[]>("outbox", "readonly", (store) => store.getAll());
    const retrying = batches.filter((batch) => batch.attempts > 0);
    const nextRetryAt = batches.filter((batch) => batch.next_attempt_at > Date.now()).reduce<number | null>((soonest, batch) => soonest === null || batch.next_attempt_at < soonest ? batch.next_attempt_at : soonest, null);
    batches.sort((a, b) => a.created_at.localeCompare(b.created_at));
    return { pending: batches.length, retrying: retrying.length, oldest: batches[0]?.created_at ?? null, nextRetryAt };
  } catch (error) { status("queue", false, error instanceof Error ? error.message : "Persistent telemetry queue is unavailable."); return { pending: 0, retrying: 0, oldest: null, nextRetryAt: null }; }
}
async function flushQueueOnce(): Promise<IngestResponse | null> {
  if (!navigator.onLine) { status("network", false, "Offline; telemetry remains in the encrypted local queue."); return null; }
  const issue = transportError() || credentialError();
  if (issue) { status("security", false, issue); return null; }
  let batches: QueuedBatch[];
  try { batches = await storeRequest("outbox", "readonly", (store) => store.getAll()); }
  catch (error) { status("queue", false, error instanceof Error ? error.message : "Persistent telemetry queue is unavailable."); return null; }
  let latest: IngestResponse | null = null;
  for (const batch of batches.sort((a, b) => a.created_at.localeCompare(b.created_at))) {
    if (batch.next_attempt_at > Date.now()) continue;
    try {
      const payload = await decryptPayload(batch.payload);
      const response = await fetch(`${API_BASE}/v1/sessions/ingest`, { method: "POST", headers: { "Content-Type": "application/json", "X-Driver-Token": DRIVER_TOKEN, "X-Telemetry-Batch-Id": batch.batch_id }, body: JSON.stringify(payload) });
      if (!response.ok) throw new Error(`API ${response.status}: ${(await response.text()) || response.statusText}`);
      latest = await response.json() as IngestResponse;
      const decision = String(latest.decision || ""); state.elevatedRisk = decision.includes("challenge") || decision.includes("step_up") || decision.includes("escalat"); updateSamplingPolicy();
      await storeRequest("outbox", "readwrite", (store) => store.delete(batch.batch_id));
      window.dispatchEvent(new CustomEvent("aslikaun:ingest-success", { detail: latest }));
      window.dispatchEvent(new CustomEvent("trustmesh:ingest-success", { detail: latest }));
      status("api", true, "Telemetry batch acknowledged by the server.");
    } catch (error) {
      const attempts = batch.attempts + 1; const delay = retryDelayMs(attempts);
      await storeRequest("outbox", "readwrite", (store) => store.put({ ...batch, attempts, next_attempt_at: Date.now() + delay }));
      const message = error instanceof Error ? error.message : "Unknown telemetry upload error";
      window.dispatchEvent(new CustomEvent("aslikaun:ingest-error", { detail: message })); status("api", false, `${message}; retry scheduled.`);
      if (state.retryTimer) clearTimeout(state.retryTimer); state.retryTimer = setTimeout(() => void flushQueue(), delay);
      break;
    }
  }
  const remaining = await storeRequest<QueuedBatch[]>("outbox", "readonly", (store) => store.getAll()).catch(() => []);
  window.dispatchEvent(new CustomEvent("aslikaun:queue-health", { detail: { pending: remaining.length, retrying: remaining.filter((batch) => batch.attempts > 0).length } }));
  return latest;
}
let activeSend: Promise<IngestResponse | null> | null = null;
export function sendSession(): Promise<IngestResponse | null> {
  if (activeSend) return activeSend;
  activeSend = sendSessionOnce().finally(() => { activeSend = null; });
  return activeSend;
}
async function sendSessionOnce(): Promise<IngestResponse | null> {
  const accelCount = state.accel.length; const gyroCount = state.gyro.length; const touchCount = state.touches.length;
  const gpsCount = state.gps.length; const eventCount = state.pendingEvents.length;
  const acceptLatencyMs = state.acceptLatencyMs;
  const accelSamples = state.accel.slice(0, accelCount); const gyroSamples = state.gyro.slice(0, gyroCount);
  const touchEvents = state.touches.slice(0, touchCount); const gpsPoints = state.gps.slice(0, gpsCount);
  const clientEvents = state.pendingEvents.slice(0, eventCount);
  const { values: accel, timestamps: accel_timestamps } = resampleAt50Hz(accelSamples);
  const { values: gyro, timestamps: gyro_timestamps } = resampleAt50Hz(gyroSamples);
  const payload = { driver_id: DRIVER_ID, device_id: makeDeviceId(),
    // Public IP/subnet must be derived server-side behind trusted proxy configuration; browser code cannot know it reliably.
    ip_subnet: null, zone: state.zone, zone_timestamp: state.zoneTimestamp, accel, gyro, accel_timestamps, gyro_timestamps, touch_events: touchEvents, client_events: clientEvents,
    gps: state.precisionEnabled ? gpsPoints : [], vehicle_speed_mph: null,
    accept_latency_ms: acceptLatencyMs, timestamp: new Date().toISOString() };
  try {
    await saveBatch(payload);
    state.accel.splice(0, accelCount); state.gyro.splice(0, gyroCount); state.touches.splice(0, touchCount); state.gps.splice(0, gpsCount); state.pendingEvents.splice(0, eventCount); if (state.acceptLatencyMs === acceptLatencyMs) state.acceptLatencyMs = null; state.sessionStartedAt = Date.now(); state.lastTouchAt = null;
    status("queue", true, "Telemetry batch saved to the encrypted local queue.");
    return flushQueue();
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unable to save telemetry batch.";
    window.dispatchEvent(new CustomEvent("aslikaun:ingest-error", { detail: message })); status("queue", false, message); return null;
  }
}
