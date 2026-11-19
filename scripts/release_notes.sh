#!/usr/bin/env bash
# Print one version's section of CHANGELOG.md, for the GitHub release notes.
#
#   scripts/release_notes.sh v0.3.0
#
# Fails when the section is missing or empty, so a version can't be released
# without notes.
set -euo pipefail

version=${1:?usage: scripts/release_notes.sh vX.Y.Z}
version=${version#v}
changelog="$(dirname "$0")/../CHANGELOG.md"

# Print the lines after "## [X.Y.Z]" up to the next "## " heading or the
# link definitions ("[X.Y.Z]: https://...") at the bottom of the file.
notes=$(awk -v version="$version" '
  found && (/^## / || /^\[[^]]+\]: /) { exit }
  found { print }
  /^## \[/ && index($0, "[" version "]") { found = 1 }
' "$changelog")

if [ -z "${notes//[[:space:]]/}" ]; then
  echo "CHANGELOG.md has no notes for version $version" >&2
  exit 1
fi
printf '%s\n' "$notes"
