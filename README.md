# .github

Organisation-level defaults and automation for **BitcreditProtocol**. Nothing here is built or deployed; every file either shows up somewhere else by itself, or keeps the other repositories from drifting apart.

## What every repository inherits from here

GitHub falls back to this repository when another one has no file of its own. Nothing is copied — a repository that adds its own version simply wins.

| File | Where it appears |
| --- | --- |
| `.github/ISSUE_TEMPLATE/` | the template chooser on **New issue** |
| `.github/PULL_REQUEST_TEMPLATE.md` | the body of every new pull request |
| `CONTRIBUTING.md` | the **Contributing** link on issues and pull requests |
| `CODE_OF_CONDUCT.md`, `SECURITY.md` | the repository's community profile |
| `profile/README.md` | the [organisation profile page](https://github.com/BitcreditProtocol) |

`LICENSE` is **not** inheritable. Each repository needs its own file; the audit reports the ones that have none, and the ones whose copyright holder disagrees with `license.yml`.

## The manifests

Three files at the root state what should be true everywhere. Each is read by a scheduled workflow that reports — and in two narrow cases corrects — anything that disagrees.

| File | States | On a mismatch |
| --- | --- | --- |
| `labels.yml` | the label set: names, colours, descriptions | corrected, never deleted |
| `license.yml` | the expected copyright holder, and the repositories that legitimately carry someone else's | reported |
| `dependabot-assignees.yml` | who is assigned to Dependabot pull requests, per repository | reported |

They record the *expected value* rather than a list of repositories to skip, so an exception that stops being true is noticed instead of staying silent forever.

`.github/scripts/README.md` covers how the workflows use them, what they refuse to do, and the GitHub App they need. [RELEASING.md](RELEASING.md) covers product release preparation, publication and recovery.

## Shared master-to-dev sync

Repositories with `master` and `dev` can opt into the reusable
[`sync-master-to-dev.yml`](.github/workflows/sync-master-to-dev.yml) workflow.
Each repository adds a small manual caller pinned to a reviewed commit here.
The shared code creates a temporary branch and sync PR in the calling repository;
a maintainer reviews and merges it. Setup, merge policy, and testing are in
[SYNCING.md](SYNCING.md). This workflow is not inherited automatically.

## Adding a repository

Most of it happens on its own. A new repository is picked up by the next scheduled run because the scheduled workflows list repositories from the API rather than from a file.

**Arrives by itself** — labels, organisation topics, merge settings, branch and tag rules within their configured repository scope, `GITHUB_TOKEN` permissions, the issue and pull request templates above, and the baseline security configuration, which is the default for new repositories.

**Shows up in the weekly report** — no description, no `LICENSE`, the wrong copyright holder, no `dependabot.yml` for detected package ecosystems or external GitHub Actions references, no entry in `dependabot-assignees.yml`, an empty public wiki, or a security configuration other than the baseline.

**Operator decisions** — project boards, environment purpose, discussions and the choice of default branch. The audit reports environment protection and reviewer findings; it does not decide which environments should exist or change their settings.

**Crowdin translation PRs** — set `pull_request_assignees: [JulianVIE]` in the Crowdin VCS configuration on the localized source branch (`dev` for `E-Bill-frontend` and `wildcat-dashboard-ui`). This uses Crowdin's native assignment; no additional GitHub workflow is needed.

## Removing a repository

Archiving is enough for the automation: an archived repository drops out of every sweep on its own, and its entry in `dependabot-assignees.yml` starts being reported as stale.

One thing has to happen **first**. Unlink the repository from any project board before archiving it — an archived repository is read-only, so GitHub refuses to unlink it afterwards and the link stays for good. `Backend (Wallet)` is permanently linked to `wallet-ffi` and `bitcredit.wallet` for exactly that reason.
