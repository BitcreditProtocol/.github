# Releasing

This contract covers the product release workflows below. Update it when their
behaviour changes. Governance retains its separate process.

## The two kinds

**A train** is one coordinated cut across the five repositories that ship together:
`Wildcat`, `Clowder`, `Wildcat-Auxiliary`, `Wildcat-deployment` and
`wildcat-dashboard-ui`.

**A package release** is a single repository publishing an artifact on its own
schedule — the WASM SDK from `Bitcredit-Core`, the component library from `ui`,
mobile builds from `wallet`, precompiled binaries from `Wallet-Core`.

## Two version numbers, and they are not the same number

| | where it lives | who owns it |
|---|---|---|
| **package version** | the manifest — `Cargo.toml`, `package.json`, `pubspec.yaml` | the repository |
| **product version** | the train tag | the train |

**They are not expected to match, and a gap between them is not a defect.**

For example, a manifest version of `0.5.0-alpha` can coexist with a product version
of `0.6.0`. Cutting a train does not require changing package versions. A package
version changes according to that package's own release contract.

## Cutting a train

**Tag name:** `v<product>-YYYY-MM-DD` — for example `v0.5.0-2026-09-08`.
Use the UTC date when the candidate is first prepared.

- **Annotated**, never lightweight, with the initiating actor and UTC timestamp.
- **The same name in all five repositories**, at the five full commit SHAs saved
  from their respective `master` heads before any tag is created.
- Message: `Release v<product>-YYYY-MM-DD`.
- **One GitHub release per tag**, with a note. See *What a release must carry*.
- **Never delete or move a tag.** A retry uses the original tag and saved SHAs.

For a separate new cut on the same UTC date, change the product suffix: for
example, product `0.5.0-rc2` produces `v0.5.0-rc2-2026-09-08`. To finish the
original cut, use `resume_run_id`; do not change its date, tag or commits.

### Why this shape

The date distinguishes a train from a package release and makes the date sortable
and unambiguous. Keep the `v` prefix: all four repositories that produce images
build them on `push: tags: ["v*.*.*"]` —
`Wildcat/build.yml`, `Clowder/build.yml`, `Wildcat-Auxiliary/build.yml`,
`wildcat-dashboard-ui/release.yml`. All four derive the image tag with
`type=semver,pattern={{version}}`, so `v0.5.0-2026-09-08` becomes image tag
`0.5.0-2026-09-08`. A `train/` prefix would not trigger these builds. The dated
tag is a SemVer prerelease and sorts below a plain release of the same version.

**The hyphens inside the date are load-bearing.** `0.5.0-2026-09-08` is a valid
prerelease because `2026-09-08` is a single alphanumeric identifier. Writing it
with dots — `0.5.0-2026.09.08` — is **not valid semver**: dots split the prerelease
into identifiers, `09` is then a numeric identifier with a leading zero, and the
whole tag fails to parse. `type=semver` would produce nothing.

### Prepare, cut and resume

**Prepare each new candidate with the native `release-train.yml` workflow from
`master` and `dry_run=true`.** Inspect its summary and saved candidate before
enabling writes. The automation GitHub App must be configured.

Preparation does not write tags, releases or issues:

```bash
gh workflow run release-train.yml --repo BitcreditProtocol/.github --ref master \
  -f product=0.5.0 -f dry_run=true
```

The run records the tag, UTC initiator timestamp, exactly five immutable commit
SHAs, previous dated train and original Actions run ID in
`release-train-plan.json`. It stores this as the immutable `release-train-plan`
Actions artifact, with overwrite disabled and 90-day retention, before any
release writes.

To cut that verified candidate, set `RESUME_RUN_ID` to the original dry-run ID:

```bash
: "${RESUME_RUN_ID:?Set the original candidate Actions run ID}"
gh workflow run release-train.yml --repo BitcreditProtocol/.github --ref master \
  -f resume_run_id="$RESUME_RUN_ID" -f dry_run=false
```

Use the same original `resume_run_id` after a partial failure. Omit `product`
when resuming. The workflow restores the original tag and SHAs, checks all five
again, accepts existing annotated tags only at their saved commits, and creates
only missing tags and releases. It never substitutes current `master` heads.
If the artifact is missing, expired or ambiguous, **stop without retagging**.
Do not recreate the candidate from current heads to complete the old train.

A fresh dispatch refuses a tag already present in any member. A conflicting or
lightweight tag stops recovery; it is not deleted or moved.

### Build, then deploy

Pushing the tags starts four image builders: `Wildcat/build.yml`,
`Clowder/build.yml`, `Wildcat-Auxiliary/build.yml` and
`wildcat-dashboard-ui/release.yml`. `Wildcat-deployment` is a train member but
does not build an image.

The release workflow checks that matching tag-push builds started. **Wait for all
four builds to finish successfully at the saved tag and corresponding SHAs before
deploying.** A started build or published GitHub release is not deployment proof.
Then dispatch `Wildcat-deployment/deploy.yml` with the image tag **without `v`**:

```bash
: "${TRAIN:?Set the tag from the saved candidate}"
: "${DEPLOY_ENVIRONMENT:?Choose the intended deployment environment}"
gh workflow run deploy.yml --repo BitcreditProtocol/Wildcat-deployment \
  --ref "$TRAIN" -f environment="$DEPLOY_ENVIRONMENT" -f image_tag="${TRAIN#v}"
```

`deploy.yml` routes the selected environment to `deploy-wildcat.yml` or
`deploy-gcp.yml`. Deployments of one target serialize across branches and tags;
a new run does not cancel the active deployment. The standard GitHub concurrency
limit keeps only one pending run per target.

Application and reverse-proxy startup use bounded Compose readiness waits and
independent state checks. Existing healthchecks must pass; a service without a
healthcheck is confirmed only as running. Success also requires a complete saved
image manifest. Inspect actual image IDs, available registry digests and capture
errors in the deployment summary/artifact. Missing digests remain unmeasured.
These checks do not establish application acceptance or rollback compatibility.

### Manual fallback

Before the first write, record the UTC tag, initiator and exactly five full
`master` SHAs, and gate all five as described below. Keep that candidate fixed
throughout the cut. For recovery of an automated cut, use its original artifact;
the fallback does not bypass missing or expired candidate storage.

For each missing tag, use a local clone whose `origin` is the corresponding
`BitcreditProtocol` repository and fetch the saved commit. Set `TRAIN`, `REPO`
and `SHA` from the verified candidate. Set `NOTES` to a file containing its release
notes, including the five SHAs and rollback limits:

```bash
set -e
: "${TRAIN:?Set the tag from the saved candidate}"
: "${REPO:?Set one of the five member repository names}"
: "${SHA:?Set the saved full commit SHA for this member}"
: "${NOTES:?Set the release notes file path}"
git -C "$REPO" tag -a "$TRAIN" -m "Release $TRAIN" "$SHA"
git -C "$REPO" push origin "refs/tags/$TRAIN"
gh release create "$TRAIN" --repo "BitcreditProtocol/$REPO" --verify-tag \
  --title "$TRAIN" --notes-file "$NOTES" --generate-notes
```

Stop on any failure. If the tag exists, first verify that it is annotated and
resolves to the saved SHA; create only a missing release with `--verify-tag`.
Never force a tag or let release creation create a missing tag. Complete all
five members, then follow *Build, then deploy*.

### Membership is five, and a miss matters

All five are full members. `Wildcat-deployment` carries the deployment
configuration, so a train without it does not describe the complete candidate.

A dated tag present in some repositories and absent from others is an
**incomplete train**, and the weekly audit reports it against every member that is
missing it.

### Tags cut before 2026-09-01 keep their names

Existing trains are **not renamed and not migrated**. Preserve their historical
names and commits. The convention above applies to new trains.

## What a release must carry

**A note and a release object for every train tag.** Include the candidate run,
all five commit SHAs, generated or curated change notes, and the rollback
comparison or its measurement gap. Shared wire-crate and OpenAPI reports are
diagnostics; commit distance and source dates do not prove compatibility.

## Cutting a release when checks are red

**Gate the exact candidate commits before release writes.** Branch rules do not
replace this check. `release-train.yml` checks all five saved SHAs during
preparation and again before reconciliation.

It considers check suites whose branch is `master` and whose head SHA is the
saved commit, then selects the latest run for each check name and app. All
non-excluded latest runs must have completed. A conclusion of `failure`,
`timed_out`, `cancelled`, `action_required` or `stale` blocks the train.
`Dependabot` is excluded by name, and every summary states that exclusion.
No remaining check runs, incomplete runs or unreadable check data also stop the
train. Tag-build and unrelated-branch suites cannot substitute for `master`
checks. Later movement of `master` does not change the saved candidate.

**For a package release, check that repository's release workflow and candidate.**
The train gate covers the five Wildcat repositories; it does not gate independent
package publication.

## Rollback: when a release goes wrong

Nothing here is a runbook for somebody else's deployment. It is what is and is not
reversible, so the decision is not being worked out while something is broken.

**A container image can be rolled back by redeploying an available older tag.**
Verify that the intended images still exist and are compatible with the current
application, schema and data before deploying them. `Wildcat-deployment`'s
`deploy.yml` takes `image_tag`, and a per-service override — `wildcat_image_tag`,
`clowder_image_tag`, `auxiliary_image_tag`, `dashboard_ui_image_tag` — so one
service can go back without the others.

**The rollback note is a SQL migration-file comparison.** The train compares
paths and blob SHAs under `migrations/` for `.sql` files at the saved previous
dated train and candidate commits. It reports additions, removals and changed
contents. No differences does **not** prove an older image is safe to redeploy;
application and data compatibility still need review. A missing baseline or
unreadable tree is reported as unmeasured, not safe.

**Fix published packages forward.** Consumers may already have resolved an npm
version; publish a corrected higher version instead of relying on removal.
For crates.io, yank a bad published version when needed and publish the fix.

**A tag cannot be deleted.** The `All tags` ruleset enforces `deletion` across every
repository, with bypass for organisation admins only. The tag of a bad release stays
where it is. Say so in the GitHub release instead: edit the body, or mark it as a
pre-release so it stops being *Latest*.

