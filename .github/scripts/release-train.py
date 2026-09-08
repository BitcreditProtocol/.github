#!/usr/bin/env python3
"""Prepare an immutable five-member release candidate, then reconcile its tags and releases.

Only dispatches cut releases. Dry runs cannot write. Resuming uses the original
Actions artifact, never current master heads. Deployment is a separate action.
"""

import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
import tempfile
from urllib.parse import quote, urlencode

ORG = os.environ.get("ORG", "BitcreditProtocol")
PRODUCT = os.environ.get("PRODUCT", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "/dev/stdout")
ACTOR = os.environ.get("GITHUB_ACTOR", "unknown")

# RELEASING.md, decision 4 of 2026-09-01: membership is five and a miss is a
# finding. Adding a member is a change here AND a change to the contract.
MEMBERS = ["Wildcat", "Clowder", "Wildcat-Auxiliary", "Wildcat-deployment",
           "wildcat-dashboard-ui"]

# The members that turn a tag into a container image, on `push: tags: v*.*.*`.
# Wildcat-deployment is deliberately absent: it carries deployment configuration
# and builds nothing, so polling it would report a missing build every train.
IMAGE_BUILDERS = {"Wildcat": "build.yml", "Clowder": "build.yml",
                  "Wildcat-Auxiliary": "build.yml", "wildcat-dashboard-ui": "release.yml"}

GATE_EXCLUDE = {"Dependabot"}
BLOCKING = {"failure", "timed_out", "cancelled", "action_required", "stale"}

# The API snapshot the dashboard builds against, and the code that generates it.
SNAPSHOT_REPO, SNAPSHOT_PATH = "wildcat-dashboard-ui", "opt/wildcat/openapi.json"
API_REPO, API_PATH = "Wildcat", "crates/bcr-wdc-admin-aggregator"

WIRE_CRATE = "bcr-common"
MARKER = "bitcredit-openapi-snapshot"
ARTIFACT = "release-train-plan"
SHA = re.compile(r"[0-9a-f]{40}")
SEMVER = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?")


class APIError(RuntimeError):
    pass


def api(path, method="GET", body=None, *, missing=False, token=None):
    env = os.environ.copy()
    if method != "GET":
        if DRY_RUN:
            raise APIError("dry-run refused a write")
        token = os.environ.get("GH_WRITE_TOKEN")
        if not token:
            raise APIError("write token is unavailable")
    if token:
        env["GH_TOKEN"] = token
    cmd = ["gh", "api", "-X", method, path]
    if body is not None:
        cmd += ["--input", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                       input=json.dumps(body) if body is not None else None, timeout=60)
    if r.returncode != 0:
        if missing and re.search(r"\(HTTP 404\)", r.stderr):
            return None
        raise APIError(f"{method} {path}: {r.stderr.strip()[:500] or 'request failed'}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise APIError(f"{method} {path}: invalid JSON response") from None


def pages(path, key=None, *, token=None):
    out, page = [], 1
    while True:
        data = api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}", token=token)
        rows = data.get(key) if isinstance(data, dict) and key else data
        if not isinstance(rows, list):
            raise APIError(f"{path}: invalid list response")
        out.extend(rows)
        if len(rows) < 100:
            return out
        page += 1


def content(repo, path, ref="master"):
    d = api(f"repos/{ORG}/{repo}/contents/{path}?ref={quote(ref, safe='')}", missing=True)
    if d is None:
        return None
    if not isinstance(d, dict) or "content" not in d:
        raise APIError(f"{repo}/{path}: invalid contents response")
    try:
        return base64.b64decode(d["content"]).decode("utf-8", "replace")
    except Exception:
        raise APIError(f"{repo}/{path}: invalid content encoding") from None


def last_commit_date(repo, path, ref):
    d = api(f"repos/{ORG}/{repo}/commits?" + urlencode(dict(path=path, sha=ref, per_page=1)))
    if not d:
        return None
    return d[0]["commit"]["committer"]["date"]


