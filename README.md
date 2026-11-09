# Open-WebUI Companion Server

## About this fork

This is a fork of [L3tum/open-webui-companion](https://github.com/L3tum/open-webui-companion),
maintained for a self-hosted **Gitea** deployment rather than GitHub. It tracks upstream's core,
with the Gitea integration reworked and extended: branch management, PR merging, draft handling,
and a set of API-behaviour fixes that upstream does not carry.

Changes from upstream, in brief:

- **Branch tools** -- `GiteaListBranches` and `GiteaCreateBranch`; upstream has neither.
- **`GiteaMergePR`** -- structured refusals instead of passing Gitea's status codes through.
- **Draft PRs** -- `draft` is translated into the `WIP:` title prefix Gitea actually honours;
  upstream sends a `draft` field that Gitea silently discards.
- **Compatibility fixes** -- PR diff route, review `event`/`type`, per-review comments, the
  Actions jobs wrapper, and 204/empty-body handling. See [Compatibility notes](#compatibility-notes).
- **Reports stored state** -- state-changing tools echo what Gitea recorded, never what was asked for.

Upstream's own notice -- that it was a spare-time project superseded by other MCP servers -- no
longer describes this branch, so it has been removed rather than carried over.

### AI-authored

**This fork is written by AI.** The code, tests and documentation here were produced by a large
language model working against a live Gitea instance, with a human directing the work, choosing
the scope and reviewing the output.

That has consequences worth weighing before relying on it:

- **Behaviour was established by probing, not by reading docs.** Several bugs were only found by
  running against a real Gitea 28.1.0 -- a closed-unmerged PR answering `404`, and mergeability
  being computed *asynchronously*. Those findings are recorded inline because they are not
  obvious from the API surface.
- **The tests pin behaviour, not the correctness of the intent behind it.** They are
  mutation-checked where it matters (reverting a fix fails a test), but a misunderstanding
  shared between the code and its tests would survive both.
- **Review it as you would any unfamiliar dependency**, especially the destructive tools
  (`GiteaClosePR`, `GiteaDeleteComment`, and anything that writes).
- **No warranty.** See [License](#license).

A FastAPI-based companion server for [Open-WebUI](https://github.com/open-webui/open-webui) that provides persistent tools for evidence management, Gitea integration, and Open-WebUI notes synchronization.

**Current version: 1.5.3.** The running service reports it at `/health`, and `app/__init__.py`
is its single source of truth -- the two are asserted equal at release time.

## 🔑 Secrets

**No live credential is stored in this repository.** An earlier state of this project tracked
`.env`, so a Gitea token and an Open-WebUI key do exist in **git history** (15 commits). Both were
revoked at the issuer and verified dead (HTTP `401`); see
[If you ever commit a credential](#if-you-ever-commit-a-credential). Treat any credential that was
ever committed as public regardless of the current tree.

Today, `GITEA_TOKEN` and `OWUI_TOKEN` come from
**Gitea secrets** and are interpolated into the container by `docker compose` during deploy.

| Secret | Purpose |
|--------|---------|
| `COMPANION_GITEA_TOKEN` | Personal access token used by every Gitea tool |
| `OWUI_TOKEN` | Open-WebUI API key used by the notes sync |

> **Name restriction.** Gitea rejects Actions **secrets and variables** whose names begin with
> `GITEA_` or `GITHUB_`. The token is therefore stored as `COMPANION_GITEA_TOKEN` and the
> instance URL as `MY_GITEA_INSTANCE_URL`, both mapped onto their real names in the workflow's
> `env:` block. The names the *container* receives are unchanged.
>
> This matters because an unusable name is accepted at creation time and then resolves to an
> empty string at run time — it does not error. A variable named `GITEA_INSTANCE_URL` silently
> produces a blank URL.

`docker-compose.yml` references them as `${GITEA_TOKEN:?...}` and `${OWUI_TOKEN:?...}`.
The `:?` form is deliberate — see the failure modes below.

> **`.env` is development-only and gitignored.** `docker compose` no longer uses
> `env_file`, so nothing in `.env` reaches the deployed container. Only
> `app/config.py`'s `load_dotenv()` reads it, and only when the service runs outside
> Docker. Copy `.env.example` if you want to run it locally.

### Rotation

Secrets are injected at **container creation**, so editing a Gitea secret does not affect the
running container until it is redeployed. Re-run the deploy from **Actions → deploy → Run
workflow** (the workflow is `workflow_dispatch`; see "Deploy trigger" below).

Two steps guard against a half-migrated deploy. `Preflight (secrets present)` aborts before
`compose down` if either credential is empty, so a missing secret cannot replace a working stack.
After deploying, `Verify` calls `/gitea/health` and fails unless the token actually authenticates
and returns a username — being non-empty is not enough.

### Failure modes worth knowing

Neither credential fails loudly at runtime, which is why the workflow checks them explicitly.

1. **An empty `GITEA_TOKEN` breaks every Gitea tool silently.** `config.py` defaults it to `""`,
   and `is_gitea_configured()` only gates the code paths that call `require_gitea_config()`.
   The tools return errors that read like upstream problems rather than a missing credential.
   `${GITEA_TOKEN:?}` turns this into a compose failure instead.
2. **An empty `OWUI_TOKEN` stops the notes sync silently.** The scheduler keeps ticking every
   300 s and does nothing visible. Nothing crashes and nothing is logged as a startup failure.
3. **A valid-but-wrong token is worse than a missing one.** A token belonging to a different
   account deploys cleanly and operates with *that* account's permissions. `Preflight (GITEA_TOKEN
   actually authenticates)` calls `/api/v1/user` and prints the resolved login before deploying.
4. **A bad token is only discoverable by trying it.** Nothing in a chat transcript distinguishes
   "Gitea rejected the request" from "the tool is buggy", so the workflow's post-deploy step reads
   `GITEA_TOKEN` back out of the running container and compares it to the secret.
5. **Give the companion its own account.** The token carries whatever scope its owner has. The
   value committed here historically belonged to an **admin** user; prefer a dedicated machine
   account with only the repo scopes the tools need.

### If you ever commit a credential

Deleting it in a later commit does **not** retract it: the value stays in git history and in every
clone. The token must be **revoked at the issuer** — that, not the commit, is what makes it
useless, and no history rewrite substitutes for it once the repository has been shared.

This project once shipped with `.env` tracked. Recovery was: rotate both tokens in the Gitea and
Open-WebUI UIs, then untrack the file. Treat any credential that was ever committed as public.

## Features

### Evidence Management
- **EvidenceAdd**: Store verified facts, sources, or findings
- **EvidenceList**: Retrieve stored evidence with optional tag filtering
- **EvidenceGet**: Get a specific evidence item by ID
- **EvidenceUpdate**: Update an existing evidence item
- **EvidenceDelete**: Delete an evidence item
- **EvidenceClear**: Clear all evidence (use with caution)

### Gitea Integration
- **GiteaCreateRepo**: Create a new repository
- **GiteaListRepos**: List your repositories
- **GiteaGetRepo**: Get repository details
- **GiteaListBranches**: List a repository's branches (name, commit SHA, protection, default branch)
- **GiteaCreateBranch**: Create a branch from a branch, tag or commit (idempotent)
- **GiteaDeleteBranch**: Delete a branch, refusing the default branch and open-PR heads
- **GiteaMergePR**: Merge a PR with structured refusals (`blocked_reason`)
- **GiteaCreateFile**: Create or update a file (whole content)
- **GiteaApplyFilePatch**: Apply a unified diff to one or more files in one atomic commit
- **GiteaEditFile**: Edit files by exact text replacement (builds the diff for you)
- **GiteaGetFile**: Get file contents
- **GiteaListFiles**: List files in a directory
- **GiteaCreateIssue**: Create an issue
- **GiteaListIssues**: List issues
- **GiteaUpdateIssue**: Update an issue's title, description, and/or state
- **GiteaGetIssue**: Get a single issue by number
- **GiteaCloseIssue**: Close an issue, optionally posting a closing comment
- **GiteaReopenIssue**: Reopen a closed issue
- **GiteaCreatePR**: Create a pull request (optionally as a draft)
- **GiteaListPRs**: List pull requests
- **GiteaUpdatePR**: Update a pull request's title, description, state, and/or draft state
- **GiteaClosePR**: Close a pull request without merging it
- **GiteaPostComment**: Post a comment on an issue or PR
- **GiteaListComments**: List comments on an issue or PR
- **GiteaDeleteComment**: Delete a comment
- **GiteaGetPRDiff**: Get the full unified diff for a PR
- **GiteaGetPRFiles**: Get changed files in a PR
- **GiteaListPRComments**: List PR review comments
- **GiteaSubmitPRReview**: Submit a PR review
- **GiteaGetPRSummary**: Get a PR summary
- **GiteaGetPRPipeline**: Get CI status for a PR, attributed by head commit (`unknown` if no run can be found)
- **GiteaGetPipelineOutput**: Get pipeline output/logs
- **GiteaGetToken**: Get the configured Gitea API token
- **GiteaHealth**: Check Gitea connection


### Partial file edits

`GiteaCreateFile` sends the **whole** file. For a one-line change in a long
file, use one of the two partial-edit tools instead. Both map to Gitea's
`POST /repos/{owner}/{repo}/diffpatch`, and both refuse rather than allow a
write that is not what was asked.

| Tool | Use it when |
|---|---|
| `GiteaEditFile` | You want to change some text. Takes `files[].edits[].old_text` / `new_text`, or the flat single-file form, and builds the diff itself. |
| `GiteaApplyFilePatch` | You already have a diff (`GiteaGetPRDiff`, `git diff`, a patch file). Supports multi-file and multi-hunk. |

Both commit **every listed file in one commit**, so a half-applied change is not
possible. `GiteaApplyFilePatch` is atomic across files too.

```json
POST /gitea/edits/LLM/demo
{
  "files": [
    {"path": "app/__init__.py",
     "edits": [{"old_text": "1.5.2", "new_text": "1.5.3"}]}
  ],
  "message": "bump version"
}
```

**For a single file there is a shorter, flat form** -- `path` and `edits` at the
top level. It produces exactly the same commit:

```json
POST /gitea/edits/LLM/demo
{"path": "app/__init__.py",
 "edits": [{"old_text": "1.5.2", "new_text": "1.5.3"}],
 "message": "bump version"}
```

It exists because a three-level nested argument (`files` -> `edits` ->
`old_text`/`new_text`) is measurably harder for a model to emit correctly. Over
63 recorded calls, 15 omitted `path` from a `files[]` object and **every one**
failed with `Field required: path`; the omission was the model, not the
transport. If a caller hits that, the flat form removes the level it was
getting wrong. Send **one form or the other** -- sending both is refused as
`ambiguous_request`, because silently picking one could edit the wrong file.

`old_text` must match **exactly once**. Zero matches is `old_text_not_found`;
more than one is `old_text_ambiguous` -- refusing rather than guessing which
occurrence was meant, because a wrong pick is invisible in a success response.

Two upstream behaviours are refused rather than passed through. Both were
verified against live Gitea 28.1.0 and both are **silent wrong success**:

- **`new_branch` naming the base branch.** Gitea answers `201` and commits to
  that branch, so "my change on a new branch" can land on `main`. Refused unless
  `allow_base_branch_commit=true`.
- **A `No newline at end of file` marker in a raw diff.** A misplaced marker is
  accepted with `201` and corrupts the file (`a\\nb\\nc` becomes `a\\nBc`).
  `GiteaEditFile` never emits one; `GiteaApplyFilePatch` refuses one.

Every upstream patch failure is a bare `500` carrying raw `git apply` text, so
the reason is classified instead: `not_a_patch`, `context_mismatch`,
`file_not_found`, `path_rejected`, `malformed_patch`, `empty_diff`,
`no_eol_marker`, `base_branch_echo`, `new_branch_exists`, plus the edit reasons
above. Each refusal carries a `recovery` hint.

Maintainer notes on the diff builder: a generated diff must be newline-terminated
(Gitea answers an unterminated tail with `corrupt patch`), and hunks need
surrounding context lines -- a hunk with none, or context on only one side of an
insertion, is rejected as `patch does not apply`.

### Open-WebUI Notes Sync
- **NotesSyncTrigger**: Manually trigger a sync of Open-WebUI notes into a Knowledge Base
- **NotesSyncStatus**: Get the current status of the notes sync
- **NotesSyncHealth**: Check if Open-WebUI is configured and accessible
- **NotesSyncDebug**: Test all known notes API endpoints and return raw responses

### Closing issues and pull requests

`GiteaCloseIssue` and `GiteaReopenIssue` are the intended way to change issue
state. Both are idempotent: closing an already-closed issue succeeds and does
not re-post the closing comment, and the response echoes the `closed_at`
Gitea actually stored.

A closing comment is posted **before** the state change, so if the comment
fails the issue is left open rather than closed without explanation.

`GiteaUpdateIssue` also accepts a `state` field (`open` / `closed`) for the same
purpose. Both update schemas reject unknown fields with `422` rather than
discarding them, so a typo can no longer produce a successful-looking no-op.

### Deleting comments

`GiteaDeleteComment` is **repository-scoped**, matching the upstream API it
wraps (`DELETE /repos/{owner}/{repo}/issues/comments/{comment_id}`). Gitea has no
issue dimension on that endpoint, so the `{index}` segment in the public route is
part of the URL shape only and is **ignored**: the comment is removed no matter
which issue or PR number you pass.

Pass the correct number for readability, but do not rely on a wrong one being
rejected. The ignored value is echoed back so it is visible in the response:

```json
{"status": "deleted", "comment_id": 124, "index_ignored": 2}
```

A successful delete returns `200` with the body above. An unknown `comment_id`
returns `404`; a delete that Gitea refuses returns that upstream status. Success
is never reported for a request that did not actually delete anything.

### Draft (work in progress) pull requests

Gitea has **no `draft` field** on `CreatePullRequestOption` or
`EditPullRequestOption`. A `draft` property in a create payload is accepted and
silently discarded, so the call returns `201` on a normal, mergeable PR and
nothing in the response reveals the difference.

Gitea instead derives draft-ness from the **title**, so `draft` is translated
into a prefix:

| You send | Title Gitea receives | Stored `draft` |
|---|---|---|
| `draft: true`, title `Add x` | `WIP: Add x` | `true` |
| `draft: false`, title `WIP: Add x` | `Add x` | `false` |

`GiteaCreatePR(draft=true)` and `GiteaUpdatePR(draft=...)` therefore **change the
PR title**. Both are idempotent — a `WIP:` prefix is never doubled — and
`GiteaUpdatePR(draft=false)` removes it, so the flag is reversible. Omitting
`draft` from an update leaves draft state untouched (`null` is not `false`).

Responses report **what Gitea stored**, not what was asked for:

```json
{"number": 10, "title": "WIP: Add x", "draft": true,
 "draft_requested": true, "draft_applied_via": "title_prefix"}
```

If a future Gitea stops honouring the prefix, `draft` comes back `false` while
`draft_requested` is `true` — the drop stays visible instead of silently
reverting to the old bug.

A WIP PR is refused a merge with `405 Work in progress PRs cannot be merged`, so
the prefix is a real guard, not a label. Prefix matching is case-insensitive and
tolerates leading whitespace; the recognised forms are `WIP:` and `[WIP]`. Note
`WIP ` without a colon, `(WIP)` and `Draft:` are **not** treated as drafts by
Gitea, so the tool always writes the canonical `WIP: ` form.

### Branches and merging

`GiteaCreateBranch` creates a branch from a branch, tag or commit. It is idempotent:
re-requesting the same branch at the same ref returns `already_existed: true` rather than
an error. If the branch exists at a *different* ref it returns `409` naming both SHAs --
a silent success there would hide that the caller is about to write to a branch that is
not what they expect.

There is now a **`GiteaDeleteBranch`**, and it is the only irreversible operation in this
toolset: a deleted ref cannot be restored through the API. It therefore refuses two cases
*before* sending any DELETE, so a refusal never destroys anything:

- **The default branch.** Gitea refuses it with `403` anyway; checking first turns an opaque
  upstream status into a clear reason.
- **A branch that is the head of an open pull request.** Verified against live Gitea 28.1.0:
  Gitea answers **`204`** and deletes the branch, leaving the PR open against a branch that no
  longer exists. That PR can then be neither merged nor usefully closed -- it is wedged -- and
  nothing in the delete response revealed it. Refused outright: merge or close the PR first.

A branch that does not exist returns `deleted: true` with `already_absent: true` rather than an
error, so a retried delete is safe. The response includes the SHA the branch pointed at, so the
commits it referenced can still be located. If the open-PR listing cannot be read, the delete
proceeds and the response carries a warning saying the guard did not run -- rather than either
blocking a legitimate delete or silently skipping the check.

Branch names may contain `/` (for example `feat/thing`) and are passed to Gitea unencoded;
`/branches/feat%2Ftwo` answers `400`, because the path is decoded before routing.

`GiteaMergePR` merges, but decides *before* calling Gitea, because Gitea's own
refusals are misleading: a PR that exists but was closed unmerged answers
`404 not found`, which reads as "wrong PR number". The tool instead returns a
`blocked_reason`:

| `blocked_reason` | Meaning |
|---|---|
| `closed_unmerged` | Closed but never merged. Reopen it, then merge. |
| `is_draft` | Still a draft. Mark it ready (`GiteaUpdatePR` with `draft=false`). |
| `conflicts` | Cannot be merged cleanly. Resolve the conflicts. |
| `not_found` | No such PR in this repository. |

No write happens on a refusal. Re-merging an already-merged PR is treated as
idempotent success (`merged: true, already_merged: true`, nothing written) rather
than an error, because a retry is not a mistake.

**Mergeability is computed asynchronously.** A just-opened PR reports
`mergeable: false` until Gitea has checked it, so the tool re-reads a few times
before reporting a conflict. A genuine conflict stays `false` throughout.

**Merge comments are not supported.** `MergePullRequestOption` has no comment
field; post one separately with `GiteaPostComment`. Unlike `GiteaCloseIssue`
the comment cannot be ordered safely around the merge, so it is two calls.

`do` defaults to the repository's own `default_merge_style` rather than a
hardcoded `merge`, so per-repo policy is honoured. `manually-merged` is rejected:
it records a merge performed by hand without merging anything. `force_merge`
defaults to `false` -- it is the only way to bypass required checks.

> **If the repository deploys on push to the base branch, merging starts a
deployment.** That is true of this project: `main` is production.

### Calling these tools from Open-WebUI

Open-WebUI's OpenAPI tool layer flattens a route's **path and body parameters
into one flat JSON object**. The route's own path params (`owner`, `repo`,
`index`) therefore arrive in the request *body* as well, alongside the real
fields.

Endpoints whose body schema used `extra="forbid"` rejected exactly that shape
with `422 Extra inputs are not permitted` and never reached Gitea — the tool
could not be called from Open-WebUI at all, even though `curl` worked. Those
body models now tolerate the route's own parameters and ignore them, while
still rejecting anything else, so a typo (`state_closed`) remains a hard error
rather than a silent no-op.

`GiteaPostComment` only requires `body`. Its `issue_index` field is accepted for
backwards compatibility but **ignored** — the issue comes from the URL path. A
value that *disagrees* with the path is rejected with `422` instead of being
silently discarded, because posting to the wrong issue is not detectable from
the response.

### Health
- **HealthCheck**: Health check endpoint for monitoring
- **Root**: Root endpoint with API documentation links

## Compatibility notes

Several endpoints were written against the API *shape* rather than observed behaviour. They are
fixed now, and documented here because each failed in a way that looked like something else:

- **`GiteaGetPRDiff`** called `/pulls/{index}/diff`, which is not a route. Gitea serves the diff
  at `/pulls/{index}.diff` — a file-extension suffix, not a sub-path — so every call 404'd. The
  `style` argument was also inert: Gitea returns byte-identical output for `unified`, `separate`,
  `full` and nonsense alike, so it was removed rather than fixed.
- **`GiteaSubmitPRReview`** sent only `type` with lowercase values. Gitea >= 1.24 and Forgejo read
  `event` with uppercase values, so the review was created as **PENDING and never posted** — a
  success-shaped call that submitted nothing. Both fields are now sent.
- **`GiteaListPRComments`** used the flat `/pulls/{n}/comments`, removed in Gitea >= 1.24. It now
  walks reviews and collects each review's comments.
- **`GiteaGetPipelineOutput`** read `workflow_jobs` only. Gitea 29 returns `{jobs, total_count}`,
  so the job list came back empty and every log fallback was skipped.
- **`PipelineJob`** declared `started_at: str` and `url: str` but passed raw `None` through, which
  is a validation error on any job Gitea reports without those fields.
- **`GiteaCreatePR`** sent `draft`, which Gitea has never had on the create/edit schemas: it was
  discarded and the tool returned success on a non-draft PR. `draft` is now applied as a `WIP:`
  title prefix, the mechanism Gitea actually honours (see
  [Draft pull requests](#draft-work-in-progress-pull-requests)).

Two further mismatches, both of which reported a **confident wrong answer** rather than an error:

- **`GiteaGetPRPipeline` looked runs up by branch.** A `pull_request` workflow run in Gitea
  carries `head_branch: null` -- the field does not exist for that event -- so the filter matched
  nothing and the tool answered `run_id: 0 / status: pending` on PRs that were in fact green. A
  merge gate built on that reads "no checks" as "safe to merge". Runs are now attributed by
  `head_sha` (falling back to the merge commit), which is the only reliable join key, and a run
  that cannot be attributed is reported as `unknown` rather than `pending`.
- **`GiteaCreateFile` fell back to `branch = "main"`** when the repository read failed or the
  payload had no `default_branch`. A repo whose default branch is not `main` then got a write
  aimed at a branch that may not exist, and Gitea 404 `branch does not exist` on the write read
  as a bad branch name. It now resolves the default branch and fails loudly (`502`) instead.
- **Deleting an open pull request's head branch** is accepted by Gitea with `204`: the branch
  goes, the PR stays `open`, and it can then be neither merged nor closed. `GiteaDeleteBranch`
  refuses it before sending anything (see [Branches and merging](#branches-and-merging)).

The suite covers these with a fake, but a fake cannot prove a route exists — it matches on
substrings, so it would happily accept the broken URL. The routes above were additionally
verified against a live Gitea.

## Quick Start

### 1. Clone and Configure

```bash
# Clone the repository
git clone <your-repo-url>
cd open-webui-companion

# For LOCAL DEVELOPMENT ONLY - .env is gitignored and is not read by the
# deployed container. Never commit a credential.
cp .env.example .env
# Edit .env with your Gitea credentials
```

If you are deploying via Gitea Actions, do **not** edit `.env` at all — set
`GITEA_TOKEN` and `OWUI_TOKEN` as repository (or organisation) secrets instead. See
[Secrets](#-secrets).

Site-specific values — instance URLs, a private CA bundle, your Knowledge Base id — belong in
`docker-compose.override.yml`, not in the compose file. When deploying via Actions, the workflow
renders that file from repository **variables** instead (`MY_GITEA_INSTANCE_URL`,
`OWUI_INSTANCE_URL`, `NOTES_SYNC_KB_ID`, `NOTES_SYNC_INTERVAL`, `CA_BUNDLE_PATH`):

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
# edit it, then:
docker compose up -d
```

Compose merges that file automatically and it is gitignored, so your addresses and CA paths never
enter the repository.

### 2. Run with Docker (Recommended)

```bash
docker compose up -d
```

Compose reads `GITEA_TOKEN` and `OWUI_TOKEN` from the environment of the shell that
invokes it, so export them first:

```bash
export GITEA_TOKEN=...
export OWUI_TOKEN=...
docker compose up -d
```

Without them, `up` fails immediately on the `${VAR:?}` guard and nothing is
recreated.

The server will be available at `http://localhost:8090`.

### 3. Run Locally (Development)

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8090 --reload
```

## Open-WebUI Integration

### Add the Tool to Open-WebUI

1. Go to **Admin Settings → Tools** in Open-WebUI
2. Click **Add Tool** and select **OpenAPI**
3. Enter the URL: `http://your-server-ip:8090/openapi.json`
4. Save

The tools will now be available to any model that supports function calling.

### System Prompt

Add this to your model's system prompt for best results:

```
You have access to evidence management, Gitea, and Open-WebUI notes sync tools.

Evidence Management:
- Before answering factual questions, check EvidenceList for stored evidence.
- When the user provides facts, sources, or findings they want remembered, use EvidenceAdd to store them with relevant tags.
- Evidence persists across conversations to reduce hallucinations.

Gitea Integration:
- Use Gitea tools to create repositories, edit files, manage issues, and review pull requests.
- Always confirm with the user before making changes on Gitea.
- Use GiteaCreateFile to scaffold new projects, or GiteaEditFile for one-line changes to an existing file.
- Check pipeline status with GiteaGetPRPipeline after creating or updating PRs.

Open-WebUI Notes Sync:
- Use NotesSyncTrigger to sync user notes into a Knowledge Base.
- Check NotesSyncStatus to see if a sync is in progress.
```

## API Documentation

Once running, visit:
- **Swagger UI**: `http://localhost:8090/docs`
- **ReDoc**: `http://localhost:8090/redoc`
- **OpenAPI JSON**: `http://localhost:8090/openapi.json`

## Environment Variables

| Variable | Description | Default | Source |
|----------|-------------|---------|--------|
| `GITEA_INSTANCE_URL` | Your Gitea instance URL | *(required)* | override |
| `GITEA_TOKEN` | Personal access token (**secret**) | `""` | Gitea secret |
| `OWUI_INSTANCE_URL` | Open-WebUI URL | *(required)* | override |
| `OWUI_TOKEN` | Open-WebUI API key (**secret**) | `""` | Gitea secret |
| `NOTES_SYNC_KB_ID` | Knowledge Base to sync notes into; empty disables the sync | `""` | override |
| `NOTES_SYNC_INTERVAL` | Notes sync interval, seconds (0 disables) | `300` | compose |
| `HOST` | Server host | `0.0.0.0` | compose |
| `PORT` | Server port | `8090` | compose |
| `LOG_LEVEL` | Logging level | `info` | compose |
| `EVIDENCE_DIR` | Directory for evidence storage | `/data` | compose |
| `GITEA_EDIT_MAX_BYTES` | Largest file `GiteaEditFile` will rewrite in memory | `1000000` | env |
| `NOTES_SYNC_UPLOAD_TIMEOUT` | Seconds allowed for one note upload (uploads embed synchronously) | `600` | env |
| `NOTES_SYNC_FETCH_CONCURRENCY` | Parallel note fetches during a sync | `4` | env |
| `NOTES_SYNC_PRUNE` | Delete KB files whose note no longer exists (scoped to `notes/`) | `true` | env |
| `LOG_STRUCTURED` | JSON logs instead of plain text | `true` | env |

`docker-compose.yml` sets the defaults. The instance URLs are **required** and normally come
from `docker-compose.override.yml` (local) or the deploy workflow (Actions). `.env` is
development-only and is never read by the container.

## Data Persistence

Evidence data is stored in a JSON file (`evidence.json`) in the `/data` directory. When using Docker, this is mounted as a volume for persistence.

## Security Notes

- **CORS**: The server allows all origins by default. Restrict this in production.
- **Gitea token**: stored **only** as a Gitea secret, injected at deploy time. Never
  commit it; `.env` is gitignored for local development.
- **Token scope**: use a dedicated account with the minimum repo scopes needed. Every
  Gitea tool operates with whatever the token can reach, including destructive calls
  (`GiteaCreateFile`, `GiteaClosePR`, `GiteaDeleteComment`, …).
- **No authentication on the companion itself.** Anyone who can reach port `8090` can
  call every tool, including the destructive ones, using the server's own token.
  Keep it on a trusted network or put an authenticating proxy in front of it.
- **`GiteaGetToken` exists and returns the configured token.** It is disabled by
  default (`EXPOSE_GITEA_TOKEN=false`) and returns `403` unless explicitly enabled.
  Leave it disabled.

## Development

### Project Structure

```
.
├── app/
│   ├── __init__.py      # __version__ lives here — the single source of truth
│   ├── main.py          # FastAPI application
│   ├── config.py        # Configuration management
│   ├── errors.py        # Error handling
│   ├── logging.py       # Logging configuration
│   ├── evidence.py      # Evidence endpoints
│   ├── gitea.py         # Gitea endpoints
│   ├── notes_sync.py    # Open-WebUI notes sync endpoints
│   └── scheduler.py      # Background task scheduler
├── tests/
│   ├── fake_gitea.py            # In-process Gitea stand-in (no network, no creds)
│   ├── test_port_api_fixes.py   # regression tests for the API compatibility fixes
│   ├── test_gitea_issue_state.py    # close/reopen, and the silent-no-op guard
│   ├── test_gitea_delete_comment.py # 204/empty-body handling
│   ├── test_gitea_body_echo.py      # Open-WebUI's flat argument shape
│   ├── test_gitea_draft.py          # draft -> WIP: title translation
│   ├── test_gitea_list_branches.py  # bounded branch listing
│   ├── test_gitea_create_branch.py  # idempotency + same-ref/different-ref
│   ├── test_gitea_merge_pr.py       # refusal matrix + check ordering
│   ├── test_gitea_apply_patch.py    # raw diffpath guards + error taxonomy
│   ├── test_gitea_edit_file.py      # text-edit -> generated diff
│   ├── test_gitea_delete_branch.py  # default-branch + open-PR guards
│   └── test_gitea_create_file_branch.py  # no silent "main" fallback
├── Dockerfile
├── docker-compose.yml           # generic; site-specific values live in the override
├── docker-compose.override.yml.example
├── requirements.txt
├── .env.example         # template for local development
├── .gitattributes       # export-ignore rules for the public snapshot
├── .gitignore           # excludes .env, keys, caches, runtime data
└── README.md
```

### CI and deployment

**No CI configuration is published in this repository.** The maintained branch deploys through
Gitea Actions, which does not translate to GitHub, so those workflow files are deliberately
excluded -- shipping a pipeline that cannot run here would be worse than shipping none.

The same applies to the publish pipeline that produces this repository: its workflow, its export
rules and its internal notes all live in the private tree and are excluded from what you are
reading. Nothing in them is needed to *use* the companion, and their absence is asserted on every
publish rather than assumed.

The deploy pipeline's shape is still worth knowing, because it explains guarantees claimed elsewhere in
this document:

- credentials are checked **before** the stack is torn down, so a missing secret aborts the
  deploy rather than replacing a working service with a broken one;
- the test suite runs in the same job, so a red suite blocks the deploy;
- a post-deploy step asserts the running service reports the expected version and that its Gitea
  token actually authenticates -- being non-empty is not treated as sufficient.

To use this yourself, wire up whatever CI you prefer around two self-contained commands:

```bash
python3 -m unittest discover -s tests -t .
docker compose up -d
```

### Running the tests

Stdlib `unittest` only — no `pytest`, no `respx`, no network. `tests/fake_gitea.py`
replaces `httpx.AsyncClient`, so the app runs in-process and every outbound Gitea call
is recorded and asserted on.

```bash
python3 -m unittest discover -s tests -t . -v
```

Run it locally before committing; there is no CI here to catch a regression for you.

When adding a test, prefer asserting on *what was sent upstream* over re-reading the
response: several bugs in this project lived in the gap between the two.

### Adding New Tools

1. Create a new module in `app/` (e.g., `app/weather.py`)
2. Define your endpoints with proper OpenAPI documentation
3. Include the router in `app/main.py`:
   ```python
   from app.weather import router as weather_router
   app.include_router(weather_router)
   ```

## License

MIT -- see [LICENSE](LICENSE).

Upstream states MIT in its README but ships no license file, so the full text is included
here: a README line is a statement of intent, not a grant, and a downstream user should not
have to guess at the terms.

**No warranty.** This is AI-authored software (see [AI-authored](#ai-authored)); it is
provided as-is, and you are responsible for reviewing it before you rely on it -- particularly
the tools that write to your repositories.
