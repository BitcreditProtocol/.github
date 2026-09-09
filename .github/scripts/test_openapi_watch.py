#!/usr/bin/env python3
"""Offline checks for the real watcher; only the workflow check invokes local yq."""
import base64
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile
import zlib

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ENV = {"ORG": "ExampleOrg", "WATCHER_BOT": "openapi-watch[bot]", "DRY_RUN": "false",
       "READ_TOKEN": "private-read-value", "WRITE_TOKEN": "private-write-value",
       "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
with patch.dict(os.environ, ENV, clear=True):
    spec = importlib.util.spec_from_file_location("openapi_watch", ROOT / "scripts/watch-openapi.py")
    watch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch)
REAL_RUN = subprocess.run
S, C = "a" * 40, "b" * 40
BOT = {"type": "Bot", "login": ENV["WATCHER_BOT"]}
HUMAN = {"type": "User", "login": "maintainer"}
DOCUMENT = {"openapi": "3.1.0", "info": {"title": "Fixture", "version": "1.0"},
            "paths": {"/status": {"get": {"responses": {"200": {"description": "ok"}}}}}}


def archive(data, extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("openapi.json", data)
        if extra:
            bundle.writestr(extra, data)
    return stream.getvalue()


class Fixture:
    def __init__(self, *, changed=False):
        self.source_doc = copy.deepcopy(DOCUMENT)
        if changed:
            self.source_doc["paths"]["/new"] = {"get": {"responses": {"200": {"description": "new"}}}}
        self.source_bytes = json.dumps(self.source_doc).encode()
        self.consumer_bytes = json.dumps(DOCUMENT).encode()
        self.archive = archive(self.source_bytes)
        self.issues = []
        self.calls = []
        self.overrides = {}
        self.source_heads, self.consumer_heads = [S], [C]
        self.issue_reads = 0
        self.dismiss_on_second_read = False
        self.lost_response = False
        self.fail_before_write = False
        self.runs = [self.make_run(20)]

    def make_run(self, number, started="2026-09-09T07:32:57Z"):
        return dict(id=number, workflow_id=10, head_sha=S, head_branch="master", event="push",
                    status="completed", conclusion="success", run_attempt=1, run_started_at=started,
                    repository={"id": 1}, head_repository={"id": 1})

    def snapshots(self):
        return (dict(repository=watch.PRODUCER, sha=S, run_id=20, artifact_id=30,
                     canonical_sha256=watch.canonical_digest(self.source_bytes)),
                dict(repository=watch.CONSUMER, sha=C,
                     canonical_sha256=watch.canonical_digest(self.consumer_bytes)))

    def issue(self, *, number=7, state="open", resolved=False, target=None, closer=None, author=None):
        source, consumer = self.snapshots()
        if target:
            source["canonical_sha256"] = target
        issue = dict(watch.issue_payload(source, consumer, resolved=resolved),
                     id=1000 + number, number=number, state=state, user=author or dict(BOT))
        if state == "closed":
            issue.update(state_reason="completed", closed_by=closer or dict(BOT if resolved else HUMAN))
        return issue

    def api(self, path, method="GET", body=None, *, paginate=False, raw=False):
        self.calls.append((method, path, copy.deepcopy(body), paginate, raw))
        route = urlsplit(path).path
        query = parse_qs(urlsplit(path).query)
        prefix = "repos/ExampleOrg/"
        if method != "GET":
            if watch.DRY_RUN:
                raise AssertionError("dry run attempted a write")
            if not route.startswith(prefix + watch.CONSUMER + "/issues"):
                raise AssertionError("write escaped the dashboard")
            watch.WRITE_ATTEMPTS += 1
            if self.fail_before_write:
                raise watch.EvidenceError("response lost before server mutation")
            if method == "POST":
                number = max((item["number"] for item in self.issues), default=6) + 1
                current = self.issue(number=number)
                self.issues.append(current)
            else:
                number = int(route.rsplit("/", 1)[-1])
                current = next(item for item in self.issues if item["number"] == number)
            current.update(copy.deepcopy(body))
            if current["state"] == "closed":
                current["closed_by"] = dict(BOT)
            if self.lost_response:
                self.lost_response = False
                raise watch.EvidenceError("response lost after server mutation")
            return {"number": number}
        if route in (prefix + watch.PRODUCER, prefix + watch.CONSUMER):
            key = "producer_repo" if route.endswith(watch.PRODUCER) else "consumer_repo"
            data = dict(id=1 if key == "producer_repo" else 2, full_name=route[6:],
                        archived=False, has_issues=True)
        elif "/git/ref/heads/" in route:
            producer = watch.PRODUCER + "/" in route
            key = "source_ref" if producer else "consumer_ref"
            heads = self.source_heads if producer else self.consumer_heads
            sha = heads.pop(0) if len(heads) > 1 else heads[0]
            data = {"ref": "refs/heads/master" if producer else "refs/heads/dev",
                    "object": {"type": "commit", "sha": sha}}
        elif route.endswith("/actions/workflows/openapi.yml"):
            key, data = "workflow", {"id": 10, "state": "active", "path": watch.WORKFLOW_PATH}
        elif route.endswith("/actions/workflows/10/runs"):
            assert paginate and query["head_sha"] and query["branch"] == ["master"]
            key, data = "runs", [{"total_count": len(self.runs), "workflow_runs": self.runs}]
        elif "/actions/runs/" in route and route.endswith("/artifacts"):
            assert paginate
            run_id = int(route.split("/actions/runs/")[1].split("/")[0])
            item = dict(id=30, name="openapi", expired=False, expires_at="2099-12-08T00:00:00Z",
                        size_in_bytes=len(self.archive), digest="sha256:" + hashlib.sha256(self.archive).hexdigest(),
                        workflow_run=dict(id=run_id, head_sha=S, head_branch="master",
                                          repository_id=1, head_repository_id=1))
            key, data = "artifacts", [{"total_count": 1, "artifacts": [item]}]
        elif route.endswith("/actions/artifacts/30/zip"):
            assert raw
            key, data = "archive", self.archive
        elif "/contents/" in route:
            assert query["ref"] == [C]
            content = self.consumer_bytes
            key = "consumer_file"
            data = dict(type="file", path=watch.SPEC_PATH, encoding="base64", size=len(content),
                        sha=hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
                        content=base64.b64encode(content).decode())
        elif route.endswith("/issues"):
            assert paginate and query["state"] == ["all"]
            self.issue_reads += 1
            if self.dismiss_on_second_read and self.issue_reads == 2:
                self.issues[0].update(state="closed", closed_by=dict(HUMAN), state_reason="completed")
            key = "issues"
            data = [self.issues[index:index + 100] for index in range(0, len(self.issues), 100)] or [[]]
        elif "/issues/" in route:
            key = "issue_detail"
            number = int(route.rsplit("/", 1)[-1])
            data = next(item for item in self.issues if item["number"] == number)
        else:
            raise AssertionError("unexpected endpoint: " + route)
        data = copy.deepcopy(data)
        override = self.overrides.get(key)
        if isinstance(override, Exception):
            raise override
        return override(data) if callable(override) else data

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class OpenAPIWatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.enterContext(patch.object(watch, "DRY_RUN", False))
        self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("unexpected real command")))

    def execute(self, fixture, *, dry=False):
        output = io.StringIO()
        with patch.object(watch, "api", side_effect=fixture.api), patch.object(watch, "DRY_RUN", dry), redirect_stdout(output):
            code = watch.main()
        text = output.getvalue()
        self.assertNotIn(ENV["READ_TOKEN"], text)
        self.assertNotIn(ENV["WRITE_TOKEN"], text)
        self.assertNotIn('"/status"', text)
        return code, json.loads(text)

    def test_equal_and_key_reordered_documents_are_quiet(self):
        fixture = Fixture()
        fixture.consumer_bytes = json.dumps(DOCUMENT, sort_keys=True, indent=4).encode()
        code, result = self.execute(fixture, dry=True)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "in_sync")
        self.assertEqual(result["proposed_actions"], [])
        self.assertEqual(result["write_attempts"], 0)
        self.assertEqual(result["verified_writes"], 0)
        self.assertEqual(result["source"]["run_id"], 20)
        self.assertEqual(result["source"]["artifact_id"], 30)
        self.assertEqual(result["consumer"]["sha"], C)
        self.assertEqual(fixture.writes(), [])

    def test_change_is_proposed_in_dry_run_and_verified_when_enabled(self):
        fixture = Fixture(changed=True)
        code, result = self.execute(fixture, dry=True)
        self.assertEqual(code, 0)
        self.assertEqual(result["proposed_actions"], [{"kind": "open", "number": None}])
        self.assertEqual(fixture.writes(), [])
        fixture = Fixture(changed=True)
        code, result = self.execute(fixture)
        self.assertEqual(code, 0)
        self.assertEqual((result["write_attempts"], result["verified_writes"]), (1, 1))
        self.assertEqual(len(fixture.writes()), 1)
        first_write = next(index for index, call in enumerate(fixture.calls) if call[0] != "GET")
        self.assertEqual(sum(call[1].startswith("repos/ExampleOrg/wildcat-dashboard-ui/issues?") for call in fixture.calls[:first_write]), 2)
        self.assertEqual(fixture.calls[-1][0], "GET")

    def test_same_digest_does_not_refresh_an_open_issue(self):
        fixture = Fixture(changed=True)
        issue = fixture.issue()
        issue["body"] += "\nMaintainer note preserved."
        fixture.issues = [issue]
        code, result = self.execute(fixture)
        self.assertEqual((code, result["status"]), (0, "already_reported"))
        self.assertEqual(fixture.writes(), [])
        self.assertEqual(fixture.issues[0]["body"], issue["body"])

    def test_manual_closure_suppresses_only_its_digest(self):
        for resolved in (False, True):
            fixture = Fixture(changed=True)
            fixture.issues = [fixture.issue(state="closed", resolved=resolved, closer=HUMAN)]
            code, result = self.execute(fixture)
            self.assertEqual((code, result["status"]), (0, "manually_dismissed"))
            self.assertEqual(fixture.writes(), [])
        fixture = Fixture(changed=True)
        fixture.issues = [fixture.issue(state="closed", target="0" * 64)]
        code, result = self.execute(fixture)
        self.assertEqual(code, 0)
        self.assertEqual(result["proposed_actions"], [{"kind": "open", "number": None}])
        self.assertEqual(sum(issue["state"] == "open" for issue in fixture.issues), 1)
        self.assertEqual(fixture.issues[0]["state"], "closed")

    def test_new_digest_updates_and_sync_closes_then_regression_reopens(self):
        fixture = Fixture(changed=True)
        fixture.issues = [fixture.issue(target="0" * 64)]
        code, result = self.execute(fixture)
        self.assertEqual((code, result["proposed_actions"][0]["kind"]), (0, "update"))
        fixture.consumer_bytes = fixture.source_bytes
        code, result = self.execute(fixture)
        self.assertEqual((code, result["proposed_actions"][0]["kind"]), (0, "close"))
        self.assertTrue(watch.issue_state(fixture.issues[0])["resolved"])
        fixture.consumer_bytes = json.dumps(DOCUMENT).encode()
        code, result = self.execute(fixture)
        self.assertEqual((code, result["proposed_actions"][0]), (0, {"kind": "update", "number": 7}))
        self.assertEqual(fixture.issues[0]["state"], "open")

    def test_only_owned_marked_issues_are_managed_and_duplicates_stop(self):
        fixture = Fixture(changed=True)
        fixture.issues = [fixture.issue(author=HUMAN)]
        code, result = self.execute(fixture)
        self.assertEqual(code, 0)
        self.assertEqual(result["proposed_actions"][0]["kind"], "open")
        self.assertEqual(fixture.issues[0]["user"], HUMAN)
        fixture = Fixture(changed=True)
        fixture.issues = [fixture.issue(number=7), fixture.issue(number=8)]
        code, result = self.execute(fixture)
        self.assertEqual(code, 1)
        self.assertIn("multiple open", result["gaps"][0])
        self.assertEqual(fixture.writes(), [])

    def test_all_issue_pages_are_read_before_a_manual_dismissal_decision(self):
        fixture = Fixture(changed=True)
        fixture.issues = [fixture.issue(number=i, author=HUMAN) for i in range(1, 202)]
        fixture.issues.append(fixture.issue(number=300, state="closed"))
        code, result = self.execute(fixture)
        self.assertEqual((code, result["status"]), (0, "manually_dismissed"))
        self.assertEqual(fixture.writes(), [])

    def test_incomplete_or_malformed_reads_never_write(self):
        cases = [
            ("runs", lambda data: [{**data[0], "total_count": 2}]),
            ("runs", lambda data: [{"total_count": 0, "workflow_runs": []}]),
            ("runs", lambda data: [{"total_count": 2, "workflow_runs": data[0]["workflow_runs"] * 2}]),
            ("runs", lambda data: [{"total_count": 1, "workflow_runs": [{**data[0]["workflow_runs"][0], "repository": {"id": True}}]}]),
            ("issues", lambda data: [{}]),
            ("issues", lambda data: [[{"id": True}]]),
            ("issues", lambda data: [[{"id": 1, "number": 1, "state": "open", "title": "unrelated", "user": HUMAN}]]),
            ("consumer_file", lambda data: {**data, "content": None}),
            ("consumer_file", lambda data: {**data, "content": "%%%"}),
            ("consumer_file", lambda data: {**data, "size": data["size"] + 1}),
            ("consumer_file", lambda data: {**data, "sha": "f" * 40}),
            ("consumer_ref", lambda data: {"object": {"sha": C}}),
            ("workflow", lambda data: {**data, "state": "disabled_manually"}),
            ("issues", watch.EvidenceError("partial issue-page request failed")),
        ]
        for key, response in cases:
            with self.subTest(key=key):
                fixture = Fixture(changed=True)
                fixture.overrides[key] = response
                code, result = self.execute(fixture)
                self.assertEqual(code, 1)
                self.assertTrue(result["gaps"])
                self.assertEqual(fixture.writes(), [])

    def test_latest_successful_exact_run_and_artifact_identity(self):
        fixture = Fixture()
        fixture.runs = [fixture.make_run(21), fixture.make_run(20, "2026-09-08T07:32:57Z")]
        code, result = self.execute(fixture, dry=True)
        self.assertEqual(code, 0)
        self.assertEqual(result["source"]["run_id"], 21)
        for change in ({"expired": True}, {"expires_at": "2000-01-01T00:00:00Z"},
                       {"id": True}, {"workflow_run": {"id": 999}}, {"digest": None}):
            fixture = Fixture(changed=True)
            fixture.overrides["artifacts"] = lambda data, change=change: [
                {"total_count": 1, "artifacts": [{**data[0]["artifacts"][0], **change}]}]
            code, result = self.execute(fixture)
            self.assertEqual(code, 1)
            self.assertEqual(fixture.writes(), [])
        fixture = Fixture(changed=True)
        fixture.overrides["artifacts"] = lambda data: [{"total_count": 2, "artifacts": [
            data[0]["artifacts"][0], {**data[0]["artifacts"][0], "id": 31}]}]
        self.assertEqual(self.execute(fixture)[0], 1)
        self.assertEqual(fixture.writes(), [])

    def test_corrupt_archives_and_invalid_openapi_are_unknown(self):
        for data in (b"not zip", archive(b"{}"), archive(b"not JSON"), archive(json.dumps(DOCUMENT).encode(), "extra.json"),
                     archive(b'{"openapi":"3.1.0","openapi":"3.0.0"}')):
            fixture = Fixture(changed=True)
            fixture.archive = data
            code, result = self.execute(fixture)
            self.assertEqual(code, 1)
            self.assertEqual(fixture.writes(), [])
        fixture = Fixture(changed=True)
        fixture.overrides["archive"] = lambda data: data + b"corruption"
        self.assertEqual(self.execute(fixture)[0], 1)
        self.assertEqual(fixture.writes(), [])
        for error in (EOFError(), zlib.error("corrupt compressed data")):
            fixture = Fixture(changed=True)
            with patch.object(zipfile.ZipFile, "read", side_effect=error):
                self.assertEqual(self.execute(fixture)[0], 1)
            self.assertEqual(fixture.writes(), [])
        for doc in ({}, {**DOCUMENT, "paths": []}, {**DOCUMENT, "components": []}):
            with self.assertRaises(watch.EvidenceError):
                watch.canonical_digest(json.dumps(doc).encode())

    def test_stale_source_consumer_and_issue_state_never_write(self):
        fixture = Fixture(changed=True)
        fixture.source_heads = [S, "c" * 40]
        self.assertEqual(self.execute(fixture)[0], 1)
        self.assertEqual(fixture.writes(), [])
        fixture = Fixture(changed=True)
        fixture.consumer_heads = [C, "d" * 40]
        self.assertEqual(self.execute(fixture)[0], 1)
        self.assertEqual(fixture.writes(), [])
        fixture = Fixture(changed=True)
        fixture.issues = [fixture.issue(target="0" * 64)]
        fixture.dismiss_on_second_read = True
        self.assertEqual(self.execute(fixture)[0], 1)
        self.assertEqual(fixture.writes(), [])

    def test_malformed_owned_state_or_unknown_closer_stops(self):
        for transform in (lambda issue: {**issue, "body": watch.MARKER},
                          lambda issue: {**issue, "body": issue["body"] + "\n" + watch.MARKER},
                          lambda issue: {**issue, "closed_by": None}):
            fixture = Fixture(changed=True)
            fixture.issues = [transform(fixture.issue(state="closed", resolved=True))]
            self.assertEqual(self.execute(fixture)[0], 1)
            self.assertEqual(fixture.writes(), [])

    def test_lost_write_responses_are_read_back_without_second_write(self):
        for existing in (False, True):
            fixture = Fixture(changed=True)
            if existing:
                fixture.issues = [fixture.issue(target="0" * 64)]
            fixture.lost_response = True
            code, result = self.execute(fixture)
            self.assertEqual(code, 0)
            self.assertEqual((result["write_attempts"], result["verified_writes"]), (1, 1))
            self.assertEqual(len(fixture.writes()), 1)
        fixture = Fixture(changed=True)
        fixture.fail_before_write = True
        code, result = self.execute(fixture)
        self.assertEqual(code, 1)
        self.assertEqual(result["write_attempts"], 1)
        self.assertEqual(len(fixture.writes()), 1)
        fixture = Fixture(changed=True)
        fixture.overrides["issue_detail"] = lambda data: {**data, "state": "closed", "closed_by": HUMAN,
                                                         "state_reason": "completed"}
        self.assertEqual(self.execute(fixture)[0], 1)
        self.assertEqual(len(fixture.writes()), 1)

    def test_cli_tokens_are_separate_and_errors_do_not_disclose_them(self):
        response = subprocess.CompletedProcess([], 0, b'{"number":7}', b"")
        with patch.object(subprocess, "run", return_value=response) as command:
            watch.api("repos/ExampleOrg/Wildcat")
            self.assertEqual(command.call_args.kwargs["env"]["GH_TOKEN"], ENV["READ_TOKEN"])
            self.assertNotIn("WRITE_TOKEN", command.call_args.kwargs["env"])
            watch.api("repos/ExampleOrg/wildcat-dashboard-ui/issues", "POST", {"title": "test"})
            self.assertEqual(command.call_args.kwargs["env"]["GH_TOKEN"], ENV["WRITE_TOKEN"])
            self.assertNotIn("READ_TOKEN", command.call_args.kwargs["env"])
            self.assertNotIn(ENV["WRITE_TOKEN"], str(command.call_args.args))
        with patch.object(subprocess, "run") as command, patch.object(watch, "DRY_RUN", True):
            with self.assertRaises(watch.EvidenceError):
                watch.api("repos/ExampleOrg/wildcat-dashboard-ui/issues", "POST", {})
            command.assert_not_called()
        with self.assertRaises(watch.EvidenceError):
            watch.api("repos/ExampleOrg/Wildcat/issues", "POST", {})
        failure = subprocess.CompletedProcess([], 1, b"secret spec", b"private-read-value (HTTP 403)")
        with patch.object(subprocess, "run", return_value=failure):
            with self.assertRaises(watch.EvidenceError) as error:
                watch.api("repos/ExampleOrg/Wildcat")
            self.assertNotIn("private-read-value", str(error.exception))
            self.assertNotIn("secret spec", str(error.exception))

    def test_step_summary_and_default_dry_run(self):
        with patch.dict(os.environ, {}, clear=True):
            spec = importlib.util.spec_from_file_location("openapi_default", ROOT / "scripts/watch-openapi.py")
            default = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(default)
            self.assertTrue(default.DRY_RUN)
        fixture = Fixture()
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                self.assertEqual(self.execute(fixture, dry=True)[0], 0)
            text = summary.read_text()
            self.assertIn("Proposed actions: **0**", text)
            self.assertIn("write attempts: **0**", text)
            self.assertIn(S, text)
        with patch.dict(os.environ, {"DRY_RUN": "invalid"}):
            code, result = self.execute(Fixture(changed=True))
            self.assertEqual(code, 1)
            self.assertEqual(result["write_attempts"], 0)

    def test_workflow_keeps_prs_unprivileged_and_tokens_scoped(self):
        result = REAL_RUN(["yq", "-o=json", ".", str(ROOT / "workflows/watch-openapi.yml")],
                          capture_output=True, text=True, check=True)
        workflow = json.loads(result.stdout)
        self.assertEqual(workflow["permissions"], {})
        self.assertTrue(workflow["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"])
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "*/15 * * * *"}])
        test, job = workflow["jobs"]["test"], workflow["jobs"]["watch"]
        self.assertEqual(test["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", json.dumps(test))
        self.assertEqual(job["if"], "github.event_name != 'pull_request'")
        self.assertEqual(job["needs"], "test")
        self.assertIs(job["concurrency"]["cancel-in-progress"], False)
        self.assertIn("inputs.dry_run != false", job["env"]["DRY_RUN"])
        steps = {step.get("id"): step for step in job["steps"]}
        read, write = steps["read"]["with"], steps["write"]["with"]
        self.assertEqual(set(read["repositories"].split()), {"Wildcat", "wildcat-dashboard-ui"})
        self.assertEqual({key: value for key, value in read.items() if key.startswith("permission-")},
                         {"permission-actions": "read", "permission-contents": "read",
                          "permission-metadata": "read", "permission-issues": "read"})
        self.assertEqual(write["repositories"], "wildcat-dashboard-ui")
        self.assertEqual(set(key for key in write if key.startswith("permission-")),
                         {"permission-metadata", "permission-issues"})
        self.assertIn("env.DRY_RUN == 'true'", write["permission-issues"])
        for name in ("read", "write"):
            self.assertRegex(steps[name]["uses"], r"@[0-9a-f]{40}$")
        env = job["steps"][-1]["env"]
        self.assertIn("steps.read.outputs.token", env["READ_TOKEN"])
        self.assertIn("steps.write.outputs.token", env["WRITE_TOKEN"])


if __name__ == "__main__":
    unittest.main()
