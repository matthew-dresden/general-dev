// generated-agents.test.mjs - reconciliation test for the generated agent set.
// Source of truth: opencode.json + lib/agents.inputs.json -> lib/generate-agents.mjs.
// Run: node tests/generated-agents.test.mjs   (exit 0 = in sync)
//
// Container copy: this suite runs from the INSTALLED config's tests/
// directory (~/.config/opencode/tests), where ROOT resolves to the installed
// config, never from the committed .devcontainer/opencode tree (that copy is
// the source the postcreate copies into place). The shipped config has no
// default_agent and no judge agents: default_agent is a per-user seam value
// (optional, set through project-setup overrides) and the six judges are
// host-side hand-written agents, not generated - so this copy pins the
// generated frontmatter contracts instead of the judge files.

import { execFileSync } from "node:child_process"
import { readFileSync, existsSync } from "node:fs"
import { join, dirname } from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..")
const manifest = JSON.parse(readFileSync(join(ROOT, "lib", "generated-agents.json"), "utf-8"))
const inputs = JSON.parse(readFileSync(join(ROOT, "lib", "agents.inputs.json"), "utf-8"))
const configText = readFileSync(join(ROOT, "opencode.json"), "utf-8")

let fail = 0
const check = (name, ok, detail = "") => {
  if (!ok) fail++
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` - ${detail}` : ""}`)
}

// 1. generator --check exits 0: every generated file byte-matches regeneration
try {
  execFileSync("node", [join(ROOT, "lib", "generate-agents.mjs"), "--check"], { stdio: "pipe" })
  check("generator --check: files in sync", true)
} catch (err) {
  check("generator --check: files in sync", false, String(err.stderr || err))
}

// 2. expected agent count: providers x models x (roles + worker)
const modelCount = inputs.providers.flatMap((p) => inputs.models[p]).length
check("manifest agent count", manifest.agents.length === modelCount * (inputs.roles.length + 1),
  `${manifest.agents.length} != ${modelCount} x (${inputs.roles.length}+1)`)

// 3. policy keys equal the provider list; prefixes follow the naming rule
const policyOk = JSON.stringify(Object.keys(manifest.policy).sort()) === JSON.stringify([...inputs.providers].sort()) &&
  Object.entries(manifest.policy).every(([p, prefixes]) => prefixes.every((x) => x === `${p}-glm-*`))
check("policy map reconciles with inputs", policyOk, JSON.stringify(manifest.policy))

// 4. depth in manifest matches opencode.json subagent_depth
const depthInConfig = Number(/"subagent_depth":\s*(\d+)/.exec(configText)?.[1])
check("depth matches config", manifest.depth === depthInConfig, `manifest=${manifest.depth} config=${depthInConfig}`)

// 5. default_agent is a per-user seam value: absent from the shipped config
// (and therefore from the installed config until a per-user override sets
// it). When it IS present - through an override - it must be a generated
// build agent.
const defaultAgent = /"default_agent":\s*"([^"]+)"/.exec(configText)?.[1]
const defaultOk = defaultAgent === undefined ||
  manifest.agents.some((a) => a.name === defaultAgent && a.role === "build")
check("default_agent, when set, is a generated build agent", defaultOk, String(defaultAgent))

// 6. every provider-model pair has its worker file, and build/plan for it
for (const provider of inputs.providers) {
  for (const model of inputs.models[provider]) {
    for (const role of [...inputs.roles, "worker"]) {
      const name = role === "worker" ? `${provider}-${model}` : `${role}-${provider}-${model}`
      const ok = existsSync(join(ROOT, "agent", `${name}.md`))
      check(`agent file exists: ${name}`, ok)
    }
  }
}

// 7. generated frontmatter contracts, pinned from the generator's own output.
const frontmatter = (file) => readFileSync(join(ROOT, "agent", file), "utf-8").split("---")[1] ?? ""

for (const provider of inputs.providers) {
  // Flagship plan agents may reach BOTH same-provider workers; flash plan
  // agents stay locked to their exact flash worker.
  const fullModel = inputs.models[provider].find((m) => !m.endsWith("flash"))
  const flashModel = inputs.models[provider].find((m) => m.endsWith("flash"))
  if (fullModel) {
    const fm = frontmatter(`plan-${provider}-${fullModel}.md`)
    check(`flagship plan task rule: ${provider}`, fm.includes(`"${provider}-glm-*": "allow"`))
  }
  if (flashModel) {
    const fm = frontmatter(`plan-${provider}-${flashModel}.md`)
    check(`flash plan locked: ${provider}`,
      fm.includes(`"${provider}-${flashModel}": "allow"`) && !fm.includes(`"${provider}-glm-*"`))
  }
  // Plan agents gate bash behind ask with the read-only allowlist, deny edit.
  for (const model of inputs.models[provider]) {
    const fm = frontmatter(`plan-${provider}-${model}.md`)
    check(`plan bash gate + edit deny: ${provider}-${model}`,
      fm.includes(`"*": "ask"`) && fm.includes(`"git grep*": "allow"`) && fm.includes(`"edit": "deny"`))
    // Build agents carry NO bash rule: they inherit full permissions.
    const buildFm = frontmatter(`build-${provider}-${model}.md`)
    check(`build has no bash rule: ${provider}-${model}`, !buildFm.includes("bash"))
  }
}

console.log(fail === 0 ? "\nALL TESTS PASSED" : `\n${fail} FAILED`)
process.exit(fail ? 1 : 0)
