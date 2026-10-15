// bash-allowlist.test.mjs - guards the plan-agent read-only bash allowlist.
//
// Emulates opencode's documented wildcard semantics ("*" matches zero or more
// of any character, "?" exactly one, all else literal) and asserts that:
//   1. every real-world read-only command observed in permission prompts matches
//   2. dangerous look-alikes never match
// Source of truth: lib/generated-agents.json (readOnlyBash), which the
// generator emits from READ_ONLY_BASH.
//
// Container copy: this suite runs from the INSTALLED config's tests/
// directory, never from the committed .devcontainer/opencode tree. Its
// observed-command fixtures use neutral project names on purpose: the
// committed tree carries no organization naming, and the allowlist's
// behavior does not depend on any project's identifiers.
//
// Run: node tests/bash-allowlist.test.mjs   (exit 0 = allowlist correct)

import { readFileSync } from "node:fs"
import { join, dirname } from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..")
const manifest = JSON.parse(readFileSync(join(ROOT, "lib", "generated-agents.json"), "utf-8"))
const patterns = manifest.readOnlyBash
if (!Array.isArray(patterns) || patterns.length === 0) {
  console.error("FAIL  manifest has no readOnlyBash; regenerate agents")
  process.exit(1)
}

// opencode docs: "*" = zero or more of any character, "?" = exactly one,
// all other characters match literally. Last matching rule wins, and the
// base rule is "*": "ask", so a segment runs without prompting iff it
// matches at least one allow pattern here.
function compiles(pattern) {
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")
  return new RegExp(`^${escaped}$`)
}
const compiled = patterns.map(compiles)
const allowed = (cmd) => compiled.some((re) => re.test(cmd))

let fail = 0
const check = (name, ok, detail = "") => {
  if (!ok) fail++
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` - ${detail}` : ""}`)
}

// Real-world compound segments observed in permission prompts. Every segment
// of these compounds must run without prompting.
const observedReadOnly = [
  // git + gh sweep
  "git fetch origin -q",
  "git status --short",
  "git log origin/main..HEAD --oneline",
  "wc -l",
  "tr -d ' '",
  "git branch",
  "gh run list --repo example-org/example-repo --workflow ci.yml --limit 1 --json conclusion,headBranch --jq '.[0]'",
  // git grep sweep (the read-only worktree search family)
  "git grep -n 'TODO' -- '*.md'",
  "git grep -c examplePattern",
  "git grep -n pattern origin/main -- '*.md'",
  // text pipeline stages
  "grep -E \"alpha|beta|gamma\"",
  "head -5",
  "echo \"(end)\"",
  // gcp sweep
  "gcloud compute instances list --project example-project --format=\"value(name,zone)\" 2>/dev/null",
  "gcloud compute disks list --project example-project",
  "gcloud compute networks list --project example-project",
  "gcloud compute firewall-rules list --project example-project",
  "gcloud storage buckets list --project example-project",
  "gcloud secrets list --project example-project --format=\"value(name.basename())\"",
  "gcloud compute routers list --project example-project",
  "gcloud compute routers nats list --router example-router --region us-east4 --project example-project",
  // azure sweep
  "az group list --query \"[?starts_with(name,'example-spike')]\" -o tsv 2>/dev/null",
  "az storage account list --query \"[?starts_with(name,'example-state')]\" -o tsv 2>/dev/null",
  "az ad app list --display-name \"example-e2e\" --query \"[].appId\" -o tsv",
  "az relay namespace list --query \"[?starts_with(name,'example-relay')]\" -o tsv 2>/dev/null",
  // aws sweep
  "aws ec2 describe-instances --region us-east-1 --filters \"Name=tag:Environment,Values=remote-dev\" --output text 2>/dev/null",
  "aws s3api list-buckets --query \"Buckets[?starts_with(Name,'example-state-')]\" --output text 2>/dev/null",
  // docker sweep
  "docker context ls --format '{{.Name}}'",
  "docker context inspect example-context",
  "docker ps -a",
  "docker images",
  "ls remote-instances/",
  "ls /tmp/example-* 2>/dev/null",
  "break",
  "continue",
]
for (const cmd of observedReadOnly) {
  check(`allowed: ${cmd.slice(0, 60)}`, allowed(cmd))
}

// Dangerous look-alikes must NEVER match. Each mirrors an allowed family
// closely enough to catch an over-broad pattern edit.
const mustDeny = [
  "git push origin main",
  "git reset --hard HEAD~1",
  "git commit -m x",
  "rm -rf /tmp/x",
  "gh pr merge 12 --squash",
  "gh api repos/x/y -X DELETE",
  "gcloud secrets versions access latest --secret my-secret",
  "gcloud compute instances delete example-vm",
  "aws ssm get-parameter --name /app/secret --with-decryption",
  "aws secretsmanager get-secret-value --secret-id x",
  "aws s3 rm s3://bucket/x",
  "az keyvault secret show --vault-name v --name s",
  "az group delete --name rg-x --yes",
  "az storage account delete --name x",
  "sed -i 's/a/b/' file.txt",
  "kubectl delete pod x",
  // interpreters can execute arbitrary code; never allowlisted
  "python3 -c 'import os; os.remove(\"/x\")'",
  "node -e 'require(\"fs\").rmSync(\"/x\", {recursive: true})'",
  "python3 - <<'PY'",
  "awk '{print $1}' /etc/passwd",
  "awk 'BEGIN{system(\"touch /tmp/pwned\")}'",
  "for f in *; do rm -rf \"$f\"; done",
]
for (const cmd of mustDeny) {
  check(`denied:  ${cmd.slice(0, 60)}`, !allowed(cmd))
}

console.log(fail === 0 ? "\nALL TESTS PASSED" : `\n${fail} FAILED`)
process.exit(fail ? 1 : 0)
