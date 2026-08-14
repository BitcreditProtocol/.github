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
#      DRY_RUN (default false)

set -eu

: "${ORG:?ORG is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${BASELINE_CONFIG:?BASELINE_CONFIG is required}"
DRY_RUN="${DRY_RUN:-false}"

REQUIRED_TOPICS="bitcoin bitcredit"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

gh api "orgs/$ORG/repos?per_page=100" --paginate --slurp > "$WORK/repos.json"
jq -r 'add | .[] | select(.archived == false) | .name' "$WORK/repos.json" > "$WORK/repos"
total=$(wc -l < "$WORK/repos" | tr -d ' ')
echo "Auditing $total non-archived repositories (dry_run=$DRY_RUN)"

n_topics=0
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

  # --- report only
  [ "$(echo "$meta" | jq -r '.description // ""')" = "" ] &&
    echo "$repo|no description" >> "$WORK/findings"

  gh api "repos/$ORG/$repo/license" >/dev/null 2>&1 ||
    echo "$repo|no LICENSE ($(echo "$meta" | jq -r .visibility))" >> "$WORK/findings"

  gh api "repos/$ORG/$repo/contents/.github/dependabot.yml" >/dev/null 2>&1 ||
    echo "$repo|no .github/dependabot.yml" >> "$WORK/findings"

  if [ "$(echo "$meta" | jq -r '.has_wiki')" = "true" ]; then
    git ls-remote "https://x-access-token:$GH_TOKEN@github.com/$ORG/$repo.wiki.git" >/dev/null 2>&1 ||
      echo "$repo|wiki enabled but empty" >> "$WORK/findings"
  fi

  attached=$(gh api "repos/$ORG/$repo/code-security-configuration" --jq '.configuration.name // "none"' 2>/dev/null || echo none)
  [ "$attached" = "$BASELINE_CONFIG" ] ||
    echo "$repo|security configuration is '$attached', expected '$BASELINE_CONFIG'" >> "$WORK/findings"
done < "$WORK/repos"

{
  echo "## Repository settings audit"
  echo
  echo "- repositories audited: **$total**"
  echo "- topics corrected: **$n_topics**"
  [ "$DRY_RUN" = "true" ] && echo "- dry run: nothing was written"
  echo
  if [ -s "$WORK/findings" ]; then
    echo "### Needs a human"
    echo
    echo "| repository | finding |"
    echo "| --- | --- |"
    sort "$WORK/findings" | awk -F'|' '{printf "| `%s` | %s |\n", $1, $2}'
  else
    echo "No drift found."
  fi
} >> "$SUMMARY"
