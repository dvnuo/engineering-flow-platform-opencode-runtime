# Runtime Contract

## Overview
This repository provides an OpenCode-based runtime adapter for EFP-facing APIs.

- Portal only calls adapter APIs on port `:8000`.
- OpenCode runs as an internal dependency on `:4096` and must not be exposed externally.
- External tools subsystem removed / not supported.
- Portal provides skills only; runtime maps skills into `/workspace/.opencode/skills` and runtime state.

## Runtime topology
- Adapter service: `0.0.0.0:8000` (Portal-facing).
- Internal OpenCode server: `127.0.0.1:4096` (runtime-only).
- Runtime workspace root: `/workspace`.

## Non-goals
- No EFP external tools subsystem.
- No external tool wrappers generated from manifests.
- No tools index contract.
- No wrapper mapping contract from source skill metadata.

## Required environment variables
- `EFP_RUNTIME_TYPE=opencode`
- `EFP_WORKSPACE_DIR`
- `EFP_SKILLS_DIR`
- `EFP_ADAPTER_STATE_DIR`
- `OPENCODE_DATA_DIR`
- `OPENCODE_CONFIG`
- `EFP_PROFILE_CONFIG` (rendered profile payload JSON from the per-profile Secret)
- `EFP_PROFILE_REVISION` (profile revision string from the Secret)
- `EFP_PROFILE_ID` (profile id, or `none` for unbound agents)

## Required mounted directories
- `/workspace`
- `/workspace/.opencode/skills`
- `/root/.local/share/opencode`
- `/root/.local/share/efp-compat`

## Portal-facing endpoints
At minimum, runtime provides:
- `/health`
- `/actuator/health`
- `/ready`
- `/api/chat`
- `/api/chat/stream`
- `/api/sessions`
- `/api/skills`
- `/api/capabilities`
- `/api/permissions/respond`
- `/api/personalization`
- `/api/workspace/repos`
- `/api/sessions/{session_id}/pending-input`
- `/api/sessions/{session_id}/permission/respond`
- `/api/tasks/execute`
- `/api/queue/status`
- `/api/usage`
- `/api/copilot/quota`
- `/api/server-files/*`
- `/api/events/ws`

## Usage and Copilot credits
- `/api/chat` answers carry `usage` with the run's `input_tokens` (the whole
  prompt, cached tokens included), `cached_input_tokens`, `cache_write_tokens`,
  `output_tokens`, `reasoning_tokens`, `iterations` (assistant steps),
  `cache_hit_ratio`, `estimated_credits` (GitHub AI credits, 1 credit = $0.01,
  from the published Copilot price table in `copilot_pricing.py`; null for a
  model the table does not know) and `pricing` (`model`, `tier` `default` or
  `long_context`, `threshold_tokens`, `source: static`). OpenCode itself
  reports a cost of 0 for Copilot models, so the estimate is what the Portal
  shows.
- `/api/usage` sums the same fields per model and provider over the period
  (`total_cached_input_tokens`, `total_reasoning_tokens`,
  `total_estimated_credits` next to the older totals).
- `/api/sessions/{session_id}/context-usage` adds `budget_tokens` (the
  declared window minus the output reserve), `billing_threshold_tokens` (the
  prompt size above which the long-context tier bills, 272,000 for the GPT-6
  line) and `billing_tier` for the latest request.
- `/api/copilot/quota` reads GitHub's internal `copilot_internal/user` with the
  member's Copilot credential and returns `available`, `plan`, `reset_date`,
  `reset_date_utc`, `token_based_billing`, `snapshots` (one per quota id with
  `entitlement` / `remaining` / `used` / `credits_used` / `percent_remaining` /
  `unlimited` / `has_quota` / `token_based_billing` / `reset_at` / overage
  fields), `credits`, `raw_keys` and `error`. Under usage-based billing
  (`token_based_billing: true`) the `premium_interactions` snapshot counts AI
  credits and is projected as `credits` `{id, label: "AI credits", included,
  used, remaining, percent_remaining, unlimited, pooled, has_quota, exhausted,
  overage_count, overage_permitted, reset_at}`: a per-seat allotment has
  `included` / `remaining`, a pooled organization allotment is `unlimited` per
  user (`included` and `remaining` null), `used` is `credits_used` and
  `exhausted` means `has_quota: false`. The endpoint is unofficial on
  GitHub's side, so a changed shape degrades to `available: false` rather
  than failing; answers are cached for a minute.
- opencode.json declares the GPT-6 models with the default pricing tier's
  window (400,000 tokens: 272k prompt + 128k output) so OpenCode compacts
  before a request bills at long-context rates; `EFP_OPENCODE_COPILOT_LONG_CONTEXT=1`
  declares the catalog's full windows instead.

