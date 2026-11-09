"""Gitea integration endpoints for Open-WebUI.

Provides tools for repository management, file operations, issue tracking,
pull requests, and commenting via the Gitea REST API.
"""

import asyncio
import difflib
import os
import base64
import urllib.parse
import fnmatch
import re
from enum import Enum
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.errors import GiteaError
from app.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/gitea", tags=["gitea"])

# Maps the companion's review type names to the Gitea/Forgejo API values.
# Current Gitea (>= 1.24) / Forgejo accept `event` with uppercase values;
# Gitea <= 1.23 accepted `type` with lowercase values.
_REVIEW_EVENT_MAP = {
    "approved": ("APPROVED", "approve"),
    "changes_requested": ("REQUEST_CHANGES", "request_changes"),
    "comment": ("COMMENT", "comment"),
}

# Gitea has no `draft` field on CreatePullRequestOption or EditPullRequestOption,
# so a `draft` property in a create/update payload is accepted and then silently
# discarded -- the call returns 201 on a normal, mergeable PR. Gitea decides
# draft-ness from the TITLE instead, matching case-insensitively and tolerating
# leading whitespace (all verified against live Gitea 28.1.0):
#
#   "WIP: x"  "WIP:"  "[WIP] x"  "wip: x"  "[wip] x"  "  WIP: x"   -> draft
#   "WIP x"   "(WIP) x"  "Draft: x"                                  -> not a draft
#
# A WIP PR is refused a merge with 405 "Work in progress PRs cannot be merged",
# so the prefix is a real guard rather than cosmetic. Setting it is the only way
# to make `draft` mean anything; clearing it is the only way to un-draft.
_WIP_PREFIX = "WIP: "
_WIP_PATTERNS = (re.compile(r"^\s*wip\s*:", re.IGNORECASE),
                 re.compile(r"^\s*\[wip\]", re.IGNORECASE))


# Gitea computes PR mergeability asynchronously, so a just-opened PR reports
# mergeable:false until the check completes. Re-read a few times before treating
# false as a real conflict. Kept small: this is a not-yet-computed value, not a
# queue, and a genuine conflict stays false through every re-check.
# How many recent pull_request runs to scan when attributing a run to a PR.
# Runs carry no branch for PR events, so attribution is by SHA and needs a
# window large enough to contain the PR head run even after later pushes.
_PIPELINE_RUN_LIMIT = 50

_DIFF_CONTEXT_LINES = 3

_MERGEABLE_RECHECKS = 3
_MERGEABLE_RECHECK_DELAY = float(os.environ.get("MERGEABLE_RECHECK_DELAY", "1.5"))


def _strip_wip_prefix(title: str) -> str:
    """Remove a leading WIP marker, so it is never doubled up."""
    for pattern in _WIP_PATTERNS:
        if pattern.match(title):
            # Drop the matched marker and any separator whitespace after it.
            return "".join(title[pattern.match(title).end():]).lstrip()
    return title


def _has_wip_prefix(title: str) -> bool:
    return any(pattern.match(title) for pattern in _WIP_PATTERNS)


def _apply_draft(title: str, draft: bool) -> str:
    """Translate the abstract `draft` flag into the title Gitea actually honours.

    `draft=true` -> ensure a WIP prefix. `draft=false` -> ensure there is none,
    so the flag is reversible in both directions through the same tool.
    """
    base = _strip_wip_prefix(title)
    return f"{_WIP_PREFIX}{base}" if draft else base


class RepoCreate(BaseModel):
    """Schema for creating a new repository."""

    name: str = Field(..., description="Repository name")
    description: str = Field(default="", description="Repository description")
    private: bool = Field(default=True, description="Whether the repo is private")
    auto_init: bool = Field(default=True, description="Initialize with README")


class FileCreate(BaseModel):
    """Schema for creating or updating a file."""

    owner: str = Field(..., description="Repository owner (username or org)")
    repo: str = Field(..., description="Repository name")
    path: str = Field(..., description="File path within the repo")
    content: str = Field(..., description="File content")
    message: str = Field(
        default="Add file via companion server", description="Commit message"
    )
    branch: str = Field(default="", description="Branch name (uses default if empty)")


class IssueCreate(BaseModel):
    """Schema for creating an issue."""

    owner: str = Field(..., description="Repository owner")
    repo: str = Field(..., description="Repository name")
    title: str = Field(..., description="Issue title")
    body: str = Field(default="", description="Issue description")
    labels: list[str] = Field(default_factory=list, description="Label names")


class PRCreate(BaseModel):
    """Schema for creating a pull request."""

    owner: str = Field(..., description="Repository owner")
    repo: str = Field(..., description="Repository name")
    title: str = Field(..., description="PR title")
    body: str = Field(default="", description="PR description")
    head: str = Field(..., description="Source branch name")
    base: str = Field(..., description="Target branch name")
    draft: bool = Field(
        default=False,
        description=("Open as a draft (work in progress) PR. Gitea has no draft field, "
                     "so this is applied as a 'WIP: ' title prefix -- the PR is then "
                     "refused a merge until the prefix is removed. The response reports "
                     "the stored title and draft flag."),
    )


IssueState = Literal["open", "closed"]


class RouteEchoModel(BaseModel):
    """Body model that tolerates the URL's own path parameters being echoed in.

    Open-WebUI's OpenAPI tool layer flattens path and body parameters into a
    single flat JSON object, so `owner`, `repo` and `index` from the route path
    arrive in the request body as well. The route has already bound those
    values, so anything echoed here is ignored -- but it must not be *rejected*,
    or the tool cannot be called at all (that was the bug: these models used
    `extra="forbid"`, so the arguments the tool itself advertised produced
    `422 Extra inputs are not permitted` and never reached Gitea).

    `extra="forbid"` is still load-bearing and is kept: it exists so a typo
    (`state_closed`) cannot silently become a no-op PATCH. The line
    drawn here is between *the route's own parameters* (tolerated) and
    *anything else* (rejected).
    """

    model_config = {"extra": "forbid"}

    owner: str | None = Field(
        default=None,
        exclude=True,
        description="Ignored: taken from the URL path",
    )
    repo: str | None = Field(
        default=None,
        exclude=True,
        description="Ignored: taken from the URL path",
    )
    index: int | None = Field(
        default=None,
        exclude=True,
        description="Ignored: taken from the URL path",
    )


class IssueUpdate(RouteEchoModel):
    """Schema for updating an issue.

    Inherits `extra="forbid"` from RouteEchoModel, which is load-bearing: with
    Pydantic's default "ignore", a caller sending an unsupported field (e.g.
    state_closed="closed") would have it silently discarded, yielding an empty
    PATCH that succeeds while changing nothing. See GiteaCloseIssue for the
    explicit state-change tool.
    """

    title: str | None = Field(default=None, description="New issue title")
    body: str | None = Field(default=None, description="New issue description")
    state: IssueState | None = Field(
        default=None,
        description="Set to 'closed' to close the issue, 'open' to reopen it",
    )


class PRUpdate(RouteEchoModel):
    """Schema for updating a pull request."""

    title: str | None = Field(default=None, description="New PR title")
    body: str | None = Field(default=None, description="New PR description")
    state: IssueState | None = Field(
        default=None,
        description="Set to 'closed' to close the PR, 'open' to reopen it",
    )
    draft: bool | None = Field(
        default=None,
        description=(
            "Set draft (work in progress) state. Applied as a 'WIP: ' title prefix, "
            "because Gitea has no draft field. `false` strips the prefix, so the "
            "flag is reversible. Omit to leave the draft state untouched."
        ),
    )


class IssueStateChange(RouteEchoModel):
    """Schema for closing an issue, optionally with a closing comment."""

    comment: str = Field(
        default="",
        description="Optional comment posted before the issue is closed",
    )


class IssueStateResponse(BaseModel):
    """Compact confirmation of an issue state change."""

    index: int = Field(..., description="Issue number")
    state: IssueState = Field(..., description="Resulting state reported by Gitea")
    title: str = Field(default="", description="Issue title")
    closed_at: str | None = Field(
        default=None, description="Close timestamp, or null if the issue is open"
    )
    html_url: str = Field(default="", description="URL to view the issue")
    comment_posted: bool = Field(
        default=False, description="Whether a closing comment was posted"
    )
    already_in_state: bool = Field(
        default=False, description="True if the issue was already in the requested state"
    )


class PRStateResponse(BaseModel):
    """Compact confirmation of a pull request state change."""

    index: int = Field(..., description="PR number")
    state: IssueState = Field(..., description="Resulting state reported by Gitea")
    title: str = Field(default="", description="PR title")
    merged: bool = Field(default=False, description="Whether the PR is merged")
    html_url: str = Field(default="", description="URL to view the PR")
    already_in_state: bool = Field(
        default=False, description="True if the PR was already in the requested state"
    )


class BranchCreate(RouteEchoModel):
    """Schema for creating a branch.

    `owner`/`repo` come from the URL path; any echo of them in the body is
    ignored rather than rejected (Open-WebUI flattens path and body params into
    one object — see RouteEchoModel).

    `old_ref_name` accepts a branch, tag or commit SHA. Left empty, Gitea
    branches from the repository's default branch.
    """

    new_branch_name: str = Field(..., description="Name of the branch to create")
    old_ref_name: str = Field(
        default="",
        description=(
            "Branch, tag or commit SHA to create from. Empty uses the repository's "
            "default branch."
        ),
    )


class BranchInfo(BaseModel):
    """One branch, as reported by Gitea."""

    name: str = Field(..., description="Branch name")
    sha: str = Field(default="", description="Commit SHA the branch points at")
    protected: bool = Field(
        default=False,
        description="Whether an effective branch protection rule applies",
    )


class BranchListResponse(BaseModel):
    """Bounded branch listing.

    `default_branch` is fetched alongside so a caller can tell which branch is
    protected-by-convention without a second round trip.
    """

    total: int = Field(..., description="Number of branches returned")
    branches: list[BranchInfo] = Field(
        default_factory=list, description="Branches in this page"
    )
    default_branch: str = Field(
        default="", description="The repository's default branch, if resolvable"
    )


class MergeBlocked(str, Enum):
    """Why a merge was refused, in terms the caller can act on.

    Gitea's own refusals are actively misleading: a PR that exists but was
    closed without merging answers `404 not found`, which reads as "wrong PR
    number" and sends a caller looking for a different index. These reasons are
    derived from a single GET before any write is attempted, so a wrong reason
    can never damage anything.
    """

    already_merged = "already_merged"
    closed_unmerged = "closed_unmerged"
    is_draft = "is_draft"
    conflicts = "conflicts"
    not_found = "not_found"


class MergeRequest(RouteEchoModel):
    """Schema for merging a pull request.

    Every field is optional: the merge style defaults to the repository's own
    `default_merge_style`, so an empty body is a valid, honest request. That
    empty-body case is the shape most likely to be rejected by an over-eager
    validator, so it is covered by a test.
    """

    do: str | None = Field(
        default=None,
        description=(
            "Merge style: merge, rebase, rebase-merge, squash, fast-forward-only. "
            "Omit to use the repository's default_merge_style. "
            "manually-merged is rejected: it records a merge without performing one."
        ),
    )
    force_merge: bool = Field(
        default=False,
        description=(
            "DANGEROUS: bypass failing required status checks. Defaults to false."
        ),
    )
    delete_branch_after_merge: bool = Field(
        default=False, description="Delete the head branch once merged"
    )


class BranchResponse(BaseModel):
    """Compact confirmation of a branch operation."""

    branch: str = Field(..., description="Branch name")
    created: bool = Field(..., description="True if this call created the branch")
    already_existed: bool = Field(
        default=False, description="True if the branch already pointed at the requested ref"
    )
    sha: str = Field(default="", description="Commit SHA the branch points at")
    ref: str = Field(default="", description="Ref the branch was created from")
    protected: bool = Field(
        default=False, description="Whether a branch protection rule applies"
    )
    html_url: str = Field(default="", description="URL to view the branch")


class BranchDeleteRequest(RouteEchoModel):
    """Schema for deleting a branch.

    `branch` comes from the URL path; an echo of it in the body is ignored
    rather than rejected (Open-WebUI flattens path and body parameters into a
    single object -- see RouteEchoModel).

    `confirm_default_branch` exists because the default branch cannot be deleted
    anyway: Gitea answers `403`. Requiring an explicit flag means the refusal is
    reported as the caller's own mistake, before any request is made, instead of
    as an opaque upstream 403 -- and, on the open-PR path below, it is the only
    way to opt out of a guard that prevents wedging a pull request.
    """

    branch: str | None = Field(
        default=None, exclude=True, description="Ignored: taken from the URL path"
    )
    confirm_default_branch: bool = Field(
        default=False,
        description=(
            "DANGEROUS: attempt to delete the default branch or an open PR's head "
            "branch. Gitea refuses the former with 403 regardless; this flag only "
            "changes the error returned. Leave it false."
        ),
    )


