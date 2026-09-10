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

permissions:
  contents: write
  pull-requests: write

jobs:
  sync:
    uses: BitcreditProtocol/.github/.github/workflows/sync-master-to-dev.yml@FULL_COMMIT_SHA
```

The reusable workflow uses `workflow_call`; it is called as a whole job rather
than as a step. It owns checkout, the runner, timeout, concurrency, ancestry
checks, branch creation, and PR creation. The `github` context and checkout refer
to the **calling repository**, so a call from `E-Bill-frontend` creates its branch
and PR there. No repository name input or organization-wide token is needed.
`github.token` is available automatically; do not add `secrets: inherit`.

Keeping this file in the organization's `.github` repository does not
automatically install it elsewhere. Each repository needs the small caller.
Pinning a full commit SHA keeps updates reviewable: change a caller's pin to
adopt a newer version. See [GitHub's reusable workflow documentation](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

## Setup and first run

- Both `master` and `dev` must exist in the calling repository.
- Allow merge commits, and ensure `dev` does not require linear history.
- Allow the caller to use this public reusable workflow in its Actions settings.
- The caller grants `contents: write` and `pull-requests: write`. Enable
  **Settings → Actions → General → Workflow permissions → Allow GitHub Actions
  to create and approve pull requests**, subject to organization/enterprise
  policy. The shared workflow cannot elevate the caller's token permissions.
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
5. Merge using **Create a merge commit**, then delete the temporary branch.

| Direction | Merge method |
| --- | --- |
| Feature branch → `dev` | Squash |
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

The workflow uses the caller's `GITHUB_TOKEN`. GitHub documents that PR workflows
it triggers require a user with write access to select **Approve workflows to
run** on the PR. Automatic CI using a custom GitHub App token would require
extending the reusable workflow's authentication; this version does not accept
custom tokens. See [GitHub token behavior](https://docs.github.com/en/actions/concepts/security/github_token).

## Test the shared implementation

Run `python3 .github/scripts/test_sync_master_to_dev.py` in this repository.
The tests require Python 3, Git, Bash, and jq, with no Python packages. They use
disposable local repositories and a mocked GitHub CLI; they do not write to
GitHub. A separate test workflow runs them on relevant PRs and master pushes.

For an end-to-end Actions test, create a separate test repository with only a
manual caller, `master`, and `dev`. Pin the caller to the published shared
commit under test. Exercise an aligned history, a divergent history, an existing
open sync PR, and a merge followed by another dispatch. Do not point this test
at a production repository unless its real branch and PR writes are intended.
