#!/usr/bin/env bash
#
# Report configuration drift across every non-archived repository, and enforce
# the one thing that is purely additive: the organisation topics.
#
# Everything else is reported rather than applied. Silently flipping repository
# settings underneath people is worse than telling them what is off — a wiki
# that looks empty may have been enabled five minutes ago on purpose.
#
# Repositories come from the API on every run, so a new repository shows up by
# itself and an archived one drops out.
#
# App permissions. The App was set up with Metadata, Issues, Administration,
# Contents and Dependabot alerts, which is what everything above the credential
# checks needs. The checks added since need more, and none of them is granted at
# the time of writing:
#
#   Members: read                organisation      environment reviewers
#   Secrets: read                organisation      shadowing, not-granted
#   Custom properties: read      organisation      the `stack` check
#   Secrets: read                repository        shadowing, orphaned credentials,
#                                                  the secret-surface figures
#   Variables: read              repository        orphaned credentials
#   Environments: read           repository        unprotected environments,
#                                                  non-member reviewers
#
# Each is probed once at startup rather than assumed, and whatever is missing is
# named in a "Not measured on this run" section. A check whose input could not be
# read is skipped, never reported as zero: an unreadable answer and an empty one
# are indistinguishable, and reporting the second when it was the first is a
# false all-clear -- on the secret surface it would read as "nothing is ungated".
#
# Env: ORG (required), GH_TOKEN (required), BASELINE_CONFIG (required),
#      DRY_RUN (default false), LICENSE_JSON (optional, skips the yaml
#      conversion — used when testing)

set -eu

: "${ORG:?ORG is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${BASELINE_CONFIG:?BASELINE_CONFIG is required}"
DRY_RUN="${DRY_RUN:-false}"

REQUIRED_TOPICS="bitcoin bitcredit"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

# How recently a branch must have been pushed for a reference on it to count as
# live. Used by the orphaned-credential check: a dead branch must not block a
# deletion, and an active one must. GNU date on the runner, BSD date locally.
CUTOFF="$(date -u -d '90 days ago' '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
          || date -u -v-90d '+%Y-%m-%dT%H:%M:%SZ')"

# How recently a pull request must have moved for its drift to be worth counting
# as live. Used by the behind-base metric to separate branches somebody is still
# working on from ones that have been parked.
CUTOFF30="$(date -u -d '30 days ago' '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
            || date -u -v-30d '+%Y-%m-%dT%H:%M:%SZ')"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ -n "${LICENSE_JSON:-}" ]; then
  cp "$LICENSE_JSON" "$WORK/license.json"
else
  yq -o=json '.' "$(dirname "$0")/../../license.yml" > "$WORK/license.json"
fi
HOLDER=$(jq -r '.holder' "$WORK/license.json")

if [ -n "${ASSIGNEES_JSON:-}" ]; then
  cp "$ASSIGNEES_JSON" "$WORK/assignees.json"
else
  yq -o=json '.' "$(dirname "$0")/../../dependabot-assignees.yml" > "$WORK/assignees.json"
fi

gh api "orgs/$ORG/repos?per_page=100" --paginate --slurp > "$WORK/repos.json"

# Custom property values for the whole organisation in one fetch -- 42 rows for
# 30 active repositories plus 12 archived, well inside a single page. --slurp
# rather than --jq for the same reason as the line above: --paginate with --jq
# returns one array per page and has produced an empty set three times here.
# Every read below is one the audit gained after the App's permissions were last
# reviewed, and a 403 on any of them is indistinguishable from an empty answer:
# no members makes every environment reviewer a non-member, no organisation
# secrets makes the secret-surface table report "behind no gate: 0", which is a
# false all-clear on its headline number. So each records a named gap and the
# checks that depend on it are skipped, the same shape the Dependabot section
# already uses. gh api is kept out of the pipeline so its status stays visible.
: > "$WORK/gaps"
gap() { echo "$1" >> "$WORK/gaps"; }

have_props=""
if gh api "orgs/$ORG/properties/values?per_page=100" --paginate --slurp > "$WORK/props.json" 2>/dev/null \
   && jq -e 'type == "array"' "$WORK/props.json" >/dev/null 2>&1; then
  have_props=1
else
  echo '[[]]' > "$WORK/props.json"
  gap "custom property values — needs \`Custom properties: read\`; the \`stack\` check is skipped"
fi

# Organisation membership, for the environment-reviewer check. An approval
# request cannot be routed to somebody without access, so such a reviewer is
# dead weight that makes a gate look stronger than it is: bcr-relay/prod
# appeared to have four approvers and had three.
have_members=""
# -s as well as exit 0. Without `Members: read` this endpoint answers 200 with an
# empty list rather than 403, so testing only the status set have_members=1 on an
# empty file and every reviewer in the organisation was reported as a non-member.
# An empty membership is not a possible true answer here: the audit is running as
# an App installed by an organisation that has at least one member.
if gh api "orgs/$ORG/members?per_page=100" --paginate --jq '.[].login' > "$WORK/members.raw" 2>/dev/null \
   && [ -s "$WORK/members.raw" ]; then
  sort "$WORK/members.raw" > "$WORK/members"; have_members=1
else
  : > "$WORK/members"
  gap "organisation members — needs \`Members: read\`; the environment-reviewer check is skipped"
fi

# Organisation secret names, and which repositories each is granted to. Read
# once: five secrets, so five extra requests rather than five per repository.
have_orgsecrets=""
: > "$WORK/orggrants"
if gh api "orgs/$ORG/actions/secrets?per_page=100" --paginate --jq '.secrets[]?.name' > "$WORK/orgsecrets.raw" 2>/dev/null; then
  sort "$WORK/orgsecrets.raw" > "$WORK/orgsecrets"
  have_orgsecrets=1
  while IFS= read -r s; do
    [ -z "$s" ] && continue
    if gh api "orgs/$ORG/actions/secrets/$s/repositories" --paginate --jq '.repositories[]?.name' > "$WORK/grant.raw" 2>/dev/null; then
      sed "s|^|$s |" "$WORK/grant.raw" >> "$WORK/orggrants"
    else
      # One unreadable grant list would make every reference to that secret look
      # ungranted, so the not-granted check is dropped rather than half-fed.
      have_orgsecrets=""
      gap "the repository list for organisation secret \`$s\` — the shadowing and not-granted checks are skipped"
      break
    fi
  done < "$WORK/orgsecrets"
else
  : > "$WORK/orgsecrets"
  gap "organisation secret names — needs \`Secrets: read\` at organisation level; the shadowing and not-granted checks are skipped"
fi

# Repository-level capability probes. None of these endpoint families is covered
# by the permissions this App was set up with -- Metadata, Issues,
# Administration, Contents, Dependabot alerts -- so a 403 is the expected result
# rather than an exceptional one, and a 403 that reads as an empty answer is
# worse than having no check at all: no repository secrets makes the surface
# table report zero, and no environments makes every gate look absent. Probed
# once against this repository, because the answer describes the token and not
# the target, and probing per repository would cost 29 requests to learn one
# fact.
have_reposecrets=""; have_repovars=""; have_envs=""; envsec_gap=""
if gh api "repos/$ORG/.github/actions/secrets?per_page=1" >/dev/null 2>&1; then
  have_reposecrets=1
else
  gap "repository secrets — needs \`Secrets: read\`; the shadowing check, the orphaned-credential check and the secret-surface figures are skipped"
fi
if gh api "repos/$ORG/.github/actions/variables?per_page=1" >/dev/null 2>&1; then
  have_repovars=1
else
  gap "repository variables — needs \`Variables: read\`; variables are left out of the orphaned-credential check"
fi
if gh api "repos/$ORG/.github/environments?per_page=1" >/dev/null 2>&1; then
  have_envs=1
else
  gap "environments — needs \`Environments: read\`; the unprotected-environment check, the non-member-reviewer check and the environment half of the secret surface are skipped"
fi

# Pages needs its own probe shape, because here a failure is ambiguous in a way
# the others are not: 403 is "no permission" and 404 is "this repository has no
# site", and a repository with no site is the normal case. Testing only the exit
# code would declare the permission missing on any organisation whose .github
# repository does not publish, so the error text decides. Succeeded, or failed
# for any reason other than the App wall, means the endpoint is readable.
have_pages=""
if gh api "repos/$ORG/.github/pages" >/dev/null 2>"$WORK/pageprobe" \
   || ! grep -q 'Resource not accessible by integration' "$WORK/pageprobe"; then
  have_pages=1
else
  gap "Pages sites — needs \`Pages: read\`; the check for a repository publishing a public site is skipped"
