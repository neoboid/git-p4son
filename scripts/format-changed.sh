#!/bin/sh
# Format all changed Python files (tracked by git) with autopep8.

files=$(git diff --name-only --diff-filter=ACM -- '*.py')
if [ -z "$files" ]; then
    echo "No changed Python files to format."
    exit 0
fi

echo "$files" | xargs autopep8 -i
echo "Formatted $(echo "$files" | wc -l | tr -d ' ') file(s)."
