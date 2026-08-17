#!/usr/bin/env bash
#
# Reconcile repository labels against labels.yml.
#
# Repositories are enumerated from the API on every run, so a newly created
# repository is covered by the next run and an archived one drops out without
# anyone editing this file.
#
# Labels are never deleted: removing a label strips it from every issue and
# pull request that carries it. Anything present in a repository but missing
# from the manifest is reported instead.
#
# Env: ORG (required), GH_TOKEN (required), DRY_RUN (default false),
#      MANIFEST_JSON (optional, skips the yaml conversion — used when testing)

set -eu

: "${ORG:?ORG is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
DRY_RUN="${DRY_RUN:-false}"

# Sentinel for "manifest entry has no description key", which means leave the
# repository's own description alone. A plain empty string cannot be used —
# that is a legitimate value meaning "no description".
OMIT=$'\x01omit\x01'

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ -n "${MANIFEST_JSON:-}" ]; then
  cp "$MANIFEST_JSON" "$WORK/manifest.json"
else
  yq -o=json '.' "$(dirname "$0")/../../labels.yml" > "$WORK/manifest.json"
fi

SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

# A colour written without quotes can be swallowed by the YAML parser as a
# number — `008672` comes back as `8672.0`, which is not a colour and would be
# pushed to every repository. Fail loudly rather than corrupt anything.
bad=$(jq -r '
  [(.required // [])[], (.managed // [])[]]
  | .[]
  | select((.color | type) != "string" or (.color | test("^[0-9a-fA-F]{6}$") | not))
  | "  " + .name + " = " + (.color | tostring)' "$WORK/manifest.json")
if [ -n "$bad" ]; then
  echo "labels.yml: colour values must be quoted six-digit hex strings." >&2
  echo "$bad" >&2
  exit 1
fi

jq -r '.renames  // {} | to_entries[] | @base64' "$WORK/manifest.json" > "$WORK/renames"
jq -r '.required // [] | .[] | @base64'          "$WORK/manifest.json" > "$WORK/required"
jq -r '.managed  // [] | .[] | @base64'          "$WORK/manifest.json" > "$WORK/managed"
jq -r '[(.required // [])[].name, (.managed // [])[].name] | .[]' "$WORK/manifest.json" > "$WORK/known"

d()     { echo "$1" | base64 --decode; }
enc()   { printf '%s' "$1" | jq -sRr @uri; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
apply() { [ "$DRY_RUN" = "true" ] || gh api "$@" >/dev/null; }

gh api "orgs/$ORG/repos?per_page=100" --paginate --slurp > "$WORK/repos.json"
jq -r 'add | .[] | select(.archived == false) | .name' "$WORK/repos.json" > "$WORK/repos"

total=$(wc -l < "$WORK/repos" | tr -d ' ')
echo "Reconciling $total non-archived repositories (dry_run=$DRY_RUN)"

n_rename=0; n_create=0; n_fix=0
: > "$WORK/extras"

while IFS= read -r repo; do
  [ -z "$repo" ] && continue
  gh api "repos/$ORG/$repo/labels?per_page=100" --jq '.' > "$WORK/current"

  # Labels this repository's own dependabot.yml asks for. Dependabot applies only
  # labels that already exist and silently drops the rest, so a configuration
  # naming a label the repository lacks produces unlabelled pull requests. That is
  # how seven repositories ended up with Dependabot pull requests carrying no
  # label at all while their configuration asked for two.
  #
  # Read per repository from the API rather than from a list, so a new repository
  # arriving with a cargo block gets `rust` on the next run and a Flutter one
  # still never does.
  : > "$WORK/dblabels"
  gh api "repos/$ORG/$repo/contents/.github/dependabot.yml" --jq '.content' 2>/dev/null \
    | base64 -d > "$WORK/db.yml" 2>/dev/null || : > "$WORK/db.yml"
  if [ -s "$WORK/db.yml" ] && yq -o=json '.' "$WORK/db.yml" > "$WORK/db.json" 2>/dev/null; then
    jq -r '[.updates[]? | .labels[]?] | unique | .[]' "$WORK/db.json" > "$WORK/dblabels"
  fi

  # 1. renames, only when the old label exists and the new one does not
  while IFS= read -r row; do
    [ -z "$row" ] && continue
    old=$(d "$row" | jq -r '.key')
    new=$(d "$row" | jq -r '.value')
    has_old=$(jq --arg n "$old" 'any(.[]; .name == $n)' "$WORK/current")
    has_new=$(jq --arg n "$new" 'any(.[]; .name == $n)' "$WORK/current")
    if [ "$has_old" = "true" ] && [ "$has_new" = "false" ]; then
      echo "  $repo: rename '$old' -> '$new'"
      apply -X PATCH "repos/$ORG/$repo/labels/$(enc "$old")" -f "new_name=$new"
      n_rename=$((n_rename + 1))
      [ "$DRY_RUN" = "true" ] || gh api "repos/$ORG/$repo/labels?per_page=100" --jq '.' > "$WORK/current"
    fi
  done < "$WORK/renames"

  # 2. required: create when missing, correct when drifted
  # 3. managed:  correct when present; created only where dependabot.yml asks
  for tier in required managed; do
    while IFS= read -r row; do
      [ -z "$row" ] && continue
      name=$(d "$row" | jq -r '.name')
      color=$(d "$row" | jq -r '.color')
      desc=$(d "$row" | jq -r --arg o "$OMIT" 'if has("description") then .description else $o end')
      existing=$(jq -c --arg n "$name" '.[] | select(.name == $n)' "$WORK/current")

      set -- -f "color=$color"
      [ "$desc" = "$OMIT" ] || set -- "$@" -f "description=$desc"

      if [ -z "$existing" ]; then
        if [ "$tier" = "managed" ] && ! grep -Fxq "$name" "$WORK/dblabels"; then
          continue
        fi
        echo "  $repo: create '$name'"
        apply -X POST "repos/$ORG/$repo/labels" -f "name=$name" "$@"
        n_create=$((n_create + 1))
        continue
      fi

      cur_color=$(echo "$existing" | jq -r '.color')
      cur_desc=$(echo "$existing" | jq -r '.description // ""')
      changes=""
      [ "$(lower "$cur_color")" = "$(lower "$color")" ] || changes="colour $cur_color -> $color"
      if [ "$desc" != "$OMIT" ] && [ "$cur_desc" != "$desc" ]; then
        changes="${changes:+$changes, }description"
      fi
      if [ -n "$changes" ]; then
        echo "  $repo: fix '$name' ($changes)"
        apply -X PATCH "repos/$ORG/$repo/labels/$(enc "$name")" "$@"
        n_fix=$((n_fix + 1))
      fi
    done < "$WORK/$tier"
  done

  # 4. report anything the manifest does not know about
  jq -r '.[].name' "$WORK/current" | while IFS= read -r name; do
    grep -Fxq "$name" "$WORK/known" || echo "$name|$repo"
  done >> "$WORK/extras"
done < "$WORK/repos"

{
  echo "## Label sync"
  echo
  echo "- repositories reconciled: **$total**"
  echo "- renamed: **$n_rename** · created: **$n_create** · corrected: **$n_fix**"
  [ "$DRY_RUN" = "true" ] && echo "- dry run: nothing was written"
  echo
  if [ -s "$WORK/extras" ]; then
    echo "### Labels not in the manifest"
    echo
    echo "Left untouched — deleting a label strips it from every issue that carries it."
    echo "Add them to \`labels.yml\` or remove them by hand."
    echo
    echo "| label | repositories |"
    echo "| --- | --- |"
    sort "$WORK/extras" | awk -F'|' '{c[$1]=c[$1]" "$2} END {for (k in c) printf "| `%s` |%s |\n", k, c[k]}' | sort
  else
    echo "Every label in every repository is covered by the manifest."
  fi
} >> "$SUMMARY"
