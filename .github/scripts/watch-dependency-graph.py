#!/usr/bin/env python3
"""Watch the intra-organisation dependency graph and tell a consumer when its pin
is behind the producer's newest release.

The graph is DERIVED from manifests on every run, never listed here, so a new
consumer or a new dependency is covered without touching this file.

Three kinds of pin, deliberately treated differently:

  exact     `=0.5.15`, `tag = "v0.5.15"`, `ref: v0.9.12`
            Behind means somebody has to act. This is the only kind that opens
            an issue.
  range     `^0.5`, `^0.1.14-bugfix`
            Resolves on the next install, so nobody has to do anything.
            Reported as a metric.
  patched   declared with a tag, then overridden by a `[patch]` section
            The declared version is dead text: cargo builds something else. Moving
            the pin would change nothing, so saying "you are behind" would send
            somebody to do useless work. Reported, with the warning that the
            manifest is misleading. Wildcat/bcr-common is exactly this today.
  revision  `rev = "e24040d"`, a submodule sha
            Behind by a commit count, but the producer's tags may not describe
            what consumers use at all -- which is the case for bcr-common today,
            tracked in bcr-common#219. Reported as a metric, so this does not
            open issues nothing can close.

Unlike prune-package-versions, the SCHEDULE here does act: an issue is cheap and
closeable, where a deleted package version cannot be undone. dry_run stays the
default for a manual run so a change can be inspected before it speaks.

Idempotent: one issue per (consumer, dependency). The marker below is searched in
the bodies of that repository's open issues, so a hundred runs produce one issue
and the body is refreshed rather than duplicated.
"""

import json
import os
import re
import subprocess
import sys

ORG = os.environ.get("ORG", "BitcreditProtocol")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
EVENT = os.environ.get("GITHUB_EVENT_NAME", "")
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "/dev/stdout")
MARKER = "bitcredit-dependency-watch"

MANIFESTS = ("package.json", "Cargo.toml", "pubspec.yaml")


def api(path, method="GET", body=None):
    """Return parsed JSON, or None when the call fails. Never raises on a 404."""
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


def content(repo, path, ref):
    d = api(f"repos/{ORG}/{repo}/contents/{path}?ref={ref}")
    if not isinstance(d, dict) or "content" not in d:
        return None
    import base64
    try:
        return base64.b64decode(d["content"]).decode("utf-8", "replace")
    except Exception:
        return None


def active_repos():
    out, page = [], 1
    while True:
        d = api(f"orgs/{ORG}/repos?per_page=100&page={page}")
        if not d:
            break
        out += [(r["name"], r["default_branch"], r["fork"]) for r in d if not r["archived"]]
        if len(d) < 100:
            break
        page += 1
    return sorted(out)


# ---------------------------------------------------------------- graph

def edges_from(repo, branch, kind, txt):
    """Every dependency in `txt` that points at something this organisation owns."""
    out = []
    if kind == "package.json":
        try:
            d = json.loads(txt)
        except Exception:
            return out
        for sect in ("dependencies", "devDependencies", "peerDependencies"):
            for name, spec in (d.get(sect) or {}).items():
                if not isinstance(spec, str):
                    continue
                if re.search(r"bitcredit", name, re.I) or ORG in spec:
                    out.append((repo, branch, name, spec, npm_kind(spec)))
    elif kind == "Cargo.toml":
        patched = patched_names(txt)
        for m in re.finditer(r"^\s*([A-Za-z0-9_-]+)\s*=\s*\{([^}]*%s[^}]*)\}" % ORG, txt, re.M):
            name = m.group(1)
            body = re.sub(r"\s+", " ", m.group(2)).strip()
            k = "patched" if name in patched else cargo_kind(body)
            out.append((repo, branch, name, body, k))
    elif kind == "pubspec.yaml":
        for m in re.finditer(r"^  ([A-Za-z0-9_]+):\s*\n\s+git:\s*\n((?:\s+\S+:.*\n)+)", txt, re.M):
            if ORG not in m.group(2):
                continue
            body = re.sub(r"\s+", " ", m.group(2)).strip()
            out.append((repo, branch, m.group(1), body, "exact" if "ref:" in body else "range"))
    return out


