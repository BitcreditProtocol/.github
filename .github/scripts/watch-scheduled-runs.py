#!/usr/bin/env python3
"""Open one issue per workflow whose newest scheduled run failed; close it when a later run passes.

GitHub tells only the person who last changed a cron line about a failed scheduled run. This
watcher opens an issue in the workflow's repository and assigns the repository's group in GROUPS,
so the people who own it hear of it.
"""
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote

ORG = os.environ.get("ORG", "BitcreditProtocol")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "/dev/stdout")
WATCHER_BOT = os.environ.get("WATCHER_BOT", "bitcredit-automation[bot]")
MARKER = "bitcredit-scheduled-run-watch"
LABEL = "awaiting triage"
FAILED = {"failure", "timed_out", "startup_failure"}
# The watcher's own workflow is not watched: an error that fails every pass would comment every
# hour. Its own failure reaches its cron author through GitHub.
SELF_PATH = ".github/workflows/watch-scheduled-runs.yml"
# For a failure of these workflows the watcher opens no issue and adds no comment. A ticket workflow
# in the same repository reports their failures (Wildcat-deployment#182). The ticket workflow finds
# the nightly by name, so the entry holds the name. When the names differ, the watcher reports the
# nightly.
# Change or remove an entry only together with its ticket workflow.
# shortcut: nothing checks that the ticket workflow still exists; add a check if the set grows.
OWN_TICKETS = {("Wildcat-deployment", ".github/workflows/nightly.yml", "deploy nightly (clowder-dev)")}
# The owner's choice of 2026-10-08: who hears of a failed scheduled run, by repository.
GROUPS = [
    (["JulianVIE", "ABBitcredit", "cleot"],  # frontend
     ["E-Bill-frontend", "ui", "ui-flutter", "wildcat-dashboard-ui", "wildcat-dashboard-flutter", "wallet",
      "eBill", "static-assets"]),
    (["codingpeanut157", "cleot", "stefanbitcr"], ["Clowder", "Wildcat", "Wildcat-Auxiliary", "Protocol-E2E"]),
    (["cleot", "zupzup", "stefanbitcr"], ["Wallet-Core"]),
    (["zupzup", "cleot"], ["Bitcredit-Core", "bcr-load-tests"]),
    (["cleot", "zupzup", "stefanbitcr", "codingpeanut157"],  # infrastructure
     ["infrastructure", "Wildcat-deployment", "helm-charts", "docker-external", "bcr-common", "bcr-relay",
      "nostr-postgres-db"]),
    (["tobomobo", "cleot"], ["bitcr-chat-widget", "bitcredit-lab", "AI-Credit"]),
    (["cleot", "zupzup", "stefanbitcr", "codingpeanut157", "mtbitcr"], ["docs-bitcr", "Governance", "cats", "internal_cats"]),
    (["cleot"], ["crowdin-sdk", "nostr"]),  # forks
    (["mtbitcr", "cleot"], [".github", "review-server", "security-reports", "bit.cr", "bitcr.org", "bitcredit-tools"]),
]
DEFAULT = ["mtbitcr", "cleot"]  # a repository that no group names yet


class APIError(RuntimeError):
    pass


def api(path, method="GET", body=None):
    if method != "GET" and DRY_RUN:
        raise APIError("dry-run refused a write")
    command = ["gh", "api", "-X", method, path]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise APIError(f"{method} {path}: {result.stderr.strip()[:500] or 'request failed'}")
    if not result.stdout.strip():  # 204 No Content
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise APIError(f"{path}: invalid JSON response") from None


def pages(path):
    result, page = [], 1
    while True:
        rows = api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        if not isinstance(rows, list):
            raise APIError(f"{path}: invalid listing")
        result.extend(rows)
        if len(rows) < 100:
            return result
        page += 1


def assignees_for(repo):
    return next((people for people, repos in GROUPS if repo in repos), DEFAULT)


def assignable(repo, user):
    try:  # a login that left the organisation would make GitHub refuse the whole issue
        api(f"repos/{ORG}/{repo}/assignees/{user}")
        return True
    except APIError as exc:
        if "(HTTP 404)" in str(exc):
            return False
        raise  # an outage is not a "no"


def own_issue(issue):
    return ("pull_request" not in issue and issue.get("user", {}).get("type") == "Bot"
            and issue.get("user", {}).get("login") == WATCHER_BOT
            and f"<!-- {MARKER}:" in (issue.get("body") or ""))


def reported_run(issue):
    match = re.search(rf"<!-- {MARKER}-run:(\d+) -->", issue.get("body") or "")
    return int(match.group(1)) if match else None


def issue_body(workflow, run, failed_jobs):
    jobs = ", ".join(failed_jobs) or "none listed; the run did not start its jobs"
    return "\n".join([
        f"<!-- {MARKER}:{workflow['path']} -->",
        f"<!-- {MARKER}-run:{run['id']} -->",
        f"The newest scheduled run of `{workflow['name']}` ended with `{run['conclusion']}`: "
        f"{run['html_url']} ({run['created_at']}).",
        "",
        f"Failed jobs: {jobs}.",
        "",
        "GitHub tells only the person who last changed the cron line about a failed scheduled run, so this "
        "issue tells the team. It closes when a later scheduled run of this workflow passes.",
    ])


