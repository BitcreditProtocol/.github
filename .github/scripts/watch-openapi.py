#!/usr/bin/env python3
"""Compare exact Wildcat/master and dashboard/dev OpenAPI snapshots."""
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile
import zlib

ORG = os.environ.get("ORG", "BitcreditProtocol")
WATCHER_BOT = os.environ.get("WATCHER_BOT", "bitcredit-automation[bot]")
DRY_RUN = os.environ.get("DRY_RUN", "true") != "false"
PRODUCER = "Wildcat"
CONSUMER = "wildcat-dashboard-ui"
SPEC_PATH = "opt/wildcat/openapi.json"
WORKFLOW_PATH = ".github/workflows/openapi.yml"
ARTIFACT_NAME = "openapi"
MARKER = "<!-- bitcredit-openapi-watch:Wildcat/master:wildcat-dashboard-ui/dev -->"
STATE = "bitcredit-openapi-watch-state"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
# ponytail: 5 MB spec ceiling; raise it if the generated contract outgrows it.
MAX_SPEC_BYTES = 5_000_000
MAX_ARCHIVE_BYTES = 10_000_000
WRITE_ATTEMPTS = 0


class EvidenceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def positive_id(value):
    return type(value) is int and value > 0


def same_id(value, expected):
    return positive_id(value) and value == expected