def npm_kind(spec):
    return "exact" if re.match(r"^=?\d+\.\d+\.\d+", spec.strip()) and not spec.strip().startswith(("^", "~", ">", "<")) else "range"


def patched_names(txt):
    """Dependencies this manifest overrides with a [patch] section.

    A declared tag under a [patch] override is not what cargo builds, so the
    declared version says nothing about the code. Reporting it as behind would
    send somebody to change a line that has no effect.
    """
    names = set()
    for m in re.finditer(r"^\[patch\.[^\]]+\]\s*\n((?:(?!^\[).*\n)*)", txt, re.M):
        for line in m.group(1).splitlines():
            e = re.match(r"\s*([A-Za-z0-9_-]+)\s*=", line)
            if e:
                names.add(e.group(1))
    return names


def cargo_kind(body):
    if "rev" in body:
        return "revision"
    if "tag" in body:
        return "exact"
    return "range"


def pinned_version(spec, kind):
    if kind != "exact":
        return None
    m = re.search(r'(?:tag|ref)\s*[:=]\s*"?v?([0-9][^"\s,}]*)', spec)
    if m:
        return m.group(1)
    m = re.match(r"^=?v?([0-9][0-9A-Za-z.+-]*)$", spec.strip())
    return m.group(1) if m else None


def producer_of(dep_name, spec):
    """Which repository publishes this dependency."""
    m = re.search(r"%s/([A-Za-z0-9._-]+?)(?:\.git)?[\"/ ,]" % ORG, spec + " ")
    if m:
        return m.group(1)
    return NPM_OWNER.get(dep_name)


NPM_OWNER = {
    "@bitcredit/bcr-ebill-wasm": "Bitcredit-Core",
    "@bitcredit/ui-library": "ui",
    "@bitcreditprotocol/ui-library": "ui",
}


# ---------------------------------------------------------------- issues

def latest_release(repo, cache={}):
    if repo not in cache:
        d = api(f"repos/{ORG}/{repo}/releases?per_page=1")
        cache[repo] = d[0]["tag_name"] if d else None
    return cache[repo]


def open_issues(repo, cache={}):
    if repo not in cache:
        d = api(f"repos/{ORG}/{repo}/issues?state=open&per_page=100")
        cache[repo] = [i for i in (d or []) if "pull_request" not in i]
    return cache[repo]


def marker_for(dep):
    return f"<!-- {MARKER}:{dep} -->"


def notify(consumer, dep, pin, producer, newest, gaps):
    mark = marker_for(dep)
    existing = next((i for i in open_issues(consumer)
                     if mark in (i.get("body") or "")), None)
    title = f"{dep} is pinned to {pin}, and {producer} has released {newest}"
    body = (
        f"{mark}\n"
        f"`{consumer}` pins **`{dep}`** at **`{pin}`**, and "
        f"[`{producer}`](https://github.com/{ORG}/{producer}/releases/tag/{newest}) "
        f"has released **`{newest}`**.\n\n"
        f"The pin is exact, so nothing moves it on its own — this needs a deliberate "
        f"change, and often a code change beside it, which is why this is an issue "
        f"rather than a pull request.\n\n"
        f"Opened by `watch-dependency-graph` in `{ORG}/.github`. The graph is read "
        f"from manifests on every run, so this issue appears once per pin and its "
        f"body is refreshed rather than duplicated. Close it when the pin moves.\n"
    )
    if DRY_RUN:
        return "would " + ("update" if existing else "open")
    if existing:
        api(f"repos/{ORG}/{consumer}/issues/{existing['number']}",
            "PATCH", {"title": title, "body": body})
        return f"updated #{existing['number']}"
    created = api(f"repos/{ORG}/{consumer}/issues", "POST",
                  {"title": title, "body": body})
    if not created or "number" not in created:
        gaps.append(f"could not open an issue in `{consumer}` — the App may lack Issues: write there")
        return "FAILED"
    return f"opened #{created['number']}"


