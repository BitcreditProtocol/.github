#!/usr/bin/env python3
"""Offline candidate/run/artifact fixtures; producer dispatches never reach GitHub."""

import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

spec = importlib.util.spec_from_file_location("candidate", Path(__file__).with_name("nightly-candidate.py"))
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)
ENV = {"ORG": "ExampleOrg", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
       "GITHUB_SHA": "a" * 40, "GITHUB_ACTOR": "operator", "GH_READ_TOKEN": "read-fixture",
       "GH_WRITE_TOKEN": "write-fixture", "GITHUB_TOKEN": "own-fixture"}


class GitHub:
    def __init__(self, cfg):
        self.cfg, self.calls = cfg, []
        self.heads = {r: f"{i:x}" * 40 for i, r in enumerate((*candidate.MEMBERS, *candidate.TESTS), 1)}
        self.run_list = {r: [] for r in candidate.IMAGES}
        self.artifact_list, self.archives, self.ci_sha = {}, {}, {}
        self.next_id = 1000
        self.lost = None
        self.accept_dispatch = True
        self.after_dispatch = None
        self.bad_read = None
        self.jobs, self.job_total = {}, None

    def own_run(self):
        return dict(id=int(self.cfg["run"]), run_attempt=self.cfg["attempt"], head_sha=self.cfg["sha"],
                    head_branch="master", event="workflow_dispatch")

    def artifact(self, repo, run, name, document):
        self.next_id += 1
        ident = self.next_id
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("metadata.json", json.dumps(document) + "\n")
        self.archives[ident] = raw.getvalue()
        now = dt.datetime.now(dt.timezone.utc)
        value = dict(id=ident, name=name, expired=False,
                     created_at=now.isoformat().replace("+00:00", "Z"),
                     expires_at=(now + dt.timedelta(days=90)).isoformat().replace("+00:00", "Z"),
                     workflow_run={"id": run["id"], "head_sha": run["head_sha"]},
                     digest="sha256:" + hashlib.sha256(raw.getvalue()).hexdigest())
        self.artifact_list.setdefault((repo, run["id"]), []).append(value)
        return value

    def plan_artifact(self, plan):
        return self.artifact(".github", self.own_run(), candidate.ARTIFACT, plan)

    def successful(self, repo, *, owner="", attempt=1, complete=True):
        self.next_id += 1
        run = dict(id=self.next_id, run_attempt=attempt, head_sha=self.heads[repo], head_branch="master",
                   event="workflow_dispatch" if owner else "push", status="completed", conclusion="success",
                   path=".github/workflows/nightly.yml",
                   display_title=candidate.title(owner, self.heads[repo]) if owner else "Merge source")
        self.run_list[repo].append(run)
        names = candidate.IMAGES[repo] if complete else candidate.IMAGES[repo][:-1]
        for name in names:
            self.image_artifact(repo, run, name, owner=owner)
        return run

    def image_artifact(self, repo, run, name, *, owner=""):
        attempt = run["run_attempt"]
        refs = {"ghcr": f"ghcr.io/{self.cfg['org'].lower()}/{name}@sha256:" + "d" * 64}
        if repo != "wildcat-dashboard-ui":
            refs["gar"] = f"{candidate.GAR}/{name}@sha256:" + "e" * 64
        record = dict(schema=1, repository=f"{self.cfg['org']}/{repo}", sha=run["head_sha"],
                      run_id=str(run["id"]), run_attempt=attempt, candidate_run_id=owner, image=name,
                      source_tag=f"source-{run['head_sha']}-{run['id']}-{attempt}", references=refs)
        return self.artifact(repo, run, f"nightly-image-{name}-{attempt}", record)

    def replace_record(self, artifact, mutate):
        with zipfile.ZipFile(io.BytesIO(self.archives[artifact["id"]])) as archive:
            record = json.loads(archive.read("metadata.json"))
        mutate(record)
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("metadata.json", json.dumps(record))
        self.archives[artifact["id"]] = raw.getvalue()
        artifact["digest"] = "sha256:" + hashlib.sha256(raw.getvalue()).hexdigest()

    def api(self, cfg, path, method="GET", body=None, *, own=False, raw=False):
        self.calls.append((method, path, copy.deepcopy(body), own))
        if self.bad_read and method == "GET" and self.bad_read in path:
            raise candidate.CandidateError("Simulated API read failure")
        bits, args = urlsplit(path), parse_qs(urlsplit(path).query)
        parts = bits.path.split("/")
        repo = parts[2]
        tail = "/".join(parts[3:])
        if method == "POST":
            assert tail == "actions/workflows/nightly.yml/dispatches" and not cfg["dry"]
            assert body == {"ref": "master", "inputs": {"expected_sha": self.heads[repo], "candidate_run_id": cfg["run"]}}
            if not self.accept_dispatch:
                raise candidate.CandidateError("Lost response; acceptance unknown")
            run = self.successful(repo, owner=cfg["run"])
            if self.after_dispatch:
                self.after_dispatch(repo, run)
            if self.lost == repo:
                self.lost = None
                raise candidate.CandidateError("Lost response after accepted dispatch")
            return {"workflow_run_id": run["id"]}
        assert method == "GET", (method, path)
        if tail == "commits/master":
            return {"sha": self.heads[repo]}
        if tail.endswith("/check-suites"):
            value = tail.split("/")[1]
            self.ci_sha[repo] = value
            return {"total_count": 1, "check_suites": [{"id": 7, "head_sha": value, "head_branch": "master"}]}
        if tail.startswith("check-suites/"):
            return {"total_count": 1, "check_runs": [{"id": 8, "head_sha": self.ci_sha[repo], "name": "Tests",
                     "app": {"id": 1}, "status": "completed", "conclusion": "success"}]}
        if tail.startswith("actions/artifacts/"):
            return self.archives[int(parts[-2])]
        if tail.endswith("/artifacts"):
            values = self.artifact_list.get((repo, int(parts[-2])), [])
            key = "artifacts"
        elif tail.endswith("/jobs"):
            assert own and repo == ".github"
            values, key = self.jobs[int(parts[-2])], "jobs"
        elif tail == "actions/workflows/nightly.yml/runs":
            values = list(self.run_list[repo])
            for query, field in (("branch", "head_branch"), ("head_sha", "head_sha"), ("event", "event")):
                if query in args:
                    values = [r for r in values if r[field] == args[query][0]]
            key = "workflow_runs"
        elif tail.startswith("actions/runs/"):
            if repo == ".github":
                assert own
                return self.own_run()
            return next(r for r in self.run_list[repo] if r["id"] == int(parts[-1]))
        else:
            raise AssertionError("Unexpected network request: " + path)
        page = int(args.get("page", ["1"])[0])
        total = self.job_total if key == "jobs" and self.job_total is not None else len(values)
        return {"total_count": total, key: copy.deepcopy(values[(page - 1) * 100:page * 100])}

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, {**ENV, "GITHUB_OUTPUT": str(self.root / "outputs")}, clear=True))
        self.cfg = candidate.context()
        self.remote = GitHub(self.cfg)
        self.enterContext(patch.object(candidate, "api", side_effect=self.remote.api))
        self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("Real commands are disabled")))
        self.enterContext(patch.object(candidate, "progress"))
        self.clock = 0
        self.enterContext(patch.object(candidate.time, "monotonic", side_effect=lambda: self.clock))
        self.enterContext(patch.object(candidate.time, "sleep", side_effect=self.sleep))
        self.plan_path, self.images_path = self.root / "plan.json", self.root / "images.json"
        self.current_dispatches = dict.fromkeys(candidate.IMAGES, "false")

    def sleep(self, seconds):
        self.assertLessEqual(seconds, 30)
        self.clock += seconds

    def prepared(self):
        plan = candidate.prepare(self.cfg, self.plan_path)
        self.remote.plan_artifact(plan)
        return plan

    def complete_builds(self):
        return {r: self.remote.successful(r) for r in candidate.IMAGES}

    def collect(self, **kwargs):
        with patch.dict(os.environ, {"CURRENT_DISPATCHES": json.dumps(self.current_dispatches)}):
            return candidate.images(self.cfg, self.plan_path, self.images_path, **kwargs)

    def images_job(self, **kwargs):
        required = candidate.inspect_images(self.cfg, self.plan_path)
        if not self.cfg["dry"]:
            for repo, needed in required.items():
                if needed:
                    out = self.root / "producer-output"
                    out.write_text("")
                    with patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}):
                        candidate.dispatch(self.cfg, self.plan_path, repo)
                    self.current_dispatches[repo] = [s.split("=", 1)[1] for s in out.read_text().splitlines()
                                                     if s.startswith("require_own=")][-1]
        return self.collect(**kwargs)

    def prior_image_job(self, attempt=1, *, submitted=(), saved=False):
        names = [*candidate.DISPATCH_STEPS.values(), candidate.SAVE_IMAGES_STEP]
        started = {candidate.DISPATCH_STEPS[r] for r in submitted}
        if saved:
            started.add(candidate.SAVE_IMAGES_STEP)
        steps = [dict(number=i + 1, name=name, status="completed",
                      conclusion="success" if name in started else "skipped",
                      started_at="2026-09-09T01:00:00Z" if name in started else None)
                 for i, name in enumerate(names)]
        return dict(id=900 + attempt, run_id=int(self.cfg["run"]), run_attempt=attempt,
                    head_sha=self.cfg["sha"], name="images", status="completed", conclusion="failure", steps=steps)

    def test_prepare_captures_all_heads_before_ci_and_has_exact_schema(self):
        plan = candidate.prepare(self.cfg, self.plan_path)
        self.assertEqual(set(plan), candidate.PLAN_KEYS)
        self.assertEqual(set(plan["members"]), set(candidate.MEMBERS))
        self.assertEqual(set(plan["tests"]), set(candidate.TESTS))
        first_ci = next(i for i, call in enumerate(self.remote.calls) if "/check-suites" in call[1])
        self.assertEqual(sum("/commits/master" in c[1] for c in self.remote.calls[:first_ci]), 7)
        self.assertEqual(plan["candidate_run_id"], "123")
        self.assertIsNone(plan["previous_accepted_run_id"])
        self.assertIn("created=true", (self.root / "outputs").read_text())
        self.assertFalse(self.remote.writes)

    def test_restore_never_recaptures_moved_master_or_previous_run(self):
        plan = self.prepared()
        self.cfg.update(attempt=2, previous="999")
        self.remote.heads = dict.fromkeys(self.remote.heads, "f" * 40)
        self.remote.calls.clear()
        self.plan_path.unlink()
        self.assertEqual(candidate.prepare(self.cfg, self.plan_path), plan)
        self.assertFalse(any("/commits/master" in c[1] for c in self.remote.calls))
        self.assertIn("created=false", (self.root / "outputs").read_text())

    def test_missing_rerun_artifact_stops_even_when_local_plan_exists(self):
        candidate.prepare(self.cfg, self.plan_path)
        self.cfg["attempt"] = 2
        self.remote.calls.clear()
        with self.assertRaises(candidate.CandidateError):
            candidate.prepare(self.cfg, self.plan_path)
        self.assertFalse(any("/commits/master" in c[1] for c in self.remote.calls))

    def test_corrupted_expired_and_wrong_own_artifacts_stop(self):
        self.prepared()
        artifact = self.remote.artifact_list[".github", 123][0]
        original = copy.deepcopy(artifact)
        for mutate in (lambda a: a.update(expired=True), lambda a: a["workflow_run"].update(id=456),
                       lambda a: a.update(digest="sha256:" + "0" * 64)):
            with self.subTest(mutate=mutate):
                artifact.clear(); artifact.update(copy.deepcopy(original)); mutate(artifact)
                with self.assertRaises(candidate.CandidateError):
                    candidate.prepare(self.cfg, self.plan_path)
        artifact.clear(); artifact.update(original)
        self.remote.replace_record(artifact, lambda p: p["members"].pop("Clowder"))
        with self.assertRaises(candidate.CandidateError):
            candidate.prepare(self.cfg, self.plan_path)
        self.assertFalse(self.remote.writes)

    def test_complete_reuse_records_twelve_images_and_distinct_registries(self):
        plan = self.prepared()
        runs = self.complete_builds()
        value = self.collect()
        self.assertEqual(value["members"], plan["members"])
        self.assertEqual(len(value["images"]), 12)
        self.assertEqual(json.loads(self.images_path.read_text()), value)
        for record in value["images"]:
            repo = record["repository"].split("/")[1]
            self.assertEqual(record["source_run_id"], str(runs[repo]["id"]))
            self.assertGreater(record["artifact_id"], 0)
            self.assertEqual(set(record["references"]), {"ghcr"} if repo == "wildcat-dashboard-ui" else {"gar", "ghcr"})
        self.assertIn("complete=true", (self.root / "outputs").read_text())
        self.assertFalse(self.remote.writes)

    def test_dry_run_missing_or_partial_matrix_has_no_dispatch_or_output(self):
        self.prepared()
        self.remote.successful("Wildcat", complete=False)
        self.assertIsNone(self.collect())
        self.assertFalse(self.images_path.exists())
        self.assertFalse(self.remote.writes)
        self.assertIn("complete=false", (self.root / "outputs").read_text())

    def test_failed_jobs_rerun_combines_latest_receipt_per_image_in_one_run(self):
        self.prepared()
        self.complete_builds()
        run = self.remote.successful("Wildcat", owner=self.cfg["run"], complete=False)
        run["run_attempt"] = 2
        self.remote.image_artifact("Wildcat", run, candidate.IMAGES["Wildcat"][-1], owner=self.cfg["run"])
        # A rebuilt image supersedes its older receipt; other successful jobs keep theirs.
        self.remote.image_artifact("Wildcat", run, candidate.IMAGES["Wildcat"][0], owner=self.cfg["run"])
        self.remote.artifact_list["Wildcat", run["id"]][0]["expired"] = True
        value = self.collect()
        records = [r for r in value["images"] if r["repository"].endswith("/Wildcat")]
        self.assertEqual([r["run_attempt"] for r in records], [2, 1, 1, 1, 2])
        self.assertTrue(all(r["source_run_id"] == str(run["id"]) for r in records))
        self.assertTrue(all(r["source_tag"].endswith("-" + str(r["run_attempt"])) for r in records))
        self.assertIn("created=true", (self.root / "outputs").read_text())
        self.assertFalse(self.remote.writes)

    def test_unavailable_saved_images_stop_without_reselecting_producers(self):
        self.prepared()
        self.complete_builds()
        value = self.collect()
        artifact = self.remote.artifact(".github", self.remote.own_run(), "clowder-nightly-images", value)
        original = copy.deepcopy(artifact)
        for change in ({"expired": True}, {"digest": "sha256:" + "0" * 64},
                       {"workflow_run": {"id": 456, "head_sha": self.cfg["sha"]}}):
            with self.subTest(change=change):
                artifact.clear()
                artifact.update(copy.deepcopy(original) | change)
                self.remote.calls.clear()
                with self.assertRaises(candidate.CandidateError):
                    self.collect()
                self.assertTrue(all("/ExampleOrg/.github/" in c[1] for c in self.remote.calls))
        self.assertFalse(self.remote.writes)

    def test_newest_malformed_or_expired_receipt_never_falls_back(self):
        self.prepared()
        run = self.remote.successful("Wildcat")
        run["run_attempt"] = 2
        artifact = self.remote.image_artifact("Wildcat", run, candidate.IMAGES["Wildcat"][0])
        for mutation in (lambda a: a.update(expired=True),
                         lambda a: self.remote.replace_record(a, lambda r: r.update(source_tag="nightly"))):
            with self.subTest(mutation=mutation):
                artifact["expired"] = False
                mutation(artifact)
                with self.assertRaises(candidate.CandidateError):
                    self.collect()
                self.assertFalse(self.images_path.exists())
        self.assertFalse(self.remote.writes)

    def test_saved_images_restore_exact_bytes_without_producer_access(self):
        self.prepared()
        self.complete_builds()
        value = self.collect()
        artifact = self.remote.artifact(".github", self.remote.own_run(), "clowder-nightly-images", value)
        with zipfile.ZipFile(io.BytesIO(self.remote.archives[artifact["id"]])) as archive:
            original = archive.read("metadata.json")
        self.cfg["attempt"] = 2
        self.complete_builds()
        self.remote.heads = dict.fromkeys(self.remote.heads, "f" * 40)
        self.remote.calls.clear()
        (self.root / "outputs").write_text("")
        self.assertEqual(self.collect(), value)
        self.assertEqual(self.images_path.read_bytes(), original)
        self.assertTrue(all("/ExampleOrg/.github/" in c[1] and c[3] for c in self.remote.calls))
        self.assertIn("created=false", (self.root / "outputs").read_text())
        self.assertNotIn("created=true", (self.root / "outputs").read_text())
        self.assertFalse(self.remote.writes)

    def test_saved_images_reject_wrong_plan_partial_duplicate_and_foreign_run(self):
        self.prepared()
        self.complete_builds()
        value = self.collect()
        artifact = self.remote.artifact(".github", self.remote.own_run(), "clowder-nightly-images", value)
        original = self.remote.archives[artifact["id"]], artifact["digest"]
        changes = (lambda v: v["members"].update(Clowder="f" * 40),
                   lambda v: v["images"].pop(),
                   lambda v: v["images"].__setitem__(0, copy.deepcopy(v["images"][1])),
                   lambda v: v["images"][0].update(artifact_id=v["images"][1]["artifact_id"]),
                   lambda v: v["images"][0].update(source_run_id="456"),
                   lambda v: v["images"][0].update(run_id="456", source_run_id="456"),
                   lambda v: v.update(candidate_run_id="456"))
        for change in changes:
            with self.subTest(change=change):
                self.remote.archives[artifact["id"]], artifact["digest"] = original
                self.remote.replace_record(artifact, change)
                with self.assertRaises(candidate.CandidateError):
                    self.collect()
        self.assertFalse(self.remote.writes)

    def test_local_plan_must_match_immutable_artifact_before_dispatch(self):
        plan = self.prepared()
        plan["members"]["Clowder"] = "f" * 40
        self.plan_path.write_text(json.dumps(plan))
        self.cfg["dry"] = False
        with self.assertRaises(candidate.CandidateError):
            self.collect()
        self.assertFalse(self.remote.writes)

    def test_wrong_sha_run_attempt_source_tag_and_registry_stop(self):
        self.prepared()
        run = self.remote.successful("Wildcat")
        artifact = self.remote.artifact_list["Wildcat", run["id"]][0]
        original = self.remote.archives[artifact["id"]], artifact["digest"]
        changes = (lambda r: r.update(sha="f" * 40), lambda r: r.update(run_id="456"),
                   lambda r: r.update(run_attempt=2), lambda r: r.update(source_tag="nightly"),
                   lambda r: r["references"].update(ghcr="ghcr.io/exampleorg/bcr-wdc-core-service:nightly"))
        for change in changes:
            with self.subTest(change=change):
                self.remote.archives[artifact["id"]], artifact["digest"] = original
                self.remote.replace_record(artifact, change)
                with self.assertRaises(candidate.CandidateError):
                    self.collect()
                self.assertFalse(self.images_path.exists())
        self.assertFalse(self.remote.writes)

    def test_latest_pending_or_failed_own_run_blocks_older_success(self):
        self.prepared()
        self.complete_builds()
        run = self.remote.successful("Wildcat", owner=self.cfg["run"])
        run.update(status="in_progress", conclusion=None)
        self.assertIsNone(self.collect())
        self.assertFalse(self.remote.writes)
        run.update(status="completed", conclusion="failure")
        with self.assertRaisesRegex(candidate.CandidateError, "existing candidate run"):
            self.collect()
        self.assertFalse(self.remote.writes)

    def test_successful_own_run_with_missing_image_is_not_replaced(self):
        self.prepared()
        self.remote.successful("Wildcat", owner=self.cfg["run"], complete=False)
        self.cfg["dry"] = False
        with self.assertRaisesRegex(candidate.CandidateError, "incomplete image matrix"):
            self.collect()
        self.assertFalse(self.remote.writes)

    def test_moving_master_stops_before_any_dispatch(self):
        self.prepared()
        self.cfg["dry"] = False
        self.remote.heads["wildcat-dashboard-ui"] = "f" * 40
        with self.assertRaisesRegex(candidate.CandidateError, "master moved"):
            self.images_job()
        self.assertFalse(self.remote.writes)

    def test_dispatch_after_all_artifact_reads_and_lost_response_readback(self):
        self.prepared()
        for repo in candidate.IMAGES:
            if repo != "Clowder":
                self.remote.successful(repo)
        self.remote.lost = "Clowder"
        self.cfg["dry"] = False
        value = self.images_job()
        self.assertEqual(len(value["images"]), 12)
        self.assertEqual(len(self.remote.writes), 1)
        first_write = next(i for i, c in enumerate(self.remote.calls) if c[0] == "POST")
        for repo in ("Wildcat", "Wildcat-Auxiliary", "wildcat-dashboard-ui"):
            self.assertTrue(any(f"/{repo}/actions/artifacts/" in c[1] for c in self.remote.calls[:first_write]))
        self.images_job()
        self.assertEqual(len(self.remote.writes), 1)

    def test_unknown_dispatch_is_not_repeated_or_replaced_by_another_build(self):
        self.prepared()
        for repo in candidate.IMAGES:
            if repo != "Clowder":
                self.remote.successful(repo)
        self.cfg["dry"] = False
        self.remote.accept_dispatch = False
        def later_unrelated(seconds):
            self.sleep(seconds)
            self.remote.successful("Clowder")
        candidate.time.sleep.side_effect = later_unrelated
        with self.assertRaisesRegex(candidate.CandidateError, "Timed out"):
            self.images_job(timeout=40)
        self.assertEqual(len(self.remote.writes), 1)
        self.assertFalse(self.images_path.exists())

    def test_poll_deadline_stops_before_another_api_read(self):
        self.prepared()
        self.cfg["dry"] = False
        self.remote.accept_dispatch = False
        calls_at_deadline = []
        def sleep(seconds):
            self.sleep(seconds)
            if self.clock >= 40:
                calls_at_deadline.extend(self.remote.calls)
        candidate.time.sleep.side_effect = sleep
        with self.assertRaisesRegex(candidate.CandidateError, "Timed out"):
            self.collect(timeout=40)
        self.assertEqual(self.remote.calls, calls_at_deadline)
        self.assertEqual(candidate.WAIT_SECONDS, 45 * 60)

    def test_dispatch_race_with_master_is_rejected_by_native_run_id(self):
        self.prepared()
        for repo in candidate.IMAGES:
            if repo != "Clowder":
                self.remote.successful(repo)
        self.cfg["dry"] = False
        self.remote.after_dispatch = lambda repo, run: run.update(head_sha="f" * 40)
        with self.assertRaisesRegex(candidate.CandidateError, "does not match"):
            self.images_job()
        self.assertEqual(len(self.remote.writes), 1)

    def test_artifact_read_failure_prevents_other_dispatches(self):
        self.prepared()
        self.remote.successful("wildcat-dashboard-ui")
        self.cfg["dry"] = False
        self.remote.bad_read = "/wildcat-dashboard-ui/actions/artifacts/"
        with self.assertRaises(candidate.CandidateError):
            self.images_job()
        self.assertFalse(self.remote.writes)

    def test_deleted_aggregate_never_regenerates_after_a_possible_save(self):
        self.prepared()
        self.complete_builds()
        value = self.collect()
        artifact = self.remote.artifact(".github", self.remote.own_run(), candidate.IMAGES_ARTIFACT, value)
        self.remote.artifact_list[".github", 123].remove(artifact)
        self.cfg["attempt"] = 2
        self.remote.jobs[1] = [self.prior_image_job(saved=True)]
        self.complete_builds()
        self.cfg["dry"] = False
        for phase in (lambda: self.collect(), lambda: candidate.inspect_images(self.cfg, self.plan_path),
                      lambda: candidate.dispatch(self.cfg, self.plan_path, "Clowder")):
            with self.subTest(phase=phase):
                self.remote.calls.clear()
                (self.root / "outputs").write_text("")
                with self.assertRaisesRegex(candidate.CandidateError, "may have been saved"):
                    phase()
                self.assertTrue(all("/ExampleOrg/.github/" in c[1] for c in self.remote.calls))
                self.assertNotIn("created=true", (self.root / "outputs").read_text())
        self.assertFalse(self.remote.writes)

    def test_missing_aggregate_allows_only_proven_never_saved_collection(self):
        self.prepared()
        self.complete_builds()
        self.cfg["attempt"] = 2
        for jobs in ([], [self.prior_image_job()], [self.prior_image_job() | {"conclusion": "skipped", "steps": []}]):
            with self.subTest(jobs=jobs):
                self.remote.jobs[1] = jobs
                self.assertEqual(len(self.collect()["images"]), 12)
        self.assertFalse(self.remote.writes)

    def test_missing_aggregate_rejects_unavailable_partial_or_malformed_history(self):
        self.prepared()
        self.complete_builds()
        self.cfg["attempt"] = 2
        job = self.prior_image_job()
        for change in ({"head_sha": "f" * 40}, {"run_attempt": 3}, {"steps": []}):
            with self.subTest(change=change):
                self.remote.jobs[1] = [job | change]
                with self.assertRaises(candidate.CandidateError):
                    self.collect()
        self.remote.jobs[1] = [job]
        self.remote.job_total = 2
        with self.assertRaisesRegex(candidate.CandidateError, "Incomplete"):
            self.collect()
        self.remote.job_total = None
        self.remote.bad_read = "/attempts/1/jobs"
        with self.assertRaisesRegex(candidate.CandidateError, "API read"):
            self.collect()
        self.assertFalse(self.remote.writes)

    def test_uncertain_producer_post_is_not_repeated_on_a_native_rerun(self):
        self.prepared()
        for repo in candidate.IMAGES:
            if repo != "Clowder":
                self.remote.successful(repo)
        self.cfg["dry"] = False
        self.remote.accept_dispatch = False
        with self.assertRaisesRegex(candidate.CandidateError, "Timed out"):
            self.images_job(timeout=40)
        self.assertEqual(len(self.remote.writes), 1)
        self.cfg["attempt"] = 2
        self.remote.jobs[1] = [self.prior_image_job(submitted=("Clowder",))]
        self.current_dispatches = dict.fromkeys(candidate.IMAGES, "false")
        self.remote.successful("Clowder")  # An unrelated exact-SHA build cannot replace an uncertain dispatch.
        self.assertFalse(candidate.inspect_images(self.cfg, self.plan_path)["Clowder"])
        with self.assertRaisesRegex(candidate.CandidateError, "prior dispatch may have started"):
            candidate.dispatch(self.cfg, self.plan_path, "Clowder")
        with self.assertRaisesRegex(candidate.CandidateError, "Timed out"):
            self.images_job(timeout=40)
        self.assertEqual(len(self.remote.writes), 1)

    def test_partial_attempt_keeps_existing_producer_and_dispatches_untouched_one(self):
        self.prepared()
        own = self.remote.successful("Wildcat", owner=self.cfg["run"])
        for repo in ("Wildcat-Auxiliary", "wildcat-dashboard-ui"):
            self.remote.successful(repo)
        self.cfg.update(attempt=2, dry=False)
        self.remote.jobs[1] = [self.prior_image_job(submitted=("Wildcat",))]
        value = self.images_job()
        self.assertEqual(len(value["images"]), 12)
        self.assertEqual(len(self.remote.writes), 1)
        self.assertIn("/Clowder/", self.remote.writes[0][1])
        self.assertTrue(all(r["run_id"] == str(own["id"]) for r in value["images"] if r["repository"].endswith("/Wildcat")))

    def test_unknown_producer_does_not_prevent_later_unattempted_producer(self):
        self.prepared()
        for repo in ("Wildcat-Auxiliary", "wildcat-dashboard-ui"):
            self.remote.successful(repo)
        self.cfg.update(attempt=2, dry=False)
        self.remote.jobs[1] = [self.prior_image_job(submitted=("Wildcat",))]
        with self.assertRaisesRegex(candidate.CandidateError, "Timed out"):
            self.images_job(timeout=40)
        self.assertEqual(len(self.remote.writes), 1)
        self.assertIn("/Clowder/", self.remote.writes[0][1])

    def test_every_prior_attempt_and_job_page_remains_authoritative(self):
        self.prepared()
        self.cfg.update(attempt=3, dry=False)
        self.remote.jobs[1] = [self.prior_image_job(submitted=("Clowder",))]
        self.remote.jobs[2] = [self.prior_image_job(attempt=2)]
        with self.assertRaisesRegex(candidate.CandidateError, "prior dispatch may have started"):
            candidate.dispatch(self.cfg, self.plan_path, "Clowder")
        others = [self.prior_image_job() | {"id": 2000 + n, "name": f"other-{n}"} for n in range(100)]
        self.remote.jobs[1] = [*others, self.prior_image_job(saved=True)]
        self.remote.calls.clear()
        with self.assertRaisesRegex(candidate.CandidateError, "may have been saved"):
            self.collect()
        self.assertEqual(len([c for c in self.remote.calls if "/attempts/1/jobs?" in c[1]]), 2)
        self.assertFalse(self.remote.writes)

    def test_complete_build_appearing_after_inspection_is_reused_without_post(self):
        self.prepared()
        self.cfg["dry"] = False
        self.assertTrue(candidate.inspect_images(self.cfg, self.plan_path)["Clowder"])
        runs = self.complete_builds()
        (self.root / "outputs").write_text("")
        self.assertEqual(candidate.dispatch(self.cfg, self.plan_path, "Clowder"), str(runs["Clowder"]["id"]))
        self.assertEqual((self.root / "outputs").read_text().splitlines()[-1], "require_own=false")
        self.assertEqual(len(self.collect()["images"]), 12)
        self.assertFalse(self.remote.writes)

    def test_collection_requires_complete_current_step_outcomes(self):
        self.prepared()
        self.complete_builds()
        for value in (None, {}, {**self.current_dispatches, "Wildcat": True},
                      {**self.current_dispatches, "other": "false"}):
            with self.subTest(value=value), patch.dict(os.environ, {"CURRENT_DISPATCHES": json.dumps(value)}):
                with self.assertRaisesRegex(candidate.CandidateError, "CURRENT_DISPATCHES"):
                    candidate.images(self.cfg, self.plan_path, self.images_path)
        self.assertFalse(self.remote.writes)

    def test_inspection_and_final_collection_never_dispatch(self):
        self.prepared()
        self.cfg["dry"] = False
        self.assertTrue(all(candidate.inspect_images(self.cfg, self.plan_path).values()))
        with self.assertRaisesRegex(candidate.CandidateError, "Timed out"):
            self.collect(timeout=20)
        self.assertFalse(self.remote.writes)
        self.cfg["dry"] = True
        with self.assertRaises(candidate.CandidateError):
            candidate.dispatch(self.cfg, self.plan_path, "Wildcat")
        self.assertFalse(self.remote.writes)

    def test_plan_previous_identity_and_schema_are_validated(self):
        for change in ({"PREVIOUS_ACCEPTED_RUN_ID": "bad"}, {"PREVIOUS_ACCEPTED_RUN_ID": "123"},
                       {"GITHUB_SHA": "short"}, {"DRY_RUN": "yes"}):
            with self.subTest(change=change), patch.dict(os.environ, change), self.assertRaises(candidate.CandidateError):
                candidate.context()
        with patch.dict(os.environ, {"PREVIOUS_ACCEPTED_RUN_ID": "99"}):
            cfg = candidate.context()
            self.assertEqual(candidate.prepare(cfg, self.plan_path)["previous_accepted_run_id"], "99")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.cfg = candidate.context()
        self.enterContext(patch.object(candidate, "progress"))

    def test_dry_run_denies_dispatch_before_invoking_gh(self):
        with patch.object(subprocess, "run") as command:
            with self.assertRaises(candidate.CandidateError):
                candidate.api(self.cfg, "repos/ExampleOrg/Clowder/actions/workflows/nightly.yml/dispatches", "POST",
                              {"ref": "master", "inputs": {"expected_sha": "a" * 40, "candidate_run_id": "123"}})
            command.assert_not_called()
            self.cfg.update(dry=False, read_only=True)
            with self.assertRaises(candidate.CandidateError):
                candidate.api(self.cfg, "repos/ExampleOrg/Clowder/actions/workflows/nightly.yml/dispatches", "POST",
                              {"ref": "master", "inputs": {"expected_sha": "a" * 40, "candidate_run_id": "123"}})
            command.assert_not_called()

    def test_tokens_api_version_and_write_scope(self):
        self.cfg["dry"] = False
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"{}", b"")) as command:
            for path, own, expected in (("repos/ExampleOrg/Clowder/commits/master", False, "read-fixture"),
                                        ("repos/ExampleOrg/.github/actions/runs/123", True, "own-fixture")):
                candidate.api(self.cfg, path, own=own)
                self.assertEqual(command.call_args.kwargs["env"]["GH_TOKEN"], expected)
                self.assertNotIn("GH_WRITE_TOKEN", command.call_args.kwargs["env"])
            path = "repos/ExampleOrg/Clowder/actions/workflows/nightly.yml/dispatches"
            candidate.api(self.cfg, path, "POST", {"ref": "master", "inputs": {"expected_sha": "a" * 40, "candidate_run_id": "123"}})
            self.assertEqual(command.call_args.kwargs["env"]["GH_TOKEN"], "write-fixture")
            self.assertIn("X-GitHub-Api-Version: 2026-03-10", command.call_args.args[0])
            for path in ("repos/ExampleOrg/Clowder/git/refs", "repos/ExampleOrg/Governance/actions/workflows/nightly.yml/dispatches"):
                with self.assertRaises(candidate.CandidateError):
                    candidate.api(self.cfg, path, "POST", {})

    def test_http_and_malformed_response_are_not_absence(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status), patch.object(subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 1, b"{}", f"HTTP {status}".encode())):
                with self.assertRaises(candidate.CandidateError):
                    candidate.api(self.cfg, "repos/ExampleOrg/Clowder/commits/master")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"broken", b"")):
            with self.assertRaises(candidate.CandidateError):
                candidate.api(self.cfg, "repos/ExampleOrg/Clowder/commits/master")

    def test_api_deadline_prevents_reads_and_limits_request_timeout(self):
        with patch.object(candidate.time, "monotonic", return_value=10), patch.object(subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, b"{}", b"")) as command:
            self.cfg["deadline"] = 10
            with self.assertRaisesRegex(candidate.CandidateError, "Timed out"):
                candidate.api(self.cfg, "repos/ExampleOrg/Clowder/commits/master")
            command.assert_not_called()
            self.cfg["deadline"] = 17
            candidate.api(self.cfg, "repos/ExampleOrg/Clowder/commits/master")
            self.assertEqual(command.call_args.kwargs["timeout"], 7)


if __name__ == "__main__":
    unittest.main()
