# Xcode Pocket — reliable build identity (v1)

This contract addresses the "wrong GitHub Actions run was selected" error. It covers only the **Pocket iOS build** workflow; other workflows have separate artifacts and must not be treated as equivalent.

## Contract

Every build request has one canonical lowercase UUID (**request_id**), its immutable **source_sha** (full lowercase 40-character Git commit), **project_path = pocket-builds/<request_id>.json**, and optionally a GitHub Actions **run_id**. Store these fields in the editor before dispatch and retain them across a page reload. Do not use "latest successful run" as a fallback.

The workflow maintains its existing display title **Pocket iOS <request_id>** for compatibility with the editor. It checks out source_sha, verifies the actual checkout HEAD, and records **artifacts/build-identity.json** with workflow_run_id, workflow_attempt, request_id, source_sha and project_path.

It exports:

- **pocket-ios-identity-<request_id>-<source_sha>** (new immutable SHA binding)
- **pocket-ios-app-<request_id>** (legacy-compatible name; only on successful compilation)
- **pocket-ios-logs-<request_id>** (legacy-compatible name; when logs are available)

The app artifact preserves the existing artifact name for compatibility with the deployed web editor. It contains an **unsigned .app.zip** and an identity JSON. Neither is a signed installable IPA.

## Resolve a build

The Python 3.12 stdlib-only resolver reads GitHub Actions REST API. It never executes a build, stores a token, modifies a repository, or signs an app. Example:

~~~sh
GH_TOKEN=... python3 native-build/build_manager.py \
  --repo HappyMistake64/Xcode-for-iphone \
  --request-id 'a7612db1-a3fc-4e61-963c-cc11fedf4d20' \
  --source-sha 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
~~~

Substitute the actual UUID/SHA. For private repositories provide a token in GH_TOKEN or GITHUB_TOKEN; public reads may work anonymously at lower rate limits. Once you know the exact run ID, pass **--run-id ID** to avoid ambiguity.

The JSON result includes **state, run_id, run_url, source_verified, artifacts, candidate_run_ids**. Only a **success** state with **source_verified: true** is allowed to show app downloads. States **not_found, queued, in_progress, ambiguous, identity_mismatch, artifact_missing** must never show results belonging to a different request. A **failed** run returns only matching logs, if available. Artifact download URLs are GitHub API URLs and may require authentication.

The Python resolver validates run metadata and requires a SHA-bound **identity artifact in the same run** before trusting legacy-named app artifacts; it does **not** validate ZIP contents itself. Before trusting a downloaded artifact, inspect **build-identity.json** in that downloaded artifact ZIP and compare request_id, source_sha, and workflow_run_id with the saved request and selected run. This final check belongs in the web backend at download time.

## Integrating the web editor

The new standalone mobile editor in web/ implements this request-persistence and GitHub API workflow. It is **not** the earlier externally deployed ChatGPT Sites editor; that original site's source is still not in this repository. GitHub Pages must be enabled and the new client tested on a real iPhone before claiming that the interactive browser workflow is complete.

1. Persist request_id, source_sha, project_path and run_id (when known) as an immutable record **before dispatch**.
2. After dispatch, look up only the exact Pocket iOS request title and workflow. If multiple runs share an ID, show ambiguity rather than guessing or selecting the newest.
3. Poll status using the saved run ID once known. The workflow's run head_sha may differ from source_sha: GitHub associates workflow_dispatch with a ref, while the workflow checks out the exact requested source commit.
4. Show app downloads only when matching artifacts and the identity are present, and validate the embedded manifest before delivering them.
5. Persist this state across page reloads; delayed GitHub indexing must not trigger a duplicate dispatch.

Workflow concurrency is scoped by request ID so independent projects can build concurrently, while repeated dispatches of the same UUID are serialized.

## Automated testing

~~~sh
python3 -m unittest discover -s tests -v
~~~

The offline suite covers concurrent requests, wrong source SHA, mismatched runs, duplicate IDs, duplicate artifacts, expired artifacts and failure states without GitHub network access. The new CI job repeats tests for pushes and PRs. A full end-to-end test still requires deployment of web editor changes and real build/run/artifact verification after browser refresh.
