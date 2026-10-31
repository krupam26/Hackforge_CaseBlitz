# AsliKaun driver telemetry integration

- Go Online requests location and motion permissions after the driver's explicit action.
- GPS uses watchPosition to refresh a coarse rounded zone; exact latitude/longitude is never uploaded unless precision mode is consented.
- Touch gestures use Pointer Events to record tap/swipe duration, distance, rhythm and pressure only where the touch device/browser exposes pressure.
- Accelerometer/orientation values are collected only while online and when supported.
- Sessions are ingested on going online, accepting an order, coarse-zone changes, and a 30-second heartbeat.
- Web telemetry batches are AES-GCM encrypted in IndexedDB, retried with bounded exponential backoff, and uploaded with stable batch IDs for server-side deduplication. They survive page reloads within the browser profile.
- Motion samples use timestamps and are interpolated to a 50 Hz grid when supported. Sampling adapts to visible state, battery level, and elevated backend risk. A browser GPS speed reading can record a sustained stop after movement; browser APIs do not reliably identify phone pickup events.
- Upload and verification API calls require HTTPS outside localhost. Configure the API URL for each deployment. A token embedded in a static frontend can be extracted, so production should exchange the authenticated driver session for a short-lived token through a server-side gateway rather than ship a reusable secret.
- The face challenge posts a camera capture to the backend liveness and face-match workflow. Successful matching depends on the model dependencies and an enrolled reference face; human review handles inconclusive results.
- Browser code cannot reliably read a public IP subnet; leave it null unless backend derives it from a correctly configured trusted proxy.

## Setup
1. Configure `VITE_ASLIKAUN_API_URL`, `VITE_ASLIKAUN_DRIVER_ID`, and `VITE_ASLIKAUN_DRIVER_TOKEN` (or legacy `VITE_TRUSTMESH_*`).
2. Run `npm install` then `npm run build`.
3. Deploy FastAPI separately from the Vercel static frontend; set CORS on the API for the deployed Vercel origin.

This is a browser-based prototype, not a native SDK. Sensor access differs by device/browser and is not guaranteed by a permission prompt alone. No Android or iOS project is present in this repository; native sensor bridges, encrypted OS-backed storage, foreground execution, and device validation remain unimplemented. Battery safeguards use the browser Battery Status API where available; browsers expose no dependable thermal-state API.
