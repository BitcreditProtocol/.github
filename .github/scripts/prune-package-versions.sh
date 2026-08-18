#!/usr/bin/env bash
#
# Report, and on an explicit manual run delete, container package versions that
# nothing references any more.
#
# Packages are enumerated from the API on every run, so a new package is covered
# by the next run and nothing here lists them by name.
#
# A version is KEPT when any of these holds:
#
#   1. it carries a real tag (anything not matching sha256-*)
#   2. it carries a sha256-* sidecar tag -- an attestation or signature
#   3. its digest is the subject of a sha256-* tag: sha256-<hex> names sha256:<hex>
#   4. its digest is a child of a tagged manifest index -- resolved from the
#      registry, because the packages API cannot see inside a manifest list
#   5. it is newer than KEEP_DAYS
#
# Rule 4 is the one that matters. A multi-architecture image carries its tag on
# the index; every per-architecture manifest underneath is untagged. Treating
# "untagged" as "unused" deletes the platforms out of a live tag. Measured on
# this organisation: 30 of bcr-wdc-dashboard-ui's 443 untagged versions are
# children of tagged indexes, and 149 of clowder's are attestation subjects.
#
# Deleting a version cannot be undone and the image cannot be recreated by hand,
# so DRY_RUN defaults to true and the scheduled workflow never passes false.
#
# Env: ORG (required), GH_TOKEN (required), DRY_RUN (default true),
#      KEEP_DAYS (default 30), ONLY_PACKAGE (optional, restricts to one package)

set -eu

: "${ORG:?ORG is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
DRY_RUN="${DRY_RUN:-true}"
KEEP_DAYS="${KEEP_DAYS:-30}"
ONLY_PACKAGE="${ONLY_PACKAGE:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
CUTOFF="$(date -u -d "$KEEP_DAYS days ago" +%Y-%m-%dT%H:%M:%SZ)"

# The registry takes the same token, base64-encoded, as a bearer credential.
REG_TOKEN="$(printf '%s' "$GH_TOKEN" | base64 | tr -d '\n')"
REG_OWNER="$(printf '%s' "$ORG" | tr '[:upper:]' '[:lower:]')"
ACCEPT='application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json'

enc() { printf '%s' "$1" | jq -sRr @uri; }

echo "Cutoff for rule 5: $CUTOFF (KEEP_DAYS=$KEEP_DAYS, dry_run=$DRY_RUN)"

gh api "orgs/$ORG/packages?package_type=container&per_page=100" --paginate --slurp \
  | jq -r 'add | .[] | .name' > "$WORK/packages"

total_pkgs=0; total_versions=0; total_delete=0; total_deleted=0; skipped=0
: > "$WORK/rows"
: > "$WORK/skips"