# ---------------------------------------------------------------- main

def main():
    if EVENT and EVENT != "workflow_dispatch" and DRY_RUN is False:
        pass  # the schedule is allowed to act here; see the module docstring

    repos = active_repos()
    if not repos:
        print("could not list repositories — refusing to report a clean graph", file=sys.stderr)
        return 1

    edges, gaps = [], []
    for name, default_branch, is_fork in repos:
        if is_fork:
            continue
        branches = [default_branch] + (["dev"] if default_branch != "dev" else [])
        for br in branches:
            if br != default_branch and not api(f"repos/{ORG}/{name}/branches/{br}"):
                continue
            for kind in MANIFESTS:
                txt = content(name, kind, br)
                if txt:
                    edges += edges_from(name, br, kind, txt)

    behind, ranges, revisions, current, patched = [], [], [], [], []
    for consumer, branch, dep, spec, kind in edges:
        producer = producer_of(dep, spec)
        if not producer:
            gaps.append(f"could not tell which repository publishes `{dep}` (in `{consumer}`)")
            continue
        newest = latest_release(producer)
        if kind == "patched":
            patched.append((consumer, branch, dep, spec, producer, newest))
        elif kind == "range":
            ranges.append((consumer, branch, dep, spec, producer, newest))
        elif kind == "revision":
            revisions.append((consumer, branch, dep, spec, producer, newest))
        else:
            pin = pinned_version(spec, kind)
            if not pin or not newest:
                gaps.append(f"could not read a version from `{dep}` in `{consumer}` (`{spec[:40]}`)")
            elif pin != newest.lstrip("v"):
                behind.append((consumer, branch, dep, pin, producer, newest))
            else:
                current.append((consumer, branch, dep, pin, producer))

    seen, actions = set(), []
    for consumer, branch, dep, pin, producer, newest in behind:
        if (consumer, dep) in seen:
            continue
        seen.add((consumer, dep))
        actions.append((consumer, dep, pin, producer, newest, notify(consumer, dep, pin, producer, newest, gaps)))

    with open(SUMMARY, "a") as f:
        w = f.write
        w("## Dependency graph\n\n")
        w(f"{len(edges)} intra-organisation edges across {len(repos)} repositories.\n\n")
        if DRY_RUN:
            w("**Dry run — no issue was opened or changed.**\n\n")
        if actions:
            w("### Exact pins behind a newer release\n\n")
            w("| consumer | dependency | pinned | producer | released | |\n|---|---|---|---|---|---|\n")
            for c, d, p, pr, n, what in actions:
                w(f"| `{c}` | `{d}` | `{p}` | `{pr}` | `{n}` | {what} |\n")
            w("\n")
        else:
            w("**Every exact pin is current.**\n\n")
        w(f"- exact and current: **{len(current)}**\n")
        w(f"- range pins, which resolve on the next install: **{len(ranges)}**\n")
        w(f"- revision pins, reported rather than chased: **{len(revisions)}**\n")
        w(f"- declared then overridden by `[patch]`: **{len(patched)}**\n")
        if patched:
            w("\n### Manifests whose declared version is dead text\n\n")
            for c, br, d, s_, pr, n in patched:
                w(f"- **`{c}`** declares `{d}` with a tag and then overrides it with a "
                  f"`[patch]` section, so the declared version is not what is built. "
                  f"`{pr}` newest release `{n}`. Moving the declared pin would change "
                  f"nothing; the manifest itself is the thing to fix.\n")
        if revisions:
            w("\n<details><summary>revision pins</summary>\n\n")
            for c, br, d, s, pr, n in revisions:
                w(f"- `{c}` ({br}) pins `{d}` by revision; `{pr}` newest release `{n}`\n")
            w("\nA revision pin is precise and reproducible. It is listed rather than "
              "reported as behind because a producer's tags may not describe what "
              "consumers actually use — which is the case for `bcr-common`, tracked "
              "in `bcr-common#219`.\n</details>\n")
        if gaps:
            w("\n### Not measured on this run\n\n")
            for g in sorted(set(gaps)):
                w(f"- {g}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