# ------------------------------------------------------------------ gate

def head_of(repo):
    d = api(f"repos/{ORG}/{repo}/commits/master")
    sha = d.get("sha") if isinstance(d, dict) else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise APIError(f"{repo}: invalid master SHA")
    return sha


def gate(repo, sha):
    """(ok, detail). ok is False when the train must not proceed for this member."""
    suites = pages(f"repos/{ORG}/{repo}/commits/{sha}/check-suites", "check_suites")
    latest = {}
    for suite in suites:
        if suite.get("head_branch") != "master" or suite.get("head_sha") != sha:
            continue
        for run in pages(f"repos/{ORG}/{repo}/check-suites/{suite['id']}/check-runs?filter=all", "check_runs"):
            if run.get("head_sha") != sha or not isinstance(run.get("name"), str):
                raise APIError(f"{repo}: invalid check run")
            key = (run.get("app", {}).get("id"), run["name"])
            # Creation order matters: a newer queued check has no started_at yet.
            # Execution timestamps can also put an older, delayed run last.
            order = run.get("id")
            if not isinstance(order, int) or order <= 0:
                raise APIError(f"{repo}: invalid check-run ID")
            if key not in latest or order > latest[key][0]:
                latest[key] = (order, run)
    runs = [r for _, r in latest.values() if r["name"] not in GATE_EXCLUDE]
    if not runs:
        return False, "no check run at all on this commit — nothing verified it"
    bad = sorted({r["name"] for r in runs
                  if r.get("conclusion") in BLOCKING and r["name"] not in GATE_EXCLUDE})
    running = sorted({r["name"] for r in runs
                      if r.get("status") != "completed" and r["name"] not in GATE_EXCLUDE})
    if bad:
        return False, "failing: " + ", ".join(f"`{b}`" for b in bad)
    if running:
        return False, "still running: " + ", ".join(f"`{r}`" for r in running)
    return True, f"{len(runs)} checks green (Dependabot excluded)"


def tag_commit(repo, tag):
    ref = api(f"repos/{ORG}/{repo}/git/ref/tags/{quote(tag, safe='')}", missing=True)
    if ref is None:
        return None
    obj = ref.get("object", {})
    if obj.get("type") != "tag":
        raise APIError(f"{repo}/{tag}: existing tag is not annotated")
    for _ in range(10):
        if not isinstance(obj.get("sha"), str) or not SHA.fullmatch(obj["sha"]):
            break
        if obj.get("type") == "commit":
            return obj["sha"]
        if obj.get("type") != "tag":
            break
        obj = api(f"repos/{ORG}/{repo}/git/tags/{obj['sha']}").get("object", {})
    raise APIError(f"{repo}/{tag}: cannot resolve annotated tag to a commit")


# ------------------------------------------------------------------ reports

def wire_pins(heads):
    """What revision of the wire crate each Rust member builds against."""
    out = {}
    for repo in MEMBERS:
        txt = content(repo, "Cargo.toml", heads[repo])
        if not txt:
            continue
        m = re.search(r"^\s*%s\s*=\s*\{([^}]*)\}" % re.escape(WIRE_CRATE), txt, re.M)
        if not m:
            continue
        body = re.sub(r"\s+", " ", m.group(1)).strip()
        patched = bool(re.search(r"^\[patch\.[^\]]*%s" % re.escape(WIRE_CRATE), txt, re.M))
        if patched:
            sub = api(f"repos/{ORG}/{repo}/contents/{WIRE_CRATE}?ref={heads[repo]}")
            sha = sub.get("sha") if isinstance(sub, dict) else None
            out[repo] = (sha, "submodule, the declared tag is overridden by [patch]")
        else:
            r = re.search(r'rev\s*=\s*"?([0-9a-f]{7,40})', body)
            if r:
                out[repo] = (r.group(1), "rev")
            else:
                t = re.search(r'tag\s*=\s*"([^"]+)"', body)
                out[repo] = ((t.group(1) if t else None), "tag")
    return out


