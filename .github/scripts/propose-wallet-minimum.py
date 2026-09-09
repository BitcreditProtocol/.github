#!/usr/bin/env python3
"""Propose one explicitly selected wallet floor; never publish it directly."""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote, urlencode

MARKER = "bitcredit-wallet-minimum-v1"
SHA = re.compile(r"[0-9a-f]{40}")
NUMBER = r"(?:0|[1-9][0-9]*)"
FLOOR = re.compile(rf"({NUMBER})\.({NUMBER})\.({NUMBER})")
IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
TAG = re.compile(rf"v?({NUMBER})\.({NUMBER})\.({NUMBER})"
                 rf"(?:-{IDENTIFIER}(?:\.{IDENTIFIER})*)?"
                 r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
STATE_KEYS = {"environment", "version", "source_release_id", "source_release_tag",
              "source_sha", "base_sha", "base_version"}


class ProposalError(RuntimeError):
    pass


def version(value, pattern=FLOOR):
    match = pattern.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ProposalError("Expected a valid numeric version or source release tag")
    return tuple(map(int, match.groups()))


def settings():
    cfg = {key: os.environ.get(key, "") for key in
           ("TARGET_ENV", "SOURCE_RELEASE_TAG", "PROPOSED_MIN_VERSION", "AUTOMATION_BOT")}
    cfg["ORG"] = os.environ.get("ORG", "BitcreditProtocol")
    dry = os.environ.get("DRY_RUN", "true").lower()
    if (cfg["TARGET_ENV"] not in ("dev", "staging", "prod") or dry not in ("true", "false")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", cfg["ORG"])
            or not re.fullmatch(r"[A-Za-z0-9-]+\[bot\]", cfg["AUTOMATION_BOT"])):
        raise ProposalError("Invalid environment, organisation, bot identity, or DRY_RUN")
    if version(cfg["PROPOSED_MIN_VERSION"]) > version(cfg["SOURCE_RELEASE_TAG"], TAG):
        raise ProposalError("The proposed floor exceeds the source release's marketing version")
    cfg["dry_run"] = dry == "true"
    cfg["root"] = f"repos/{cfg['ORG']}/static-assets"
    cfg["branch"] = "automation/wallet-minimum/" + cfg["TARGET_ENV"]
    cfg["path"] = f"static/wallet/min-version/{cfg['TARGET_ENV']}/min-supported-version.json"
    return cfg


def api(cfg, path, method="GET", body=None):
    read_roots = (f"repos/{cfg['ORG']}/wallet", cfg["root"])
    if not any(path == root or path.startswith(root + "/") for root in read_roots):
        raise ProposalError("GitHub request is outside the two approved repositories")
    if method == "GET":
        token = os.environ.get("READ_TOKEN")
    else:
        if cfg["dry_run"]:
            raise ProposalError("Dry run refused a mutation")
        suffix = path.removeprefix(cfg["root"] + "/")
        allowed = (method == "POST" and suffix in ("git/blobs", "git/trees", "git/commits", "git/refs", "pulls"))
        allowed |= method == "PATCH" and (suffix == "git/refs/heads/" + cfg["branch"]
                                            or re.fullmatch(r"pulls/[1-9][0-9]*", suffix) is not None)
        if not path.startswith(cfg["root"] + "/") or not allowed:
            raise ProposalError("Mutation is outside the proposal interface")
        if suffix == "git/refs" and body.get("ref") != "refs/heads/" + cfg["branch"]:
            raise ProposalError("Ref creation must target the proposal branch")
        if suffix.startswith("git/refs/heads/") and body.get("force") is not False:
            raise ProposalError("Force updates are forbidden")
        token = os.environ.get("WRITE_TOKEN")
    if not token:
        raise ProposalError("The required read or write token is unavailable")
    env = {key: value for key, value in os.environ.items()
           if key not in ("READ_TOKEN", "WRITE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN")}
    env["GH_TOKEN"] = token
    command = ["gh", "api", "--hostname", "github.com", "--method", method, path]
    if body is not None:
        command += ["--input", "-"]
    try:
        result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                                capture_output=True, text=True, env=env, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProposalError("GitHub response unavailable") from error
    if result.returncode:
        status = re.search(r"\(HTTP ([0-9]{3})\)", result.stderr)
        raise ProposalError("GitHub request failed (HTTP " + (status[1] if status else "unknown") + ")")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise ProposalError("GitHub returned invalid JSON") from error


def pages(cfg, path):
    rows, page = [], 1
    while True:
        values = api(cfg, path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(v, dict) for v in values):
            raise ProposalError("Invalid GitHub list response")
        rows.extend(values)
        if len(values) < 100:
            return rows
        page += 1


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ProposalError("Invalid Git object SHA")
    return value


def source_release(cfg):
    root = f"repos/{cfg['ORG']}/wallet"
    tag = cfg["SOURCE_RELEASE_TAG"]
    release = api(cfg, root + "/releases/tags/" + quote(tag, safe=""))
    if (not isinstance(release, dict) or type(release.get("id")) is not int or release["id"] <= 0
            or release.get("tag_name") != tag or release.get("draft") is not False
            or type(release.get("prerelease")) is not bool or not isinstance(release.get("published_at"), str)
            or not release["published_at"] or not isinstance(release.get("body"), str)):
        raise ProposalError("Source release is unpublished or its metadata/description is unreadable")
    ref = api(cfg, root + "/git/ref/tags/" + quote(tag, safe=""))
    if not isinstance(ref, dict) or ref.get("ref") != "refs/tags/" + tag:
        raise ProposalError("Source tag response does not match the requested release")
    obj, seen = ref.get("object"), set()
    while isinstance(obj, dict):
        target = sha(obj.get("sha"))
        if obj.get("type") == "commit":
            return release["id"], target
        if obj.get("type") != "tag" or target in seen or len(seen) >= 32:
            break
        seen.add(target)
        value = api(cfg, root + "/git/tags/" + target)
        if not isinstance(value, dict) or value.get("sha") != target:
            break
        obj = value.get("object")
    raise ProposalError("Source tag cannot be resolved to a terminal commit")


def floor_at(cfg, ref):
    value = api(cfg, cfg["root"] + "/contents/" + cfg["path"] + "?ref=" + quote(ref, safe=""))
    if (not isinstance(value, dict) or value.get("type") != "file" or value.get("path") != cfg["path"]
            or value.get("encoding") != "base64" or not isinstance(value.get("content"), str)):
        raise ProposalError("The existing floor file is unreadable")
    blob = sha(value.get("sha"))
    try:
        data = base64.b64decode("".join(value["content"].split()), validate=True)
        parsed = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise ProposalError("Invalid floor JSON or content encoding") from error
    if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != blob:
        raise ProposalError("Floor contents do not match their blob SHA")
    if not isinstance(parsed, dict) or set(parsed) != {"min_supported_version"}:
        raise ProposalError("Unexpected floor JSON schema")
    version(parsed["min_supported_version"])
    return parsed["min_supported_version"], blob


def state_of(text, cfg):
    matches = re.findall(r"<!-- " + MARKER + r" (.*?) -->", text or "")
    if len(matches) != 1:
        raise ProposalError("Proposal ownership marker is missing or ambiguous")
    try:
        state = json.loads(matches[0])
    except ValueError as error:
        raise ProposalError("Invalid proposal ownership marker") from error
    if (not isinstance(state, dict) or set(state) != STATE_KEYS or state["environment"] != cfg["TARGET_ENV"]
            or type(state["source_release_id"]) is not int or state["source_release_id"] <= 0):
        raise ProposalError("Proposal marker has the wrong identity or schema")
    version(state["version"])
    version(state["base_version"])
    if version(state["version"]) > version(state["source_release_tag"], TAG):
        raise ProposalError("Saved proposal exceeds its source release")
    sha(state["source_sha"])
    sha(state["base_sha"])
    return state


def marker(state):
    return "<!-- " + MARKER + " " + json.dumps(state, sort_keys=True, separators=(",", ":")) + " -->"


def title(state):
    return f"Propose {state['environment']} wallet minimum {state['version']}"


def commit_message(state):
    return title(state) + "\n\n" + marker(state)


def public_body(state):
    body = (marker(state) + "\n\n"
            f"Proposed **{state['environment']}** wallet minimum: **{state['base_version']} → {state['version']}**.\n\n"
            f"Source wallet release: `{state['source_release_tag']}` (ID `{state['source_release_id']}`), "
            f"commit `{state['source_sha']}`.\n\n"
            "The minimum was explicitly selected by the requester; it was not inferred from release notes. "
            "This PR contains no private wallet changelog content. Merging activates the selected environment's floor.\n")
    if state["environment"] == "prod":
        body += ("\n**Before merging:** provide evidence that an eligible version is publicly available in both "
                 "Google Play and the Apple App Store for affected users. Candidate uploads, TestFlight, "
                 "or a successful promotion job alone are not that evidence.\n")
    return body


def bot(value, cfg):
    return isinstance(value, dict) and value.get("type") == "Bot" and value.get("login") == cfg["AUTOMATION_BOT"]


def proposal_history(cfg, head):
    states, current, anchor = [], head, None
    # ponytail: cap unmerged proposal history at 100 commits; retire the owned branch if it outgrows this bound.
    for _ in range(100):
        commit = api(cfg, cfg["root"] + "/commits/" + current)
        if (not isinstance(commit, dict) or commit.get("sha") != current or not bot(commit.get("author"), cfg)
                or not bot(commit.get("committer"), cfg) or not isinstance(commit.get("commit"), dict)):
            raise ProposalError("Proposal branch contains a commit not attributed to the automation bot")
        verification = commit["commit"].get("verification", {})
        if not isinstance(verification, dict) or verification.get("verified") is not True or verification.get("reason") != "valid":
            raise ProposalError("Proposal commit does not have a verified bot signature")
        state = state_of(commit["commit"].get("message"), cfg)
        if commit["commit"].get("message") != commit_message(state):
            raise ProposalError("Proposal commit metadata was edited")
        anchor = state["base_sha"] if anchor is None else anchor
        if state["base_sha"] != anchor:
            raise ProposalError("Proposal history changed its base identity")
        files, parents = commit.get("files"), commit.get("parents")
        if (not isinstance(files, list) or len(files) > 1 or any(not isinstance(f, dict)
                or f.get("filename") != cfg["path"] or f.get("status") != "modified" for f in files)
                or not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], dict)):
            raise ProposalError("Proposal history contains unexpected files or parents")
        actual, current_blob = floor_at(cfg, current)
        if actual != state["version"]:
            raise ProposalError("Proposal file does not match its ownership marker")
        states.append(state)
        current = sha(parents[0].get("sha"))
        previous, previous_blob = floor_at(cfg, current)
        if bool(files) != (current_blob != previous_blob):
            raise ProposalError("Proposal commit file inventory is incomplete or changes file metadata")
        if current == anchor:
            if any(s["base_version"] != previous for s in states):
                raise ProposalError("Proposal history changed its original floor")
            return states
    raise ProposalError("Proposal history is too long or does not reach its recorded base")


