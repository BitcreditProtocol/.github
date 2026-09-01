# Organisation automation

Three scheduled workflows keep settings from drifting apart across the
organisation. All three discover repositories from the API and skip archived
ones, so creating or archiving a repository needs no change here.

| Workflow | Script | Writes | Reports |
| --- | --- | --- | --- |
| `sync-labels.yml` | `sync-labels.sh` | label names, colours, descriptions | labels not in `labels.yml` |
| `prune-package-versions.yml` | `prune-package-versions.sh` | nothing on a schedule | container versions nothing references |
| `audit-repo-settings.yml` | `audit-repo-settings.sh` | organisation topics, merge settings | 32 findings, 3 metrics and a list of what it could not read — see below |

All three accept a `dry_run` input on manual runs, which prints the intended
changes without writing anything. On `audit-repo-settings` a dry run stops after
the job summary: it does **not** touch the drift issue, so a dry run is not a way
to refresh that issue.

`audit-repo-settings` mirrors its findings into a single issue titled
**Repository settings drift** in this repository. The issue is updated in place
on every run and closed automatically once nothing is left to report, so the
weekly schedule does not pile up duplicates. A summary that only exists inside a
workflow log is a report nobody reads.

## What the audit reports

Thirty-two findings, grouped by what they are about. Every one names the
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

**Its community files** — a file byte-identical to the organisation version in
this repository, which could simply be inherited.

### Three things are counted rather than reported

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

### What it could not read

Every check that depends on a read the token cannot make is **skipped and
named**, in a *Not measured on this run* section, with the permission it needs.
None of them reports zero.

That distinction is the reason the section exists. An unreadable answer and an
empty one are indistinguishable in the response, and reporting the second when it
was the first is a false all-clear — an audit that says *no unprotected
environments* because it could not list environments is worse than one that says
nothing. The same rule governs the credential verdict above.

It also means the report is the App's own permission probe: whatever it lists is
exactly what the App is missing.

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

## Setup

All three need an organisation-scoped token. `GITHUB_TOKEN` cannot be used
— it is scoped to this repository alone and cannot touch the others.

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
| Issues | **write** | creating and updating labels, and the drift issue |
| Administration | **write** | setting topics |
| Contents | read | `LICENSE`, `dependabot.yml`, workflow bodies, branch trees |
| Dependabot alerts | read | the open-alert summary |

It also holds `contents: write`, `pull_requests: write` and repository
`packages: write`, granted for work outside these three scripts. None of the
three writes a file or opens a pull request.

### What it is missing

Seven permissions, each one named by a run rather than guessed at. Six are the
audit's, and the seventh stops the prune before it starts.

| Permission | Level | Unlocks |
| --- | --- | --- |
| Custom properties: read | organisation | the `stack` check |
| Members: read | organisation | the non-member-reviewer check |
| Secrets: read | organisation | the shadowing and not-granted checks |
| Secrets: read | repository | the shadowing check, the orphaned-credential check, the secret-surface figures |
| Variables: read | repository | variables in the orphaned-credential check |
| Environments: read | repository | the unprotected-environment check, the non-member-reviewer check, the environment half of the secret surface |
| Packages: read | **organisation** | listing container packages at all — `prune-package-versions.sh` stops here |

**The Packages one is the trap.** The App holds repository `packages: write`, and
that is a different permission: `GET /orgs/{org}/packages` needs the
**organisation** Packages permission, which the App does not have. The prune's
first run stopped at the package listing and said so, which is how the difference
was established rather than assumed.

The installation is `repository_selection: all`, so every permission added lands
on every repository in the organisation. That is worth weighing per permission
rather than granting the list in one go.

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

## Pruning package versions

`prune-package-versions.sh` reports container package versions that nothing
references. The organisation reached **2,974 versions across 21 packages** with no
cleanup anywhere; `clowder` alone held 752.

**91% of them are untagged, and that is not the same as unused.** A version is
kept when any of these holds:

1. it carries a real tag — anything not matching `sha256-*`
2. it carries a `sha256-*` sidecar tag, which is an attestation or signature
3. its digest is the subject of one: `sha256-<hex>` names `sha256:<hex>`
4. its digest is a child of a tagged manifest index
5. it is newer than `keep_days`, default 30

Rule 4 is the one that cannot be skipped. A multi-architecture image carries its
tag on the index and every per-architecture manifest underneath is untagged, so
"delete the untagged ones" removes the platforms out of a live tag. The packages
API cannot see inside a manifest list, so the script resolves each tagged index
against `ghcr.io/v2/...` directly. Measured here before the script existed: 30 of
`bcr-wdc-dashboard-ui`'s 443 untagged versions are index children, and 149 of
`clowder`'s are attestation subjects — a naive policy is wrong by 27% on the
attested package, irreversibly.

### What it will not do

**The schedule never deletes.** Deleting a version cannot be undone, and unlike a
label it cannot be recreated by hand — so removal needs a manual run with
`dry_run` unticked, and `only_package` exists so the first one can be a single
package. Two independent guards enforce it: the workflow computes `DRY_RUN=false`
only for a `workflow_dispatch`, and the script itself refuses to delete when
`GITHUB_EVENT_NAME` is set to anything else.

It never deletes a *package*, only versions. A package whose versions carry no
real tag at all is **skipped and reported** rather than emptied, because deleting
every version removes the package itself.

If a tag cannot be resolved against the registry the whole package is skipped, not
partly processed. An unresolved tag means unknown children, and a partly resolved
keep-set is how a live image loses an architecture.

**It cannot see a reference from outside the registry.** Every keep rule is
registry-side, so a deployment manifest that pins an image by digest —
`image@sha256:…` — is invisible to all five. A version that manifest depends on
classifies as deletable the moment it is untagged, older than the cutoff and not
an index child. This is inherent to the approach rather than a gap to close: the
script has no way to know which repositories deploy what. **Before the first real
deletion, grep the deploy repositories for `@sha256:` and check the digests
against the list.** The run summary repeats this, so the warning reaches whoever
is about to press the button rather than only whoever read this file.

### Setup

Needs the automation App to hold the **organisation** Packages permission —
`read` for the report, `write` to delete. It does not have it, so the script
currently stops at the package listing and says which permission is missing,
rather than reporting an empty estate. See *What it is missing* above for why the
repository `packages: write` it does hold is not the same thing.

The same token is reused as a registry credential, base64-encoded, as a
`Bearer` against `ghcr.io/v2/<owner>/<package>/manifests/<tag>`.
