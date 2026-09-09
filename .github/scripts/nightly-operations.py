#!/usr/bin/env python3
"""Reconcile one saved deployment or rollback with its immutable recipient evidence."""

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode

spec = importlib.util.spec_from_file_location("nightly_candidate_operations", Path(__file__).with_name("nightly-candidate.py"))
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)
Error = candidate.CandidateError
DEPLOYMENT = "Wildcat-deployment"
WORKFLOW = ".github/workflows/deploy.yml"
ROOT_WORKFLOWS = {"deploy": ".github/workflows/clowder-dev-nightly.yml", "rollback": ".github/workflows/clowder-dev-rollback.yml"}
REQUEST = "clowder-rollback-request"
INTENT = "clowder-dispatch-intent"
TARGETS = {f"clowder-dev-{n}" for n in range(5)}
DELETE_FLAGS = ("delete_all_data", "delete_treasury_data", "delete_surrealdb_data", "delete_postgres_data", "delete_clowder_node_data")
WAIT_SECONDS = 45 * 60


def require(condition, message):
    if not condition:
        raise Error(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read(path):
    require(path.stat().st_size <= 65536, "Operation metadata is too large")
    return json.loads(path.read_bytes())


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def native_run(cfg, run_id, operation=None):
    require(isinstance(run_id, str) and candidate.ID.fullmatch(run_id), "Invalid central run ID")
    run = candidate.run_identity(candidate.api(cfg, f"repos/{cfg['org']}/.github/actions/runs/{run_id}", own=True), run_id)
    path = run.get("path", "").split("@", 1)[0]
    require(path in ROOT_WORKFLOWS.values() and (operation is None or path == ROOT_WORKFLOWS[operation]),
            "The saved operation belongs to another central workflow")
    require(run.get("event") in ("workflow_dispatch", "schedule")
            and (path != ROOT_WORKFLOWS["rollback"] or run.get("event") == "workflow_dispatch")
            and isinstance(run.get("actor"), dict)
            and re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", str(run["actor"].get("login", ""))),
            "Invalid original operation initiator")
    require(run.get("head_branch") == "master" or cfg["dry"] and run_id == cfg["run"],
            "A live or accepted operation must originate from master")
    if run_id == cfg["run"]:
        require(run["head_sha"] == cfg["sha"] and run["run_attempt"] == cfg["attempt"]
                and run["actor"]["login"] == cfg["actor"], "The central run identity changed")
    candidate.timestamp(run.get("created_at"))
    return run


def source_context(cfg, run):
    return {**cfg, "run": str(run["id"]), "attempt": run["run_attempt"], "sha": run["head_sha"],
            "actor": run["actor"]["login"], "previous": None}


def named_artifact(cfg, repo, run, name, *, own=False, required=True):
    found = [a for a in candidate.artifacts(cfg, repo, run, own=own) if a["name"] == name]
    if not found:
        require(not required, f"Required immutable artifact {name} is missing; no fresh composition will be substituted")
        return None
    value, raw = candidate.artifact_json(cfg, repo, run, found[0], own=own)
    return value, raw, found[0]


def original(cfg, run_id):
    run = native_run(cfg, run_id, "deploy")
    saved_cfg = source_context(cfg, run)
    plan = candidate.saved_plan(saved_cfg)
    require(plan is not None, "The original candidate plan artifact is missing")
    images = candidate.saved_images(saved_cfg, plan[0])
    require(images is not None, "The original candidate image artifact is missing")
    payload = dict(schema=1, candidate_run_id=run_id, operation="deploy", plan=plan[0], images=images[0]["images"])
    return payload, dict(plan_artifact_id=plan[2], images_artifact_id=images[2]), run


def deployment_input(cfg, plan_path, images_path):
    plan = candidate.read_plan(cfg, plan_path)
    images = candidate.validate_images(cfg, plan, read(images_path))
    payload, provenance, run = original(cfg, cfg["run"])
    require(payload["plan"] == plan and payload["images"] == images["images"],
            "Local files differ from the immutable original candidate")
    return payload, payload["plan"]["members"][DEPLOYMENT], provenance, run


def title(candidate_id):
    return f"Deploy clowder-dev | candidate {candidate_id}"


def recipient_identity(run, candidate_id, sha=None):
    candidate.run_identity(run, sha=sha)
    require(run.get("path", "").split("@", 1)[0] == WORKFLOW and run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == "master" and run.get("display_title") == title(candidate_id),
            "Recipient workflow does not match the saved operation")
    require(run.get("status") in ("queued", "in_progress", "completed", "waiting", "pending", "requested")
            and (run["status"] != "completed" or isinstance(run.get("conclusion"), str) and run["conclusion"]),
            "Invalid recipient run status")
    return run


def recipient_runs(cfg, *, since=None):
    query = {"branch": "master", "event": "workflow_dispatch"}
    if since:
        query["created"] = ">=" + since
    return candidate.pages(cfg, f"repos/{cfg['org']}/{DEPLOYMENT}/actions/workflows/deploy.yml/runs?" + urlencode(query), "workflow_runs")


def matching_recipient(cfg, payload, sha, created):
    runs = [r for r in recipient_runs(cfg, since=created) if r.get("display_title") == title(payload["candidate_run_id"])]
    require(len(runs) <= 1, "Multiple recipient runs claim this candidate; inspect them before continuing")
    return recipient_identity(runs[0], payload["candidate_run_id"], sha) if runs else None


def validate_locks(locks, payload, run):
    require(isinstance(locks, dict) and set(locks) == TARGETS, "Recipient result omits a clowder-dev target")
    expected = {record["references"]["ghcr"] for record in payload["images"]}
    for target, lock in locks.items():
        require(isinstance(lock, dict) and set(lock) == {"schema", "candidate_run_id", "target", "deployment_sha", "run_id", "run_attempt", "services"}
                and type(lock["schema"]) is int and lock["schema"] == 1 and lock["target"] == target
                and lock["candidate_run_id"] == payload["candidate_run_id"] and lock["run_id"] == str(run["id"])
                and type(lock["run_attempt"]) is int and lock["run_attempt"] == run["run_attempt"]
                and lock["deployment_sha"] == payload["plan"]["members"][DEPLOYMENT],
                "A target lock belongs to another candidate, source or recipient attempt")
        services = lock["services"]
        require(isinstance(services, dict) and services and all(isinstance(k, str) and k
                and isinstance(v, str) and re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}", v)
                for k, v in services.items()) and expected.issubset(set(services.values())),
                "Target lock has incomplete or mutable image references")
        if payload["operation"] == "rollback":
            require(services == payload["rollback_locks"][target]["services"], "Rollback changed a saved service digest")


