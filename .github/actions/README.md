# Shared composite actions

Steps that were pasted into workflows across the organisation and now exist once.
This repository is public, so any repository can reference them without an Actions
access policy — internal repositories included.

| Action | Replaces | Was duplicated in |
| --- | --- | --- |
| `free-disk-space` | an 11-line inline block, identical everywhere | 13 workflows |
| `git-auth` | a 5-line `git config ... insteadOf` step | 18 workflows |

## Using them

Reference a **release**, never a branch:

```yaml
      - uses: BitcreditProtocol/.github/.github/actions/free-disk-space@<sha>  # v1.0.0

      - uses: BitcreditProtocol/.github/.github/actions/git-auth@<sha>  # v1.0.0
        with:
          token: ${{ steps.app-token.outputs.token }}
```

The doubled `.github/.github/` is not a typo: the first is this repository's name,
the second is the directory inside it.

Pin the commit SHA and keep the `# v1.0.0` comment. Dependabot reads that comment
to know what to bump and rewrites both the SHA and the comment when a new release
is published — the same way every third-party action in this organisation is
handled. A branch reference would also be rejected outright once
`sha_pinning_required` is on at the enterprise.

**A composite that nobody can bump is worse than a copied snippet.** That is why
these are released rather than merely committed: without a tag to move to,
Dependabot has nothing to propose, and a fix here would never reach a consumer.

## Releasing a change

1. Merge the change here.
2. Tag the merge commit `vMAJOR.MINOR.PATCH` and publish a release.
3. Dependabot opens a pull request in each consuming repository on its next run.

Tags are protected by the organisation's `All tags` ruleset, so a published
version cannot be moved or deleted by anyone but an organisation admin. Consumers
stay on the version they pinned until a bump is merged, which is what keeps a
mistake here from breaking five repositories at once.

## What these deliberately do not do

`git-auth` rewrites five URL forms — `https`, `ssh://git@`, `git@github.com:`,
`git://` and `ssh://github.com/`. Most callers only need `https`, which is what
most of the inlined copies did. The extra forms are why `wallet` wrote its own
copy: a cargo or pub manifest that names a dependency as `git@github.com:...`
fails silently when only `https` is rewritten. Rewriting more forms never removes
behaviour, so every caller gets the superset.

It does **not** echo the resulting configuration back. One earlier copy ended with
`git config --get-all`, which prints a config key containing the token into the
log and reports only values that are known constants anyway.

Neither action mints a token. `actions/create-github-app-token` stays in the
calling workflow, because a composite action cannot read `secrets` — the caller
would have to pass the private key in either case, so wrapping it would move a
line rather than remove one, while adding a version to keep in step.

`free-disk-space` takes no inputs. Every one of the 13 copies it replaces was
byte-identical, so there is nothing to parameterise until something actually
differs.