class BranchDeleteResponse(BaseModel):
    """Outcome of a branch delete, reported from Gitea's stored state."""

    branch: str = Field(..., description="Branch name")
    deleted: bool = Field(..., description="True if the branch no longer exists")
    already_absent: bool = Field(
        default=False,
        description=(
            "True if the branch was already gone. Reported as success: the "
            "caller's intent (the branch is not there) holds, and nothing was "
            "destroyed."
        ),
    )
    sha: str = Field(
        default="",
        description=(
            "SHA the branch pointed at before deletion, so the commits it "
            "referenced can still be located"
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal findings, e.g. a guard that could not be applied",
    )


class MergeResponse(BaseModel):
    """Result of a merge attempt, reported from Gitea's stored state."""

    index: int = Field(..., description="PR number")
    merged: bool = Field(..., description="Whether Gitea reports the PR as merged")
    merge_commit_sha: str = Field(default="", description="Merge commit SHA")
    merged_at: str | None = Field(default=None, description="Merge timestamp")
    base: str = Field(default="", description="Target branch")
    head: str = Field(default="", description="Source branch")
    do: str = Field(default="", description="Merge style used")
    already_merged: bool = Field(
        default=False, description="True if it was already merged before this call"
    )
    branch_deleted: bool = Field(default=False, description="Whether the head branch was deleted")
    html_url: str = Field(default="", description="URL to view the PR")
    blocked_reason: str | None = Field(
        default=None, description="Why the merge was refused; null on success"
    )


class FileListResponse(BaseModel):
    """Schema for a single file/directory entry in a listing."""

    name: str = Field(..., description="Entry name")
    path: str = Field(..., description="Full path within the repo")
    type: str = Field(..., description="Entry type: file or dir")
    size: int = Field(default=0, description="Size in bytes (files only)")
    download_url: str = Field(
        default="", description="Direct download URL (files only)"
    )


class CommentCreate(RouteEchoModel):
    """Schema for creating a comment on an issue or PR.

    Only `body` is required. `owner`/`repo`/`index` come from the URL path.

    `issue_index` is accepted purely for backwards compatibility and is ignored:
    the comment always goes to the issue in the path. It previously *competed*
    with the path, so a caller setting `issue_index` to issue 5 while the URL
    pointed at issue 2 got the comment on issue 2 with a success response. A
    mismatched value is now rejected rather than silently redirected, because
    commenting on the wrong issue is not something the caller can detect from
    the reply.
    """

    body: str = Field(..., description="Comment text")
    issue_index: int | None = Field(
        default=None,
        exclude=True,
        description="Deprecated and ignored: the issue is taken from the URL path",
    )


class PRFileChange(BaseModel):
    """Schema for a single file changed in a PR."""

    filename: str = Field(..., description="File path")
    status: str = Field(
        ..., description="Change status: added, removed, modified, renamed"
    )
    additions: int = Field(default=0, description="Lines added")
    deletions: int = Field(default=0, description="Lines deleted")
    changes: int = Field(default=0, description="Total line changes")
    binary: bool = Field(default=False, description="Is a binary file")
    patch: str = Field(default="", description="Unified diff patch")


class ReviewCreate(RouteEchoModel):
    """Schema for submitting a PR review.

    `owner`/`repo`/`index` come from the URL path; any echo of them in the body
    is ignored rather than rejected.
    """

    body: str = Field(default="", description="Review comment text")
    type: str = Field(
        ..., description="Review type: approved, changes_requested, comment"
    )
    commit_id: str = Field(
        default="", description="Commit ID to review (uses latest if empty)"
    )


class PRSummaryResponse(BaseModel):
    """Schema for PR summary response."""

    pr_number: int = Field(..., description="PR number")
    title: str = Field(..., description="PR title")
    state: str = Field(default="", description="PR state: open, closed, merged")
    body: str = Field(default="", description="PR description/body")
    files_changed: int = Field(default=0, description="Number of files changed")
    total_additions: int = Field(default=0, description="Total lines added")
    total_deletions: int = Field(default=0, description="Total lines deleted")
    changed_files: list[str] = Field(
        default_factory=list, description="List of changed file paths"
    )
    base_branch: str = Field(default="", description="Target base branch")
    head_branch: str = Field(default="", description="Source head branch")
    summary: str = Field(default="", description="Auto-generated summary of the PR")


class PatchBlocked(str, Enum):
    """Why a patch or file edit was refused, in terms the caller can act on.

    Gitea answers *every* failed patch with a bare ``500`` and a raw
    ``git apply error: ...`` string -- five distinct causes behind one status
    (verified against live 28.1.0). The reason is therefore derived here
    rather than passed through.

    A refusal decided before the POST costs nothing: no write has happened, so
    a wrong reason can never damage the repository. That is why several of
    these are preflight checks rather than parses of an upstream error.
    """

    empty_diff = "empty_diff"
    not_a_patch = "not_a_patch"
    no_eol_marker = "no_eol_marker"
    context_mismatch = "context_mismatch"
    file_not_found = "file_not_found"
    path_rejected = "path_rejected"
    malformed_patch = "malformed_patch"
    base_branch_echo = "base_branch_echo"
    new_branch_exists = "new_branch_exists"
    empty_old_text = "empty_old_text"
    old_text_not_found = "old_text_not_found"
    old_text_ambiguous = "old_text_ambiguous"
    no_change = "no_change"
    binary_file = "binary_file"
    file_too_large = "file_too_large"
    ambiguous_request = "ambiguous_request"


class FilePatchRequest(RouteEchoModel):
    """Schema for applying a unified diff to one or more files.

    `owner`/`repo` come from the URL path; an echo of them in the body is
    tolerated and discarded (Open-WebUI flattens path and body parameters into
    a single object -- see RouteEchoModel).

    `allow_base_branch_commit` exists because of a verified upstream trap:
    Gitea treats ``new_branch`` equal to the base branch as "commit to that
    branch" and answers ``201``. A caller that asked for a new branch did not
    ask to write to ``main``, so that case is refused unless this flag says
    the caller really means it.
    """

    content: str = Field(..., description="Unified diff to apply")
    message: str = Field(
        default="Apply patch via companion server", description="Commit message"
    )
    branch: str = Field(
        default="", description="Base branch (empty uses the repository default)"
    )
    new_branch: str = Field(
        default="",
        description=(
            "Create this branch and commit there instead of writing to the base "
            "branch. Must not name the base branch (see allow_base_branch_commit)."
        ),
    )
    allow_base_branch_commit: bool = Field(
        default=False,
        description=(
            "DANGEROUS: allow new_branch to name the base branch. Without this, "
            "Gitea would silently commit to that branch and report success."
        ),
    )


class TextEdit(BaseModel):
    """One literal find-and-replace, applied to the file's current content.

    Literal, not a regex: no escaping traps, and no way for a stray bracket to
    match something the caller did not intend.
    """

    model_config = {"extra": "forbid"}

    old_text: str = Field(
        ..., description="Exact text to find. Must occur exactly once in the file."
    )
    # NO default. A default makes this field OPTIONAL in the OpenAPI schema
    # that Open-WebUI turns into the model-facing tool schema, and an omitted
    # new_text then means "" -- i.e. DELETE old_text. The model was being told
    # "you may omit this" on the one field whose omission is unrecoverable.
    # Verified before the fix: {"old_text": "x"} with no new_text returned 200
    # and deleted the text. Deleting stays supported, but only as an EXPLICIT
    # empty string, never as an omission.
    new_text: str = Field(
        ...,
        description=(
            "Replacement text. An empty string DELETES old_text; omitting "
            "this field is refused."
        ),
    )


class FileEditSpec(BaseModel):
    """Edits to apply to a single file."""

    model_config = {"extra": "forbid"}

    path: str = Field(..., description="File path within the repo")
    edits: list[TextEdit] = Field(
        ..., description="Find/replace operations, applied in order"
    )


class FileEditRequest(RouteEchoModel):
    """Schema for editing files by literal text replacement.

    Every file in `files` lands in ONE commit: the edits are turned into a
    single diff and applied at once, so a half-applied change is not possible.

    SINGLE-FILE FLAT FORM. `path` + `edits` may be given at the TOP LEVEL
    instead of inside `files`. Both are accepted because they produce the same
    edit; the flat one just removes a level of nesting.

    Why it exists (measured, not assumed): across 63 persisted GiteaEditFile
    calls, 15 omitted `path` from a `files[]` object and every one of them
    failed with `Field required: path`, while 44 that carried it succeeded.
    The omission was the MODEL not emitting a key, not the transport losing
    it: adjacent retries differed by exactly the 21 characters of
    `"path": "STATUS.md", `, and a Pydantic error cannot encode field order,
    so "put path first" could never have been the mechanism. The OpenAPI
    schema already says `path` is required, so telling the model again is not
    a fix. A shallower argument shape is: models are markedly more reliable
    on a flat object than on files -> edits -> old_text/new_text.

    Exactly one form per call. Sending both is refused rather than resolved by
    precedence, because guessing which one the caller meant is how an edit
    lands on the wrong file and still reports success.
    """

    files: list[FileEditSpec] | None = Field(
        default=None,
        description=(
            "Files to edit; all changes land in one commit. Omit this and use "
            "path+edits for a single file."
        ),
    )
    path: str = Field(
        default="",
        description=(
            "Single-file form: file path within the repo. Use INSTEAD of "
            "`files`, not alongside it."
        ),
    )
    edits: list[TextEdit] | None = Field(
        default=None,
        description=(
            "Single-file form: find/replace operations, applied in order. Use "
            "INSTEAD of `files`, not alongside it."
        ),
    )
    message: str = Field(
        default="Edit file(s) via companion server", description="Commit message"
    )
    branch: str = Field(
        default="", description="Base branch (empty uses the repository default)"
    )
    new_branch: str = Field(
        default="",
        description="Create this branch and commit there instead of the base branch",
    )
    allow_base_branch_commit: bool = Field(
        default=False,
        description=(
            "DANGEROUS: allow new_branch to name the base branch, which commits "
            "to that branch directly."
        ),
    )


class PatchApplyResponse(BaseModel):
    """What Gitea actually stored after a patch was applied.

    Every field is read back from Gitea's response (or derived from the diff it
    accepted); nothing here echoes the request back.
    """

    commit_sha: str = Field(..., description="SHA of the commit Gitea created")
    branch: str = Field(..., description="Branch the commit landed on")
    new_branch: str = Field(
        default="", description="Branch created for this commit, if any"
    )
    files_changed: list[str] = Field(
        default_factory=list, description="Paths the diff touched"
    )
    lines_added: int = Field(default=0, description="Lines added by the diff")
    lines_removed: int = Field(default=0, description="Lines removed by the diff")
    html_url: str = Field(default="", description="URL to view the commit")


class FileEditFileResult(BaseModel):
    """Per-file outcome of a text edit."""

    path: str = Field(..., description="File path")
    edits_applied: int = Field(..., description="Number of replacements applied")
    lines_added: int = Field(default=0, description="Lines added in this file")
    lines_removed: int = Field(default=0, description="Lines removed in this file")


class FileEditResponse(PatchApplyResponse):
    """Outcome of a text edit, with the per-file breakdown."""

    results: list[FileEditFileResult] = Field(
        default_factory=list, description="Per-file results"
    )


# ─── Pipeline / Actions models ──────────────────────────────────────────────


class PipelineStatusResponse(BaseModel):
    """Schema for pipeline status response."""

    run_id: int = Field(..., description="Workflow run ID")
    run_number: int = Field(
        default=0, description="Run number within the workflow (index_in_repo)"
    )
    status: str = Field(
        ...,
        description=("Run status: success, failure, running, waiting, cancelled, skipped, "
                     "or unknown when no run could be attributed to this PR. unknown is "
                     "NOT pending: pending invites polling, unknown says the run could "
                     "not be found."),
    )
    conclusion: str = Field(
        default="",
        description="Run conclusion: success, failure, neutral, cancelled, timed_out, or empty if still running",
    )
    name: str = Field(default="", description="Workflow name")
    display_title: str = Field(
        default="", description="Human-readable title for the run"
    )
    event: str = Field(
        default="", description="Trigger event: push, pull_request, schedule, etc."
    )
    branch: str = Field(default="", description="Branch the run was triggered on")
    commit_sha: str = Field(default="", description="Commit SHA")
    html_url: str = Field(default="", description="URL to view the run in the web UI")
    started_at: str = Field(
        default="", description="ISO 8601 timestamp when the run started"
    )
    updated_at: str = Field(default="", description="ISO 8601 timestamp of last update")
    matched_by: str = Field(
        default="",
        description=("Which commit the run was matched on: head_sha, "
                     "merge_commit_sha, or empty when nothing matched"),
    )
    path: str = Field(
        default="",
        description="Workflow file and ref, e.g. test.yml@refs/pull/17/head",
    )


class PipelineJob(BaseModel):
    """Schema for a single job in a pipeline run."""

    job_id: int = Field(..., description="Job/Task ID")
    name: str = Field(default="", description="Job name")
    status: str = Field(default="", description="Job status")
    conclusion: str = Field(default="", description="Job conclusion")
    started_at: str = Field(
        default="", description="ISO 8601 timestamp when the job started"
    )
    url: str = Field(default="", description="URL to view the job in the web UI")


class PipelineOutputResponse(BaseModel):
    """Schema for pipeline output/logs response."""

    run_id: int = Field(..., description="Workflow run ID")
    status: str = Field(..., description="Run status")
    conclusion: str = Field(..., description="Run conclusion")
    name: str = Field(default="", description="Workflow name")
    jobs: list[PipelineJob] = Field(
        default_factory=list, description="List of jobs in this run"
    )
    logs: str = Field(
        default="",
        description="Combined log output from all jobs, or note if logs are unavailable",
    )


async def _gitea_request(method: str, url: str, **kwargs) -> dict | list | str:
    """Make a request to the Gitea API with error handling.

    Returns the decoded JSON body. Empty bodies are not an error: Gitea answers
    a successful DELETE with ``204 No Content``, and calling ``.json()`` on that
    raises ``JSONDecodeError``, which is neither an ``HTTPStatusError`` nor a
    ``RequestError`` and so escaped the handlers below as a 500 *after* the
    mutation had already succeeded. Non-JSON bodies (e.g. the raw ``.diff``
    endpoint) are returned as text for the same reason.
    """
    settings.require_gitea_config()

    kwargs.setdefault("headers", settings.gitea_headers)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                # JSONDecodeError is a ValueError. Hand back the raw body so a
                # text/plain endpoint yields text instead of a 500.
                return response.text
    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500]
        logger.error(
            "Gitea API request failed",
            method=method,
            url=str(url),
            status_code=e.response.status_code,
            response=error_body,
        )
        raise GiteaError(
            f"Gitea API error {e.response.status_code}: {error_body}",
            status_code=e.response.status_code,
        ) from e
    except httpx.RequestError as e:
        logger.error(
            "Gitea API request error", method=method, url=str(url), error=str(e)
        )
        raise GiteaError(f"Failed to connect to Gitea: {e}") from e


