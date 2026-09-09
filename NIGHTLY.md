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

1. Merge and verify the PostgreSQL restoration in `Wildcat-deployment#154`,
   readiness changes in `#156`, and their dependent nightly integration.
2. Merge the four producer interfaces, frontend correlation, wallet receipt,
   and central coordinator changes linked from `infrastructure#246`. Run their
   ordinary checks on the merged commits.
3. Verify the existing `wildcat-deployment-app` installation covers Wildcat,
   Clowder, Wildcat-Auxiliary, wildcat-dashboard-ui, Wildcat-deployment,
   E-Bill-frontend and wallet. Preserve other existing grants. Do not add Governance.
4. Make `WILDCAT_DEPLOYMENT_APP_CLIENT_ID` and
   `WILDCAT_DEPLOYMENT_APP_PRIVATE_KEY` available to the central repository,
   Wildcat-deployment, E-Bill-frontend and wallet. These references use the existing
   App. The child preflights request Actions read on the parent deployment only;
   the coordinator requests dispatch access only for the current stage's targets.
   Confirm token issuance and repository scope before deployment. Do not copy
   private keys into plans, issues, logs or artifacts.
5. Demonstrate backup and restore for the self-hosted clowder-dev data. A GCP
   backup or the presence of a backup script does not prove this. Record the
   restore result and recovery procedure in `infrastructure#246`.

These are prerequisites. They are not established by offline regression tests or
by a successful candidate preparation. This change does not provision credentials
or claim that backup/restore has already been demonstrated.

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

Use GitHub's rerun of the original coordinator. It restores the original plan and
image artifact. Missing, expired, corrupt or conflicting saved evidence stops
recovery; it does not capture new `master` heads.

If a producer failed, inspect and rerun that producer run. Its successful image
receipts may span native attempts; the collector retains the latest valid receipt
for each image within that same saved source/run. A newer invalid receipt blocks
fallback to an older receipt.

If the recipient failed, wait for its child tests to become terminal and resolve
the reported cause. Rerun all recipient jobs to establish fresh readiness for all
five targets and fresh test evidence, then rerun the original coordinator. A
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

Artifact retention does not extend GitHub's native rerun window. Keep the recorded
run and artifact links in the issue; do not store database backups or secrets in
these artifacts. Existing mint token transfer stays within the test handoff and
is not copied into central plans or acceptance records.

Enable `CLOWDER_NIGHTLY_ENABLED=true` only after prerequisite merges, a verified
self-hosted backup/restore, a complete manual deployment with functional tests,
and a demonstrated manual rollback. Keep the issue open while any of those
operational conditions remains unproven.
