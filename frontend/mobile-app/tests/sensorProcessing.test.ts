import assert from "node:assert/strict";
import test from "node:test";
import { resampleAt50Hz, sensorIntervalMs } from "../src/sensorProcessing.ts";
import { stageForVerificationStatus } from "../src/verificationState.ts";
import { retryDelayMs, secureTransportIssue, shouldRecordStoppedEvent } from "../src/telemetryPolicy.ts";

test("resamples irregular out-of-order samples onto a 50 Hz grid", () => {
  const output = resampleAt50Hz([
    { at: 1040, value: 4 },
    { at: 1000, value: 0 },
  ]);
  assert.deepEqual(output.values, [0, 2, 4]);
  assert.deepEqual(output.timestamps.map((value) => Date.parse(value)), [1000, 1020, 1040]);
});

test("omits interpolated points inside a stale sample gap", () => {
  const output = resampleAt50Hz([
    { at: 1000, value: 1 },
    { at: 1100, value: 2 },
    { at: 3000, value: 3 },
    { at: 3100, value: 4 },
  ]);
  assert.equal(output.values[0], 1);
  assert.equal(output.values.at(-1), 4);
  assert.ok(output.values.length < 20);
});

test("drops invalid samples and bounds output length", () => {
  const output = resampleAt50Hz([
    { at: 1000, value: Number.NaN },
    { at: 1020, value: 3 },
    { at: 1040, value: 4 },
  ], { maxSamples: 1 });
  assert.deepEqual(output.values, [4]);
  assert.deepEqual(output.timestamps, [new Date(1040).toISOString()]);
});

test("adapts capture cadence to app visibility, battery and risk", () => {
  assert.equal(sensorIntervalMs({ hidden: true, batteryLevel: 0.9, charging: true, elevatedRisk: false }), 100);
  assert.equal(sensorIntervalMs({ hidden: false, batteryLevel: 0.15, charging: false, elevatedRisk: false }), 50);
  assert.equal(sensorIntervalMs({ hidden: false, batteryLevel: 0.3, charging: false, elevatedRisk: false }), 33);
  assert.equal(sensorIntervalMs({ hidden: false, batteryLevel: 0.15, charging: false, elevatedRisk: true }), 20);
});

test("maps authoritative verification statuses to UI states", () => {
  assert.equal(stageForVerificationStatus("pending"), "review");
  assert.equal(stageForVerificationStatus("escalated"), "review");
  assert.equal(stageForVerificationStatus("successful"), "success");
  assert.equal(stageForVerificationStatus("failed"), "failed");
  assert.equal(stageForVerificationStatus("unknown"), "failed");
});

test("blocks insecure production API transports while allowing local development", () => {
  assert.equal(secureTransportIssue("https://api.example.test", "https:", "app.example.test"), null);
  assert.equal(secureTransportIssue("http://localhost:8000", "http:", "localhost"), null);
  assert.match(secureTransportIssue("http://api.example.test", "https:", "app.example.test") || "", /API does not use HTTPS/);
  assert.match(secureTransportIssue("https://api.example.test", "http:", "app.example.test") || "", /app is not served over HTTPS/);
  assert.match(secureTransportIssue("not a url", "https:", "app.example.test") || "", /URL is invalid/);
});

test("uses bounded exponential upload retry delays", () => {
  assert.equal(retryDelayMs(1), 2_000);
  assert.equal(retryDelayMs(5), 32_000);
  assert.equal(retryDelayMs(100), 256_000);
});

test("detects a stop only after movement and a sustained low-speed interval", () => {
  assert.equal(shouldRecordStoppedEvent(null, 0, 60_000), false);
  assert.equal(shouldRecordStoppedEvent(40_000, 1.2, 80_000), false);
  assert.equal(shouldRecordStoppedEvent(40_000, 0.1, 69_999), false);
  assert.equal(shouldRecordStoppedEvent(40_000, 0.1, 70_000), true);
});
