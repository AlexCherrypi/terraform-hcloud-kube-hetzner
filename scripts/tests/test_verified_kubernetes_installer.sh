#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_script="$repo_root/scripts/install-verified-kubernetes.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

[[ $(rg -c 'download_with_retries -fsS' "$source_script") -eq 3 ]] \
  || { echo "verified downloads must use curl's quiet error-only mode" >&2; exit 1; }
if rg -n 'download_with_retries -fS' "$source_script"; then
  echo "verified downloads still emit transfer progress" >&2
  exit 1
fi

real_curl=$(command -v curl)
k3s_installer="$tmp/k3s-install.sh"
rke2_installer="$tmp/rke2-install.sh"

"$real_curl" -fsS --proto '=https' --tlsv1.2 --max-redirs 0 --retry 5 --retry-all-errors \
  https://raw.githubusercontent.com/k3s-io/k3s/2d0f82fa2f933cd227fe38e1482558ce4769f464/install.sh \
  -o "$k3s_installer"
"$real_curl" -fsS --proto '=https' --tlsv1.2 --max-redirs 0 --retry 5 --retry-all-errors \
  https://raw.githubusercontent.com/rancher/rke2/c4f306e6c5fa18dfb447bf6b8a0423f2da68c939/install.sh \
  -o "$rke2_installer"

printf '%s  %s\n' ed01f89fd977bf20ac1516bbebf8370bf3ddbaa55dac8aba610956a4c78cc00b "$k3s_installer" | sha256sum -c - >/dev/null
printf '%s  %s\n' 42983c86d1da64a92061d83afb57630cedd69241989f1b0673f3db6c3d92ee6b "$rke2_installer" | sha256sum -c - >/dev/null

fake_payload_amd64="$tmp/replaced-payload-amd64"
fake_payload_arm64="$tmp/replaced-payload-arm64"
printf '%s\n' 'attacker-controlled amd64 payload' > "$fake_payload_amd64"
printf '%s\n' 'independently different arm64 payload' > "$fake_payload_arm64"
fake_payload_amd64_sha=$(sha256sum "$fake_payload_amd64" | awk '{print $1}')
fake_payload_arm64_sha=$(sha256sum "$fake_payload_arm64" | awk '{print $1}')
wrong_payload_sha=$(printf '%s\n' 'independently pinned different payload' | sha256sum | awk '{print $1}')
printf '%s  %s\n' "$fake_payload_amd64_sha" "$fake_payload_amd64" | sha256sum -c - >/dev/null

mkdir "$tmp/fake-bin"
cat > "$tmp/fake-bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

destination=
url=
while (($#)); do
  case $1 in
    -o | --output)
      destination=$2
      shift 2
      ;;
    http://* | https://*)
      url=$1
      shift
      ;;
    *)
      shift
      ;;
  esac
done

[[ -n $destination && -n $url ]]
printf '%s\n' "$url" >> "$KH_TEST_DOWNLOAD_LOG"

if [[ -n ${KH_TEST_CURL_FAIL_ONCE:-} && $url == *"$KH_TEST_CURL_FAIL_ONCE"* && ! -e $KH_TEST_CURL_STATE ]]; then
  : > "$KH_TEST_CURL_STATE"
  exit 88
fi

