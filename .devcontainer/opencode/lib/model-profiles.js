// Shared model profile registry: single source of truth for per-model
// request parameters.
//
// Lives outside plugins/ so OpenCode's auto-loader never evaluates it as a
// plugin. Consumed by plugins/model-profiles.js (config + chat.params) and
// by the Z.AI transport wrappers, which enforce the registry on the wire:
// OpenCode's own reasoning machinery synthesizes reasoning_effort (variant
// default low, and max is not offered for openai-compatible providers),
// overriding both provider model options and chat.params, so the last word
// belongs at the fetch boundary.
//
// Profile values are vendor-sourced, not guessed:
//   - GLM 5.3 / 5.3-flash: docs.z.ai/guides/llm/glm-5.3 - temperature 1.0,
//     reasoning always enabled, reasoning_effort low/high/max; Z.AI
//     recommends max for coding on the flagship. Flash runs high to keep the
//     dev-delegate role fast without gutting depth.
//   - Claude Sonnet 5 / Opus 5: deliberate no-ops. models.dev marks
//     temperature non-tunable for the Claude 5 series and Anthropic tunes
//     them to run at defaults; overriding degrades output.
export const MODEL_PROFILES = {
  "glm-5.3": {
    temperature: 1.0,
    thinking: { type: "enabled" },
    reasoning_effort: "max",
  },
  "glm-5.3-flash": {
    temperature: 1.0,
    thinking: { type: "enabled" },
    reasoning_effort: "high",
  },
  "claude-opus-5": {},
}

export function resolveProfile(id) {
  return MODEL_PROFILES[String(id)]
}

// Pure helper: apply the matching profile to a parsed request body object.
// Returns the body unchanged when the model is unknown or the profile is a
// deliberate no-op. Mutates in place and returns the same object.
export function applyProfileToBody(body) {
  if (!body || typeof body.model !== "string") return body
  const profile = resolveProfile(body.model)
  if (!profile) return body
  for (const [key, value] of Object.entries(profile)) {
    body[key] = value
  }
  return body
}