async def _patch_issue(
    owner: str, repo: str, index: int, payload: dict, resource: str = "issues"
) -> dict:
    """PATCH an issue or PR, remapping Gitea's 412 to a clearer 422.

    Gitea answers an unrecognised ``state`` value with 412 Precondition Failed.
    That reads as a concurrency conflict to a caller, so surface it as 422
    Unprocessable Content, which is what it actually is: a bad field value.
    """
    try:
        return await _gitea_request(
            "PATCH",
            f"{settings.gitea_api_url}/repos/{owner}/{repo}/{resource}/{index}",
            json=payload,
        )
    except GiteaError as e:
        if e.status_code == 412:
            raise HTTPException(
                status_code=422,
                detail=f"Gitea rejected the update as invalid: {e}",
            ) from e
        raise


# ─── Repository endpoints ───────────────────────────────────────────────────


@router.post(
    "/repos", summary="Create a new repository", operation_id="GiteaCreateRepo"
)
async def create_repo(repo: RepoCreate) -> dict:
    """
    Create a new repository on Gitea.

    Creates a new repository with the specified name and settings.
    Optionally initializes it with a README file.
    """
    logger.info("Creating repository", name=repo.name, private=repo.private)

    result = await _gitea_request(
        "POST",
        f"{settings.gitea_api_url}/user/repos",
        json={
            "name": repo.name,
            "description": repo.description,
            "private": repo.private,
            "auto_init": repo.auto_init,
        },
    )

    logger.info("Repository created", name=repo.name, clone_url=result.get("clone_url"))
    return result


@router.get("/repos", summary="List repositories", operation_id="GiteaListRepos")
async def list_repos(
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=30, ge=1, le=100, description="Items per page"),
) -> list:
    """List repositories accessible to the authenticated user."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/user/repos",
        params={"page": page, "limit": limit},
    )


@router.get(
    "/repos/{owner}/{repo}",
    summary="Get repository details",
    operation_id="GiteaGetRepo",
)
async def get_repo(owner: str, repo: str) -> dict:
    """Get details of a specific repository."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}",
    )


# ─── Branch endpoints ───────────────────────────────────────────────────────


@router.get(
    "/branches/{owner}/{repo}",
    summary="List a repository's branches",
    operation_id="GiteaListBranches",
)
async def list_branches(
    owner: str,
    repo: str,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=100, description="Items per page"),
    q: str = Query(
        default="",
        description="Branch name substring to filter by (empty lists all)",
    ),
) -> BranchListResponse:
    """
    List a repository's branches.

    Returns a bounded shape rather than Gitea's raw payload: name, commit SHA and
    whether a protection rule applies. `default_branch` is included so callers do
    not have to call GetRepo separately -- and so it is obvious which branch the
    delete tool will refuse to remove without an explicit confirmation.

    No body model: this route has only path and query parameters, so there is
    nothing for Open-WebUI's flat-argument shape to collide with.
    """
    logger.info("Listing branches", owner=owner, repo=repo, page=page, limit=limit)

    params: dict = {"page": page, "limit": limit}
    if q:
        params["q"] = q

    raw = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/branches",
        params=params,
    )

    if not isinstance(raw, list):
        # A non-list means the response was not what this endpoint documents.
        # Surface it: returning zero branches would read as "no branches exist".
        raise HTTPException(
            status_code=502,
            detail=f"Expected a branch list from Gitea, got {type(raw).__name__}",
        )

    branches = [
        BranchInfo(
            name=b.get("name", ""),
            sha=((b.get("commit") or {}).get("id") or ""),
            protected=bool(
                b.get("protected") or b.get("effective_branch_protection_name")
            ),
        )
        for b in raw
        if isinstance(b, dict)
    ]

    # Best effort: a listing must not fail because the repo read did. Forgejo has
    # reported `default_branch` as both a string and an object, so accept either.
    default_branch = ""
    try:
        info = await _gitea_request(
            "GET", f"{settings.gitea_api_url}/repos/{owner}/{repo}"
        )
        if isinstance(info, dict):
            value = info.get("default_branch", "")
            default_branch = (
                value.get("name", "") if isinstance(value, dict) else str(value or "")
            )
    except GiteaError as e:
        logger.warning(
            "Branch list succeeded but default_branch lookup failed",
            owner=owner,
            repo=repo,
            status_code=e.status_code,
        )

    logger.info("Branches listed", owner=owner, repo=repo, count=len(branches))

    return BranchListResponse(
        total=len(branches), branches=branches, default_branch=default_branch
    )


@router.post(
    "/branches/{owner}/{repo}",
    summary="Create a branch",
    operation_id="GiteaCreateBranch",
)
async def create_branch(owner: str, repo: str, branch: BranchCreate) -> BranchResponse:
    """
    Create a branch, optionally from a specific branch, tag or commit.

    Idempotent: if the branch already exists **and points at the requested ref**,
    this returns `created: false, already_existed: true` rather than an error.
    If it exists at a *different* ref it returns `409` naming both SHAs -- a
    silent success there would hide that the caller is about to write to a
    branch that is not what they think it is (the `PATCH {}` class of bug).

    Gitea answers an existing branch with `409` and an unknown source ref with
    `404`; both are distinguished here rather than blanket-mapped.
    """
    logger.info(
        "Creating branch",
        owner=owner,
        repo=repo,
        new_branch=branch.new_branch_name,
        old_ref=branch.old_ref_name or "default",
    )

    base = f"{settings.gitea_api_url}/repos/{owner}/{repo}"

    # Resolve the source ref first, so a same-ref repeat call can be answered
    # honestly instead of being reported as a conflict.
    source = branch.old_ref_name
    if not source:
        info = await _gitea_request("GET", base)
        source = (info.get("default_branch", "") if isinstance(info, dict) else "") or "main"

    try:
        existing = await _gitea_request(
            "GET", f"{base}/branches/{branch.new_branch_name}"
        )
    except GiteaError as e:
        if e.status_code != 404:
            raise
        existing = None

    if isinstance(existing, dict):
        existing_sha = ((existing.get("commit") or {}).get("id") or "")
        source_sha = ""
        try:
            src = await _gitea_request("GET", f"{base}/branches/{source}")
            source_sha = ((src.get("commit") or {}).get("id") or "") if isinstance(src, dict) else ""
        except GiteaError:
            pass

        if source_sha and existing_sha and existing_sha == source_sha:
            logger.info(
                "Branch already exists at the requested ref",
                owner=owner,
                repo=repo,
                branch=branch.new_branch_name,
            )
            return BranchResponse(
                branch=branch.new_branch_name,
                created=False,
                already_existed=True,
                sha=existing_sha,
                ref=source,
                protected=bool(
                    existing.get("protected")
                    or existing.get("effective_branch_protection_name")
                ),
            )

        raise HTTPException(
            status_code=409,
            detail=(
                f"Branch '{branch.new_branch_name}' already exists at "
                f"{existing_sha[:12] or 'an unknown commit'}, but '{source}' is at "
                f"{source_sha[:12] or 'an unknown commit'}. Refusing to report success: "
                "the branch is not what was requested. Delete it first, or pick "
                "another name."
            ),
        )

    result = await _gitea_request(
        "POST",
        f"{base}/branches",
        json={"new_branch_name": branch.new_branch_name, "old_ref_name": source},
    )

    sha = ((result.get("commit") or {}).get("id") or "") if isinstance(result, dict) else ""
    logger.info(
        "Branch created",
        owner=owner,
        repo=repo,
        branch=branch.new_branch_name,
        sha=sha[:12],
    )

    return BranchResponse(
        branch=(result.get("name") if isinstance(result, dict) else "")
        or branch.new_branch_name,
        created=True,
        sha=sha,
        ref=source,
        protected=bool(
            isinstance(result, dict)
            and (result.get("protected") or result.get("effective_branch_protection_name"))
        ),
    )


@router.delete(
    "/branches/{owner}/{repo}/{branch:path}",
    summary="Delete a branch",
    operation_id="GiteaDeleteBranch",
)
async def delete_branch(
    owner: str,
    repo: str,
    branch: str,
    confirm_default_branch: bool = Query(
        default=False,
        description="DANGEROUS: skip the default-branch and open-PR guards.",
    ),
) -> BranchDeleteResponse:
    """
    Delete a branch. Refuses the two cases where the caller's intent is unclear.

    This is the only irreversible operation in the toolset: a deleted ref cannot
    be restored through the API. Gitea's own guards are incomplete, so two
    refusals happen here BEFORE any DELETE, and nothing is mutated on either:

    - **The default branch.** Gitea refuses it with `403`; checking first means
      the caller gets a clear reason rather than an opaque upstream status.
    - **A branch that is the head of an OPEN pull request.** Verified against
      live 28.1.0: Gitea answers **`204`** and deletes it, leaving the PR open
      against a branch that no longer exists. Closing or merging that PR then
      fails -- it is wedged -- and nothing in the delete response revealed it.
      That is silent-wrong-success, so it is refused outright: close or merge
      the PR first, which is what the caller actually wants.

    A branch that does not exist is reported as `deleted: true` with
    `already_absent: true` rather than as an error: the caller's intent (the
    branch is not there) holds and nothing was destroyed, so a retried delete is
    safe.

    `branch` is declared as `:path` so a name containing `/` (`feat/x`) arrives
    intact. Gitea answers `400` to the percent-encoded form, because the path is
    decoded before routing.
    """
    branch = urllib.parse.unquote(branch).strip("/")
    logger.info(
        "Deleting branch",
        owner=owner,
        repo=repo,
        branch=branch,
        confirm=confirm_default_branch,
    )

    if not branch:
        raise HTTPException(status_code=422, detail="No branch name was given.")

    base = f"{settings.gitea_api_url}/repos/{owner}/{repo}"

    info = await _gitea_request("GET", base)
    default_branch = ""
    if isinstance(info, dict):
        value = info.get("default_branch", "")
        default_branch = (
            value.get("name", "") if isinstance(value, dict) else str(value or "")
        )

    if branch == default_branch and not confirm_default_branch:
        raise HTTPException(
            status_code=409,
            detail={
                "blocked_reason": "default_branch",
                "detail": (
                    f"'{branch}' is the default branch of {owner}/{repo}. Gitea "
                    "refuses to delete it (403), so nothing was attempted."
                ),
                "recovery": (
                    "Delete a different branch. If you really want Gitea's own "
                    "refusal, set confirm_default_branch=true."
                ),
            },
        )

    sha = ""
    warnings: list[str] = []
    try:
        existing = await _gitea_request("GET", f"{base}/branches/{branch}")
        if isinstance(existing, dict):
            commit = existing.get("commit") or {}
            sha = commit.get("id") or "" if isinstance(commit, dict) else ""
    except GiteaError as e:
        if e.status_code != 404:
            raise
        logger.info("Branch already absent", owner=owner, repo=repo, branch=branch)
        return BranchDeleteResponse(
            branch=branch,
            deleted=True,
            already_absent=True,
            warnings=["The branch did not exist, so nothing was deleted."],
        )

    # An open PR whose head is this branch becomes unmergeable and unclosable
    # once the ref is gone. Refuse rather than wedge it.
    try:
        open_prs = await _gitea_request(
            "GET", f"{base}/pulls", params={"state": "open", "limit": 100}
        )
    except GiteaError as e:
        # A failed PR listing must not block a legitimate delete -- but the
        # guard did not run, and the caller has to know that.
        logger.warning(
            "Could not list open PRs before delete", status_code=e.status_code
        )
        open_prs = []
        warnings.append(
            "Could not list open pull requests, so the open-PR guard was not "
            "applied. Check for an open PR from this branch."
        )

    blocking: list[int] = []
    for pr in open_prs if isinstance(open_prs, list) else []:
        if not isinstance(pr, dict):
            continue
        head = pr.get("head") or {}
        name = head.get("ref") or head.get("label") or ""
        if name == branch or name.endswith("/" + branch):
            blocking.append(pr.get("number"))

    if blocking and not confirm_default_branch:
        raise HTTPException(
            status_code=409,
            detail={
                "blocked_reason": "open_pull_request",
                "detail": (
                    f"'{branch}' is the head of open pull request(s) "
                    f"{', '.join('#' + str(n) for n in blocking)}. Gitea would "
                    "delete the branch (204) and leave the PR open against a "
                    "branch that no longer exists, which cannot then be merged "
                    "or closed. Nothing was deleted."
                ),
                "pull_requests": blocking,
                "recovery": (
                    "Merge or close the pull request first (GiteaMergePR or "
                    "GiteaClosePR), then delete the branch."
                ),
            },
        )

    try:
        await _gitea_request("DELETE", f"{base}/branches/{branch}")
    except GiteaError as e:
        text = str(e).lower()
        if e.status_code == 404:
            return BranchDeleteResponse(
                branch=branch,
                deleted=True,
                already_absent=True,
                sha=sha,
                warnings=["The branch disappeared before the delete was applied."],
            )
        if e.status_code == 403 and "protected" in text:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Gitea refused to delete '{branch}': the branch is "
                    "protected. Remove the protection rule in Gitea first."
                ),
            ) from e
        if e.status_code == 403:
            raise HTTPException(
                status_code=409,
                detail=f"Gitea refused to delete '{branch}': {str(e)[:200]}",
            ) from e
        raise

    logger.info("Branch deleted", owner=owner, repo=repo, branch=branch, sha=sha[:12])
    return BranchDeleteResponse(branch=branch, deleted=True, sha=sha, warnings=warnings)


