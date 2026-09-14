# Sync master into dev

Repositories with `master` and `dev` can call the reusable workflow in
`.github/workflows/sync-master-to-dev.yml`. It creates a reviewed sync PR in the
calling repository. It does not merge the PR or delete branches.

## Adopt the workflow

Add this caller at `.github/workflows/sync-master-to-dev.yml` in each repository.
Replace `FULL_COMMIT_SHA` with the full SHA of a reviewed commit in this repository
that contains the reusable workflow before committing the caller:

```yaml
name: Sync master into dev

on:
  workflow_dispatch:

permissions: {}

jobs:
  sync:
    uses: BitcreditProtocol/.github/.github/workflows/sync-master-to-dev.yml@FULL_COMMIT_SHA
    secrets:
      AUTOMATION_APP_CLIENT_ID: ${{ vars.AUTOMATION_APP_CLIENT_ID }}
      AUTOMATION_APP_PRIVATE_KEY: ${{ secrets.AUTOMATION_APP_PRIVATE_KEY }}
```

The reusable workflow uses `workflow_call`; it is called as a whole job rather
than as a step. It owns checkout, the runner, timeout, concurrency, ancestry
checks, branch creation, and PR creation. The `github` context and checkout refer
to the **calling repository**, so a call from `E-Bill-frontend` creates its branch
and PR there. The caller passes the organization variable `AUTOMATION_APP_CLIENT_ID`
and secret `AUTOMATION_APP_PRIVATE_KEY` through the two declared workflow secrets.
The Client ID remains a variable in the caller; it is passed through the reusable
workflow's secret interface. Use this explicit mapping instead of `secrets: inherit`.

The shared workflow mints a `bitcredit-automation` installation token scoped to
the calling repository with `contents: write` and `pull_requests: write`. Checkout,
Git pushes, and GitHub CLI requests all use that token. The caller's `GITHUB_TOKEN`
needs no permissions. The token action revokes the App token when the job finishes.

Keeping this file in the organization's `.github` repository does not
automatically install it elsewhere. Each repository needs the small caller.
Pinning a full commit SHA keeps updates reviewable: change a caller's pin to
adopt a newer version. See [GitHub's reusable workflow documentation](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

When upgrading a caller that passes `AUTOMATION_APP_ID`, change both the shared
workflow SHA and the secret mapping to `AUTOMATION_APP_CLIENT_ID` in the same
commit. Set the organization variable to the App's Client ID from its settings.
The existing `AUTOMATION_APP_PRIVATE_KEY` continues to work. Keep the older
`AUTOMATION_APP_ID` variable while other workflows still use it.

## Setup and first run

- Both `master` and `dev` must exist in the calling repository.
- Allow merge commits, and ensure `dev` does not require linear history.
- Allow the caller to use this public reusable workflow in its Actions settings.
- The `bitcredit-automation` App installation must have access to the calling
  repository and grant `contents: write` and `pull_requests: write`. See the
  [automation App setup](.github/scripts/README.md#setup).
- Ensure the calling repository has access to the organization variable
  `AUTOMATION_APP_CLIENT_ID` and secret `AUTOMATION_APP_PRIVATE_KEY`. Ask an
  organization owner to grant access if needed.
  Passing secrets to a reusable workflow does not grant access to credentials
  stored in the shared workflow's repository.
- The organization disables **Allow GitHub Actions to create and approve pull
  requests** for `GITHUB_TOKEN`; a repository cannot override that policy.
  This workflow uses the App token and needs no change to that setting.
- The caller's `workflow_dispatch` file must reach the caller's default branch
  before its manual dispatch is available. The shared workflow only needs to
  exist at the referenced commit. Publishing it here alone does not register
  manual dispatch in a consuming repository.
- Open **Actions → Sync master into dev → Run workflow** in the calling
  repository. Selecting a feature branch chooses that caller's workflow version
  and shared-workflow pin; the sync still targets that repository's real
  `master` and `dev`.

See [manual dispatch prerequisites](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
and [reusable workflow access and permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).

## Behavior and merge policy

1. Fetch the latest `master` and `dev` with full history. Exit successfully if
   `master` is already an ancestor of `dev`.
2. Search every page of open PRs into `dev`. If a same-repository PR from
   `master` or `sync/master-to-dev-*` is open, exit with its link.
3. Create `sync/master-to-dev-<run-id>` at the fetched `master` commit and open
   **`chore: sync master into dev`** with base **`dev`**.
4. Review the changes and complete CI. **Update branch** brings `dev` into the
   temporary branch; resolve conflicts there. It does not modify `master`.
5. Merge using **Create a merge commit**.

| Direction | Merge method |
| --- | --- |
| `dev` → `master` for releases | Merge commit |
| Temporary branch from `master` → `dev` | Merge commit |

Merge commits preserve ancestry. Squashing long-lived branches can make
previously merged commits appear again in later PRs. The generated PR describes
this policy; it does not enforce the merge-button choice. See
[GitHub's merge documentation](https://docs.github.com/en/pull-requests/reference/pull-request-merges).

The temporary branch is a snapshot at fetch time. Later commits on `master`
require a new dispatch after the open PR is merged. “Aligned” means `dev`
contains all changes from `master`; the tips and files can still differ because
`dev` contains unreleased work.

Dispatches are serialized within each calling repository, across its refs. Each
repository runs independently. If PR creation fails, the branch remains. A
rerun reuses it only if it still matches the fetched `master`; otherwise start a
new dispatch. Existing branches are never force-pushed.

PRs opened with the App token trigger eligible CI workflows automatically,
without the `GITHUB_TOKEN` **Approve workflows to run** step. Workflow event,
branch, and path filters still apply. See
[triggering workflows with an App token](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
and [App token scoping and revocation](https://github.com/actions/create-github-app-token/tree/bcd2ba49218906704ab6c1aa796996da409d3eb1).

## Test the shared implementation

Run `python3 .github/scripts/test_sync_master_to_dev.py` in this repository.
The tests require Python 3, Git, Bash, and jq, with no Python packages. They use
disposable local repositories and a mocked GitHub CLI; they do not write to
GitHub. A separate test workflow runs them on relevant PRs and master pushes.

For an end-to-end Actions test, create a separate test repository with only a
manual caller, `master`, and `dev`. Grant the test repository access to the App
and the caller credentials described above. Pin the caller to the published shared
commit under test. Exercise an aligned history, a divergent history, an existing
open sync PR, and a merge followed by another dispatch. Do not point this test
at a production repository unless its real branch and PR writes are intended.
