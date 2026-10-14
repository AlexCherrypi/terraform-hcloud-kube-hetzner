#!/bin/sh
set -eu

# This script is embedded into node bootstrap by Terraform. Remote installer
# bytes are pinned here. Kubernetes payload digests come from the module's
# reviewed manifest, explicit operator input, or the exact official release's
# checksum publication for backward-compatible custom-version bootstrap.

K3S_INSTALLER_COMMIT="2d0f82fa2f933cd227fe38e1482558ce4769f464"
K3S_INSTALLER_SHA256="ed01f89fd977bf20ac1516bbebf8370bf3ddbaa55dac8aba610956a4c78cc00b"
RKE2_INSTALLER_COMMIT="c4f306e6c5fa18dfb447bf6b8a0423f2da68c939"
RKE2_INSTALLER_SHA256="42983c86d1da64a92061d83afb57630cedd69241989f1b0673f3db6c3d92ee6b"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  fail "usage: $0 <k3s|rke2> <version> <amd64-sha256> <arm64-sha256> <base64-install-exec>"
}

download_with_retries() {
  attempt=1
  while :; do
    if curl "$@"; then
      return 0
    fi
    if [ "$attempt" -ge 3 ]; then
      return 1
    fi
    sleep "$attempt"
    attempt=$((attempt + 1))
  done
}

download_immutable_installer() {
  destination=$1
  url=$2

  download_with_retries -fsS --proto '=https' --tlsv1.2 --max-redirs 0 \
    --connect-timeout 20 --max-time 300 \
    --output "$destination" "$url"
}

download_release_payload() {
  destination=$1
  url=$2

  download_with_retries -fsS --proto '=https' --proto-redir '=https' --tlsv1.2 --location \
    --connect-timeout 20 --max-time 900 --max-filesize 1073741824 \
    --output "$destination" "$url"
}

download_release_metadata() {
  destination=$1
  url=$2

  download_with_retries -fsS --proto '=https' --proto-redir '=https' --tlsv1.2 --location \
    --connect-timeout 20 --max-time 300 --max-filesize 10485760 \
    --output "$destination" "$url"
}

# A snapshot build may preload the pinned installer and payload of one exact
# version (packer-template kubernetes_preload_version). They are used only when
# their digest equals the pin (or, for a newer patch release, the digest of its
# build manifest), so a stale or damaged preload can never be installed.
preload_root=${KH_KUBERNETES_PRELOAD_DIR:-/var/lib/kube-hetzner/preload}

use_preloaded_file() {
  preloaded_file=$1
  destination=$2
  expected=$3
  label=$4

  [ -n "$expected" ] || return 1
  [ -f "$preloaded_file" ] && [ ! -L "$preloaded_file" ] || return 1
  preloaded_sha256=$(sha256sum "$preloaded_file" | awk '{print $1}')
  if [ "$preloaded_sha256" != "$expected" ]; then
    echo "WARNING: preloaded $label does not match its pinned SHA-256; downloading it instead" >&2
    return 1
  fi
  cp "$preloaded_file" "$destination"
  echo "Using the preloaded $label from $preloaded_file" >&2
}

