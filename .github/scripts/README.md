# Organisation automation

Scheduled workflows maintain organisation settings and watch cross-repository dependencies. They discover repositories from the API, so creating or archiving a repository needs no configuration edit.

| Workflow | Script | Writes | Reports |
| --- | --- | --- | --- |
| `watch-dependency-graph.yml` | `watch-dependency-graph.py` | dependency issues, after explicit activation | exact pins, ranges, revisions, overrides and incomplete reads |
| `sync-labels.yml` | `sync-labels.sh` | label names, colours, descriptions | labels not in `labels.yml` |
| `audit-repo-settings.yml` | `audit-repo-settings.sh` | organisation topics, merge settings | configuration findings, coverage metrics and a list of what it could not read — see below |

The workflows accept a `dry_run` input on manual runs, which prints the intended
changes without writing anything. On `audit-repo-settings` a dry run stops after
the job summary: it does **not** touch the drift issue, so a dry run is not a way
to refresh that issue.

`audit-repo-settings` mirrors its findings into a single issue titled
**Repository settings drift** in this repository. The issue is updated in place
on writing runs and closed only after a complete audit finds nothing left to
report, so the weekly schedule does not pile up duplicates. A summary that only exists inside a
workflow log is a report nobody reads.

## What the audit reports

Findings are grouped by what they are about. Every one names the
repository and is a single line, so the issue stays readable when several fire
at once.

**The repository itself** — no description · no `stack` custom property · no
`LICENSE` · a `LICENSE` with no copyright line · a `LICENSE` naming a holder
other than the one `license.yml` expects · a wiki enabled but empty · a security
configuration other than the enforced baseline.

**Its Dependabot configuration** — a detected manifest with no `dependabot.yml`
· an ecosystem the configuration does not cover · an ecosystem whose group count
is not one · a label the configuration asks for that the repository does not
have · an assignee that disagrees with `dependabot-assignees.yml` on any update
block · a `dependabot.yml` that does not parse · a configuration with no entry in
`dependabot-assignees.yml`, and an entry pointing at a repository that is no
longer active · an `upstream_config` exemption whose repository is not a fork any
more, so the exemption has lost its premise · a lock file nested inside a cargo
workspace member, which cargo ignores and which generates alerts against versions
nothing builds.

**Its workflows** — a job granting `attestations: write` with no attest step · a
`pull_request_target` trigger · a `uses:` reference not pinned to a commit · a
job granting `id-token: write` with nothing that mints a token · a public
repository whose `pull_request` workflow declares no `permissions` block.

**Its credentials** — a repository secret whose name also exists at organisation
level, where the repository copy silently wins · a workflow referencing an
organisation secret the repository was not granted · and a secret or variable
that no workflow on the default branch reads any more, reported with one of five
verdicts: safe to delete, safe because only branches dead for 90 days hold it,
not safe because a live branch still needs it, not safe because something outside
`.github/workflows` reads it, or **unknown** because a read failed. The last one
matters most: a failed read must never render as *safe to delete*.

**Its environments** — one holding secrets with no protection rule · one listing
a reviewer who is not an organisation member.

**Its community files** — a file byte-identical to the organisation version, or a repository issue-template directory missing its own `config.yml`.

**Its releases and sites** — the highest-versioned tag without a release in a repository that has released before; incomplete dated trains; disagreement about train membership; public Pages sites with the source repository visibility; credentials still held by archived repositories.

### Coverage metrics

Each of these is real and none is a finding, because a check that prints fifty
lines on its first run trains its audience to skip the whole report.

- **jobs with no `timeout-minutes`**, caller jobs excluded because they cannot
  take the key at all. The number falls as pull requests merge; what remains is
  deferred for stated reasons — a dormant repository, dispatch-only workflows,
  one job behind `if: false`.
- **open pull requests behind their base**, split into Dependabot's, which
  self-heal when the branch is recreated from a pinned default, and the live
  human remainder. The remedy is `update-branch` on somebody else's branch, and
  tip-commit authorship is not ownership. `bit.cr#1` and `bitcr.org#1` are
  excluded by the owner's decision of 2026-08-25.
- **agent-instruction files outside the enterprise ruleset's globs.** The ruleset
  restricts `.github/agents/*.md` and `agents/*.md`, and neither directory exists
  anywhere in the organisation. Owner decision 2026-08-18: record, do not widen.

- **repositories with a wiki enabled**, across all visibilities. This counts the flag; it does not claim to read private wiki content.

### What it could not read

Every check that depends on a read the token cannot make is **skipped and
named**, in a *Not measured on this run* section, with the permission it needs.
Failed reads never become zero. A failed tag-list request skips train completeness. An incomplete workflow corpus suppresses dependent credential verdicts. An incomplete audit does not close the drift issue.

Run the committed fault checks with `python3 .github/scripts/test_audit.py`. Pull requests run these checks without an App token; only scheduled and manual runs can execute the audit. Its timeout is 30 minutes, based on the measured 19m17s full run on 2026-09-02.