fi

# Counters for the metrics that are deliberately not findings.
n_untimed=0; n_agentfiles=0; n_agentrepos=0; n_wiki=0
: > "$WORK/untimedrepos"
: > "$WORK/wikirepos"
jq -r 'add | .[] | select(.archived == false) | .name' "$WORK/repos.json" > "$WORK/repos"
total=$(wc -l < "$WORK/repos" | tr -d ' ')
echo "Auditing $total non-archived repositories (dry_run=$DRY_RUN)"

n_topics=0; n_merge=0
: > "$WORK/findings"

# The organisation copies of the inherited community files, fetched once. A
# repository carrying a byte-identical copy is inheriting nothing and gains a file
# that can silently drift; three of these existed and differed only by a trailing
# newline, which is exactly how long a duplicate stays identical.
mkdir -p "$WORK/org"
for f in CONTRIBUTING.md CODE_OF_CONDUCT.md SECURITY.md; do
  gh api "repos/$ORG/.github/contents/$f" -H "Accept: application/vnd.github.raw" \
    > "$WORK/org/$f" 2>/dev/null || : > "$WORK/org/$f"
done

while IFS= read -r repo; do
  [ -z "$repo" ] && continue
  meta=$(gh api "repos/$ORG/$repo")

  # --- enforce: organisation topics (additive, never removes anything)
  gh api "repos/$ORG/$repo/topics" --jq '.names // [] | .[]' > "$WORK/topics"
  missing=""
  for t in $REQUIRED_TOPICS; do
    grep -Fxq "$t" "$WORK/topics" || missing="$missing $t"
  done
  if [ -n "$missing" ]; then
    echo "  $repo: adding topics$missing"
    { cat "$WORK/topics"; for t in $missing; do echo "$t"; done; } | sort -u \
      | jq -Rs 'split("\n") | map(select(length > 0)) | {names: .}' > "$WORK/topics.json"
    [ "$DRY_RUN" = "true" ] || gh api -X PUT "repos/$ORG/$repo/topics" --input "$WORK/topics.json" >/dev/null
    n_topics=$((n_topics + 1))
  fi

  # --- enforce: merge settings. Every flag here only enables something, so a
  # repository can gain a merge method or branch cleanup but never lose one.
  drift=$(echo "$meta" | jq -r '
    {allow_merge_commit, allow_squash_merge, allow_rebase_merge,
     delete_branch_on_merge, allow_update_branch}
    | to_entries | map(select(.value != true) | .key) | join(", ")')
  if [ -n "$drift" ]; then
    echo "  $repo: enabling $drift"
    [ "$DRY_RUN" = "true" ] || gh api -X PATCH "repos/$ORG/$repo" \
      -F allow_merge_commit=true -F allow_squash_merge=true -F allow_rebase_merge=true \
      -F delete_branch_on_merge=true -F allow_update_branch=true >/dev/null
    n_merge=$((n_merge + 1))
  fi

  # --- report only
  visibility=$(echo "$meta" | jq -r '.visibility')

  [ "$(echo "$meta" | jq -r '.description // ""')" = "" ] &&
    echo "$repo|no description" >> "$WORK/findings"

  # A repository with no stack value. The property is what lets a ruleset or a
  # report address a group of repositories instead of naming them one by one, so
  # an unset value silently drops a repository out of every such selection.
  # "none" is an explicit value for the three repositories that genuinely have no
  # stack, which is what keeps this check at zero rather than carrying permanent
  # exceptions -- the failure the dependabot coverage check had until it was
  # narrowed. It found crowdin-sdk, which arrived as a fork on 2026-08-28 and was
  # the only active repository without one; the archived twelve are deliberately
  # unset and never reach here, because the loop filters them.
  # Gated on have_props. The gap was declared and the check was not skipped, so a
  # 403 on the property values reported all 29 repositories as unclassified --
  # a gap message and a wall of findings saying the opposite of each other.
  if [ -n "$have_props" ] && [ "$(jq -r --arg r "$repo" 'add | map(select(.repository_name == $r))
        | .[0].properties // [] | map(select(.property_name == "stack"))
        | .[0].value // ""' "$WORK/props.json")" = "" ]; then
    echo "$repo|no stack custom property" >> "$WORK/findings"
  fi

  # LICENSE: present at all, and naming the holder we expect
  if ! gh api "repos/$ORG/$repo/license" --jq '.content' 2>/dev/null | base64 -d > "$WORK/lic" 2>/dev/null \
     || [ ! -s "$WORK/lic" ]; then
    echo "$repo|no LICENSE ($(echo "$meta" | jq -r .visibility))" >> "$WORK/findings"
  else
    line=$(grep -i -m1 '^copyright' "$WORK/lic" || true)
    expected=$(jq -r --arg r "$repo" '.third_party // [] | map(select(.repository == $r)) | .[0].holder // ""' "$WORK/license.json")
    [ -n "$expected" ] || expected="$HOLDER"
    case "$line" in
      *"$expected"*) : ;;
      "") echo "$repo|LICENSE has no copyright line" >> "$WORK/findings" ;;
      *)  echo "$repo|LICENSE names '${line#*) }' — expected '$expected'" >> "$WORK/findings" ;;
    esac
  fi

  # Reported only where there is something for Dependabot to update. Six
  # repositories carry no package manifest at all — documentation and asset
  # repositories — and flagging them every week would keep the issue permanently
  # open and train everyone to ignore it. Detection is by manifest, not by a list
  # of repository names, so a repository that later gains a manifest starts being
  # reported without anyone editing this script.
  branch=$(echo "$meta" | jq -r '.default_branch')
  gh api "repos/$ORG/$repo/git/trees/$branch?recursive=1" \
    --jq '[.tree[]? | select(.type == "blob") | .path] | join("\n")' 2>/dev/null > "$WORK/tree.all" || : > "$WORK/tree.all"

  # Vendored and generated paths carry manifests that are not ours to update.
  # Dependabot pointed at a vendored copy would diverge it from upstream, so a
  # manifest in here is not evidence of a missing ecosystem.
  grep -vE '(^|/)(node_modules|vendor|target|build|\.dart_tool|cargokit|crowdin_sdk)/' \
    "$WORK/tree.all" > "$WORK/tree" || : > "$WORK/tree"

  eco=""
  grep -qiE '(^|/)Cargo\.toml$'                      "$WORK/tree" && eco="$eco cargo"
  grep -qiE '(^|/)package\.json$'                    "$WORK/tree" && eco="$eco npm"
  grep -qiE '(^|/)pubspec\.yaml$'                    "$WORK/tree" && eco="$eco pub"
  grep -qiE '(^|/)go\.mod$'                          "$WORK/tree" && eco="$eco gomod"
  grep -qiE '(^|/)(requirements.*\.txt|pyproject\.toml)$' "$WORK/tree" && eco="$eco pip"
  grep -qiE '(^|/)Gemfile$'                          "$WORK/tree" && eco="$eco bundler"
  grep -qiE '(^|/)composer\.json$'                   "$WORK/tree" && eco="$eco composer"
  # Alternation binds looser than the anchors, so a single trailing $ would only
  # have closed the second branch: '(^|/)Dockerfile|...$' also matched
  # docs/Dockerfile-notes.md. Each branch is anchored on its own, and the
  # optional suffix keeps the common Dockerfile.dev convention detected.
  grep -qiE '(^|/)Dockerfile(\.[A-Za-z0-9_-]+)?$|(^|/)docker-compose.*\.ya?ml$' \
    "$WORK/tree" && eco="$eco docker"
  grep -qiE '\.tf$'                                  "$WORK/tree" && eco="$eco terraform"
  # workflows need no manifest: the github-actions ecosystem updates the
  # action versions pinned inside them
  grep -qE '^\.github/workflows/.*\.ya?ml$'          "$WORK/tree" && eco="$eco github-actions"

  if ! gh api "repos/$ORG/$repo/contents/.github/dependabot.yml" >/dev/null 2>&1; then
    if [ -n "$eco" ]; then
      echo "$repo|no .github/dependabot.yml, but has$eco" >> "$WORK/findings"
    fi

  # Dependabot assigns nobody by default, and the setting lives in each
  # repository's own file — so an unassigned configuration is invisible unless
  # something compares it against a list. That is how helm-charts stayed
  # unassigned, and how every cargo, npm and pub block in the organisation ended
  # up with no assignee while the github-actions blocks had one.
  else
    gh api "repos/$ORG/$repo/contents/.github/dependabot.yml" --jq '.content' 2>/dev/null \
      | base64 -d > "$WORK/db.yml" 2>/dev/null || : > "$WORK/db.yml"
    want=$(jq -r --arg r "$repo" '.assignees[$r] // ""' "$WORK/assignees.json")

    # A fork's dependabot.yml belongs to the project we forked. crowdin-sdk
    # arrived in August 2026 as a fork of crowdin/flutter-sdk carrying upstream's
    # file -- monthly pub, all majors ignored, no assignees -- and demanding our
    # assignee convention there would mean editing a forked file to diverge from
    # upstream for no gain. So dependabot-assignees.yml records the exemption
    # rather than the script testing .fork directly: a list says which repository
    # and which upstream, and it stops applying when the premise goes, which a
    # fork test cannot notice. If the repository is no longer a fork, say so --
    # the same reasoning license.yml gives for third_party.
    upstream=$(jq -r --arg r "$repo" \
      '.upstream_config // [] | map(select(.repository == $r)) | .[0].upstream // ""' \
      "$WORK/assignees.json")
    if [ -n "$upstream" ] && [ "$(echo "$meta" | jq -r '.fork')" != "true" ]; then
      echo "$repo|listed in dependabot-assignees.yml upstream_config as forked from $upstream, but is not a fork any more — the exemption has lost its premise" >> "$WORK/findings"
    fi

    if [ -z "$want" ] && [ -z "$upstream" ]; then
      echo "$repo|has dependabot.yml but no entry in dependabot-assignees.yml" >> "$WORK/findings"
    elif ! yq -o=json '.' "$WORK/db.yml" > "$WORK/db.json" 2>/dev/null; then
      echo "$repo|.github/dependabot.yml does not parse as YAML" >> "$WORK/findings"
    else
      # Every update block, not just the first: the blocks are what produce pull
      # requests, and a repository can easily have one assigned and one not.
      wrong=$(jq -r --arg w "$want" '
        [ .updates[]
          | (.assignees // [])
          | if . == [$w] then empty
            elif length == 0 then "nobody"
            else join("+") end ]
        | unique | join(", ")' "$WORK/db.json")
      # No expected assignee means the repository is exempt above, not that
      # every block is wrong -- with $want empty the comparison below calls
      # every block "nobody".
      if [ -n "$wrong" ] && [ -n "$want" ]; then
        echo "$repo|Dependabot assignee should be $want on every update block, found: $wrong" >> "$WORK/findings"
      fi

      # Existence is not coverage. Five repositories had a dependabot.yml that
      # updated their actions and ignored their code -- three Rust workspaces with
      # no cargo block at all -- so 32 alerts had no route to a fix while every
      # check here passed. Compared by ecosystem rather than by directory, because
      # a vendored manifest is deliberately left uncovered.
      configured=$(jq -r '[.updates[]?."package-ecosystem"] | unique | join(" ")' "$WORK/db.json")
      uncovered=""
      for e in $eco; do
        # docker is uncovered on purpose. Nine repositories carry a Dockerfile
        # or a compose file and no docker block, and that was a decision rather
        # than an oversight: the ecosystem needs a docker label in labels.yml
        # and would add a notable volume of pull requests, so it was deferred
        # until after the P2 hoisting work. Reporting nine repositories every
        # week against a standing decision is how a report trains its audience
        # to ignore it -- the same reasoning as the no-manifest case above.
        # Delete this line when the docker blocks land and the check covers
        # them again with no other edit. terraform is deliberately not skipped:
        # it fires on one repository, and one standing line is a reminder.
        [ "$e" = docker ] && continue
        case " $configured " in *" $e "*) ;; *) uncovered="$uncovered $e" ;; esac
      done
      # Gated on upstream_config, like the assignee and groups checks. A fork's
      # dependabot.yml is upstream's file, so covering an ecosystem there means
      # diverging from them -- which #31 recorded as an exemption rather than a
      # fix. The earlier reasoning here, that coverage is about our own alert
      # exposure and not upstream's conventions, is true but does not justify the
      # asymmetry: the exemption is an explicit per-repository list, so gating
      # touches only what is deliberately marked upstream-owned. Revisit if a
      # fork is ever something we build and deploy ourselves.
      if [ -n "$uncovered" ] && [ -z "$upstream" ]; then
        echo "$repo|dependabot.yml does not cover:$uncovered (has: $configured)" >> "$WORK/findings"
      fi

      # One group per code ecosystem. Two -- a patch group and a minor group --
      # doubles the weekly pull requests for no gain, and none at all means every
      # dependency arrives separately: that is what produced a 68-pull-request
      # backlog nobody could filter. github-actions is excluded because its group
      # is patterns-based and monthly by design. A fork is excluded too: its
      # dependabot.yml belongs upstream, recorded in upstream_config rather than
      # tested for here, for the reason license.yml gives about third_party.
      if [ -z "$upstream" ]; then
        badgroups=$(jq -r '[.updates[]?
          | select(."package-ecosystem" != "github-actions")
          | select(((.groups // {}) | length) != 1)
          | "\(."package-ecosystem") has \(((.groups // {}) | length)) group(s)"]
          | unique | join(", ")' "$WORK/db.json")
        [ -n "$badgroups" ] &&
          echo "$repo|an ecosystem should have exactly one group: $badgroups" >> "$WORK/findings"
      fi

      # Dependabot applies only labels that already exist and silently drops the
      # rest. Fifteen of twenty-two configured repositories named a label they did
      # not have -- 31 pairs -- so pull requests arrived carrying fewer labels than
      # the file asked for, and seven arrived with none at all.
      gh api "repos/$ORG/$repo/labels?per_page=100" --paginate --jq '.[].name' > "$WORK/labels" 2>/dev/null || : > "$WORK/labels"
      # while read, not for-in: an unquoted command substitution word-splits, so a
      # multi-word label -- labels.yml defines five, including `good first issue`
      # -- would be reported as several missing labels that are all present. No
      # manifest asks for one today (67 label references across 24 manifests, all
      # single-word), which is exactly how a defect like this survives review.
      missing_labels=""
      while IFS= read -r l; do
        [ -z "$l" ] && continue
        grep -Fxq "$l" "$WORK/labels" || missing_labels="$missing_labels $l"
      done < <(jq -r '[.updates[]?.labels[]?] | unique | .[]' "$WORK/db.json")
      if [ -n "$missing_labels" ]; then
        echo "$repo|dependabot.yml asks for labels this repository does not have:$missing_labels" >> "$WORK/findings"
      fi
    fi
  fi

  # A lock file inside a cargo workspace member is never read: cargo resolves a
  # member against the workspace-root lock. So it is either stale or a mistake, and
  # it still produces Dependabot alerts against versions nothing builds with. One
  # sat there for six months and generated two unactionable alerts.
  if grep -qxE 'Cargo\.lock' "$WORK/tree"; then
    nested=$(grep -E '.+/Cargo\.lock$' "$WORK/tree" | tr '\n' ' ')
    [ -n "$nested" ] &&
      echo "$repo|lock file inside a cargo workspace, ignored by cargo: $nested" >> "$WORK/findings"
  fi

  # A write permission that is never exercised. Four workflows granted
  # attestations: write and produced no attestation, while ten container images
  # shipped with no provenance at all.
  : > "$WORK/wfbody"
  : > "$WORK/prt"
  : > "$WORK/floating"
  : > "$WORK/idtoken"
  : > "$WORK/noperm"
  while IFS= read -r wf; do
    [ -z "$wf" ] && continue
    body=$(gh api "repos/$ORG/$repo/contents/$wf?ref=$branch" -H "Accept: application/vnd.github.raw" 2>/dev/null)
    [ -z "$body" ] && continue
    # kept for the credential check below: these bodies are already paid for here
    printf '%s\n' "$body" >> "$WORK/wfbody"

    # pull_request_target runs in the base repository's context, with its token and
    # its secrets, on a pull request from anywhere. It is only an escalation when
    # something attacker-controlled then executes -- a checkout of the head ref, a
    # build, a script reading the branch -- so this reports the trigger rather than
    # asserting a vulnerability, and a reviewer decides. crowdin-sdk acquired one in
    # August 2026 by being forked with upstream's CI intact; that one reads the
    # pull-request title from the event payload and checks out nothing, so it is
    # safe as it stands. The point is that nothing would have said so.
    #
    # workflow_run and repository_dispatch are deliberately not reported. Both
    # exist, both are recorded in the plan, and neither is going to change, so a
    # weekly line for each would be two permanent findings against no decision.
    # Both spellings. `on: [pull_request_target]` carries no colon after the
    # trigger, so matching only `pull_request_target:` misses it. Nothing in the
    # estate uses flow style today; the fork that brought us a
    # pull_request_target at all did not, which is not a reason to rely on it.
    case "$body" in
      *pull_request_target:*|*pull_request_target,*|*pull_request_target\]*)
        printf '%s\n' "$wf" >> "$WORK/prt" ;;
    esac

    # id-token: write lets a workflow mint an OIDC identity. Granted and never
    # used it is a capability nobody asked for, and the consumers are a closed
    # list: a cloud login, an npm or PyPI publish with provenance, buildx
    # A file whose jobs call a reusable workflow has delegated the work, and with
    # it the reason for the permission: a called workflow's token is the
    # INTERSECTION of the caller's and its own, so a caller that omits the grant
    # silently strips it. Clowder's build.yml and nightly.yml are three lines each
    # and exist only to call build-image.yml, where the GCP auth and both attest
    # steps live -- and the called workflow is scanned in its own right, so a real
    # gap still surfaces there rather than being lost here.
    delegates=""
    printf '%s\n' "$body" | grep -qE '^[[:space:]]+uses:[[:space:]]*[^[:space:]]*\.github/workflows/' \
      && delegates=1

    # provenance, an attestation, cosign. Nine files grant it without one.
    [ -n "$delegates" ] || case "$body" in
      *"id-token: write"*)
        printf '%s\n' "$body" | grep -qE 'npm publish|--provenance|provenance:[[:space:]]*true|google-github-actions/auth|aws-actions/configure-aws-credentials|azure/login|attest-build-provenance|cosign|sigstore|pypa/gh-action-pypi|dart-lang/setup-dart' \
          || printf '%s\n' "$wf" >> "$WORK/idtoken"
        ;;
    esac

    # A public repository whose pull_request workflow declares no permissions
    # inherits whatever the default is, on a run triggered from a fork. The
    # organisation default is read, so this is a tightening rather than a hole --
    # but the default is a setting somebody can change, and a workflow that
    # states its own needs does not depend on that.
    if [ "$visibility" = "public" ]; then
      case "$body" in
        *"pull_request:"*)
          printf '%s\n' "$body" | grep -qE '^[[:space:]]*permissions:' \
            || printf '%s\n' "$wf" >> "$WORK/noperm"
          ;;
      esac
    fi

    # Untimed jobs, counted rather than reported: fifty across thirteen
    # repositories would bury every other finding, and fifteen open pull
    # requests fix most of it. A job that calls a reusable workflow cannot take
    # timeout-minutes at all, so those are excluded -- the timeout for that work
    # belongs inside the called workflow.
    printf '%s\n' "$body" > "$WORK/wf1.yml"
    if yq -o=json '.' "$WORK/wf1.yml" > "$WORK/wfjson" 2>/dev/null; then
      c=$(jq '[.jobs // {} | to_entries[] | select((.value|type)=="object")
               | select((.value|has("uses")) == false)
               | select((.value|has("timeout-minutes")) == false)] | length' "$WORK/wfjson" 2>/dev/null || echo 0)
      n_untimed=$((n_untimed + c))
      [ "$c" -gt 0 ] && printf '%s\n' "$repo" >> "$WORK/untimedrepos"
    fi

    # A tag or branch in a uses: reference is a moving target: whoever controls it
    # can change what runs here, and the reference says nothing about what ran last
    # time. Every active default branch in this organisation is SHA-pinned -- the
    # figure Clemens was given when asked to enable sha_pinning_required -- except
    # the forked repository, whose four inherited workflows carry thirteen floating
    # references including one into another organisation pinned to @main.
    # Aggregated per repository: thirteen findings for one repository is a wall,
    # one finding naming the count is a fact. Local calls are exempt because ./
    # resolves inside the repository itself.
    printf '%s\n' "$body" \
      | grep -oE '^[[:space:]]*(- )?uses:[[:space:]]*[^[:space:]]+' \
      | sed 's|.*uses:[[:space:]]*||' \
      | grep -v '^\./' | grep -vE '@[0-9a-f]{40}$' | grep -vE '@sha256:[0-9a-f]{64}$' >> "$WORK/floating" || true

    [ -n "$delegates" ] && continue
    case "$body" in *"attestations: write"*) ;; *) continue ;; esac
    case "$body" in *attest-build-provenance*) continue ;; esac
    echo "$repo|$wf grants attestations: write with no attest step" >> "$WORK/findings"
  done < <(grep -E '^\.github/workflows/.*\.ya?ml$' "$WORK/tree" || true)

  if [ -s "$WORK/prt" ]; then
    echo "$repo|pull_request_target in: $(tr '\n' ' ' < "$WORK/prt" | sed 's/ $//') — runs with this repository's token on a pull request from anywhere" >> "$WORK/findings"
  fi
  if [ -s "$WORK/floating" ]; then
    n_float=$(sort -u "$WORK/floating" | wc -l | tr -d ' ')
    eg=$(sort -u "$WORK/floating" | head -2 | tr '\n' ' ' | sed 's/ $//')
    echo "$repo|$n_float uses: reference(s) not pinned to a commit, e.g. $eg" >> "$WORK/findings"
  fi
  if [ -s "$WORK/idtoken" ]; then
    echo "$repo|id-token: write with nothing that mints a token, in: $(tr '\n' ' ' < "$WORK/idtoken" | sed 's/ $//')" >> "$WORK/findings"
  fi
  if [ -s "$WORK/noperm" ]; then
    echo "$repo|public repository, pull_request workflow with no permissions block: $(tr '\n' ' ' < "$WORK/noperm" | sed 's/ $//')" >> "$WORK/findings"
  fi

  # A credential no workflow on the default branch reads any more. Removing the
  # last consumer orphans a secret silently: nothing fails, and a default-branch
  # sweep then calls it safe to delete. Wildcat#1001 removed a nightly deploy job
  # in August 2026 and left a GitHub App private key in exactly that state.
  #
  # Which is why the second stage exists. That key was still referenced in
  # nightly.yml on six other branches, three of them with an open pull request,
  # so deleting it would have failed create-github-app-token at the start of
  # their jobs. So the finding says *when* it becomes safe rather than asserting
  # that it is, and an older precedent argues the same way from the other side:
  # bcr-common/add-clippy-ci referenced an organisation secret on a branch dead
  # since January, and granting the secret to satisfy it would have widened the
  # blast radius for nothing. Live branches block a deletion; dead ones must not
  # block it. Ninety days is where that line sits.
  if [ -s "$WORK/wfbody" ] && [ -n "$have_reposecrets" ]; then
    { gh api "repos/$ORG/$repo/actions/secrets?per_page=100" --paginate \
        --jq '.secrets[]?.name' 2>/dev/null
      if [ -n "$have_repovars" ]; then
        gh api "repos/$ORG/$repo/actions/variables?per_page=100" --paginate \
          --jq '.variables[]?.name' 2>/dev/null
      fi
    } | sort -u > "$WORK/creds" || : > "$WORK/creds"
    while IFS= read -r name; do
      [ -z "$name" ] && continue
      # A reference, not a mention. E-Bill-frontend/notify-wallet-repo.yml
      # sets an env key literally named GH_TOKEN, fed from secrets.GITHUB_TOKEN --
      # six bare matches for "GH_TOKEN" and none for "secrets.GH_TOKEN". A bare
      # grep therefore skipped a genuinely orphaned secret of that name. The
      # trailing class is a portable word boundary: it keeps FOO from matching
      # secrets.FOO_BAR, and BSD grep has no \b.
      grep -qE -- "(secrets|vars)\.${name}([^A-Za-z0-9_]|$)" "$WORK/wfbody" \
        && continue
      # Unreferenced on the default branch. Now the second half, and only for the
      # few names that reach it: which other branches still name it, and are any
      # of them alive.
      #
      # Every branch is scanned, not only recent ones, because a reference on an
      # old branch still has to be *found* before it can be judged. The date is
      # then fetched only for the branches that matched -- repos/{r}/branches
      # returns just {sha, url} under .commit, so the push date is not in the
      # listing and needs the per-branch endpoint. Blobs are deduplicated by SHA,
      # so a workflow unchanged across twenty branches is fetched once.
      # Any fetch that fails here makes the credential look unreferenced, and the
      # verdict below would then read "safe to delete" -- the one false positive
      # this script must never produce. At this run's request volume the
      # installation token hitting a secondary rate limit mid-sweep is realistic,
      # and a 403 is indistinguishable from an empty answer. So a failure sets
      # cred_unknown and the credential is reported as unknown instead, which is
      # the conservatism prune-package-versions.sh already applies to a tag it
      # cannot resolve. Process substitution hides the producer's exit status,
      # so each listing goes to a file whose status can be tested.
      : > "$WORK/seen"
      : > "$WORK/matched"
      cred_unknown=""
      if ! gh api "repos/$ORG/$repo/branches?per_page=100" --paginate \
             --jq '.[].name' > "$WORK/brlist" 2>/dev/null; then
        cred_unknown="the branch listing"
      else
        while IFS= read -r bname; do
          [ -z "$bname" ] && continue
          [ "$bname" = "$branch" ] && continue
          if ! gh api "repos/$ORG/$repo/git/trees/$bname?recursive=1" \
                 --jq '.tree[]? | select(.type == "blob")
                       | select(.path | test("^\\.github/workflows/.*\\.ya?ml$"))
                       | [.path, .sha] | @tsv' > "$WORK/btree" 2>/dev/null; then
            cred_unknown="the tree of branch $bname"
            break
          fi
          while IFS="$(printf '\t')" read -r bpath bsha; do
            [ -z "$bsha" ] && continue
            grep -qxF "$bsha" "$WORK/seen" && continue
            echo "$bsha" >> "$WORK/seen"
            if ! gh api "repos/$ORG/$repo/git/blobs/$bsha" --jq '.content' > "$WORK/blob.b64" 2>/dev/null; then
              cred_unknown="a blob on branch $bname"
              break
            fi
            if base64 -d < "$WORK/blob.b64" 2>/dev/null \
               | grep -qE -- "(secrets|vars)\\.${name}([^A-Za-z0-9_]|$)"; then
              grep -qxF "$bname" "$WORK/matched" || echo "$bname" >> "$WORK/matched"
            fi
          done < "$WORK/btree"
          [ -n "$cred_unknown" ] && break
        done < "$WORK/brlist"
      fi

      live=""
      dead=0
      while IFS= read -r bname; do
        [ -z "$bname" ] && continue
        if ! bdate=$(gh api "repos/$ORG/$repo/branches/$bname" \
                       --jq '.commit.commit.committer.date // .commit.commit.author.date // ""' 2>/dev/null); then
          # An unreadable push date would count the branch as dead, which pushes
          # the verdict toward deletion. Unknown instead.
          cred_unknown="the push date of branch $bname"
          break
        fi
        if [ -n "$bdate" ] && [ "$bdate" \> "$CUTOFF" ]; then
          live="$live $bname"
        else
          dead=$((dead + 1))
        fi
      done < "$WORK/matched"

      # Before telling anyone to delete a credential, look outside
      # .github/workflows on the default branch. E-Bill-frontend keeps
      # CROWDIN_PERSONAL_TOKEN in crowdin.yml, and this repository has 37
      # config files there that no workflow scan can see -- so a workflow-only
      # sweep would call a live credential orphaned. Telling someone to delete a
      # working secret is the one false positive this script must never produce,
      # so the match here is deliberately the loose bare name: over-reporting
      # "still in use" is the safe direction. Only the few names that would
      # otherwise reach a delete verdict pay for the extra tree walk.
      elsewhere=""
      if [ -z "$live" ] && [ -z "$cred_unknown" ]; then
        if ! gh api "repos/$ORG/$repo/git/trees/$branch?recursive=1" \
               --jq '.tree[]? | select(.type == "blob")
                     | select(.path | test("^\\.github/workflows/") | not)
                     | select(.path | test("\\.(ya?ml|json|sh|toml)$") or test("\\.env"))
                     | [.path, .sha] | @tsv' > "$WORK/ctree" 2>/dev/null; then
          cred_unknown="the default-branch tree"
        else
          while IFS="$(printf '\t')" read -r cpath csha; do
            [ -z "$csha" ] && continue
            if ! gh api "repos/$ORG/$repo/git/blobs/$csha" --jq '.content' > "$WORK/blob.b64" 2>/dev/null; then
              cred_unknown="a config blob on $branch"
              break
            fi
            base64 -d < "$WORK/blob.b64" 2>/dev/null | grep -qF -- "$name" \
              && elsewhere="$elsewhere $cpath"
          done < "$WORK/ctree"
        fi
      fi

      if [ -n "$cred_unknown" ]; then
        echo "$repo|$name is read by no workflow on $branch, and whether anything else reads it is UNKNOWN — could not read $cred_unknown. Not safe to delete on this run" >> "$WORK/findings"
      elif [ -n "$live" ]; then
        echo "$repo|$name is read by no workflow on $branch, but is still referenced on:$live — deletable once those merge or rebase" >> "$WORK/findings"
      elif [ -n "$elsewhere" ]; then
        echo "$repo|$name is read by no workflow, but is referenced outside .github/workflows on $branch:$elsewhere — not safe to delete" >> "$WORK/findings"
      elif [ "$dead" -gt 0 ]; then
        echo "$repo|$name is read by no workflow on $branch, and only by $dead branch(es) with no push in 90 days — safe to delete" >> "$WORK/findings"
      else
        echo "$repo|$name is read by no workflow on any branch — safe to delete" >> "$WORK/findings"
      fi
    done < "$WORK/creds"
  fi

  # A copy of an inherited file that is identical to the organisation version is a
  # file that drifts later and reports nothing when it does.
  for f in CONTRIBUTING.md CODE_OF_CONDUCT.md SECURITY.md; do
    # Not in .github itself, where the file *is* the organisation version rather
    # than a copy of it -- comparing it with itself reported all three every run.
    # Found by running the check rather than by reading it, which is the argument
    # for running it: the comparison is correct and its subject was wrong.
    [ "$repo" = ".github" ] && continue
    [ -s "$WORK/org/$f" ] || continue
    grep -qxF "$f" "$WORK/tree" || continue
    gh api "repos/$ORG/$repo/contents/$f?ref=$branch" -H "Accept: application/vnd.github.raw" \
      > "$WORK/own.tmp" 2>/dev/null || continue
    if cmp -s "$WORK/org/$f" "$WORK/own.tmp"; then
      echo "$repo|$f is byte-identical to the organisation version and could be inherited" >> "$WORK/findings"
    fi
  done

  # A repository's own .github/ISSUE_TEMPLATE/ suppresses the organisation
  # config.yml entirely, so the shared contact links vanish from the chooser
  # unless the repository carries its own copy. Read from the default branch,
  # which is the whole point: GitHub reads issue templates from there and nowhere
  # else, so a config.yml merged to dev is invisible until dev reaches master and
  # nothing anywhere says so. Measured 2026-09-02 -- two repositories with the
  # file on dev showed zero contact links, against three in a control.
  if grep -q '^\.github/ISSUE_TEMPLATE/' "$WORK/tree.all" &&
     ! grep -qxF '.github/ISSUE_TEMPLATE/config.yml' "$WORK/tree.all" &&
     [ "$(echo "$meta" | jq -r '.has_issues')" = "true" ] &&
     [ "$(echo "$meta" | jq -r '.fork')" = "false" ]; then
    echo "$repo|own .github/ISSUE_TEMPLATE/ on $branch and no config.yml — the organisation contact links are suppressed here" >> "$WORK/findings"
  fi

  # The newest tag with no GitHub release. The estate uses tags for two purposes
  # -- a train marker and a shipped release -- and nothing tells them apart.
  # Only the newest is checked: the backlog of 55 older orphans is a historical
  # fact rather than drift, and reporting it weekly would bury everything else.
  #
  # A repository that has never cut a release is skipped. It is not behind on
  # releasing; it does not release. That rule rather than a list of names is what
  # keeps AI-Credit (its own tobo-ai-credit-testnet-N scheme) and the crowdin-sdk
  # fork (21 upstream tags) out, and it lets either in on the day it cuts a first
  # release, with no edit here. Owner decision 2026-09-02.
  newest_tag=$(gh api "repos/$ORG/$repo/tags?per_page=1" --jq '.[0].name // empty' 2>/dev/null || true)
  if [ -n "$newest_tag" ]; then
    n_rel=$(gh api "repos/$ORG/$repo/releases?per_page=1" --jq 'length' 2>/dev/null || echo 0)
    case "$n_rel" in ''|*[!0-9]*) n_rel=0 ;; esac
    if [ "$n_rel" != "0" ]; then
      gh api "repos/$ORG/$repo/releases/tags/$newest_tag" >/dev/null 2>&1 ||
        echo "$repo|newest tag \`$newest_tag\` has no GitHub release" >> "$WORK/findings"
    fi
  fi

  # A repository publishing a public Pages site. Not a defect by itself -- one
  # repository is a documentation site and is meant to -- but an internal
  # repository serving a public site is an exposure nobody chose on purpose, so
  # the visibility of the repository is reported beside the URL.
  if [ -n "$have_pages" ]; then
    pg=$(gh api "repos/$ORG/$repo/pages" 2>/dev/null || true)
    pg_url=$(echo "$pg" | jq -r '.html_url // empty' 2>/dev/null || true)
    pg_public=$(echo "$pg" | jq -r '.public // false' 2>/dev/null || echo false)
    if [ -n "$pg_url" ] && [ "$pg_public" = "true" ]; then
      pg_src=$(echo "$pg" | jq -r '.source.branch // "?"' 2>/dev/null || echo "?")
      vis=$(echo "$meta" | jq -r '.visibility')
      echo "$repo|$vis repository publishes a public Pages site at $pg_url (source: $pg_src)" >> "$WORK/findings"
    fi
  fi

  # The has_wiki flag is readable at every visibility; wiki *content* is not.
  # Counted here as a flag, which is a fact, and left as a metric rather than a
  # finding by owner decision 2026-09-02 -- three wiki questions are still open,
  # and three findings a week ahead of the answer is noise.
  if [ "$(echo "$meta" | jq -r '.has_wiki')" = "true" ]; then
    n_wiki=$((n_wiki + 1))
    echo "$repo ($(echo "$meta" | jq -r '.visibility'))" >> "$WORK/wikirepos"
  fi

  # Only public repositories: an App installation token cannot read a wiki, so
  # for an internal or private one "no content" and "no access" look identical.
  # Checking those reported the two repositories that actually use their wiki as
  # empty, which is worse than not checking at all.
  if [ "$(echo "$meta" | jq -r '.has_wiki')" = "true" ] &&
     [ "$(echo "$meta" | jq -r '.visibility')" = "public" ]; then
    git ls-remote "https://github.com/$ORG/$repo.wiki.git" >/dev/null 2>&1 ||
      echo "$repo|wiki enabled but empty" >> "$WORK/findings"
  fi

  attached=$(gh api "repos/$ORG/$repo/code-security-configuration" --jq '.configuration.name // "none"' 2>/dev/null || echo none)
  [ "$attached" = "$BASELINE_CONFIG" ] ||
    echo "$repo|security configuration is '$attached', expected '$BASELINE_CONFIG'" >> "$WORK/findings"

  # A repository secret whose name also exists at organisation level. Which value
  # a workflow gets is then not obvious from reading the workflow, and the answer
  # is the repository one. bcr-relay has its own GCP_SERVICE_ACCOUNT_KEY, last
  # rotated 2025-09-11, in a repository whose last functional commit was March --
  # so an eleven-month-old cloud credential sits somewhere nobody is looking, and
  # narrowing the organisation secret did not touch it.
  : > "$WORK/reposecrets"
  shadow=""
  if [ -n "$have_reposecrets" ] && [ -n "$have_orgsecrets" ]; then
    gh api "repos/$ORG/$repo/actions/secrets?per_page=100" --paginate --jq '.secrets[]?.name' 2>/dev/null \
      | sort > "$WORK/reposecrets" || : > "$WORK/reposecrets"
    shadow=$(comm -12 "$WORK/reposecrets" "$WORK/orgsecrets" | tr '\n' ' ' | sed 's/ $//')
  fi
  [ -n "$shadow" ] &&
    echo "$repo|repository secret shadows an organisation secret of the same name: $shadow" >> "$WORK/findings"

  # A workflow naming an organisation secret this repository was never granted.
  # The repository's own secrets are subtracted first: bcr-relay references
  # GCP_SERVICE_ACCOUNT_KEY and holds its own, so the reference resolves and the
  # naive form of this check reports a repository that is working correctly. The
  # shadowing check above is the honest way to surface that same fact.
  # Gated on have_orgsecrets like the shadowing check above: a partial grants
  # file makes granted secrets look ungranted, which fault injection caught
  # after the sibling check had already been gated and this one had not.
  notgranted=""
  while [ -n "$have_orgsecrets" ] && IFS= read -r s; do
    [ -z "$s" ] && continue
    grep -qE "secrets\.$s([^A-Za-z0-9_]|\$)" "$WORK/wfbody" || continue
    grep -qxF "$s" "$WORK/reposecrets" && continue
    grep -qxF "$s $repo" "$WORK/orggrants" && continue
    notgranted="$notgranted $s"
  done < "$WORK/orgsecrets"
  [ -n "$notgranted" ] &&
    echo "$repo|references organisation secret(s) it was not granted:$notgranted" >> "$WORK/findings"

  # An environment holding secrets with no protection rule at all. Two are
  # accepted and named here rather than skipped silently: Wildcat-deployment's
  # wildcat-dev holds a test seed and stays open so dev iteration is fast, and
  # wallet's dev cannot take a branch policy because 292 of 343 dispatches of
  # build-dev.yml come from feature branches -- restricting it would break the
  # process rather than close a hole. Delete a line here and the check reports it
  # again, which is the point of a list over a silent skip.
  [ -n "$have_envs" ] &&
  gh api "repos/$ORG/$repo/environments?per_page=100" --jq '.environments[]?|select((.protection_rules|length)==0)|.name' 2>/dev/null \
  | while IFS= read -r env; do
      [ -z "$env" ] && continue
      case "$repo/$env" in
        Wildcat-deployment/wildcat-dev) continue ;;
        wallet/dev)                     continue ;;
      esac
      # Not `|| echo 0`: gh writes the error body to stdout and then exits, so the
      # fallback appends to it rather than replacing it and ns became the whole
      # 403 JSON -- which is not "0", so every environment was reported as holding
      # secrets. Unreadable is skipped and named once, never counted as a number.
      if ! ns=$(gh api "repos/$ORG/$repo/environments/$env/secrets" --jq '.total_count' 2>/dev/null); then
        [ -n "$envsec_gap" ] || gap "environment secret counts — needs \`Secrets: read\`; the unprotected-environment check is skipped"
        envsec_gap=1
        continue
      fi
      case "$ns" in ''|*[!0-9]*) ns=0 ;; esac
      # if, not [ ] && -- this while is the right-hand side of a pipeline, so its
      # body's last command is the pipeline's exit status, and a false AND-list
      # there aborts the whole script under set -e. Demonstrated, not assumed:
      # `printf 1 | while read x; do [ "$x" -gt 5 ] && echo big; done; echo alive`
      # never reaches the echo, while the same loop fed by a redirect does.
      if [ "$ns" != "0" ]; then
        echo "$repo|environment '$env' holds $ns secret(s) and has no protection rule" >> "$WORK/findings"
      fi
    done

  # A required reviewer who is not an organisation member. An approval request
  # cannot reach them, so the gate has fewer approvers than it appears to --
  # bcr-relay/prod listed four and had three until tompro was removed.
  [ -n "$have_envs" ] && [ -n "$have_members" ] &&
  gh api "repos/$ORG/$repo/environments?per_page=100" --jq \
    '.environments[]?|.name as $e|.protection_rules[]?|select(.type=="required_reviewers")|.reviewers[]?|"\($e)|\(.reviewer.login // .reviewer.name // "?")"' 2>/dev/null \
  | while IFS='|' read -r env who; do
      [ -z "$who" ] && continue
      grep -qxF "$who" "$WORK/members" ||
        echo "$repo|environment '$env' lists '$who' as a reviewer, who is not an organisation member" >> "$WORK/findings"
    done

  # Agent-instruction files outside the enterprise ruleset's two globs, counted
  # rather than reported. The ruleset restricts .github/agents/*.md and
  # agents/*.md, neither of which exists anywhere, while seventeen files that do
  # shape agent behaviour -- CLAUDE.md, AGENTS.md, .claude/ -- are uncovered.
  # Owner decision 2026-08-18: record, do not widen, because file_path_restriction
  # on a push target would put an enterprise owner in the way of routine .claude/
  # edits in bit.cr.
  c=$(grep -cE '(^|/)(CLAUDE|AGENTS)\.md$|(^|/)\.claude/|(^|/)\.github/copilot-instructions\.md$' "$WORK/tree" || true)
  if [ "$c" -gt 0 ]; then
    n_agentfiles=$((n_agentfiles + c))
    n_agentrepos=$((n_agentrepos + 1))
  fi