def scheduled_runs(repo):
    """Yield (workflow, newest completed scheduled run) for each active workflow file of repo."""
    for workflow in api(f"repos/{ORG}/{repo}/actions/workflows?per_page=100")["workflows"]:
        if (workflow["state"] != "active" or not workflow["path"].startswith(".github/workflows/")
                or (repo == ".github" and workflow["path"] == SELF_PATH)):
            continue
        runs = api(f"repos/{ORG}/{repo}/actions/workflows/{workflow['id']}/runs"
                   "?event=schedule&status=completed&per_page=1")["workflow_runs"]
        if runs:
            yield workflow, runs[0]


def failed_jobs(repo, run):
    jobs = api(f"repos/{ORG}/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")["jobs"]
    return [job["name"] for job in jobs if job["conclusion"] in FAILED]


def plan(runs, issues):
    """Return (kind, workflow, run, issue) for one repository: open, comment or close."""
    open_issues = {}
    for issue in issues:
        mark = re.search(rf"<!-- {MARKER}:(\S+) -->", issue.get("body") or "")
        if own_issue(issue) and mark:
            open_issues[mark.group(1)] = issue
    actions = []
    for workflow, run in runs:
        issue = open_issues.get(workflow["path"])
        if issue is not None and run["id"] < (reported_run(issue) or 0):
            continue  # an older run, for example while the reported one is re-run
        if run["conclusion"] in FAILED and issue is None:
            actions.append(("open", workflow, run, None))
        elif run["conclusion"] in FAILED and reported_run(issue) != run["id"]:
            actions.append(("comment", workflow, run, issue))
        elif run["conclusion"] == "success" and issue is not None:
            actions.append(("close", workflow, run, issue))
    return actions


def closed_report(repo, workflow, run):
    """True when a person closed this watcher's issue about this very run: do not open it again."""
    for issue in pages(f"repos/{ORG}/{repo}/issues?state=closed&creator={quote(WATCHER_BOT)}&since={run['created_at']}"):
        if (own_issue(issue) and f"<!-- {MARKER}:{workflow['path']} -->" in issue["body"]
                and reported_run(issue) == run["id"]):
            return True
    return False


def apply(repo, kind, workflow, run, issue):
    path = f"repos/{ORG}/{repo}/issues"
    if kind == "open":
        body = issue_body(workflow, run, failed_jobs(repo, run))
        people = [user for user in assignees_for(repo) if assignable(repo, user)]
        result = api(path, "POST", {"title": f"Scheduled run failed: {workflow['name']}", "body": body,
                                    "labels": [LABEL], "assignees": people})
        return f"opened #{result['number']}"
    number = issue["number"]
    if kind == "comment":
        jobs = failed_jobs(repo, run)
        api(f"{path}/{number}", "PATCH", {"body": issue_body(workflow, run, jobs)})
        # A comment, not only the edit: it notifies the assignees of the new failure.
        api(f"{path}/{number}/comments", "POST",
            {"body": f"Failed again: {run['html_url']} ({run['created_at']}, `{run['conclusion']}`). "
                     f"Failed jobs: {', '.join(jobs) or 'none listed'}."})
        return f"commented on #{number}"
    api(f"{path}/{number}/comments", "POST", {"body": f"The scheduled run {run['html_url']} ({run['created_at']}) passed."})
    api(f"{path}/{number}", "PATCH", {"state": "closed", "state_reason": "completed"})
    return f"closed #{number}"


def main():
    failing, actions, notes, errors = [], [], [], []
    try:
        repos = [r for r in pages(f"orgs/{ORG}/repos?type=all") if not r.get("archived") and not r.get("disabled")]
        for repo in repos:
            name = repo["name"]
            try:  # one repository that cannot be read must not stop the others
                runs = list(scheduled_runs(name))
                failing += [(name, workflow, run) for workflow, run in runs if run["conclusion"] in FAILED]
                if not repo.get("has_issues", True):
                    if any(run["conclusion"] in FAILED for _, run in runs):
                        notes.append(f"{name}: issues are off, so a failed run cannot be reported")
                    continue
                for kind, workflow, run, issue in plan(runs, pages(f"repos/{ORG}/{name}/issues?state=open&creator={quote(WATCHER_BOT)}")):
                    if kind != "close" and (name, workflow["path"], workflow["name"]) in OWN_TICKETS:
                        notes.append(f"{name} `{workflow['path']}`: in OWN_TICKETS, left to its ticket workflow")
                        continue
                    if kind == "open" and closed_report(name, workflow, run):
                        continue
                    done = f"would {kind}" if DRY_RUN else apply(name, kind, workflow, run, issue)
                    actions.append(f"{name} {workflow['path']}: {done}")
            except (APIError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                errors.append(f"{name}: {exc}")
    except (APIError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        errors.append(str(exc))
    with open(SUMMARY, "a") as stream:
        stream.write("## Scheduled runs\n\n")
        if DRY_RUN:
            stream.write("**Dry run: no issue was opened, changed or closed.**\n\n")
        stream.write(f"{len(failing)} workflow(s) whose newest scheduled run failed.\n\n")
        for name, workflow, run in failing:
            stream.write(f"- {name} `{workflow['path']}`: {run['conclusion']} {run['html_url']}\n")
        stream.write("\n### Issue actions\n\n" + ("".join(f"- {a}\n" for a in actions) or "None.\n"))
        if notes:
            stream.write("\n### Not reported\n\n" + "".join(f"- {n}\n" for n in sorted(notes)))
        if errors:
            stream.write("\n### Errors\n\n" + "".join(f"- {e}\n" for e in sorted(errors)))
    if errors and SUMMARY != "/dev/stdout":
        print("scheduled-run watch failed: " + "; ".join(sorted(errors)), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
