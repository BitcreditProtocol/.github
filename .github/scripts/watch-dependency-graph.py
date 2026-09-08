#!/usr/bin/env python3
"""Read the dependency graph before acting; notify once per dependency/version.

Only exact SemVer pins produce issues. Manual closure suppresses that target
version; an automatically resolved issue may be reopened after a regression.
"""
import base64
from collections import defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys
import tomllib
from urllib.parse import quote

ORG = os.environ.get("ORG", "BitcreditProtocol")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "/dev/stdout")
MARKER = "bitcredit-dependency-watch"
STATE = "bitcredit-dependency-watch-state"
WATCHER_BOT = os.environ.get("WATCHER_BOT", "bitcredit-automation[bot]")
MANIFESTS = {"Cargo.toml", "package.json", "pubspec.yaml"}
EXCLUDED = {"node_modules", "vendor", "target", "build", ".dart_tool", "cargokit", "crowdin_sdk"}
NPM_OWNER = {"@bitcredit/bcr-ebill-wasm": "Bitcredit-Core",
             "@bitcredit/ui-library": "ui", "@bitcreditprotocol/ui-library": "ui"}
VERSION = re.compile(
    r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")


class APIError(RuntimeError):
    pass


@dataclass(frozen=True)
class Edge:
    consumer: str
    branch: str
    path: str
    dependency: str
    producer: str
    pin: str
    kind: str


def api(path, method="GET", body=None, *, missing=False):
    if method != "GET" and DRY_RUN:
        raise APIError("dry-run refused a write")
    command = ["gh", "api", "-X", method, path]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        if missing and re.search(r"\(HTTP 404\)", result.stderr):
            return None
        raise APIError(f"{method} {path}: {result.stderr.strip()[:500] or 'request failed'}")
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


def semver(value):
    match = VERSION.fullmatch(value)
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    identifiers = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return int(major), int(minor), int(patch), int(pre is None), identifiers


def cargo_kind(body):
    if re.search(r"\brev\s*=", body):
        return "revision"
    return "exact" if re.search(r"\btag\s*=", body) else "range"


def producer_of(spec):
    match = re.search(r"(?:github\.com[:/]|github:)" + re.escape(ORG) + r"/([A-Za-z0-9_.-]+)", spec, re.I)
    return match[1].removesuffix(".git") if match else None


def owner_of(dep, spec, owners):
    producer = producer_of(spec)
    if producer:
        return producer
    if dep in owners and owners[dep] is None:
        raise ValueError(f"ambiguous producer for {dep}")
    return owners.get(dep)


def content(repo, path, sha):
    data = api(f"repos/{ORG}/{repo}/contents/{quote(path, safe='/')}?ref={sha}")
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        raise APIError(f"{repo}/{path}: contents unavailable")
    try:
        return base64.b64decode("".join(data["content"].split()), validate=True).decode("utf-8")
    except (KeyError, ValueError, UnicodeError):
        raise APIError(f"{repo}/{path}: invalid content encoding") from None


def parse_manifest(path, text):
    name = PurePosixPath(path).name
    if name == "Cargo.toml":
        return tomllib.loads(text)
    if name == "package.json":
        return json.loads(text)
    result = subprocess.run(["yq", "-o=json", ".", "-"], input=text,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"{path}: invalid YAML")
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest must be a mapping")
    return data


def declarations(doc):
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        yield from (doc.get(section) or {}).items()
    yield from (doc.get("workspace", {}).get("dependencies") or {}).items()
    for target in (doc.get("target") or {}).values():
        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            yield from (target.get(section) or {}).items()


def cargo_patches(path, manifests):
    doc = manifests[path]
    if "workspace" not in doc:
        for parent in PurePosixPath(path).parents:
            candidate = str(parent / "Cargo.toml")
            if candidate in manifests and "workspace" in manifests[candidate]:
                doc = manifests[candidate]
                break
    return {name for source in (doc.get("patch") or {}).values() for name in source}


def edges_from(repo, branch, path, manifests, owners):
    doc = manifests[path]
    edges = []
    name = PurePosixPath(path).name

    def add(dep, producer, pin, kind):
        if producer and producer != repo:
            edges.append(Edge(repo, branch, path, dep, producer, str(pin), kind))

    if name == "Cargo.toml":
        patched = cargo_patches(path, manifests)
        for dep, value in declarations(doc):
            attrs = value if isinstance(value, dict) else {"version": value}
            if attrs.get("workspace") or "path" in attrs:
                continue  # Workspace declarations are read separately; path sources are local.
            producer = owner_of(attrs.get("package", dep), attrs.get("git", ""), owners)
            if "rev" in attrs:
                pin, kind = attrs["rev"], "revision"
            elif "tag" in attrs:
                pin, kind = attrs["tag"], "exact"
            else:
                pin = attrs.get("version", attrs.get("branch", "default branch"))
                kind = "exact" if str(pin).startswith("=") else "range"
                if kind == "exact":
                    pin = str(pin)[1:]
            add(dep, producer, pin, "patched" if attrs.get("package", dep) in patched else kind)
    elif name == "package.json":
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for dep, value in (doc.get(section) or {}).items():
                if not isinstance(value, str):
                    continue
                producer = owner_of(dep, value, owners)
                pin = value.removeprefix("=")
                kind = "exact" if semver(pin) is not None else "range"
                if "#" in value and producer_of(value):
                    pin = value.rsplit("#", 1)[1]
                    kind = "exact" if semver(pin) is not None else (
                        "revision" if re.fullmatch(r"[0-9a-f]{7,40}", pin) else "range")
                add(dep, producer, pin, kind)
    else:
        for section in ("dependencies", "dev_dependencies"):
            for dep, attrs in (doc.get(section) or {}).items():
                if not isinstance(attrs, dict) or "git" not in attrs:
                    continue
                git = attrs["git"]
                git = {"url": git} if isinstance(git, str) else git
                if not isinstance(git, dict):
                    raise ValueError(f"{path}: invalid git dependency {dep}")
                pin = git.get("ref", "default branch")
                kind = "exact" if semver(pin) is not None else (
                    "revision" if re.fullmatch(r"[0-9a-f]{7,40}", pin) else "range")
                add(dep, producer_of(git.get("url", "")), pin, kind)
    return edges


def collect_graph(gaps, incomplete):
    repos = [r for r in pages(f"orgs/{ORG}/repos")
             if not r["archived"] and not r["fork"]]
    graphs, package_owners = {}, defaultdict(set)
    for repo in repos:
        name, default = repo["name"], repo["default_branch"]
        for branch in dict.fromkeys((default, "dev")):
            try:
                data = api(f"repos/{ORG}/{name}/branches/{quote(branch, safe='')}",
                           missing=branch != default)
                if data is None:
                    continue
                sha = data.get("commit", {}).get("sha")
                if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                    raise APIError(f"{name}/{branch}: invalid branch SHA")
                tree = api(f"repos/{ORG}/{name}/git/trees/{sha}?recursive=1")
                if not isinstance(tree.get("tree"), list) or tree.get("truncated") is not False:
                    raise APIError(f"{name}/{branch}: incomplete tree")
                manifests = {}
                for entry in tree["tree"]:
                    path = PurePosixPath(entry["path"])
                    if entry.get("type") != "blob" or path.name not in MANIFESTS or EXCLUDED.intersection(path.parts):
                        continue
                    try:
                        doc = parse_manifest(str(path), content(name, str(path), sha))
                        if not isinstance(doc, dict):
                            raise ValueError("manifest must be a mapping")
                        manifests[str(path)] = doc
                        package_name = doc.get("package", {}).get("name") if path.name == "Cargo.toml" else doc.get("name")
                        if isinstance(package_name, str):
                            package_owners[package_name].add(name)
                    except (APIError, ValueError, OSError, subprocess.SubprocessError) as exc:
                        gaps.append(f"{name}/{branch}/{path}: {exc}")
                        incomplete.add(name)
                graphs[name, branch] = manifests
            except (APIError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
                gaps.append(f"{name}/{branch}: {exc}")
                incomplete.add(name)
    owners = {name: next(iter(values)) if len(values) == 1 else None
              for name, values in package_owners.items()}
    owners.update(NPM_OWNER)
    edges = []
    for (repo, branch), manifests in graphs.items():
        for path in manifests:
            try:
                edges.extend(edges_from(repo, branch, path, manifests, owners))
            except (ValueError, TypeError, AttributeError) as exc:
                gaps.append(f"{repo}/{branch}/{path}: {exc}")
                incomplete.add(repo)
    return repos, sorted(set(edges), key=lambda e: (e.consumer, e.dependency, e.branch, e.path))


def latest_release(repo, cache):
    if repo not in cache:
        release = api(f"repos/{ORG}/{repo}/releases/latest", missing=True)
        if release is not None:
            if (not isinstance(release, dict) or release.get("draft") is not False
                    or release.get("prerelease") is not False or not isinstance(release.get("tag_name"), str)
                    or semver(release["tag_name"]) is None):
                raise APIError(f"{repo}: latest full release has no valid SemVer tag")
            release = release["tag_name"]
        cache[repo] = release
    return cache[repo]


def marker_for(dep):
    return f"<!-- {MARKER}:{dep} -->"


def own_issue(issue):
    return ("pull_request" not in issue and issue.get("user", {}).get("type") == "Bot"
            and issue.get("user", {}).get("login") == WATCHER_BOT
            and f"<!-- {MARKER}:" in (issue.get("body") or ""))


def issue_state(issue):
    match = re.search(r"<!-- " + STATE + r" (.*?) -->", issue.get("body") or "")
    if not match:
        return None
    data = json.loads(match[1])
    if (not isinstance(data, dict) or not isinstance(data.get("producer"), str)
            or not isinstance(data.get("target"), str) or semver(data["target"]) is None
            or type(data.get("resolved")) is not bool):
        raise ValueError(f"issue #{issue['number']}: invalid watcher state")
    if issue.get("state") == "closed" and data["resolved"]:
        if issue.get("state_reason") == "not_planned":
            data["resolved"] = False
        else:
            closer, author = issue.get("closed_by"), issue.get("user")
            if not isinstance(closer, dict) or not isinstance(author, dict):
                raise ValueError(f"issue #{issue['number']}: last closure could not be attributed")
            data["resolved"] = (closer.get("type") == "Bot" and author.get("type") == "Bot"
                                and closer.get("login") == author.get("login")
                                and issue.get("state_reason") == "completed")
    return data


def issue_payload(dep, producer, target, edges, *, resolved=False):
    state = dict(producer=producer, target=target, resolved=resolved)
    body = marker_for(dep) + f"\n<!-- {STATE} {json.dumps(state, sort_keys=True)} -->\n"
    if resolved:
        body += f"All observed exact pins for **{dep}** have caught up, or no longer require an exact version.\n"
    else:
        body += f"**{producer}** has released **{target}**. These exact pins are behind:\n\n"
    body += "| branch | manifest | pinned |\n|---|---|---|\n"
    for edge in edges:
        url = f"https://github.com/{ORG}/{edge.consumer}/blob/{quote(edge.branch, safe='')}/{quote(edge.path, safe='/')}"
        body += f"| {edge.branch} | [{edge.path}]({url}) | {edge.pin} |\n"
    body += ("\nOpened by watch-dependency-graph. Update the pin and any required product code deliberately. "
             "Closing without an update skips this target version only; a newer release may notify again.\n")
    return {"title": f"{dep}: update exact pins for {producer} {target}", "body": body}


def plan_actions(edges, issues_by_repo, gaps, incomplete):
    source_incomplete = frozenset(incomplete)
    groups = defaultdict(list)
    for edge in edges:
        groups[edge.consumer, edge.dependency].append(edge)
    for repo, issues in issues_by_repo.items():
        for issue in issues:
            mark = re.search(r"<!-- " + MARKER + r":([^\s<>]+) -->", issue.get("body") or "")
            if mark and issue["state"] == "open":
                groups.setdefault((repo, mark[1]), [])
    actions, latest = [], {}
    for (repo, dep), locations in sorted(groups.items()):
        if repo in incomplete:
            continue
        if repo not in issues_by_repo:
            if any(e.kind == "exact" for e in locations):
                gaps.append(f"{repo}/{dep}: issues are disabled; notification not evaluated")
            continue
        try:
            matching = [i for i in issues_by_repo.get(repo, [])
                        if marker_for(dep) in (i.get("body") or "")]
            opened = [i for i in matching if i["state"] == "open"]
            if len(opened) > 1:
                raise ValueError(f"{repo}/{dep}: multiple open watcher issues; reconcile them first")
            existing = opened[0] if opened else None
            if existing:
                recorded = issue_state(existing)
                if recorded and recorded["producer"] in source_incomplete:
                    gaps.append(f"{repo}/{dep}: producer {recorded['producer']} was not fully read; issue unchanged")
                    incomplete.add(repo)
                    continue
            producers = {e.producer for e in locations}
            if len(producers) > 1:
                raise ValueError(f"{repo}/{dep}: manifests refer to different producers")
            producer = next(iter(producers), None)
            exact = [e for e in locations if e.kind == "exact"]
            behind, target = [], None
            if exact:
                target = latest_release(producer, latest)
                if target is None:
                    raise ValueError(f"{producer}: no published full release; exact pins not compared")
                for edge in exact:
                    pin = semver(edge.pin)
                    if pin is None:
                        raise ValueError(f"{repo}/{edge.path}: {dep} has an invalid exact SemVer pin")
                    if pin < semver(target):
                        behind.append(edge)
            if behind:
                states = [(i, issue_state(i)) for i in matching if i["state"] == "closed"]
                if any(state is None for _, state in states):
                    raise ValueError(f"{repo}/{dep}: closed watcher issue has no version state")
                manual_skip = any(not state["resolved"] and state["producer"] == producer
                                  and semver(state["target"]) == semver(target) for _, state in states)
                if manual_skip:
                    continue
                if not existing:
                    resolved = [i for i, state in states if state["resolved"] and state["producer"] == producer]
                    existing = max(resolved, key=lambda i: i["number"], default=None)
                payload = issue_payload(dep, producer, target, behind)
                if existing:
                    payload["state"] = "open"
                    if all(existing.get(key) == value for key, value in payload.items()):
                        continue
                actions.append(dict(repo=repo, dep=dep, kind="update" if existing else "open",
                                    existing=existing, payload=payload))
            elif existing:
                if not locations:
                    # Missing ownership evidence is not proof the pin was removed.
                    # Keep the issue for manual review instead of silently closing it.
                    raise ValueError(f"{repo}/{dep}: previous dependency is no longer mapped; issue unchanged")
                state = issue_state(existing)
                if state is None:
                    raise ValueError(f"{repo}/{dep}: open watcher issue has no version state")
                payload = issue_payload(dep, state["producer"], state["target"], exact, resolved=True)
                payload.update(state="closed", state_reason="completed")
                actions.append(dict(repo=repo, dep=dep, kind="close", existing=existing, payload=payload))
        except (APIError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            gaps.append(str(exc))
            incomplete.add(repo)
    # An error found later in this pass must also suppress earlier planned actions
    # for that consumer. No writes happen until the whole pass has finished.
    return [a for a in actions if a["repo"] not in incomplete]


def apply_action(action):
    repo, existing, payload = action["repo"], action["existing"], action["payload"]
    path = f"repos/{ORG}/{repo}/issues"
    if existing:
        path += f"/{existing['number']}"
    try:
        result = api(path, "PATCH" if existing else "POST", payload)
        number = result.get("number") if isinstance(result, dict) else None
        if not isinstance(number, int):
            raise APIError(f"{repo}: invalid issue write response")
    except (APIError, subprocess.TimeoutExpired):
        if existing:
            number = existing["number"]
        else:
            # Read after an uncertain creation; never blindly issue a second POST.
            matches = [i for i in pages(f"repos/{ORG}/{repo}/issues?state=all")
                       if own_issue(i) and i.get("body") == payload["body"]]
            if len(matches) != 1:
                raise
            number = matches[0]["number"]
    result = api(f"repos/{ORG}/{repo}/issues/{number}")
    if (not isinstance(result, dict) or not own_issue(result) or result.get("body") != payload["body"]
            or result.get("state") != payload.get("state", "open")):
        raise APIError(f"{repo} issue #{number}: write could not be verified")
    return f"{action['kind']} verified: #{number}"


def main():
    gaps, incomplete, edges, actions, repos = [], set(), [], [], []
    failed = False
    try:
        repos, edges = collect_graph(gaps, incomplete)
        # Read every issue page before the first write. This also finds issues for
        # dependencies that have been removed entirely since the previous run.
        issues = {}
        for repo in repos:
            if repo.get("has_issues", True):
                issues[repo["name"]] = [i for i in pages(f"repos/{ORG}/{repo['name']}/issues?state=all")
                                        if own_issue(i)]
                for index, issue in enumerate(issues[repo["name"]]):
                    if issue["state"] == "closed" and f"<!-- {MARKER}:" in (issue.get("body") or ""):
                        # List responses need not carry closed_by. Read native last-
                        # closure metadata before deciding whether a human dismissed it.
                        current = api(f"repos/{ORG}/{repo['name']}/issues/{issue['number']}")
                        if not isinstance(current, dict) or current.get("number") != issue["number"]:
                            raise APIError(f"{repo['name']}: invalid issue detail response")
                        issues[repo["name"]][index] = current
        actions = plan_actions(edges, issues, gaps, incomplete)
        for action in actions:
            if DRY_RUN:
                action["result"] = "would " + action["kind"]
                continue
            try:
                action["result"] = apply_action(action)
            except (APIError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                action["result"] = f"FAILED: {exc}"
                raise
    except (APIError, ValueError, OSError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        gaps.append(str(exc))
        failed = True
    finally:
        with open(SUMMARY, "a") as stream:
            stream.write("## Dependency graph\n\n")
            if DRY_RUN:
                stream.write("**Dry run: no issues were opened, changed or closed.**\n\n")
            stream.write(f"{len(edges)} declarations across {len(repos)} active, non-fork repositories.\n\n")
            for kind in ("exact", "range", "revision", "patched"):
                stream.write(f"- {kind}: {sum(e.kind == kind for e in edges)}\n")
            stream.write("\n| consumer | branch | manifest | dependency | kind | pin | producer |\n")
            stream.write("|---|---|---|---|---|---|---|\n")
            for edge in edges:
                cells = (edge.consumer, edge.branch, edge.path, edge.dependency, edge.kind, edge.pin, edge.producer)
                stream.write("| " + " | ".join(str(c).replace("|", r"\|").replace("\n", " ") for c in cells) + " |\n")
            stream.write("\n### Issue actions\n\n")
            for action in actions:
                stream.write(f"- {action['repo']}/{action['dep']}: {action.get('result', 'not attempted')}\n")
            if not actions:
                stream.write("No issue changes planned from the completed reads.\n")
            if gaps:
                stream.write("\n### Not measured or stopped\n\n")
                for gap in sorted(set(gaps)):
                    stream.write(f"- {gap}\n")
                stream.write("\nThis run does not establish that all exact pins are current.\n")
    return 1 if failed or gaps else 0


if __name__ == "__main__":
    sys.exit(main())