def refs(cfg):
    rows = api(cfg, cfg["root"] + "/git/matching-refs/heads/" + quote(cfg["branch"], safe="/"))
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ProposalError("Invalid branch-reference response")
    found = [r for r in rows if r.get("ref") == "refs/heads/" + cfg["branch"]]
    if len(found) > 1 or any(not isinstance(r.get("ref"), str) or not isinstance(r.get("object"), dict) for r in rows):
        raise ProposalError("Invalid or duplicate branch references")
    if not found:
        return None
    if found[0]["object"].get("type") != "commit":
        raise ProposalError("Proposal branch does not point to a commit")
    return sha(found[0]["object"].get("sha"))


def pull_requests(cfg):
    rows = pages(cfg, cfg["root"] + "/pulls?" + urlencode({"state": "all", "head": cfg["ORG"] + ":" + cfg["branch"]}))
    if any(type(r.get("number")) is not int or r["number"] <= 0 or r.get("state") not in ("open", "closed")
           or not isinstance(r.get("head"), dict) or not isinstance(r.get("base"), dict)
           or not isinstance(r["head"].get("repo"), dict) or not isinstance(r["base"].get("repo"), dict)
           or not isinstance(r.get("user"), dict) or not isinstance(r["user"].get("login"), str)
           or r["user"].get("type") not in ("Bot", "User")
           or not isinstance(r.get("body"), str) or not isinstance(r.get("title"), str)
           or "merged_at" not in r or (r["merged_at"] is not None and not isinstance(r["merged_at"], str)) for r in rows):
        raise ProposalError("Invalid pull-request list")
    if len({r["number"] for r in rows}) != len(rows) or sum(r["state"] == "open" for r in rows) > 1:
        raise ProposalError("Duplicate proposal pull requests")
    return rows


