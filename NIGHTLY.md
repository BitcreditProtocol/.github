# Clowder development nightly operations

The coordinator prepares one saved candidate, builds missing images, and asks
`Wildcat-deployment/deploy.yml` to deploy and test it. It never deploys production.
The schedule is Sunday through Thursday at 23:00 in `Europe/Vienna`, using
GitHub's [native schedule timezone](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule).
GitHub may delay a scheduled run.

Keep `CLOWDER_NIGHTLY_ENABLED` unset or `false` until the activation checks below
are complete. The older deployment `nightly.yml` remains disabled: it targets
`wildcat-dev` and requests data deletion.

## Before a real candidate

1. Verify the merged PostgreSQL restoration in `Wildcat-deployment#154`, then
   merge and verify readiness changes in `#156` and their dependent nightly integration.
2. Merge the four producer interfaces, frontend correlation, wallet receipt,
   and central coordinator changes linked from `infrastructure#246`. Run their
   ordinary checks on the merged commits.
3. Verify the existing `wildcat-deployment-app` installation covers Wildcat,
   Clowder, Wildcat-Auxiliary, wildcat-dashboard-ui, Wildcat-deployment,
   E-Bill-frontend and wallet. Preserve other existing grants. Do not add Governance.
4. Make `WILDCAT_DEPLOYMENT_APP_CLIENT_ID` and
   `WILDCAT_DEPLOYMENT_APP_PRIVATE_KEY` available to the central repository,
   and Wildcat-deployment. The coordinator requests dispatch access only for the
   current stage's targets. Frontend and wallet parent checks use the existing
   `private-repo-access-for-ci` App instead, requesting only Actions read for
   `Wildcat-deployment`. The frontend candidate uses
   `PRIVATE_REPO_ACCESS_APP_ID`; wallet's `dev` candidate in `wallet#1115` uses
   `PRIVATE_REPO_ACCESS_CLIENT_ID`. Both use the existing
   `PRIVATE_REPO_ACCESS_APP_PRIVATE_KEY`; preserve these identifier names.
   Merge the explicit Contents-read limits for all 19 Git-token creation sites
   before adding Actions read to this CI App. For wallet, the default-branch
   dispatch handler must receive the `dev` changes through normal branch
   integration. A merge into `dev` alone does not activate that handler.
   Preserve the approved nine selected
   repositories; they already include the parent deployment and exclude Governance.
   Add only E-Bill-frontend to the existing organisation ID/key recipients,
   preserving all seven current recipients and the existing key value. Read back
   the permissions and grants, then verify parent-run access with GET requests
   from frontend and wallet without dispatching tests or deployment.
   The CI App remains read-only, but a holder of its key can request Actions read
   across its installed repositories. Product jobs and the App-token Action's
   post step still share a runner; this is not signing-key isolation. Preserve
   immediate parent checks and native token revocation. Regression jobs do not
   receive App keys. Never copy keys into plans, issues, logs or artifacts.
5. Demonstrate backup and restore for the self-hosted clowder-dev data. A GCP
   backup or the presence of a backup script does not prove this. Record the
   restore result and recovery procedure in `infrastructure#246`.

These are prerequisites. They are not established by offline regression tests or
by a successful candidate preparation. This change does not provision credentials
or claim that backup/restore has already been demonstrated.

If the new CI App access must be rolled back, remove only its added Actions-read
permission and the newly added frontend ID/key grants. Keep the Git-token limits
and the nightly schedule disabled; do not restore a deployment-writing key to
product test jobs. Existing wallet dispatch credentials and routes stay unchanged.

## Read-only operator inventory