def distance(a, b):
    """How far apart two commits are, in commits, in both directions.

    `compare` is directional: ahead_by counts what b has that a does not, and it
    is 0 whenever b is simply an ancestor of a. Taking ahead_by alone reported
    two revisions 25 commits apart as 6, which understated the one number this
    report exists to show. The distance is ahead_by + behind_by.
    """
    d = api(f"repos/{ORG}/{WIRE_CRATE}/compare/{a}...{b}")
    if not isinstance(d, dict) or "ahead_by" not in d:
        return None
    return d["ahead_by"] + d.get("behind_by", 0)


def snapshot_state(heads):
    snap = last_commit_date(SNAPSHOT_REPO, SNAPSHOT_PATH, heads[SNAPSHOT_REPO])
    code = last_commit_date(API_REPO, API_PATH, heads[API_REPO])
    if not snap or not code:
        return None, snap, code
    return (code > snap), snap, code


def notify_stale_snapshot(snap, code, tag, existing):
    body = (f"<!-- {MARKER} -->\nThe dashboard snapshot changed {snap}, while "
            f"the API code changed {code}. Observed in train `{tag}`.\n\n"
            "This is a commit-date signal, not a comparison of the generated specs. "
            "Tracked in infrastructure#82.")
    payload = {"title": "Refresh the dashboard OpenAPI snapshot", "body": body}
    path = f"repos/{ORG}/{SNAPSHOT_REPO}/issues"
    method = "POST"
    if existing:
        path += f"/{existing['number']}"
        method = "PATCH"
    result = api(path, method, payload)
    if not isinstance(result, dict) or not isinstance(result.get("number"), int):
        raise APIError("invalid snapshot issue response")
    return f"snapshot issue #{result['number']}"


def builds_started(plan, gaps):
    started = []
    for repo, workflow in IMAGE_BUILDERS.items():
        query = urlencode(dict(event="push", branch=plan["tag"],
                               head_sha=plan["heads"][repo], per_page=1))
        for attempt in range(7):
            try:
                result = api(f"repos/{ORG}/{repo}/actions/workflows/{workflow}/runs?{query}")
                runs = result.get("workflow_runs") if isinstance(result, dict) else None
                if not isinstance(runs, list):
                    raise APIError(f"{repo}: invalid workflow-run response")
                match = next((r for r in runs if r.get("head_sha") == plan["heads"][repo]
                              and r.get("head_branch") == plan["tag"] and r.get("event") == "push"), None)
                if match:
                    started.append(repo)
                    if match.get("conclusion") in BLOCKING:
                        gaps.append(f"{repo}/{workflow}: build {match['conclusion']} — {match.get('html_url', '')}")
                    break
            except (APIError, subprocess.TimeoutExpired) as exc:
                gaps.append(f"{repo}/{workflow}: could not verify build start: {exc}")
                break
            if attempt < 6:
                time.sleep(15)
        else:
            gaps.append(f"{repo}/{workflow}: no matching image build started within 90s")
    return started