done < "$WORK/repos"

# An open pull request whose head is behind its base. A green tick on a stale
# branch is a lie: it says the code passed against a base that has since moved.
#
# Counted rather than reported, and split three ways, because the raw number is
# 57 and 39 of those are Dependabot's -- a Dependabot branch self-heals the
# moment it is recreated from a pinned default branch, so reporting it every
# Monday is noise. What is left is a dozen branches other teams are actively
# working on, and the only remedy is update-branch on somebody else's branch,
# which is not ours to press: tip-commit authorship is not ownership.
#
# Two are excluded by an explicit owner decision of 2026-08-25 -- bit.cr#1 and
# bitcr.org#1 are not to be touched at all, so their drift is the expected state
# rather than a finding, and keeping a branch level with base is a write to it.
: > "$WORK/behind"
while IFS= read -r r; do
  [ -z "$r" ] && continue
  gh api "repos/$ORG/$r/pulls?state=open&per_page=100" --paginate \
    --jq '.[]|"\(.number)|\(.base.ref)|\(.head.ref)|\(.head.repo.full_name // "")|\(.user.login)|\(.updated_at)|\(.draft)"' 2>/dev/null \
  | while IFS='|' read -r num base head hrepo who upd draft; do
      [ -z "$num" ] && continue
      [ "$hrepo" = "$ORG/$r" ] || continue          # a fork's branch is not ours to update
      case "$r#$num" in "bit.cr#1"|"bitcr.org#1") continue ;; esac
      n=$(gh api "repos/$ORG/$r/compare/$base...$head" --jq '.behind_by' 2>/dev/null || echo 0)
      case "$n" in ''|*[!0-9]*) n=0 ;; esac         # a null behind_by is not an integer
      if [ "$n" -gt 0 ]; then                       # if, not && -- see the note above
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$r" "$num" "$n" "$who" "$upd" "$draft" >> "$WORK/behind"
      fi
    done