## Assistant personalization
`/api/personalization` serves the greeting and starter cards from the agents
repository branch this assistant booted with. Portal's init container copies the
repo's optional `portal/` directory into the workspace alongside `AGENTS.md` and
`instructions/`, so the runtime reads what is already on disk rather than having
Portal clone the repo a second time.

- `portal/welcome.md` -- greeting shown when a chat has no messages yet.
- `portal/cards.yaml` -- starter prompts offered as clickable cards.

Both files are optional. A branch without them returns
`{"welcome": null, "cards": []}` and Portal falls back to its generic welcome.

A card needs a `title` and at least one way to build its prompt:

- `prompt`, with an optional `input` (`label`, `placeholder`) -- the legacy
  form; what the member types replaces `{{input}}`. An `input` may also carry
  `source` (the id of a picker Portal offers, such as `github.branches`),
  `params` (a small flat mapping handed to that source) and `multiline: true`.
- `fields` + `template` -- a form of one to six named fields; each value
  replaces its `{{name}}` in the template. A field has `name`, `label`,
  `type` (`text`, `textarea`, `choice` or `file`; `text` when missing),
  `placeholder` and `required` (true unless it says otherwise), plus `source`
  / `params` on a `text` field, `options` (`{value, label}`) and `multiple`
  on a `choice`, `accept` (lower-cased extensions) on a `file`, and an
  optional `default` (a string or a list of strings, as written; on a
  `choice` only values among its options, and only the first unless it is
  `multiple`).

The runtime only checks shapes; what a `source` means is Portal's business.
Invalid keys are dropped, never raised. `fields` and `template` are valid
together or dropped together: an unknown `type`, a missing, malformed or
repeated `name`, a `choice` with no option, a seventh field, or a template
placeholder that names no field drops both, and the card falls back to its
`prompt` if it has one.

Without a version the endpoint answers in the legacy shape -- `{title,
description, icon, prompt, input?}`, leaving out any card that has no
`prompt` -- so an older Portal never receives a card it cannot send. With
`?v=2` (any integer of 2 or more) a card also carries its `fields` and
`template`, and a card that only has the form is sent with `prompt: ""` and
no `input`.

## Workspace repositories
`GET /api/workspace/repos` lists the git repositories cloned in the workspace,
for Portal's "branches in this workspace" picker. It is read-only and takes no
parameters: which directories are looked at is never the caller's choice.

The answer is `{"repos": [...], "truncated": bool}`. Each entry has `path`
(workspace-relative, forward slashes), `name`, `remote_url` (the origin URL
without its userinfo), `host` and `repo` (parsed from an https, `ssh://` or
`git@host:owner/name.git` origin; both empty without one), `branch` (empty on
a detached HEAD), `head`, `upstream` (empty when the branch tracks nothing),
`default_branch` (from `refs/remotes/origin/HEAD`, empty when unknown),
`ahead` (commits the upstream does not have, else `origin/<default_branch>`;
null when there is neither), `dirty` (tracked files changed; null when git
did not answer in time) and `last_commit_at` (UTC, ISO 8601).

- Scanned, in this order: `repos/*`, `repos/*/*`, `EFP_WORKSPACE_REPOS_DIR`
  (the same two levels, when it is set to another directory inside the
  workspace), `.efp_runtime/repositories/*` and the workspace root's direct
  children. A repository is a directory holding a `.git` entry. One that
  resolves outside the workspace is skipped, and the workspace root itself is
  never listed.
- At most 30 repositories, newest commit first. Every git command is a local
  query with a 3 second timeout, `GIT_TERMINAL_PROMPT=0` and
  `GIT_OPTIONAL_LOCKS=0`: nothing is fetched and nothing is written. The
  whole listing has an 8 second budget; `truncated` is true when it ran out or
  when more than 30 repositories were found.
- A repository git cannot read (no commit yet, a broken clone) is left out
  rather than failing the listing.

## Chat attachments

- `/api/chat` `attachments` are file ids from `/api/files/upload`. Text-like
  files are parsed into the prompt; PDFs go to OpenCode as file parts, and so
  do images while the chat provider can see them (AI Platform, or Copilot
  with `EFP_COPILOT_VISION_VIA_CHAT=1`). The boot projection records that
  as `vision_via_chat`, shown under `runtime_integrations.attachments` in the
  effective-config view.
- Images the provider cannot see, and the office formats the adapter does not
  parse (`application/msword`, `application/vnd.ms-excel`: doc, dot, xls),
  stay under the workspace and are described to the agent in an "Attached
  files" handoff block instead (`efp_opencode_adapter/attachment_handoff.py`).
  This runtime has no browser tool, so the block tells the agent the files
  cannot be analyzed here and never to guess what they show; the native
  runtime with the Local bridge connector analyzes them through Productivity
  Suite.

