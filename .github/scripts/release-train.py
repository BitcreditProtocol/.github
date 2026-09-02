#!/usr/bin/env python3
"""Cut a release train: one tag and one release across every member, or none.

A train is the coordinated cut this organisation has been doing by hand since
February 2026 -- nine of them, twice as a loop in one engineer's shell. RELEASING.md
in this repository is the contract; this implements it.

What blocks a train, and what is only reported
----------------------------------------------
Blocks: a red check on any member's head, and a tag name already in use.

Reports: the shared wire-crate spread and the age of the dashboard's openapi
snapshot. Owner decision 2026-09-02. Three members build against three different
revisions of bcr-common today and nine trains have shipped anyway; refusing them all
on a risk nothing has confirmed would break a working practice, and only the teams
could clear it by editing manifests. So the number is put in front of whoever cuts
the train instead of being left for them to find.

Fail closed
-----------
If the check runs cannot be read the train is refused and the missing permission is
named. A gate that waves things through when it cannot verify is worse than no gate,
because it looks like protection.

`Dependabot` is excluded from the gate by name. It is the dependency updater
reporting on itself, not CI reporting on the code, and it fails for its own reasons
-- it is the single red check across all five members today.
"""

import base64
import datetime
import json
import os
import re
import subprocess
import sys

ORG = os.environ.get("ORG", "BitcreditProtocol")
PRODUCT = os.environ.get("PRODUCT", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "/dev/stdout")
ACTOR = os.environ.get("GITHUB_ACTOR", "unknown")

# RELEASING.md, decision 4 of 2026-09-01: membership is five and a miss is a
# finding. Adding a member is a change here AND a change to the contract.
MEMBERS = ["Wildcat", "Clowder", "Wildcat-Auxiliary", "Wildcat-deployment",
           "wildcat-dashboard-ui"]

GATE_EXCLUDE = {"Dependabot"}
BLOCKING = {"failure", "timed_out", "cancelled", "action_required", "stale"}

# The API snapshot the dashboard builds against, and the code that generates it.
SNAPSHOT_REPO, SNAPSHOT_PATH = "wildcat-dashboard-ui", "opt/wildcat/openapi.json"
API_REPO, API_PATH = "Wildcat", "crates/bcr-wdc-admin-aggregator"

WIRE_CRATE = "bcr-common"
MARKER = "bitcredit-openapi-snapshot"


def api(path, method="GET", body=None):
    cmd = ["gh", "api", "-X", method, path]
    if body is not None:
        cmd += ["--input", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       input=json.dumps(body) if body is not None else None)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def content(repo, path, ref="master"):
    d = api(f"repos/{ORG}/{repo}/contents/{path}?ref={ref}")
    if not isinstance(d, dict) or "content" not in d:
        return None
    try:
        return base64.b64decode(d["content"]).decode("utf-8", "replace")
    except Exception:
        return None


def last_commit_date(repo, path):
    d = api(f"repos/{ORG}/{repo}/commits?path={path}&per_page=1")
    if not d:
        return None
    return d[0]["commit"]["committer"]["date"]


# ------------------------------------------------------------------ gate

def head_of(repo):
    d = api(f"repos/{ORG}/{repo}/commits/master")
    return d["sha"] if d else None


def gate(repo, sha):
    """(ok, detail). ok is False when the train must not proceed for this member."""
    d = api(f"repos/{ORG}/{repo}/commits/{sha}/check-runs?per_page=100")
    if not isinstance(d, dict) or "check_runs" not in d:
        return False, "could not read check runs — the App is missing `Checks: read`"
    runs = d["check_runs"]
    if not runs:
        return False, "no check run at all on this commit — nothing verified it"
    bad = sorted({r["name"] for r in runs
                  if r.get("conclusion") in BLOCKING and r["name"] not in GATE_EXCLUDE})
    skipped = sorted({r["name"] for r in runs
                      if r.get("conclusion") in BLOCKING and r["name"] in GATE_EXCLUDE})
    running = sorted({r["name"] for r in runs if r.get("status") != "completed"})
    if bad:
        return False, "failing: " + ", ".join(f"`{b}`" for b in bad)
    if running:
        return False, "still running: " + ", ".join(f"`{r}`" for r in running)
    note = f"{len(runs)} checks green"
    if skipped:
        note += " (excluded: " + ", ".join(f"`{s}`" for s in skipped) + ")"
    return True, note


