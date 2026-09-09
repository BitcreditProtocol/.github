#!/usr/bin/env python3
"""Prepare one immutable candidate and collect its verified producer images."""

import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from urllib.parse import urlencode
import zipfile

MEMBERS = ("Wildcat", "Clowder", "Wildcat-Auxiliary", "Wildcat-deployment", "wildcat-dashboard-ui")
TESTS = ("E-Bill-frontend", "wallet")
IMAGES = {
    "Wildcat": tuple("bcr-wdc-" + n for n in ("core-service", "treasury-service", "quote-service",
                                             "wallet-aggregator", "admin-aggregator")),
    "Clowder": ("clowder",),
    "Wildcat-Auxiliary": tuple("bcr-wdc-" + n for n in ("eic-service", "ens-service", "ebill-service",
                                                       "relay", "demo-faucet")),
    "wildcat-dashboard-ui": ("bcr-wdc-dashboard-ui",),
}
ARTIFACT = "clowder-nightly-plan"
IMAGES_ARTIFACT = "clowder-nightly-images"
GAR = "europe-west1-docker.pkg.dev/bitcr-shared/bitcr-wildcat-dev"
SHA = re.compile(r"[0-9a-f]{40}")
ID = re.compile(r"[1-9][0-9]*")
PLAN_KEYS = {"schema", "candidate_run_id", "initiator", "workflow_sha", "members", "tests",
             "previous_accepted_run_id", "created_at"}
IMAGE_KEYS = {"schema", "repository", "sha", "run_id", "run_attempt", "candidate_run_id",
              "image", "source_tag", "references"}
POLL_SECONDS = 20
WAIT_SECONDS = 45 * 60
last_progress = 0.0


class CandidateError(RuntimeError):
    pass


def progress(message):
    global last_progress
    print(message, flush=True)
    last_progress = time.monotonic()


def output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"{name}={value}\n")


def positive(value):
    return type(value) is int and value > 0


def timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CandidateError("Expected a UTC timestamp")
    try:
        result = dt.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise CandidateError("Invalid UTC timestamp") from error
    return result


def context():
    cfg = dict(org=os.environ.get("ORG", "BitcreditProtocol"),
               run=os.environ.get("GITHUB_RUN_ID", ""),
               attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
               sha=os.environ.get("GITHUB_SHA", ""), actor=os.environ.get("GITHUB_ACTOR", ""))
    dry = os.environ.get("DRY_RUN", "true").lower()
    previous = os.environ.get("PREVIOUS_ACCEPTED_RUN_ID", "") or None
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", cfg["org"])
            or not ID.fullmatch(cfg["run"]) or not ID.fullmatch(cfg["attempt"])
            or not SHA.fullmatch(cfg["sha"])
            or not re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", cfg["actor"])
            or dry not in ("true", "false")
            or previous is not None and (not ID.fullmatch(previous) or previous == cfg["run"])):
        raise CandidateError("Invalid candidate run identity or configuration")
    cfg.update(attempt=int(cfg["attempt"]), dry=dry == "true", previous=previous)
    return cfg


def check_deadline(cfg):
    remaining = cfg.get("deadline", float("inf")) - time.monotonic()
    if remaining <= 0:
        raise CandidateError("Timed out waiting for complete candidate evidence; inspect existing runs before retrying")
    return remaining