def validate_receipt(value, payload, run):
    require(isinstance(value, dict) and set(value) == {"schema", "candidate_run_id", "operation", "payload_sha256", "run_id", "run_attempt", "accepted", "restored", "locks", "functional", "errors"}
            and type(value["schema"]) is int and value["schema"] == 1
            and value["candidate_run_id"] == payload["candidate_run_id"] and value["operation"] == payload["operation"]
            and value["payload_sha256"] == digest(payload) and value["run_id"] == str(run["id"])
            and type(value["run_attempt"]) is int and value["run_attempt"] == run["run_attempt"],
            "Recipient result does not match the exact saved payload or attempt")
    require(value["errors"] == [] and value["accepted"] is (payload["operation"] == "deploy")
            and value["restored"] is (payload["operation"] == "rollback"),
            "Recipient result does not prove complete deployment or restoration")
    validate_locks(value["locks"], payload, run)
    functional = value["functional"]
    if payload["operation"] == "deploy":
        require(isinstance(functional, dict) and type(functional.get("schema")) is int and functional["schema"] == 1
                and functional.get("result") == "success" and functional.get("candidate_run_id") == payload["candidate_run_id"]
                and functional.get("deployment_run_id") == str(run["id"])
                and type(functional.get("deployment_run_attempt")) is int
                and functional.get("deployment_run_attempt") == run["run_attempt"]
                and functional.get("frontend_sha") == payload["plan"]["tests"]["E-Bill-frontend"]
                and functional.get("wallet_sha") == payload["plan"]["tests"]["wallet"],
                "Functional evidence belongs to another source or candidate")
    else:
        require(functional is None, "Rollback result must not claim a new functional acceptance")
    return value


