#!/usr/bin/env bash
# The Packer template preloads the k3s installer and payload; the node installer
# accepts them. Both sides must name the same installer pin and directory.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
template="$repo_root/packer-template/hcloud-microos-snapshots.pkr.hcl"
installer="$repo_root/scripts/install-verified-kubernetes.sh"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

installer_commit=$(sed -n 's/^K3S_INSTALLER_COMMIT="\([0-9a-f]\{40\}\)"$/\1/p' "$installer")
installer_sha256=$(sed -n 's/^K3S_INSTALLER_SHA256="\([0-9a-f]\{64\}\)"$/\1/p' "$installer")
[[ -n $installer_commit && -n $installer_sha256 ]] \
  || fail "the node installer does not pin the k3s installer commit and digest"

grep -Fq "k3s_installer_commit   = \"$installer_commit\"" "$template" \
  || fail "the template preloads a different k3s installer commit than the node installer pins"
grep -Fq "k3s_installer_sha256   = \"$installer_sha256\"" "$template" \
  || fail "the template preloads a different k3s installer digest than the node installer pins"

# shellcheck disable=SC2016 # literal shell and HCL interpolations are matched on purpose
grep -Fq 'preload_root=${KH_KUBERNETES_PRELOAD_DIR:-/var/lib/kube-hetzner/preload}' "$installer" \
  || fail "the node installer looks for the preload in another directory"
# shellcheck disable=SC2016
grep -Fq 'kubernetes_preload_dir = "/var/lib/kube-hetzner/preload/k3s/${var.kubernetes_preload_version}"' "$template" \
  || fail "the template preloads into another directory than the node installer reads"

preload_block=$(awk '/kubernetes_preload_commands = /,/^  EOT$/' "$template")
[[ $(grep -c 'sha256sum -c -' <<< "$preload_block") -eq 2 ]] \
  || fail "the preload must check both the installer and the payload digest"
grep -Fq 'kube-hetzner/kubernetes-preload' "$template" \
  || fail "preloaded snapshots must carry the kube-hetzner/kubernetes-preload label"

echo "kubernetes preload template and node installer agree"