# ─── File endpoints ─────────────────────────────────────────────────────────


@router.post(
    "/files", summary="Create or update a file", operation_id="GiteaCreateFile"
)
async def create_file(file: FileCreate) -> dict:
    """
    Create or update a file in a repository.

    Creates a new file (POST) or updates an existing one (PUT with SHA).
    """
    logger.info(
        "Creating/updating file",
        owner=file.owner,
        repo=file.repo,
        path=file.path,
        branch=file.branch or "default",
    )

    # Resolve the branch, and fail loudly rather than guessing "main".
    #
    # This used to fall back to "main" in two ways -- a `.get(default_branch,
    # "main")` default and a bare `except GiteaError` -- so a failed repo read,
    # or a repo whose default branch is not called main, wrote to a branch that
    # may not exist. Gitea then answers 404 `branch does not exist`, which reads
    # as "bad branch" when the real fault is that the default was never
    # resolved. The new partial-edit tools already refuse the guess; this makes
    # GiteaCreateFile consistent with them.
    branch = await _resolve_working_branch(file.owner, file.repo, file.branch)
    logger.info(
        "Using branch",
        owner=file.owner,
        repo=file.repo,
        branch=branch,
        explicit=bool(file.branch),
    )

    # Check if file exists
    file_exists = False
    file_sha = None
    try:
        existing = await _gitea_request(
            "GET",
            f"{settings.gitea_api_url}/repos/{file.owner}/{file.repo}/contents/{file.path}",
            params={"ref": branch},
        )
        # Handle directory response (API returns a list for directories)
        if isinstance(existing, list):
            logger.info(
                "Path is a directory, treating file as new",
                owner=file.owner,
                repo=file.repo,
                path=file.path,
            )
            file_exists = False
        else:
            file_exists = True
            file_sha = existing["sha"]
    except GiteaError as e:
        if e.status_code != 404:
            raise

    encoded_content = base64.b64encode(file.content.encode()).decode()
    payload = {
        "content": encoded_content,
        "message": file.message,
        "branch": branch,
    }

    if file_exists:
        # Update existing file — PUT with SHA
        payload["sha"] = file_sha
        method = "PUT"
        logger.info(
            "Updating existing file",
            owner=file.owner,
            repo=file.repo,
            path=file.path,
            sha=file_sha[:12],
        )
    else:
        # Create new file — POST without SHA
        method = "POST"
        logger.info(
            "Creating new file",
            owner=file.owner,
            repo=file.repo,
            path=file.path,
        )

    result = await _gitea_request(
        method,
        f"{settings.gitea_api_url}/repos/{file.owner}/{file.repo}/contents/{file.path}",
        json=payload,
    )

    logger.info("File written", owner=file.owner, repo=file.repo, path=file.path)
    return result


@router.get(
    "/files/{owner}/{repo}/{path:path}/ls",
    summary="List files in directory",
    operation_id="GiteaListFiles",
)
async def list_files(
    owner: str,
    repo: str,
    path: str = "",
    branch: str = Query(default="", description="Branch name"),
    filter: str = Query(
        default="", description="Glob pattern to filter by (e.g. '*.py', 'README*')"
    ),
) -> list[FileListResponse]:
    """
    List files and directories at the given path.

    Supports an optional glob filter to narrow results.
    """
    params = {"ref": branch} if branch else {}

    entries = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/contents/{path}",
        params=params,
    )

    # Apply filter if provided
    if filter:
        entries = [e for e in entries if fnmatch.fnmatch(e.get("name", ""), filter)]

    return [
        FileListResponse(
            name=e.get("name") or "",
            path=e.get("path") or "",
            type=e.get("type") or "file",
            size=e.get("size") or 0,
            download_url=e.get("download_url") or "",
        )
        for e in entries
    ]


@router.get(
    "/files/{owner}/{repo}/{path:path}",
    summary="Get file contents",
    operation_id="GiteaGetFile",
)
async def get_file(
    owner: str,
    repo: str,
    path: str,
    branch: str = Query(default="", description="Branch name"),
) -> str:
    """
    Get the contents of a file from a repository.

    Returns the decoded file content as a string.
    If the path is a directory, returns a formatted listing of its contents.
    """
    params = {"ref": branch} if branch else {}

    response = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/contents/{path}",
        params=params,
    )

    # Handle directory listing (API returns a list for directories)
    if isinstance(response, list):
        logger.info(
            "Path is a directory, returning listing",
            owner=owner,
            repo=repo,
            path=path,
            entries=len(response),
        )
        lines = [f"Directory listing for `{path}` ({len(response)} entries):\n"]
        for entry in response:
            name = entry.get("name", "unknown")
            entry_type = "📁" if entry.get("type") == "dir" else "📄"
            lines.append(f"  {entry_type} {name}")
        return "\n".join(lines)

    # Decode the base64 content (single file)
    content = response.get("content", "")
    encoding = response.get("encoding", "base64")

    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8")
    return content


def _refuse_patch(
    reason: PatchBlocked, detail: str, status_code: int = 422, **extra
) -> None:
    """Refuse a patch/edit with a machine-readable reason.

    Called *before* any write in every case that matters, so a wrong reason
    cannot damage anything. `recovery` is included because the useful part of a
    refusal is what to do next, and these are consumed by a model.
    """
    logger.info("Patch/edit refused", reason=reason.value, detail=detail)
    raise HTTPException(
        status_code=status_code,
        detail={"blocked_reason": reason.value, "detail": detail, **extra},
    )


_RECOVERY = {
    PatchBlocked.empty_diff: "Send a non-empty diff, or use GiteaCreateFile to write the whole file.",
    PatchBlocked.not_a_patch: (
        "The body is not a unified diff. It needs at least one hunk header "
        "('@@ -a,b +c,d @@') and '---'/'+++' file headers. Prefer GiteaEditFile, "
        "which builds the diff for you."
    ),
    PatchBlocked.no_eol_marker: (
        "The diff contains a 'No newline at end of file' marker. Gitea accepts a "
        "misplaced marker with 201 and silently corrupts the file, so it is "
        "refused here. Use GiteaEditFile, which reconstructs the file instead of "
        "relying on the marker."
    ),
    PatchBlocked.context_mismatch: (
        "The diff's context does not match the file at this branch. Re-read the "
        "file (GiteaGetFile) and rebuild the diff against its current content, or "
        "use GiteaEditFile with exact old_text."
    ),
    PatchBlocked.file_not_found: (
        "A path in the diff does not exist on this branch. Check the path, or use "
        "the contents API to create a new file."
    ),
    PatchBlocked.path_rejected: (
        "Gitea rejected a path in the diff (absolute, or containing '..'). Use a "
        "repository-relative path."
    ),
    PatchBlocked.malformed_patch: (
        "Gitea could not parse the patch. Check the hunk headers and that every "
        "line is newline-terminated, then retry."
    ),
    PatchBlocked.base_branch_echo: (
        "new_branch names the base branch. Gitea would silently commit to that "
        "branch and report success. Pick a different new branch name, or set "
        "allow_base_branch_commit=true if committing to it is really intended."
    ),
    PatchBlocked.new_branch_exists: (
        "new_branch already exists, so nothing was written. Choose another name, "
        "or pass branch=<that branch> to commit onto it (force_push is ignored by "
        "Gitea, so it cannot overwrite)."
    ),
    PatchBlocked.empty_old_text: "old_text must not be empty.",
    PatchBlocked.old_text_not_found: (
        "old_text was not found in the file. Re-read it with GiteaGetFile and copy "
        "the exact text, including indentation."
    ),
    PatchBlocked.old_text_ambiguous: (
        "old_text matches more than once, so the target line is ambiguous. Include "
        "more surrounding lines to make it unique."
    ),
    PatchBlocked.no_change: (
        "This edit would not change the file, so no commit was made. Send an edit "
        "that actually differs."
    ),
    PatchBlocked.ambiguous_request: (
        "Send EITHER files=[{path, edits}] for one or more files, OR a top-level "
        "path+edits for exactly one file. Sending both is refused, because "
        "silently picking one of them could edit the wrong file."
    ),
    PatchBlocked.binary_file: (
        "The file is not valid UTF-8 text, so it cannot be edited as text. Use "
        "GiteaCreateFile with base64 content instead."
    ),
    PatchBlocked.file_too_large: (
        "The file exceeds the size this tool will rewrite in memory. Raise "
        "GITEA_EDIT_MAX_BYTES, or use GiteaCreateFile."
    ),
}

# Gitea's marker for 'the preceding line has no trailing newline'. Emitting it
# is refused rather than supported: a misplaced marker is accepted with 201 and
# silently corrupts the file (verified -- 'a\\nb\\nc' became 'a\\nBc').
_NO_EOL_MARKER = "No newline at end of file"


def _diff_stats(diff: str) -> tuple[list[str], int, int]:
    """Return (paths, lines_added, lines_removed) parsed from a unified diff.

    Used so the response can describe what Gitea accepted rather than echoing
    what the caller sent.
    """
    paths: list[str] = []
    added = removed = 0
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            halves = line[len("diff --git "):].split(" b/")
            if len(halves) == 2:
                new_path = halves[1].strip()
                if new_path and new_path not in paths:
                    paths.append(new_path)
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return paths, added, removed


def _map_apply_error(message: str) -> PatchBlocked:
    """Classify Gitea's raw `git apply` failure text into a reason.

    Every failed patch is a bare 500 (verified: five distinct causes), so the
    body is the only signal. Specific causes are tested before the generic
    'patch does not apply' fallback, or the useful distinctions would be lost.
    """
    text = message.lower()
    if "no valid patches in input" in text:
        return PatchBlocked.not_a_patch
    if "does not exist in index" in text:
        return PatchBlocked.file_not_found
    if "invalid path" in text:
        return PatchBlocked.path_rejected
    if "corrupt patch" in text:
        return PatchBlocked.malformed_patch
    if "patch failed" in text or "patch does not apply" in text:
        return PatchBlocked.context_mismatch
    return PatchBlocked.malformed_patch


def _diff_paths_violating_repo(diff: str) -> list[str]:
    """Return any path in the diff that escapes the repository root.

    Gitea rejects these, but reports them as `does not exist in index` as often
    as `invalid path`, so relying on the error text mislabels a traversal as a
    missing file. Checking first means the caller is told the real problem.
    """
    bad: list[str] = []
    for line in diff.split("\n"):
        candidate = ""
        if line.startswith("diff --git "):
            candidate = line[len("diff --git "):].split(" b/")[-1].strip()
        elif line.startswith(("+++ ", "--- ")):
            candidate = line[4:].strip()
        if not candidate or candidate == "/dev/null":
            continue
        if candidate[:2] in ("a/", "b/"):
            candidate = candidate[2:]
        if not candidate:
            continue
        if candidate.startswith("/") or ".." in candidate.split("/"):
            if candidate not in bad:
                bad.append(candidate)
    return bad


def _build_file_diff(path: str, old: str, new: str) -> str:
    """Build a unified diff for one file in the exact shape Gitea accepts.

    Two things here are load-bearing and were established by probing live
    Gitea 28.1.0, not by reading docs:

    1. Every emitted line is newline-terminated. `difflib` omits the terminator
       on its last line when the source file has no trailing newline, and the
       next line is then glued onto it ('-c+c\\n'), which Gitea answers with
       '500 corrupt patch'.
    2. No 'No newline at end of file' marker is emitted. The marker is the only
       way to express that state, but a misplaced one is accepted with 201 and
       silently corrupts the file. Terminating every line instead was verified
       to preserve a no-trailing-newline file byte-for-byte.
    """
    parts = [
        line if line.endswith("\n") else line + "\n"
        for line in difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=_DIFF_CONTEXT_LINES,
        )
    ]
    if not parts:
        return ""
    return f"diff --git a/{path} b/{path}\n" + "".join(parts)


def _apply_text_edits(content: str, edits: list[TextEdit], path: str) -> tuple[str, int]:
    """Apply literal replacements in order, refusing anything ambiguous.

    Each edit is searched in the content as modified by the previous edits, so a
    second edit can target text the first one introduced. `old_text` must match
    exactly once: guessing which occurrence was meant is how an edit lands on
    the wrong line and still reports success.
    """
    updated = content
    applied = 0

    for position, edit in enumerate(edits, start=1):
        if not edit.old_text:
            _refuse_patch(
                PatchBlocked.empty_old_text,
                f"{path}: edit #{position} has an empty old_text.",
                path=path,
                recovery=_RECOVERY[PatchBlocked.empty_old_text],
            )

        if edit.old_text == edit.new_text:
            _refuse_patch(
                PatchBlocked.no_change,
                f"{path}: edit #{position} has identical old_text and new_text.",
                path=path,
                recovery=_RECOVERY[PatchBlocked.no_change],
            )

        occurrences = updated.count(edit.old_text)
        if occurrences == 0:
            detail = f"{path}: edit #{position} old_text was not found."
            if "\r\n" in content and "\r\n" not in edit.old_text:
                detail += " The file uses CRLF line endings; old_text uses LF."
            _refuse_patch(
                PatchBlocked.old_text_not_found,
                detail,
                path=path,
                edit=position,
                recovery=_RECOVERY[PatchBlocked.old_text_not_found],
            )
        if occurrences > 1:
            _refuse_patch(
                PatchBlocked.old_text_ambiguous,
                f"{path}: edit #{position} old_text matches {occurrences} times.",
                path=path,
                edit=position,
                matches=occurrences,
                recovery=_RECOVERY[PatchBlocked.old_text_ambiguous],
            )

        updated = updated.replace(edit.old_text, edit.new_text, 1)
        applied += 1

    return updated, applied


