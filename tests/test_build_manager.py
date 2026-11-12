"""Offline acceptance tests for artifact provenance and concurrent requests."""

import importlib.util
from pathlib import Path
import unittest

FILE = Path(__file__).resolve().parents[1] / "native-build" / "build_manager.py"
spec = importlib.util.spec_from_file_location("build_manager", FILE)
import sys
manager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = manager
spec.loader.exec_module(manager)

REPO = "HappyMistake64/Xcode-for-iphone"
SHA_A = "a" * 40
SHA_B = "b" * 40
UUID_A = "a7612db1-a3fc-4e61-963c-cc11fedf4d20"
UUID_B = "b8612db1-a3fc-4e61-963c-cc11fedf4d20"
UUID_C = "c9612db1-a3fc-4e61-963c-cc11fedf4d20"
SHA_C = "c" * 40


def run(identity, id=11, status="completed", conclusion="success", **overrides):
    return {
        "id": id, "display_title": identity.title, "event": "workflow_dispatch",
        "path": ".github/workflows/pocket-ios.yml@refs/heads/main",
        "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
        "status": status, "conclusion": conclusion,
        "html_url": f"https://github.com/{REPO}/actions/runs/{id}", **overrides,
    }


def artifact(identity, kind, id, *, source=None):
    return {"id": id, "name": identity.artifact_name(kind) if source is None else
            f"pocket-ios-{kind}-{identity.request_id}-{source}",
            "expired": False, "workflow_run": {"id": 11}, "size_in_bytes": 200}


class FakeAPI:
    def __init__(self, runs, artifacts=None):
        self.runs = runs
        self.artifacts = artifacts or []
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if "/artifacts?" in path:
            return {"artifacts": self.artifacts}
        if "/actions/runs/" in path:
            rid = int(path.rsplit("/", 1)[-1])
            return next(r for r in self.runs if r["id"] == rid)
        if "page=1" in path:
            return {"workflow_runs": self.runs}
        return {"workflow_runs": []}