done < "$WORK/repos"
n_behind=$(awk 'END{print NR}' "$WORK/behind")
n_behind_bot=$(awk -F'\t' '$4 ~ /dependabot/ {c++} END{print c+0}' "$WORK/behind")
n_behind_live=$(awk -F'\t' -v c="$CUTOFF30" \
  '$4 !~ /dependabot/ && $6 != "true" && $5 > c {n++} END{print n+0}' "$WORK/behind")

# The other direction: an entry left behind by a rename, an archive or a
# deletion. A mapping that quietly points at nothing stops being trusted, and an
# untrusted mapping is worse than no mapping at all.
jq -r '.assignees | keys[]' "$WORK/assignees.json" | while IFS= read -r listed; do
  if ! grep -qxF "$listed" "$WORK/repos"; then
    echo "$listed|listed in dependabot-assignees.yml but not an active repository" >> "$WORK/findings"
  fi
done

# An incomplete release train. Five repositories are tagged with one shared name
# on one day, and the defect is the silent partial: Wildcat-deployment missed
# june17, july31 and aug4 and nothing noticed for three months.
#
# Only the train/ namespace is checked. The nine historical trains predate the
# convention and are not migrated by contract, so matching them here would be
# nine permanent findings for a decision already taken. Zero train/* tags exist
# as of 2026-09-02, which means this is silent until the first train is cut --
# by design, not by accident.
#
# The finding is attributed to each repository that is missing the tag, not to
# the train, because that is the repository somebody has to act on.
#
# This list exists twice: here, and as MEMBERS in scripts/release-train.py, which
# is what actually cuts the tags. They must agree -- a member the train tags and
# the audit does not know about can never be reported as missing, and one the
# audit knows about and the train does not would be reported missing every week
# forever. Change both, or neither. The contract is .github/RELEASING.md.
TRAIN_MEMBERS="Wildcat Clowder Wildcat-Auxiliary Wildcat-deployment wildcat-dashboard-ui"
n_train_members=$(echo "$TRAIN_MEMBERS" | wc -w | tr -d ' ')

