const { readFileSync } = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const assert = require("node:assert/strict");
const { test } = require("node:test");
const ts = require("typescript");

function loadTypescript(relative) {
  const filename = path.resolve(__dirname, "..", relative);
  const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const loaded = new Module(filename, module);
  loaded.filename = filename;
  loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  loaded._compile(compiled, filename);
  return loaded.exports;
}

const { estimateSampling, samplingValidation } = loadTypescript("lib/sampling.ts");
const { normalizeCreateJobOptions } = loadTypescript("lib/api.ts");
const defaults = { mode: "adaptive", interval_ms: 1000, target_frames: 300, frame_format: "jpeg", frame_max_dim: 1280, frame_budget: 2000 };

test("30-minute 5 FPS request exposes the actual budget reduction", () => {
  const result = estimateSampling({ ...defaults, mode: "interval", interval_ms: 200 }, 1800);
  assert.equal(result.requestedFrames, 9000);
  assert.equal(result.effectiveFrames, 2000);
  assert.equal(result.requestedFps, 5);
  assert.equal(result.effectiveFps, 2000 / 1800);
  assert.equal(result.capped, true);
});

test("short original-timeline range makes source-frame extraction practical", () => {
  const result = estimateSampling({ ...defaults, mode: "every_frame", range_start_seconds: 600, range_end_seconds: 610 }, 1800, 60);
  assert.equal(result.requestedFrames, 600);
  assert.equal(result.everyFrameOverBudget, false);
  assert.equal(result.duration, 10);
});

test("every-frame over-budget estimate does not promise a capped successful export", () => {
  const result = estimateSampling({ ...defaults, mode: "every_frame" }, 1800, 30);
  assert.equal(result.requestedFrames, 54000);
  assert.equal(result.everyFrameOverBudget, true);
});

test("unknown duration stays unknown unless a complete range is provided", () => {
  assert.equal(estimateSampling(defaults, null), null);
  assert.equal(estimateSampling({ ...defaults, range_start_seconds: 60, range_end_seconds: 70 }, null).duration, 10);
});

test("dense bursts show an upper estimate and share the total frame budget", () => {
  const result = estimateSampling({ ...defaults, mode: "per_second", frame_budget: 500,
    frame_bursts: [{ start_seconds: 100, end_seconds: 110, fps: 10 }] }, 600);
  assert.equal(result.requestedFrames, 700);
  assert.equal(result.effectiveFrames, 500);
  assert.equal(result.hasBursts, true);
});

test("invalid ranges and bursts are caught without treating a duration guess as truth", () => {
  assert.match(samplingValidation({ ...defaults, range_start_seconds: 10, range_end_seconds: 5 }, null), /later/);
  assert.match(samplingValidation({ ...defaults, range_start_seconds: 200 }, 100), /video ends/);
  assert.match(samplingValidation({ ...defaults, frame_bursts: [{ start_seconds: 1, end_seconds: 200, fps: 5 }] }, 100), /fit inside/);
  assert.equal(samplingValidation({ ...defaults, range_start_seconds: 200 }, null), null);
});

test("submission retains increased frame limits, zero start and nullable end", () => {
  const result = normalizeCreateJobOptions({ ...defaults, interval_ms: 17, target_frames: 2000, frame_budget: 20000, range_start_seconds: 0, range_end_seconds: null });
  assert.equal(result.interval_ms, 17);
  assert.equal(result.target_frames, 2000);
  assert.equal(result.frame_budget, 20000);
  assert.equal(result.range_start_seconds, 0);
  assert.equal(result.range_end_seconds, null);
});

test("hidden bursts cannot reject an every-frame job after switching modes", () => {
  const result = normalizeCreateJobOptions({ ...defaults, mode: "every_frame", frame_bursts: [{ start_seconds: 10, end_seconds: 5, fps: 5 }] });
  assert.deepEqual(result.frame_bursts, []);
});