# Prefer the newest preloaded patch release of the same minor that is newer
# than the pin and matches the manifest its snapshot build wrote, from a tree
# that only this user can change (the manifest is the digest).
select_newer_k3s_preload() {
  minor=${version%".${version#v*.*.}"} newer_version='' newer_asset=k3s
  [ "$architecture" = amd64 ] || newer_asset=k3s-arm64
  if [ -n "$([ ! -e "$preload_root" ] || find "$preload_root" ! -user "$(id -u)" -o ! -type l \
    \( -perm -020 -o -perm -002 \) 2>&1 || echo failed)" ]; then
    echo "WARNING: ignoring newer preloaded k3s: others can change $preload_root" >&2
    return 0
  fi
  for newer_dir in "$preload_root/k3s/$minor".*; do
    candidate=${newer_dir##*/}
    printf '%s' "$candidate" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+\+k3s[0-9]+$' || continue
    # Newer: higher patch, same patch as a pre-release, or higher k3s revision.
    a=${candidate#"$minor".}
    b=${newer_version:-$version}
    b=${b#"$minor".}
    [ "${a%%+*}" -gt "${b%%[-+]*}" ] || { [ "${a%%+*}" -eq "${b%%[-+]*}" ] &&
      case $b in *-*) ;; *) [ "${a##*k3s}" -gt "${b##*k3s}" ] ;; esac; } || continue
    newer_manifest=$newer_dir/manifest newer_payload=$newer_dir/$newer_asset
    digest=$([ -f "$newer_manifest" ] && [ ! -L "$newer_manifest" ] && awk -F= -v v="$candidate" -v a="$newer_asset" '
      NF != 2 { bad = 1 } $1 == "version" { V++; bad += $2 != v } $1 == "asset" { A++; bad += $2 != a } $1 == "sha256" { S++; d = $2 }
      END { if (!bad && NR == 3 && V == 1 && A == 1 && S == 1 && length(d) == 64 && d !~ /[^0-9a-f]/ && d !~ /^0+$/) print d }' "$newer_manifest") || digest=
    if [ -z "$digest" ] || [ -L "$newer_dir" ] || [ ! -f "$newer_payload" ] || [ -L "$newer_payload" ] ||
      [ "$(sha256sum "$newer_payload" | awk '{print $1}')" != "$digest" ]; then
      echo "WARNING: ignoring preloaded k3s $candidate: bad manifest or payload" >&2
      continue
    fi
    newer_version=$candidate newer_sha256=$digest
  done
  if [ -n "$newer_version" ]; then
    echo "Installing preloaded k3s $newer_version, newer than $version" >&2
    version=$newer_version expected_payload_sha256=$newer_sha256
  else
    echo "No preloaded k3s newer than $version" >&2
  fi
}

verify_sha256() {
  file=$1
  expected=$2
  label=$3
  actual=$(sha256sum "$file" | awk '{print $1}')

  if [ "$actual" != "$expected" ]; then
    fail "$label SHA-256 mismatch: expected $expected, got $actual"
  fi
}

resolve_release_payload_sha256() {
  release_distribution=$1
  release_version_urlsafe=$2
  release_architecture=$3
  release_asset_name=$4
  checksum_file=$work_dir/sha256sum-$release_distribution-$release_architecture.txt

  case $release_distribution in
    k3s)
      checksum_url="https://github.com/k3s-io/k3s/releases/download/$release_version_urlsafe/sha256sum-$release_architecture.txt"
      ;;
    rke2)
      checksum_url="https://github.com/rancher/rke2/releases/download/$release_version_urlsafe/sha256sum-$release_architecture.txt"
      ;;
    *)
      fail "unsupported checksum distribution: $release_distribution"
      ;;
  esac

  download_release_metadata "$checksum_file" "$checksum_url"
  resolved_sha256=$(awk -v asset="$release_asset_name" '
    NF == 2 && $2 == asset && length($1) == 64 && $1 !~ /[^0-9A-Fa-f]/ {
      matches++
      digest = tolower($1)
    }
    END {
      if (matches == 1 && digest !~ /^0+$/) print digest
      else exit 1
    }
  ' "$checksum_file") || fail "official checksum publication did not identify exactly one valid $release_asset_name digest"

  echo "WARNING: no independent payload digest was configured for $release_distribution $version on $release_architecture; using its exact official release checksum publication" >&2
  printf '%s\n' "$resolved_sha256"
}

check_external_k3s_selinux() {
  external_k3s_selinux_state=Disabled
  [ -n "${KH_K3S_EXTERNAL_NODE_IP:-}" ] || return 0

  if ! command -v getenforce >/dev/null 2>&1; then
    if [ -e /sys/fs/selinux/enforce ] ||
      { command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled; } ||
      { [ -r /etc/selinux/config ] && grep -Eiq '^[[:space:]]*SELINUX[[:space:]]*=[[:space:]]*(enforcing|permissive)[[:space:]]*$' /etc/selinux/config; }; then
      fail "SELinux is active or configured but getenforce is unavailable; install the SELinux userland before joining this node"
    fi
    return 0
  fi

  external_k3s_selinux_state=$(getenforce 2>/dev/null || fail "could not determine SELinux state on external node")
  case $external_k3s_selinux_state in
    Disabled)
      return 0
      ;;
    Enforcing | Permissive)
      ;;
    *)
      fail "unsupported SELinux state on external node: $external_k3s_selinux_state"
      ;;
  esac

  command -v semodule >/dev/null 2>&1 || fail "SELinux is active but semodule is unavailable; install the k3s SELinux policy before joining this node"
  command -v chcon >/dev/null 2>&1 || fail "SELinux is active but chcon is unavailable; install the SELinux userland before joining this node"
  policy_size=$(stat -c %s /usr/share/selinux/packages/k3s.pp 2>/dev/null || printf '0')
  [ "$policy_size" -gt 0 ] || fail "SELinux is active but /usr/share/selinux/packages/k3s.pp is missing; install k3s-selinux before joining this node"
  if command -v rpm >/dev/null 2>&1 && ! rpm -q k3s-selinux >/dev/null 2>&1; then
    fail "SELinux is active but the k3s-selinux package is not installed"
  fi
}