def receipt(cfg, payload, run, artifact=None):
    require(run["status"] == "completed" and run["conclusion"] == "success",
            f"Recipient run {run['id']} is not successful; rerun all its jobs before resuming the original central run")
    if artifact is None:
        stored = named_artifact(cfg, DEPLOYMENT, run, f"clowder-deployment-result-{run['run_attempt']}")
    else:
        value, raw = candidate.artifact_json(cfg, DEPLOYMENT, run, artifact)
        stored = value, raw, artifact
    value, _, metadata = stored
    validate_receipt(value, payload, run)
    return value, metadata


def completed_receipts(cfg, payload, sha, run):
    results = []
    for artifact in candidate.artifacts(cfg, DEPLOYMENT, run):
        match = re.fullmatch(r"clowder-deployment-result-([1-9][0-9]*)", artifact["name"])
        if not match:
            continue
        attempt = int(match[1])
        require(attempt <= run["run_attempt"], "Recipient result belongs to a future attempt")
        attempt_run = run if attempt == run["run_attempt"] else candidate.api(
            cfg, f"repos/{cfg['org']}/{DEPLOYMENT}/actions/runs/{run['id']}/attempts/{attempt}")
        recipient_identity(attempt_run, payload["candidate_run_id"], sha)
        require(attempt_run["run_attempt"] == attempt, "Recipient attempt identity mismatch")
        if attempt_run["status"] == "completed" and attempt_run["conclusion"] == "success":
            results.append(receipt(cfg, payload, attempt_run, artifact))
    return results


def accepted_candidate(cfg, run_id):
    payload, provenance, root = original(cfg, run_id)
    expected_sha = payload["plan"]["members"][DEPLOYMENT]
    source_cfg = source_context(cfg, root)
    intent = saved_intent(source_cfg, payload, expected_sha, root)
    require(intent is not None, "Accepted candidate has no immutable dispatch intent")
    run = matching_recipient(cfg, payload, expected_sha, root["created_at"])
    require(run is not None, "Accepted candidate has no matching recipient run")
    results = completed_receipts(cfg, payload, expected_sha, run)
    require(results, "Candidate has no complete, successful deployment and functional result")
    value, artifact = max(results, key=lambda result: candidate.timestamp(result[1]["created_at"]))
    return payload, provenance, value, artifact, root


def compatibility():
    value = os.environ.get("DATA_COMPATIBLE", "false").lower()
    require(value in ("true", "false"), "Invalid data compatibility confirmation")
    return value == "true"


