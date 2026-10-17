#!/bin/sh
# Type-check git_p4son/ and tests/ with mypy, once for each platform git-p4son runs on; fails if either check fails.

cd "$(dirname "$0")/.." || exit 1
if ! python -c 'import mypy' 2>/dev/null; then
    echo "mypy is not installed. Install it with: pip install mypy==2.4.0" >&2
    exit 1
fi

status=0
for platform in linux win32; do
    echo "Type-checking for $platform..."
    python -m mypy --platform "$platform" || status=1
done
exit $status
