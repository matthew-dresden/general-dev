// Model profiles plugin: applies the shared registry at config load and at
// chat-params time. The registry itself and its rationale live in
// lib/model-profiles.js (single source of truth), which the Z.AI transport
// wrappers also use to enforce the profiles on the wire - OpenCode's
// reasoning synthesis (variant default low, no max for openai-compatible)
// overrides both config options and chat.params, so the fetch boundary has
// the final word for Z.AI.
//
// Env overrides:
//   MODEL_PROFILES_DISABLED=1   skip application entirely
//   MODEL_PROFILES_LOG=<path>   append applied profiles to this file
import { MODEL_PROFILES, resolveProfile } from "../lib/model-profiles.js"

export const ModelProfiles = async () => {
  if (process.env.MODEL_PROFILES_DISABLED === "1") return {}

  const logPath = process.env.MODEL_PROFILES_LOG
  let appendFileSync
  if (logPath) {
    ;({ appendFileSync } = await import("node:fs"))
  }
  const log = (msg) => {
    if (!logPath) return
    appendFileSync(logPath, `${new Date().toISOString()} ${msg}\n`)
  }

  return {
    "chat.params": (input, output) => {
      const profile = resolveProfile(input.model?.id)
      if (!profile) return
      for (const [key, value] of Object.entries(profile)) {
        output.options[key] = value
      }
      if (typeof profile.temperature === "number") output.temperature = profile.temperature
      log(`chat.params ${input.provider?.id}/${input.model?.id}: ${Object.keys(profile).join(",") || "no-op"}`)
    },

    config: (cfg) => {
      const applied = []
      for (const [providerId, provider] of Object.entries(cfg.provider || {})) {
        if (!provider?.models) continue
        for (const [modelId, model] of Object.entries(provider.models)) {
          const profile = resolveProfile(modelId)
          if (!profile || Object.keys(profile).length === 0) continue
          model.options = { ...(model.options || {}), ...profile }
          applied.push(`${providerId}/${modelId}`)
        }
      }
      log(`config applied ${applied.length} profiles: ${applied.join(", ") || "none"}`)
    },
  }
}

export default ModelProfiles