def _resolve_edit_files(edit: "FileEditRequest") -> list[FileEditSpec]:
    """Return the files to edit, accepting the flat single-file form.

    `files` and top-level `path`+`edits` are alternatives, never a pair: both
    present means the caller's intent is ambiguous, and choosing one of them
    would be a guess about which file gets written. So it is refused.

    Called before any read or write, so a wrong answer here cannot damage the
    repository.
    """
    flat_given = bool(edit.path) or edit.edits is not None
    if edit.files and flat_given:
        _refuse_patch(
            PatchBlocked.ambiguous_request,
            (
                f"Both `files` ({len(edit.files)} entry/entries) and a top-level "
                "`path`/`edits` were sent; it is unclear which file to edit."
            ),
            recovery=_RECOVERY[PatchBlocked.ambiguous_request],
        )

    if flat_given:
        if not edit.path:
            _refuse_patch(
                PatchBlocked.empty_diff,
                "`edits` was given without `path`, so there is no file to edit.",
                recovery=(
                    "Add `path` naming the file, or use the `files` form with "
                    "{path, edits} objects."
                ),
            )
        if not edit.edits:
            _refuse_patch(
                PatchBlocked.empty_diff,
                f"`path` was given ('{edit.path}') but `edits` is empty or missing.",
                path=edit.path,
                recovery=(
                    "Add at least one {old_text, new_text} edit, or use the "
                    "`files` form."
                ),
            )
        return [FileEditSpec(path=edit.path, edits=edit.edits)]

    if not edit.files:
        _refuse_patch(
            PatchBlocked.empty_diff,
            "No files were listed.",
            recovery=_RECOVERY[PatchBlocked.empty_diff],
        )

    return edit.files


async def _resolve_working_branch(owner: str, repo: str, branch: str) -> str:
    """Resolve the base branch, and fail loudly rather than guessing 'main'.

    A silent fallback to 'main' can target a branch that does not exist, which
    then reads as a patch failure instead of a resolution failure.
    """
    if branch:
        return branch
    info = await _gitea_request("GET", f"{settings.gitea_api_url}/repos/{owner}/{repo}")
    resolved = (info.get("default_branch", "") if isinstance(info, dict) else "") or ""
    if not resolved:
        raise HTTPException(
            status_code=502,
            detail=f"Could not resolve the default branch of {owner}/{repo}.",
        )
    return resolved


async def _guard_branches(
    owner: str, repo: str, branch: str, new_branch: str, allow_base: bool
) -> None:
    """Refuse the two branch mistakes that produce a silent wrong write.

    ORDER IS LOAD-BEARING: the base-branch check must run BEFORE the
    exists-check, because the base branch always exists -- testing existence
    first would report a misleading 'new_branch exists' and never explain that
    the real problem is that the write would land on the base branch.
    """
    if not new_branch or allow_base:
        return
    if new_branch == branch:
        _refuse_patch(
            PatchBlocked.base_branch_echo,
            (
                f"new_branch '{new_branch}' is the base branch. Gitea treats this as "
                "a commit to that branch and answers 201, so the caller would get "
                "their change on the base branch instead of a new one."
            ),
            base_branch=branch,
            recovery=_RECOVERY[PatchBlocked.base_branch_echo],
        )
    base = f"{settings.gitea_api_url}/repos/{owner}/{repo}"
    # Keep "/" unencoded: Gitea answers 400 to /branches/feat%2Ftwo, because
    # the path is decoded before routing and the slash then reads as a
    # separator. A 400 there aborted every tool that used a feat/... new
    # branch, which is the usual naming convention.
    branch_ref = urllib.parse.quote(new_branch, safe="/")
    try:
        await _gitea_request("GET", f"{base}/branches/{branch_ref}")
    except GiteaError as e:
        if e.status_code == 404:
            return
        raise
    _refuse_patch(
        PatchBlocked.new_branch_exists,
        (
            f"new_branch '{new_branch}' already exists, so the patch was not "
            "attempted and no commit was made."
        ),
        status_code=409,
        new_branch=new_branch,
        recovery=_RECOVERY[PatchBlocked.new_branch_exists],
    )


async def _post_diffpatch(
    owner: str,
    repo: str,
    content: str,
    message: str,
    branch: str,
    new_branch: str,
) -> dict:
    """POST a diff to Gitea, translating its bare 500s into refusals."""
    payload: dict = {"content": content, "message": message}
    if branch:
        payload["branch"] = branch
    if new_branch:
        payload["new_branch"] = new_branch

    try:
        result = await _gitea_request(
            "POST",
            f"{settings.gitea_api_url}/repos/{owner}/{repo}/diffpatch",
            json=payload,
        )
    except GiteaError as e:
        text = str(e).lower()
        if e.status_code == 500:
            reason = _map_apply_error(str(e))
            _refuse_patch(
                reason,
                f"Gitea rejected the patch: {str(e)[:400]}",
                recovery=_RECOVERY[reason],
                upstream_status=500,
            )
        if e.status_code == 422 and "already exists" in text:
            _refuse_patch(
                PatchBlocked.new_branch_exists,
                f"new_branch already exists: {str(e)[:200]}",
                status_code=409,
                recovery=_RECOVERY[PatchBlocked.new_branch_exists],
            )
        if e.status_code == 404:
            _refuse_patch(
                PatchBlocked.context_mismatch,
                f"Gitea could not resolve the branch or repository: {str(e)[:200]}",
                status_code=404,
                recovery="Check the branch name with GiteaListBranches.",
            )
        raise

    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail=f"Expected a file response from Gitea, got {type(result).__name__}",
        )
    return result


def _apply_response(
    result: dict, diff: str, branch: str, new_branch: str
) -> PatchApplyResponse:
    """Describe what Gitea stored, read back from its response."""
    commit = result.get("commit") or {}
    commit = commit if isinstance(commit, dict) else {}
    sha = commit.get("sha") or ""
    if not sha:
        # A success status without a commit SHA means nothing was committed.
        # Reporting success here would be the exact failure shape this repo
        # keeps fixing: a green result for a write that did not happen.
        raise HTTPException(
            status_code=502,
            detail="Gitea reported success without a commit SHA; no commit was made.",
        )
    paths, added, removed = _diff_stats(diff)
    return PatchApplyResponse(
        commit_sha=sha,
        branch=new_branch or branch,
        new_branch=new_branch,
        files_changed=paths,
        lines_added=added,
        lines_removed=removed,
        html_url=commit.get("html_url") or "",
    )


@router.post(
    "/patches/{owner}/{repo}",
    summary="Apply a unified diff to one or more files",
    operation_id="GiteaApplyFilePatch",
)
async def apply_file_patch(
    owner: str, repo: str, patch: FilePatchRequest
) -> PatchApplyResponse:
    """
    Apply a unified diff, committing every touched file at once.

    This is the 1:1 mapping of Gitea's `POST /repos/{owner}/{repo}/diffpatch`.
    Use it when you already have a diff (from `GiteaGetPRDiff`, a `git diff`, or
    a patch file), and use `GiteaEditFile` when you only want to change a few
    lines -- that tool builds the diff and avoids the line-number and newline
    traps entirely.

    TWO UPSTREAM TRAPS ARE REFUSED RATHER THAN PASSED THROUGH, both verified
    against live Gitea 28.1.0 and both silent-wrong-success:

    - `new_branch` naming the base branch returns 201 and commits to that
      branch, so 'my change on a new branch' can land on `main`. Refused unless
      `allow_base_branch_commit=true`.
    - a diff containing a 'No newline at end of file' marker is accepted with
      201 and corrupts the file if the marker is misplaced. Refused; use
      `GiteaEditFile`, which reconstructs the file instead.

    Every patch failure upstream is a bare `500` with raw `git apply` text, so
    the failure is classified into a `blocked_reason` with a recovery step.
    Patches are atomic across files: either every hunk applies and one commit is
    made, or nothing is written.

    No mutation happens on any refusal.
    """
    logger.info(
        "Applying file patch",
        owner=owner,
        repo=repo,
        branch=patch.branch or "default",
        new_branch=patch.new_branch,
    )

    content = patch.content or ""
    if not content.strip():
        _refuse_patch(
            PatchBlocked.empty_diff,
            "The diff body is empty.",
            recovery=_RECOVERY[PatchBlocked.empty_diff],
        )

    if _NO_EOL_MARKER in content:
        _refuse_patch(
            PatchBlocked.no_eol_marker,
            (
                "The diff contains a 'No newline at end of file' marker. Gitea "
                "accepts a misplaced marker with 201 and silently corrupts the "
                "file, so it is refused here."
            ),
            recovery=_RECOVERY[PatchBlocked.no_eol_marker],
        )

    bad_paths = _diff_paths_violating_repo(content)
    if bad_paths:
        _refuse_patch(
            PatchBlocked.path_rejected,
            (
                "The diff refers to a path outside the repository root: "
                + ", ".join(repr(x) for x in bad_paths)
            ),
            paths=bad_paths,
            recovery=_RECOVERY[PatchBlocked.path_rejected],
        )

    if "@@" not in content:
        _refuse_patch(
            PatchBlocked.not_a_patch,
            "The body has no hunk header ('@@'), so it is not a unified diff.",
            recovery=_RECOVERY[PatchBlocked.not_a_patch],
        )

    # Normalise a missing final newline instead of rejecting it: a formatting
    # detail, not a semantic problem, and Gitea rejects some unterminated tails
    # as 'corrupt patch' (verified).
    diff = content if content.endswith("\n") else content + "\n"

    branch = await _resolve_working_branch(owner, repo, patch.branch)
    await _guard_branches(
        owner, repo, branch, patch.new_branch, patch.allow_base_branch_commit
    )

    result = await _post_diffpatch(
        owner, repo, diff, patch.message, branch, patch.new_branch
    )

    response = _apply_response(result, diff, branch, patch.new_branch)
    logger.info(
        "File patch applied",
        owner=owner,
        repo=repo,
        commit_sha=response.commit_sha[:12],
        files=len(response.files_changed),
    )
    return response


@router.post(
    "/edits/{owner}/{repo}",
    summary="Edit a file by exact text replacement",
    operation_id="GiteaEditFile",
)
async def edit_file(owner: str, repo: str, edit: FileEditRequest) -> FileEditResponse:
    """
    Edit one or more files by replacing exact text, in ONE commit.

    This is the tool to reach for instead of writing a diff by hand. It removes
    the three things that go wrong when a model writes a patch: line numbers,
    context lines, and end-of-file newline handling.

    HOW IT WORKS: each file is read at `branch`, the literal replacements are
    applied in order, and the result is turned into a single diff that is
    applied through Gitea's `diffpatch`. Because it is one diff, all listed
    files land in one atomic commit -- a half-applied change is not possible.

    RULES (each refused, never guessed):

    - `old_text` must match EXACTLY ONCE per file. Zero matches and multiple
      matches are both refused: picking an occurrence is how an edit silently
      lands on the wrong line.
    - Search is literal, never a regex, so there are no escaping traps.
    - A later edit sees the content produced by the earlier ones.
    - An edit that changes nothing is refused rather than committed as a no-op.
    - Binary (non-UTF-8) files are refused; use `GiteaCreateFile` for those.

    The response reports the commit SHA read back from Gitea, so success means
    the repository says so.
    """
    files_to_edit = _resolve_edit_files(edit)
    logger.info(
        "Editing file(s)",
        owner=owner,
        repo=repo,
        files=len(files_to_edit),
        branch=edit.branch or "default",
        new_branch=edit.new_branch,
    )

    branch = await _resolve_working_branch(owner, repo, edit.branch)
    await _guard_branches(
        owner, repo, branch, edit.new_branch, edit.allow_base_branch_commit
    )

    base = f"{settings.gitea_api_url}/repos/{owner}/{repo}"
    max_bytes = settings.GITEA_EDIT_MAX_BYTES
    diffs: list[str] = []
    results: list[FileEditFileResult] = []
    seen: set[str] = set()

    for spec in files_to_edit:
        path = (spec.path or "").lstrip("/")
        if not path:
            _refuse_patch(
                PatchBlocked.empty_diff,
                "A file entry has an empty path.",
                recovery="Provide a repository-relative path.",
            )
        if path in seen:
            _refuse_patch(
                PatchBlocked.no_change,
                f"{path} is listed twice; its edits would be ambiguous.",
                path=path,
                recovery="Combine the edits into a single entry for that path.",
            )
        seen.add(path)

        quoted = urllib.parse.quote(path, safe="/")
        try:
            current = await _gitea_request(
                "GET", f"{base}/contents/{quoted}", params={"ref": branch}
            )
        except GiteaError as e:
            if e.status_code == 404:
                _refuse_patch(
                    PatchBlocked.file_not_found,
                    f"{path} does not exist on branch '{branch}'.",
                    path=path,
                    recovery=(
                        "Check the path with GiteaListFiles, or use GiteaCreateFile "
                        "to create a new file."
                    ),
                )
            raise

        if isinstance(current, list):
            _refuse_patch(
                PatchBlocked.file_not_found,
                f"{path} is a directory, not a file.",
                path=path,
                recovery="List it with GiteaListFiles and pick a file.",
            )

        size = int(current.get("size") or 0)
        if size > max_bytes:
            _refuse_patch(
                PatchBlocked.file_too_large,
                f"{path} is {size} bytes, over the {max_bytes}-byte limit.",
                path=path,
                size=size,
                recovery=_RECOVERY[PatchBlocked.file_too_large],
            )

        try:
            raw = base64.b64decode(current.get("content") or "").decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            _refuse_patch(
                PatchBlocked.binary_file,
                f"{path} is not valid UTF-8 text.",
                path=path,
                recovery=_RECOVERY[PatchBlocked.binary_file],
            )

        updated, applied = _apply_text_edits(raw, spec.edits, path)
        if updated == raw:
            _refuse_patch(
                PatchBlocked.no_change,
                f"{path} would be unchanged; no commit was made.",
                path=path,
                recovery=_RECOVERY[PatchBlocked.no_change],
            )

        file_diff = _build_file_diff(path, raw, updated)
        if not file_diff:
            _refuse_patch(
                PatchBlocked.no_change,
                f"{path} produced an empty diff; no commit was made.",
                path=path,
                recovery=_RECOVERY[PatchBlocked.no_change],
            )

        _, added, removed = _diff_stats(file_diff)
        diffs.append(file_diff)
        results.append(
            FileEditFileResult(
                path=path,
                edits_applied=applied,
                lines_added=added,
                lines_removed=removed,
            )
        )

    combined = "".join(diffs)
    if not combined.strip():
        _refuse_patch(
            PatchBlocked.no_change,
            "The edits produced no changes; no commit was made.",
            recovery=_RECOVERY[PatchBlocked.no_change],
        )

    result = await _post_diffpatch(
        owner, repo, combined, edit.message, branch, edit.new_branch
    )

    applied_response = _apply_response(result, combined, branch, edit.new_branch)
    logger.info(
        "File(s) edited",
        owner=owner,
        repo=repo,
        commit_sha=applied_response.commit_sha[:12],
        files=len(results),
    )
    return FileEditResponse(**applied_response.model_dump(), results=results)