case $url in
  *raw.githubusercontent.com/k3s-io/k3s/*)
    cp "$KH_TEST_K3S_INSTALLER" "$destination"
    ;;
  *raw.githubusercontent.com/rancher/rke2/*)
    cp "$KH_TEST_RKE2_INSTALLER" "$destination"
    ;;
  *github.com/k3s-io/k3s/releases/*/k3s-arm64 | *github.com/rancher/rke2/releases/*/rke2.linux-arm64.tar.gz)
    cp "$KH_TEST_PAYLOAD_ARM64" "$destination"
    ;;
  *github.com/k3s-io/k3s/releases/*/k3s | *github.com/rancher/rke2/releases/*/rke2.linux-amd64.tar.gz)
    cp "$KH_TEST_PAYLOAD_AMD64" "$destination"
    ;;
  *github.com/k3s-io/k3s/releases/*/sha256sum-amd64.txt)
    printf '%s  k3s\n' "${KH_TEST_CHECKSUM_SHA:-$KH_TEST_EXPECTED_PAYLOAD_SHA}" > "$destination"
    ;;
  *github.com/k3s-io/k3s/releases/*/sha256sum-arm64.txt)
    printf '%s  k3s-arm64\n' "${KH_TEST_CHECKSUM_SHA:-$KH_TEST_EXPECTED_PAYLOAD_SHA}" > "$destination"
    ;;
  *github.com/rancher/rke2/releases/*/sha256sum-amd64.txt)
    printf '%s  rke2.linux-amd64.tar.gz\n' "${KH_TEST_CHECKSUM_SHA:-$KH_TEST_EXPECTED_PAYLOAD_SHA}" > "$destination"
    ;;
  *github.com/rancher/rke2/releases/*/sha256sum-arm64.txt)
    printf '%s  rke2.linux-arm64.tar.gz\n' "${KH_TEST_CHECKSUM_SHA:-$KH_TEST_EXPECTED_PAYLOAD_SHA}" > "$destination"
    ;;
  *)
    echo "unexpected URL: $url" >&2
    exit 90
    ;;
esac
EOF
chmod +x "$tmp/fake-bin/curl"

cat > "$tmp/fake-bin/uname" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} == -m ]]
printf '%s\n' "${KH_TEST_UNAME:?}"
EOF
chmod +x "$tmp/fake-bin/uname"

cat > "$tmp/fake-bin/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$tmp/fake-bin/sleep"

cat > "$tmp/fake-bin/getenforce" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "${KH_TEST_SELINUX_STATE:-Disabled}"
EOF
chmod +x "$tmp/fake-bin/getenforce"

mkdir "$tmp/no-getenforce-bin"
ln -s "$(command -v bash)" "$tmp/no-getenforce-bin/bash"
ln -s "$(command -v base64)" "$tmp/no-getenforce-bin/base64"
ln -s "$(command -v grep)" "$tmp/no-getenforce-bin/grep"
ln -s "$tmp/fake-bin/uname" "$tmp/no-getenforce-bin/uname"
cat > "$tmp/no-getenforce-bin/selinuxenabled" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$tmp/no-getenforce-bin/selinuxenabled"

cat > "$tmp/fake-bin/stat" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${KH_TEST_MISSING_SELINUX_POLICY:-} == true ]]; then
  printf '0\n'
else
  printf '128\n'
fi
EOF
chmod +x "$tmp/fake-bin/stat"

cat > "$tmp/fake-bin/rpm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ $* == '-q k3s-selinux' && ${KH_TEST_MISSING_SELINUX_PACKAGE:-} != true ]]
EOF
chmod +x "$tmp/fake-bin/rpm"

cat > "$tmp/fake-bin/semodule" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == -l ]]; then
  printf 'k3s 1.0\n'
else
  printf 'semodule %s\n' "$*" >> "$KH_TEST_SELINUX_LOG"
fi
EOF
chmod +x "$tmp/fake-bin/semodule"

cat > "$tmp/fake-bin/chcon" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'chcon %s\n' "$*" >> "$KH_TEST_SELINUX_LOG"
EOF
chmod +x "$tmp/fake-bin/chcon"

cat > "$tmp/fake-bin/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >> "$KH_TEST_SERVICE_LOG"
EOF
chmod +x "$tmp/fake-bin/systemctl"

cat > "$tmp/fake-bin/install" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'install %s\n' "$*" >> "$KH_TEST_INSTALL_LOG"
if [[ -n ${KH_TEST_INSTALLED_BIN:-} ]]; then
  cp "${@: -2:1}" "$KH_TEST_INSTALLED_BIN"
fi
EOF
chmod +x "$tmp/fake-bin/install"

cat > "$tmp/fake-bin/sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ ${INSTALL_K3S_SKIP_DOWNLOAD:-} == true ]]; then
  [[ ${ARCH:-} == "$KH_TEST_ARCH" ]]
  [[ ${INSTALL_K3S_BIN_DIR:-} == /usr/local/bin ]]
  [[ ${INSTALL_K3S_SKIP_START:-} == true ]]
  [[ ${INSTALL_K3S_SKIP_SELINUX_RPM:-} == true ]]
  [[ ${INSTALL_K3S_VERSION:-} == "${KH_TEST_INSTALL_VERSION:-v1.36.3+k3s1}" ]]
  [[ ${INSTALL_K3S_EXEC:-} == "${KH_TEST_INSTALL_EXEC:-server}" ]]
  [[ -z ${INSTALL_K3S_COMMIT:-} ]]
  [[ -z ${INSTALL_K3S_NAME:-} ]]
  [[ -z ${INSTALL_K3S_SYSTEMD_DIR:-} ]]
  printf '%s\n' k3s >> "$KH_TEST_INSTALL_LOG"
  exit 0
fi

if [[ ${INSTALL_RKE2_METHOD:-} == tar ]]; then
  [[ ${ARCH:-} == "$KH_TEST_ARCH" ]]
  [[ ${INSTALL_RKE2_VERSION:-} == 'v1.32.5+rke2r1' ]]
  [[ ${INSTALL_RKE2_EXEC:-} == server ]]
  [[ -z ${INSTALL_RKE2_COMMIT:-} ]]
  [[ -z ${INSTALL_RKE2_TAR_PREFIX:-} ]]
  [[ -s ${INSTALL_RKE2_ARTIFACT_PATH}/rke2.linux-${KH_TEST_ARCH}.tar.gz ]]
  if [[ ! -s ${INSTALL_RKE2_ARTIFACT_PATH}/sha256sum-${KH_TEST_ARCH}.txt ]]; then
    find "$INSTALL_RKE2_ARTIFACT_PATH" -maxdepth 1 -type f -print >&2
    exit 92
  fi
  grep -Fq "$KH_TEST_EXPECTED_PAYLOAD_SHA  rke2.linux-${KH_TEST_ARCH}.tar.gz" \
    "${INSTALL_RKE2_ARTIFACT_PATH}/sha256sum-${KH_TEST_ARCH}.txt"
  printf '%s\n' rke2 >> "$KH_TEST_INSTALL_LOG"
  exit 0
fi

echo 'pinned installer invoked without verified local-artifact mode' >&2
exit 91
EOF
chmod +x "$tmp/fake-bin/sh"

export KH_TEST_K3S_INSTALLER="$k3s_installer"
export KH_TEST_RKE2_INSTALLER="$rke2_installer"
export KH_TEST_PAYLOAD_AMD64="$fake_payload_amd64"
export KH_TEST_PAYLOAD_ARM64="$fake_payload_arm64"
export KH_TEST_DOWNLOAD_LOG="$tmp/downloads.log"
export KH_TEST_INSTALL_LOG="$tmp/install.log"
export KH_TEST_SELINUX_LOG="$tmp/selinux.log"
export KH_TEST_SERVICE_LOG="$tmp/service.log"
export KH_TEST_EXPECTED_PAYLOAD_SHA="$fake_payload_amd64_sha"
export KH_TEST_CURL_STATE="$tmp/curl-failed-once"
export KH_TEST_UNAME=x86_64
export KH_TEST_ARCH=amd64

run_failure() {
  name=$1
  expected_message=$2
  shift 2
  runner="$tmp/installer-$name"
  cp "$source_script" "$runner"
  chmod +x "$runner"

  if PATH="${KH_TEST_PATH:-$tmp/fake-bin:$PATH}" "$runner" "$@" >"$tmp/$name.out" 2>&1; then
    echo "expected $name to fail" >&2
    exit 1
  fi
  grep -Fq "$expected_message" "$tmp/$name.out"
}

run_success() {
  name=$1
  shift
  runner="$tmp/installer-$name"
  cp "$source_script" "$runner"
  chmod +x "$runner"
  if ! PATH="$tmp/fake-bin:$PATH" "$runner" "$@" >"$tmp/$name.out" 2>&1; then
    cat "$tmp/$name.out" >&2
    return 1
  fi
}

rm -f "$KH_TEST_CURL_STATE"
KH_TEST_CURL_FAIL_ONCE='/releases/' run_success verified-k3s-transient-download-retry \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy

for architecture_spec in amd64:x86_64 arm64:aarch64; do
  KH_TEST_ARCH=${architecture_spec%%:*}
  KH_TEST_UNAME=${architecture_spec#*:}
  if [[ $KH_TEST_ARCH == amd64 ]]; then
    KH_TEST_EXPECTED_PAYLOAD_SHA=$fake_payload_amd64_sha
  else
    KH_TEST_EXPECTED_PAYLOAD_SHA=$fake_payload_arm64_sha
  fi
  export KH_TEST_ARCH KH_TEST_UNAME KH_TEST_EXPECTED_PAYLOAD_SHA
  run_success "verified-k3s-local-install-$KH_TEST_ARCH" \
    k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy
  run_success "verified-rke2-local-install-$KH_TEST_ARCH" \
    rke2 'v1.32.5+rke2r1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy

  run_success "official-checksum-k3s-install-$KH_TEST_ARCH" \
    k3s 'v1.36.3+k3s1' '' '' c2VydmVy
  grep -Fq "/sha256sum-$KH_TEST_ARCH.txt" "$KH_TEST_DOWNLOAD_LOG"

  run_success "official-checksum-rke2-install-$KH_TEST_ARCH" \
    rke2 'v1.32.5+rke2r1' '' '' c2VydmVy
  grep -Fq "/sha256sum-$KH_TEST_ARCH.txt" "$KH_TEST_DOWNLOAD_LOG"
done
grep -Fq k3s "$KH_TEST_INSTALL_LOG"
grep -Fq 'install -o root -g root -m 0755' "$KH_TEST_INSTALL_LOG"
grep -Fq rke2 "$KH_TEST_INSTALL_LOG"
grep -Fq '/k3s' "$KH_TEST_DOWNLOAD_LOG"
grep -Fq '/k3s-arm64' "$KH_TEST_DOWNLOAD_LOG"

export KH_TEST_UNAME=x86_64
export KH_TEST_ARCH=amd64
export KH_TEST_EXPECTED_PAYLOAD_SHA="$fake_payload_amd64_sha"

rm -f "$KH_TEST_SELINUX_LOG" "$KH_TEST_SERVICE_LOG"
KH_TEST_SELINUX_STATE=Enforcing KH_K3S_EXTERNAL_NODE_IP=203.0.113.10 KH_TEST_INSTALL_EXEC='agent --node-external-ip=203.0.113.10 --flannel-backend=wireguard-native' \
  run_success verified-k3s-external-install \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" YWdlbnQ=
grep -Fq 'semodule -i /usr/share/selinux/packages/k3s.pp' "$KH_TEST_SELINUX_LOG"
grep -Fq 'chcon -u system_u -r object_r -t container_runtime_exec_t /usr/local/bin/k3s' "$KH_TEST_SELINUX_LOG"
grep -Fq 'systemctl enable --now k3s-agent' "$KH_TEST_SERVICE_LOG"
KH_K3S_EXTERNAL_NODE_IP='<PUBLIC_NODE_IP>' run_failure \
  invalid-k3s-external-ip 'external node IP must contain only IPv4/IPv6 address characters' \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" YWdlbnQ=
KH_TEST_SELINUX_STATE=Enforcing KH_TEST_MISSING_SELINUX_POLICY=true KH_K3S_EXTERNAL_NODE_IP=203.0.113.10 run_failure \
  missing-k3s-external-selinux-policy 'SELinux is active but /usr/share/selinux/packages/k3s.pp is missing' \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" YWdlbnQ=
download_count_before=$(wc -l < "$KH_TEST_DOWNLOAD_LOG")
KH_TEST_PATH="$tmp/no-getenforce-bin" KH_K3S_EXTERNAL_NODE_IP=203.0.113.10 run_failure \
  missing-k3s-external-getenforce 'SELinux is active or configured but getenforce is unavailable' \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" YWdlbnQ=
[[ $(wc -l < "$KH_TEST_DOWNLOAD_LOG") -eq $download_count_before ]]

INSTALL_K3S_BIN_DIR=/tmp/unverified INSTALL_K3S_COMMIT=unverified INSTALL_K3S_NAME=unverified \
  run_success sanitized-k3s-installer-environment \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy
INSTALL_RKE2_COMMIT=unverified INSTALL_RKE2_TAR_PREFIX=/tmp/unverified \
  run_success sanitized-rke2-installer-environment \
  rke2 'v1.32.5+rke2r1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy

export KH_TEST_UNAME=aarch64
export KH_TEST_ARCH=arm64
run_failure wrong-k3s-arm64-digest 'k3s payload SHA-256 mismatch' \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_amd64_sha" c2VydmVy
run_failure wrong-rke2-arm64-digest 'RKE2 payload SHA-256 mismatch' \
  rke2 'v1.32.5+rke2r1' "$fake_payload_amd64_sha" "$fake_payload_amd64_sha" c2VydmVy

export KH_TEST_UNAME=x86_64
export KH_TEST_ARCH=amd64
run_failure malformed-k3s-amd64-digest 'invalid pinned SHA-256 for k3s v1.36.3+k3s1 on amd64' \
  k3s 'v1.36.3+k3s1' invalid "$fake_payload_arm64_sha" c2VydmVy
run_failure zero-rke2-amd64-digest 'refusing an all-zero SHA-256 pin for rke2 v1.32.5+rke2r1 on amd64' \
  rke2 'v1.32.5+rke2r1' 0000000000000000000000000000000000000000000000000000000000000000 "$fake_payload_arm64_sha" c2VydmVy

KH_TEST_CHECKSUM_SHA="$wrong_payload_sha" run_failure changed-official-k3s-checksum \
  'k3s payload SHA-256 mismatch' \
  k3s 'v1.36.3+k3s1' '' '' c2VydmVy
KH_TEST_CHECKSUM_SHA="$wrong_payload_sha" run_failure changed-official-rke2-checksum \
  'RKE2 payload SHA-256 mismatch' \
  rke2 'v1.32.5+rke2r1' '' '' c2VydmVy

cp "$k3s_installer" "$tmp/k3s-install-changed.sh"
printf '%s\n' '# changed in transit' >> "$tmp/k3s-install-changed.sh"
KH_TEST_K3S_INSTALLER="$tmp/k3s-install-changed.sh" run_failure \
  changed-k3s-installer 'k3s installer SHA-256 mismatch' \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy

cp "$rke2_installer" "$tmp/rke2-install-changed.sh"
printf '%s\n' '# changed in transit' >> "$tmp/rke2-install-changed.sh"
KH_TEST_RKE2_INSTALLER="$tmp/rke2-install-changed.sh" run_failure \
  changed-rke2-installer 'rke2 installer SHA-256 mismatch' \
  rke2 'v1.32.5+rke2r1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy

rm -f "$KH_TEST_DOWNLOAD_LOG"
run_failure replaced-k3s-payload 'k3s payload SHA-256 mismatch' \
  k3s 'v1.36.3+k3s1' "$wrong_payload_sha" "$wrong_payload_sha" c2VydmVy
if grep -q sha256sum "$KH_TEST_DOWNLOAD_LOG"; then
  echo "verifier unexpectedly downloaded an upstream checksum" >&2
  exit 1
fi

rm -f "$KH_TEST_DOWNLOAD_LOG"
run_failure replaced-rke2-payload 'RKE2 payload SHA-256 mismatch' \
  rke2 'v1.32.5+rke2r1' "$wrong_payload_sha" "$wrong_payload_sha" c2VydmVy
if grep -q sha256sum "$KH_TEST_DOWNLOAD_LOG"; then
  echo "verifier unexpectedly downloaded an upstream checksum" >&2
  exit 1
fi

# Artifacts preloaded into the snapshot replace the downloads only when they
# match the pins; anything else falls back to the verified download path.
export KH_TEST_UNAME=x86_64
export KH_TEST_ARCH=amd64
export KH_TEST_EXPECTED_PAYLOAD_SHA="$fake_payload_amd64_sha"
preload_version_dir="$tmp/preload/k3s/v1.36.3+k3s1"
mkdir -p "$preload_version_dir"
cp "$k3s_installer" "$preload_version_dir/install.sh"
cp "$fake_payload_amd64" "$preload_version_dir/k3s"

rm -f "$KH_TEST_DOWNLOAD_LOG"
KH_KUBERNETES_PRELOAD_DIR="$tmp/preload" run_success preloaded-k3s-install \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy
if [[ -e $KH_TEST_DOWNLOAD_LOG ]]; then
  echo "preloaded k3s install still downloaded: $(cat "$KH_TEST_DOWNLOAD_LOG")" >&2
  exit 1
fi
grep -Fq 'Using the preloaded k3s installer' "$tmp/preloaded-k3s-install.out"
grep -Fq 'Using the preloaded k3s payload' "$tmp/preloaded-k3s-install.out"

printf '%s\n' 'stale preloaded payload' > "$preload_version_dir/k3s"
rm -f "$KH_TEST_DOWNLOAD_LOG"
KH_KUBERNETES_PRELOAD_DIR="$tmp/preload" run_success stale-preloaded-k3s-payload \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy
grep -Fq 'preloaded k3s payload does not match its pinned SHA-256' "$tmp/stale-preloaded-k3s-payload.out"
grep -Fq '/releases/download/v1.36.3%2Bk3s1/k3s' "$KH_TEST_DOWNLOAD_LOG"
if grep -Fq 'raw.githubusercontent.com' "$KH_TEST_DOWNLOAD_LOG"; then
  echo "a matching preloaded installer was downloaded again" >&2
  exit 1
fi

cp "$fake_payload_amd64" "$preload_version_dir/k3s"
rm -f "$KH_TEST_DOWNLOAD_LOG"
KH_KUBERNETES_PRELOAD_DIR="$tmp/preload" run_success unpinned-k3s-ignores-preload \
  k3s 'v1.36.3+k3s1' '' '' c2VydmVy
grep -Fq '/sha256sum-amd64.txt' "$KH_TEST_DOWNLOAD_LOG"
grep -Fq '/releases/download/v1.36.3%2Bk3s1/k3s' "$KH_TEST_DOWNLOAD_LOG"

rm -f "$preload_version_dir/install.sh"
ln -s "$k3s_installer" "$preload_version_dir/install.sh"
rm -f "$KH_TEST_DOWNLOAD_LOG"
KH_KUBERNETES_PRELOAD_DIR="$tmp/preload" run_success symlinked-preloaded-installer \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy
grep -Fq 'raw.githubusercontent.com/k3s-io/k3s/' "$KH_TEST_DOWNLOAD_LOG"

# A newer patch release of the pinned minor, preloaded with a manifest that
# its payload matches, replaces the pinned version without any download.
# Every other preload leaves the pinned path exactly as above.
fake_head_amd64="$tmp/head-payload-amd64"
fake_head_arm64="$tmp/head-payload-arm64"
printf '%s\n' 'channel head amd64 payload' > "$fake_head_amd64"
printf '%s\n' 'channel head arm64 payload' > "$fake_head_arm64"
fake_head_amd64_sha=$(sha256sum "$fake_head_amd64" | awk '{print $1}')
fake_head_arm64_sha=$(sha256sum "$fake_head_arm64" | awk '{print $1}')
export KH_TEST_INSTALLED_BIN="$tmp/installed-k3s"

# write_preload <root> <version> <asset> <payload> [manifest lines]
write_preload() {
  local dir="$1/k3s/$2"
  mkdir -p "$dir"
  cp "$k3s_installer" "$dir/install.sh"
  cp "$4" "$dir/$3"
  if [[ $# -ge 5 ]]; then
    printf '%s' "$5" > "$dir/manifest"
  else
    printf 'version=%s\nasset=%s\nsha256=%s\n' "$2" "$3" "$(sha256sum "$4" | awk '{print $1}')" > "$dir/manifest"
  fi
}

# run_head_case <name> <root> <expected version> <expected payload sha> <installer args...>
run_head_case() {
  name=$1 root=$2 expected_version=$3 expected_sha=$4
  shift 4
  rm -f "$KH_TEST_DOWNLOAD_LOG" "$KH_TEST_INSTALLED_BIN"
  KH_KUBERNETES_PRELOAD_DIR="$root" KH_TEST_INSTALL_VERSION="$expected_version" run_success "$name" "$@"
  if [[ -e $KH_TEST_DOWNLOAD_LOG ]]; then
    echo "$name downloaded: $(cat "$KH_TEST_DOWNLOAD_LOG")" >&2
    exit 1
  fi
  [[ $(sha256sum "$KH_TEST_INSTALLED_BIN" | awk '{print $1}') == "$expected_sha" ]] \
    || { echo "$name installed another payload" >&2; exit 1; }
  grep -Fq "Using the preloaded k3s payload from $root/k3s/$expected_version/" "$tmp/$name.out"
}

export KH_TEST_UNAME=x86_64
export KH_TEST_ARCH=amd64

root="$tmp/preload-head"
write_preload "$root" 'v1.36.3+k3s1' k3s "$fake_payload_amd64"
rm -f "$root/k3s/v1.36.3+k3s1/manifest"
write_preload "$root" 'v1.36.5+k3s1' k3s "$fake_head_amd64"
run_head_case newer-preload "$root" 'v1.36.5+k3s1' "$fake_head_amd64_sha" \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy
grep -Fq 'Installing preloaded k3s v1.36.5+k3s1, newer than v1.36.3+k3s1' "$tmp/newer-preload.out"
grep -Fq 'Using the preloaded k3s installer' "$tmp/newer-preload.out"

# The pins do not matter for the newer preload: its manifest is the digest.
run_head_case newer-preload-without-pin "$root" 'v1.36.5+k3s1' "$fake_head_amd64_sha" \
  k3s 'v1.36.3+k3s1' '' '' c2VydmVy

# A pinned pre-release is older than the release of the same patch.
run_head_case newer-preload-over-prerelease "$root" 'v1.36.5+k3s1' "$fake_head_amd64_sha" \
  k3s 'v1.36.5-rc1+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy

# The newest wins, compared numerically (10 > 9), and k3s revisions count.
root="$tmp/preload-several"
write_preload "$root" 'v1.36.4+k3s1' k3s "$fake_payload_amd64"
write_preload "$root" 'v1.36.9+k3s2' k3s "$fake_payload_amd64"
write_preload "$root" 'v1.36.10+k3s1' k3s "$fake_head_amd64"
run_head_case newest-preload "$root" 'v1.36.10+k3s1' "$fake_head_amd64_sha" \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy
root="$tmp/preload-revision"
write_preload "$root" 'v1.36.3+k3s2' k3s "$fake_head_amd64"
run_head_case newer-k3s-revision "$root" 'v1.36.3+k3s2' "$fake_head_amd64_sha" \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy

export KH_TEST_UNAME=aarch64
export KH_TEST_ARCH=arm64
root="$tmp/preload-arm64"
write_preload "$root" 'v1.36.5+k3s1' k3s-arm64 "$fake_head_arm64"
run_head_case newer-preload-arm64 "$root" 'v1.36.5+k3s1' "$fake_head_arm64_sha" \
  k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy

export KH_TEST_UNAME=x86_64
export KH_TEST_ARCH=amd64

# Each case next to the exact preload of the pinned version (written by an
# older template, without a manifest): the node installs the pinned version
# from that preload, as before.
head_sha=$fake_head_amd64_sha
zero_sha=0000000000000000000000000000000000000000000000000000000000000000
for spec in \
  'other-minor|v1.37.0+k3s1|k3s|' \
  'older-patch|v1.36.2+k3s1|k3s|' \
  'same-version|v1.36.3+k3s1|k3s|' \
  'prerelease|v1.36.6-rc1+k3s1|k3s|' \
  "payload-mismatch|v1.36.5+k3s1|k3s|version=v1.36.5+k3s1;asset=k3s;sha256=$fake_payload_amd64_sha" \
  "zero-digest|v1.36.5+k3s1|k3s|version=v1.36.5+k3s1;asset=k3s;sha256=$zero_sha" \
  "uppercase-digest|v1.36.5+k3s1|k3s|version=v1.36.5+k3s1;asset=k3s;sha256=$(tr a-f A-F <<< "$head_sha")" \
  "other-version|v1.36.5+k3s1|k3s|version=v1.36.6+k3s1;asset=k3s;sha256=$head_sha" \
  "other-asset|v1.36.5+k3s1|k3s|version=v1.36.5+k3s1;asset=k3s-arm64;sha256=$head_sha" \
  "duplicate-digest|v1.36.5+k3s1|k3s|version=v1.36.5+k3s1;asset=k3s;sha256=$head_sha;sha256=$head_sha" \
  'missing-manifest|v1.36.5+k3s1|k3s|-' \
  'arm64-payload-only|v1.36.5+k3s1|k3s-arm64|' \
  'symlinked-payload|v1.36.5+k3s1|k3s|' \
  'symlinked-manifest|v1.36.5+k3s1|k3s|'; do
  IFS='|' read -r case_name case_version case_asset case_manifest <<< "$spec"
  root="$tmp/preload-ignored-$case_name"
  write_preload "$root" 'v1.36.3+k3s1' k3s "$fake_payload_amd64"
  rm -f "$root/k3s/v1.36.3+k3s1/manifest"
  case_payload=$fake_head_amd64
  [[ $case_name != same-version ]] || case_payload=$fake_payload_amd64
  if [[ -z $case_manifest ]]; then
    write_preload "$root" "$case_version" "$case_asset" "$case_payload"
  else
    write_preload "$root" "$case_version" "$case_asset" "$case_payload" "$(tr ';' '\n' <<< "$case_manifest")"$'\n'
  fi
  case_dir="$root/k3s/$case_version"
  case $case_name in
    missing-manifest) rm -f "$case_dir/manifest" ;;
    symlinked-payload) mv "$case_dir/k3s" "$tmp/$case_name-payload" && ln -s "$tmp/$case_name-payload" "$case_dir/k3s" ;;
    symlinked-manifest) mv "$case_dir/manifest" "$tmp/$case_name-manifest" && ln -s "$tmp/$case_name-manifest" "$case_dir/manifest" ;;
  esac
  run_head_case "ignored-$case_name" "$root" 'v1.36.3+k3s1' "$fake_payload_amd64_sha" \
    k3s 'v1.36.3+k3s1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy
  grep -Fq 'No preloaded k3s newer than v1.36.3+k3s1' "$tmp/ignored-$case_name.out" \
    || { echo "ignored-$case_name: $(cat "$tmp/ignored-$case_name.out")" >&2; exit 1; }
  case $case_name in
    other-minor | older-patch | same-version | prerelease) ;;
    *) grep -Fq "WARNING: ignoring preloaded k3s $case_version: bad manifest or payload" "$tmp/ignored-$case_name.out" ;;
  esac
done

# Without a pin and without a newer preload, the exact preload is still not
# trusted on its manifest alone: the release checksum decides, as before.
root="$tmp/preload-unpinned-same"
write_preload "$root" 'v1.36.3+k3s1' k3s "$fake_payload_amd64"
rm -f "$KH_TEST_DOWNLOAD_LOG"
KH_KUBERNETES_PRELOAD_DIR="$root" run_success unpinned-same-version-preload \
  k3s 'v1.36.3+k3s1' '' '' c2VydmVy
grep -Fq '/sha256sum-amd64.txt' "$KH_TEST_DOWNLOAD_LOG"
grep -Fq '/releases/download/v1.36.3%2Bk3s1/k3s' "$KH_TEST_DOWNLOAD_LOG"

# RKE2 never looks at a k3s preload.
root="$tmp/preload-rke2"
write_preload "$root" 'v1.32.6+k3s1' k3s "$fake_head_amd64"
rm -f "$KH_TEST_DOWNLOAD_LOG"
KH_KUBERNETES_PRELOAD_DIR="$root" run_success rke2-ignores-k3s-preload \
  rke2 'v1.32.5+rke2r1' "$fake_payload_amd64_sha" "$fake_payload_arm64_sha" c2VydmVy
grep -Fq '/releases/download/v1.32.5%2Brke2r1/rke2.linux-amd64.tar.gz' "$KH_TEST_DOWNLOAD_LOG"
unset KH_TEST_INSTALLED_BIN

if rg -n 'get\.k3s\.io|get\.rke2\.io|curl[^\n]*\|[^\n]*(sh|bash)' \
  "$repo_root/locals.tf" "$repo_root/robot-nodes.tf" "$repo_root/output.tf"; then
  echo "mutable curl-pipe installer path remains" >&2
  exit 1
fi

echo "verified Kubernetes installer tamper tests passed"