That distinction is the reason the section exists. An unreadable answer and an
empty one are indistinguishable in the response, and reporting the second when it
was the first is a false all-clear — an audit that says *no unprotected
environments* because it could not list environments is worse than one that says
nothing. The same rule governs the credential verdict above.

The gaps identify unavailable measurements. Check the recorded API failure
before changing a permission: access errors, rate limits, server failures and
invalid responses need different remedies.

## What they will not do

No workflow here deletes a label. Deleting one strips it from every issue and
pull request that carries it, and there is no undo. Labels outside the manifest
are listed in the job summary so a human can decide.

`audit-repo-settings.sh` writes only topics and merge settings — every merge flag
it sets is enabling, so a repository can gain a merge method or branch cleanup but
never lose one. Everything else it finds is reported, because a wiki that looks
empty may have been enabled deliberately a minute earlier, and a missing LICENSE
is a legal decision rather than a setting.

The empty-wiki check runs on public repositories only. An App installation token
cannot read a wiki, so for an internal or private repository "no content" and "no
access" are indistinguishable — checking those reported the repositories that
actually use their wiki as empty.

## Dependency notifications

The watcher reads nested Cargo, npm and pubspec manifests from immutable snapshots of each default branch and dev, where present. It skips archived repositories, forks and the same generated/vendor paths as the audit. Package declarations identify producers; local workspace dependencies within one repository do not create cross-repository notifications. Cargo uses Python 3.11+ tomllib, JSON uses the standard library, and YAML uses the yq tool already used by the audit.

Only exact SemVer pins older than the producer's latest published full release trigger an issue. Prerelease and build-metadata precedence follow SemVer; an ahead pin is not behind. All affected branches and manifests share one open issue for each consumer/dependency. Identical reports do not refresh the issue.

Explicit Git sources identify their own producer; an external source never falls
back to an organisation package with the same name. Registry package lookup is
limited to the matching ecosystem and default registry. Cargo patches retain both
the source and package name, including aliases. A crates.io patch does not affect
a Git dependency. A matching or ambiguous patch still needs Cargo resolution to
prove whether it applies, so that dependency is reported as not measured.

Dart `dependency_overrides` and tracked sibling `pubspec_overrides.yaml` files are
read from the same commit as the manifest. A dependency with an override or an
unmeasured source cannot open, update or automatically close an issue. Other
dependencies continue to be checked. Missing or unreadable evidence never proves
that an existing issue is resolved.

A manual closure suppresses that target version only. A later release can notify again. The watcher closes its issue automatically after all mapped exact pins catch up or remaining declarations cease requiring an exact version; that automatically resolved issue can reopen after a regression. A dependency that disappears from the mapped graph remains open for manual review, since missing ownership evidence does not prove its removal. Failed consumer or recorded-producer reads cannot prove resolution. The issue body retains machine-readable target and closure state; native last-closure metadata takes precedence if a person later reopens and closes it.

Only marked issues authored by the current automation App are managed; a marker in somebody else's issue does not grant ownership. All issue pages are read before any issue mutation. API failures and malformed/truncated inputs appear in the run summary; they do not mean every exact pin is current. Unknown write outcomes are read back before a subsequent run can create anything again.

Scheduled runs stay read-only until the repository variable DEPENDENCY_WATCH_ENABLED is exactly true. Dispatch watch-dependency-graph.yml with dry_run=true and inspect its native summary before separately enabling notifications. Manual runs also default to dry_run=true.

The App token requests only Contents/Metadata read and Issues read for a dry run, or Issues write for an enabled run. PRs execute python3 .github/scripts/test_dependency_watch.py without App credentials; the operational job cannot run on pull_request.

## Setup

Cross-repository operations require an organisation-scoped App token.
`GITHUB_TOKEN` remains scoped to this repository and is used for its own Actions
artifacts, including saved release-train candidates.

- organisation **variable** `AUTOMATION_APP_ID` — the App's numeric ID
- organisation **secret** `AUTOMATION_APP_PRIVATE_KEY` — the App's private key

The App is `bitcredit-automation`, installed on **all** repositories. Adding a
permission to an App only changes what it *requests*: an owner has to accept the
request on the installation before anything changes, so check the installation
rather than the App when confirming a grant.

### What it holds

| Permission | Level | Needed for |
| --- | --- | --- |
| Metadata | read | listing repositories |
| Issues | **write** | labels, drift reports, dependency notifications and release diagnostics |
| Administration | **write** | topics and merge settings |
| Contents | **write** | source reads, annotated train tags and releases |
| Checks | read | CI results for the saved release-train commits |
| Actions | read | release-train image-build runs |
| Dependabot alerts | read | the open-alert summary |
| Pages | read | public Pages sites |
| Members | read, organisation | environment reviewers |
| Secrets | read, organisation and repository | credential checks and secret counts |
| Custom properties | read, organisation | stack classification |
| Variables | read, repository | orphaned variables |
| Environments | read | protection rules and environment secrets |