def validate_request(cfg, value, root, *, current=False):
    require(isinstance(value, dict) and set(value) == {"schema", "candidate_run_id", "workflow_sha", "initiator", "created_at", "accepted_run_id", "dry_run", "data_compatible", "deployment_workflow_sha", "source_result_artifact_id", "payload"}
            and type(value["schema"]) is int and value["schema"] == 1
            and value["candidate_run_id"] == str(root["id"]) and value["workflow_sha"] == root["head_sha"]
            and value["initiator"] == root["actor"]["login"]
            and isinstance(value["accepted_run_id"], str) and candidate.ID.fullmatch(value["accepted_run_id"])
            and value["accepted_run_id"] != value["candidate_run_id"]
            and type(value["dry_run"]) is bool and type(value["data_compatible"]) is bool
            and isinstance(value["deployment_workflow_sha"], str) and candidate.SHA.fullmatch(value["deployment_workflow_sha"])
            and candidate.positive(value["source_result_artifact_id"]), "Invalid saved rollback request identity")
    candidate.timestamp(value["created_at"])
    if current:
        require(value["dry_run"] == cfg["dry"] and value["data_compatible"] == compatibility(),
                "The original dry-run and compatibility choices cannot be promoted on a rerun")
    require(value["dry_run"] or value["data_compatible"], "Live rollback requires confirmed data compatibility")
    payload = value["payload"]
    require(isinstance(payload, dict) and set(payload) == {"schema", "candidate_run_id", "operation", "plan", "images", "data_compatible", "source_deployment_run_id", "rollback_locks"}
            and type(payload["schema"]) is int and payload["schema"] == 1 and payload["operation"] == "rollback"
            and payload["candidate_run_id"] == value["candidate_run_id"]
            and payload["data_compatible"] is value["data_compatible"], "Rollback payload differs from the saved request")
    original_payload, _, source_root = original(cfg, value["accepted_run_id"])
    require(payload["plan"] == original_payload["plan"] and payload["images"] == original_payload["images"],
            "Rollback changed the original accepted sources or image records")
    require(isinstance(payload["source_deployment_run_id"], str) and candidate.ID.fullmatch(payload["source_deployment_run_id"]),
            "Invalid source deployment identity")
    run = recipient_identity(candidate.api(cfg, f"repos/{cfg['org']}/{DEPLOYMENT}/actions/runs/{payload['source_deployment_run_id']}"),
                             value["accepted_run_id"], payload["plan"]["members"][DEPLOYMENT])
    artifacts = candidate.artifacts(cfg, DEPLOYMENT, run)
    found = [a for a in artifacts if a["id"] == value["source_result_artifact_id"]]
    require(len(found) == 1, "The saved acceptance artifact is unavailable")
    match = re.fullmatch(r"clowder-deployment-result-([1-9][0-9]*)", found[0]["name"])
    require(match is not None, "Saved acceptance artifact has the wrong name")
    attempt = int(match[1])
    if attempt != run["run_attempt"]:
        run = recipient_identity(candidate.api(cfg, f"repos/{cfg['org']}/{DEPLOYMENT}/actions/runs/{run['id']}/attempts/{attempt}"),
                                 value["accepted_run_id"], payload["plan"]["members"][DEPLOYMENT])
    require(run["run_attempt"] == attempt, "Saved acceptance attempt changed")
    accepted, _ = receipt(cfg, original_payload, run, found[0])
    require(payload["rollback_locks"] == accepted["locks"], "Rollback digests differ from the original accepted locks")
    saved_intent(source_context(cfg, source_root), original_payload, payload["plan"]["members"][DEPLOYMENT], source_root, required=True)
    return value


def rollback_request(cfg, accepted_run_id, path):
    root = native_run(cfg, cfg["run"], "rollback")
    saved = named_artifact(cfg, ".github", root, REQUEST, own=True, required=False)
    if saved:
        value = validate_request(cfg, saved[0], root, current=True)
        require(value["accepted_run_id"] == accepted_run_id, "A rerun cannot select another accepted candidate")
        path.write_bytes(saved[1])
        candidate.output("created", "false")
        candidate.output("artifact_id", saved[2]["id"])
        return value
    require(cfg["attempt"] == 1, "The original rollback request artifact is missing; refusing a new request")
    compatible = compatibility()
    require(cfg["dry"] or compatible, "Live rollback requires confirmed data compatibility")
    payload, _, accepted, artifact, _ = accepted_candidate(cfg, accepted_run_id)
    payload = {**payload, "candidate_run_id": cfg["run"], "operation": "rollback", "data_compatible": compatible,
               "source_deployment_run_id": accepted["run_id"], "rollback_locks": accepted["locks"]}
    value = dict(schema=1, candidate_run_id=cfg["run"], workflow_sha=cfg["sha"], initiator=cfg["actor"], created_at=now(),
                 accepted_run_id=accepted_run_id, dry_run=cfg["dry"], data_compatible=compatible,
                 deployment_workflow_sha=candidate.head_of(cfg, DEPLOYMENT), source_result_artifact_id=artifact["id"], payload=payload)
    validate_request(cfg, value, root, current=True)
    if path.exists():
        require(read(path) == value, "An existing local rollback request differs; it was not overwritten")
    else:
        write(path, value)
    candidate.output("created", "true")
    candidate.progress(f"Prepared rollback request for accepted candidate {accepted_run_id}; original choices and digests are frozen")
    return value


def rollback_input(cfg, path):
    root = native_run(cfg, cfg["run"], "rollback")
    saved = named_artifact(cfg, ".github", root, REQUEST, own=True)
    request = validate_request(cfg, saved[0], root, current=True)
    require(read(path) == request, "Local rollback request differs from its immutable artifact")
    require(not request["dry_run"] and request["data_compatible"], "This saved rollback request does not authorize live restoration")
    return request["payload"], request["deployment_workflow_sha"], dict(request_artifact_id=saved[2]["id"]), root


