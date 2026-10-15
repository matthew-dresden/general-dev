// Shared Z.AI quota-wait helper for the zai-multi-account plugin.
//
// Lives outside plugins/ so OpenCode's auto-loader never treats it as a
// plugin module (the loader requires plugin-shaped exports).

// Pure helper: how long to wait before retrying a 429 from the Z.AI
// subscription endpoint. Honors a standard Retry-After header (seconds);
// without one, uses the configured poll interval.
export function quotaWaitMs(res, pollMs) {
  const ra = res.headers?.get?.("retry-after")
  if (ra) {
    const s = Number(ra)
    if (Number.isFinite(s) && s > 0) return s * 1000
  }
  return pollMs
}