# ...and the agreement is checked rather than trusted. A member the train tags
# and this list omits can never be reported missing; one this list carries and
# the train does not would be reported missing every week forever. Both failures
# are silent, which is why the comparison is a finding. Skipped while
# release-train.py does not exist -- it arrives with .github#39.
rt_script="$(dirname "$0")/release-train.py"
if [ -f "$rt_script" ]; then
  # awk rather than a sed range, because a sed range cannot close on the line it
  # opened on: the moment somebody reformats MEMBERS onto one line -- and the list
  # is short enough that they will -- the range runs on to the next ] in the file
  # and sweeps in GATE_EXCLUDE and BLOCKING. Caught by a control that reformatted
  # it, which is the only reason it is not a weekly false finding.
  rt_members=$(awk '/^MEMBERS *= *\[/ { inside = 1 } inside { print; if (/\]/) exit }' "$rt_script" \
               | grep -o '"[^"]*"' | tr -d '"' | sort | tr '\n' ' ')
  au_members=$(echo "$TRAIN_MEMBERS" | tr ' ' '\n' | sort | tr '\n' ' ')
  [ "$rt_members" = "$au_members" ] ||
    echo ".github|release-train.py and this script disagree about train membership — train: [$rt_members] audit: [$au_members]" >> "$WORK/findings"