def timestamp(value):
    try:
        require(isinstance(value, str), "invalid timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "timestamp has no timezone")
        return parsed
    except (TypeError, ValueError):
        raise EvidenceError("invalid timestamp") from None


def load_json(value, label):
    def unique_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def invalid_constant(value):
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(value, object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    except (ValueError, TypeError, UnicodeError):
        raise EvidenceError(f"{label}: invalid JSON") from None


def api(path, method="GET", body=None, *, paginate=False, raw=False):
    """Keep credentials in the environment and response bodies out of errors."""
    global WRITE_ATTEMPTS
    require(method in ("GET", "POST", "PATCH"), "unsupported API method")
    if method != "GET":
        require(not DRY_RUN, "dry run refused an issue write")
        require(re.fullmatch(rf"repos/{re.escape(ORG)}/{CONSUMER}/issues(?:/[1-9][0-9]*)?", path),
                "write outside the dashboard issue endpoint refused")
    token_name = "READ_TOKEN" if method == "GET" else "WRITE_TOKEN"
    token = os.environ.get(token_name)
    require(bool(token), f"{token_name} is required")
    command = ["gh", "api", "--hostname", "github.com", "-X", method, path]
    if paginate:
        require(method == "GET" and not raw, "invalid paginated request")
        command += ["--paginate", "--slurp"]
    if body is not None:
        command += ["--input", "-"]
    if method != "GET":
        WRITE_ATTEMPTS += 1
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("READ_TOKEN", "WRITE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")}
    environment.update(GH_TOKEN=token, GH_HOST="github.com")
    try:
        result = subprocess.run(
            command, input=json.dumps(body).encode() if body is not None else None,
            env=environment,
            capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError(f"{method} {path}: request did not complete") from None
    if result.returncode:
        status = re.search(rb"\(HTTP ([0-9]{3})\)", result.stderr)
        detail = "HTTP " + status[1].decode() if status else "request failed"
        raise EvidenceError(f"{method} {path}: {detail}")
    return result.stdout if raw else load_json(result.stdout, path)


def pages(path, key=None):
    response = api(path + ("&" if "?" in path else "?") + "per_page=100", paginate=True)
    require(isinstance(response, list) and bool(response), f"{path}: invalid pages")
    rows, expected = [], None
    for page in response:
        if key is None:
            batch = page
        else:
            require(isinstance(page, dict) and type(page.get("total_count")) is int
                    and page["total_count"] >= 0 and isinstance(page.get(key), list),
                    f"{path}: invalid collection page")
            expected = page["total_count"] if expected is None else expected
            require(page["total_count"] == expected, f"{path}: count changed during pagination")
            batch = page[key]
        require(isinstance(batch, list) and len(batch) <= 100, f"{path}: invalid page")
        require(all(isinstance(row, dict) and positive_id(row.get("id")) for row in batch),
                f"{path}: invalid row identity")
        rows.extend(batch)
    require(len({row["id"] for row in rows}) == len(rows), f"{path}: duplicate rows")
    require(expected is None or len(rows) == expected, f"{path}: incomplete pagination")
    return rows


def repository(repo):
    data = api(f"repos/{ORG}/{repo}")
    require(isinstance(data, dict) and positive_id(data.get("id"))
            and data.get("full_name") == f"{ORG}/{repo}" and data.get("archived") is False,
            f"{repo}: repository identity or active state is unknown")
    if repo == CONSUMER:
        require(data.get("has_issues") is True, "dashboard issues are not available")
    return data["id"]


def branch(repo, ref):
    data = api(f"repos/{ORG}/{repo}/git/ref/heads/{ref}")
    require(isinstance(data, dict) and data.get("ref") == f"refs/heads/{ref}"
            and isinstance(data.get("object"), dict) and data["object"].get("type") == "commit"
            and isinstance(data["object"].get("sha"), str) and SHA.fullmatch(data["object"]["sha"]),
            f"{repo}/{ref}: invalid commit reference")
    return data["object"]["sha"]


def source_metadata():
    repo_id, sha = repository(PRODUCER), branch(PRODUCER, "master")
    workflow = api(f"repos/{ORG}/{PRODUCER}/actions/workflows/openapi.yml")
    require(isinstance(workflow, dict) and positive_id(workflow.get("id"))
            and workflow.get("path") == WORKFLOW_PATH and workflow.get("state") == "active",
            "OpenAPI workflow is unavailable or has changed identity")
    # The existing generator runs on trusted pushes, not pull-request merges.
    runs = pages(f"repos/{ORG}/{PRODUCER}/actions/workflows/{workflow['id']}/runs"
                 f"?branch=master&event=push&status=success&head_sha={sha}", "workflow_runs")
    require(bool(runs), f"no successful OpenAPI run for Wildcat/master {sha}")
    for run in runs:
        require(same_id(run.get("workflow_id"), workflow["id"]) and run.get("head_sha") == sha
                and run.get("head_branch") == "master" and run.get("event") == "push"
                and run.get("status") == "completed" and run.get("conclusion") == "success"
                and positive_id(run.get("run_attempt"))
                and isinstance(run.get("repository"), dict) and same_id(run["repository"].get("id"), repo_id)
                and isinstance(run.get("head_repository"), dict) and same_id(run["head_repository"].get("id"), repo_id),
                "invalid or mismatched OpenAPI run")
        timestamp(run.get("run_started_at"))
    run = max(runs, key=lambda item: (timestamp(item["run_started_at"]), item["id"]))
    artifacts = pages(f"repos/{ORG}/{PRODUCER}/actions/runs/{run['id']}/artifacts", "artifacts")
    for artifact in artifacts:
        require(isinstance(artifact.get("name"), str) and bool(artifact["name"])
                and type(artifact.get("expired")) is bool, "invalid artifact listing")
    active = [item for item in artifacts if item["name"] == ARTIFACT_NAME and not item["expired"]]
    require(len(active) == 1, "expected exactly one unexpired openapi artifact")
    artifact = active[0]
    origin = artifact.get("workflow_run")
    require(isinstance(origin, dict) and same_id(origin.get("id"), run["id"])
            and origin.get("head_sha") == sha and origin.get("head_branch") == "master"
            and same_id(origin.get("repository_id"), repo_id) and same_id(origin.get("head_repository_id"), repo_id),
            "artifact does not belong to the exact source run")
    require(type(artifact.get("size_in_bytes")) is int and 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE_BYTES
            and isinstance(artifact.get("digest"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]),
            "artifact size or archive digest is invalid")
    require(timestamp(artifact.get("expires_at")) > datetime.now(timezone.utc), "OpenAPI artifact has expired")
    return dict(repository=PRODUCER, ref="master", sha=sha, workflow_id=workflow["id"],
                run_id=run["id"], run_attempt=run["run_attempt"], run_started_at=run["run_started_at"],
                artifact_id=artifact["id"], artifact_name=ARTIFACT_NAME,
                archive_digest=artifact["digest"], archive_bytes=artifact["size_in_bytes"],
                expires_at=artifact["expires_at"])


def canonical_digest(data):
    require(len(data) <= MAX_SPEC_BYTES, "OpenAPI document is too large")
    doc = load_json(data, "OpenAPI document")
    require(isinstance(doc, dict) and isinstance(doc.get("openapi"), str)
            and re.fullmatch(r"3\.[0-9]+\.[0-9]+", doc["openapi"])
            and isinstance(doc.get("info"), dict)
            and all(isinstance(doc["info"].get(key), str) and bool(doc["info"][key]) for key in ("title", "version"))
            and isinstance(doc.get("paths"), dict), "invalid OpenAPI document structure")
    require(all(isinstance(value, dict) for key, value in doc["paths"].items() if not key.startswith("x-"))
            and all(key.startswith(("/", "x-")) for key in doc["paths"]), "invalid OpenAPI paths")
    components = doc.get("components", {})
    require(isinstance(components, dict) and isinstance(components.get("schemas", {}), dict),
            "invalid OpenAPI components")
    try:
        canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, UnicodeError):
        raise EvidenceError("OpenAPI document has invalid JSON values") from None
    return hashlib.sha256(canonical).hexdigest()


def source_digest(source):
    archive = api(f"repos/{ORG}/{PRODUCER}/actions/artifacts/{source['artifact_id']}/zip", raw=True)
    require(len(archive) == source["archive_bytes"]
            and "sha256:" + hashlib.sha256(archive).hexdigest() == source["archive_digest"],
            "OpenAPI artifact archive digest or size mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            require(len(entries) == 1 and entries[0].filename == "openapi.json"
                    and not entries[0].is_dir() and entries[0].file_size <= MAX_SPEC_BYTES,
                    "artifact must contain exactly one bounded openapi.json file")
            data = bundle.read(entries[0])
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, zlib.error):
        raise EvidenceError("OpenAPI artifact is corrupt or unreadable") from None
    return canonical_digest(data)


