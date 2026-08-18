# Organisation automation

Three scheduled workflows keep settings from drifting apart across the
organisation. Both discover repositories from the API and skip archived ones,
so creating or archiving a repository needs no change here.

| Workflow | Script | Writes | Reports |
| --- | --- | --- | --- |
| `sync-labels.yml` | `sync-labels.sh` | label names, colours, descriptions | labels not in `labels.yml` |
| `prune-package-versions.yml` | `prune-package-versions.sh` | nothing on a schedule | container versions nothing references |
| `audit-repo-settings.yml` | `audit-repo-settings.sh` | organisation topics, merge settings | description, LICENSE and its copyright holder, missing `dependabot.yml` where a manifest exists, Dependabot assignees, empty wikis, security configuration, open Dependabot alerts |

Both accept a `dry_run` input on manual runs, which prints the intended changes
without writing anything.

`audit-repo-settings` also mirrors its findings into a single issue titled
**Repository settings drift** in this repository. The issue is updated in place
on every run and closed automatically once nothing is left to report, so the
weekly schedule does not pile up duplicates. A summary that only exists inside a
workflow log is a report nobody reads.

## What they will not do

Neither workflow deletes a label. Deleting one strips it from every issue and
pull request that carries it, and there is no undo. Labels outside the manifest
are listed in the job summary so a human can decide.

`audit-repo-settings.sh` writes only topics and merge settings — every merge
flag it sets is enabling, so a repository can gain a merge method or branch
cleanup but never lose one. Everything else it finds is
reported, because a wiki that looks empty may have been enabled deliberately a
minute earlier, and a missing LICENSE is a legal decision rather than a setting.

The empty-wiki check runs on public repositories only. An App installation token
cannot read a wiki, so for an internal or private repository "no content" and "no
access" are indistinguishable — checking those reported the repositories that
actually use their wiki as empty.

## Setup

Both workflows need an organisation-scoped token. `GITHUB_TOKEN` cannot be used
— it is scoped to this repository alone and cannot touch the others.

Create a GitHub App owned by the organisation with these repository
permissions, install it on **all** repositories, then record its credentials:

| Permission | Level | Needed for |
| --- | --- | --- |
| Metadata | read | listing repositories |
| Issues | **write** | creating and updating labels |
| Administration | **write** | setting topics |
| Contents | read | detecting `LICENSE` and `dependabot.yml` |
| Dependabot alerts | read | the open-alert summary |

- organisation **variable** `AUTOMATION_APP_ID` — the App's numeric ID
- organisation **secret** `AUTOMATION_APP_PRIVATE_KEY` — the App's private key

The existing `private-repo-access-for-ci` App is not a substitute: it holds only
`contents:read`, `dependabot_secrets:read` and `metadata:read`, and is installed
on selected repositories rather than all of them.

Prefer this App over the long-lived `FE_REPO_ACCESS_PAT` organisation secret —
App tokens expire after an hour and are scoped per run.

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
package.

It never deletes a *package*, only versions. A package whose versions carry no
real tag at all is **skipped and reported** rather than emptied, because deleting
every version removes the package itself.

If a tag cannot be resolved against the registry the whole package is skipped, not
partly processed. An unresolved tag means unknown children, and a partly resolved
keep-set is how a live image loses an architecture.

### Setup

Needs the automation App to hold the organisation **Packages** permission —
`read` for the report, `write` to delete. It currently holds `administration`,
`contents`, `issues`, `metadata` and `vulnerability_alerts` only, so the first run
will fail on the package listing until that is added.

The same token is reused as a registry credential, base64-encoded, as a
`Bearer` against `ghcr.io/v2/<owner>/<package>/manifests/<tag>`.