def saved_intent(cfg, payload, sha, root, *, required=False):
    found = named_artifact(cfg, ".github", root, INTENT, own=True, required=required)
    if found is None:
        return None
    value = found[0]
    require(isinstance(value, dict) and set(value) == {"schema", "candidate_run_id", "operation", "payload_sha256", "workflow_sha", "deployment_workflow_sha", "first_submission_attempt", "created_at"}
            and type(value["schema"]) is int and value["schema"] == 1
            and value["candidate_run_id"] == payload["candidate_run_id"] and value["operation"] == payload["operation"]
            and value["payload_sha256"] == digest(payload) and value["workflow_sha"] == root["head_sha"]
            and value["deployment_workflow_sha"] == sha
            and candidate.positive(value["first_submission_attempt"]) and value["first_submission_attempt"] <= root["run_attempt"],
            "Dispatch intent differs from the saved payload, source or first submission attempt")
    candidate.timestamp(value["created_at"])
    return found


def prove_no_prior_submission(cfg, root, operation):
    job_name, step_name = {
        "deploy": ("deploy", "Reconcile the locked deployment and functional-test operation"),
        "rollback": ("restore", "Restore saved digests under the shared environment lock"),
    }[operation]
    for attempt in range(1, cfg["attempt"]):
        jobs = candidate.pages(cfg, f"repos/{cfg['org']}/.github/actions/runs/{root['id']}/attempts/{attempt}/jobs", "jobs", own=True)
        require(all(type(job.get("run_id")) is int and job["run_id"] == root["id"]
                    and type(job.get("run_attempt")) is int and job["run_attempt"] == attempt
                    and job.get("head_sha") == root["head_sha"]
                    and isinstance(job.get("name"), str) and job["name"]
                    and job.get("status") == "completed"
                    and isinstance(job.get("conclusion"), str) and job["conclusion"]
                    and isinstance(job.get("steps"), list) for job in jobs),
                "Dispatch recovery: prior job history is incomplete or belongs to another attempt")
        matched = [job for job in jobs if job["name"] == job_name]
        require(len(matched) <= 1, "Dispatch recovery: the prior submission job is ambiguous")
        if not matched or matched[0]["conclusion"] == "skipped" and not matched[0]["steps"]:
            continue
        steps = matched[0]["steps"]
        require(all(isinstance(step, dict) and isinstance(step.get("name"), str) and step["name"]
                    and candidate.positive(step.get("number")) for step in steps)
                and len({step["number"] for step in steps}) == len(steps),
                "Dispatch recovery: prior step history is incomplete or ambiguous")
        selected = [step for step in steps if step["name"] == step_name]
        require(len(selected) == 1, "Dispatch recovery: the prior submission step is unmeasured")
        step = selected[0]
        require((step.get("status") == "completed" and step.get("conclusion") == "skipped")
                or (step.get("status") == "queued" and "conclusion" in step and step["conclusion"] is None
                    and "started_at" in step and step["started_at"] is None),
                "Dispatch recovery: prior submission may have started; refusing a replacement intent or POST")


def prepare_intent(cfg, inputs, path):
    require(not cfg["dry"], "A dry run cannot prepare a live dispatch intent")
    payload, sha, _, root = inputs
    found = saved_intent(cfg, payload, sha, root)
    if found:
        path.write_bytes(found[1])
        candidate.output("created", "false")
        candidate.output("artifact_id", found[2]["id"])
        candidate.progress(f"Restored dispatch intent: payload {digest(payload)}, first submission attempt {found[0]['first_submission_attempt']}")
        return found[0]
    prove_no_prior_submission(cfg, root, payload["operation"])
    require(matching_recipient(cfg, payload, sha, root["created_at"]) is None,
            "A recipient already exists without a dispatch intent; inspect its provenance")
    value = dict(schema=1, candidate_run_id=cfg["run"], operation=payload["operation"], payload_sha256=digest(payload),
                 workflow_sha=cfg["sha"], deployment_workflow_sha=sha, first_submission_attempt=cfg["attempt"], created_at=now())
    if path.exists():
        require(read(path) == value, "An existing local dispatch intent differs; it was not overwritten")
    else:
        write(path, value)
    candidate.output("created", "true")
    candidate.progress(f"Prepared dispatch intent: payload {digest(payload)}, first submission attempt {cfg['attempt']}; upload before execution")
    return value