def consumer_snapshot():
    repository(CONSUMER)
    sha = branch(CONSUMER, "dev")
    data = api(f"repos/{ORG}/{CONSUMER}/contents/{SPEC_PATH}?ref={sha}")
    require(isinstance(data, dict) and data.get("type") == "file" and data.get("path") == SPEC_PATH
            and data.get("encoding") == "base64" and isinstance(data.get("content"), str)
            and isinstance(data.get("sha"), str) and SHA.fullmatch(data["sha"])
            and type(data.get("size")) is int and 0 <= data["size"] <= MAX_SPEC_BYTES,
            "invalid dashboard OpenAPI file response")
    try:
        content = base64.b64decode(data["content"].replace("\n", "").replace("\r", ""), validate=True)
    except (ValueError, UnicodeError):
        raise EvidenceError("dashboard OpenAPI content is not valid base64") from None
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest()
    require(len(content) == data["size"] and blob == data["sha"], "dashboard OpenAPI blob identity mismatch")
    return dict(repository=CONSUMER, ref="dev", sha=sha, path=SPEC_PATH,
                blob_sha=data["sha"], canonical_sha256=canonical_digest(content))


def validate_issue(issue):
    require(isinstance(issue, dict) and positive_id(issue.get("id")) and positive_id(issue.get("number"))
            and issue.get("state") in ("open", "closed")
            and isinstance(issue.get("title"), str) and bool(issue["title"])
            and "body" in issue and (issue["body"] is None or isinstance(issue["body"], str))
            and isinstance(issue.get("user"), dict)
            and issue["user"].get("type") in ("User", "Bot")
            and isinstance(issue["user"].get("login"), str) and bool(issue["user"]["login"]),
            "invalid issue metadata")


def own_issue(issue):
    return ("pull_request" not in issue and issue["user"]["type"] == "Bot"
            and issue["user"]["login"] == WATCHER_BOT and MARKER in (issue["body"] or ""))


