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

# The manifest is the only way a preload newer than the module's pin reaches
# a node, so it must be written last, after both digest checks.
[[ $(awk '/sha256sum -c -/ { last_check = NR } /manifest/ && !first_manifest { first_manifest = NR }
  END { print (last_check && first_manifest > last_check) ? "ordered" : "unordered" }' <<< "$preload_block") == ordered ]] \
  || fail "the template must write the preload manifest after both digest checks"

# Round trip: the preload commands the template renders produce a directory
# that the node installer's selection takes as a newer patch release.
packer_bin=${PACKER_BIN:-packer}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
umask 022
mkdir "$tmp/bin" "$tmp/preload"
printf '%s\n' 'preloaded k3s payload' > "$tmp/payload"
payload_sha256=$(sha256sum "$tmp/payload" | awk '{print $1}')
curl -fsS --proto '=https' --tlsv1.2 --max-redirs 0 --retry 5 --retry-all-errors \
  "https://raw.githubusercontent.com/k3s-io/k3s/$installer_commit/install.sh" -o "$tmp/install.sh"
cat > "$tmp/bin/curl" <<STUB
#!/usr/bin/env bash
set -euo pipefail
destination= url=
while ((\$#)); do
  case \$1 in
    --output) destination=\$2; shift 2 ;;
    https://*) url=\$1; shift ;;
    *) shift ;;
  esac
done
case \$url in
  https://raw.githubusercontent.com/k3s-io/k3s/$installer_commit/install.sh) cp "$tmp/install.sh" "\$destination" ;;
  https://github.com/k3s-io/k3s/releases/download/v1.36.5%2Bk3s1/k3s) cp "$tmp/payload" "\$destination" ;;
  *) echo "unexpected URL: \$url" >&2; exit 90 ;;
esac
STUB
printf '#!/bin/sh\necho x86_64\n' > "$tmp/bin/uname"
chmod +x "$tmp/bin/curl" "$tmp/bin/uname"

(cd "$repo_root/packer-template" && echo 'local.kubernetes_preload_commands' | "$packer_bin" console \
  -var hcloud_token=test-placeholder -var selinux_package_to_install=k3s \
  -var kubernetes_preload_version=v1.36.5+k3s1 \
  -var "kubernetes_preload_sha256={\"amd64\"=\"$payload_sha256\"}" \
  "$template") > "$tmp/preload-commands.sh" || fail "packer console could not render the preload commands"
sed -i.orig "s|/var/lib/kube-hetzner/preload/|$tmp/preload/|g" "$tmp/preload-commands.sh"
PATH="$tmp/bin:$PATH" sh -eu "$tmp/preload-commands.sh" > "$tmp/preload-commands.out" 2>&1 \
  || { cat "$tmp/preload-commands.out" >&2; fail "the rendered preload commands failed"; }
[[ $(cat "$tmp/preload/k3s/v1.36.5+k3s1/manifest") == "$(printf 'version=v1.36.5+k3s1\nasset=k3s\nsha256=%s' "$payload_sha256")" ]] \
  || fail "the preload manifest does not name the version, asset and verified digest"

selection=$(awk '/^select_newer_k3s_preload\(\) \{$/, /^}$/' "$installer")
[[ -n $selection ]] || fail "the node installer has no select_newer_k3s_preload"
selected=$(preload_root="$tmp/preload" version='v1.36.3+k3s1' architecture=amd64 expected_payload_sha256='' \
  sh -c "$selection"'
select_newer_k3s_preload 2>/dev/null
printf "%s %s" "$version" "$expected_payload_sha256"')
[[ $selected == "v1.36.5+k3s1 $payload_sha256" ]] \
  || fail "the node installer does not take the template's preload as a newer patch release (got: $selected)"

echo "kubernetes preload template and node installer agree"