def tag_exists(repo, tag):
    return api(f"repos/{ORG}/{repo}/git/ref/tags/{tag}") is not None


# ------------------------------------------------------------------ reports

def wire_pins():
    """What revision of the wire crate each Rust member builds against."""
    out = {}
    for repo in MEMBERS:
        txt = content(repo, "Cargo.toml")
        if not txt:
            continue
        m = re.search(r"^\s*%s\s*=\s*\{([^}]*)\}" % re.escape(WIRE_CRATE), txt, re.M)
        if not m:
            continue
        body = re.sub(r"\s+", " ", m.group(1)).strip()
        patched = bool(re.search(r"^\[patch\.[^\]]*%s" % re.escape(WIRE_CRATE), txt, re.M))
        if patched:
            sub = api(f"repos/{ORG}/{repo}/contents/{WIRE_CRATE}?ref=master")
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


def snapshot_state():
    snap = last_commit_date(SNAPSHOT_REPO, SNAPSHOT_PATH)
    code = last_commit_date(API_REPO, API_PATH)
    if not snap or not code:
        return None, snap, code
    return (code > snap), snap, code


def notify_stale_snapshot(snap, code, tag, gaps):
    mark = f"<!-- {MARKER} -->"
    issues = api(f"repos/{ORG}/{SNAPSHOT_REPO}/issues?state=open&per_page=100") or []
    existing = next((i for i in issues
                     if "pull_request" not in i and mark in (i.get("body") or "")), None)
    title = f"opt/wildcat/openapi.json is older than the API it describes"
    body = (
        f"{mark}\n"
        f"`{SNAPSHOT_PATH}` was last changed **{snap[:10]}**, and "
        f"`{API_REPO}/{API_PATH}` was last changed **{code[:10]}** — so the committed "
        f"snapshot describes an older version of the admin API than the one being "
        f"released.\n\n"
        f"Noticed while cutting **`{tag}`**.\n\n"
        f"This is a comparison of commit dates, not of the specs themselves: "
        f"`{API_REPO}` generates `openapi.json` into a build artifact that expires, "
        f"so there is nothing durable to diff against. The dates are the signal that "
        f"is available without publishing the spec.\n\n"
        f"Tracked in `infrastructure#82`. Opened by `release-train` in `{ORG}/.github`; "
        f"one issue, refreshed rather than duplicated.\n"
    )
    if DRY_RUN:
        return "would " + ("update" if existing else "open")
    if existing:
        api(f"repos/{ORG}/{SNAPSHOT_REPO}/issues/{existing['number']}", "PATCH",
            {"title": title, "body": body})
        return f"updated #{existing['number']}"
    made = api(f"repos/{ORG}/{SNAPSHOT_REPO}/issues", "POST", {"title": title, "body": body})
    if not made or "number" not in made:
        gaps.append(f"could not open an issue in `{SNAPSHOT_REPO}`")
        return "FAILED"
    return f"opened #{made['number']}"


# ------------------------------------------------------------------ act

def cut(repo, tag, expected_sha, others, wire_note, snap_note, gaps):
    now = head_of(repo)
    if now != expected_sha:
        gaps.append(f"`{repo}` moved from `{expected_sha[:8]}` to `{str(now)[:8]}` "
                    f"after it was gated — refusing to tag an unverified commit")
        return "REFUSED — head moved"
    obj = api(f"repos/{ORG}/{repo}/git/tags", "POST", {
        "tag": tag, "message": f"Release {tag}", "object": expected_sha, "type": "commit"})
    if not obj or "sha" not in obj:
        gaps.append(f"could not create the tag object in `{repo}`")
        return "FAILED"
    ref = api(f"repos/{ORG}/{repo}/git/refs", "POST",
              {"ref": f"refs/tags/{tag}", "sha": obj["sha"]})
    if not ref:
        gaps.append(f"created a tag object in `{repo}` but could not push the ref")
        return "FAILED"
    body = (
        f"Part of release train **`{tag}`**, cut across "
        + ", ".join(f"`{o}`" for o in MEMBERS) + f" by @{ACTOR}.\n\n"
        f"| member | commit |\n|---|---|\n"
        + "".join(f"| `{r}` | `{s[:8]}` |\n" for r, s in others.items())
        + f"\n{wire_note}\n\n{snap_note}\n"
    )
    rel = api(f"repos/{ORG}/{repo}/releases", "POST", {
        "tag_name": tag, "name": tag, "body": body, "generate_release_notes": True})
    if not rel or "number" not in rel:
        gaps.append(f"tagged `{repo}` but could not create its release")
        return "tagged, release FAILED"
    return f"tagged and released"