def issue_state(issue):
    require((issue["body"] or "").count(MARKER) == 1, "managed issue has ambiguous ownership markers")
    matches = re.findall(r"<!-- " + STATE + r" (.*?) -->", issue["body"] or "")
    require(len(matches) == 1, "managed issue has missing or duplicate digest state")
    state = load_json(matches[0], "managed issue state")
    require(isinstance(state, dict) and isinstance(state.get("target_digest"), str)
            and DIGEST.fullmatch(state["target_digest"]) and type(state.get("resolved")) is bool,
            "managed issue has invalid digest state")
    state = dict(target_digest=state["target_digest"], resolved=state["resolved"])
    if issue["state"] == "closed":
        closer = issue.get("closed_by")
        require(isinstance(closer, dict) and closer.get("type") in ("User", "Bot")
                and isinstance(closer.get("login"), str) and bool(closer["login"])
                and "state_reason" in issue and issue["state_reason"] in (None, "completed", "not_planned"),
                "managed issue closure could not be attributed")
        state["resolved"] = (state["resolved"] and closer["type"] == "Bot"
                             and closer["login"] == WATCHER_BOT and issue["state_reason"] == "completed")
    return state


def read_issues():
    rows = pages(f"repos/{ORG}/{CONSUMER}/issues?state=all")
    for issue in rows:
        validate_issue(issue)
    require(len({issue["number"] for issue in rows}) == len(rows), "duplicate issue numbers")
    managed = []
    for issue in rows:
        if not own_issue(issue):
            continue
        current = api(f"repos/{ORG}/{CONSUMER}/issues/{issue['number']}")
        validate_issue(current)
        require(current["id"] == issue["id"] and current["number"] == issue["number"] and own_issue(current),
                "managed issue identity changed during the read")
        issue_state(current)
        managed.append(current)
    require(sum(issue["state"] == "open" for issue in managed) <= 1, "multiple open managed OpenAPI issues")
    return sorted(managed, key=lambda issue: issue["number"])


def issue_payload(source, consumer, *, resolved=False):
    state = {"target_digest": source["canonical_sha256"], "resolved": resolved}
    body = MARKER + f"\n<!-- {STATE} {json.dumps(state, sort_keys=True)} -->\n\n"
    body += ("The dashboard dev specification matches the current Wildcat master specification.\n\n"
             if resolved else "The dashboard dev specification differs from the current Wildcat master specification. "
             "Review the specification and update the snapshot/client deliberately; this is not a compatibility gate.\n\n")
    for label, snapshot in (("Wildcat master", source), ("Dashboard dev", consumer)):
        body += (f"- {label}: [{snapshot['sha']}](https://github.com/{ORG}/{snapshot['repository']}/commit/{snapshot['sha']}); "
                 f"canonical SHA-256 {snapshot['canonical_sha256']}.\n")
    body += (f"- Source [run {source['run_id']}](https://github.com/{ORG}/{PRODUCER}/actions/runs/{source['run_id']}), "
             f"artifact {source['artifact_id']} (openapi.json).\n\n"
             "Closing this issue without synchronization dismisses this source digest only. "
             "A different source digest may notify again. Missing or unreadable evidence never proves synchronization.\n")
    return {"title": "Wildcat OpenAPI: update dashboard dev snapshot", "body": body}


def plan_action(source, consumer, issues):
    target = source["canonical_sha256"]
    opened = [issue for issue in issues if issue["state"] == "open"]
    require(len(opened) <= 1, "multiple open managed OpenAPI issues")
    existing = opened[0] if opened else None
    states = [(issue, issue_state(issue)) for issue in issues]
    if target == consumer["canonical_sha256"]:
        if not existing:
            return None, "in_sync"
        payload = issue_payload(source, consumer, resolved=True)
        payload.update(state="closed", state_reason="completed")
        return dict(kind="close", number=existing["number"], payload=payload), "synchronized"
    if any(issue["state"] == "closed" and not state["resolved"] and state["target_digest"] == target
           for issue, state in states):
        return None, "manually_dismissed"
    if existing and issue_state(existing) == {"target_digest": target, "resolved": False}:
        return None, "already_reported"
    if not existing:
        resolved = [issue for issue, state in states if issue["state"] == "closed" and state["resolved"]]
        existing = max(resolved, key=lambda issue: issue["number"], default=None)
    payload = issue_payload(source, consumer)
    if existing:
        payload["state"] = "open"
    return dict(kind="update" if existing else "open", number=existing["number"] if existing else None,
                payload=payload), "drift"