Use this checklist to answer the
[existing operator questions in #246](https://github.com/BitcreditProtocol/infrastructure/issues/246#issuecomment-5617208957).
Record a separate result for each of `clowder-dev-0` through `clowder-dev-4`.
They share `clowder-dev/docker-compose.yml` and its inherited services, with
`.env-github` and the corresponding `clowder-dev/env-0` through `env-4`.
The source inventory in that issue is a starting point, not proof of live paths
or backup coverage. Do not substitute the Ansible default for the actual mounts.

An authorized operator runs the following on the intended host. First match the
hostname and Docker daemon to the target's GitHub runner; do not assume the
current Docker context is local. These commands list metadata only:

```sh
date -u '+%Y-%m-%dT%H:%M:%SZ'
hostname
docker info --format '{{.Name}}'
docker ps --all --filter label=com.docker.compose.project \
  --format 'table {{.ID}}\t{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}\t{{.Status}}'
```

Copy the observed project name into `NIGHTLY_PROJECT` below. Inspect every
container in that project, including stopped init containers and replicas:

```sh
set -eu
: "${NIGHTLY_PROJECT:?Set the observed Compose project for this target}"
nightly_ids="$(docker ps --all --quiet --filter "label=com.docker.compose.project=$NIGHTLY_PROJECT")"
test -n "$nightly_ids" || { echo 'No containers found; inventory is incomplete' >&2; exit 1; }
for nightly_id in $nightly_ids; do
  docker inspect --type container --format 'id={{.Id}} service={{index .Config.Labels "com.docker.compose.service"}} state={{.State.Status}} exit={{.State.ExitCode}} image={{.Config.Image}} image_id={{.Image}} config_files={{index .Config.Labels "com.docker.compose.project.config_files"}}' "$nightly_id"
  docker inspect --type container --format '{{range .Mounts}}type={{.Type}} name={{if .Name}}{{.Name}}{{else}}-{{end}} source={{.Source}} destination={{.Destination}} writable={{.RW}}{{println}}{{end}}' "$nightly_id"
  nightly_image_id="$(docker inspect --type container --format '{{.Image}}' "$nightly_id")"
  docker image inspect --format '{{json .RepoDigests}}' "$nightly_image_id"
done
```

An empty digest list is **unmeasured**, not the requested tag. A failed read,
missing label, missing expected service or unexplained replica is an inventory
gap. Compare the observed services and Compose file labels with the last
deployment's configuration SHA and manifest, following all `extends` files.
Do not count an old stopped container as a current healthy service. If no prior
manifest or configuration revision is available, record that absence.
Do not print `Config.Env`, full `docker inspect`, expanded Compose configuration,
environment files, database contents or credentials. See Docker's
[filtered container listing](https://docs.docker.com/reference/cli/docker/container/ls/)
and [formatted inspection](https://docs.docker.com/reference/cli/docker/inspect/).

Match each target's observed mounts to these storage responsibilities:

| Source scope | Recovery coverage to establish |
| --- | --- |
| `DATA_PATH/postgres` | The complete cluster and actual database list, including relay, Clowder and Wildcat database families; not the relay database alone. |
| `DATA_PATH/surrealdb` | All configured namespaces/databases used by core, quote, aggregator, treasury, ebill, eic and ens. |
| `DATA_PATH/treasury-service` | Treasury file state, together with its database state in the stores above. |
| `DATA_PATH/clowder-node` | Clowder file state and its PostgreSQL database; identify protected recovery sources for the signatory configuration separately. |
| Inherited Keycloak | The source uses `dev-file` and mounts a realm import, without a declared runtime-database bind. Establish the live database location and retained user/realm state, or an owner-approved reconstruction procedure. |
| Inherited ebill-service | The source sets `data_dir = "./"` and mounts its config only. Establish the actual file-state location and its SurrealDB coverage, or an owner-approved reconstruction procedure. |

Include additional live mounts and the proxy/certificate configuration if found;
absence of a child Compose mount does not prove absence of inherited state.
For each store, record the actual host path or volume, covered databases/files,
existing backup procedure and destination reference, latest usable recovery
point in UTC, retention and consistency method, and a linked restore receipt.
Keep backup contents and credential values in their existing protected stores.
A GCP backup script does not establish coverage of these self-hosted targets.

Use the same #246 record for the owners' recovery and compatibility decisions:
the intended baseline SHA/digests, database/schema versions, which old application
can read the restored/current data, explicit conditions that block rollback,
and the separately approved operator and window. Record acceptable data loss
and downtime as owner decisions, not defaults. A restore receipt must identify
the source recovery point and isolated destination, prove the required stores
were restored, and link readiness plus the agreed functional checks. An archive
listing or healthy container alone is insufficient. This inventory runs no
backup, restore, service restart or rehearsal and does not enable the schedule.

## Prepare and execute

Start from `master` with an explicit dry run:

```sh
gh workflow run clowder-dev-nightly.yml -R BitcreditProtocol/.github \
  --ref master -f dry_run=true
```

Inspect the five full member SHAs, the captured frontend and wallet SHAs, and the
previous accepted run. Missing image receipts are reported as requiring a build;
a dry run does not dispatch builds, deployments or functional tests.

After the prerequisites are verified, dispatch a new manual run with
`dry_run=false`. A first manual baseline may have no earlier accepted candidate;
that absence is recorded explicitly. It is not an automatic promotion of a
historical deployment. Scheduling requires an accepted baseline.

All four producers use saved `master` commits. A dispatch that resolves a different
SHA stops before publishing. A complete existing build can be reused. Each of the
12 images retains its actual registry digest, producer run and image attempt;
`nightly` remains an alias, not the deployment reference.

The recipient deploys all five `clowder-dev-*` targets with every deletion flag
false. It uses native Compose
[`config --lock-image-digests`](https://docs.docker.com/reference/cli/docker/compose/config/)
and saves only image assignments, never expanded configuration containing secrets.
The complete image lock is uploaded before services are stopped. Readiness and
the resulting manifest must agree with every locked active service.

The recipient then runs the 13 dev Playwright groups, mint0/mint1, and the wallet
Intermint test. The original candidate ID and exact deployment run/attempt pass
through the whole chain. Queued or delayed children check that their exact parent
is still active before using clowder-dev. The sender checks again before creating
a wallet handoff. Repeated recipient attempts cannot accept an earlier attempt's
test results.

## Environment protection and failures

The entire recipient workflow holds the `operation-clowder-dev` concurrency group,
including tests. Individual target jobs use separate locks. Ordinary manual
clowder-dev deployment and rollback use the same parent group and do not cancel
the current operation. GitHub keeps its standard one-pending-run limit.

Before any new clowder-dev operation, admission checks the candidate frontend,
mint, notifier and wallet workflows. Active runs or unreadable evidence block
environment mutation. Thus a parent timeout does not authorize deployment over
tests that are still running. Parent checks also prevent a delayed handoff from
starting work after its parent has ended or advanced to another attempt.

An operation is accepted only after all five target manifests and matching
functional results succeed. A failed API read, partial matrix, missing manifest,
unknown digest or failed test cannot establish acceptance. Inspect the failed
run before choosing recovery; there is no automatic data restore or rollback.

## Recover the saved candidate

Use **Re-run failed jobs** or rerun the relevant individual job in the original
coordinator. Keep its saved plan, image and dispatch-intent artifacts. Do not use
**Re-run all jobs**: the native full rerun removes previous artifacts, even within
their retention period. Missing, expired, corrupt or conflicting saved evidence
stops recovery; it does not capture new `master` heads or re-upload a local copy.

If a producer failed, inspect and rerun its failed jobs. Its successful image
receipts may span native attempts; the collector retains the latest valid receipt
for each image within that same saved source/run. A newer invalid receipt blocks
fallback to an older receipt.

The coordinator inspects all producer state before its four named dispatch
steps. Native step history distinguishes an untouched producer from a submission
that may already have started. An unresolved earlier submission is never repeated
just because its run is not visible yet. Final image collection cannot dispatch
builds. If the aggregate artifact is missing after its save step may have run,
recovery stops rather than collecting replacement image references.

If the recipient failed, wait for its child tests to become terminal and resolve
the reported cause. Rerun all recipient jobs to establish fresh readiness for all
five targets and fresh test evidence, then rerun only the coordinator's waiting
or failed execution job. The recipient's full rerun replaces that recipient's
attempt diagnostics; it does not rerun the central artifact-owning workflow.
Preserve any failed-attempt diagnostics needed for investigation first. A
45-minute observation window can end before the recipient; this does not cancel
its work or establish failure of the child. Read the actual recipient state.

Every deployment submission has an immutable dispatch intent before its POST.
An uncertain response is reconciled through native run state. If a previous
submission may have started but its intent or recipient is unavailable, stop and
inspect the recorded payload and Actions history. Do not issue a blind second
POST or create a fresh candidate as a substitute for recovery. The operator may
use the existing recipient workflow with the preserved payload after resolving
the ambiguity, then resume the original coordinator.

## Manual rollback

Use `clowder-dev-rollback.yml` with an earlier accepted central run ID. Start with
`dry_run=true`. Verify that its saved images are still available and that the old
application/configuration can use the current data. Only then confirm
`data_compatible=true` and execute a new non-dry manual rollback.

The rollback request is saved before dispatch. It restores the original full
digest locks for all five targets, preserving the old configuration SHA. It never
resolves the current `nightly` alias. Missing digest evidence or unconfirmed data
compatibility blocks the request. Pull/readiness failure blocks restoration.

A verified rollback records `restored=true`; it does not claim fresh functional
acceptance. The next candidate's previous-accepted lookup follows that restoration
back to the original accepted candidate. A late coordinator rerun is ordered by
the recipient's immutable result time, not by later bookkeeping.

## Evidence and activation

All metadata artifacts use native immutable Actions storage with 90-day retention:

| Repository | Evidence |
| --- | --- |
| `.github` | `clowder-nightly-plan`, `clowder-nightly-images`, `clowder-dispatch-intent`, attempt result |
| `.github` rollback | `clowder-rollback-request`, dispatch intent, attempt result |
| Producers | one `nightly-image-<image>-<attempt>` receipt per required image |
| Deployment | each target's image lock and manifest, frontend/functional result, `clowder-deployment-result-<attempt>` |
| Frontend / wallet | attempt-specific context, reports and wallet receipt; credentials are excluded |

Artifact retention does not protect against deletion by a full rerun and does
not extend GitHub's native rerun window. Keep the recorded
run and artifact links in the issue; do not store database backups or secrets in
these artifacts. Existing mint token transfer stays within the test handoff and
is not copied into central plans or acceptance records.

Enable `CLOWDER_NIGHTLY_ENABLED=true` only after prerequisite merges, a verified
self-hosted backup/restore, a complete manual deployment with functional tests,
and a demonstrated manual rollback. Keep the issue open while any of those
operational conditions remains unproven.
