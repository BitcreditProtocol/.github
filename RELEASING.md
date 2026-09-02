<!--
Organisation-wide. This describes how releases are actually cut here, plus the
conventions decided on 2026-09-01. Every claim below is measured; the figures come
from a full sweep of 247 releases and 302 tags. If you change a convention, change
this file with it.
-->

# Releasing

Two different things are called a release in this organisation, and they follow
different rules. Getting them confused is the reason two version numbers currently
look like they disagree.

## The two kinds

**A train** is one coordinated cut across the five repositories that ship together:
`Wildcat`, `Clowder`, `Wildcat-Auxiliary`, `Wildcat-deployment` and
`wildcat-dashboard-ui`. Nine trains have run since February 2026.

**A package release** is a single repository publishing an artifact on its own
schedule — the WASM SDK from `Bitcredit-Core`, the component library from `ui`,
mobile builds from `wallet`, precompiled binaries from `Wallet-Core`.

## Two version numbers, and they are not the same number

| | where it lives | who owns it |
|---|---|---|
| **package version** | the manifest — `Cargo.toml`, `package.json`, `pubspec.yaml` | the repository |
| **product version** | the train tag | the train |

**They are not expected to match, and a gap between them is not a defect.**

`Wildcat`'s workspace says `0.5.0-alpha` while the product has shipped `v0.6.0`.
That is the two systems doing their separate jobs. **Do not "fix" it.** The same
goes for `Wildcat-Auxiliary` and `wildcat-dashboard-ui`, whose manifests sit three
minors below the product number.

A package that nobody consumes may legitimately have no version at all — `ui` and
`E-Bill-frontend` both carry `0.0.0` across 40 and 32 releases, and that is fine.

## Cutting a train

**Tag name:** `v<product>-<YYYY-MM-DD>` — for example `v0.4.0-2026-08-25`.
The date suffix is what makes a tag a train tag; nothing else does.

- **Annotated**, never lightweight. The tagger and the date are the only record of
  who cut the train and when.
- **The same name in all five repositories**, cut from `master` in each.
- Message: `Release v<product>-<YYYY-MM-DD>`.
- **One GitHub release per tag**, with a note. See *What a release must carry*.

### Why this shape

The date suffix stops the product number colliding with each repository's own
tags: today `v0.5.0` means 2025-09-16 in `Wildcat`, 2026-08-25 in `Clowder`, and
2025-10-22 in `bcr-common` — three different things under one name. `2026-08-25`
in place of `aug25` makes the date sortable and unambiguous.

**A separate `train/` namespace was the first choice and was withdrawn before
anything merged, because it silently breaks the build.** All four repositories that
produce images build them on `push: tags: ["v*.*.*"]` —
`Wildcat/build.yml`, `Clowder/build.yml`, `Wildcat-Auxiliary/build.yml`,
`wildcat-dashboard-ui/release.yml` — and three of them derive the image tag with
`type=semver,pattern={{version}}`, which is how `v0.4.0-aug25` became the image
`0.4.0-aug25`. A ref beginning `train/` matches neither: no workflow fires, no
image is built, and `Wildcat-deployment` — whose `deploy-wildcat.yml` takes
`image_tag` as an input — has nothing to deploy. The train would tag five
repositories, publish five releases, and produce nothing runnable, **with no error
anywhere**. Keeping the `v` prefix costs one thing, deliberately accepted: a
hyphen makes `v0.5.0-2026-09-08` a semver *prerelease* of `0.5.0`, so a train tag
sorts below a plain release of the same number.

**The hyphens inside the date are load-bearing.** `0.5.0-2026-09-08` is a valid
prerelease because `2026-09-08` is a single alphanumeric identifier. Writing it
with dots — `0.5.0-2026.09.08` — is **not valid semver**: dots split the prerelease
into identifiers, `09` is then a numeric identifier with a leading zero, and the
whole tag fails to parse. `type=semver` would produce nothing.

### The sequence

Until this is automated, a train is cut by hand. All five, from `master`, in one
pass — that is what keeps a train a train:

```bash
TRAIN=v0.4.0-2026-08-25

for repo in Wildcat Clowder Wildcat-Auxiliary Wildcat-deployment wildcat-dashboard-ui; do
  git -C "$repo" fetch origin
  git -C "$repo" tag -a "$TRAIN" -m "Release $TRAIN" origin/master
  git -C "$repo" push origin "$TRAIN"
done
```

Then a release per tag, in each of the five:

```bash
gh release create "$TRAIN" --repo "BitcreditProtocol/$repo" \
  --title "$TRAIN" --generate-notes
```

Tag the commit you actually intend to ship, and make sure it is the commit that was
tested — see *Cutting a release when checks are red*.

### Membership is five, and a miss matters