# ─── Issue endpoints ────────────────────────────────────────────────────────


@router.post("/issues", summary="Create an issue", operation_id="GiteaCreateIssue")
async def create_issue(issue: IssueCreate) -> dict:
    """Create a new issue in a repository."""
    logger.info("Creating issue", owner=issue.owner, repo=issue.repo, title=issue.title)

    result = await _gitea_request(
        "POST",
        f"{settings.gitea_api_url}/repos/{issue.owner}/{issue.repo}/issues",
        json={
            "title": issue.title,
            "body": issue.body,
            "labels": issue.labels,
        },
    )

    logger.info(
        "Issue created", owner=issue.owner, repo=issue.repo, index=result.get("number")
    )
    return result


@router.get(
    "/issues/{owner}/{repo}", summary="List issues", operation_id="GiteaListIssues"
)
async def list_issues(
    owner: str,
    repo: str,
    state: str = Query(default="open", description="Issue state: open, closed, or all"),
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=30, ge=1, le=100, description="Items per page"),
) -> list:
    """List issues in a repository."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues",
        params={"state": state, "page": page, "limit": limit},
    )


@router.patch(
    "/issues/{owner}/{repo}/{index}",
    summary="Update an issue",
    operation_id="GiteaUpdateIssue",
)
async def update_issue(owner: str, repo: str, index: int, update: IssueUpdate) -> dict:
    """
    Update an issue's title and/or description.

    Only the fields provided in the request body will be updated.
    Omitted fields remain unchanged.
    """
    logger.info("Updating issue", owner=owner, repo=repo, index=index)

    payload = update.model_dump(include={"title", "body", "state"}, exclude_none=True)
    if not payload:
        # Sending PATCH {} would be a no-op round-trip whose result depends on
        # Gitea's tolerance. Fail loudly instead of silently changing nothing.
        raise HTTPException(
            status_code=422,
            detail="No updatable fields supplied. Provide at least one of: title, body, state.",
        )

    result = await _patch_issue(owner, repo, index, payload)

    logger.info("Issue updated", owner=owner, repo=repo, index=index)
    return result


@router.get(
    "/issues/{owner}/{repo}/{index}",
    summary="Get a single issue",
    operation_id="GiteaGetIssue",
)
async def get_issue(owner: str, repo: str, index: int) -> dict:
    """Get one issue by its index.

    Lets a caller verify the outcome of a state change independently of the
    mutation response.
    """
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues/{index}",
    )


async def _change_issue_state(
    owner: str,
    repo: str,
    index: int,
    target: str,
    comment: str = "",
) -> IssueStateResponse:
    """Close or reopen an issue, optionally commenting first.

    The comment is posted *before* the state flip so that a failure to comment
    aborts the change entirely. An issue left open with a stray comment is
    recoverable; an issue closed with no explanation is not.
    """
    current = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues/{index}",
    )
    already = current.get("state") == target

    comment_posted = False
    if comment and not already:
        await _gitea_request(
            "POST",
            f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues/{index}/comments",
            json={"body": comment},
        )
        comment_posted = True

    # Only write when the state actually needs changing. This keeps repeat calls
    # idempotent and preserves Gitea's original closed_at timestamp.
    result = current
    if not already:
        result = await _patch_issue(owner, repo, index, {"state": target})

    return IssueStateResponse(
        index=result.get("number", index),
        state=result.get("state", target),
        title=result.get("title", ""),
        closed_at=result.get("closed_at"),
        html_url=result.get("html_url", ""),
        comment_posted=comment_posted,
        already_in_state=already,
    )


@router.post(
    "/issues/{owner}/{repo}/{index}/close",
    summary="Close an issue",
    operation_id="GiteaCloseIssue",
)
async def close_issue(
    owner: str, repo: str, index: int, change: IssueStateChange | None = None
) -> IssueStateResponse:
    """Close an issue, optionally posting a comment first.

    Idempotent: closing an already-closed issue succeeds without re-posting the
    comment. The response reports the state Gitea actually stored, never the
    requested state.
    """
    change = change or IssueStateChange()
    logger.info("Closing issue", owner=owner, repo=repo, index=index)
    return await _change_issue_state(
        owner, repo, index, "closed", comment=change.comment
    )


@router.post(
    "/issues/{owner}/{repo}/{index}/reopen",
    summary="Reopen an issue",
    operation_id="GiteaReopenIssue",
)
async def reopen_issue(owner: str, repo: str, index: int) -> IssueStateResponse:
    """Reopen a closed issue."""
    logger.info("Reopening issue", owner=owner, repo=repo, index=index)
    return await _change_issue_state(owner, repo, index, "open")


# ─── Pull request endpoints ─────────────────────────────────────────────────


@router.post("/pulls", summary="Create a pull request", operation_id="GiteaCreatePR")
async def create_pr(pr: PRCreate) -> dict:
    """
    Create a new pull request.

    Creates a PR from the head branch to the base branch.
    """
    logger.info(
        "Creating PR",
        owner=pr.owner,
        repo=pr.repo,
        head=pr.head,
        base=pr.base,
        title=pr.title,
    )

    # `draft` is NOT sent: Gitea has no such field and would discard it, which is
    # how this tool used to return success on a non-draft PR. It is translated
    # into the title prefix Gitea does honour.
    title = _apply_draft(pr.title, pr.draft)

    result = await _gitea_request(
        "POST",
        f"{settings.gitea_api_url}/repos/{pr.owner}/{pr.repo}/pulls",
        json={
            "title": title,
            "body": pr.body,
            "head": pr.head,
            "base": pr.base,
        },
    )

    logger.info(
        "PR created",
        owner=pr.owner,
        repo=pr.repo,
        index=result.get("number"),
        url=result.get("html_url"),
        draft_requested=pr.draft,
        draft_stored=bool(result.get("draft")),
    )

    if not isinstance(result, dict):
        return result

    # Report what Gitea stored, not what was asked for. If the prefix did not
    # take (a Gitea version with different markers, say), the caller must see
    # that instead of being told a draft was created.
    if pr.draft and not result.get("draft"):
        logger.warning(
            "draft requested but Gitea did not store draft state",
            owner=pr.owner,
            repo=pr.repo,
            index=result.get("number"),
            title=result.get("title"),
        )

    return {
        **result,
        "draft_requested": pr.draft,
        "draft": bool(result.get("draft")),
        "draft_applied_via": "title_prefix" if pr.draft else "none",
    }


@router.get(
    "/pulls/{owner}/{repo}", summary="List pull requests", operation_id="GiteaListPRs"
)
async def list_prs(
    owner: str,
    repo: str,
    state: str = Query(default="open", description="PR state: open, closed, or all"),
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=30, ge=1, le=100, description="Items per page"),
) -> list:
    """List pull requests in a repository."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls",
        params={"state": state, "page": page, "limit": limit},
    )


@router.patch(
    "/pulls/{owner}/{repo}/{index}",
    summary="Update a pull request",
    operation_id="GiteaUpdatePR",
)
async def update_pr(owner: str, repo: str, index: int, update: PRUpdate) -> dict:
    """
    Update a pull request's title, description, state and/or draft state.

    Only the fields provided in the request body will be updated.
    Omitted fields remain unchanged.

    `draft` is applied through the title, because Gitea has no draft field: see
    `_apply_draft`. That means a `draft` change always moves the title too.
    """
    logger.info("Updating PR", owner=owner, repo=repo, index=index)

    payload = update.model_dump(include={"title", "body", "state"}, exclude_none=True)
    if not payload and update.draft is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "No updatable fields supplied. Provide at least one of: "
                "title, body, state, draft."
            ),
        )

    if update.draft is not None:
        # Reuse the incoming title when there is one; otherwise read the current
        # one, since the prefix has to be applied to something.
        base_title = payload.get("title")
        if base_title is None:
            current = await _gitea_request(
                "GET", f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls/{index}"
            )
            base_title = current.get("title", "")
        payload["title"] = _apply_draft(base_title, update.draft)

    result = await _patch_issue(owner, repo, index, payload, resource="pulls")

    logger.info(
        "PR updated",
        owner=owner,
        repo=repo,
        index=index,
        draft_requested=update.draft,
        draft_stored=bool(result.get("draft")),
    )

    if not isinstance(result, dict):
        return result

    if update.draft is not None:
        result = {
            **result,
            "draft_requested": update.draft,
            "draft": bool(result.get("draft")),
            "draft_applied_via": "title_prefix" if update.draft else "none",
        }
    return result


@router.post(
    "/pulls/{owner}/{repo}/{index}/close",
    summary="Close a pull request without merging",
    operation_id="GiteaClosePR",
)
async def close_pr(owner: str, repo: str, index: int) -> PRStateResponse:
    """Close a pull request without merging it.

    Distinct from merging: the branch is not merged and the PR is simply marked
    closed. Idempotent for an already-closed PR.
    """
    logger.info("Closing PR", owner=owner, repo=repo, pr_index=index)

    current = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls/{index}",
    )
    if current.get("merged"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"PR #{index} is already merged and cannot be closed. "
                "Merged pull requests are immutable in Gitea."
            ),
        )

    already = current.get("state") == "closed"
    result = current
    if not already:
        result = await _patch_issue(owner, repo, index, {"state": "closed"}, resource="pulls")

    return PRStateResponse(
        index=result.get("number", index),
        state=result.get("state", "closed"),
        title=result.get("title", ""),
        merged=bool(result.get("merged")),
        html_url=result.get("html_url", ""),
        already_in_state=already,
    )


