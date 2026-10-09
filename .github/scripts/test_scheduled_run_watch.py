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
SELF = {"id": 9, "name": "Watch scheduled runs", "path": watch.SELF_PATH, "state": "active"}
TICKETED = {**NIGHTLY, "name": "deploy nightly (clowder-dev)"}  # Wildcat-deployment's nightly-ticket.yml reports it
STAGING = {"id": 11, "name": "deploy staging", "path": ".github/workflows/staging.yml", "state": "active"}


def run(number, conclusion):
    return {"id": number, "conclusion": conclusion, "created_at": "2026-10-08T02:20:58Z",
            "html_url": f"https://github.com/ExampleOrg/deploy/actions/runs/{number}"}


class FakeGitHub:
    """The API calls of one pass, for repositories named in `repos`; a repository named "broken" fails."""

    def __init__(self, runs, issues=(), repos=("deploy",), issues_on=True, workflows=(NIGHTLY,)):
        self.runs, self.issues, self.writes, self.next_number = runs, list(issues), [], 100
        self.repos, self.issues_on, self.workflows, self.not_assignable = repos, issues_on, workflows, {"gone"}

    def api(self, path, method="GET", body=None):
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
            listing = [{"name": r, "archived": False, "disabled": False, "has_issues": self.issues_on} for r in self.repos]
            return listing if "page=1" in path else []
        if "/repos/ExampleOrg/broken/" in "/" + path:
            raise watch.APIError("GET broken: HTTP 500")
        if path.endswith("/actions/workflows?per_page=100"):
            return {"workflows": [*self.workflows, {"id": 8, "name": "Copilot", "path": "dynamic/copilot", "state": "active"}]}
        if "/actions/workflows/" in path and "/runs" in path:
            return {"workflow_runs": self.runs}
        if "/jobs" in path:
            return {"jobs": [{"name": "dev-0 / deploy", "conclusion": "failure"}, {"name": "dev-1 / deploy", "conclusion": "success"}]}
        if "/assignees/" in path:
            if path.rsplit("/", 1)[1] in self.not_assignable:
                raise watch.APIError("GET assignees: Not Found (HTTP 404)")
            return None
        if "/issues?state=" in path:
            assert "&creator=run-watch%5Bbot%5D" in path, path  # only the watcher's own issues are read
        if "/issues?state=open" in path:
            return [i for i in self.issues if i["state"] == "open"] if "page=1" in path else []
        if "/issues?state=closed" in path:
            return [i for i in self.issues if i["state"] == "closed"] if "page=1" in path else []
        raise AssertionError(path)


