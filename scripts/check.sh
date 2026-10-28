#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -v
tofu -chdir=infrastructure fmt -check -recursive
tofu -chdir=infrastructure init -backend=false -lockfile=readonly
tofu -chdir=infrastructure validate
tofu -chdir=infrastructure test
nix --extra-experimental-features 'nix-command flakes' flake check "path:$PWD/nix" --no-build
if [[ ${NIXVM_BUILD_CHECKS:-0} == 1 ]]; then
  nix --extra-experimental-features 'nix-command flakes' build "path:$PWD/nix#bootstrap"
  nix --extra-experimental-features 'nix-command flakes' flake check "path:$PWD/nix"
fi
