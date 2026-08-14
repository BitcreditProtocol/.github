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

gh api "orgs/$ORG/repos?per_page=100" --paginate --slurp > "$WORK/repos.json"
jq -r 'add | .[] | select(.archived == false) | .name' "$WORK/repos.json" > "$WORK/repos"
total=$(wc -l < "$WORK/repos" | tr -d ' ')
echo "Auditing $total non-archived repositories (dry_run=$DRY_RUN)"

n_topics=0; n_merge=0
: > "$WORK/findings"

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

  gh api "repos/$ORG/$repo/contents/.github/dependabot.yml" >/dev/null 2>&1 ||
    echo "$repo|no .github/dependabot.yml" >> "$WORK/findings"

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