Redeploy an older image only after the compatibility and availability checks,
then fix forward. Keep the original train tags as the release record.

## Package releases

| Line | Workflow | Version/source identity | Saved output | First publication write |
|---|---|---|---|---|
| Wildcat train | `.github/release-train.yml` | Product input, UTC tag, five saved SHAs | `release-train-plan` Actions artifact, 90 days | First missing annotated tag |
| WASM | `Bitcredit-Core/wasm_release.yml` | Workspace Cargo version, matching generated npm manifest, saved SHA | `release-package`: tarball, release assets and checksums, 90 days | First missing tag |
| UI library | `ui/npm_release.yml` | SemVer tag applied to the two staged package manifests, saved SHA | `release-package`: separate npmjs/GitHub tarballs and checksums, 90 days | First missing registry version |
| Mobile candidate | `wallet/build-candidate.yml` | Tag marketing version, resolved build number and original run/SHA | Original APK/IPA Actions artifacts; `candidate-manifest.json` in GitHub release | Store candidate upload, before the later GitHub asset job |
| Precompiled binaries | `Wallet-Core/cd_precompiled.yml` | Cargokit content-derived key and the tag's original build SHA | `precompiled_<hash>` release with binary/signature pairs | Release creation or metadata update, before target reconciliation |

Workflow filenames above live under each repository's `.github/workflows/`.
For every package candidate, inspect that repository's CI at the source SHA and
its workflow-specific evidence before approving publication. The train gate does
not validate independent packages. Environment settings are the source of truth
for required reviewers; repository instructions describe their release inputs.

### Package channels and prepared bytes

WASM and UI prepare their exact publication bytes before the first write and
retain immutable artifacts with original run identity and checksums. A repeat
verifies existing remote content; matching results are preserved, missing results
are added, and conflicts or unreadable state stop the operation. Never substitute
fresh build output to complete a version that has already been published.

Stable packages use npm `latest`. WASM prereleases use `next`. UI prereleases with
first identifier `alpha`, `beta`, `rc` or `test` use that named channel; other UI
prereleases use `next`. SemVer build metadata stays in source tags and package
manifests, but does not create a distinct npm registry version identity.

WASM keeps its GitHub release draft until assets and npm integrity are confirmed.
UI's GitHub release is still created and documented separately. Package versions
use repository-owned SemVer tags without the train date suffix. Cargokit's
`precompiled_<hash>` tags are a separate existing content-derived convention.

### Recover the original operation

For a train, dispatch `release-train.yml` with the original `resume_run_id` while
its 90-day candidate artifact remains available. This is a new dispatch restoring
saved state, so it does not rely on the native rerun window.

For WASM and UI, rerun only the failed publication job on the original run to
restore saved archives. Do not use **Re-run all jobs**: a full rerun removes
previous artifacts despite their retention period. Preserve successful package
preparation. For wallet, if both mobile jobs succeeded, rerun
only the failed GitHub asset job; it reuses the original APK/IPA files and build
number. A stable candidate manifest and checksums protect those identities.
`promote-release.yml` continues to promote existing store builds without rebuilding.

GitHub's [native rerun window][reruns] is 30 days. Longer artifact retention does
not extend it. Missing, expired, ambiguous or conflicting recovery data requires
an operator recovery decision; a fresh run must not silently adopt a previous
publication. Native regression and packaging checks do not exercise production
registry authentication, store delivery or a hosted deployment.

For Wallet-Core, complete platform targets are preserved during retries;
incomplete targets are rebuilt by existing Cargokit behaviour. Verify the expected
three iOS and four Android binary/signature pairs. A retry may update release
metadata even when all target assets are complete. Consumers verify signatures;
handled HTTP failures and rejected signatures fall back to a local build, while
transport errors may stop the build. The vendored
[content-hash defect #336][crate-hash] remains a developer task; valid signatures
alone do not prove that the content key distinguishes every meaningful source change.

## `bcr-common`

The crates.io publication decision is tracked in [bcr-common#219][common-publish],
with signed wire-format regression coverage tracked in [#209][wire-tests]. These
remain developer tasks. Until publication lands, consumers pin Git revisions.
Revision differences and overrides are diagnostic information, not a new train gate.

Resolve the revision each consumer actually uses; do not infer it from the latest
tag or a manifest version. A `[patch]` or submodule can override a declared tag.

## Tag protection

The organisation ruleset **`All tags`** applies to every tag in every repository —
`deletion` and `non_fast_forward`, enforcement active, bypass for organisation
admins only. A train tag is protected the moment it is pushed; nothing needs
configuring.

---

Questions about a specific repository's build belong in its own `README.md`. How to
contribute at all is in [CONTRIBUTING.md][contributing].

[contributing]: https://github.com/BitcreditProtocol/.github/blob/master/CONTRIBUTING.md
[reruns]: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs
[crate-hash]: https://github.com/BitcreditProtocol/Wallet-Core/issues/336
[common-publish]: https://github.com/BitcreditProtocol/bcr-common/issues/219
[wire-tests]: https://github.com/BitcreditProtocol/bcr-common/issues/209