# ------------------------------------------------------------------ main

def main():
    gaps = []
    if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.]+)?", PRODUCT):
        print(f"product version {PRODUCT!r} is not of the form 0.5.0 or v0.5.0",
              file=sys.stderr)
        return 1
    product = PRODUCT if PRODUCT.startswith("v") else "v" + PRODUCT
    tag = f"train/{product}-{datetime.date.today().isoformat()}"

    heads, results, blocked = {}, {}, []
    for repo in MEMBERS:
        sha = head_of(repo)
        if not sha:
            blocked.append((repo, "could not read `master`"))
            continue
        heads[repo] = sha
        if tag_exists(repo, tag):
            blocked.append((repo, f"`{tag}` already exists"))
            continue
        ok, detail = gate(repo, sha)
        results[repo] = (sha, ok, detail)
        if not ok:
            blocked.append((repo, detail))

    pins = wire_pins()
    spread = []
    keys = [k for k, v in pins.items() if v[0]]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if pins[a][0] != pins[b][0]:
                n = distance(pins[a][0], pins[b][0])
                if n:
                    spread.append((a, b, n))
    if len(set(v[0] for v in pins.values() if v[0])) <= 1:
        wire_note = f"All members that use `{WIRE_CRATE}` build against the same revision."
    else:
        worst = max((n for _, _, n in spread), default=0)
        wire_note = (f"**`{WIRE_CRATE}` is not the same revision across this train** — "
                     f"up to **{worst} commits** apart. "
                     + "; ".join(f"`{r}` at `{str(v[0])[:8]}` ({v[1]})" for r, v in sorted(pins.items()))
                     + f". Nothing tests that the encodings agree — `{WIRE_CRATE}#209`.")

    stale, snap, code = snapshot_state()
    if stale is None:
        snap_note = "Could not compare the dashboard's openapi snapshot against the API code."
        gaps.append("could not read one of the two commit dates for the openapi snapshot")
    elif stale:
        snap_note = (f"**The dashboard's `openapi.json` snapshot is older than the API code** — "
                     f"snapshot {snap[:10]}, API {code[:10]}. `infrastructure#82`.")
    else:
        snap_note = f"The dashboard's `openapi.json` snapshot is current (snapshot {snap[:10]}, API {code[:10]})."

    actions = {}
    if not blocked and not DRY_RUN:
        for repo in MEMBERS:
            others = {r: s for r, s in heads.items() if r != repo}
            actions[repo] = cut(repo, tag, heads[repo], others, wire_note, snap_note, gaps)
        if stale:
            actions["_snapshot"] = notify_stale_snapshot(snap, code, tag, gaps)
    elif not blocked and stale:
        actions["_snapshot"] = notify_stale_snapshot(snap, code, tag, gaps)

    with open(SUMMARY, "a") as f:
        w = f.write
        w(f"## Release train `{tag}`\n\n")
        if blocked:
            w("### Refused\n\nA train is all five or none, so nothing was tagged.\n\n")
            for repo, why in blocked:
                w(f"- **`{repo}`** — {why}\n")
            w("\n")
        elif DRY_RUN:
            w("**Dry run — nothing was tagged, released or opened.**\n\n")
        w("| member | commit | gate | |\n|---|---|---|---|\n")
        for repo in MEMBERS:
            sha, ok, detail = results.get(repo, ("—", False, "not reached"))
            w(f"| `{repo}` | `{str(sha)[:8]}` | {'pass' if ok else '**blocked**'} | "
              f"{detail} | \n")
        w(f"\n{wire_note}\n\n{snap_note}\n")
        if actions:
            w("\n### What was done\n\n")
            for k, v in actions.items():
                w(f"- `{k}`: {v}\n")
        if gaps:
            w("\n### Not measured, or refused\n\n")
            for g in sorted(set(gaps)):
                w(f"- {g}\n")
    return 1 if (blocked or gaps) else 0


if __name__ == "__main__":
    sys.exit(main())
