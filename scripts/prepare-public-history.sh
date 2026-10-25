#!/usr/bin/env bash
set -euo pipefail

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo 'A clean working tree is required.' >&2
  exit 1
fi

git rev-parse --verify 'main^{commit}' >/dev/null
tree="$(git rev-parse 'main^{tree}')"
name='OCNAI'
email='155850614+OCNAI@users.noreply.github.com'
message=$'Kebivo 0.1.0\n\nInitial public source release.'

commit="$(printf '%s\n' "$message" | \
  GIT_AUTHOR_NAME="$name" GIT_AUTHOR_EMAIL="$email" \
  GIT_COMMITTER_NAME="$name" GIT_COMMITTER_EMAIL="$email" \
  git commit-tree "$tree")"
git branch -f public-main "$commit" >/dev/null

if [[ "$(git rev-list --count public-main)" != 1 ]]; then
  echo 'public-main must contain exactly one commit.' >&2
  exit 1
fi
if [[ "$(git rev-list --parents -n 1 public-main)" != "$commit" ]]; then
  echo 'The public commit must be a root commit.' >&2
  exit 1
fi

tracker='OG''S-'
if git log --format=%B public-main | grep -Eiq "${tracker}|Co-Authored-By|Paperclip|Claude"; then
  echo 'The public commit message contains a private-history marker.' >&2
  exit 1
fi
identity="$(git log -1 --format='%an <%ae> / %cn <%ce>' public-main)"
expected="$name <$email> / $name <$email>"
if [[ "$identity" != "$expected" ]]; then
  echo "Unexpected public commit identity: $identity" >&2
  exit 1
fi

tree_pattern="${tracker}[0-9]+|ogs"'_pt_|ogs''_admin_session|knowledge''-ops|<''owner>|OG''s Lab|ogs''-lab'
grep_status=0
git grep -n -I -E "$tree_pattern" public-main -- . ':(exclude)**/node_modules/**' || grep_status=$?
if [[ "$grep_status" -eq 0 ]]; then
  echo 'The public tree contains an old name or tracker reference.' >&2
  exit 1
elif [[ "$grep_status" -ne 1 ]]; then
  echo 'The public tree scan failed.' >&2
  exit 1
fi
if [[ "$(git rev-parse 'public-main^{tree}')" != "$tree" ]]; then
  echo 'public-main has the wrong tree.' >&2
  exit 1
fi
if ! git diff --quiet main public-main; then
  echo 'public-main differs from main.' >&2
  exit 1
fi

echo "public-main: $commit"
echo "Identity: $identity"
echo 'Verified: one root commit; message and tree scans empty; git diff main public-main empty.'
