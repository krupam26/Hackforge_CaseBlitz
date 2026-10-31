export type TimestampedSample = { at: number; value: number };

export function sensorIntervalMs(input: {
  hidden: boolean;
  batteryLevel: number | null;
  charging: boolean;
  elevatedRisk: boolean;
}) {
  if (input.hidden) return 100;
  if (input.elevatedRisk || input.charging || input.batteryLevel === null || input.batteryLevel > 0.35) return 20;
  if (input.batteryLevel <= 0.2) return 50;
  return 33;
}

export function resampleAt50Hz(samples: TimestampedSample[], options: { maxGapMs?: number; maxSamples?: number } = {}) {
  const maxGap = options.maxGapMs ?? 500;
  const maxSamples = options.maxSamples ?? 500;
  const ordered = samples
    .filter((sample) => Number.isFinite(sample.at) && Number.isFinite(sample.value))
    .slice()
    .sort((a, b) => a.at - b.at);
  if (!ordered.length) return { values: [] as number[], timestamps: [] as string[] };

  const values: number[] = [];
  const timestamps: string[] = [];
  const start = ordered[0].at;
  const end = ordered[ordered.length - 1].at;
  for (let target = start; target <= end; target += 20) {
    let right = ordered.findIndex((sample) => sample.at >= target);
    if (right < 0) right = ordered.length - 1;
    const after = ordered[right];
    const before = ordered[Math.max(0, right - 1)];
    if (target - before.at > maxGap || after.at - target > maxGap) continue;
    const span = after.at - before.at;
    const ratio = span > 0 ? (target - before.at) / span : 0;
    const value = before.value + (after.value - before.value) * ratio;
    if (!Number.isFinite(value)) continue;
    values.push(Number(value.toFixed(3)));
    timestamps.push(new Date(target).toISOString());
  }
  return { values: values.slice(-maxSamples), timestamps: timestamps.slice(-maxSamples) };
}