def api(cfg, path, method="GET", body=None, *, own=False, raw=False):
    check_deadline(cfg)
    roots = [f"repos/{cfg['org']}/{repo}" for repo in (*MEMBERS, *TESTS, ".github")]
    if not any(path == root or path.startswith(root + "/") for root in roots):
        raise CandidateError("GitHub request is outside the candidate repositories")
    if own and not path.startswith(f"repos/{cfg['org']}/.github/"):
        raise CandidateError("The native token is restricted to this repository's artifacts")
    if method != "GET":
        allowed = {f"repos/{cfg['org']}/{r}/actions/workflows/nightly.yml/dispatches" for r in IMAGES}
        if cfg["dry"] or method != "POST" or path not in allowed or own:
            raise CandidateError("Only producer dispatches are permitted, outside dry-run")
        if (not isinstance(body, dict) or body.get("ref") != "master"
                or set(body.get("inputs", {})) != {"expected_sha", "candidate_run_id"}
                or not SHA.fullmatch(str(body["inputs"]["expected_sha"]))
                or body["inputs"]["candidate_run_id"] != cfg["run"]):
            raise CandidateError("Invalid producer dispatch identity")
    key = "GITHUB_TOKEN" if own else "GH_READ_TOKEN" if method == "GET" else "GH_WRITE_TOKEN"
    token = os.environ.get(key)
    if not token:
        raise CandidateError(f"{key} is unavailable")
    env = {k: v for k, v in os.environ.items()
           if k not in ("GH_READ_TOKEN", "GH_WRITE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN")}
    env["GH_TOKEN"] = token
    if time.monotonic() - last_progress >= 30:
        progress(f"Reading GitHub state: {path.split('?')[0]}")
    command = ["gh", "api", "--hostname", "github.com", "--method", method, path,
               "-H", "X-GitHub-Api-Version: 2026-03-10"]
    if body is not None:
        command += ["--input", "-"]
    try:
        result = subprocess.run(command, input=json.dumps(body).encode() if body is not None else None,
                                capture_output=True, env=env, timeout=min(30, check_deadline(cfg)))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CandidateError("GitHub response unavailable") from error
    if result.returncode:
        status = re.search(rb"HTTP ([0-9]{3})", result.stderr)
        raise CandidateError("GitHub request failed (HTTP " + (status[1].decode() if status else "unknown") + ")")
    if raw:
        return result.stdout
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise CandidateError("GitHub returned invalid JSON") from error


def pages(cfg, path, key, *, own=False):
    rows, page, count = [], 1, None
    while True:
        value = api(cfg, path + ("&" if "?" in path else "?") + f"per_page=100&page={page}", own=own)
        if (not isinstance(value, dict) or type(value.get("total_count")) is not int
                or value["total_count"] < 0 or not isinstance(value.get(key), list)
                or len(value[key]) > 100 or any(not isinstance(row, dict) for row in value[key])):
            raise CandidateError(f"Invalid {key} page")
        count = value["total_count"] if count is None else count
        if count != value["total_count"]:
            raise CandidateError(f"{key} changed during pagination")
        rows.extend(value[key])
        if len(value[key]) < 100:
            if len(rows) != count or any(not positive(r.get("id")) for r in rows) or len({r["id"] for r in rows}) != count:
                raise CandidateError(f"Incomplete or duplicate {key} listing")
            return rows
        page += 1


def run_identity(run, run_id=None, sha=None):
    if (not isinstance(run, dict) or not positive(run.get("id"))
            or not positive(run.get("run_attempt")) or not isinstance(run.get("head_sha"), str)
            or not SHA.fullmatch(run["head_sha"])
            or run_id is not None and str(run["id"]) != str(run_id)
            or sha is not None and run["head_sha"] != sha):
        raise CandidateError("Workflow run identity does not match the saved source")
    return run


def own_run(cfg):
    run = run_identity(api(cfg, f"repos/{cfg['org']}/.github/actions/runs/{cfg['run']}", own=True),
                       cfg["run"], cfg["sha"])
    if run.get("event") not in ("workflow_dispatch", "schedule") or run["run_attempt"] != cfg["attempt"]:
        raise CandidateError("The candidate must belong to a scheduled or manually dispatched run")
    return run


def artifacts(cfg, repo, run, *, own=False):
    rows = pages(cfg, f"repos/{cfg['org']}/{repo}/actions/runs/{run['id']}/artifacts", "artifacts", own=own)
    if (any(not isinstance(a.get("name"), str) or not a["name"] or type(a.get("expired")) is not bool for a in rows)
            or len({a["name"] for a in rows}) != len(rows)):
        raise CandidateError("Invalid or ambiguous artifact identities")
    return rows


