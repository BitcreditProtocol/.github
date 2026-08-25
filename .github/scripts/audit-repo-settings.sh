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
  [ "$(echo "$meta" | jq -r '.description // ""')" = "" ] &&
    echo "$repo|no description" >> "$WORK/findings"

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
    if [ -z "$want" ]; then
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
      if [ -n "$wrong" ]; then
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
      if [ -n "$uncovered" ]; then
        echo "$repo|dependabot.yml does not cover:$uncovered (has: $configured)" >> "$WORK/findings"
      fi

      # Dependabot applies only labels that already exist and silently drops the
      # rest. Fifteen of twenty-two configured repositories named a label they did
      # not have -- 31 pairs -- so pull requests arrived carrying fewer labels than
      # the file asked for, and seven arrived with none at all.
      gh api "repos/$ORG/$repo/labels?per_page=100" --paginate --jq '.[].name' > "$WORK/labels" 2>/dev/null || : > "$WORK/labels"
      missing_labels=""
      for l in $(jq -r '[.updates[]?.labels[]?] | unique | .[]' "$WORK/db.json"); do
        grep -Fxq "$l" "$WORK/labels" || missing_labels="$missing_labels $l"
      done
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
  while IFS= read -r wf; do
    [ -z "$wf" ] && continue
    body=$(gh api "repos/$ORG/$repo/contents/$wf?ref=$branch" -H "Accept: application/vnd.github.raw" 2>/dev/null)
    [ -z "$body" ] && continue
    case "$body" in *"attestations: write"*) ;; *) continue ;; esac
    case "$body" in *attest-build-provenance*) continue ;; esac
    echo "$repo|$wf grants attestations: write with no attest step" >> "$WORK/findings"
  done < <(grep -E '^\.github/workflows/.*\.ya?ml$' "$WORK/tree" || true)

  # A copy of an inherited file that is identical to the organisation version is a
  # file that drifts later and reports nothing when it does.
  for f in CONTRIBUTING.md CODE_OF_CONDUCT.md SECURITY.md; do
    [ -s "$WORK/org/$f" ] || continue
    grep -qxF "$f" "$WORK/tree" || continue
    gh api "repos/$ORG/$repo/contents/$f?ref=$branch" -H "Accept: application/vnd.github.raw" \
      > "$WORK/own.tmp" 2>/dev/null || continue
    if cmp -s "$WORK/org/$f" "$WORK/own.tmp"; then
      echo "$repo|$f is byte-identical to the organisation version and could be inherited" >> "$WORK/findings"
    fi
  done

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
done < "$WORK/repos"

# The other direction: an entry left behind by a rename, an archive or a
# deletion. A mapping that quietly points at nothing stops being trusted, and an
# untrusted mapping is worse than no mapping at all.
jq -r '.assignees | keys[]' "$WORK/assignees.json" | while IFS= read -r listed; do
  if ! grep -qxF "$listed" "$WORK/repos"; then
    echo "$listed|listed in dependabot-assignees.yml but not an active repository" >> "$WORK/findings"
  fi
done

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

{
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
