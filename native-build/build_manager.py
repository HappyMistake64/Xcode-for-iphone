#!/usr/bin/env python3
"""Resolve Xcode Pocket builds without ever guessing which run owns an artifact.

Only read-only GitHub Actions REST API endpoints are used. The original request
identity (UUID and full source commit SHA) must be retained by the caller.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

WORKFLOW = ".github/workflows/pocket-ios.yml"
STATE_SCHEMA = "xcode-pocket-build-state/v1"
MAX_RUN_PAGES = 5
UUID_PATTERN = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class API(Protocol):
    def get(self, path: str) -> dict[str, Any]: ...


class GitHubAPI:
    def __init__(self, token: str | None = None):
        self.token = token

    def get(self, path: str) -> dict[str, Any]:
        if not path.startswith("/repos/"):
            raise ValueError("Only repository-scoped API endpoints are supported")
        req = urllib.request.Request(
            "https://api.github.com" + path,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "xcode-pocket-build-manager/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": "Bearer " + self.token} if self.token else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("Unexpected GitHub API response")
        return result


@dataclass(frozen=True)
class Identity:
    repo: str
    request_id: str
    source_sha: str

    def __post_init__(self) -> None:
        if not REPO_PATTERN.fullmatch(self.repo):
            raise ValueError("repo must be in owner/name format")
        if not UUID_PATTERN.fullmatch(self.request_id) or str(uuid.UUID(self.request_id)) != self.request_id:
            raise ValueError("request_id must be a canonical lowercase UUID")
        if not SHA_PATTERN.fullmatch(self.source_sha):
            raise ValueError("source_sha must be a full lowercase 40-character commit SHA")

    @property
    def prefix(self) -> str:
        return f"/repos/{self.repo}"

    @property
    def title(self) -> str:
        return f"Pocket iOS {self.request_id}"

    def artifact_name(self, kind: str) -> str:
        if kind == "identity":
            return f"pocket-ios-identity-{self.request_id}-{self.source_sha}"
        if kind in ("app", "logs"):
            # Retain names consumed by the current web editor; the SHA-bound
            # identity artifact in the SAME run is required before downloads.
            return f"pocket-ios-{kind}-{self.request_id}"
        raise ValueError("Unsupported artifact kind")


def state(identity: Identity, name: str, *, run: dict[str, Any] | None = None,
          artifacts: list[dict[str, Any]] | None = None, verified: bool = False,
          message: str = "", candidates: list[int] | None = None) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "request_id": identity.request_id,
        "source_sha": identity.source_sha,
        "state": name,
        "run_id": run.get("id") if run else None,
        "run_url": run.get("html_url") if run else None,
        "source_verified": verified,
        "artifacts": artifacts or [],
        "message": message,
        "candidate_run_ids": candidates or [],
    }


def run_matches(identity: Identity, run: dict[str, Any]) -> bool:
    if run.get("display_title") != identity.title or run.get("event") != "workflow_dispatch":
        return False
    if str(run.get("path", "")).split("@")[0] != WORKFLOW:
        return False
    repository = run.get("repository") or {}
    head_repository = run.get("head_repository") or {}
    if repository.get("full_name") != identity.repo:
        return False
    if head_repository and head_repository.get("full_name") != identity.repo:
        return False
    return True


def find_run(api: API, identity: Identity, run_id: int | None) -> tuple[dict[str, Any] | None, list[int], str]:
    if run_id is not None:
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise ValueError("run_id must be a positive integer")
        run = api.get(f"{identity.prefix}/actions/runs/{run_id}")
        return (run if run_matches(identity, run) else None, [run_id],
                "" if run_matches(identity, run) else "Run identity does not match the requested project.")

    matches: list[dict[str, Any]] = []
    for page in range(1, MAX_RUN_PAGES + 1):
        path = (f"{identity.prefix}/actions/workflows/pocket-ios.yml/runs"
                f"?event=workflow_dispatch&per_page=100&page={page}")
        runs = api.get(path).get("workflow_runs", [])
        if not isinstance(runs, list):
            raise ValueError("Unexpected workflow runs payload")
        matches.extend(r for r in runs if isinstance(r, dict) and run_matches(identity, r))
        if len(runs) < 100 or len(matches) > 1:
            break
    matches.sort(key=lambda r: r.get("id", 0))
    if len(matches) > 1:
        return None, [r["id"] for r in matches], "Multiple runs share this request ID. Supply an exact run ID."
    if not matches:
        return None, [], "No matching run in the latest 500 workflow runs."
    return matches[0], [matches[0]["id"]], ""


def get_artifacts(api: API, identity: Identity, run: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in ("app", "logs", "identity")}
    for page in range(1, 6):
        path = f"{identity.prefix}/actions/runs/{run['id']}/artifacts?per_page=100&page={page}"
        response = api.get(path)
        artifacts = response.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise ValueError("Unexpected workflow artifacts payload")
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("expired") is True:
                continue
            parent = artifact.get("workflow_run") or {}
            if parent and parent.get("id") != run["id"]:
                continue
            for kind in grouped:
                if artifact.get("name") == identity.artifact_name(kind):
                    grouped[kind].append({
                        "kind": kind,
                        "name": artifact["name"],
                        "id": artifact["id"],
                        "size_in_bytes": artifact.get("size_in_bytes"),
                        "download_url": f"https://api.github.com{identity.prefix}/actions/artifacts/{artifact['id']}/zip",
                    })
        if len(artifacts) < 100:
            break
    return grouped


def resolve(api: API, identity: Identity, run_id: int | None = None) -> dict[str, Any]:
    run, candidates, warning = find_run(api, identity, run_id)
    if run is None:
        name = "identity_mismatch" if run_id is not None else ("ambiguous" if candidates else "not_found")
        return state(identity, name, message=warning, candidates=candidates)
    status = run.get("status")
    if status in ("queued", "pending", "requested", "waiting", "in_progress"):
        return state(identity, status, run=run, message="Waiting for build completion; source SHA not yet verified.")
    if status != "completed":
        return state(identity, "unknown", run=run, message="Unrecognized GitHub Actions run status.")

    artifacts = get_artifacts(api, identity, run)
    unique = all(len(items) <= 1 for items in artifacts.values())
    if not unique:
        return state(identity, "ambiguous", run=run, message="Duplicate artifact names; refusing to guess.")
    identity_artifact = artifacts["identity"]
    verified = bool(identity_artifact)
    log_artifacts = artifacts["logs"]
    if run.get("conclusion") != "success":
        return state(identity, "failed" if run.get("conclusion") == "failure" else "cancelled",
                     run=run, verified=verified, artifacts=log_artifacts + identity_artifact,
                     message=f"Workflow finished with {run.get('conclusion')}; no app can be installed.")
    if not identity_artifact or not artifacts["app"]:
        return state(identity, "artifact_missing", run=run,
                     message="Build succeeded, but matching SHA-bound identity/app artifacts are missing or expired.")
    return state(identity, "success", run=run, verified=True,
                 artifacts=artifacts["app"] + log_artifacts + identity_artifact,
                 message="Exact run and request verified; source SHA bound by the identity artifact.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--source-sha", required=True, help="full 40-character SHA")
    parser.add_argument("--run-id", type=int, help="strongest binding; use when known")
    args = parser.parse_args(argv)
    try:
        identity = Identity(args.repo, args.request_id, args.source_sha)
        response = resolve(GitHubAPI(os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")), identity, args.run_id)
    except (ValueError, urllib.error.URLError, TimeoutError) as error:
        print(json.dumps({"state": "error", "message": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0 if response["state"] in ("success", "queued", "in_progress", "requested", "pending", "waiting") else 1


if __name__ == "__main__":
    raise SystemExit(main())
