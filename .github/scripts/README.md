# Organisation automation

Two scheduled workflows keep settings from drifting apart across the
organisation. Both discover repositories from the API and skip archived ones,
so creating or archiving a repository needs no change here.

| Workflow | Script | Writes | Reports |
| --- | --- | --- | --- |
| `sync-labels.yml` | `sync-labels.sh` | label names, colours, descriptions | labels not in `labels.yml` |
| `audit-repo-settings.yml` | `audit-repo-settings.sh` | organisation topics | description, LICENSE, `dependabot.yml`, empty wikis, security configuration |

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

`audit-repo-settings.sh` only writes topics. Everything else it finds is
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

- organisation **variable** `AUTOMATION_APP_ID` — the App's numeric ID
- organisation **secret** `AUTOMATION_APP_PRIVATE_KEY` — the App's private key

The existing `private-repo-access-for-ci` App is not a substitute: it holds only
`contents:read`, `dependabot_secrets:read` and `metadata:read`, and is installed
on selected repositories rather than all of them.

Prefer this App over the long-lived `FE_REPO_ACCESS_PAT` organisation secret —
App tokens expire after an hour and are scoped per run.

## Editing the label set

`labels.yml` at the repository root holds three sections:

- `renames` — old name to new name, applied only when the old label exists and
  the new one does not, so existing assignments are preserved
- `required` — created everywhere and corrected when it drifts
- `managed` — corrected where present, never created

Omit `description` on an entry to pin only its colour and leave whatever
description each repository already has.
