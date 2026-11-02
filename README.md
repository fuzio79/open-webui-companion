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

**Current version: 1.4.0.** The running service reports it at `/health`, and `app/__init__.py`
is its single source of truth -- the two are asserted equal at release time.

## 🔑 Secrets

**No credential is stored in this repository.** `GITEA_TOKEN` and `OWUI_TOKEN` come from
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
- **GiteaMergePR**: Merge a PR with structured refusals (`blocked_reason`)
- **GiteaCreateFile**: Create or update a file
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
- **GiteaGetPRPipeline**: Get pipeline status for a PR
- **GiteaGetPipelineOutput**: Get pipeline output/logs
- **GiteaGetToken**: Get the configured Gitea API token
- **GiteaHealth**: Check Gitea connection

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

`GiteaCreateBranch` creates a branch from a branch, tag or commit. It is
idempotent: re-requesting the same branch at the same ref returns
`already_existed: true` rather than an error. If the branch exists at a
*different* ref it returns `409` naming both SHAs -- a silent success there would
hide that the caller is about to write to a branch that is not what they expect.

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
- Use GiteaCreateFile to scaffold new projects or update existing code.
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
│   └── test_gitea_merge_pr.py       # refusal matrix + check ordering
├── Dockerfile
├── docker-compose.yml           # generic; site-specific values live in the override
├── docker-compose.override.yml.example
├── requirements.txt
├── .env.example         # template for local development
├── .gitignore           # excludes .env, keys, caches, runtime data
└── README.md
```

### CI and deployment

**No CI configuration is published in this repository.** The maintained branch deploys through
Gitea Actions, which does not translate to GitHub, so those workflow files are deliberately
excluded -- shipping a pipeline that cannot run here would be worse than shipping none.

The pipeline's shape is still worth knowing, because it explains guarantees claimed elsewhere in
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