def own_pr(cfg, pr, default, states):
    repo = cfg["ORG"] + "/static-assets"
    if (not bot(pr.get("user"), cfg) or pr["head"].get("ref") != cfg["branch"]
            or pr["head"].get("repo", {}).get("full_name") != repo
            or pr["base"].get("ref") != default or pr["base"].get("repo", {}).get("full_name") != repo):
        raise ProposalError("The existing proposal PR is not owned by this automation")
    state = state_of(pr.get("body"), cfg)
    if state not in states or pr.get("body") != public_body(state) or pr.get("title") != title(state):
        raise ProposalError("Proposal PR metadata has foreign edits")
    return state


def readback_write(cfg, path, method, body, readback):
    try:
        api(cfg, path, method, body)
    except ProposalError:
        pass  # A lost response is resolved from state, never a blind repeat write.
    if not readback():
        raise ProposalError("Mutation was not confirmed; rerun the original request")


def new_commit(cfg, parent, state):
    commit = api(cfg, cfg["root"] + "/git/commits/" + parent)
    if not isinstance(commit, dict) or commit.get("sha") != parent or not isinstance(commit.get("tree"), dict):
        raise ProposalError("Invalid parent commit")
    tree = sha(commit["tree"].get("sha"))
    data = (json.dumps({"min_supported_version": state["version"]}) + "\n").encode()
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    readback_write(cfg, cfg["root"] + "/git/blobs", "POST", {"content": base64.b64encode(data).decode(), "encoding": "base64"},
                   lambda: api(cfg, cfg["root"] + "/git/blobs/" + blob).get("sha") == blob)
    # Lost immutable tree/commit responses stop before a ref is moved. A rerun can
    # recreate these unreferenced objects without overwriting any branch or PR.
    result = api(cfg, cfg["root"] + "/git/trees", "POST", {"base_tree": tree, "tree": [
        {"path": cfg["path"], "mode": "100644", "type": "blob", "sha": blob}]})
    tree = sha(result.get("sha") if isinstance(result, dict) else None)
    # GitHub can sign for the authenticated App when custom author, committer,
    # and signature fields are omitted. Ownership never relies on an email alone.
    result = api(cfg, cfg["root"] + "/git/commits", "POST",
                 {"message": commit_message(state), "tree": tree, "parents": [parent]})
    created = sha(result.get("sha") if isinstance(result, dict) else None)
    if proposal_history(cfg, created)[0] != state:
        raise ProposalError("New proposal commit could not be verified")
    return created