## Reply language
The Portal sends the member's language preference in the trusted
`metadata.portal_user` object of every proxied chat, stream, question and
permission answer, message-edit and `title/generate` request:

- `metadata.portal_user.reply_language`: `en`, `zh-Hans` or `zh-Hant-HK`.
- `metadata.portal_user.reply_language_strict`: `true` when the member chose
  the Portal's language, the account menu's only language setting (always
  reply in it until they ask to switch in the chat; it wins over saved
  notes); `false` when it follows the browser, so it is only a default: the member's own
  messages and requests about the reply language win over it, while messages
  the Portal sends on the member's behalf, such as "Continue", do not.

A missing or unknown value, and `en` without `reply_language_strict: true`,
change nothing: the request behaves exactly as it did before the setting
existed. Otherwise the adapter renders the "Reply language" block (texts in
`efp_opencode_adapter/prompts/reply_language.json`, shared verbatim with the
native runtime) and puts it first in the per-turn `system` string, ahead of
the request's own `system` text and the runtime-contract suffix; OpenCode
appends that string at the very end of its system prompt for every step of
the turn. The block is never a message part. It reaches the chat and stream
paths, slash-skill prompts and both message-edit paths; OpenCode 1.14.39's
`/session/{id}/command` body has no `system` field, so a native OpenCode
command runs without it. With a block in force, session titles follow the
language of the assistant's answer instead of the user's message.

## Session user
The same `metadata.portal_user` object names the member who is asking:
`username`, `display_name`, `id` and, when the Portal knows it,
`github_login`. An assistant's CLIs sign in with the member's Portal
connectors, usually a shared service account, so the adapter renders those
fields as a "Session user" block with identity rules
(`efp_opencode_adapter/session_user.py`, the rules shared verbatim with the
native runtime) at the front of the per-turn `system` string, after the
"Reply language" block when there is one: "my tickets" then means the
member's, not the service account's. A request without `portal_user`, or with
neither a `username` nor a `display_name` in it, keeps its `system` string
unchanged.

## Interactive input contract
Portal drives its approval card off the session rather than the permission id:
it polls `/api/sessions/{session_id}/pending-input` to rebuild a card after a
refresh, and posts the answer to
`/api/sessions/{session_id}/permission/respond` with
`{"request_id": ..., "decision": "approve"|"deny", "always": bool}`.

OpenCode keeps no record of an unanswered permission -- `GET /session/{id}`
does not report one, and the only trace is the event that announced it. The
adapter therefore reconstructs pending state from the event stream, and
`permission.requested` events carry an assembled `data.permission_request`
object for the live card.

`/api/sessions/{session_id}/question/respond` answers `501
question_response_unsupported`. OpenCode's server exposes
`/session/{id}/permissions/{permissionID}` and no question route, so a typed
answer cannot be delivered back to a `question` tool call on this runtime;
`pending-input` accordingly always reports `question_request: null`. Assistants
that depend on the question tool belong on the native runtime.

## Internal-only OpenCode server
OpenCode is an implementation detail behind adapter APIs.
- It is bound to `:4096` internal loopback.
- It must not be exposed as a direct Portal target.

## Skills asset mapping
- Portal provides skills only (`EFP_SKILLS_DIR`, default `/app/skills`).
- Adapter syncs source skills into `/workspace/.opencode/skills` and writes `skills-index` state.
- Source directory skills may use `<skill-name>/SKILL.md` (preferred) or legacy `<skill-name>/skill.md`.
- Top-level Markdown skills with frontmatter remain supported.
- OpenCode output always uses `/workspace/.opencode/skills/<normalized-name>/SKILL.md`.
- Directory skill sidecar resources are recursively copied into the same output skill directory, including `scripts/`, `templates/`, `reference/`, `examples/`, and other regular files.
- Source entry files are not copied as resources, cache directories are skipped, and symlinks are skipped.
- Skills are synced during asset initialization and before managed OpenCode startup or restart.
- `tools` / `task_tools` in source skill frontmatter are informational metadata only.
- Source metadata is not interpreted as runtime executable wrapper mappings.
- `/api/skills` reports `repo_path`: the skill's source file relative to the skills checkout root, so Portal can link a slash command to the branch the assistant booted with. It is empty when the source cannot be placed under `EFP_SKILLS_DIR`. The native runtime answers with the same field; a skill's folder and its declared name may differ, so this cannot be derived from the name.

## State persistence contract
Persisted state directories:
- OpenCode runtime state: `/root/.local/share/opencode`
- Adapter state: `/root/.local/share/efp-compat`

State should survive runtime restarts when mounted persistently.