class BuildManagerTests(unittest.TestCase):
    def setUp(self):
        self.a = manager.Identity(REPO, UUID_A, SHA_A)
        self.b = manager.Identity(REPO, UUID_B, SHA_B)

    def test_success_returns_only_bound_artifacts(self):
        api = FakeAPI([run(self.a)], [artifact(self.a, "app", 123), artifact(self.a, "logs", 124),
                                      artifact(self.a, "identity", 125), artifact(self.b, "app", 333)])
        value = manager.resolve(api, self.a)
        self.assertEqual(value["state"], "success")
        self.assertTrue(value["source_verified"])
        self.assertEqual([a["id"] for a in value["artifacts"]], [123, 124, 125])

    def test_parallel_projects_never_cross(self):
        for identity in (self.a, self.b):
            api = FakeAPI([run(self.a, 11), run(self.b, 12)],
                          [artifact(self.a, "app", 123), artifact(self.a, "identity", 125)])
            result = manager.resolve(api, identity)
            self.assertEqual(result["run_id"], 11 if identity is self.a else 12)
            if identity is self.a:
                self.assertEqual(result["state"], "success")
            else:
                self.assertEqual(result["state"], "artifact_missing")
                self.assertFalse(result["artifacts"])

    def test_three_concurrent_requests_remain_isolated(self):
        c = manager.Identity(REPO, UUID_C, SHA_C)
        identities = (self.a, self.b, c)
        runs = [run(item, id=11 + i, status="in_progress") for i, item in enumerate(identities)]
        for i, item in enumerate(identities):
            api = FakeAPI(runs)
            result = manager.resolve(api, item)
            self.assertEqual(result["run_id"], 11 + i)
            self.assertEqual(result["request_id"], item.request_id)
            self.assertEqual(result["source_sha"], item.source_sha)
            self.assertFalse(result["source_verified"])

    def test_same_uuid_multiple_runs_refuses_guess(self):
        api = FakeAPI([run(self.a, 11), run(self.a, 12)])
        result = manager.resolve(api, self.a)
        self.assertEqual(result["state"], "ambiguous")
        self.assertEqual(result["candidate_run_ids"], [11, 12])

    def test_explicit_wrong_run_is_rejected(self):
        api = FakeAPI([run(self.b, 12)])
        result = manager.resolve(api, self.a, run_id=12)
        self.assertEqual(result["state"], "identity_mismatch")
        self.assertFalse(result["artifacts"])

    def test_legacy_app_and_logs_are_accepted_only_with_sha_identity(self):
        legacy = [artifact(self.a, "app", 123), artifact(self.a, "logs", 124)]
        self.assertEqual(manager.resolve(FakeAPI([run(self.a)], legacy), self.a)["state"], "artifact_missing")
        legacy.append(artifact(self.a, "identity", 125))
        outcome = manager.resolve(FakeAPI([run(self.a)], legacy), self.a)
        self.assertEqual(outcome["state"], "success")
        self.assertTrue(outcome["source_verified"])
        self.assertEqual(outcome["artifacts"][0]["name"], f"pocket-ios-app-{UUID_A}")

    def test_wrong_source_sha_cannot_return_app(self):
        api = FakeAPI([run(self.a)], [artifact(self.a, "identity", 125, source=SHA_B),
                                      artifact(self.a, "app", 123, source=SHA_B)])
        result = manager.resolve(api, self.a)
        self.assertEqual(result["state"], "artifact_missing")
        self.assertFalse(result["source_verified"])

    def test_in_progress_never_claims_verified_source(self):
        result = manager.resolve(FakeAPI([run(self.a, status="in_progress")]), self.a)
        self.assertEqual(result["state"], "in_progress")
        self.assertFalse(result["source_verified"])
        self.assertEqual(result["artifacts"], [])

    def test_failed_run_only_returns_matching_logs(self):
        api = FakeAPI([run(self.a, conclusion="failure")], [artifact(self.a, "logs", 124),
                                                       artifact(self.a, "identity", 125), artifact(self.b, "app", 333)])
        result = manager.resolve(api, self.a)
        self.assertEqual(result["state"], "failed")
        self.assertEqual([a["kind"] for a in result["artifacts"]], ["logs", "identity"])

    def test_workflow_path_and_event_are_strict(self):
        for modification in ({"event": "push"}, {"path": ".github/workflows/other.yml"},
                             {"repository": {"full_name": "other/repo"}}):
            result = manager.resolve(FakeAPI([run(self.a, **modification)]), self.a, 11)
            self.assertEqual(result["state"], "identity_mismatch")

    def test_expired_or_duplicate_artifacts_fail_closed(self):
        initial = [artifact(self.a, "app", 123), artifact(self.a, "identity", 125)]
        duplicate = [*initial, artifact(self.a, "app", 126)]
        self.assertEqual(manager.resolve(FakeAPI([run(self.a)], duplicate), self.a)["state"], "ambiguous")
        initial[0]["expired"] = True
        self.assertEqual(manager.resolve(FakeAPI([run(self.a)], initial), self.a)["state"], "artifact_missing")

    def test_noncanonical_identity_is_rejected(self):
        for req, sha in ((UUID_A.upper(), SHA_A), (UUID_A, SHA_A.upper()), (UUID_A, "abc")):
            with self.assertRaises(ValueError):
                manager.Identity(REPO, req, sha)

    def test_unknown_status(self):
        self.assertEqual(manager.resolve(FakeAPI([run(self.a, status="weird")]), self.a)["state"], "unknown")

    def test_running_will_not_download_artifacts(self):
        api = FakeAPI([run(self.a, status="queued")])
        manager.resolve(api, self.a)
        self.assertFalse(any("/artifacts?" in p for p in api.calls))


if __name__ == "__main__":
    unittest.main()