fi

: > "$WORK/trainrefs"
for m in $TRAIN_MEMBERS; do
  gh api "repos/$ORG/$m/git/matching-refs/tags/train/" \
    --jq '.[].ref | sub("^refs/tags/"; "")' 2>/dev/null > "$WORK/trainone" || : > "$WORK/trainone"
  while IFS= read -r t; do
    [ -z "$t" ] && continue
    echo "$t|$m" >> "$WORK/trainrefs"
  done < "$WORK/trainone"
done
cut -d'|' -f1 "$WORK/trainrefs" | sort -u > "$WORK/trainnames"
while IFS= read -r t; do
  [ -z "$t" ] && continue
  awk -F'|' -v t="$t" '$1 == t {print $2}' "$WORK/trainrefs" | sort -u > "$WORK/trainhave"
  n_have=$(wc -l < "$WORK/trainhave" | tr -d ' ')
  if [ "$n_have" != "$n_train_members" ]; then
    for m in $TRAIN_MEMBERS; do
      grep -qxF "$m" "$WORK/trainhave" ||
        echo "$m|release train \`$t\` was cut in $n_have of $n_train_members repositories but not here" >> "$WORK/findings"
    done
  fi
done < "$WORK/trainnames"

# Archived repositories are outside the audit loop by design: nothing about them
# can drift, because nothing about them can change. Their credentials can.
# Archiving revokes nothing -- a token in an archived repository is still valid,
# and it belongs to no CI that anyone watches. Guarded by the same probe as the
# other secret checks, so it declares itself skipped rather than reporting zero.
if [ -n "$have_reposecrets" ]; then
  jq -r 'add | .[] | select(.archived == true) | .name' "$WORK/repos.json" > "$WORK/archived"
  while IFS= read -r arch; do
    [ -z "$arch" ] && continue
    ns=$(gh api "repos/$ORG/$arch/actions/secrets" --jq '.total_count' 2>/dev/null || echo 0)
    case "$ns" in ''|*[!0-9]*) ns=0 ;; esac
    if [ "$ns" != "0" ]; then
      echo "$arch|archived, but still holds $ns repository secret(s) — archiving does not revoke a credential" >> "$WORK/findings"
    fi
  done < "$WORK/archived"