def artifact_json(cfg, repo, run, artifact, *, own=False):
    recorded = artifact.get("workflow_run")
    if (artifact["expired"] or not isinstance(recorded, dict) or recorded.get("id") != run["id"]
            or recorded.get("head_sha") != run["head_sha"]
            or timestamp(artifact.get("expires_at")) <= dt.datetime.now(dt.timezone.utc)
            or timestamp(artifact.get("expires_at")) <= timestamp(artifact.get("created_at"))):
        raise CandidateError("Artifact is expired or belongs to another workflow run")
    raw = api(cfg, f"repos/{cfg['org']}/{repo}/actions/artifacts/{artifact['id']}/zip", own=own, raw=True)
    if len(raw) > 1024 * 1024:
        raise CandidateError("Candidate metadata artifact is too large")
    digest = artifact.get("digest")
    if digest is not None and digest != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise CandidateError("Artifact archive digest mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.infolist()
            if (len(names) != 1 or names[0].is_dir() or names[0].file_size > 65536
                    or PurePosixPath(names[0].filename).name != names[0].filename
                    or not names[0].filename.endswith(".json")):
                raise CandidateError("Artifact must contain exactly one metadata JSON file")
            data = archive.read(names[0])
        return json.loads(data), data
    except (ValueError, UnicodeError, zipfile.BadZipFile) as error:
        raise CandidateError("Invalid metadata artifact") from error


def validate_plan(cfg, plan):
    if (not isinstance(plan, dict) or set(plan) != PLAN_KEYS or type(plan["schema"]) is not int
            or plan["schema"] != 1 or plan["candidate_run_id"] != cfg["run"]
            or plan["workflow_sha"] != cfg["sha"] or plan["initiator"] != cfg["actor"]):
        raise CandidateError("Plan belongs to another source, initiator or candidate run")
    for key, expected in (("members", MEMBERS), ("tests", TESTS)):
        values = plan[key]
        if not isinstance(values, dict) or set(values) != set(expected) or any(not isinstance(s, str) or not SHA.fullmatch(s) for s in values.values()):
            raise CandidateError(f"Plan requires the complete saved {key} SHA set")
    previous = plan["previous_accepted_run_id"]
    if previous is not None and (not isinstance(previous, str) or not ID.fullmatch(previous) or previous == cfg["run"]):
        raise CandidateError("Invalid previous accepted run ID")
    timestamp(plan["created_at"])
    return plan


def saved_plan(cfg):
    run = own_run(cfg)
    found = [a for a in artifacts(cfg, ".github", run, own=True) if a["name"] == ARTIFACT]
    if not found:
        return None
    value, raw = artifact_json(cfg, ".github", run, found[0], own=True)
    return validate_plan(cfg, value), raw, found[0]["id"]


def read_plan(cfg, path):
    if path.stat().st_size > 65536:
        raise CandidateError("Plan file is too large")
    return validate_plan(cfg, json.loads(path.read_bytes()))


def head_of(cfg, repo):
    value = api(cfg, f"repos/{cfg['org']}/{repo}/commits/master")
    if not isinstance(value, dict) or not isinstance(value.get("sha"), str) or not SHA.fullmatch(value["sha"]):
        raise CandidateError(f"{repo}: invalid master SHA")
    return value["sha"]


def gate_members(cfg, plan):
    # Reuse the existing train gate with a read-only adapter and an isolated module.
    spec = importlib.util.spec_from_file_location("nightly_train_gate", Path(__file__).with_name("release-train.py"))
    train = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(train)
    train.ORG = cfg["org"]
    def read_only(path, method="GET", body=None, **_):
        if method != "GET":
            raise CandidateError("The reused CI gate cannot write")
        return api(cfg, path)
    train.api = read_only
    for repo, sha in plan["members"].items():
        progress(f"{repo}: checking CI for saved commit {sha}")
        try:
            ok, detail = train.gate(repo, sha)
        except train.APIError as error:
            raise CandidateError(str(error)) from error
        if not ok:
            raise CandidateError(f"{repo}: {detail}")


def prepare(cfg, path):
    stored = saved_plan(cfg)
    if stored:
        plan, raw, artifact_id = stored
        gate_members(cfg, plan)
        path.write_bytes(raw)
        output("created", "false")
        output("artifact_id", artifact_id)
        progress(f"Restored candidate {cfg['run']} from artifact {artifact_id}")
        return plan
    if cfg["attempt"] > 1:
        raise CandidateError("Original candidate artifact is missing; refusing fresh master heads")
    if path.exists():
        plan = read_plan(cfg, path)
    else:
        heads = {repo: head_of(cfg, repo) for repo in (*MEMBERS, *TESTS)}
        plan = dict(schema=1, candidate_run_id=cfg["run"], initiator=cfg["actor"], workflow_sha=cfg["sha"],
                    members={r: heads[r] for r in MEMBERS}, tests={r: heads[r] for r in TESTS},
                    previous_accepted_run_id=cfg["previous"],
                    created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    validate_plan(cfg, plan)
    gate_members(cfg, plan)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    output("created", "true")
    progress(f"Prepared candidate {cfg['run']}; upload {ARTIFACT} with overwrite disabled and 90-day retention")
    return plan


def title(candidate, sha):
    return f"Nightly images | candidate {candidate} | {sha}"


def producer_run(run):
    run_identity(run)
    if (not isinstance(run.get("path"), str) or run["path"].split("@", 1)[0] != ".github/workflows/nightly.yml"
            or not isinstance(run.get("display_title"), str)
            or not isinstance(run.get("head_branch"), str) or not run["head_branch"]
            or not isinstance(run.get("event"), str) or not run["event"]
            or run.get("status") not in ("queued", "in_progress", "completed", "waiting", "pending", "requested")
            or run.get("status") == "completed" and (not isinstance(run.get("conclusion"), str) or not run["conclusion"])):
        raise CandidateError("Invalid nightly workflow run metadata")
    return run


def runs(cfg, plan, repo, *, own_only=False):
    query = {"branch": "master", "event": "workflow_dispatch", "created": ">=" + plan["created_at"]} if own_only else {
        "branch": "master", "head_sha": plan["members"][repo]}
    values = pages(cfg, f"repos/{cfg['org']}/{repo}/actions/workflows/nightly.yml/runs?" + urlencode(query), "workflow_runs")
    return [producer_run(r) for r in values]


def validate_image(cfg, plan, repo, run, image, record, *, attempt=None):
    candidate = record.get("candidate_run_id") if isinstance(record, dict) else None
    attempt = run["run_attempt"] if attempt is None else attempt
    expected_tag = f"source-{run['head_sha']}-{run['id']}-{attempt}"
    if (not isinstance(record, dict) or set(record) != IMAGE_KEYS or type(record["schema"]) is not int
            or record["schema"] != 1 or record["repository"] != f"{cfg['org']}/{repo}"
            or record["sha"] != plan["members"][repo] or record["sha"] != run["head_sha"]
            or record["run_id"] != str(run["id"]) or type(record["run_attempt"]) is not int
            or not positive(attempt) or attempt > run["run_attempt"]
            or record["run_attempt"] != attempt or record["image"] != image
            or record["source_tag"] != expected_tag or not isinstance(candidate, str)
            or run["event"] == "push" and candidate != ""
            or run["event"] == "workflow_dispatch" and (not ID.fullmatch(candidate)
                                                        or run["display_title"] != title(candidate, run["head_sha"]))):
        raise CandidateError(f"{repo}/{image}: image source/run identity mismatch")
    expected = {"ghcr": f"ghcr.io/{cfg['org'].lower()}/{image}"}
    if repo != "wildcat-dashboard-ui":
        expected["gar"] = f"{GAR}/{image}"
    refs = record["references"]
    if not isinstance(refs, dict) or set(refs) != set(expected) or any(
            not isinstance(refs[key], str) or not re.fullmatch(re.escape(prefix) + r"@sha256:[0-9a-f]{64}", refs[key])
            for key, prefix in expected.items()):
        raise CandidateError(f"{repo}/{image}: mutable, missing or incorrect registry reference")
    return record


def validate_images(cfg, plan, value):
    if (not isinstance(value, dict) or set(value) != {"schema", "candidate_run_id", "members", "images"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or value["candidate_run_id"] != plan["candidate_run_id"] or value["members"] != plan["members"]
            or not isinstance(value["images"], list)
            or len(value["images"]) != sum(map(len, IMAGES.values()))):
        raise CandidateError("Saved images do not match the complete candidate plan")
    seen, receipt_ids, sources = set(), set(), {}
    for record in value["images"]:
        if (not isinstance(record, dict) or set(record) != IMAGE_KEYS | {"artifact_id", "source_run_id"}
                or not positive(record["artifact_id"]) or record["artifact_id"] in receipt_ids
                or not positive(record["run_attempt"])
                or not isinstance(record["run_id"], str) or not ID.fullmatch(record["run_id"])
                or record["source_run_id"] != record["run_id"]):
            raise CandidateError("Invalid saved image receipt identity")
        repo = next((r for r in IMAGES if record["repository"] == f"{cfg['org']}/{r}"), None)
        if repo is None or record["image"] not in IMAGES[repo] or (repo, record["image"]) in seen:
            raise CandidateError("Saved images contain an unknown or duplicate matrix entry")
        source = record["run_id"], record["candidate_run_id"]
        if repo in sources and sources[repo] != source:
            raise CandidateError("Saved images mix different producer runs")
        sources[repo] = source
        seen.add((repo, record["image"]))
        receipt_ids.add(record["artifact_id"])
        run = dict(id=int(record["run_id"]), head_sha=plan["members"][repo], run_attempt=record["run_attempt"],
                   event="workflow_dispatch" if record["candidate_run_id"] else "push",
                   display_title=title(record["candidate_run_id"], plan["members"][repo]))
        validate_image(cfg, plan, repo, run, record["image"], {k: record[k] for k in IMAGE_KEYS})
    return value


def saved_images(cfg, plan):
    run = own_run(cfg)
    found = [a for a in artifacts(cfg, ".github", run, own=True) if a["name"] == IMAGES_ARTIFACT]
    if not found:
        return None
    value, raw = artifact_json(cfg, ".github", run, found[0], own=True)
    return validate_images(cfg, plan, value), raw, found[0]["id"]


def image_records(cfg, plan, repo, run, cache):
    key = (repo, run["id"], run["run_attempt"])
    if key in cache and cache[key][1] > dt.datetime.now(dt.timezone.utc):
        return cache[key][0]
    available = {}
    for artifact in artifacts(cfg, repo, run):
        if not artifact["name"].startswith("nightly-image-"):
            continue
        parts = artifact["name"].removeprefix("nightly-image-").rsplit("-", 1)
        if (len(parts) != 2 or parts[0] not in IMAGES[repo] or not ID.fullmatch(parts[1])
                or int(parts[1]) > run["run_attempt"]):
            raise CandidateError(f"{repo}: unexpected image artifact identity")
        image, attempt = parts[0], int(parts[1])
        if image not in available or attempt > available[image][0]:
            available[image] = attempt, artifact
    records, expirations = [], []
    for image in IMAGES[repo]:
        if image not in available:
            continue
        attempt, artifact = available[image]
        record, _ = artifact_json(cfg, repo, run, artifact)
        validate_image(cfg, plan, repo, run, image, record, attempt=attempt)
        records.append({**record, "artifact_id": artifact["id"], "source_run_id": str(run["id"])})
        expirations.append(timestamp(artifact["expires_at"]))
    if len(records) != len(IMAGES[repo]):
        return None
    cache[key] = records, min(expirations)
    return records


def scan(cfg, plan, repo, cache, *, require_own=False):
    expected = plan["members"][repo]
    own = [r for r in runs(cfg, plan, repo, own_only=True)
           if r["display_title"] == title(cfg["run"], expected)]
    if own:
        run = max(own, key=lambda r: r["id"])
        if run["head_sha"] != expected or run.get("head_branch") != "master" or run.get("event") != "workflow_dispatch":
            raise CandidateError(f"{repo}: existing candidate dispatch used a different source")
        if run["status"] != "completed":
            return run, None
        if run["conclusion"] != "success":
            raise CandidateError(f"{repo}: existing candidate run {run['id']} is {run['conclusion']}; rerun that producer run")
        records = image_records(cfg, plan, repo, run, cache)
        if records is None:
            raise CandidateError(f"{repo}: candidate run {run['id']} has an incomplete image matrix")
        return run, records
    if require_own:
        return None, None
    for run in sorted(runs(cfg, plan, repo), key=lambda r: r["id"], reverse=True):
        if (run["head_sha"] == expected and run.get("head_branch") == "master"
                and run.get("event") in ("push", "workflow_dispatch")
                and run["status"] == "completed" and run["conclusion"] == "success"):
            records = image_records(cfg, plan, repo, run, cache)
            if records is not None:
                return run, records
    return None, None


def dispatch(cfg, plan, repo):
    expected = plan["members"][repo]
    if head_of(cfg, repo) != expected:
        raise CandidateError(f"{repo}: master moved after capture; refusing a different build")
    path = f"repos/{cfg['org']}/{repo}/actions/workflows/nightly.yml/dispatches"
    body = {"ref": "master", "inputs": {"expected_sha": expected, "candidate_run_id": cfg["run"]}}
    try:
        value = api(cfg, path, "POST", body)
        run_id = value.get("workflow_run_id") if isinstance(value, dict) else None
        if not positive(run_id):
            raise CandidateError("Dispatch response did not identify a workflow run")
    except CandidateError:
        progress(f"{repo}: dispatch response uncertain; reading matching runs before any further decision")
        found = [r for r in runs(cfg, plan, repo, own_only=True)
                 if r["display_title"] == title(cfg["run"], expected)]
        if not found:
            return None  # The caller polls; it never sends another POST in this pass.
        run_id = max(found, key=lambda r: r["id"])["id"]
    run = producer_run(api(cfg, f"repos/{cfg['org']}/{repo}/actions/runs/{run_id}"))
    if (run["head_sha"] != expected or run.get("head_branch") != "master"
            or run.get("event") != "workflow_dispatch" or run["display_title"] != title(cfg["run"], expected)):
        raise CandidateError(f"{repo}: dispatched run does not match the saved candidate")
    return str(run_id)


def images(cfg, plan_path, destination, *, timeout=WAIT_SECONDS):
    cfg = {**cfg, "deadline": min(cfg.get("deadline", float("inf")), time.monotonic() + min(timeout, WAIT_SECONDS))}
    output("created", "false")
    output("complete", "false")
    plan = read_plan(cfg, plan_path)
    stored = saved_plan(cfg)
    if stored is None or stored[0] != plan:
        raise CandidateError("Image preparation requires the immutable original plan artifact")
    captured = saved_images(cfg, plan)
    if captured is not None:
        value, raw, artifact_id = captured
        destination.write_bytes(raw)
        output("artifact_id", artifact_id)
        output("source_run_ids", json.dumps({r["repository"].split("/")[1]: r["run_id"] for r in value["images"]}, sort_keys=True))
        output("complete", "true")
        progress(f"Restored candidate {cfg['run']} images from artifact {artifact_id}; preserved the original producer results")
        return value
    destination.unlink(missing_ok=True)
    attempted, selected, verified = set(), {}, {}
    while True:
        check_deadline(cfg)
        states = {}
        for repo in IMAGES:
            progress(f"{repo}: reading builds and image artifacts for {plan['members'][repo]}")
            states[repo] = scan(cfg, plan, repo, verified, require_own=repo in attempted)
        selected = {r: str(run["id"]) for r, (run, _) in states.items() if run is not None}
        output("source_run_ids", json.dumps(selected, sort_keys=True))
        if all(records is not None for _, records in states.values()):
            value = dict(schema=1, candidate_run_id=plan["candidate_run_id"], members=plan["members"],
                         images=[record for _, records in states.values() for record in records])
            validate_images(cfg, plan, value)
            destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
            output("created", "true")
            output("complete", "true")
            progress(f"Candidate {cfg['run']}: all 12 image records verified; no deployment performed")
            return value
        missing = [r for r, (run, records) in states.items() if run is None and records is None and r not in attempted]
        if cfg["dry"]:
            for repo, (run, records) in states.items():
                if records is None:
                    progress(f"Dry run: {repo}: " + (f"existing run {run['id']} is {run['status']}" if run else "build required; no dispatch"))
            return None
        # All available image artifacts and all missing masters are read before the first dispatch.
        for repo in missing:
            if head_of(cfg, repo) != plan["members"][repo]:
                raise CandidateError(f"{repo}: master moved after capture; no dispatch")
        for repo in missing:
            attempted.add(repo)
            run_id = dispatch(cfg, plan, repo)
            progress(f"{repo}: " + (f"dispatched run {run_id}" if run_id else "waiting for dispatch readback; no repeat POST"))
        progress("Waiting for producer completion and immutable image artifacts")
        time.sleep(min(POLL_SECONDS, check_deadline(cfg)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="phase", required=True)
    commands.add_parser("prepare").add_argument("plan", type=Path)
    collect = commands.add_parser("images")
    collect.add_argument("plan", type=Path)
    collect.add_argument("images", type=Path)
    args = parser.parse_args()
    try:
        cfg = context()
        if args.phase == "prepare":
            prepare(cfg, args.plan)
        else:
            images(cfg, args.plan, args.images)
        return 0
    except (CandidateError, ValueError, KeyError, TypeError, AttributeError, OSError, zipfile.BadZipFile) as error:
        progress(f"Nightly candidate stopped: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