The installation also holds `pull_requests: write`, repository `packages: write`
and organisation Projects read for other work. These workflows do not create
pull requests or prune package versions. The release-train and watcher jobs
request narrower tokens for their specific read or write operations.

### What it was missing, and what happened to the list

Eight of the nine permissions this file used to list were granted on 2026-09-02,
and Actions read was added for the release train. Read the installation rather
than this paragraph — `orgs/{org}/installations` is the source of truth, and
`Variables` appears there under its API name `actions_variables`.

**The ninth does not exist, and asking for it was an error.** This file claimed
`GET /orgs/{org}/packages` needed an *organisation* Packages permission the App
lacked. There is no such App permission: that endpoint, and the version-list and
version-delete endpoints beside it, accept **only OAuth tokens and classic
personal access tokens** with `read:packages`. A GitHub App installation token is
not supported for any of them, at any grant.

The prune could therefore never have worked, and it has been removed rather than
left looking like package hygiene that happens. The claim above survived because
the evidence for it was the prune's own error message — which this repository
wrote. A message you authored is not a measurement.

The installation is `repository_selection: all`, so every permission added lands
on every repository in the organisation. That is worth weighing per permission
rather than granting a list in one go.

The existing `private-repo-access-for-ci` App is not a substitute: it holds only
`contents:read`, `dependabot_secrets:read` and `metadata:read`, and is installed
on selected repositories rather than all of them.

## Editing the licence expectations

`license.yml` at the repository root names the copyright holder every LICENSE
should carry, and records the repositories that legitimately carry someone
else's because the code is derived from their project.

Those exceptions record the *expected* third-party holder rather than merely
skipping the repository, so a change to their notice is still noticed. Nothing
is ever written to a LICENSE from here — a licence is a legal statement, so a
mismatch is reported and a human decides.

## Editing the Dependabot assignees

`dependabot-assignees.yml` at the repository root maps each repository to the
person who gets its Dependabot pull requests.

The setting itself lives in each repository's own `.github/dependabot.yml`, and
Dependabot assigns nobody by default — so an unassigned configuration is
invisible unless something compares it against a list. It stayed invisible for a
while: `assignees` had only ever been set on the `github-actions` block, so every
cargo, npm and pub pull request in the organisation opened with no assignee at
all, thirty-nine of them at once.

The audit reads **every update block** rather than the first. A repository with
one assigned block and one unassigned block looks fine to any check that stops at
the first, and that was the state nearly everywhere. It reports in both
directions: a repository with a configuration and no entry here, and an entry
pointing at a repository that is no longer active.

`upstream_config` lists repositories whose `dependabot.yml` belongs to an
upstream project — a fork we do not want to diverge from. The assignee and group
checks skip those, and the file records *which upstream*, so if the repository
stops being a fork the audit says the exemption has lost its premise instead of
staying silent.

Nothing is written to a `dependabot.yml` from here. Adding a repository means
adding the line here *and* setting `assignees` on every update block in that
repository — the audit says so if only one of the two is done.

## Editing the label set

`labels.yml` at the repository root holds three sections:

- `renames` — old name to new name, applied only when the old label exists and
  the new one does not, so existing assignments are preserved
- `required` — created everywhere and corrected when it drifts
- `managed` — corrected where present, never created

Omit `description` on an entry to pin only its colour and leave whatever
description each repository already has.

Every label a `dependabot.yml` can ask for is in `required` rather than
`managed`, deliberately. Dependabot applies only labels that already exist and
**fails the update** when one is missing, so a label that exists everywhere costs
nothing next to an update that does not run.

## Release trains

| Workflow | Script | Writes | Reports |
| --- | --- | --- | --- |
| `release-train.yml` | `release-train.py` | annotated tags and releases in the five members; dashboard snapshot issue | exact candidate, master checks, shared-crate revisions, migrations and image-build starts |

See [the release contract](https://github.com/BitcreditProtocol/.github/blob/master/RELEASING.md). Prepare each candidate with a native dry-run dispatch from `master` and inspect its saved composition before cutting a real train.

A new dispatch takes `product`; recovery takes the original `resume_run_id` and leaves `product` blank. `dry_run` defaults to true. Preparation records all five full SHAs and the UTC tag in the immutable `release-train-plan` Actions artifact before any tags are written. Its retention is 90 days; an expired or missing plan stops recovery instead of recapturing current heads. Rerunning an attempt restores that run's original candidate. A new dispatch can resume the original dry-run to cut exactly what was inspected.

The read token is scoped to the five members plus `bcr-common`, with Contents, Checks, Actions, Issues and Metadata read access. The write token is minted only for a real cut and grants Contents/Issues write and Metadata read in the five members. Artifact reads use the current repository's `GITHUB_TOKEN`. The App installation must grant Actions read; the application request alone is insufficient.

Pull requests execute only `python3 .github/scripts/test_release_train.py`, without App credentials. The operational job is dispatch-only and writes only from `master`. Matching tag/release readback makes retry safe after a lost response. A started image build is not proof of a successful build or deployment; wait for the four successful builds before the separate deployment step.