def apply_action(action):
    path = f"repos/{ORG}/{CONSUMER}/issues"
    number = action["number"]
    try:
        result = api(path + (f"/{number}" if number else ""),
                     "PATCH" if number else "POST", action["payload"])
        require(isinstance(result, dict) and positive_id(result.get("number"))
                and (number is None or result["number"] == number), "invalid issue write response")
        number = result["number"]
    except EvidenceError:
        if number is None:
            matches = [issue for issue in read_issues()
                       if issue["body"] == action["payload"]["body"] and issue["title"] == action["payload"]["title"]]
            require(len(matches) == 1, "issue creation outcome is unknown; no second write attempted")
            number = matches[0]["number"]
    current = api(f"{path}/{number}")
    validate_issue(current)
    require(own_issue(current) and current["number"] == number
            and current["state"] == action["payload"].get("state", "open")
            and all(current.get(key) == value for key, value in action["payload"].items()),
            "issue write could not be verified")
    return number


def run():
    source = source_metadata()
    source["canonical_sha256"] = source_digest(source)
    consumer = consumer_snapshot()
    issues = read_issues()
    action, status = plan_action(source, consumer, issues)
    fresh_source = source_metadata()
    require(fresh_source == {key: value for key, value in source.items() if key != "canonical_sha256"},
            "source snapshot changed during the read")
    repository(CONSUMER)
    require(branch(CONSUMER, "dev") == consumer["sha"], "dashboard dev changed during the read")
    report = dict(status=status, dry_run=DRY_RUN, source=source, consumer=consumer,
                  proposed_actions=[] if action is None else [{"kind": action["kind"], "number": action["number"]}],
                  verified_writes=0, gaps=[])
    if action is not None and not DRY_RUN:
        require(bool(os.environ.get("WRITE_TOKEN")), "WRITE_TOKEN is required")
        # Recheck native issue state after all source reads, including manual closures.
        require(read_issues() == issues, "managed issue state changed before the write")
        number = apply_action(action)
        report["verified_writes"] = 1
        report["issue_number"] = number
    return report


def main():
    global WRITE_ATTEMPTS
    WRITE_ATTEMPTS = 0
    report = dict(status="incomplete", dry_run=DRY_RUN, source=None, consumer=None,
                  proposed_actions=[], verified_writes=0, gaps=[])
    try:
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", ORG), "ORG is invalid")
        require(re.fullmatch(r"[A-Za-z0-9-]+\[bot\]", WATCHER_BOT), "WATCHER_BOT is invalid")
        require(os.environ.get("DRY_RUN", "true") in ("true", "false"), "DRY_RUN must be true or false")
        report = run()
    except (EvidenceError, OSError, ValueError, TypeError, KeyError) as exc:
        report["gaps"] = [str(exc) if isinstance(exc, EvidenceError) else "unexpected or malformed evidence"]
    report["write_attempts"] = WRITE_ATTEMPTS
    print(json.dumps(report, sort_keys=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as stream:
            stream.write("## Wildcat OpenAPI watch\n\n")
            stream.write(f"Status: **{report['status']}**; dry run: **{str(DRY_RUN).lower()}**.\n\n")
            stream.write(f"Proposed actions: **{len(report['proposed_actions'])}**; "
                         f"write attempts: **{WRITE_ATTEMPTS}**; verified writes: **{report['verified_writes']}**.\n\n")
            for label in ("source", "consumer"):
                if report[label]:
                    stream.write(f"- {label}: {report[label]['sha']}; "
                                 f"canonical SHA-256 {report[label]['canonical_sha256']}.\n")
            for gap in report["gaps"]:
                stream.write(f"- Not measured: {gap}\n")
    return 1 if report["gaps"] else 0


if __name__ == "__main__":
    sys.exit(main())
