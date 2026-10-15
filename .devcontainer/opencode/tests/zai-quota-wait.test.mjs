// Unit tests for the subscription quota hold-and-resume helper in
// lib/zai-quota.js (used by plugins/zai-multi-account.js).
const mod = await import("../lib/zai-quota.js?v=" + Date.now())
const { quotaWaitMs } = mod

let fail = 0
const check = (name, ok) => { if (!ok) fail++; console.log(`${ok ? "PASS" : "FAIL"}  ${name}`) }

const mkRes = (headers = {}) => ({ headers: { get: (k) => headers[k] } })

// quotaWaitMs: Retry-After seconds honored
check("Retry-After 120s -> 120000ms", quotaWaitMs(mkRes({ "retry-after": "120" }), 999) === 120000)
check("no header -> poll interval", quotaWaitMs(mkRes(), 300000) === 300000)
check("garbage header -> poll interval", quotaWaitMs(mkRes({ "retry-after": "soon" }), 300000) === 300000)
check("zero header -> poll interval", quotaWaitMs(mkRes({ "retry-after": "0" }), 300000) === 300000)
check("fractional header -> rounded ms", quotaWaitMs(mkRes({ "retry-after": "1.5" }), 999) === 1500)

console.log(fail === 0 ? "\nALL TESTS PASSED" : `\n${fail} FAILED`)
process.exit(fail ? 1 : 0)