else
  gap "archived repositories' secrets — needs \`Secrets: read\`; a credential left in an archived repository is unchecked"
fi

# Dependabot backlog. Not drift, so it is reported rather than counted as a
# finding — alerts come and go with upstream advisories and would keep the issue
# open forever. Needs the App's "Dependabot alerts: read"; if that is ever
# withdrawn the section says so instead of failing the whole audit.
if gh api "orgs/$ORG/dependabot/alerts?state=open&per_page=100" --paginate --slurp > "$WORK/alerts.json" 2>/dev/null \
   && jq -e 'type == "array"' "$WORK/alerts.json" >/dev/null 2>&1; then
  jq -r '[.[][]] as $a
    | if ($a|length) == 0 then "No open Dependabot alerts."
      else
        "- open alerts: **\($a|length)** — "
        + ([$a[].security_advisory.severity] | group_by(.)
           | sort_by(-length) | map("\(.[0]) \(length)") | join(" · "))
        + "\n- with a published fix: **\([$a[] | select(.security_vulnerability.first_patched_version != null)] | length)**"
        + " — merging Dependabot'"'"'s pull requests clears those\n\n"
        + "| repository | alerts |\n| --- | --- |\n"
        + ([$a[].repository.name] | group_by(.) | sort_by(-length)
           | map("| `\(.[0])` | \(length) |") | join("\n"))
      end' "$WORK/alerts.json" > "$WORK/dependabot.md"
