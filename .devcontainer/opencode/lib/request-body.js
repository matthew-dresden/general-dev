// Transport-level enforcement of the model profile registry (see
// lib/model-profiles.js) for OpenAI-compatible request bodies.
//
// OpenCode synthesizes reasoning fields itself (variant default low; max is
// not offered for openai-compatible providers) and its values override both
// provider model options and chat.params, so the fetch boundary in the Z.AI
// wrappers is where the registry gets the final word.
//
// withProfileBody(input, init, log) returns { input, init } with the JSON
// body rewritten to match the profile for body.model. Non-JSON or absent
// bodies (GET /models, health probes) pass through untouched - that is
// correct passthrough, not a swallowed error. Malformed JSON is passed
// through with a logged reason so the provider surfaces the real error.
import { applyProfileToBody, resolveProfile } from "./model-profiles.js"

export async function withProfileBody(input, init, log = () => {}) {
  let bodyText
  if (init?.body != null) {
    bodyText = typeof init.body === "string" ? init.body : null
    if (bodyText === null) {
      log("profile skip: non-string init.body")
      return { input, init }
    }
  } else if (input instanceof Request) {
    try {
      bodyText = await input.clone().text()
    } catch (err) {
      log(`profile skip: request body unreadable (${err?.message || String(err)})`)
      return { input, init }
    }
  } else {
    return { input, init }
  }

  if (!bodyText || !bodyText.trim().startsWith("{")) {
    log("profile skip: no JSON body")
    return { input, init }
  }

  let body
  try {
    body = JSON.parse(bodyText)
  } catch (err) {
    log(`profile skip: malformed JSON (${err?.message || String(err)})`)
    return { input, init }
  }

  if (!resolveProfile(body.model)) {
    return { input, init }
  }

  const before = bodyText
  applyProfileToBody(body)
  const after = JSON.stringify(body)
  if (after === before) return { input, init }

  log(`profile enforced: ${body.model}`)
  if (input instanceof Request) {
    return { input: new Request(input, { body: after }), init }
  }
  return { input, init: { ...init, body: after } }
}