def propose(cfg):
    release_id, source_sha = source_release(cfg)
    repo = api(cfg, cfg["root"])
    if not isinstance(repo, dict) or not isinstance(repo.get("default_branch"), str) or not repo["default_branch"]:
        raise ProposalError("Static-assets default branch is unknown")
    default = repo["default_branch"]
    if default == cfg["branch"]:
        raise ProposalError("Proposal branch must not be the default branch")
    base = api(cfg, cfg["root"] + "/git/ref/heads/" + quote(default, safe=""))
    if not isinstance(base, dict) or base.get("ref") != "refs/heads/" + default or base.get("object", {}).get("type") != "commit":
        raise ProposalError("Invalid default-branch reference")
    base_sha = sha(base["object"].get("sha"))
    current, _ = floor_at(cfg, base_sha)
    state = dict(environment=cfg["TARGET_ENV"], version=cfg["PROPOSED_MIN_VERSION"], source_release_id=release_id,
                 source_release_tag=cfg["SOURCE_RELEASE_TAG"], source_sha=source_sha, base_sha=base_sha, base_version=current)
    head, prs = refs(cfg), pull_requests(cfg)
    opened = next((p for p in prs if p["state"] == "open"), None)
    states = proposal_history(cfg, head) if head else []
    parent = base_sha
    if head:
        comparison = api(cfg, cfg["root"] + f"/compare/{states[0]['base_sha']}...{base_sha}")
        if (not isinstance(comparison, dict) or comparison.get("status") not in ("ahead", "identical")
                or comparison.get("merge_base_commit", {}).get("sha") != states[0]["base_sha"]):
            raise ProposalError("The recorded proposal base is not on the current default branch")
        comparison = api(cfg, cfg["root"] + f"/compare/{base_sha}...{head}")
        if not isinstance(comparison, dict) or comparison.get("status") not in ("ahead", "behind", "identical", "diverged"):
            raise ProposalError("Cannot determine proposal ancestry")
        if comparison["status"] in ("ahead", "diverged"):
            if states[0]["base_version"] != current:
                raise ProposalError("The default floor changed; review the pending proposal before updating it")
            parent = head
            state.update(base_sha=states[0]["base_sha"], base_version=states[0]["base_version"])
        if any(s["source_release_id"] == release_id and s["source_sha"] != source_sha for s in states):
            raise ProposalError("The saved source release tag moved to a different commit")
    if opened:
        if not head or opened["head"].get("sha") != head:
            raise ProposalError("Proposal PR and branch identities disagree")
        own_pr(cfg, opened, default, states)
        files = pages(cfg, cfg["root"] + f"/pulls/{opened['number']}/files")
        if len(files) != 1 or files[0].get("filename") != cfg["path"] or files[0].get("status") != "modified":
            raise ProposalError("Proposal PR contains unexpected changes")
    result = {"environment": state["environment"], "version": state["version"], "source_release_id": release_id,
              "source_release_tag": state["source_release_tag"], "source_sha": source_sha, "path": cfg["path"]}
    if current == state["version"] or (opened and states[0] == state and own_pr(cfg, opened, default, states) == state):
        return {**result, "status": "no-op", "pr": opened["number"] if opened else None}
    for pr in prs:
        if pr["state"] == "closed" and pr.get("merged_at") is None and bot(pr.get("user"), cfg):
            saved = state_of(pr.get("body"), cfg)
            if all(saved[k] == state[k] for k in STATE_KEYS - {"base_sha", "base_version"}):
                own_pr(cfg, pr, default, [saved])
                return {**result, "status": "no-op-closed", "pr": pr["number"]}
    if cfg["dry_run"]:
        return {**result, "status": "dry-run", "action": "update" if opened else "create"}
    if not os.environ.get("WRITE_TOKEN"):
        raise ProposalError("Write token is unavailable")
    created = head if states and states[0] == state else new_commit(cfg, parent, state)
    if created != head:
        if refs(cfg) != head:
            raise ProposalError("Proposal branch changed after preflight")
        path = cfg["root"] + ("/git/refs/heads/" + cfg["branch"] if head else "/git/refs")
        body = {"sha": created, "force": False} if head else {"ref": "refs/heads/" + cfg["branch"], "sha": created}
        readback_write(cfg, path, "PATCH" if head else "POST", body, lambda: refs(cfg) == created)
    payload = {"title": title(state), "body": public_body(state)}
    if not opened:
        payload.update(head=cfg["branch"], base=default, draft=True)
    else:
        fresh = [p for p in pull_requests(cfg) if p["state"] == "open"]
        if (len(fresh) != 1 or fresh[0]["number"] != opened["number"]
                or fresh[0]["head"].get("sha") != created
                or any(fresh[0][key] != opened[key] for key in ("title", "body"))):
            raise ProposalError("Proposal PR changed after preflight")
    def confirmed():
        found = [p for p in pull_requests(cfg) if p["state"] == "open"]
        return (len(found) == 1 and found[0]["head"].get("sha") == created
                and own_pr(cfg, found[0], default, [state]) == state)
    if opened and own_pr(cfg, opened, default, states) != state:
        readback_write(cfg, cfg["root"] + f"/pulls/{opened['number']}", "PATCH", payload, confirmed)
    elif not opened:
        readback_write(cfg, cfg["root"] + "/pulls", "POST", payload, confirmed)
    elif not confirmed():
        raise ProposalError("Proposal changed during reconciliation")
    return {**result, "status": "proposed", "branch": cfg["branch"]}


def main():
    try:
        print(json.dumps(propose(settings()), sort_keys=True))
    except (ProposalError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"Wallet minimum proposal stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