while IFS= read -r pkg; do
  [ -z "$pkg" ] && continue
  [ -n "$ONLY_PACKAGE" ] && [ "$pkg" != "$ONLY_PACKAGE" ] && continue
  total_pkgs=$((total_pkgs + 1))

  gh api "orgs/$ORG/packages/container/$(enc "$pkg")/versions?per_page=100&state=active" \
    --paginate --slurp | jq 'add' > "$WORK/versions.json"

  n_versions=$(jq 'length' "$WORK/versions.json")
  total_versions=$((total_versions + n_versions))

  # real tags drive the registry lookups
  jq -r '.[] | .metadata.container.tags[]? | select(startswith("sha256-") | not)' \
    "$WORK/versions.json" | sort -u > "$WORK/realtags"
  n_realtags=$(wc -l < "$WORK/realtags" | tr -d ' ')

  # A package with no real tag at all: every version would look deletable, which
  # would remove the package itself. Report and move on.
  if [ "$n_realtags" -eq 0 ]; then
    echo "$pkg|no real tag on any version" >> "$WORK/skips"
    skipped=$((skipped + 1))
    continue
  fi

  # rule 4: children of every tagged index. A failed lookup means unknown
  # children, so the whole package is skipped rather than partly resolved.
  : > "$WORK/children"
  resolve_failed=""
  while IFS= read -r tag; do
    [ -z "$tag" ] && continue
    if ! curl -sS --fail-with-body -H "Authorization: Bearer $REG_TOKEN" -H "Accept: $ACCEPT" \
         "https://ghcr.io/v2/$REG_OWNER/$pkg/manifests/$(enc "$tag")" > "$WORK/manifest.json" 2>/dev/null; then
      resolve_failed="$tag"
      break
    fi
    jq -r '.manifests[]?.digest // empty' "$WORK/manifest.json" >> "$WORK/children"
  done < "$WORK/realtags"

  if [ -n "$resolve_failed" ]; then
    echo "$pkg|could not resolve tag '$resolve_failed' from the registry" >> "$WORK/skips"
    skipped=$((skipped + 1))
    continue
  fi
  sort -u "$WORK/children" -o "$WORK/children"

  # children as a JSON array, so jq can test membership directly
  jq -R . "$WORK/children" | jq -s . > "$WORK/children.json"

  # Classification, verified against an independent hand count on three packages:
  # bcr-wdc-dashboard-ui 413, clowder 429, bcr-wdc-core-service 155 deletable with
  # rule 5 held inert. Capturing the version in $v matters -- inside a pipe the
  # dot refers to the array being searched, not to the version.
  jq -r --slurpfile ch "$WORK/children.json" --arg cutoff "$CUTOFF" '
    ($ch[0] // []) as $children
    | ([ .[] | .metadata.container.tags[]? | select(startswith("sha256-")) | "sha256:" + .[7:] ] | unique) as $subjects
    | [ .[]
        | . as $v
        | ((.metadata.container.tags) // []) as $tags
        | { id: $v.id, name: $v.name, created_at: $v.created_at,
            real:    ($tags | map(startswith("sha256-") | not) | any),
            sidecar: (($tags | length) > 0 and ($tags | map(startswith("sha256-")) | all)),
            subject: ($subjects | index($v.name) != null),
            child:   ($children | index($v.name) != null),
            fresh:   ($v.created_at > $cutoff) } ]
    | .[]
    | select((.real or .sidecar or .subject or .child or .fresh) | not)
    | "\(.id)\t\(.name)\t\(.created_at)"
  ' "$WORK/versions.json" > "$WORK/delete" || : > "$WORK/delete"

  n_delete=$(wc -l < "$WORK/delete" | tr -d ' ')
  n_children=$(wc -l < "$WORK/children" | tr -d ' ')
  n_keep=$((n_versions - n_delete))
  total_delete=$((total_delete + n_delete))

  echo "$pkg|$n_versions|$n_keep|$n_delete|$n_realtags|$n_children" >> "$WORK/rows"
  echo "  $pkg: $n_versions versions, keeping $n_keep, deletable $n_delete ($n_realtags real tags, $n_children index children)"

  if [ "$DRY_RUN" != "true" ] && [ "$n_delete" -gt 0 ]; then
    while IFS=$'\t' read -r id name created; do
      [ -z "$id" ] && continue
      gh api -X DELETE "orgs/$ORG/packages/container/$(enc "$pkg")/versions/$id" >/dev/null
      total_deleted=$((total_deleted + 1))
    done < "$WORK/delete"
    echo "    deleted $n_delete versions"
  fi
done < "$WORK/packages"

{
  echo "## Package versions"
  echo
  echo "- packages examined: **$total_pkgs**"
  echo "- versions seen: **$total_versions**"
  echo "- deletable: **$total_delete**"
  if [ "$DRY_RUN" = "true" ]; then
    echo "- dry run: **nothing was deleted**"
  else
    echo "- deleted: **$total_deleted**"
  fi
  echo "- keeping anything newer than **$KEEP_DAYS days** ($CUTOFF), plus everything referenced"
  echo
  if [ -s "$WORK/rows" ]; then
    echo "| package | versions | keep | deletable | real tags | index children |"
    echo "| --- | --- | --- | --- | --- | --- |"
    awk -F'|' '{printf "| `%s` | %s | %s | %s | %s | %s |\n", $1, $2, $3, $4, $5, $6}' "$WORK/rows"
    echo
  fi
  if [ -s "$WORK/skips" ]; then
    echo "### Skipped"
    echo
    echo "Skipped rather than partly resolved. An unresolved tag means unknown"
    echo "children, and a package with no real tag would lose every version."
    echo
    echo "| package | reason |"
    echo "| --- | --- |"
    awk -F'|' '{printf "| `%s` | %s |\n", $1, $2}' "$WORK/skips"
    echo
  fi
  echo "A version is kept when it carries a real tag, carries a \`sha256-*\` sidecar,"
  echo "is the subject of one, is a child of a tagged manifest index, or is newer"
  echo "than the cutoff. Deletion is irreversible, so the schedule only ever reports."
} >> "$SUMMARY"

echo "packages=$total_pkgs versions=$total_versions deletable=$total_delete deleted=$total_deleted skipped=$skipped"