def send_dispatch(cfg, payload):
    require(not cfg["dry"] and payload["operation"] in ("deploy", "rollback")
            and payload["candidate_run_id"] == cfg["run"]
            and (payload["operation"] != "rollback" or payload.get("data_compatible") is True),
            "Only the saved live clowder-dev operation may be dispatched")
    token = os.environ.get("GH_WRITE_TOKEN")
    require(bool(token), "GH_WRITE_TOKEN is unavailable")
    remaining = cfg.get("deadline", time.monotonic() + 30) - time.monotonic()
    require(remaining > 0, "Timed out before recipient dispatch")
    body = {"ref": "master", "inputs": {"environment": "clowder-dev", "image_tag": "nightly",
            "candidate_payload": canonical(payload), **{key: "false" for key in DELETE_FLAGS}}}
    require(len(canonical(body).encode()) <= 65536, "Deployment payload exceeds the dispatch limit")
    env = {k: v for k, v in os.environ.items() if k not in ("GH_READ_TOKEN", "GH_WRITE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN")}
    env["GH_TOKEN"] = token
    try:
        result = subprocess.run(["gh", "api", "--hostname", "github.com", "--method", "POST",
            f"repos/{cfg['org']}/{DEPLOYMENT}/actions/workflows/deploy.yml/dispatches",
            "-H", "X-GitHub-Api-Version: 2026-03-10", "--input", "-"], input=canonical(body).encode(),
            capture_output=True, env=env, timeout=min(30, remaining))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Error("Recipient dispatch response unavailable") from error
    require(result.returncode == 0, "Recipient dispatch response unavailable")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise Error("Recipient dispatch response did not identify a workflow run") from error
    require(isinstance(value, dict) and candidate.positive(value.get("workflow_run_id")),
            "Recipient dispatch response did not identify a workflow run")
    return str(value["workflow_run_id"])


def reconcile(cfg, inputs, destination, *, timeout=WAIT_SECONDS):
    require(not cfg["dry"], "Dry run does not dispatch deployment or rollback")
    payload, sha, provenance, root = inputs
    cfg = {**cfg, "deadline": time.monotonic() + min(timeout, WAIT_SECONDS)}
    intent, _, intent_artifact = saved_intent(cfg, payload, sha, root, required=True)
    destination.unlink(missing_ok=True)
    recipient_id, submitted = None, False
    while True:
        require(time.monotonic() < cfg["deadline"], "Timed out waiting for recipient evidence; inspect the existing recipient before resuming")
        run = matching_recipient(cfg, payload, sha, root["created_at"])
        if run is not None:
            if recipient_id is not None:
                require(str(run["id"]) == recipient_id, "Dispatch response and recipient readback disagree")
            recipient_id = str(run["id"])
            if run["status"] == "completed":
                result, artifact = receipt(cfg, payload, run)
                value = dict(schema=1, candidate_run_id=cfg["run"], operation=payload["operation"],
                             accepted_run_id=payload["plan"]["candidate_run_id"], payload_sha256=digest(payload),
                             first_submission_attempt=intent["first_submission_attempt"], intent_artifact_id=intent_artifact["id"],
                             deployment_run_id=recipient_id, deployment_run_attempt=run["run_attempt"], deployment_workflow_sha=sha,
                             deployment_result_artifact_id=artifact["id"], deployment_result_created_at=artifact["created_at"],
                             provenance=provenance, result=result)
                write(destination, value)
                candidate.output("accepted_run_id", value["accepted_run_id"])
                candidate.output("deployment_run_id", recipient_id)
                candidate.progress(f"Verified {payload['operation']} recipient {recipient_id}, attempt {run['run_attempt']}, result artifact {artifact['id']}")
                return value
        elif not submitted:
            if intent["first_submission_attempt"] != cfg["attempt"]:
                prove_no_prior_submission(cfg, root, payload["operation"])
            require(candidate.head_of(cfg, DEPLOYMENT) == sha, "Deployment master moved after capture; saved sources will not be replaced")
            submitted = True  # A missing/invalid response is never permission for another POST.
            try:
                recipient_id = send_dispatch(cfg, payload)
                returned = recipient_identity(candidate.api(cfg, f"repos/{cfg['org']}/{DEPLOYMENT}/actions/runs/{recipient_id}"),
                                              payload["candidate_run_id"], sha)
                require(str(returned["id"]) == recipient_id, "Dispatch response identified another recipient")
            except Error:
                candidate.progress("Recipient dispatch response uncertain; reading the exact candidate title without repeating POST")
        candidate.progress(f"Waiting for {payload['operation']} candidate {cfg['run']}; payload {digest(payload)}, first submission attempt {intent['first_submission_attempt']}")
        time.sleep(min(20, max(0, cfg["deadline"] - time.monotonic())))