def own(number, run_id, user=BOT, state="open"):
    return {"number": number, "state": state, "user": user, "title": "Scheduled run failed: deploy nightly",
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
        self.assertEqual(body["assignees"], ["mtbitcr", "cleot"])  # a repository no group names
        self.assertEqual(body["labels"], ["awaiting triage"])
        self.assertIn("runs/41", body["body"])
        self.assertIn("dev-0 / deploy", body["body"])
        self.assertNotIn("dev-1 / deploy", body["body"])
        self.assertIn("deploy", summary)
        self.assertEqual(self.pass_once(gh)[0], 0)
        self.assertEqual(len(gh.writes), 1)  # the same failed run: nothing more

    def test_each_repository_has_its_group(self):
        self.assertEqual(watch.assignees_for("Wildcat-deployment"), ["cleot", "zupzup", "stefanbitcr", "codingpeanut157"])
        self.assertEqual(watch.assignees_for("E-Bill-frontend"), ["JulianVIE", "ABBitcredit", "cleot"])
        self.assertEqual(watch.assignees_for("docs-bitcr"), ["cleot", "zupzup", "stefanbitcr", "codingpeanut157", "mtbitcr"])
        self.assertEqual(watch.assignees_for("a-new-repository"), ["mtbitcr", "cleot"])

    def test_a_login_that_cannot_be_assigned_is_left_out(self):
        gh = FakeGitHub([run(41, "failure")])
        with patch.object(watch, "DEFAULT", ["mtbitcr", "gone"]):
            self.pass_once(gh)
        self.assertEqual(gh.writes[0][2]["assignees"], ["mtbitcr"])
        with patch.object(watch, "api", side_effect=watch.APIError("GET assignees: (HTTP 503)")), \
                self.assertRaises(watch.APIError):
            watch.assignable("deploy", "mtbitcr")  # an outage is not a "no"

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

    def test_an_issue_that_a_person_closed_is_not_opened_again_for_the_same_run(self):
        gh = FakeGitHub([run(41, "failure")], [own(9, 41, state="closed")])
        self.pass_once(gh)
        self.assertEqual(gh.writes, [])
        gh.runs = [run(42, "failure")]  # a later failure is news again
        self.pass_once(gh)
        self.assertEqual([(m, p) for m, p, _ in gh.writes], [("POST", "repos/ExampleOrg/deploy/issues")])

    def test_a_run_older_than_the_reported_one_changes_nothing(self):
        # While run 41 is re-run, the newest completed run is an older one.
        gh = FakeGitHub([run(40, "success")], [own(9, 41)])
        self.pass_once(gh)
        self.assertEqual(gh.writes, [])

    def test_a_cancelled_run_and_issues_of_others_are_left_alone(self):
        gh = FakeGitHub([run(44, "cancelled")], [own(9, 41)])
        self.pass_once(gh)
        self.assertEqual(gh.writes, [])
        gh = FakeGitHub([run(41, "failure")], [own(10, 41, user=HUMAN)])  # a person's copy of the marker
        self.pass_once(gh)
        self.assertEqual([(m, p) for m, p, _ in gh.writes], [("POST", "repos/ExampleOrg/deploy/issues")])

    def test_the_watcher_does_not_watch_itself(self):
        gh = FakeGitHub([run(46, "failure")], repos=(".github",), workflows=(SELF,))
        self.assertEqual(self.pass_once(gh)[0], 0)
        self.assertEqual(gh.writes, [])

    def test_a_workflow_with_a_ticket_workflow_is_listed_but_gets_no_issue(self):
        gh = FakeGitHub([run(41, "failure")], repos=("Wildcat-deployment",), workflows=(TICKETED, STAGING))
        code, summary = self.pass_once(gh)
        self.assertEqual(code, 0)
        (method, path, body), = gh.writes  # only the other workflow gets an issue
        self.assertEqual((method, path), ("POST", "repos/ExampleOrg/Wildcat-deployment/issues"))
        self.assertIn(STAGING["path"], body["body"])
        self.assertIn("Wildcat-deployment `.github/workflows/nightly.yml`: failure", summary)  # still listed
        self.assertIn("its ticket workflow reports the failure", summary)
        gh = FakeGitHub([run(41, "failure")], repos=("Wildcat-deployment",), issues_on=False, workflows=(TICKETED,))
        code, summary = self.pass_once(gh)
        self.assertEqual((code, gh.writes), (0, []))
        self.assertIn("issues are off", summary)  # then the ticket workflow cannot report it either
        self.assertNotIn("its ticket workflow", summary)

    def test_a_renamed_ticketed_workflow_is_reported_again(self):
        # The ticket workflow finds the nightly by its name, so after a rename it is silent.
        gh = FakeGitHub([run(41, "failure")], repos=("Wildcat-deployment",), workflows=(NIGHTLY,))
        self.pass_once(gh)
        self.assertEqual([(m, p) for m, p, _ in gh.writes], [("POST", "repos/ExampleOrg/Wildcat-deployment/issues")])

    def test_an_older_watcher_issue_of_a_ticketed_workflow_still_closes(self):
        gh = FakeGitHub([run(42, "failure")], [own(9, 41)], repos=("Wildcat-deployment",), workflows=(TICKETED,))
        self.pass_once(gh)
        self.assertEqual(gh.writes, [])  # no comment: the ticket workflow reports the failure
        gh.runs = [run(43, "success")]
        self.pass_once(gh)
        self.assertEqual([(m, p) for m, p, _ in gh.writes], [("POST", "repos/ExampleOrg/Wildcat-deployment/issues/9/comments"),
                                                             ("PATCH", "repos/ExampleOrg/Wildcat-deployment/issues/9")])

    def test_one_broken_repository_does_not_stop_the_others(self):
        gh = FakeGitHub([run(41, "failure")], repos=("broken", "deploy"))
        code, summary = self.pass_once(gh)
        self.assertEqual(code, 1)  # the run shows the error
        self.assertEqual([(m, p) for m, p, _ in gh.writes], [("POST", "repos/ExampleOrg/deploy/issues")])
        self.assertIn("broken", summary)

    def test_a_repository_without_issues_is_named_in_the_summary(self):
        gh = FakeGitHub([run(41, "failure")], issues_on=False)
        code, summary = self.pass_once(gh)
        self.assertEqual((code, gh.writes), (0, []))
        self.assertIn("issues are off", summary)

    def test_a_dry_run_writes_nothing_and_says_what_it_would_do(self):
        gh = FakeGitHub([run(45, "startup_failure")])
        code, summary = self.pass_once(gh, dry_run=True)
        self.assertEqual((code, gh.writes), (0, []))
        self.assertIn("would open", summary)


if __name__ == "__main__":
    unittest.main()