## Task restart contract
The adapter follows upstream OpenCode's recovery boundary: durable
session/history state may be reused, but in-flight provider/tool activity from a
previous process is not automatically replayed on adapter startup. Active task
records found during startup are marked `blocked` with
`adapter_restarted_task_recovery_required`; callers should re-dispatch the task
when the work is still required.

Task state loading and persistence are bounded:
- `EFP_OPENCODE_TASKS_LIST_MAX_RECORDS` limits task records returned by store
  list operations. The default is `512`.
- `EFP_OPENCODE_TASKS_SCAN_MAX_RECORDS` limits candidate task files scanned
  while listing. The default is `1024`.
- `EFP_OPENCODE_TASKS_LOAD_MAX_FILE_BYTES` skips individual task files larger
  than the configured byte limit. The default is `2000000`.
- `EFP_OPENCODE_TASKS_PERSIST_MAX_FILE_BYTES` caps each persisted task record.
  Large payloads are replaced with a diagnostic omission marker while identity,
  status, error, and bounded event context remain available. The default is
  `2000000`.
- `EFP_OPENCODE_TASKS_PERSIST_EVENT_TAIL` limits runtime events retained when a
  task record must be minimized. The default is `50`.

## Runtime profile boot/status contract
Profile config is delivered exclusively through pod env at container start;
there is no apply endpoint and no hot-apply path.

- Delivery: the Portal renders each profile into a per-profile Secret and
  injects it as pod env — `EFP_PROFILE_CONFIG` (full payload JSON, key
  `opencode.json`), `EFP_PROFILE_REVISION`, and `EFP_PROFILE_ID`.
- Boot projection: the adapter parses `EFP_PROFILE_CONFIG` once at startup and
  projects it into runtime assets (opencode.json, auth.json, opencode.env,
  git/gh auth assets, atlassian/mobile CLI config, the aws node with its
  account matrix for aws-auth plus AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE /
  KUBECONFIG under the adapter state dir), then removes the
  blob from its process env before the managed OpenCode child starts. The
  child env never contains `EFP_PROFILE_CONFIG`.
- Proxy: the profile's `proxy` section is the Portal's Proxy connector, a
  list of named proxies (`proxies`), the `default` one, and `assignments`
  (connector type -> proxy name, `none`, or empty for the default). The
  default proxy goes to the child env as `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`
  and `NO_PROXY` (which always exempts loopback, so opencode reaches the
  adapter's own proxies); the proxy assigned to the model provider reaches the
  adapter's copilot and AI Platform loopback proxies through `EFP_LLM_PROXY`
  (`none`, or a URL) and `EFP_LLM_NO_PROXY`; the other connectors' proxies
  travel in their own tool config (`EFP_<PRODUCT>_INSTANCES_<i>_PROXY`,
  mobile-auto's `browserstack.http_proxy`/`local`). Every proxy's
  credentials are exported as
  `EFP_PROXY_<NAME>_USERNAME`/`_PASSWORD` for mobile-auto. The flat
  `proxy.url` shape of older profiles reads as one proxy named `default`.
  See `efp_opencode_adapter/proxy_plan.py`.
- Failure semantics: a missing `EFP_PROFILE_CONFIG` env var is a fatal pod
  misconfiguration (the adapter stays alive but unready); an empty
  `"config": {}` payload is a valid empty profile (base config).
- Activation is restart-only: config changes reach a pod only via a
  Portal-triggered restart with an updated Secret. The managed OpenCode
  watchdog only revives the child with the boot-time env.
- Readiness: `GET /ready` returns 200 with
  `{"ready": true, "runtime_profile_id": ..., "revision": ...}` only after the
  boot projection succeeded and the managed OpenCode child is healthy;
  otherwise 503 with `{"ready": false, "error": ...}`.
- Status endpoint reports the running revision from the pod env plus the boot
  projection record (warnings, hashes, per-integration configured flags).
- Effective config endpoint exposes sanitized runtime configuration and integration status.

## Runtime contract tests
Quick contract check:

```bash
python -m pytest -q runtime_contract_tests
```

Live runtime check against a running adapter:

```bash
RUNTIME_BASE_URL=http://localhost:8000 python -m pytest -q runtime_contract_tests
```

## Live LLM checks are opt-in
Optional live checks are guarded and skipped by default unless explicitly enabled:
- `RUNTIME_CONTRACT_ENABLE_CHAT=1`
- `RUNTIME_CONTRACT_ENABLE_TASKS=1`

## Failure modes and expected status
- Health degradation returns non-200 when internal OpenCode or state readiness fails.
- Optional live checks skip when required env is not present.
- Task polling may return transient states (`accepted`, `running`) before terminal states.
- Chat/tool execution can return empty final payload; callers should handle `empty_final` and `ok=false` states gracefully.