def previous(cfg):
    # Parent artifact uploads can finish late. Only immutable recipient result time orders accepted states.
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).isoformat(timespec="seconds").replace("+00:00", "Z")
    values, seen = [], set()
    for run in recipient_runs(cfg, since=cutoff):
        match = re.fullmatch(r"Deploy clowder-dev \| candidate ([1-9][0-9]*)", str(run.get("display_title", "")))
        if not match or match[1] == cfg["run"]:
            continue
        candidate_id = match[1]
        require(candidate_id not in seen, "Multiple recipient runs claim one saved candidate")
        seen.add(candidate_id)
        # Failed attempts are diagnostic. Successful attempt receipts remain the accepted history.
        artifacts = candidate.artifacts(cfg, DEPLOYMENT, candidate.run_identity(run))
        if not any(re.fullmatch(r"clowder-deployment-result-[1-9][0-9]*", a["name"]) for a in artifacts):
            require(run.get("status") != "completed" or run.get("conclusion") != "success",
                    "A successful candidate recipient has no result artifact; previous acceptance is unmeasured")
            continue
        root = native_run(cfg, candidate_id)
        if root["path"].split("@", 1)[0] == ROOT_WORKFLOWS["deploy"]:
            payload, _, _ = original(cfg, candidate_id)
            sha = payload["plan"]["members"][DEPLOYMENT]
        else:
            stored = named_artifact(cfg, ".github", root, REQUEST, own=True)
            request = validate_request(cfg, stored[0], root)
            require(not request["dry_run"] and request["data_compatible"], "A dry-run request cannot establish restored acceptance")
            payload, sha = request["payload"], request["deployment_workflow_sha"]
        recipient_identity(run, candidate_id, sha)
        saved_intent(source_context(cfg, root), payload, sha, root, required=True)
        for _, artifact in completed_receipts(cfg, payload, sha, run):
            values.append((candidate.timestamp(artifact["created_at"]), artifact["id"], payload["plan"]["candidate_run_id"]))
    if values:
        latest_time = max(v[0] for v in values)
        latest = [v for v in values if v[0] == latest_time]
        require(len({v[2] for v in latest}) == 1, "Accepted recipient results have ambiguous ordering")
        selected = latest[0][2]
        candidate.progress(f"Previous accepted candidate: {selected}, ordered by recipient result time")
    else:
        selected = ""
        candidate.progress("No verified accepted candidate is available; scheduling requires an accepted baseline")
    candidate.output("previous_accepted_run_id", selected)
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="phase", required=True)
    commands.add_parser("previous")
    for name in ("deploy", "deploy-intent"):
        command = commands.add_parser(name)
        command.add_argument("plan", type=Path)
        command.add_argument("images", type=Path)
        command.add_argument("output", type=Path)
    request = commands.add_parser("rollback-request")
    request.add_argument("accepted_run_id")
    request.add_argument("output", type=Path)
    for name in ("rollback", "rollback-intent"):
        command = commands.add_parser(name)
        command.add_argument("request", type=Path)
        command.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        cfg = candidate.context()
        if args.phase == "previous":
            previous(cfg)
        elif args.phase == "rollback-request":
            rollback_request(cfg, args.accepted_run_id, args.output)
        else:
            inputs = deployment_input(cfg, args.plan, args.images) if args.phase.startswith("deploy") else rollback_input(cfg, args.request)
            if args.phase.endswith("-intent"):
                prepare_intent(cfg, inputs, args.output)
            else:
                reconcile(cfg, inputs, args.output)
        return 0
    except (Error, ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        candidate.progress(f"Nightly operation stopped: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