@router.post(
    "/pulls/{owner}/{repo}/{index}/merge",
    summary="Merge a pull request",
    operation_id="GiteaMergePR",
)
async def merge_pr(
    owner: str, repo: str, index: int, merge: MergeRequest | None = None
) -> MergeResponse:
    """
    Merge a pull request, refusing with a ``blocked_reason`` instead of passing
    Gitea's own status codes through.

    WHY NOT JUST CALL UPSTREAM: Gitea's refusals are misleading. A PR that exists
    but was closed without merging answers ``404 not found``, which reads as
    "wrong PR number" and sends a caller looking for a different index instead of
    reopening it. A WIP PR answers ``405``, indistinguishable from other 405s. So
    one GET is done first and the refusal is expressed in caller-actionable terms.

    CHECK ORDER IS LOAD-BEARING: ``merged`` -> ``state`` -> ``draft`` -> ``mergeable``.
    ``mergeable`` means "no conflicts", NOT "can be merged now": an already-merged
    PR reports ``mergeable: true``, and a WIP PR reports ``mergeable: false``.
    Testing ``mergeable`` first would pass an already-merged PR and mislabel a
    draft as a conflict. Reordering these must fail a test.

    No mutation happens on a refusal, so a wrong reason can never be destructive.

    MERGE COMMENTS ARE NOT SUPPORTED: ``MergePullRequestOption`` has no comment
    field. Post the comment separately with ``GiteaPostComment``. Unlike closing,
    the comment cannot be ordered safely around the merge, so it is two calls.

    WARNING: if the repository deploys on push to the base branch, merging starts
    a deployment. ``force_merge`` defaults to false; it is the only way to bypass
    required checks.
    """
    req = merge or MergeRequest()
    logger.info("Merging PR", owner=owner, repo=repo, pr_index=index, do=req.do)

    base = f"{settings.gitea_api_url}/repos/{owner}/{repo}"

    if req.do == "manually-merged":
        raise HTTPException(
            status_code=422,
            detail=(
                "do='manually-merged' records a merge that was done by hand without "
                "merging anything. Refusing: use merge, rebase, rebase-merge, "
                "squash, or fast-forward-only."
            ),
        )

    try:
        pr = await _gitea_request("GET", f"{base}/pulls/{index}")
    except GiteaError as e:
        if e.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail={
                    "blocked_reason": MergeBlocked.not_found.value,
                    "detail": f"Pull request #{index} does not exist in {owner}/{repo}.",
                    "index": index,
                    "merged": False,
                },
            ) from e
        raise

    if not isinstance(pr, dict):
        raise HTTPException(
            status_code=502,
            detail=f"Expected a pull request from Gitea, got {type(pr).__name__}",
        )

    pr_base = ((pr.get("base") or {}).get("ref") or "")
    pr_head = ((pr.get("head") or {}).get("ref") or "")
    merge_sha = pr.get("merge_commit_sha") or ""

    def refuse(reason: MergeBlocked, detail: str) -> None:
        logger.info(
            "Merge refused",
            owner=owner,
            repo=repo,
            pr_index=index,
            reason=reason.value,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "blocked_reason": reason.value,
                "detail": detail,
                "index": index,
                "merged": False,
                "base": pr_base,
                "head": pr_head,
                "html_url": pr.get("html_url", ""),
            },
        )

    # 1. merged -- FIRST. A merged PR still reports mergeable: true.
    #
    # This is IDEMPOTENT SUCCESS, not a refusal: re-requesting a merge that
    # already happened is not a caller error, and returning 409 would make a
    # retrying agent believe the merge failed. Nothing is written, and Gitea's
    # original merge_commit_sha/merged_at are preserved rather than a new merge
    # being claimed.
    if pr.get("merged"):
        logger.info(
            "PR already merged; no write", owner=owner, repo=repo, pr_index=index
        )
        return MergeResponse(
            index=index,
            merged=True,
            merge_commit_sha=merge_sha,
            merged_at=pr.get("merged_at"),
            base=pr_base,
            head=pr_head,
            do="",
            already_merged=True,
            html_url=pr.get("html_url", ""),
        )

    # 2. state -- before mergeable: a closed PR reports mergeable: false, which
    # would be misreported as a conflict.
    if pr.get("state") == "closed":
        refuse(
            MergeBlocked.closed_unmerged,
            (
                f"PR #{index} is closed but was never merged. Reopen it first "
                "(GiteaUpdatePR with state='open'), then merge."
            ),
        )

    # 3. draft -- before mergeable: a WIP PR reports mergeable: false.
    if pr.get("draft"):
        refuse(
            MergeBlocked.is_draft,
            (
                f"PR #{index} is a draft (work in progress). Mark it ready before "
                "merging (GiteaUpdatePR with draft=false)."
            ),
        )

    # 4. mergeable -- LAST. "No conflicts", not "can merge now".
    #
    # Gitea computes this ASYNCHRONOUSLY. Observed live: a freshly opened PR
    # reported mergeable=false, and 2s later the same PR reported true, after
    # which the merge succeeded. Treating the first false as a conflict refuses a
    # perfectly mergeable PR -- a wrong answer, not merely a slow one. So a false
    # is re-read a few times first; a genuine conflict stays false throughout.
    mergeable = bool(pr.get("mergeable"))
    if not mergeable:
        for _ in range(_MERGEABLE_RECHECKS):
            await asyncio.sleep(_MERGEABLE_RECHECK_DELAY)
            retry = await _gitea_request("GET", f"{base}/pulls/{index}")
            if isinstance(retry, dict) and retry.get("mergeable"):
                pr = retry
                mergeable = True
                break

    if not mergeable:
        refuse(
            MergeBlocked.conflicts,
            (
                f"PR #{index} cannot be merged cleanly into '{pr_base or 'the base branch'}'. "
                "Gitea reports no mergeable state even after re-checking, which "
                "normally means a genuine conflict. Resolve the conflicts first."
            ),
        )

    # Resolve the merge style from the repository rather than hardcoding "merge",
    # so per-repo policy is honoured instead of overruled.
    style = req.do
    if not style:
        info = await _gitea_request("GET", base)
        style = (
            info.get("default_merge_style", "") if isinstance(info, dict) else ""
        ) or "merge"

    payload: dict = {"do": style}
    if req.force_merge:
        payload["force_merge"] = True
    if req.delete_branch_after_merge:
        payload["delete_branch_after_merge"] = True

    await _gitea_request("POST", f"{base}/pulls/{index}/merge", json=payload)

    # Report Gitea's STORED state, never the requested one.
    final = await _gitea_request("GET", f"{base}/pulls/{index}")
    final = final if isinstance(final, dict) else {}

    logger.info(
        "PR merged",
        owner=owner,
        repo=repo,
        pr_index=index,
        do=style,
        merged=bool(final.get("merged")),
    )

    return MergeResponse(
        index=index,
        merged=bool(final.get("merged")),
        merge_commit_sha=(final.get("merge_commit_sha") or ""),
        merged_at=final.get("merged_at"),
        base=((final.get("base") or {}).get("ref") or pr_base),
        head=((final.get("head") or {}).get("ref") or pr_head),
        do=style,
        branch_deleted=req.delete_branch_after_merge,
        html_url=final.get("html_url", pr.get("html_url", "")),
    )


# ─── Comment endpoints ──────────────────────────────────────────────────────


@router.post(
    "/issues/{owner}/{repo}/{index}/comments",
    summary="Post a comment on an issue or PR",
    operation_id="GiteaPostComment",
)
async def post_comment(
    owner: str, repo: str, index: int, comment: CommentCreate
) -> dict:
    """
    Post a comment on an issue or pull request.

    Works for both issues and PRs — they share the same comments endpoint in Gitea.
    """
    logger.info("Posting comment", owner=owner, repo=repo, index=index)

    # Reject a body that names a different issue than the URL. Silently posting
    # to the path's issue (the old behaviour) left the caller believing the
    # comment landed somewhere else, which is undetectable from the response.
    if comment.issue_index is not None and comment.issue_index != index:
        raise HTTPException(
            status_code=422,
            detail=(
                f"issue_index={comment.issue_index} conflicts with the issue in the "
                f"URL path ({index}). The path wins and issue_index is ignored; "
                f"either drop issue_index or make the URL match it."
            ),
        )

    result = await _gitea_request(
        "POST",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues/{index}/comments",
        json={"body": comment.body},
    )

    logger.info("Comment posted", owner=owner, repo=repo, index=index)
    return result