def previous_train(tag):
    candidates = {}
    for repo in MEMBERS:
        refs = api(f"repos/{ORG}/{repo}/git/matching-refs/tags/v")
        if not isinstance(refs, list):
            raise APIError(f"{repo}: invalid train-ref response")
        for ref in refs:
            name = ref.get("ref", "").removeprefix("refs/tags/")
            if name != tag and re.fullmatch(r"v.+-\d{4}-\d{2}-\d{2}", name):
                candidates.setdefault(name, []).append((repo, ref.get("object", {})))
    if not candidates:
        return None
    # All new train dates are UTC. Only the latest date needs tag-object reads.
    day = max(n[-10:] for n in candidates)
    dated = []
    for name, refs in candidates.items():
        if name[-10:] != day:
            continue
        for repo, obj in refs:
            if obj.get("type") != "tag" or not SHA.fullmatch(str(obj.get("sha", ""))):
                raise APIError(f"{repo}/{name}: previous train is not an annotated tag")
            data = api(f"repos/{ORG}/{repo}/git/tags/{obj['sha']}")
            timestamp = data.get("tagger", {}).get("date")
            if not timestamp:
                raise APIError(f"{repo}/{name}: missing tagger date")
            dated.append((datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")), name))
    return max(dated)[1]


def migrations_at(repo, ref):
    data = api(f"repos/{ORG}/{repo}/git/trees/{quote(ref, safe='')}?recursive=1")
    if not isinstance(data, dict) or not isinstance(data.get("tree"), list) or data.get("truncated") is not False:
        raise APIError(f"{repo}/{ref}: incomplete migration tree")
    return {entry["path"]: entry["sha"] for entry in data["tree"]
            if entry.get("type") == "blob" and re.search(r"migrations/.*\.sql$", entry["path"], re.I)}


def rollback_note(repo, sha, prev):
    if not prev:
        return "**Rollback:** no previous dated train exists under this convention."
    was = migrations_at(repo, prev)
    now = migrations_at(repo, sha)
    added = sorted(now.keys() - was.keys())
    removed = sorted(was.keys() - now.keys())
    changed = sorted(path for path in now.keys() & was.keys() if now[path] != was[path])
    details = "; ".join(f"{name}: " + ", ".join(f"`{p}`" for p in paths)
                        for name, paths in (("added", added), ("changed", changed), ("removed", removed)) if paths)
    if details:
        return f"**Rollback to `{prev}`: SQL migrations differ** — {details}. Review schema/data compatibility before deploying an older image."
    return f"**Rollback to `{prev}`: no SQL migration-file changes detected.** This does not establish application or data compatibility."


def validate_tag(tag):
    if not isinstance(tag, str) or not tag.startswith("v") or not SEMVER.fullmatch(tag[1:]):
        raise ValueError("train tag must be valid v-prefixed SemVer")
    if not re.search(r"-\d{4}-\d{2}-\d{2}$", tag):
        raise ValueError("train tag must end in an ISO date")
    datetime.date.fromisoformat(tag[-10:])


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("unsupported release plan")
    if plan.get("repository") != f"{ORG}/.github":
        raise ValueError("release plan belongs to another repository")
    validate_tag(plan.get("tag"))
    heads = plan.get("heads")
    if not isinstance(heads, dict) or set(heads) != set(MEMBERS):
        raise ValueError("release plan must contain exactly the five members")
    if any(not isinstance(s, str) or not SHA.fullmatch(s) for s in heads.values()):
        raise ValueError("release plan contains an invalid commit SHA")
    if plan.get("previous_tag") is not None:
        validate_tag(plan["previous_tag"])
        if plan["previous_tag"] == plan["tag"]:
            raise ValueError("release plan cannot be its own predecessor")
    actor = plan.get("tagger")
    if not isinstance(actor, dict) or not re.fullmatch(r"[A-Za-z0-9-]+", str(actor.get("name", ""))):
        raise ValueError("release plan has an invalid initiator")
    if not re.fullmatch(r"\d+\+" + re.escape(actor["name"]) + r"@users\.noreply\.github\.com", str(actor.get("email", ""))):
        raise ValueError("release plan has an invalid tagger email")
    when = datetime.datetime.fromisoformat(str(actor.get("date", "")).replace("Z", "+00:00"))
    if when.utcoffset() != datetime.timedelta(0) or when.date().isoformat() != plan["tag"][-10:]:
        raise ValueError("release plan tag/date must agree in UTC")
    if not re.fullmatch(r"[1-9]\d*", str(plan.get("run_id", ""))):
        raise ValueError("release plan must identify its original Actions run")
    return plan


def load_plan(path):
    if path.stat().st_size > 65536:
        raise ValueError("release plan is too large")
    return validate_plan(json.loads(path.read_text()))


def artifact_token():
    token = os.environ.get("GH_ARTIFACT_TOKEN")
    if not token:
        raise APIError("native Actions token is unavailable for release-plan storage")
    return token


def restore_plan(run_id, destination):
    if not re.fullmatch(r"[1-9]\d*", run_id):
        raise ValueError("resume_run_id must be a positive Actions run ID")
    token = artifact_token()
    run = api(f"repos/{ORG}/.github/actions/runs/{run_id}", token=token)
    if (run.get("event") != "workflow_dispatch" or run.get("head_branch") != "master"
            or run.get("path") != ".github/workflows/release-train.yml"):
        raise ValueError("resume source must be the release workflow dispatched from master")
    artifacts = pages(f"repos/{ORG}/.github/actions/runs/{run_id}/artifacts", "artifacts", token=token)
    matches = [a for a in artifacts if a.get("name") == ARTIFACT and not a.get("expired")]
    if len(matches) != 1:
        raise ValueError("original release plan is missing, expired or ambiguous; refusing new heads")
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GH_TOKEN=token)
        subprocess.run(["gh", "run", "download", run_id, "--repo", f"{ORG}/.github",
                        "--name", ARTIFACT, "--dir", tmp], env=env, check=True, timeout=60,
                       stdout=subprocess.DEVNULL)
        source = Path(tmp) / "release-train-plan.json"
        plan = load_plan(source)
        if str(plan["run_id"]) != run_id:
            raise ValueError("release plan does not belong to the requested run")
        destination.write_bytes(source.read_bytes())
    return plan, str(matches[0]["id"])


def release_for(repo, tag):
    result = api(f"repos/{ORG}/{repo}/releases/tags/{quote(tag, safe='')}", missing=True)
    if result is not None and (not isinstance(result, dict) or not isinstance(result.get("id"), int)
                               or result.get("tag_name") != tag):
        raise APIError(f"{repo}/{tag}: invalid release response")
    return result


def preflight(plan):
    results = {}
    for repo, sha in plan["heads"].items():
        existing = tag_commit(repo, plan["tag"])
        if existing is not None and existing != sha:
            raise APIError(f"{repo}/{plan['tag']}: existing tag points at {existing}, expected {sha}")
        ok, detail = gate(repo, sha)
        results[repo] = detail
        if not ok:
            raise APIError(f"{repo}@{sha}: {detail}")
        release_for(repo, plan["tag"])
    return results


def reports(plan, gaps):
    heads = plan["heads"]
    wire = "Shared wire-crate revisions could not be measured."
    try:
        pins = wire_pins(heads)
        if any(not value[0] for value in pins.values()):
            raise APIError("one shared-crate pin could not be resolved")
        revisions = {p[0] for p in pins.values()}
        if len(revisions) <= 1:
            wire = "All measured shared wire-crate pins agree: " + str(next(iter(revisions), "none"))
        else:
            distances = [distance(a, b) for i, a in enumerate(sorted(revisions))
                         for b in sorted(revisions)[i+1:]]
            wire = "**Shared wire-crate pins differ**: " + "; ".join(
                f"`{repo}` at `{pin[0]}` ({pin[1]})" for repo, pin in sorted(pins.items()))
            wire += f". Maximum measured distance: {max(distances)} commits. No cross-version wire compatibility test is implied."
    except (APIError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        gaps.append(f"shared wire-crate report: {exc}")
    snapshot = "OpenAPI snapshot comparison could not be measured."
    notice = None
    try:
        stale, snap, code = snapshot_state(heads)
        if stale is None:
            raise APIError("one OpenAPI source commit date is missing")
        snapshot = f"Dashboard OpenAPI snapshot: {snap}; API source: {code}. " + (
            "**Snapshot is older by commit date; review it.**" if stale else "Snapshot is not older by commit date.")
        if stale:
            issues = pages(f"repos/{ORG}/{SNAPSHOT_REPO}/issues?state=open")
            existing = next((i for i in issues if "pull_request" not in i
                             and f"<!-- {MARKER} -->" in (i.get("body") or "")), None)
            notice = (snap, code, existing)
    except (APIError, subprocess.TimeoutExpired) as exc:
        gaps.append(f"OpenAPI report: {exc}")
    rollback = {}
    for repo, sha in heads.items():
        try:
            rollback[repo] = rollback_note(repo, sha, plan["previous_tag"])
        except (APIError, subprocess.TimeoutExpired) as exc:
            rollback[repo] = f"**Rollback: not measured.** {exc}"
            gaps.append(f"{repo} rollback: {exc}")
    return wire, snapshot, rollback, notice


def cut(repo, plan, wire, snapshot, rollback):
    tag, sha = plan["tag"], plan["heads"][repo]
    existing = tag_commit(repo, tag)
    if existing is not None and existing != sha:
        raise APIError(f"{repo}/{tag}: tag conflict")
    if existing is None:
        try:
            obj = api(f"repos/{ORG}/{repo}/git/tags", "POST", {
                "tag": tag, "message": f"Release {tag}", "object": sha, "type": "commit",
                "tagger": plan["tagger"]})
            if not isinstance(obj, dict) or not SHA.fullmatch(str(obj.get("sha", ""))):
                raise APIError(f"{repo}: invalid tag-object response")
            api(f"repos/{ORG}/{repo}/git/refs", "POST", {"ref": f"refs/tags/{tag}", "sha": obj["sha"]})
        except (APIError, subprocess.TimeoutExpired):
            # A lost response is not proof that the write failed. Never blindly retry.
            if tag_commit(repo, tag) != sha:
                raise
    if tag_commit(repo, tag) != sha:
        raise APIError(f"{repo}/{tag}: tag readback does not match the candidate")
    if release_for(repo, tag) is None:
        body = (f"Release train **`{tag}`**, initiated by @{plan['tagger']['name']}.\n\n"
                f"Candidate: https://github.com/{ORG}/.github/actions/runs/{plan['run_id']}\n\n"
                "| member | commit |\n|---|---|\n"
                + "".join(f"| `{r}` | [{s}](https://github.com/{ORG}/{r}/commit/{s}) |\n"
                          for r, s in plan["heads"].items())
                + f"\n{wire}\n\n{snapshot}\n\n{rollback}\n")
        try:
            release = api(f"repos/{ORG}/{repo}/releases", "POST", {
                "tag_name": tag, "name": tag, "body": body, "generate_release_notes": True})
            if not isinstance(release, dict) or not isinstance(release.get("id"), int):
                raise APIError(f"{repo}: invalid release creation response")
        except (APIError, subprocess.TimeoutExpired):
            if release_for(repo, tag) is None:
                raise
    release = release_for(repo, tag)
    if release is None:
        raise APIError(f"{repo}/{tag}: release absent after creation")
    return f"tag verified at {sha}; release id {release['id']}"


def output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as stream:
            stream.write(f"{name}={value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", type=Path)
    mode.add_argument("--apply", type=Path)
    args = parser.parse_args(argv)
    plan, checks, actions, gaps = None, {}, {}, []
    wire = snapshot = ""
    failed = False
    try:
        if args.prepare:
            resume = os.environ.get("RESUME_RUN_ID", "").strip()
            if not resume and int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")) > 1:
                resume = os.environ.get("GITHUB_RUN_ID", "")
            if resume:
                plan, artifact_id = restore_plan(resume, args.prepare)
                output("artifact_id", artifact_id)
                output("new_plan", "false")
            else:
                when = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
                product = PRODUCT.removeprefix("v")
                tag = f"v{product}-{when.date().isoformat()}"
                validate_tag(tag)
                plan = validate_plan({
                    "schema_version": 1, "repository": f"{ORG}/.github",
                    "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                    "tag": tag, "heads": {repo: head_of(repo) for repo in MEMBERS},
                    "previous_tag": previous_train(tag),
                    "tagger": {"name": ACTOR,
                               "email": f"{os.environ.get('GITHUB_ACTOR_ID', '0')}+{ACTOR}@users.noreply.github.com",
                               "date": when.isoformat().replace("+00:00", "Z")}})
                # A new dispatch must never silently adopt somebody else's dated cut.
                for repo in MEMBERS:
                    if tag_commit(repo, tag) is not None:
                        raise APIError(f"{repo}/{tag}: tag already exists; resume the original run or choose a new product suffix")
                checks = preflight(plan)
                args.prepare.write_text(json.dumps(plan, indent=2) + "\n")
                output("new_plan", "true")
            checks = preflight(plan) if not checks else checks
            wire, snapshot, _, _ = reports(plan, gaps)
        else:
            plan = load_plan(args.apply)
            artifact_id = os.environ.get("PLAN_ARTIFACT_ID", "")
            if not re.fullmatch(r"[1-9]\d*", artifact_id):
                raise ValueError("a successfully stored candidate artifact is required before any write")
            artifact = api(f"repos/{ORG}/.github/actions/artifacts/{artifact_id}", token=artifact_token())
            if (artifact.get("expired") or artifact.get("name") != ARTIFACT
                    or str(artifact.get("workflow_run", {}).get("id")) != str(plan["run_id"])):
                raise ValueError("candidate artifact does not match the original run")
            with tempfile.TemporaryDirectory() as tmp:
                canonical, stored_id = restore_plan(str(plan["run_id"]), Path(tmp) / "release-train-plan.json")
            if canonical != plan or stored_id != artifact_id:
                raise ValueError("local candidate differs from its immutable original artifact")
            checks = preflight(plan)
            wire, snapshot, rollback, notice = reports(plan, gaps)
            if not DRY_RUN:
                for repo in MEMBERS:
                    try:
                        actions[repo] = cut(repo, plan, wire, snapshot, rollback[repo])
                    except (APIError, subprocess.TimeoutExpired) as exc:
                        actions[repo] = f"STOPPED: {exc}"
                        raise
                if notice:
                    try:
                        actions["OpenAPI"] = notify_stale_snapshot(notice[0], notice[1], plan["tag"], notice[2])
                    except (APIError, subprocess.TimeoutExpired) as exc:
                        gaps.append(f"snapshot notification: {exc}")
                actions["Image builds"] = ", ".join(builds_started(plan, gaps))
    except (APIError, ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        gaps.append(str(exc))
        failed = True
    finally:
        with open(SUMMARY, "a") as stream:
            stream.write(f"## Release train {plan['tag'] if plan else 'preparation'}\n\n")
            stream.write("Checks exclude Dependabot by name. A started build is not a successful deployment.\n\n")
            if args.prepare or DRY_RUN:
                stream.write("**Preview: no tags, releases or issues were written.**\n\n")
            if plan:
                stream.write(f"Original candidate run: {plan['run_id']}; previous train: {plan['previous_tag'] or 'none'}.\n\n")
                for repo, sha in plan["heads"].items():
                    stream.write(f"- `{repo}` at `{sha}`: {checks.get(repo, 'not verified on this attempt')}\n")
            stream.write(f"\n{wire}\n\n{snapshot}\n")
            for name, action in actions.items():
                stream.write(f"- {name}: {action}\n")
            if gaps:
                stream.write("\n### Not measured or stopped\n\n")
                for gap in sorted(set(gaps)):
                    stream.write(f"- {gap}\n")
    # Diagnostic gaps are visible but do not add wire/OpenAPI admission gates.
    return 1 if failed or (args.apply and gaps) else 0


if __name__ == "__main__":
    sys.exit(main())
