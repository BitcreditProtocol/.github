<!--
Organisation-wide default. A repository can override it with its own
CONTRIBUTING.md. Repository-specific contribution and governance instructions
take precedence over this guide.
Everything below describes what is actually configured and enforced. If you
change a ruleset or a repository setting, change this file with it.
-->

# Contributing

Thanks for working on Bitcredit. This describes how contributions actually move
through this organisation. Repository rules show the effective protections,
including any exceptions to the organisation baseline. Governance retains its
separate contribution and decision process.

## Before you start

- **Two-factor authentication is required** to be a member of this organisation,
  and the requirement is set at the enterprise level as well.
- Read the repository's **LICENSE** before contributing; this shared guide does
  not replace the licence carried by that repository.
- **Never report a security problem in a public issue or pull request.** Use the
  repository's **Security** tab, or the private routes in
  [the organisation security policy][security] — a GitHub security advisory, or
  email with a PGP key. A few repositories publish their own `SECURITY.md` with
  a different contact; if the repository has one, that one wins.
- [The code of conduct][coc] applies everywhere, including reviews.

## Finding something to work on

Work is tracked as **issues on the repository itself**. There are also
organisation-level project boards, but most of them are private, so if you are
not a member you will see only part of the picture — the repository's own issue
list is the reliable place to look.

Four labels are worth knowing. All four are defined in [`labels.yml`][labels],
which is the only place any of them is described:

| Label | What it means | Where |
|---|---|---|
| `good first issue` | open to anyone, and a reasonable starting point | every repository |
| `help wanted` | open to anyone, not necessarily beginner-friendly | every repository |
| `blocked` | waiting on other work — do not start it | the repositories that use it |
| `staff only` | needs infrastructure access or institutional knowledge that cannot practically be handed over — do not start it | the repositories that use it |

The distinction in that last column is real rather than pedantic. The first two
are created everywhere automatically; the other two are corrected where a
repository already has them and are never added to a repository that does not,
because which workflow labels a team wants is that team's business. So a
repository having no `blocked` label does not mean nothing there is blocked.

The first two are also **applied sparingly** — at the time of writing only a
couple of open issues carry either, out of several hundred. Their absence is not
a signal that an issue is unavailable. If an issue interests you and nothing
marks it as taken, ask in a comment.

Before you start, read the issue's comments. If someone has said they are on it
and there has been activity in the last week or so, pick something else.

**For anything larger than a small fix, open an issue first** and describe what
you intend to do and why. This is the maintainers' preference rather than
something enforced — a one-line change does not need one, a redesign does, and
agreeing the approach before you write it is cheaper for everybody than
discovering the disagreement in review.

## Work out which branch to target first

**The default branch is not always the one to open your pull request against.**
These product repositories normally develop on `dev` and promote changes to
their default branch for releases:

| Repository | Open pull requests against |
|---|---|
| `E-Bill-frontend` | `dev` |
| `eBill` | `dev` |
| `wallet` | `dev` |
| `wildcat-dashboard-ui` | `dev` |

For other repositories and changes to CI or release management, confirm the
intended base with the repository's instructions or the agreed issue/PR scope.
Check the actual default branch rather than assuming `master` or `main`.

If you are unsure, ask the repository what it does:

```bash
gh pr list --repo BitcreditProtocol/<repo> --state merged --limit 30 \
  --json baseRefName --jq '[.[].baseRefName] | group_by(.) | map({(.[0]): length}) | add'
```

Branch from the branch you intend to merge into. Basing work on the default
branch when the repository develops on `dev` produces conflicts in exactly the
files other people are changing.

## What the rules enforce

The organisation baseline for the **default branch** provides these rules.
Check the repository's **Rules** page for its effective scope and exceptions:

- **One approving review** is required before merge.
- **Copilot code review** is requested automatically on every branch.
- The branch cannot be **deleted** or **force-pushed**.
- All three merge methods — merge, squash, rebase — are available. Pick whatever
  suits the change; nothing enforces one.

Two things that are deliberately *not* enforced, and are worth knowing:

- **No status check is required to merge.** A red check does not block anything,
  so read the checks yourself instead of trusting the merge button. Several
  suites in this organisation are chronically red for reasons unrelated to your
  change — if one fails, confirm it was already failing before you blame it on
  yourself, and say so in the pull request.
- **Approvals are not dismissed when you push.** An approval survives later
  commits, so if you change something substantive after review, say so rather
  than relying on the process to notice.

On `dev`, the ruleset protects against deletion only. Review there is the
repository's own convention rather than something enforced — follow whatever the
repository already does.

## Make your commits attributable

The organisation baseline requires an additional approval for **a commit whose
author email is not linked to a GitHub account**. Repository-specific exceptions
are shown on the Rules page. Before you start:

```bash
git config user.email   # must be an email on your GitHub account
```

The organisation baseline does not require signed commits. Signing is welcome;
correct author attribution is still required for the approval rule above.

## Opening the pull request

The template asks three things. The third is the one reviewers read:

- **What** changes, in a sentence or two.
- **Why** — the problem it solves. `Closes #123` if there is an issue.
- **How it was verified** — what you ran and what it showed. "CI is green"
  counts. So does a manual check, if you say what you checked.

Review your own diff before requesting review, and make sure no secret, token or
credential is in it. Both are checkboxes on the template because both get
missed.

For issues, use the templates — there is one for bug reports and one for feature
requests.

**Reviewing someone else's pull request is welcome**, whether or not you are the
assigned reviewer. Review capacity is the thing this organisation is shortest
of. Be kind and specific about it: say what you would change and why, not just
that something is wrong.

## After it merges

The repository's merge settings control automatic branch deletion and available
merge methods. Passing PR checks or a PR approval do not establish successful
publication or deployment; follow the repository's merge and release process.

## How your change ships

Merging is not shipping. Five repositories — `Wildcat`, `Clowder`,
`Wildcat-Auxiliary`, `Wildcat-deployment` and `wildcat-dashboard-ui` — ship
together as one coordinated **train**; every other repository releases on its own
schedule. Which version number means what, how a train is cut, and what a release
has to carry are in [RELEASING.md][releasing].

One thing worth knowing before you file a bug about it: **the version in a
manifest and the version in a tag are two different numbers here**, and they are
not expected to agree.

## Dependencies

Where Dependabot is configured, each repository's `.github/dependabot.yml`
defines its ecosystems, groups, schedule, cooldown and assignees.
**Do not hand-bump a dependency it already offers** — you will conflict with an
open pull request, and the bump will be raised again anyway. If an update needs
code changes, do that work on the Dependabot branch or in its own pull request
and say which advisory or bump it is for.

## Which kind of repository am I in?

Every repository carries a `stack` custom property — `rust`, `node`, `flutter`,
`infra`, `docs`, or `none` — visible on the repository page. Build and test
instructions live in each repository's own `README.md`; this file deliberately
does not duplicate them.

[security]: https://github.com/BitcreditProtocol/.github/blob/master/SECURITY.md
[coc]: https://github.com/BitcreditProtocol/.github/blob/master/CODE_OF_CONDUCT.md
[labels]: https://github.com/BitcreditProtocol/.github/blob/master/labels.yml
[releasing]: https://github.com/BitcreditProtocol/.github/blob/master/RELEASING.md