@router.get(
    "/issues/{owner}/{repo}/{index}/comments",
    summary="List comments",
    operation_id="GiteaListComments",
)
async def list_comments(
    owner: str,
    repo: str,
    index: int,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=30, ge=1, le=100, description="Items per page"),
) -> list:
    """List comments on an issue or pull request."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues/{index}/comments",
        params={"page": page, "limit": limit},
    )


# ─── PR detail endpoints ────────────────────────────────────────────────────


@router.get(
    "/pulls/{owner}/{repo}/{index}/diff",
    summary="Get PR diff",
    operation_id="GiteaGetPRDiff",
)
async def get_pr_diff(owner: str, repo: str, index: int) -> str:
    """
    Get the full unified diff for a pull request.

    Gitea/Forgejo serve the diff at ``/pulls/{index}.diff`` -- a file-extension
    suffix. ``/pulls/{index}/diff`` is not a route and 404s on every version
    so this tool never worked against a real server until it called the right one.
    """
    logger.info("Getting PR diff", owner=owner, repo=repo, pr_index=index)

    response = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls/{index}.diff",
    )

    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return response.get("content", response.get("diff", str(response)))
    return str(response)



@router.get(
    "/pulls/{owner}/{repo}/{index}/files",
    summary="Get PR changed files",
    operation_id="GiteaGetPRFiles",
)
async def get_pr_files(
    owner: str,
    repo: str,
    index: int,
) -> list[PRFileChange]:
    """
    Get the list of files changed in a pull request.

    Returns file-level change summaries (additions, deletions, status).
    """
    logger.info("Getting PR files", owner=owner, repo=repo, pr_index=index)

    files_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls/{index}/files",
    )

    return [
        PRFileChange(
            filename=f.get("filename", ""),
            status=f.get("status", ""),
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            changes=f.get("changes", 0),
            binary=f.get("binary", False),
            patch=f.get("patch", ""),
        )
        for f in files_data
    ]


@router.get(
    "/pulls/{owner}/{repo}/{index}/comments",
    summary="List PR review comments",
    operation_id="GiteaListPRComments",
)
async def list_pr_comments(owner: str, repo: str, index: int) -> list:
    """
    List review/inline comments on a pull request.

    Current Gitea/Forgejo group comments per review; the flat
    /pulls/{index}/comments endpoint this used to call was removed.
    """
    logger.info("Listing PR review comments", owner=owner, repo=repo, pr_index=index)
    api = f"{settings.gitea_api_url}/repos/{owner}/{repo}"

    try:
        reviews = await _gitea_request("GET", f"{api}/pulls/{index}/reviews")
    except GiteaError as e:
        if e.status_code != 404:
            raise
        reviews = None  # very old Gitea: fall back to the flat endpoint

    if isinstance(reviews, list):
        comments = []
        for review in reviews[:50]:
            review_id = review.get("id")
            if not review_id:
                continue
            try:
                found = await _gitea_request(
                    "GET", f"{api}/pulls/{index}/reviews/{review_id}/comments"
                )
            except GiteaError as e:
                if e.status_code == 404:
                    continue
                raise
            if isinstance(found, list):
                comments.extend(found)
        comments.sort(key=lambda c: c.get("id", 0))
        return comments

    return await _gitea_request("GET", f"{api}/pulls/{index}/comments")



@router.delete(
    "/issues/{owner}/{repo}/{index}/comments/{comment_id}",
    summary="Delete a comment",
    operation_id="GiteaDeleteComment",
)
async def delete_comment(
    owner: str,
    repo: str,
    index: int,
    comment_id: int,
) -> dict:
    """
    Delete a comment on an issue or pull request.

    Use this to clean up outdated or incorrect comments.

    The `index` path segment is accepted for URL compatibility only and is
    **ignored**: Gitea's delete-comment endpoint is repository-scoped
    (`DELETE /repos/{owner}/{repo}/issues/comments/{comment_id}`) and has no
    issue dimension, so the comment is removed regardless of which issue or PR
    number is supplied here. Pass the correct number for clarity, but do not
    expect a wrong one to be rejected. The ignored value is echoed back as
    `index_ignored`.
    """
    logger.info(
        "Deleting comment", owner=owner, repo=repo, index=index, comment_id=comment_id
    )

    # Success is 204 No Content, which _gitea_request reports as {}. A non-2xx
    # status (notably 404 for an unknown comment id) has already raised.
    await _gitea_request(
        "DELETE",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues/comments/{comment_id}",
    )

    logger.info("Comment deleted", owner=owner, repo=repo, comment_id=comment_id)
    return {
        "status": "deleted",
        "comment_id": comment_id,
        "index_ignored": index,
    }


@router.post(
    "/pulls/{owner}/{repo}/{index}/reviews",
    summary="Submit a PR review",
    operation_id="GiteaSubmitPRReview",
)
async def submit_pr_review(
    owner: str, repo: str, index: int, review: ReviewCreate
) -> dict:
    """
    Submit a review on a pull request.

    Review types: `approved`, `changes_requested`, or `comment`.
    """
    logger.info(
        "Submitting PR review",
        owner=owner,
        repo=repo,
        pr_index=index,
        review_type=review.type,
    )

    # Send `event` (>= 1.24 / Forgejo, uppercase) AND legacy `type` (<= 1.23,
    # lowercase). Each generation reads its own field and ignores the other.
    # Sending only `type` on current versions silently creates a PENDING
    # review that is never posted.
    event, legacy_type = _REVIEW_EVENT_MAP.get(review.type, (None, None))
    if event is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid review type '{review.type}'. Use 'approved', "
                "'changes_requested', or 'comment'."
            ),
        )
    if review.type == "changes_requested" and not review.body.strip():
        raise HTTPException(
            status_code=400,
            detail="A review body is required when requesting changes.",
        )

    payload = {"event": event, "type": legacy_type, "body": review.body}
    if review.commit_id:
        payload["commit_id"] = review.commit_id

    result = await _gitea_request(
        "POST",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls/{index}/reviews",
        json=payload,
    )

    logger.info("PR review submitted", owner=owner, repo=repo, pr_index=index)
    return result


@router.post(
    "/pulls/{owner}/{repo}/{index}/summary",
    summary="Get PR summary",
    operation_id="GiteaGetPRSummary",
)
async def get_pr_summary(
    owner: str,
    repo: str,
    index: int,
) -> PRSummaryResponse:
    """
    Get a structured summary of a pull request.

    Combines PR metadata with file change statistics to produce
    a concise overview. Useful for quick triage before deep review.
    """
    logger.info("Getting PR summary", owner=owner, repo=repo, pr_index=index)

    # Fetch PR details
    pr_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls/{index}",
    )

    # Fetch changed files
    files_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls/{index}/files",
    )

    # Calculate stats
    total_additions = sum(f.get("additions", 0) for f in files_data)
    total_deletions = sum(f.get("deletions", 0) for f in files_data)
    changed_files = [f.get("filename", "") for f in files_data]

    # Build auto-summary
    base_branch = pr_data.get("base", {}).get("ref", "")
    head_branch = pr_data.get("head", {}).get("ref", "")
    pr_title = pr_data.get("title", "")
    pr_body = pr_data.get("body", "")
    pr_state = pr_data.get("state", "")

    # Generate a concise summary from available data
    summary_parts = []
    summary_parts.append(f"**PR #{index}: {pr_title}**")
    summary_parts.append(f"State: {pr_state} | {head_branch} → {base_branch}")
    summary_parts.append(
        f"Changes: {len(files_data)} files, +{total_additions} -{total_deletions} lines"
    )
    if pr_body:
        # Truncate body for summary
        body_preview = pr_body[:200]
        if len(pr_body) > 200:
            body_preview += "..."
        summary_parts.append(f"Description: {body_preview}")

    summary = "\n".join(summary_parts)

    return PRSummaryResponse(
        pr_number=pr_data.get("number", index),
        title=pr_title,
        state=pr_state,
        body=pr_body,
        files_changed=len(files_data),
        total_additions=total_additions,
        total_deletions=total_deletions,
        changed_files=changed_files,
        base_branch=base_branch,
        head_branch=head_branch,
        summary=summary,
    )


# ─── Health check ───────────────────────────────────────────────────────────


@router.get("/health", summary="Check Gitea connection", operation_id="GiteaHealth")
async def gitea_health() -> dict:
    """Check if Gitea is configured and accessible."""
    if not settings.is_gitea_configured():
        return {"status": "error", "message": "Gitea is not configured"}

    try:
        response = await _gitea_request("GET", f"{settings.gitea_api_url}/user")
        return {
            "status": "ok",
            "username": response.get("login", "unknown"),
            "instance": settings.GITEA_INSTANCE_URL,
        }
    except GiteaError as e:
        return {"status": "error", "message": str(e)}


@router.get("/token", summary="Get Gitea API token", operation_id="GiteaGetToken")
async def get_token() -> dict:
    """
    Get the configured Gitea API token.

    This endpoint is **disabled by default** for security reasons.
    Enable it by setting the `EXPOSE_GITEA_TOKEN=true` environment variable.

    Primarily useful for agents that need the token to perform
    git operations (e.g., cloning repos, committing changes).

    Returns 403 if the endpoint is not explicitly enabled.
    """
    if not settings.is_gitea_configured():
        raise HTTPException(
            status_code=403,
            detail="Gitea is not configured. Set GITEA_TOKEN and GITEA_INSTANCE_URL environment variables.",
        )

    if not settings.EXPOSE_GITEA_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="Token endpoint is disabled. Set EXPOSE_GITEA_TOKEN=true to enable it.",
        )

    logger.info("Gitea token retrieved via API")
    return {
        "token": settings.GITEA_TOKEN,
        "instance": settings.GITEA_INSTANCE_URL,
    }


# ─── Pipeline / Actions endpoints ───────────────────────────────────────────
# These use the Gitea/Forgejo Actions API. Note: Forgejo's Actions API differs
# from GitHub Actions. The endpoints /actions/runs/{id}/jobs and job log
# endpoints may not be available on all Forgejo versions. We fall back to
# /actions/tasks?run_id={id} for job listing when /jobs is unavailable.


@router.get(
    "/pulls/{owner}/{repo}/{index}/pipeline",
    summary="Get pipeline status for a PR",
    operation_id="GiteaGetPRPipeline",
)
async def get_pr_pipeline(
    owner: str,
    repo: str,
    index: int,
) -> PipelineStatusResponse:
    """
    Get the CI status for a pull request.

    Use this before merging: a run that cannot be found is reported as
    ``status: "unknown"``, never as ``pending``.

    HOW THE RUN IS FOUND: a ``pull_request``-event workflow run in Gitea carries
    ``head_branch: null`` (verified against live 28.1.0 -- PR runs have no branch
    field at all), so filtering by branch matches nothing and the old code
    answered ``run_id: 0 / "No pipeline runs found"`` on PRs that were in fact
    green. The only reliable join key is ``head_sha``.

    Consequently the PR's OWN head commit is checked first, and the merge commit
    is the fallback: runs are triggered per push, so a PR whose branch has not
    moved since the run was created has a run for its head SHA, while a PR that
    was merged afterwards has one for the merge commit instead. Both are the
    "status of this PR" as a caller means it.

    A merged or closed PR is deliberately still answered. Gitea rewrites
    ``head.ref`` on a closed PR to ``refs/pull/<n>/head`` (so branch filtering
    could never have worked for one), and "did CI pass on this PR?" is asked
    after the merge at least as often as before it.

    Returns ``status``: ``success`` | ``failure`` | ``running`` | ``waiting`` |
    ``cancelled`` | ``skipped`` | ``unknown`` (no run could be attributed to
    this PR).
    """
    logger.info("Getting PR pipeline status", owner=owner, repo=repo, pr_index=index)

    base = f"{settings.gitea_api_url}/repos/{owner}/{repo}"

    pr_data = await _gitea_request("GET", f"{base}/pulls/{index}")
    if not isinstance(pr_data, dict):
        raise HTTPException(
            status_code=502,
            detail=f"Expected a pull request from Gitea, got {type(pr_data).__name__}",
        )

    head = pr_data.get("head") or {}
    head_sha = (head.get("sha") or "")
    head_branch = head.get("ref") or head.get("label") or ""
    # label keeps the human name when ref has been rewritten to refs/pull/N/head.
    head_label = head.get("label") or head_branch
    merge_sha = pr_data.get("merge_commit_sha") or ""

    if not head_sha:
        raise GiteaError(
            f"Could not determine the head commit for PR #{index}", status_code=404
        )

    logger.info(
        "Looking up pipeline for PR head",
        head_sha=head_sha[:12],
        head_branch=head_label,
    )

    runs_data = await _gitea_request(
        "GET",
        f"{base}/actions/runs",
        params={"event": "pull_request", "limit": _PIPELINE_RUN_LIMIT},
    )

    runs = (
        runs_data if isinstance(runs_data, list) else (runs_data or {}).get("workflow_runs", [])
    )
    runs = [r for r in runs if isinstance(r, dict)]

    def run_sha(run: dict) -> str:
        return run.get("head_sha") or run.get("commit_sha") or ""

    # The PR's own commit first, then the merge commit. Highest id wins inside
    # each group: re-runs and repeat pushes both produce several runs per SHA.
    matched = [r for r in runs if run_sha(r) == head_sha]
    matched_via = "head_sha"
    if not matched and merge_sha:
        matched = [r for r in runs if run_sha(r) == merge_sha]
        matched_via = "merge_commit_sha"

    if not matched:
        # NOT "pending". pending reads as "still running, wait", which invites a
        # caller to poll forever or to merge assuming checks are queued. The
        # honest answer is that no run could be attributed to this PR.
        logger.info(
            "No workflow run matched this PR",
            owner=owner,
            repo=repo,
            pr_index=index,
            head_sha=head_sha[:12],
            runs_seen=len(runs),
        )
        return PipelineStatusResponse(
            run_id=0,
            run_number=0,
            status="unknown",
            conclusion="",
            name="",
            display_title=(
                f"No workflow run found for PR #{index} "
                f"(head {head_sha[:8]}, {len(runs)} pull_request run(s) examined)"
            ),
            event="",
            branch=head_label,
            commit_sha=head_sha,
            html_url="",
            started_at="",
            updated_at="",
            matched_by="",
        )

    run = max(matched, key=lambda r: r.get("id", 0))
    display_title = run.get("display_title") or run.get("title") or ""
    status = run.get("status") or "unknown"
    conclusion = run.get("conclusion") or ""

    return PipelineStatusResponse(
        run_id=run.get("id", 0),
        run_number=run.get("index_in_repo") or run.get("run_number", 0),
        status=status,
        conclusion=conclusion,
        name=run.get("name") or display_title,
        display_title=display_title,
        event=run.get("event", ""),
        branch=run.get("head_branch") or run.get("prettyref") or head_label,
        commit_sha=run_sha(run) or head_sha,
        html_url=run.get("html_url") or run.get("url", ""),
        started_at=run.get("started_at") or run.get("run_started_at") or run.get("started", ""),
        updated_at=run.get("updated_at") or run.get("updated", ""),
        matched_by=matched_via,
        path=run.get("path", ""),
    )

async def _fetch_gitea_log_text(url: str) -> str:
    """Fetch a Gitea log endpoint and return its text. Capped at 50 KB."""
    response = await _gitea_request("GET", url)
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        log_url = response.get("url", "")
        if log_url:
            async with httpx.AsyncClient(timeout=30.0) as client:
                got = await client.get(log_url, headers=settings.gitea_headers)
                got.raise_for_status()
                return got.text[:50000]
    return str(response)


async def _fetch_job_logs(
    api_url: str, owner: str, repo: str, run_id: int, job_id: int
) -> str | None:
    """Try both known per-job log routes; None if neither exists here."""
    for url in (
        f"{api_url}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/logs",
        f"{api_url}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
    ):
        try:
            return await _fetch_gitea_log_text(url)
        except GiteaError as e:
            if e.status_code != 404:
                raise
    return None


@router.get(
    "/actions/runs/{owner}/{repo}/{run_id}/logs",
    summary="Get pipeline output/logs",
    operation_id="GiteaGetPipelineOutput",
)
async def get_pipeline_output(
    owner: str,
    repo: str,
    run_id: int,
) -> PipelineOutputResponse:
    """
    Get the full pipeline output and logs for a workflow run.

    Fetches all jobs in the specified workflow run and retrieves their logs.
    This is useful for debugging pipeline failures — after checking the status
    with GiteaGetPRPipeline, you can fetch the detailed output.

    Returns combined logs from all jobs, along with job-level status information.

    Note: On some Forgejo versions, individual job logs are not available via API.
    In that case, job statuses and URLs are returned instead.
    """
    logger.info("Getting pipeline output", owner=owner, repo=repo, run_id=run_id)

    # Get run details
    run_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/actions/runs/{run_id}",
    )

    # Try to get jobs — first attempt the standard /jobs endpoint,
    # fall back to /actions/tasks for older Forgejo versions
    jobs_list = []
    try:
        jobs_data = await _gitea_request(
            "GET",
            f"{settings.gitea_api_url}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
        )
        # Gitea 29 wraps the list as {"jobs": [...], "total_count": N};
        # older builds use "workflow_jobs". Reading only the latter yielded an
        # empty job list, silently skipping the log fallback below.
        jobs_list = (
            jobs_data
            if isinstance(jobs_data, list)
            else jobs_data.get("jobs") or jobs_data.get("workflow_jobs", [])
        )
        logger.info("Fetched jobs via /jobs endpoint", count=len(jobs_list))
    except GiteaError as e:
        if e.status_code == 404:
            logger.info(
                "/jobs endpoint not available, falling back to /actions/tasks",
                run_id=run_id,
            )
            # Fall back to the tasks endpoint available on older Forgejo.
            # Note: /actions/tasks?run_id=X doesn't filter on some Forgejo versions,
            # so we fetch all tasks and filter client-side by run_number.
            run_number = run_data.get("index_in_repo")
            tasks_data = await _gitea_request(
                "GET",
                f"{settings.gitea_api_url}/repos/{owner}/{repo}/actions/tasks",
            )
            all_tasks = (
                tasks_data
                if isinstance(tasks_data, list)
                else tasks_data.get("workflow_runs", [])
            )
            # Filter to only tasks for this run
            if run_number is not None:
                jobs_list = [t for t in all_tasks if t.get("run_number") == run_number]
                logger.info(
                    "Filtered tasks by run_number",
                    run_number=run_number,
                    total=all_tasks.__len__(),
                    matched=len(jobs_list),
                )
            else:
                # No run_number available, use all tasks (unlikely but safe fallback)
                jobs_list = all_tasks
                logger.warning("No run_number found, returning all tasks")
        else:
            raise

    # Build job summaries
    jobs = [
        PipelineJob(
            job_id=job.get("id", 0),
            name=job.get("name") or "",
            status=job.get("status") or "",
            conclusion=job.get("conclusion") or "",
            started_at=job.get("started_at") or job.get("run_started_at") or "",
            url=job.get("url") or job.get("html_url") or "",
        )
        for job in jobs_list
    ]

    # Fetch logs for each job
    log_sections = []
    logs_available = True

    for job in jobs_list:
        job_id = job.get("id", 0)
        job_name = job.get("name", f"job-{job_id}")
        log_sections.append(f"Job: {job_name} (ID: {job_id})")

        log_text = await _fetch_job_logs(
            settings.gitea_api_url, owner, repo, run_id, job_id
        )
        if log_text is not None:
            log_sections.append(log_text)
        else:
            logs_available = False
            job_url = job.get("url") or job.get("html_url") or ""
            if job_url:
                log_sections.append(
                    f"[Logs not available via API. View in browser: {job_url}]"
                )
            else:
                log_sections.append(f"[Logs not available via API for job {job_id}]")

    combined_logs = "\n".join(log_sections)

    # Last resort: current Gitea/Forgejo expose run-level logs.
    if not logs_available and jobs_list:
        try:
            run_logs = await _fetch_gitea_log_text(
                f"{settings.gitea_api_url}/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
            )
        except GiteaError as e:
            if e.status_code == 404:
                run_logs = None
            else:
                raise
        if run_logs:
            log_sections.insert(
                0,
                "=" * 60
                + "\nRun-level logs (per-job logs unavailable on this instance)\n"
                + "=" * 60
                + "\n"
                + run_logs,
            )
            combined_logs = "\n".join(log_sections)
            logs_available = True

    return PipelineOutputResponse(
        run_id=run_data.get("id", run_id),
        status=run_data.get("status", "unknown"),
        conclusion=run_data.get("conclusion", ""),
        name=run_data.get("name", ""),
        jobs=jobs,
        logs=combined_logs,
    )
