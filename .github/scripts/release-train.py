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
import time

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
IMAGE_BUILDERS = ["Wildcat", "Clowder", "Wildcat-Auxiliary", "wildcat-dashboard-ui"]

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
    running = sorted({r["name"] for r in runs
                      if r.get("status") != "completed" and r["name"] not in GATE_EXCLUDE})
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


def builds_started(tag, gaps):
    """Did tagging actually start the image builds?

    The train tags with an App installation token, and nothing in this
    organisation has ever done that before -- every historical tag-push build
    was started by a person, and the only bot in the run history is Dependabot,
    which the platform handles specially. If an App push does not fire
    `push: tags`, the train tags five repositories, publishes five releases and
    produces no images, with no error anywhere.

    So the consequence is checked rather than assumed. A missing build becomes a
    gap, which makes the run exit non-zero -- the loudest signal available
    without a second write to every release.

    Bounded and late: the tags already exist, so nothing here can undo a correct
    train. A build that is merely slow is reported as not started, which is the
    safe direction to be wrong in.
    """
    missing = []
    for repo in IMAGE_BUILDERS:
        # Filtered server-side. A tag push reports the tag as head_branch, so
        # `branch=` matches it exactly and total_count answers in one row.
        # Scanning the most recent 30 runs instead looked equivalent and was
        # not: Clowder is busy enough that a real tag-push run had already
        # fallen off the first page, and the check reported it as missing.
        q = f"repos/{ORG}/{repo}/actions/runs?event=push&branch={tag}&per_page=1"
        for attempt in range(6):
            if (api(q) or {}).get("total_count", 0) > 0:
                break
            if attempt < 5:
                time.sleep(15)
        else:
            missing.append(repo)
    for repo in missing:
        gaps.append(f"`{repo}` started no workflow run for `{tag}` within 90s — "
                    f"the tag exists but no image is being built")
    return missing


def previous_train(tag):
    """The dated tag before this one, taken across the whole train.

    Sorted by the date suffix, not by the name: `v0.10.0-2026-01-05` sorts below
    `v0.9.0-2026-02-01` as a string, and the later date is what "previous" means.
    """
    names = set()
    for m in MEMBERS:
        for r in api(f"repos/{ORG}/{m}/git/matching-refs/tags/v") or []:
            n = r.get("ref", "").replace("refs/tags/", "")
            if n != tag and re.fullmatch(r"v.+-\d{4}-\d{2}-\d{2}", n):
                names.add(n)
    return max(names, key=lambda n: n[-10:], default=None)


def migrations_at(repo, ref):
    """Migration files present at a ref, or None if the ref is not there."""
    d = api(f"repos/{ORG}/{repo}/git/trees/{ref}?recursive=1")
    if not isinstance(d, dict) or "tree" not in d:
        return None
    return {t["path"] for t in d["tree"]
            if t.get("type") == "blob"
            and re.search(r"migrations/.*\.sql$", t["path"], re.I)}


def rollback_note(repo, tag, prev):
    """One line per release saying whether rolling back is a plain redeploy.

    Rolling an image back is only simple when no migration landed in between.
    Clowder carries nine migration files and Wildcat one, and there are no
    down-migrations anywhere in the train -- so an older image can meet a newer
    schema, and nothing tests that pairing. Working that out at the moment
    something is broken is exactly the wrong time, so it is computed here and
    written into every release body, including the members with no schema at
    all: a missing line reads as a bug, not as an all-clear.
    """
    if not prev:
        return ("**Rollback:** first train under this convention — no previous "
                "train to roll back to.")
    was = migrations_at(repo, prev)
    if was is None:
        return (f"**Rollback:** `{repo}` did not carry `{prev}`, so there is "
                f"nothing to compare against.")
    now = migrations_at(repo, tag)
    if now is None:
        return f"**Rollback:** could not read `{repo}` at `{tag}`."
    new = sorted(now - was)
    if not new and not now:
        return (f"**Rollback to `{prev}`:** no schema in this repository, so it "
                f"is a plain redeploy of the older image.")
    if not new:
        return (f"**Rollback to `{prev}`:** no migration landed since, so it is "
                f"a plain redeploy of the older image.")
    return (f"**Rollback to `{prev}` is not clean** — {len(new)} migration(s) "
            f"landed since: " + ", ".join(f"`{q.rsplit('/', 1)[-1]}`" for q in new)
            + ". There are no down-migrations, so an older image would run "
              "against a newer schema.")


# ------------------------------------------------------------------ act

def cut(repo, tag, expected_sha, others, wire_note, snap_note, roll_note, gaps):
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
        + f"\n{wire_note}\n\n{snap_note}\n\n{roll_note}\n"
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
    tag = f"{product}-{datetime.date.today().isoformat()}"

    # The tag has to match the image builds' trigger or the train produces
    # nothing at all. Wildcat, Clowder, Wildcat-Auxiliary and
    # wildcat-dashboard-ui build on `push: tags: ["v*.*.*"]`, and three derive
    # the image tag with type=semver,pattern={{version}} -- which is how
    # v0.4.0-aug25 became the image 0.4.0-aug25. A name outside that glob tags
    # five repositories, publishes five releases and builds nothing, with no
    # error anywhere; Wildcat-deployment is then left with no image_tag to
    # deploy. That is exactly what the train/ namespace would have done, and it
    # is why the namespace was withdrawn before this ever ran. Asserted here so
    # an odd product string cannot bring the same failure back quietly.
    # Two conditions, and both are needed. The glob decides whether the build
    # runs at all; GitHub's * never matches /, so it is [^/]* three times over.
    # Semver decides whether type=semver can name the image once it does run --
    # a product string like 0.5.0.1 clears the glob and produces no image tag,
    # which is the same failure one step later. Found by testing the pair, not
    # by reading them.
    if not re.fullmatch(r"v[^/]*\.[^/]*\.[^/]*", tag):
        print(f"tag {tag!r} does not match the build trigger v*.*.* used by the "
              f"image-producing members — refusing to cut a train that would "
              f"build no images", file=sys.stderr)
        return 1
    if not re.fullmatch(
            r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
            r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
            r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?", tag[1:]):
        print(f"tag {tag!r} is not valid semver, so type=semver would give the "
              f"image no tag — refusing", file=sys.stderr)
        return 1

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
        if spread:
            apart = f"up to **{max(n for _, _, n in spread)} commits** apart. "
        else:
            # distance() returned None for every differing pair, so the compare call
            # failed; "0 commits apart" would read as agreement.
            apart = "how far apart could not be measured (the compare call failed). "
            gaps.append(f"could not measure how far apart the `{WIRE_CRATE}` revisions are")
        wire_note = (f"**`{WIRE_CRATE}` is not the same revision across this train** — "
                     + apart
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

    prev = previous_train(tag)

    actions = {}
    if not blocked and not DRY_RUN:
        for repo in MEMBERS:
            others = {r: s for r, s in heads.items() if r != repo}
            actions[repo] = cut(repo, tag, heads[repo], others, wire_note, snap_note,
                                rollback_note(repo, tag, prev), gaps)
        if stale:
            actions["_snapshot"] = notify_stale_snapshot(snap, code, tag, gaps)
        missing = builds_started(tag, gaps)
        actions["_builds"] = ("every image builder started a run"
                              if not missing
                              else "NO BUILD STARTED in: " + ", ".join(missing))
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
        w(f"\nPrevious train: {('`' + prev + '`') if prev else 'none — this is the first'}\n")
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