apply_external_k3s_selinux() {
  [ "${external_k3s_selinux_state:-Disabled}" != Disabled ] || return 0

  semodule -i /usr/share/selinux/packages/k3s.pp || fail "could not load the k3s SELinux policy"
  chcon -u system_u -r object_r -t container_runtime_exec_t /usr/local/bin/k3s || fail "could not label the verified k3s binary"
  if [ "$external_k3s_selinux_state" = Enforcing ] && ! semodule -l 2>/dev/null | awk '{print $1}' | grep -qx k3s; then
    fail "SELinux is enforcing but the k3s policy is not loaded"
  fi
}

start_external_k3s_agent() {
  [ -n "${KH_K3S_EXTERNAL_NODE_IP:-}" ] || return 0

  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now k3s-agent
  elif command -v rc-update >/dev/null 2>&1 && command -v rc-service >/dev/null 2>&1; then
    rc-update add k3s-agent default
    rc-service k3s-agent restart
  else
    fail "k3s-agent was installed but no supported service manager was found"
  fi
}

[ "$#" -eq 5 ] || usage

distribution=$1
version=$2
amd64_sha256=$3
arm64_sha256=$4
install_exec_b64=$5

case $(uname -m) in
  x86_64 | amd64)
    architecture=amd64
    expected_payload_sha256=$amd64_sha256
    ;;
  aarch64 | arm64)
    architecture=arm64
    expected_payload_sha256=$arm64_sha256
    ;;
  *)
    fail "unsupported Kubernetes artifact architecture: $(uname -m)"
    ;;
esac

if [ -n "$expected_payload_sha256" ]; then
  case $expected_payload_sha256 in
    *[!0-9a-f]*)
      fail "invalid pinned SHA-256 for $distribution $version on $architecture"
      ;;
  esac
  if [ "${#expected_payload_sha256}" -ne 64 ]; then
    fail "invalid pinned SHA-256 for $distribution $version on $architecture"
  fi
  if [ "$expected_payload_sha256" = "0000000000000000000000000000000000000000000000000000000000000000" ]; then
    fail "refusing an all-zero SHA-256 pin for $distribution $version on $architecture"
  fi
fi

install_exec=$(printf '%s' "$install_exec_b64" | base64 --decode) || fail "could not decode install arguments"
[ -n "$install_exec" ] || fail "install arguments must not be empty"

case $distribution in
  k3s)
    printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?\+k3s[0-9]+$' || fail "invalid k3s release tag: $version"
    installer_url="https://raw.githubusercontent.com/k3s-io/k3s/$K3S_INSTALLER_COMMIT/install.sh"
    installer_sha256=$K3S_INSTALLER_SHA256
    ;;
  rke2)
    printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?\+rke2(r[0-9]+)?$' || fail "invalid RKE2 release tag: $version"
    installer_url="https://raw.githubusercontent.com/rancher/rke2/$RKE2_INSTALLER_COMMIT/install.sh"
    installer_sha256=$RKE2_INSTALLER_SHA256
    ;;
  *)
    usage
    ;;
esac

if [ "$distribution" = k3s ] && [ -n "${KH_K3S_EXTERNAL_NODE_IP:-}" ]; then
  case $KH_K3S_EXTERNAL_NODE_IP in
    *[!0-9A-Fa-f:.,]*)
      fail "external node IP must contain only IPv4/IPv6 address characters; replace <PUBLIC_NODE_IP> in the generated command"
      ;;
  esac
  install_exec="$install_exec --node-external-ip=$KH_K3S_EXTERNAL_NODE_IP --flannel-backend=wireguard-native"
fi

if [ "$distribution" = k3s ]; then
  check_external_k3s_selinux
fi

umask 077
work_dir=$(mktemp -d -t kh-kubernetes-install.XXXXXXXXXX)
installer=$work_dir/upstream-install.sh
cleanup() {
  status=$?
  trap - EXIT INT TERM
  rm -rf "$work_dir"
  exit "$status"
}
trap cleanup EXIT INT TERM

# Set by Terraform only while the upgrade plans follow the channel.
if [ "$distribution" = k3s ] && [ "${KH_K3S_NEWER_PRELOAD:-}" = 1 ]; then
  select_newer_k3s_preload
fi
preload_dir=$preload_root/$distribution/$version
if [ "$distribution" != k3s ] ||
  ! use_preloaded_file "$preload_dir/install.sh" "$installer" "$installer_sha256" "$distribution installer"; then
  download_immutable_installer "$installer" "$installer_url"
fi
verify_sha256 "$installer" "$installer_sha256" "$distribution installer"

version_urlsafe=$(printf '%s' "$version" | sed 's/+/%2B/g')

