#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 || ! -x "$1" ]]; then
    echo "Usage: $0 /absolute/path/to/lean4lean" >&2
    exit 2
fi
checker="$(realpath "$1")"
cd "$(dirname "$0")/.."
working_directory="$(mktemp -d)"
trap 'rm -rf "$working_directory"' EXIT

# task.formal_verification.010: source -> compiled module -> separately checked declarations.
lean --version | tee "$working_directory/proof.txt"
lean --trust=0 -o "$working_directory/VaterLocal.olean" formal/VaterLocal.lean |
    tee -a "$working_directory/proof.txt"
diff -u evidence/formal/lean-4.19.0.log "$working_directory/proof.txt"
LEAN_PATH="$working_directory" "$checker" --verbose VaterLocal |
    tee "$working_directory/checker.txt"

for theorem in checkedAdd_sound checkedAdd_complete checkedAdd_overflow \
    noDispatchWithoutApproval noDispatchWithoutLiveGrant; do
    grep -Fxq "adding thmDecl VaterLocal.$theorem" "$working_directory/checker.txt"
done
