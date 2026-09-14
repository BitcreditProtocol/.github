#!/usr/bin/env python3
"""Offline checks for saved deployment, rollback and dispatch uncertainty boundaries."""

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

spec = importlib.util.spec_from_file_location("nightly_operations_tested", Path(__file__).with_name("nightly-operations.py"))
operations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operations)
candidate = operations.candidate
ENV = {"ORG": "BitcreditProtocol", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
       "GITHUB_ACTOR": "operator", "DRY_RUN": "false", "DATA_COMPATIBLE": "true",
       "GH_READ_TOKEN": "read-fixture", "GH_WRITE_TOKEN": "write-fixture", "GITHUB_TOKEN": "own-fixture"}


class GitHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.roots, self.runs, self.attempts, self.artifacts, self.archives = {}, {}, {}, {}, {}
        self.calls, self.posts = [], []
        self.next_id = 1000
        self.head = "4" * 40
        self.lose_response = False
        self.accept = True
        self.after_dispatch = None
        self.fail_read = None
        self.jobs, self.job_total = {}, None

    def root(self, run_id="123", *, operation="deploy", sha="a" * 40, attempt=1):
        value = dict(id=int(run_id), run_attempt=attempt, head_sha=sha, head_branch="master", actor={"login": "operator"},
                     event="workflow_dispatch", path=operations.ROOT_WORKFLOWS[operation], created_at="2026-09-09T01:00:00Z")
        self.roots[int(run_id)] = value
        return value

    def artifact(self, repo, run, name, value, *, created=None):
        self.next_id += 1
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("metadata.json", json.dumps(value))
        self.archives[self.next_id] = raw.getvalue()
        moment = dt.datetime.now(dt.timezone.utc)
        metadata = dict(id=self.next_id, name=name, expired=False, workflow_run={"id": run["id"], "head_sha": run["head_sha"]},
                        created_at=created or moment.isoformat().replace("+00:00", "Z"),
                        expires_at=(moment + dt.timedelta(days=90)).isoformat().replace("+00:00", "Z"),
                        digest="sha256:" + hashlib.sha256(raw.getvalue()).hexdigest())
        self.artifacts.setdefault((repo, run["id"]), []).append(metadata)
        return metadata

    def replace(self, metadata, change):
        with zipfile.ZipFile(io.BytesIO(self.archives[metadata["id"]])) as archive:
            value = json.loads(archive.read("metadata.json"))
        change(value)
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("metadata.json", json.dumps(value))
        self.archives[metadata["id"]] = raw.getvalue()
        metadata["digest"] = "sha256:" + hashlib.sha256(raw.getvalue()).hexdigest()

    def bundle(self, run_id="123", *, sha="a" * 40):
        root = self.root(run_id, sha=sha)
        members = {r: f"{i:x}" * 40 for i, r in enumerate(candidate.MEMBERS, 1)}
        plan = dict(schema=1, candidate_run_id=run_id, workflow_sha=sha, initiator="operator", members=members,
                    tests={"E-Bill-frontend": "6" * 40, "wallet": "7" * 40}, previous_accepted_run_id=None,
                    created_at=root["created_at"])
        records = []
        for index, (repo, names) in enumerate(candidate.IMAGES.items(), 1):
            for name in names:
                refs = {"ghcr": f"ghcr.io/bitcreditprotocol/{name}@sha256:" + "d" * 64}
                if repo != "wildcat-dashboard-ui":
                    refs["gar"] = f"{candidate.GAR}/{name}@sha256:" + "e" * 64
                records.append(dict(schema=1, repository="BitcreditProtocol/" + repo, sha=members[repo],
                    run_id=str(index + 100), run_attempt=1, candidate_run_id="", image=name,
                    source_tag=f"source-{members[repo]}-{index + 100}-1", references=refs,
                    artifact_id=len(records) + 1, source_run_id=str(index + 100)))
        images = dict(schema=1, candidate_run_id=run_id, members=members, images=records)
        self.artifact(".github", root, candidate.ARTIFACT, plan)
        self.artifact(".github", root, candidate.IMAGES_ARTIFACT, images)
        return plan, images

    def recipient(self, payload, *, sha=None, attempt=1, created=None):
        self.next_id += 1
        run = dict(id=self.next_id, run_attempt=attempt, head_sha=sha or self.head, head_branch="master", event="workflow_dispatch",
                   status="completed", conclusion="success", path=operations.WORKFLOW,
                   display_title=operations.title(payload["candidate_run_id"]), created_at="2026-09-09T02:00:00Z")
        self.runs[run["id"]] = run
        self.result(payload, run, created=created)
        return run

    def result(self, payload, run, *, created=None):
        services = {record["image"]: record["references"]["ghcr"] for record in payload["images"]}
        services["postgres"] = "postgres@sha256:" + "f" * 64
        locks = {target: dict(schema=1, candidate_run_id=payload["candidate_run_id"], target=target,
                              deployment_sha=payload["plan"]["members"][operations.DEPLOYMENT], run_id=str(run["id"]),
                              run_attempt=run["run_attempt"], services=copy.deepcopy(services)) for target in operations.TARGETS}
        functional = None if payload["operation"] == "rollback" else dict(schema=1, deployment_run_id=str(run["id"]), deployment_run_attempt=run["run_attempt"], candidate_run_id=payload["candidate_run_id"],
            result="success", frontend_sha=payload["plan"]["tests"]["E-Bill-frontend"], wallet_sha=payload["plan"]["tests"]["wallet"])
        result = dict(schema=1, candidate_run_id=payload["candidate_run_id"], operation=payload["operation"],
                      payload_sha256=operations.digest(payload), run_id=str(run["id"]), run_attempt=run["run_attempt"],
                      accepted=payload["operation"] == "deploy", restored=payload["operation"] == "rollback", locks=locks,
                      functional=functional, errors=[])
        return self.artifact(operations.DEPLOYMENT, run, f"clowder-deployment-result-{run['run_attempt']}", result, created=created)

    def api(self, cfg, path, method="GET", body=None, *, own=False, raw=False):
        assert method == "GET", "Candidate's read adapter cannot mutate GitHub"
        candidate.check_deadline(cfg)
        self.calls.append((path, own))
        if self.fail_read and self.fail_read in path:
            raise operations.Error("Simulated API read failure")
        parts = urlsplit(path).path.split("/")
        repo, tail = parts[2], "/".join(parts[3:])
        args = parse_qs(urlsplit(path).query)
        if repo == ".github":
            assert own
        if tail == "commits/master":
            return {"sha": self.head}
        if tail.startswith("actions/artifacts/"):
            return self.archives[int(parts[-2])]
        if tail.endswith("/artifacts"):
            rows, key = self.artifacts.get((repo, int(parts[-2])), []), "artifacts"
        elif tail.endswith("/jobs"):
            rows, key = self.jobs[int(parts[-4]), int(parts[-2])], "jobs"
        elif tail == "actions/workflows/deploy.yml/runs":
            rows, key = list(self.runs.values()), "workflow_runs"
            if "created" in args:
                lower = candidate.timestamp(args["created"][0].removeprefix(">="))
                rows = [r for r in rows if candidate.timestamp(r["created_at"]) >= lower]
        elif tail.startswith("actions/runs/"):
            if "attempts" in parts:
                return copy.deepcopy(self.attempts[int(parts[-3]), int(parts[-1])])
            return copy.deepcopy((self.roots if repo == ".github" else self.runs)[int(parts[-1])])
        else:
            raise AssertionError("Unexpected API request: " + path)
        page = int(args.get("page", ["1"])[0])
        total = self.job_total if key == "jobs" and self.job_total is not None else len(rows)
        return {"total_count": total, key: copy.deepcopy(rows[(page - 1) * 100:page * 100])}

    def command(self, argv, **kwargs):
        assert "--method" in argv and argv[argv.index("--method") + 1] == "POST"
        assert "repos/BitcreditProtocol/Wildcat-deployment/actions/workflows/deploy.yml/dispatches" in argv
        body = json.loads(kwargs["input"])
        assert body["ref"] == "master" and body["inputs"]["environment"] == "clowder-dev"
        assert all(body["inputs"][key] == "false" for key in operations.DELETE_FLAGS)
        assert kwargs["env"]["GH_TOKEN"] == "write-fixture"
        assert not set(("GH_READ_TOKEN", "GH_WRITE_TOKEN", "GITHUB_TOKEN")) & set(kwargs["env"])
        assert 0 < kwargs["timeout"] <= 30
        self.posts.append(body)
        if not self.accept:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        run = self.recipient(json.loads(body["inputs"]["candidate_payload"]))
        if self.after_dispatch:
            self.after_dispatch(run)
        if self.lose_response:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, json.dumps({"workflow_run_id": run["id"]}).encode(), b"")


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, {**ENV, "GITHUB_OUTPUT": str(self.directory / "outputs")}, clear=True))
        self.cfg = candidate.context()
        self.github = GitHub(self.cfg)
        self.enterContext(patch.object(candidate, "api", side_effect=self.github.api))
        self.enterContext(patch.object(subprocess, "run", side_effect=self.github.command))
        self.enterContext(patch.object(candidate, "progress"))
        self.clock = 0
        self.enterContext(patch.object(operations.time, "monotonic", side_effect=lambda: self.clock))
        self.enterContext(patch.object(operations.time, "sleep", side_effect=self.sleep))
        self.plan_path, self.images_path = self.directory / "plan.json", self.directory / "images.json"
        self.intent_path, self.result_path = self.directory / "intent.json", self.directory / "result.json"
        self.request_path = self.directory / "request.json"
        self.plan, self.images = self.github.bundle()
        operations.write(self.plan_path, self.plan)
        operations.write(self.images_path, self.images)

    def sleep(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.assertLessEqual(seconds, 20)
        self.clock += seconds

    def inputs(self):
        return operations.deployment_input(self.cfg, self.plan_path, self.images_path)

    def intent(self, inputs=None):
        inputs = inputs or self.inputs()
        value = operations.prepare_intent(self.cfg, inputs, self.intent_path)
        self.github.artifact(".github", inputs[3], operations.INTENT, value)
        return value

    def deploy(self, **kwargs):
        return operations.reconcile(self.cfg, self.inputs(), self.result_path, **kwargs)

    def change_root(self, run="124", *, operation="rollback", dry=False, attempt=1):
        self.cfg.update(run=run, attempt=attempt, dry=dry)
        self.github.root(run, operation=operation, attempt=attempt)
        self.intent_path = self.directory / ("intent-" + run + ".json")

    def rollback_setup(self, *, dry=False):
        self.intent()
        accepted = self.deploy()
        self.change_root(dry=dry)
        request = operations.rollback_request(self.cfg, "123", self.request_path)
        self.github.artifact(".github", self.github.roots[124], operations.REQUEST, request)
        return accepted, request

    def test_positive_deploy_verifies_saved_composition_and_recipient(self):
        intent = self.intent()
        self.assertEqual(intent["first_submission_attempt"], 1)
        value = self.deploy()
        self.assertTrue(value["result"]["accepted"])
        self.assertEqual(value["payload_sha256"], operations.digest(self.inputs()[0]))
        self.assertEqual(set(value["result"]["locks"]), operations.TARGETS)
        self.assertEqual(json.loads(self.result_path.read_text()), value)
        self.assertEqual(len(self.github.posts), 1)
        self.deploy()
        self.assertEqual(len(self.github.posts), 1)

    def test_saved_artifacts_and_complete_images_are_required(self):
        for name in (candidate.ARTIFACT, candidate.IMAGES_ARTIFACT):
            with self.subTest(name=name):
                saved = self.github.artifacts[".github", 123]
                self.github.artifacts[".github", 123] = [a for a in saved if a["name"] != name]
                with self.assertRaises(operations.Error):
                    self.inputs()
                self.github.artifacts[".github", 123] = saved
        aggregate = next(a for a in self.github.artifacts[".github", 123] if a["name"] == candidate.IMAGES_ARTIFACT)
        self.github.replace(aggregate, lambda value: value["images"].pop())
        with self.assertRaises(operations.Error):
            self.inputs()
        self.assertFalse(self.github.posts)

    def test_expired_corrupt_or_wrong_run_artifact_is_not_absence(self):
        artifact = self.github.artifacts[".github", 123][0]
        original = copy.deepcopy(artifact)
        for change in (lambda a: a.update(expired=True), lambda a: a.update(digest="sha256:" + "0" * 64),
                       lambda a: a["workflow_run"].update(id=999), lambda a: a.update(expires_at="2026-01-01T00:00:00Z")):
            with self.subTest(change=change):
                artifact.clear(); artifact.update(copy.deepcopy(original)); change(artifact)
                with self.assertRaises(operations.Error):
                    self.inputs()
        self.assertFalse(self.github.posts)

    def test_local_source_and_image_changes_stop_before_intent(self):
        plan = copy.deepcopy(self.plan)
        plan["tests"]["wallet"] = "f" * 40
        operations.write(self.plan_path, plan)
        with self.assertRaises(operations.Error):
            self.inputs()
        operations.write(self.plan_path, self.plan)
        images = copy.deepcopy(self.images)
        images["images"][0]["references"]["ghcr"] = images["images"][0]["references"]["ghcr"].replace("d" * 64, "e" * 64)
        operations.write(self.images_path, images)
        with self.assertRaises(operations.Error):
            self.inputs()
        self.assertFalse(self.github.posts)

    def test_intent_must_be_uploaded_before_the_only_dispatch(self):
        operations.prepare_intent(self.cfg, self.inputs(), self.intent_path)
        with self.assertRaisesRegex(operations.Error, "artifact"):
            self.deploy()
        self.assertFalse(self.github.posts)

    def test_preserved_intent_allows_first_post_after_proven_pre_submission_failure(self):
        self.intent()
        artifact = next(a for a in self.github.artifacts[".github", 123] if a["name"] == operations.INTENT)
        original = copy.deepcopy(artifact), self.github.archives[artifact["id"]]
        self.cfg["attempt"] = self.github.roots[123]["run_attempt"] = 2
        self.github.jobs[123, 1] = [self.prior_job()]
        value = operations.prepare_intent(self.cfg, self.inputs(), self.intent_path)
        self.assertEqual(value["first_submission_attempt"], 1)
        result = self.deploy()
        self.assertTrue(result["result"]["accepted"])
        self.assertEqual(result["first_submission_attempt"], 1)
        self.assertEqual(result["intent_artifact_id"], artifact["id"])
        self.assertEqual((artifact, self.github.archives[artifact["id"]]), original)
        self.assertEqual(len(self.github.posts), 1)

    def test_preserved_intent_cannot_repeat_an_executed_or_uncertain_prior_step(self):
        self.intent()
        artifact = next(a for a in self.github.artifacts[".github", 123] if a["name"] == operations.INTENT)
        original = copy.deepcopy(artifact), self.github.archives[artifact["id"]]
        self.cfg["attempt"] = self.github.roots[123]["run_attempt"] = 3
        self.github.jobs[123, 2] = []
        for conclusion in ("success", "failure", "cancelled", None):
            with self.subTest(conclusion=conclusion):
                job = self.prior_job()
                job["steps"][0].update(status="in_progress" if conclusion is None else "completed",
                                       conclusion=conclusion, started_at="2026-09-09T02:00:00Z")
                self.github.jobs[123, 1] = [job]
                with self.assertRaisesRegex(operations.Error, "submission may have started"):
                    self.deploy()
                self.assertEqual((artifact, self.github.archives[artifact["id"]]), original)
        self.assertFalse(self.github.posts)

    def test_rerun_that_never_reached_intent_can_prepare_it(self):
        self.cfg["attempt"] = self.github.roots[123]["run_attempt"] = 2
        self.github.jobs[123, 1] = []
        value = self.intent()
        self.assertEqual(value["first_submission_attempt"], 2)
        self.assertTrue(self.deploy()["result"]["accepted"])

    def prior_job(self, *, operation="deploy", attempt=1):
        return dict(id=900 + attempt, run_id=int(self.cfg["run"]), run_attempt=attempt, head_sha=self.cfg["sha"],
                    name="deploy" if operation == "deploy" else "restore", status="completed", conclusion="failure",
                    steps=[dict(number=1, name="Reconcile the locked deployment and functional-test operation" if operation == "deploy"
                                else "Restore saved digests under the shared environment lock",
                                status="completed", conclusion="skipped", started_at=None)])

    def test_prior_skipped_or_unstarted_submission_allows_intent_recovery(self):
        self.cfg["attempt"] = self.github.roots[123]["run_attempt"] = 2
        skipped_job = self.prior_job() | {"conclusion": "skipped", "steps": []}
        unstarted = self.prior_job()
        unstarted["steps"][0].update(status="queued", conclusion=None)
        for job in (skipped_job, self.prior_job(), unstarted):
            with self.subTest(job=job):
                self.github.jobs[123, 1] = [job]
                self.intent_path.unlink(missing_ok=True)
                value = operations.prepare_intent(self.cfg, self.inputs(), self.intent_path)
                self.assertEqual(value["first_submission_attempt"], 2)
        self.assertFalse(self.github.posts)

    def test_deleted_intent_after_uncertain_post_cannot_be_replaced(self):
        self.intent()
        self.github.accept = False
        with self.assertRaisesRegex(operations.Error, "Timed out"):
            self.deploy(timeout=20)
        self.github.artifacts[".github", 123] = [a for a in self.github.artifacts[".github", 123] if a["name"] != operations.INTENT]
        self.cfg["attempt"] = self.github.roots[123]["run_attempt"] = 2
        job = self.prior_job()
        job["steps"][0].update(conclusion="failure", started_at="2026-09-09T02:00:00Z")
        self.github.jobs[123, 1] = [job]
        self.intent_path.unlink()
        with self.assertRaisesRegex(operations.Error, "submission may have started"):
            operations.prepare_intent(self.cfg, self.inputs(), self.intent_path)
        self.assertEqual(len(self.github.posts), 1)
        self.assertFalse(self.intent_path.exists())

    def test_missing_intent_requires_complete_unambiguous_prior_jobs(self):
        self.cfg["attempt"] = self.github.roots[123]["run_attempt"] = 2
        changes = (lambda j: j.update(run_attempt=2), lambda j: j.update(run_id=999),
                   lambda j: j.update(head_sha="b" * 40), lambda j: j.pop("name"),
                   lambda j: j.update(steps=[]), lambda j: j["steps"].append(copy.deepcopy(j["steps"][0])),
                   lambda j: j["steps"][0].update(status="in_progress", conclusion=None),
                   lambda j: j["steps"][0].update(status="queued", conclusion=None, started_at="2026-09-09T02:00:00Z"))
        for change in changes:
            with self.subTest(change=change):
                job = self.prior_job(); change(job)
                self.github.jobs[123, 1] = [job]
                with self.assertRaises(operations.Error):
                    operations.prepare_intent(self.cfg, self.inputs(), self.intent_path)
                self.assertFalse(self.intent_path.exists())
        self.github.jobs[123, 1] = [self.prior_job()]
        self.github.job_total = 2
        with self.assertRaisesRegex(operations.Error, "Incomplete"):
            operations.prepare_intent(self.cfg, self.inputs(), self.intent_path)
        self.github.job_total = None
        self.github.fail_read = "/attempts/1/jobs"
        with self.assertRaisesRegex(operations.Error, "API read"):
            operations.prepare_intent(self.cfg, self.inputs(), self.intent_path)
        self.assertFalse(self.github.posts)

    def test_every_prior_attempt_and_page_is_checked_before_replacement(self):
        self.cfg["attempt"] = self.github.roots[123]["run_attempt"] = 3
        job = self.prior_job()
        job["steps"][0].update(conclusion="success", started_at="2026-09-09T02:00:00Z")
        other_jobs = [self.prior_job() | {"id": 2000 + n, "name": f"prior-{n}"} for n in range(100)]
        self.github.jobs[123, 1] = [*other_jobs, job]
        self.github.jobs[123, 2] = []
        with self.assertRaisesRegex(operations.Error, "submission may have started"):
            operations.prepare_intent(self.cfg, self.inputs(), self.intent_path)
        self.assertEqual(len([path for path, own in self.github.calls if "/attempts/1/jobs?" in path and own]), 2)
        self.assertFalse(self.github.posts)

    def test_rollback_recovery_checks_its_exact_submission_step(self):
        self.rollback_setup()
        self.cfg["attempt"] = self.github.roots[124]["run_attempt"] = 2
        self.github.jobs[124, 1] = [self.prior_job(operation="rollback")]
        inputs = operations.rollback_input(self.cfg, self.request_path)
        value = operations.prepare_intent(self.cfg, inputs, self.intent_path)
        self.assertEqual(value["first_submission_attempt"], 2)
        self.intent_path.unlink()
        self.github.jobs[124, 1][0]["steps"][0].update(conclusion="failure", started_at="2026-09-09T02:00:00Z")
        with self.assertRaisesRegex(operations.Error, "submission may have started"):
            operations.prepare_intent(self.cfg, inputs, self.intent_path)
        self.assertEqual(len(self.github.posts), 1)

    def test_lost_response_is_read_back_without_duplicate_post(self):
        self.intent()
        self.github.lose_response = True
        self.assertTrue(self.deploy()["result"]["accepted"])
        self.assertEqual(len(self.github.posts), 1)
        self.cfg["attempt"] = self.github.roots[123]["run_attempt"] = 2
        self.assertTrue(self.deploy()["result"]["accepted"])
        self.assertEqual(len(self.github.posts), 1)

    def test_unknown_response_never_reposts_and_later_rerun_stops(self):
        self.intent()
        self.github.accept = False
        with self.assertRaisesRegex(operations.Error, "Timed out"):
            self.deploy(timeout=40)
        self.assertEqual(len(self.github.posts), 1)
        self.assertFalse(self.result_path.exists())
        self.cfg["attempt"] = self.github.roots[123]["run_attempt"] = 2
        job = self.prior_job()
        job["steps"][0].update(conclusion="failure", started_at="2026-09-09T02:00:00Z")
        self.github.jobs[123, 1] = [job]
        with self.assertRaisesRegex(operations.Error, "submission may have started"):
            self.deploy()
        self.assertEqual(len(self.github.posts), 1)

    def test_master_movement_and_wrong_dispatched_sha_are_rejected(self):
        self.intent()
        self.github.head = "b" * 40
        with self.assertRaisesRegex(operations.Error, "master moved"):
            self.deploy()
        self.assertFalse(self.github.posts)
        self.github.head = "4" * 40
        self.github.after_dispatch = lambda run: run.update(head_sha="b" * 40)
        with self.assertRaises(operations.Error):
            self.deploy()
        self.assertEqual(len(self.github.posts), 1)

    def test_existing_failed_or_partial_recipient_requires_all_jobs_rerun(self):
        self.intent()
        run = self.github.recipient(self.inputs()[0])
        run["conclusion"] = "failure"
        with self.assertRaisesRegex(operations.Error, "rerun all its jobs"):
            self.deploy()
        run["conclusion"] = "success"
        artifact = self.github.artifacts[operations.DEPLOYMENT, run["id"]][0]
        self.github.replace(artifact, lambda value: value["locks"].pop("clowder-dev-4"))
        with self.assertRaisesRegex(operations.Error, "target"):
            self.deploy()
        self.assertFalse(self.github.posts)

    def test_pending_recipient_is_polled_without_dispatch_and_obeys_deadline(self):
        self.intent()
        run = self.github.recipient(self.inputs()[0])
        run.update(status="in_progress", conclusion=None)
        with self.assertRaisesRegex(operations.Error, "Timed out"):
            self.deploy(timeout=35)
        self.assertEqual(self.clock, 35)
        self.assertFalse(self.github.posts)
        run.update(status="completed", conclusion="success")
        self.assertTrue(self.deploy()["result"]["accepted"])
        self.assertFalse(self.github.posts)

    def test_successful_recipient_requires_current_attempt_payload_and_test_identity(self):
        self.intent()
        run = self.github.recipient(self.inputs()[0])
        artifact = self.github.artifacts[operations.DEPLOYMENT, run["id"]][0]
        raw, digest = self.github.archives[artifact["id"]], artifact["digest"]
        changes = (lambda r: r.update(payload_sha256="0" * 64), lambda r: r.update(candidate_run_id="998"),
                   lambda r: r.update(run_attempt=2), lambda r: r["functional"].update(wallet_sha="f" * 40),
                   lambda r: r["functional"].update(deployment_run_attempt=True),
                   lambda r: r["locks"]["clowder-dev-0"]["services"].update(postgres="postgres:latest"))
        for change in changes:
            with self.subTest(change=change):
                self.github.archives[artifact["id"]], artifact["digest"] = raw, digest
                self.github.replace(artifact, change)
                with self.assertRaises(operations.Error):
                    self.deploy()
                self.assertFalse(self.result_path.exists())
        self.assertFalse(self.github.posts)

    def test_missing_successful_recipient_result_and_duplicate_runs_stop(self):
        self.intent()
        run = self.github.recipient(self.inputs()[0])
        self.github.artifacts[operations.DEPLOYMENT, run["id"]] = []
        with self.assertRaisesRegex(operations.Error, "artifact"):
            self.deploy()
        self.github.recipient(self.inputs()[0])
        with self.assertRaisesRegex(operations.Error, "Multiple recipient"):
            self.deploy()
        self.assertFalse(self.github.posts)

    def test_successful_native_recipient_rerun_uses_only_its_fresh_result(self):
        self.intent()
        payload = self.inputs()[0]
        run = self.github.recipient(payload)
        self.github.attempts[run["id"], 1] = copy.deepcopy(run)
        run["run_attempt"] = 2
        with self.assertRaisesRegex(operations.Error, "artifact"):
            self.deploy()
        self.github.result(payload, run)
        value = self.deploy()
        self.assertEqual(value["deployment_run_attempt"], 2)
        self.assertFalse(self.github.posts)

    def test_positive_rollback_keeps_all_original_digests_and_sources(self):
        accepted, request = self.rollback_setup()
        self.assertEqual(request["payload"]["plan"], self.plan)
        self.assertEqual(request["payload"]["rollback_locks"], accepted["result"]["locks"])
        inputs = operations.rollback_input(self.cfg, self.request_path)
        self.intent(inputs)
        value = operations.reconcile(self.cfg, inputs, self.result_path)
        self.assertTrue(value["result"]["restored"])
        self.assertFalse(value["result"]["accepted"])
        self.assertEqual(value["accepted_run_id"], "123")
        self.assertEqual(len(self.github.posts), 2)

    def test_dry_run_or_unconfirmed_request_cannot_be_promoted(self):
        self.rollback_setup(dry=True)
        self.cfg["dry"] = False
        with self.assertRaisesRegex(operations.Error, "cannot be promoted"):
            operations.rollback_input(self.cfg, self.request_path)
        self.cfg["dry"] = True
        with patch.dict(os.environ, {"DATA_COMPATIBLE": "false"}):
            with self.assertRaisesRegex(operations.Error, "cannot be promoted"):
                operations.rollback_request(self.cfg, "123", self.request_path)
        self.assertEqual(len(self.github.posts), 1)

    def test_live_rollback_requires_compatibility_and_missing_request_stops_rerun(self):
        self.change_root()
        with patch.dict(os.environ, {"DATA_COMPATIBLE": "false"}):
            with self.assertRaisesRegex(operations.Error, "compatibility"):
                operations.rollback_request(self.cfg, "123", self.request_path)
        self.cfg["attempt"] = self.github.roots[124]["run_attempt"] = 2
        with self.assertRaisesRegex(operations.Error, "artifact is missing"):
            operations.rollback_request(self.cfg, "123", self.request_path)
        self.assertFalse(self.github.posts)

    def test_saved_rollback_request_reuses_source_after_master_moves(self):
        _, original = self.rollback_setup()
        self.cfg["attempt"] = self.github.roots[124]["run_attempt"] = 2
        self.github.head = "b" * 40
        self.github.calls.clear()
        self.request_path.unlink()
        restored = operations.rollback_request(self.cfg, "123", self.request_path)
        self.assertEqual(original, restored)
        self.assertFalse(any(path.endswith("/commits/master") for path, _ in self.github.calls))

    def test_previous_uses_recipient_result_time_and_tracks_restored_old_candidate(self):
        self.intent()
        payload = self.inputs()[0]
        old = self.github.recipient(payload, created="2026-09-09T03:00:00Z")
        self.change_root("125", operation="deploy")
        plan, images = self.github.bundle("125")
        operations.write(self.plan_path, plan); operations.write(self.images_path, images)
        self.intent()
        new = self.github.recipient(self.inputs()[0], created="2026-09-09T04:00:00Z")
        # Late parent bookkeeping for the older candidate cannot change selection.
        self.github.artifact(".github", self.github.roots[123], "clowder-nightly-result-1", {"accepted": True}, created="2026-09-09T08:00:00Z")
        self.change_root("126", operation="deploy")
        self.assertEqual(operations.previous(self.cfg), "125")
        self.change_root("124", operation="rollback")
        request = operations.rollback_request(self.cfg, "123", self.request_path)
        self.github.artifact(".github", self.github.roots[124], operations.REQUEST, request)
        inputs = operations.rollback_input(self.cfg, self.request_path)
        self.intent(inputs)
        self.github.recipient(inputs[0], created="2026-09-09T05:00:00Z")
        self.change_root("127", operation="deploy")
        self.assertEqual(operations.previous(self.cfg), "123")
        self.assertFalse(self.github.posts)

    def test_previous_does_not_replace_expired_acceptance_with_an_older_candidate(self):
        self.intent()
        run = self.github.recipient(self.inputs()[0])
        self.github.artifacts[operations.DEPLOYMENT, run["id"]][0]["expired"] = True
        self.change_root("124", operation="deploy")
        with self.assertRaisesRegex(operations.Error, "expired"):
            operations.previous(self.cfg)

    def test_api_error_before_dispatch_does_not_produce_success(self):
        self.intent()
        self.github.fail_read = "actions/workflows/deploy.yml/runs"
        with self.assertRaisesRegex(operations.Error, "API read"):
            self.deploy()
        self.assertFalse(self.github.posts)
        self.assertFalse(self.result_path.exists())

    def test_dry_run_and_deadline_stop_transport_before_process(self):
        payload = self.inputs()[0]
        self.cfg["dry"] = True
        with self.assertRaises(operations.Error):
            operations.send_dispatch(self.cfg, payload)
        self.cfg["dry"] = False
        self.cfg["deadline"] = 0
        with self.assertRaises(operations.Error):
            operations.send_dispatch(self.cfg, payload)
        self.assertFalse(self.github.posts)


if __name__ == "__main__":
    unittest.main()
