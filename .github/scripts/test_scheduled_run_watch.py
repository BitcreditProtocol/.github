#!/usr/bin/env python3
"""Offline checks for the scheduled-run watcher against a fake GitHub API."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ENV = {"ORG": "ExampleOrg", "WATCHER_BOT": "run-watch[bot]", "DRY_RUN": "false",
       "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
with patch.dict(os.environ, ENV, clear=True):
    spec = importlib.util.spec_from_file_location("run_watch", ROOT / "scripts/watch-scheduled-runs.py")
    watch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch)
BOT = {"type": "Bot", "login": ENV["WATCHER_BOT"]}
HUMAN = {"type": "User", "login": "maintainer"}
NIGHTLY = {"id": 7, "name": "deploy nightly", "path": ".github/workflows/nightly.yml", "state": "active"}


def run(number, conclusion):
    return {"id": number, "conclusion": conclusion, "created_at": "2026-10-08T02:20:58Z",
            "html_url": f"https://github.com/ExampleOrg/deploy/actions/runs/{number}"}


class FakeGitHub:
    """The API calls of one pass: repositories, workflows, the newest scheduled run, jobs and issues."""

    def __init__(self, runs, issues=()):
        self.runs, self.issues, self.writes, self.next_number = runs, list(issues), [], 100

    def api(self, path, method="GET", body=None, *, missing=False):
        if method != "GET":
            if watch.DRY_RUN:
                raise watch.APIError("dry-run refused a write")
            self.writes.append((method, path, body))
            if method == "POST" and path.endswith("/issues"):
                self.next_number += 1
                self.issues.append({"number": self.next_number, "state": "open", "user": BOT, **body})
                return {"number": self.next_number}
            if method == "PATCH":
                number = int(path.rsplit("/", 1)[1])
                issue = next(i for i in self.issues if i["number"] == number)
                issue.update(body)
                return issue
            return {"id": 1}
        if path.startswith("orgs/ExampleOrg/repos"):
            return [{"name": "deploy", "archived": False, "disabled": False, "has_issues": True}] if "page=1" in path else []
        if path.endswith("/actions/workflows?per_page=100"):
            return {"workflows": [NIGHTLY, {"id": 8, "name": "Copilot", "path": "dynamic/copilot", "state": "active"}]}
        if "/actions/workflows/7/runs" in path:
            return {"workflow_runs": self.runs}
        if "/jobs" in path:
            return {"jobs": [{"name": "dev-0 / deploy", "conclusion": "failure"}, {"name": "dev-1 / deploy", "conclusion": "success"}]}
        if "/issues?state=open" in path:
            return [i for i in self.issues if i["state"] == "open"] if "page=1" in path else []
        raise AssertionError(path)

    def bodies(self):
        return [body for _, _, body in self.writes]


def own(number, run_id, user=BOT):
    return {"number": number, "state": "open", "user": user, "title": "Scheduled run failed: deploy nightly",
            "body": watch.issue_body(NIGHTLY, run(run_id, "failure"), ["dev-0 / deploy"])}


class WatchTest(unittest.TestCase):
    def pass_once(self, gh, dry_run=False):
        with tempfile.NamedTemporaryFile("w+") as summary, patch.object(watch, "api", gh.api), \
                patch.object(watch, "DRY_RUN", dry_run), patch.object(watch, "SUMMARY", summary.name):
            code = watch.main()
            return code, Path(summary.name).read_text()

    def test_a_failed_scheduled_run_opens_one_assigned_issue(self):
        gh = FakeGitHub([run(41, "failure")])
        code, summary = self.pass_once(gh)
        self.assertEqual(code, 0)
        (method, path, body), = gh.writes
        self.assertEqual((method, path), ("POST", "repos/ExampleOrg/deploy/issues"))
        self.assertEqual(body["assignees"], ["mtbitcr", "cleot", "zupzup", "stefanbitcr", "codingpeanut157"])
        self.assertEqual(body["labels"], ["awaiting triage"])
        self.assertIn("runs/41", body["body"])
        self.assertIn("dev-0 / deploy", body["body"])
        self.assertNotIn("dev-1 / deploy", body["body"])
        self.assertIn("deploy", summary)
        self.assertEqual(self.pass_once(gh)[0], 0)
        self.assertEqual(len(gh.writes), 1)  # the same failed run: nothing more

    def test_a_new_failed_run_comments_and_a_green_run_closes(self):
        gh = FakeGitHub([run(42, "failure")], [own(9, 41)])
        self.pass_once(gh)
        self.assertEqual([(m, p) for m, p, _ in gh.writes],
                         [("PATCH", "repos/ExampleOrg/deploy/issues/9"), ("POST", "repos/ExampleOrg/deploy/issues/9/comments")])
        self.assertIn("runs/42", gh.writes[1][2]["body"])  # a comment notifies the assignees again
        gh.runs, gh.writes = [run(43, "success")], []
        self.pass_once(gh)
        self.assertEqual([(m, p) for m, p, _ in gh.writes],
                         [("POST", "repos/ExampleOrg/deploy/issues/9/comments"), ("PATCH", "repos/ExampleOrg/deploy/issues/9")])
        self.assertEqual(gh.writes[1][2], {"state": "closed", "state_reason": "completed"})

    def test_a_cancelled_run_and_issues_of_others_are_left_alone(self):
        gh = FakeGitHub([run(44, "cancelled")], [own(9, 41), own(10, 41, user=HUMAN)])
        self.pass_once(gh)
        self.assertEqual(gh.writes, [])

    def test_a_dry_run_writes_nothing_and_says_what_it_would_do(self):
        gh = FakeGitHub([run(45, "startup_failure")])
        code, summary = self.pass_once(gh, dry_run=True)
        self.assertEqual((code, gh.writes), (0, []))
        self.assertIn("would open", summary)


if __name__ == "__main__":
    unittest.main()