else
  echo "Could not read Dependabot alerts — the App is missing \`Dependabot alerts: read\`." > "$WORK/dependabot.md"
fi

# Secret surface. Counts, not findings: this is a standing property of how the
# estate is configured rather than something that drifts week to week, so it
# belongs beside the Dependabot backlog and not in the drift table.
#
# The distinction the report exists to make visible: environment protection gates
# only *environment-level* secrets. A repository-level secret is readable by every
# workflow in the repository, on any branch, whatever environment a job declares
# or omits -- no reviewer rule can gate it. Twelve environments in
# Wildcat-deployment were gated in August 2026 and that closed the mint seed,
# which is environment-level. It did nothing for the repository-level secrets
# elsewhere, and nothing here counted them, so the audit read as clean while most
# of the estate's secrets sat behind no approval at all.
gated=0
ungated_env=0
repo_level=0
have_surface=""
: > "$WORK/secsurface"
if [ -n "$have_reposecrets" ] && [ -n "$have_envs" ]; then have_surface=1; fi
while [ -n "$have_surface" ] && read -r repo; do
  n=$(gh api "repos/$ORG/$repo/actions/secrets" --jq '.total_count' 2>/dev/null || echo 0)
  n=${n:-0}
  repo_level=$((repo_level + n))
  envs=$(gh api "repos/$ORG/$repo/environments?per_page=100" 2>/dev/null || echo '{}')
  g=0
  u=0
  # protection_rules is empty for an unprotected environment, so its length is the
  # whole test -- and a count of zero secrets means the environment contributes
  # nothing either way.
  while IFS=$'\t' read -r ename prot; do
    [ -z "$ename" ] && continue
    es=$(gh api "repos/$ORG/$repo/environments/$ename/secrets" --jq '.total_count' 2>/dev/null || echo 0)
    es=${es:-0}
    [ "$es" -eq 0 ] && continue
    if [ "$prot" = "0" ]; then u=$((u + es)); else g=$((g + es)); fi
  done < <(printf '%s' "$envs" | jq -r '.environments[]? | [.name, ((.protection_rules//[])|length)] | @tsv')
  gated=$((gated + g))
  ungated_env=$((ungated_env + u))
  if [ $((n + g + u)) -gt 0 ]; then
    printf '%s\t%s\t%s\t%s\n' "$repo" "$n" "$g" "$u" >> "$WORK/secsurface"
  fi
done < "$WORK/repos"

{
  if [ -s "$WORK/gaps" ]; then
    echo "### Not measured on this run"
    echo
    echo "Each of these is a read that failed. They are listed rather than counted"
    echo "as zero: an unreadable answer and an empty one are indistinguishable, and"
    echo "reporting the second when it was the first is a false all-clear."
    echo
    sort -u "$WORK/gaps" | sed 's/^/- could not read /'
    echo
  fi
  echo "- repositories audited: **$total**"
  echo "- topics corrected: **$n_topics**"
  echo "- merge settings corrected: **$n_merge**"
  [ "$DRY_RUN" = "true" ] && echo "- dry run: nothing was written"
  echo
  if [ -s "$WORK/findings" ]; then
    echo "| repository | finding |"
    echo "| --- | --- |"
    sort "$WORK/findings" | awk -F'|' '{printf "| `%s` | %s |\n", $1, $2}'
  else
    echo "No drift found."
  fi
  echo
  echo "### Dependabot"
  echo
  cat "$WORK/dependabot.md"
  echo
  echo "### Coverage that is measured rather than reported"
  echo
  n_untimed_repos=$(sort -u "$WORK/untimedrepos" | awk 'END{print NR}')
  echo "- jobs with no \`timeout-minutes\`: **$n_untimed** across **$n_untimed_repos** repositories — a job with none runs to GitHub's six-hour default. Not a finding: fifty lines would bury everything else, and the remainder after the open pull requests merge is deferred for stated reasons — a dormant repository, dispatch-only workflows, one job behind \`if: false\`. Caller jobs are excluded because they cannot take the key at all."
  echo "- open pull requests behind their base: **$n_behind** — of which **$n_behind_bot** are Dependabot's, which self-heal when the branch is recreated from a pinned default, leaving **$n_behind_live** that are not drafts and were touched in the last 30 days. Not a finding: the remedy is \`update-branch\` on another team's branch, and tip-commit authorship is not ownership. \`bit.cr#1\` and \`bitcr.org#1\` are excluded by the owner's decision of 2026-08-25."
  if [ "$n_wiki" -gt 0 ]; then
    echo "- repositories with the wiki enabled: **$n_wiki** — $(sort "$WORK/wikirepos" | tr '\n' ',' | sed 's/,$//; s/,/, /g'). A wiki is a separate git repository, invisible to code search, to the contents API and to every ruleset, which targets \`branch\` and \`tag\` only. Not a finding: whether these should exist is an open question, and an App token cannot read an internal or private wiki, so emptiness is only establishable for the public one."
  fi
  echo "- agent-instruction files outside the enterprise ruleset's globs: **$n_agentfiles** across **$n_agentrepos** repositories — the ruleset restricts \`.github/agents/*.md\` and \`agents/*.md\`, and neither directory exists anywhere. Owner decision 2026-08-18: record, do not widen."
  echo

  echo "### Secrets"
  echo
  if [ -z "$have_surface" ]; then
    echo "Not measured on this run — see above. The figures below would read as"
    echo "zero rather than as unknown, so they are omitted instead."
  else
    echo "- behind an approval gate: **$gated** — environment-level, inside an environment that has a protection rule"
    echo "- behind no gate: **$((repo_level + ungated_env))** — $repo_level repository-level, which no environment rule can gate, plus $ungated_env environment-level in an unprotected environment"
    echo "- organisation-level: **$(gh api "orgs/$ORG/actions/secrets" --jq '.total_count' 2>/dev/null || echo '?')** — granted per repository, and likewise not gated by any environment"
    echo
    if [ -s "$WORK/secsurface" ]; then
      echo "| repository | ungated | repository-level | environment, gated | environment, ungated |"
      echo "| --- | --- | --- | --- | --- |"
      awk -F'\t' '{print ($2+$4) "\t" $0}' "$WORK/secsurface" | sort -rn \
        | cut -f2- | awk -F'\t' '{printf "| `%s` | %s | %s | %s | %s |\n", $1, $2+$4, $2, $3, $4}'
    fi
  fi
  echo
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    echo "[Run](https://github.com/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID) · reruns every Monday."
  fi
} > "$WORK/report.md"

{ echo "## Repository settings audit"; echo; cat "$WORK/report.md"; } >> "$SUMMARY"

# A summary buried in a workflow log is a report nobody reads. Mirror it into a
# single issue that is updated in place, and closed once the drift is gone —
# opening a fresh one every Monday would just be noise.
[ "$DRY_RUN" = "true" ] && exit 0

REPORT_REPO="${GITHUB_REPOSITORY:-$ORG/.github}"
TITLE="Repository settings drift"

# Matched by listing open issues rather than through the search API, whose
# index lags behind writes by minutes and would open duplicates.
existing=$(gh api "repos/$REPORT_REPO/issues?state=open&per_page=100" --paginate --slurp \
  | jq -r --arg t "$TITLE" 'add | map(select(.pull_request == null and .title == $t)) | .[0].number // empty')

if [ -s "$WORK/findings" ]; then
  count=$(wc -l < "$WORK/findings" | tr -d ' ')
  { echo "$count findings across $total repositories."; echo; cat "$WORK/report.md"; } > "$WORK/issue.md"
  if [ -n "$existing" ]; then
    jq -Rs '{body: .}' < "$WORK/issue.md" \
      | gh api -X PATCH "repos/$REPORT_REPO/issues/$existing" --input - >/dev/null
    echo "Updated issue #$existing ($count findings)"
  else
    n=$(jq -Rs --arg t "$TITLE" '{title: $t, body: .}' < "$WORK/issue.md" \
      | gh api -X POST "repos/$REPORT_REPO/issues" --input - --jq '.number')
    echo "Opened issue #$n ($count findings)"
  fi
elif [ -n "$existing" ]; then
  gh api -X POST "repos/$REPORT_REPO/issues/$existing/comments" \
    -f "body=No drift found across $total repositories. Closing; it will reopen if anything drifts again." >/dev/null
  gh api -X PATCH "repos/$REPORT_REPO/issues/$existing" -f state=closed >/dev/null
  echo "Closed issue #$existing — no findings"
fi
