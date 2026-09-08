#!/usr/bin/env python3
"""Offline release-train regression checks: python3 .github/scripts/test_release_train.py."""

import copy
import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch
from urllib.parse import parse_qs, urlsplit


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / "workflows/release-train.yml").read_text()
ENV = {
    "ORG": "ExampleOrg", "PRODUCT": "1.2.3", "DRY_RUN": "false",
    "GITHUB_ACTOR": "tester", "GITHUB_ACTOR_ID": "42", "GITHUB_RUN_ID": "123",
    "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REF": "refs/heads/master", "GITHUB_OUTPUT": "/mock/output",
    "GH_TOKEN": "mock-read", "GH_WRITE_TOKEN": "mock-write",
    "GH_ARTIFACT_TOKEN": "mock-artifacts", "PLAN_ARTIFACT_ID": "456",
}
with patch.dict(os.environ, ENV, clear=True):
    spec = importlib.util.spec_from_file_location("release_train", ROOT / "scripts/release-train.py")
    train = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(train)
REAL_API = train.api


class ReleaseTrainTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.enterContext(patch.object(train, "DRY_RUN", False))
        self.api = self.enterContext(patch.object(train, "api", side_effect=AssertionError("unexpected API call")))
        self.command = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("external command forbidden")))
        self.sleep = self.enterContext(patch.object(train.time, "sleep"))
        self.stream = self.enterContext(patch("builtins.open", mock_open()))
        self.write_text = self.enterContext(patch.object(Path, "write_text"))
        self.write_bytes = self.enterContext(patch.object(Path, "write_bytes"))
        temporary = self.enterContext(patch.object(train.tempfile, "TemporaryDirectory"))
        temporary.return_value.__enter__.return_value = "/mock/download"
        self.plan = {
            "schema_version": 1, "repository": "ExampleOrg/.github", "run_id": "123",
            "tag": "v1.2.3-2026-09-08",
            "heads": {repo: str(i) * 40 for i, repo in enumerate(train.MEMBERS, 1)},
            "previous_tag": "v1.2.2-2026-09-07",
            "tagger": {"name": "tester", "email": "42+tester@users.noreply.github.com",
                       "date": "2026-09-08T12:00:00Z"},
        }
        self.repo = train.MEMBERS[0]
        self.sha = self.plan["heads"][self.repo]
        self.base = f"repos/ExampleOrg/{self.repo}"
        self.ref_path = f"{self.base}/git/ref/tags/{self.plan['tag']}"
        self.tag_path = f"{self.base}/git/tags/{'a' * 40}"
        self.release_path = f"{self.base}/releases/tags/{self.plan['tag']}"
        self.tag_ref = {"object": {"type": "tag", "sha": "a" * 40}}
        self.tag_object = {"object": {"type": "commit", "sha": self.sha}}
        self.release = {"id": 987, "tag_name": self.plan["tag"]}
        self.artifact = {"id": 456, "name": train.ARTIFACT, "expired": False,
                         "workflow_run": {"id": 123}}

    def replies(self, responses):
        """Consume exact endpoint responses; unexpected reads or writes fail closed."""
        pending = {key: list(values) for key, values in responses.items()}

        def respond(path, method="GET", body=None, **kwargs):
            key = (method, path)
            self.assertTrue(pending.get(key), f"unexpected or repeated request: {key}")
            result = pending[key].pop(0)
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)

        self.api.side_effect = respond
        return pending

    def consumed(self, pending):
        self.assertFalse({key: values for key, values in pending.items() if values})

    def plan_file(self, plan=None):
        text = json.dumps(self.plan if plan is None else plan)
        self.enterContext(patch.object(Path, "stat", return_value=SimpleNamespace(st_size=len(text))))
        reader = self.enterContext(patch.object(Path, "read_text", return_value=text))
        self.enterContext(patch.object(Path, "read_bytes", return_value=text.encode()))
        return reader

    def stored_candidate(self, stored_id=456):
        self.command.side_effect = None
        self.command.return_value = subprocess.CompletedProcess([], 0)
        return {
            ("GET", "repos/ExampleOrg/.github/actions/artifacts/456"): [self.artifact],
            ("GET", "repos/ExampleOrg/.github/actions/runs/123"): [{
                "event": "workflow_dispatch", "head_branch": "master", "path": ".github/workflows/release-train.yml"}],
            ("GET", "repos/ExampleOrg/.github/actions/runs/123/artifacts?per_page=100&page=1"):
                [{"artifacts": [dict(self.artifact, id=stored_id)]}],
        }

    def orchestration(self):
        checks = self.enterContext(patch.object(train, "preflight", return_value={self.repo: "green"}))
        self.enterContext(patch.object(train, "reports", return_value=(
            "wire", "snapshot", dict.fromkeys(train.MEMBERS, "rollback"), None)))
        return checks

    def test_cut_uses_release_id_and_reads_back_the_annotated_tag(self):
        pending = self.replies({
            ("GET", self.ref_path): [None, self.tag_ref],
            ("POST", f"{self.base}/git/tags"): [{"sha": "a" * 40}],
            ("POST", f"{self.base}/git/refs"): [{}],
            ("GET", self.tag_path): [self.tag_object],
            ("GET", self.release_path): [None, self.release],
            ("POST", f"{self.base}/releases"): [{"id": 987}],
        })
        self.assertEqual(train.cut(self.repo, self.plan, "wire", "snapshot", "rollback"),
                         f"tag verified at {self.sha}; release id 987")
        self.consumed(pending)
        writes = [c for c in self.api.call_args_list if len(c.args) > 1]
        self.assertEqual(writes[0].args[2]["object"], self.sha)
        self.assertEqual(writes[0].args[2]["tagger"], self.plan["tagger"])
        self.assertEqual(writes[1].args[2], {"ref": f"refs/tags/{self.plan['tag']}", "sha": "a" * 40})
        body = writes[2].args[2]["body"]
        for sha in self.plan["heads"].values():
            self.assertIn(sha, body)
        self.assertIn("/actions/runs/123", body)

    def test_lost_write_responses_are_reread_before_a_retry(self):
        lost = subprocess.TimeoutExpired("mock gh", 60)
        pending = self.replies({
            ("GET", self.ref_path): [None, self.tag_ref, self.tag_ref],
            ("POST", f"{self.base}/git/tags"): [{"sha": "a" * 40}],
            ("POST", f"{self.base}/git/refs"): [lost],
            ("GET", self.tag_path): [self.tag_object, self.tag_object],
            ("GET", self.release_path): [None, self.release, self.release],
            ("POST", f"{self.base}/releases"): [lost],
        })
        self.assertIn("release id 987", train.cut(self.repo, self.plan, "", "", ""))
        self.consumed(pending)
        calls = self.api.call_args_list
        for path, readback in ((f"{self.base}/git/refs", self.ref_path),
                               (f"{self.base}/releases", self.release_path)):
            index = next(i for i, c in enumerate(calls) if c.args[:2] == (path, "POST"))
            self.assertEqual(calls[index + 1].args[0], readback)
        self.api.reset_mock()
        pending = self.replies({
            ("GET", self.ref_path): [self.tag_ref, self.tag_ref],
            ("GET", self.tag_path): [self.tag_object, self.tag_object],
            ("GET", self.release_path): [self.release, self.release],
        })
        self.assertIn("release id 987", train.cut(self.repo, self.plan, "", "", ""))
        self.consumed(pending)
        self.assertTrue(all(len(c.args) == 1 for c in self.api.call_args_list))

    def test_tag_resolution_requires_an_annotated_full_sha(self):
        pending = self.replies({
            ("GET", self.ref_path): [self.tag_ref],
            ("GET", self.tag_path): [{"object": {"type": "tag", "sha": "b" * 40}}],
            ("GET", f"{self.base}/git/tags/{'b' * 40}"): [self.tag_object],
        })
        self.assertEqual(train.tag_commit(self.repo, self.plan["tag"]), self.sha)
        self.consumed(pending)
        for obj in ({"type": "commit", "sha": self.sha}, {"type": "tag", "sha": "abc123"}):
            with self.subTest(object=obj):
                self.replies({("GET", self.ref_path): [{"object": obj}]})
                with self.assertRaises(train.APIError):
                    train.tag_commit(self.repo, self.plan["tag"])

    def test_cut_stops_when_durable_tag_readback_has_a_different_commit(self):
        self.replies({
            ("GET", self.ref_path): [None, self.tag_ref],
            ("POST", f"{self.base}/git/tags"): [{"sha": "a" * 40}],
            ("POST", f"{self.base}/git/refs"): [{}],
            ("GET", self.tag_path): [{"object": {"type": "commit", "sha": "f" * 40}}],
        })
        with self.assertRaisesRegex(train.APIError, "readback"):
            train.cut(self.repo, self.plan, "", "", "")
        self.assertFalse(any("releases" in c.args[0] for c in self.api.call_args_list))

    def test_gate_paginates_master_suites_and_uses_latest_check_even_if_not_green(self):
        for status, conclusion, expected, detail in (
            ("completed", "success", True, "green"),
            ("completed", "failure", False, "failing"),
            ("in_progress", None, False, "still running"),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                old = {"name": "build", "head_sha": self.sha, "app": {"id": 1},
                       "status": "completed", "conclusion": "failure" if expected else "success",
                       "started_at": "2026-09-08T10:00:00Z"}
                latest = dict(old, id=101, status=status, conclusion=conclusion,
                              started_at="2026-09-08T11:00:00Z")
                suites = [{"id": i, "head_branch": "feature", "head_sha": self.sha} for i in range(98)]
                suites += [{"id": 98, "head_branch": "master", "head_sha": "f" * 40},
                           {"id": 100, "head_branch": "master", "head_sha": self.sha}]
                prefix = f"{self.base}/commits/{self.sha}/check-suites"
                pending = self.replies({
                    ("GET", prefix + "?per_page=100&page=1"): [{"check_suites": suites}],
                    ("GET", prefix + "?per_page=100&page=2"): [{"check_suites": [
                        {"id": 101, "head_branch": "master", "head_sha": self.sha}]}],
                    ("GET", f"{self.base}/check-suites/100/check-runs?filter=all&per_page=100&page=1"):
                        [{"check_runs": [dict(old, id=i) for i in range(1, 101)]}],
                    ("GET", f"{self.base}/check-suites/100/check-runs?filter=all&per_page=100&page=2"):
                        [{"check_runs": [latest]}],
                    ("GET", f"{self.base}/check-suites/101/check-runs?filter=all&per_page=100&page=1"):
                        [{"check_runs": [dict(old, id=100), dict(old, id=1000, name="Dependabot")]}],
                })
                ok, message = train.gate(self.repo, self.sha)
                self.assertEqual(ok, expected)
                self.assertIn(detail, message)
                self.consumed(pending)

    def test_queued_check_without_timestamps_blocks_the_gate(self):
        queued = {"id": 101, "name": "build", "head_sha": self.sha, "app": {"id": 1},
                  "status": "queued", "conclusion": None, "started_at": None}
        self.replies({
            ("GET", f"{self.base}/commits/{self.sha}/check-suites?per_page=100&page=1"):
                [{"check_suites": [{"id": 1, "head_branch": "master", "head_sha": self.sha}]}],
            ("GET", f"{self.base}/check-suites/1/check-runs?filter=all&per_page=100&page=1"):
                [{"check_runs": [queued]}],
        })
        ok, detail = train.gate(self.repo, self.sha)
        self.assertFalse(ok)
        self.assertIn("still running", detail)

    def test_new_queued_check_supersedes_old_success_in_either_api_order(self):
        old = {"id": 100, "name": "build", "head_sha": self.sha, "app": {"id": 1},
               "status": "completed", "conclusion": "success", "started_at": "2026-09-08T10:00:00Z"}
        queued = dict(old, id=101, status="queued", conclusion=None, started_at=None)
        for runs in ([old, queued], [queued, old]):
            with self.subTest(ids=[run["id"] for run in runs]):
                self.replies({
                    ("GET", f"{self.base}/commits/{self.sha}/check-suites?per_page=100&page=1"):
                        [{"check_suites": [{"id": 1, "head_branch": "master", "head_sha": self.sha}]}],
                    ("GET", f"{self.base}/check-suites/1/check-runs?filter=all&per_page=100&page=1"):
                        [{"check_runs": runs}],
                })
                ok, detail = train.gate(self.repo, self.sha)
                self.assertFalse(ok)
                self.assertIn("still running", detail)

    def test_pull_request_checks_cannot_satisfy_master_gate(self):
        self.replies({("GET", f"{self.base}/commits/{self.sha}/check-suites?per_page=100&page=1"):
                      [{"check_suites": [{"id": 1, "head_branch": "feature", "head_sha": self.sha}]}]})
        self.assertFalse(train.gate(self.repo, self.sha)[0])

    def test_rollback_reports_added_changed_and_removed_sql(self):
        before = [{"type": "blob", "path": f"db/migrations/{name}.sql", "sha": sha}
                  for name, sha in (("changed", "a"), ("removed", "b"), ("same", "c"))]
        after = [{"type": "blob", "path": f"db/migrations/{name}.sql", "sha": sha}
                 for name, sha in (("changed", "d"), ("added", "e"), ("same", "c"))]
        after += [{"type": "blob", "path": "db/migrations/readme.txt", "sha": "f"}]
        self.replies({
            ("GET", f"{self.base}/git/trees/{self.plan['previous_tag']}?recursive=1"):
                [{"tree": before, "truncated": False}],
            ("GET", f"{self.base}/git/trees/{self.sha}?recursive=1"):
                [{"tree": after, "truncated": False}],
        })
        note = train.rollback_note(self.repo, self.sha, self.plan["previous_tag"])
        for name in ("added", "changed", "removed"):
            self.assertIn(f"{name}: `db/migrations/{name}.sql`", note)
        self.assertNotIn("same.sql", note)
        self.assertNotIn("readme.txt", note)
        self.replies({("GET", f"{self.base}/git/trees/{self.sha}?recursive=1"):
                      [{"tree": [], "truncated": True}]})
        with self.assertRaisesRegex(train.APIError, "incomplete"):
            train.migrations_at(self.repo, self.sha)

    def test_build_start_requires_the_exact_workflow_tag_sha_and_push_event(self):
        self.assertEqual(train.IMAGE_BUILDERS, {
            "Wildcat": "build.yml", "Clowder": "build.yml", "Wildcat-Auxiliary": "build.yml",
            "wildcat-dashboard-ui": "release.yml"})
        for changed in (None, "head_sha", "head_branch", "event"):
            with self.subTest(changed=changed):
                seen = []

                def runs(path, method="GET", body=None, **kwargs):
                    self.assertEqual(method, "GET")
                    parsed = urlsplit(path)
                    repo = parsed.path.split("/")[2]
                    self.assertEqual(parsed.path, f"repos/ExampleOrg/{repo}/actions/workflows/{train.IMAGE_BUILDERS[repo]}/runs")
                    self.assertEqual(parse_qs(parsed.query), {
                        "event": ["push"], "branch": [self.plan["tag"]],
                        "head_sha": [self.plan["heads"][repo]], "per_page": ["1"]})
                    seen.append(repo)
                    run = {"head_sha": self.plan["heads"][repo], "head_branch": self.plan["tag"],
                           "event": "push", "conclusion": None}
                    if changed:
                        run[changed] = "wrong"
                    return {"workflow_runs": [run]}

                self.api.side_effect = runs
                gaps = []
                started = train.builds_started(self.plan, gaps)
                self.assertEqual(started, [] if changed else list(train.IMAGE_BUILDERS))
                self.assertEqual(len(gaps), len(train.IMAGE_BUILDERS) if changed else 0)
                self.assertNotIn("Wildcat-deployment", seen)
                self.assertEqual(len(seen), len(train.IMAGE_BUILDERS) * (7 if changed else 1))

    def test_prepare_saves_the_five_selected_heads_before_apply(self):
        checks = self.orchestration()
        self.enterContext(patch.object(train, "head_of", side_effect=self.plan["heads"].__getitem__))
        self.enterContext(patch.object(train, "previous_train", return_value=self.plan["previous_tag"]))
        self.enterContext(patch.object(train, "tag_commit", return_value=None))
        clock = self.enterContext(patch.object(train.datetime, "datetime", wraps=datetime.datetime))
        clock.now.return_value = datetime.datetime(2026, 9, 8, 12, tzinfo=datetime.timezone.utc)
        cut = self.enterContext(patch.object(train, "cut"))
        self.assertEqual(train.main(["--prepare", "/mock/candidate.json"]), 0)
        saved = json.loads(self.write_text.call_args.args[0])
        self.assertEqual(saved, self.plan)
        checks.assert_called_once_with(self.plan)
        cut.assert_not_called()
        self.api.assert_not_called()
        self.stream().write.assert_any_call("new_plan=true\n")

    def test_resume_after_partial_failure_keeps_original_day_and_heads(self):
        self.plan_file()
        self.orchestration()
        head = self.enterContext(patch.object(train, "head_of", side_effect=AssertionError("must not resnapshot master")))
        cut = self.enterContext(patch.object(train, "cut", side_effect=["done", train.APIError("partial failure")]))
        builds = self.enterContext(patch.object(train, "builds_started", return_value=[]))
        self.replies(self.stored_candidate())
        self.assertEqual(train.main(["--apply", "/mock/candidate.json"]), 1)
        self.assertEqual([c.args[0] for c in cut.call_args_list], train.MEMBERS[:2])
        builds.assert_not_called()
        os.environ.update(RESUME_RUN_ID="123", GITHUB_RUN_ID="999", PRODUCT="9.9.9")
        clock = self.enterContext(patch.object(train.datetime, "datetime", wraps=datetime.datetime))
        clock.now.return_value = datetime.datetime(2026, 9, 10, tzinfo=datetime.timezone.utc)
        self.command.side_effect = None
        self.command.return_value = subprocess.CompletedProcess([], 0)
        pending = self.replies({
            ("GET", "repos/ExampleOrg/.github/actions/runs/123"): [{
                "event": "workflow_dispatch", "head_branch": "master", "path": ".github/workflows/release-train.yml"}],
            ("GET", "repos/ExampleOrg/.github/actions/runs/123/artifacts?per_page=100&page=1"):
                [{"artifacts": [self.artifact]}],
        })
        self.assertEqual(train.main(["--prepare", "/mock/candidate.json"]), 0)
        self.consumed(pending)
        self.assertEqual(json.loads(self.write_bytes.call_args.args[0]), self.plan)
        self.stream().write.assert_any_call("artifact_id=456\n")
        self.stream().write.assert_any_call("new_plan=false\n")
        self.assertEqual(self.command.call_args.args[0][:4], ["gh", "run", "download", "123"])
        self.assertEqual(self.command.call_args.kwargs["env"]["GH_TOKEN"], "mock-artifacts")
        cut.reset_mock(side_effect=True)
        cut.return_value = "already verified"
        self.replies(self.stored_candidate())
        self.assertEqual(train.main(["--apply", "/mock/candidate.json"]), 0)
        self.assertEqual([c.args[0] for c in cut.call_args_list], train.MEMBERS)
        self.assertTrue(all(c.args[1] == self.plan for c in cut.call_args_list))
        head.assert_not_called()
        clock.now.assert_not_called()
        self.write_text.assert_not_called()

    def test_missing_expired_ambiguous_or_invalid_resume_never_selects_new_heads(self):
        os.environ["RESUME_RUN_ID"] = "123"
        head = self.enterContext(patch.object(train, "head_of"))
        cut = self.enterContext(patch.object(train, "cut"))
        self.command.side_effect = None
        self.command.return_value = subprocess.CompletedProcess([], 0)
        for case in ("missing", "expired", "ambiguous", "invalid plan", "wrong run"):
            with self.subTest(case=case):
                artifact_list = {"missing": [], "expired": [dict(self.artifact, expired=True)],
                                 "ambiguous": [self.artifact, dict(self.artifact, id=457)]}.get(case, [self.artifact])
                plan = copy.deepcopy(self.plan)
                if case == "invalid plan":
                    plan["heads"][self.repo] = "short-sha"
                if case == "wrong run":
                    plan["run_id"] = "999"
                self.plan_file(plan)
                self.replies({
                    ("GET", "repos/ExampleOrg/.github/actions/runs/123"): [{
                        "event": "workflow_dispatch", "head_branch": "master", "path": ".github/workflows/release-train.yml"}],
                    ("GET", "repos/ExampleOrg/.github/actions/runs/123/artifacts?per_page=100&page=1"):
                        [{"artifacts": artifact_list}],
                })
                self.assertEqual(train.main(["--prepare", "/mock/candidate.json"]), 1)
        head.assert_not_called()
        cut.assert_not_called()
        self.write_bytes.assert_not_called()
        self.write_text.assert_not_called()

    def test_apply_requires_a_live_artifact_from_the_original_run(self):
        self.plan_file()
        preflight = self.enterContext(patch.object(train, "preflight"))
        cut = self.enterContext(patch.object(train, "cut"))
        for artifact_id, artifact in (("", None), ("456", dict(self.artifact, expired=True)),
                                      ("456", dict(self.artifact, name="other")),
                                      ("456", dict(self.artifact, workflow_run={"id": 999}))):
            with self.subTest(artifact_id=artifact_id, artifact=artifact):
                os.environ["PLAN_ARTIFACT_ID"] = artifact_id
                self.replies({("GET", "repos/ExampleOrg/.github/actions/artifacts/456"): [artifact]} if artifact_id else {})
                self.assertEqual(train.main(["--apply", "/mock/candidate.json"]), 1)
        preflight.assert_not_called()
        cut.assert_not_called()

    def test_apply_rejects_content_or_artifact_id_that_differs_from_canonical(self):
        preflight = self.enterContext(patch.object(train, "preflight"))
        cut = self.enterContext(patch.object(train, "cut"))
        for changed in ("content", "artifact ID"):
            with self.subTest(changed=changed):
                local = copy.deepcopy(self.plan)
                if changed == "content":
                    local["heads"][self.repo] = "f" * 40
                reader = self.plan_file()
                reader.side_effect = [json.dumps(local), json.dumps(self.plan)]
                pending = self.replies(self.stored_candidate(457 if changed == "artifact ID" else 456))
                self.stream().write.reset_mock()
                self.assertEqual(train.main(["--apply", "/mock/candidate.json"]), 1)
                self.consumed(pending)
                self.assertEqual(reader.call_count, 2)
                summary = "".join(c.args[0] for c in self.stream().write.call_args_list)
                self.assertIn("local candidate differs from its immutable original artifact", summary)
                preflight.assert_not_called()
                cut.assert_not_called()

    def test_dry_run_cannot_write_even_with_a_write_token(self):
        train.DRY_RUN = True
        for method in ("POST", "PATCH", "DELETE", "PUT"):
            with self.subTest(method=method), self.assertRaisesRegex(train.APIError, "dry-run"):
                REAL_API(f"{self.base}/releases", method, {})
        self.command.assert_not_called()
        self.plan_file()
        self.orchestration()
        train.reports.return_value = ("wire", "snapshot", dict.fromkeys(train.MEMBERS, "rollback"),
                                      ("2026-09-07", "2026-09-08", None))
        self.replies(self.stored_candidate())
        cut = self.enterContext(patch.object(train, "cut"))
        notify = self.enterContext(patch.object(train, "notify_stale_snapshot"))
        builds = self.enterContext(patch.object(train, "builds_started"))
        self.assertEqual(train.main(["--apply", "/mock/candidate.json"]), 0)
        cut.assert_not_called()
        notify.assert_not_called()
        builds.assert_not_called()

    def test_workflow_keeps_pr_jobs_and_dry_runs_out_of_write_steps(self):
        test_job, separator, train_job = WORKFLOW.partition("\n  train:\n")
        self.assertTrue(separator)
        self.assertIn("permissions: {}", test_job)
        self.assertIn("python3 .github/scripts/test_release_train.py", test_job)
        self.assertNotIn("secrets.", test_job)
        self.assertNotRegex(test_job, r"\b(?:contents|actions|issues|checks): write")
        self.assertIn("    if: github.event_name == 'workflow_dispatch'\n", train_job.partition("    steps:\n")[0])
        steps = train_job.split("\n      - ")
        for phrase in ("Require master for writes", "Grant writes only", "Reconcile the saved candidate"):
            step = next(step for step in steps if phrase in step)
            self.assertIn("if: inputs.dry_run == false", step)
        self.assertIn('test "$GITHUB_REF" = refs/heads/master', train_job)
        self.assertLess(train_job.index("Store the candidate"), train_job.index("Grant writes only"))
        self.assertLess(train_job.index("Grant writes only"), train_job.index("Reconcile the saved candidate"))


if __name__ == "__main__":
    unittest.main()
