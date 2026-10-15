// Unit tests for the model profiles registry application.
const { ModelProfiles } = await import("../plugins/model-profiles.js?v=" + Date.now())

let fail = 0
const check = (name, ok) => { if (!ok) fail++; console.log(`${ok ? "PASS" : "FAIL"}  ${name}`) }

function fakeConfig() {
  return {
    provider: {
      "zai-max": { models: { "glm-5.3": {}, "glm-5.3-flash": {}, "glm-5.2": {} } },
      openrouter: { models: { "glm-5.3": {}, "glm-5.3-flash": {}, "claude-opus-5": {} } },
      opencode: { options: {} }, // provider with no models key
    },
  }
}

const p = await ModelProfiles({})
check("plugin returns config hook", typeof p.config === "function")
const cfg = fakeConfig()
p.config(cfg)

const zm = cfg.provider["zai-max"].models
const or = cfg.provider.openrouter.models

check("zai-max glm-5.3 got flagship profile",
  zm["glm-5.3"].options.reasoning_effort === "max" && zm["glm-5.3"].options.temperature === 1.0 && zm["glm-5.3"].options.thinking.type === "enabled")
check("openrouter glm-5.3 identical to zai-max (single source)",
  JSON.stringify(or["glm-5.3"].options) === JSON.stringify(zm["glm-5.3"].options))
check("glm-5.3-flash got effort high",
  zm["glm-5.3-flash"].options.reasoning_effort === "high" && or["glm-5.3-flash"].options.reasoning_effort === "high")
check("unknown glm-5.2 untouched", zm["glm-5.2"].options === undefined)
check("claude-opus-5 no-op: no options object created", or["claude-opus-5"].options === undefined)
check("provider without models key survives", cfg.provider.opencode !== undefined)

// profile precedence over existing per-provider options
const cfg2 = fakeConfig()
cfg2.provider.openrouter.models["glm-5.3"].options = { reasoning_effort: "low", custom: "keep" }
p.config(cfg2)
const o = cfg2.provider.openrouter.models["glm-5.3"].options
check("registry wins over provider options, unknown keys preserved",
  o.reasoning_effort === "max" && o.custom === "keep")

// disabled via env
process.env.MODEL_PROFILES_DISABLED = "1"
const p2 = await ModelProfiles({})
const cfg3 = fakeConfig()
if (typeof p2.config === "function") p2.config(cfg3)
check("disabled plugin leaves config untouched", cfg3.provider.openrouter.models["glm-5.3"].options === undefined)
delete process.env.MODEL_PROFILES_DISABLED

// --- lib/model-profiles.js + lib/request-body.js ---
const { applyProfileToBody, resolveProfile } = await import("../lib/model-profiles.js?v=" + Date.now())
const { withProfileBody } = await import("../lib/request-body.js?v=" + Date.now())

check("resolveProfile exact match", resolveProfile("glm-5.3")?.reasoning_effort === "max")
check("resolveProfile unknown", resolveProfile("moonshot.kimi-k2-thinking") === undefined)

const b1 = { model: "glm-5.3", reasoning_effort: "low", temperature: 0.2 }
applyProfileToBody(b1)
check("applyProfileToBody rewrites effort low -> max", b1.reasoning_effort === "max" && b1.temperature === 1.0 && b1.thinking.type === "enabled")
const b2 = { model: "kimi-k2-thinking", temperature: 0.2 }
applyProfileToBody(b2)
check("applyProfileToBody unknown model untouched", b2.temperature === 0.2 && b2.reasoning_effort === undefined)

const r1 = await withProfileBody("https://x/v1", { method: "POST", body: JSON.stringify({ model: "glm-5.3", reasoning_effort: "low" }) })
check("withProfileBody string body rewritten", JSON.parse(r1.init.body).reasoning_effort === "max")
const r2 = await withProfileBody("https://x/v1", { method: "GET" })
check("withProfileBody no body passes through", r2.init.method === "GET" && r2.init.body === undefined)
const r3 = await withProfileBody("https://x/v1", { method: "POST", body: "not json" })
check("withProfileBody non-JSON passes through", r3.init.body === "not json")
const req4 = new Request("https://x/v1/chat", { method: "POST", body: JSON.stringify({ model: "glm-5.3-flash", reasoning_effort: "low" }) })
const r4 = await withProfileBody(req4, undefined)
const r4body = await r4.input.clone().text()
check("withProfileBody Request body rewritten", JSON.parse(r4body).reasoning_effort === "high")

console.log(fail === 0 ? "\nALL TESTS PASSED" : `\n${fail} FAILED`)
process.exit(fail ? 1 : 0)
