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

`.github/scripts/README.md` covers how the workflows use them, what they refuse to do, and the GitHub App they need.

## Adding a repository

Most of it happens on its own. A new repository is picked up by the next scheduled run because both workflows list repositories from the API rather than from a file.

**Arrives by itself** — labels, organisation topics, merge settings, branch and tag rules (every ruleset targets `~ALL`), `GITHUB_TOKEN` permissions, the issue and pull request templates above, and the baseline security configuration, which is the default for new repositories.

**Shows up in the weekly report** — no description, no `LICENSE`, the wrong copyright holder, no `dependabot.yml` where a package manifest or a workflow exists, no entry in `dependabot-assignees.yml`, an empty public wiki, or a security configuration other than the baseline.

**Nobody checks** — which project board the repository belongs to, its environments, whether discussions are on, and whether its default branch is `master` or `main`. These are decisions rather than drift, so no automation touches them.

## Removing a repository

Archiving is enough for the automation: an archived repository drops out of every sweep on its own, and its entry in `dependabot-assignees.yml` starts being reported as stale.

One thing has to happen **first**. Unlink the repository from any project board before archiving it — an archived repository is read-only, so GitHub refuses to unlink it afterwards and the link stays for good. `Backend (Wallet)` is permanently linked to `wallet-ffi` and `bitcredit.wallet` for exactly that reason.
