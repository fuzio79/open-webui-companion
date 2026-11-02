"""Gitea integration endpoints for Open-WebUI.

Provides tools for repository management, file operations, issue tracking,
pull requests, and commenting via the Gitea REST API.
"""

import asyncio
import os
import base64
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


# ─── Pipeline / Actions models ──────────────────────────────────────────────


class PipelineStatusResponse(BaseModel):
    """Schema for pipeline status response."""

    run_id: int = Field(..., description="Workflow run ID")
    run_number: int = Field(
        default=0, description="Run number within the workflow (index_in_repo)"
    )
    status: str = Field(
        ...,
        description="Run status: success, failure, running, waiting, cancelled, skipped",
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

    # Determine branch — fetch repo default if not provided
    branch = file.branch
    if not branch:
        try:
            repo_info = await _gitea_request(
                "GET",
                f"{settings.gitea_api_url}/repos/{file.owner}/{file.repo}",
            )
            branch = repo_info.get("default_branch", "main")
            logger.info(
                "Using repo default branch",
                owner=file.owner,
                repo=file.repo,
                branch=branch,
            )
        except GiteaError:
            branch = "main"  # fallback

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
    Get the latest pipeline (workflow run) status for a pull request.

    Fetches the most recent workflow run triggered by this PR and returns
    its status. An agent can use this after getting PR details to check
    whether CI has passed.

    Returns the latest run's status, which can be:
    - `success`: Pipeline passed
    - `failure`: Pipeline failed
    - `running`: Pipeline is still in progress
    - `waiting`: Pipeline is waiting for a runner
    - `cancelled`: Pipeline was cancelled
    - `skipped`: Pipeline was skipped
    """
    logger.info("Getting PR pipeline status", owner=owner, repo=repo, pr_index=index)

    # Get PR details to find the head branch
    pr_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls/{index}",
    )

    head_branch = pr_data.get("head", {}).get("ref", "")
    if not head_branch:
        raise GiteaError(
            f"Could not determine head branch for PR #{index}", status_code=404
        )

    logger.info("Looking up pipeline for branch", branch=head_branch)

    # List workflow runs, filtered by the PR's head branch and pull_request event.
    # Note: Gitea/Forgejo returns runs sorted by ID ascending (oldest first) by default.
    # We fetch all matching runs and select the latest one (highest ID).
    # Gitea filters by `branch`, Forgejo by `ref` -- send both, so the branch
    # filter works on either flavour (each server ignores the unknown one).
    runs_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/actions/runs",
        params={"branch": head_branch, "ref": head_branch, "event": "pull_request"},
    )

    # The API returns {"workflow_runs": [...], "total_count": N}
    runs = (
        runs_data if isinstance(runs_data, list) else runs_data.get("workflow_runs", [])
    )

    if not runs:
        return PipelineStatusResponse(
            run_id=0,
            run_number=0,
            status="pending",
            conclusion="",
            name="",
            display_title=f"No pipeline runs found for PR #{index}",
            event="",
            branch=head_branch,
            commit_sha="",
            html_url="",
            started_at="",
            updated_at="",
        )

    # Select the most recent run (highest ID), since the API returns ascending order
    run = max(runs, key=lambda r: r.get("id", 0))

    # Map fields — Gitea/Forgejo Actions API uses slightly different field names
    # than GitHub Actions. Handle both conventions.
    display_title = run.get("display_title") or run.get("title") or ""
    return PipelineStatusResponse(
        run_id=run.get("id", 0),
        run_number=run.get("index_in_repo") or run.get("run_number", 0),
        status=run.get("status", "unknown"),
        conclusion=run.get("conclusion", ""),
        name=run.get("name") or display_title,
        display_title=display_title,
        event=run.get("event", ""),
        branch=run.get("head_branch") or run.get("prettyref") or head_branch,
        commit_sha=run.get("head_sha") or run.get("commit_sha", ""),
        html_url=run.get("html_url") or run.get("url", ""),
        started_at=run.get("started_at")
        or run.get("run_started_at")
        or run.get("started", ""),
        updated_at=run.get("updated_at") or run.get("updated", ""),
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
