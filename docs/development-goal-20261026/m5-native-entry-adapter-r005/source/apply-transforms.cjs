'use strict';
// apply-transforms.cjs -- SHARED deterministic transform implementation for the
// task-local response-only entry adapter (revision r005, task
// m5-native-entry-adapter-dev-020-001, author017/018/019/020).
//
// Imported by BOTH the production preload (response-only-entry.cjs) and the
// actual-source test seam (test-actual-source.cjs): author020 §2 requires the
// tests to apply the SAME transform implementation used by the production
// preload, with no second copy. Pure text operations; no I/O beyond reading the
// sidecar pin.

const fs = require('node:fs');
const crypto = require('node:crypto');

function loadPin(sidecarPath) {
  return JSON.parse(fs.readFileSync(sidecarPath, 'utf8'));
}

// Returns null when the installed bytes match the pin, else a drift string.
function verifyInstalled(pin, buf) {
  if (!pin || !pin.installed_entry) return 'pin-malformed';
  if (pin.installed_entry.bytes != null && buf.length !== pin.installed_entry.bytes) {
    return 'bytes=' + buf.length + ' expected=' + pin.installed_entry.bytes;
  }
  const sha = crypto.createHash('sha256').update(buf).digest('hex');
  if (sha !== pin.installed_entry.sha256) return 'sha256=' + sha;
  return null;
}

// Applies the pin's transforms to source text with strict per-transform
// occurrence counting (checked extraction fails closed on any count mismatch).
// Returns { errors } or { src, applied: [transform ids] }.
function countAndApply(pin, source) {
  const errors = [];
  for (const t of pin.transforms) {
    const n = source.split(t.find).length - 1;
    if (n !== t.expected_occurrences) errors.push(t.id + ' count=' + n + ' expected=' + t.expected_occurrences);
  }
  if (errors.length) return { errors };
  let s = source;
  const applied = [];
  for (const t of pin.transforms) {
    s = s.split(t.find).join(t.replace);
    const a = s.split(t.replace).length - 1;
    if (a !== t.expected_occurrences) return { errors: [t.id + ' applied=' + a + ' expected=' + t.expected_occurrences] };
    applied.push(t.id);
  }
  return { src: s, applied };
}

module.exports = { loadPin, verifyInstalled, countAndApply };