All five are full members. `Wildcat-deployment` included: it carries the deployment
configuration and the self-hosted runners, so a train without it does not describe
what is actually deployed. It missed the `june17`, `july31` and `aug4` trains.

A dated tag present in some repositories and absent from others is an
**incomplete train**, and the weekly audit reports it against every member that is
missing it.

### Tags cut before 2026-09-01 keep their names

The nine existing trains are **not renamed and not migrated**. Retagging is
destructive and would rewrite a record people rely on. They are history; the
convention above applies to the next train.

## What a release must carry

**A note.** Measured across the estate, **72 of 247 releases (29%) have an empty
body** — `ui` alone accounts for 36 of its 40. A release with no note is a date and
a number, and nobody can tell later what shipped.

`--generate-notes` costs nothing and is better than blank. A curated note is better
still.

**A tag needs a release.** The `v0.4.0-aug25` train produced **five tags and two
releases** — three of the five repositories have no release object for a train that
shipped.

## Cutting a release when checks are red

This organisation has **no required status checks**, deliberately, and the
`developers` team can bypass branch rules. Nothing in GitHub will stop a release
being cut from a red commit.

The gate therefore belongs in the release process: **do not cut a release when the
head commit's checks are not green.** The intended end state is a release workflow
that refuses to proceed and says why. That is not built yet; until it is, this is a
rule people keep by hand.

## When a release goes wrong

Nothing here is a runbook for somebody else's deployment. It is what is and is not
reversible, so the decision is not being worked out while something is broken.

**A container image goes back by redeploying an older tag.** `Wildcat-deployment`'s
`deploy.yml` takes `image_tag`, and a per-service override — `wildcat_image_tag`,
`clowder_image_tag`, `auxiliary_image_tag`, `dashboard_ui_image_tag` — so one
service can go back without the others. Every image ever built is still in the
registry: the package prune keeps 30 days and has never run.

**That is only clean when no migration landed in between.** `Clowder` carries nine
migration files and `Wildcat` one, they are numbered forward, and **there are no
down-migrations anywhere**. An older image against a newer schema is untested. Each
release body now says which case it is — the train compares the migration files at
the previous train tag against this one and writes the answer in.

**A published npm version cannot be taken back.** `@bitcredit/bcr-ebill-wasm` is at
54 published versions and `@bitcredit/ui-library` at 37; npm restricts unpublishing
after 72 hours, and consumers may already have resolved it. The fix is a higher
version, not a removal. `bcr-common` is not on crates.io yet; when it is, crates.io
never unpublishes at all — a bad version is yanked, which hides it from new
resolution and leaves existing lockfiles working.

**A tag cannot be deleted.** The `All tags` ruleset enforces `deletion` across every
repository, with bypass for organisation admins only. The tag of a bad release stays
where it is. Say so in the GitHub release instead: edit the body, or mark it as a
pre-release so it stops being *Latest*.

**So the honest order is: redeploy the previous image, then fix forward.** Reversing
a train is not one action, and pretending otherwise is how a bad hour becomes a bad
day.

## Package releases

Four workflows in the whole organisation create a release or publish a package:

| repository | workflow | trigger |
|---|---|---|
| `Bitcredit-Core` | `wasm_release.yml` — npm publish and the GitHub release | `workflow_dispatch`, `environment: release-wasm` |
| `wallet` | `build-candidate.yml` — creates the release if absent | `push: tags: v*.*.*` |
| `ui` | `npm_release.yml` — publishes to npmjs and GitHub Packages | `push: tags: v*` |
| `Wallet-Core` | cargokit publishes the `precompiled_*` releases | build |

Everything else is published by a person. `ui` is worth knowing about: its package
publish is automated and its GitHub release is not, which is why so many of its
releases are empty.

Package release tags keep the plain `vX.Y.Z` form, with no date. They are the
repository's own numbering, and the absence of a date suffix is what tells them
apart from a train tag.

## `bcr-common`

The wire crate is being published to **crates.io** — decided 2026-09-01, tracked in
its own repository. Until that lands, consumers pin git revisions, and that is the
documented state rather than an oversight.

Its existing tags do **not** describe what consumers use: the newest, `v0.7.0`, is
155 commits behind `master` while the manifest says `0.10.0`. Do not pin to them.

## Tag protection

The organisation ruleset **`All tags`** applies to every tag in every repository —
`deletion` and `non_fast_forward`, enforcement active, bypass for organisation
admins only. A train tag is protected the moment it is pushed; nothing needs
configuring.

---

Questions about a specific repository's build belong in its own `README.md`. How to
contribute at all is in [CONTRIBUTING.md][contributing].

[contributing]: https://github.com/BitcreditProtocol/.github/blob/master/CONTRIBUTING.md