case $distribution in
  k3s)
    if [ "$architecture" = amd64 ]; then
      asset_name=k3s
    else
      asset_name=k3s-arm64
    fi

    payload=$work_dir/$asset_name
    payload_url="https://github.com/k3s-io/k3s/releases/download/$version_urlsafe/$asset_name"
    # Only a pinned (or manifest) digest can accept the preload; without one
    # the digest itself has to come from the release's checksum publication.
    if ! use_preloaded_file "$preload_dir/$asset_name" "$payload" "$expected_payload_sha256" "k3s payload"; then
      if [ -z "$expected_payload_sha256" ]; then
        expected_payload_sha256=$(resolve_release_payload_sha256 k3s "$version_urlsafe" "$architecture" "$asset_name")
      fi
      download_release_payload "$payload" "$payload_url"
    fi
    verify_sha256 "$payload" "$expected_payload_sha256" "k3s payload"
    install -o root -g root -m 0755 "$payload" /usr/local/bin/k3s

    apply_external_k3s_selinux

    # Ignore inherited installer controls from /etc/environment or preinstall
    # hooks. They must not redirect the service to bytes outside the path that
    # was just verified above.
    unset INSTALL_K3S_ARTIFACT_URL INSTALL_K3S_BIN_DIR_READ_ONLY INSTALL_K3S_CHANNEL INSTALL_K3S_CHANNEL_URL
    unset INSTALL_K3S_COMMIT INSTALL_K3S_FORCE_RESTART INSTALL_K3S_NAME INSTALL_K3S_PR INSTALL_K3S_SELINUX_WARN
    unset INSTALL_K3S_SKIP_ENABLE INSTALL_K3S_SYMLINK INSTALL_K3S_SYSTEMD_DIR INSTALL_K3S_TYPE
    unset KILLALL_K3S_SH UNINSTALL_K3S_SH

    ARCH=$architecture
    INSTALL_K3S_BIN_DIR=/usr/local/bin
    INSTALL_K3S_SKIP_DOWNLOAD=true
    INSTALL_K3S_SKIP_START=true
    INSTALL_K3S_SKIP_SELINUX_RPM=true
    INSTALL_K3S_VERSION=$version
    INSTALL_K3S_EXEC=$install_exec
    export ARCH INSTALL_K3S_BIN_DIR INSTALL_K3S_SKIP_DOWNLOAD INSTALL_K3S_SKIP_START INSTALL_K3S_SKIP_SELINUX_RPM INSTALL_K3S_VERSION INSTALL_K3S_EXEC
    sh "$installer"
    start_external_k3s_agent
    ;;
  rke2)
    suffix=linux-$architecture
    asset_name=rke2.$suffix.tar.gz
    artifact_dir=$work_dir/artifacts
    mkdir -p "$artifact_dir"
    payload=$artifact_dir/$asset_name
    payload_url="https://github.com/rancher/rke2/releases/download/$version_urlsafe/$asset_name"
    if [ -z "$expected_payload_sha256" ]; then
      expected_payload_sha256=$(resolve_release_payload_sha256 rke2 "$version_urlsafe" "$architecture" "$asset_name")
    fi
    download_release_payload "$payload" "$payload_url"
    verify_sha256 "$payload" "$expected_payload_sha256" "RKE2 payload"

    # The selected digest is written locally for the official installer's
    # air-gap path so it cannot fetch or reinterpret another checksum itself.
    printf '%s  %s\n' "$expected_payload_sha256" "$asset_name" > "$artifact_dir/sha256sum-$architecture.txt"

    unset INSTALL_RKE2_AGENT_IMAGES_DIR INSTALL_RKE2_ARTIFACT_URL INSTALL_RKE2_CHANNEL INSTALL_RKE2_CHANNEL_URL
    unset INSTALL_RKE2_COMMIT INSTALL_RKE2_RPM_RELEASE_VERSION INSTALL_RKE2_SKIP_FAPOLICY INSTALL_RKE2_SKIP_RELOAD
    unset INSTALL_RKE2_SKIP_RESTORECON INSTALL_RKE2_TAR_PREFIX INSTALL_RKE2_TYPE

    ARCH=$architecture
    INSTALL_RKE2_METHOD=tar
    INSTALL_RKE2_VERSION=$version
    INSTALL_RKE2_EXEC=$install_exec
    INSTALL_RKE2_ARTIFACT_PATH=$artifact_dir
    export ARCH INSTALL_RKE2_METHOD INSTALL_RKE2_VERSION INSTALL_RKE2_EXEC INSTALL_RKE2_ARTIFACT_PATH
    sh "$installer"
    ;;
esac
