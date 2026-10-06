/*
 * Creates a MicroOS snapshot for Kube-Hetzner
 */
packer {
  required_version = "= 1.16.0"

  required_plugins {
    hcloud = {
      version = "= 1.7.2"
      source  = "github.com/hetznercloud/hcloud"
    }
  }
}

variable "hcloud_token" {
  type      = string
  default   = env("HCLOUD_TOKEN")
  sensitive = true
}

# Server type and location used to build each snapshot. Override these when the defaults are
# unavailable in your project (Hetzner availability varies by location and over time), e.g.:
#   packer build -var x86_server_type=cpx31 -var x86_location=fsn1 <template>.pkr.hcl
# To build only one architecture (e.g. no ARM capacity available), use -only with the
# corresponding source name. Any server type works as long as its disk is >= 40GiB.
variable "x86_server_type" {
  type    = string
  default = "cx23"
}

variable "x86_location" {
  type    = string
  default = "nbg1"
}

variable "arm_server_type" {
  type    = string
  default = "cax11"
}

variable "arm_location" {
  type    = string
  default = "fsn1"
}


variable "k3s_selinux_version" {
  type        = string
  default     = "v1.6.stable.1"
  description = "k3s-selinux release tag to install."

  validation {
    condition     = can(regex("^v[0-9]+[.][0-9]+[.](stable|latest|testing)[.][0-9]+$", var.k3s_selinux_version))
    error_message = "The k3s-selinux version must be a release tag such as v1.6.stable.1."
  }
}

variable "k3s_selinux_package_name" {
  type        = string
  default     = ""
  description = "Optional k3s-selinux RPM filename override. If empty, derived from tag using the MicroOS sle package form."

  validation {
    condition     = var.k3s_selinux_package_name == "" || can(regex("^k3s-selinux-[0-9]+[.][0-9]+-[0-9]+[.]sle[.]noarch[.]rpm$", var.k3s_selinux_package_name))
    error_message = "The k3s-selinux package override must be empty or a safe sle noarch RPM filename."
  }
}

variable "k3s_selinux_expected_sha256" {
  type        = string
  default     = ""
  description = "Optional SHA-256 override for the exact k3s-selinux RPM release asset."

  validation {
    condition     = var.k3s_selinux_expected_sha256 == "" || can(regex("^[0-9A-Fa-f]{64}$", var.k3s_selinux_expected_sha256))
    error_message = "The k3s-selinux RPM SHA-256 must be empty or exactly 64 hexadecimal characters."
  }
}

variable "rke2_selinux_version" {
  type        = string
  default     = "v0.22.stable.1"
  description = "rke2-selinux release tag to install."

  validation {
    condition     = can(regex("^v[0-9]+[.][0-9]+[.](stable|latest|testing)[.][0-9]+$", var.rke2_selinux_version))
    error_message = "The rke2-selinux version must be a release tag such as v0.22.stable.1."
  }
}

variable "rke2_selinux_package_name" {
  type        = string
  default     = ""
  description = "Optional rke2-selinux RPM filename override. If empty, derived from tag using the MicroOS sle package form."

  validation {
    condition     = var.rke2_selinux_package_name == "" || can(regex("^rke2-selinux-[0-9]+[.][0-9]+-[0-9]+[.]sle[.]noarch[.]rpm$", var.rke2_selinux_package_name))
    error_message = "The rke2-selinux package override must be empty or a safe sle noarch RPM filename."
  }
}

variable "rke2_selinux_expected_sha256" {
  type        = string
  default     = ""
  description = "Optional SHA-256 override for the exact rke2-selinux RPM release asset."

  validation {
    condition     = var.rke2_selinux_expected_sha256 == "" || can(regex("^[0-9A-Fa-f]{64}$", var.rke2_selinux_expected_sha256))
    error_message = "The rke2-selinux RPM SHA-256 must be empty or exactly 64 hexadecimal characters."
  }
}

variable "selinux_package_to_install" {
  type        = string
  default     = "k3s"
  description = "Which Kubernetes SELinux package variant to preinstall in the snapshot: k3s or rke2."

  validation {
    condition     = contains(["k3s", "rke2"], var.selinux_package_to_install)
    error_message = "The SELinux package variant must be either k3s or rke2."
  }
}

# Empty or the exact historical official URL uses reviewed official mode. Any other URL selects custom-mirror mode.
variable "opensuse_microos_x86_mirror_link" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional x86 appliance URL. Custom URLs require HTTPS, no redirects, explicit signed sidecars, and an independently pinned SHA-256 digest."

}

variable "opensuse_microos_x86_checksum_link" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Explicit x86 SHA-256 sidecar URL. Required together with signature_link in custom-mirror mode."
}

variable "opensuse_microos_x86_signature_link" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Explicit x86 detached-signature sidecar URL. Required together with checksum_link in custom-mirror mode."
}

variable "opensuse_microos_x86_expected_sha256" {
  type        = string
  default     = ""
  description = "Optional official-image digest override. Required and independently reviewed for custom mirrors."

  validation {
    condition     = var.opensuse_microos_x86_expected_sha256 == "" || can(regex("^[0-9A-Fa-f]{64}$", var.opensuse_microos_x86_expected_sha256))
    error_message = "The x86 custom image SHA-256 must be empty or exactly 64 hexadecimal characters."
  }
}

# Empty or the exact historical official URL uses reviewed official mode. Any other URL selects custom-mirror mode.
variable "opensuse_microos_arm_mirror_link" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional ARM appliance URL. Custom URLs require HTTPS, no redirects, explicit signed sidecars, and an independently pinned SHA-256 digest."

}

variable "opensuse_microos_arm_checksum_link" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Explicit ARM SHA-256 sidecar URL. Required together with signature_link in custom-mirror mode."
}

variable "opensuse_microos_arm_signature_link" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Explicit ARM detached-signature sidecar URL. Required together with checksum_link in custom-mirror mode."
}

variable "opensuse_microos_arm_expected_sha256" {
  type        = string
  default     = ""
  description = "Optional official-image digest override. Required and independently reviewed for custom mirrors."

  validation {
    condition     = var.opensuse_microos_arm_expected_sha256 == "" || can(regex("^[0-9A-Fa-f]{64}$", var.opensuse_microos_arm_expected_sha256))
    error_message = "The ARM custom image SHA-256 must be empty or exactly 64 hexadecimal characters."
  }
}

variable "opensuse_microos_x86_mirror_authorization_header" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional Authorization header for x86 custom URLs. When set, image and sidecars must share one HTTPS origin; custom redirects are always rejected."
}

variable "opensuse_microos_arm_mirror_authorization_header" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional Authorization header for ARM custom URLs. When set, image and sidecars must share one HTTPS origin; custom redirects are always rejected."
}

# If you need to add other packages to the OS, do it here in the default value, like ["vim", "curl", "wget"].
variable "packages_to_install" {
  type    = list(string)
  default = []
}

# Timezone to set on the snapshot (e.g., "Europe/Madrid", "UTC", "America/New_York").
variable "timezone" {
  type    = string
  default = "UTC"

  validation {
    condition     = can(regex("^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$", var.timezone))
    error_message = "The timezone must be a safe relative zoneinfo name such as UTC or Europe/Madrid."
  }
}

# Path to a local file containing sysctl settings (one per line, e.g., "vm.swappiness = 10").
# These will be installed to /etc/sysctl.d/99-custom.conf
variable "sysctl_config_file" {
  type    = string
  default = ""
}

# Choose which kernel to use: "default" for the rolling release kernel or "longterm" for LTS kernel.
variable "kernel_type" {
  type    = string
  default = "default"

  validation {
    condition     = contains(["longterm", "default"], var.kernel_type)
    error_message = "The kernel_type must be either longterm or default."
  }
}

# Hooks for automated snapshot pipelines (for example a cluster-side image bot).
# Every default keeps the previous behaviour.

variable "upgrade_packages" {
  type        = bool
  default     = false
  description = "Run transactional-update dup before installing the needed packages, so the snapshot carries the newest packages and kernel of the configured repositories instead of the appliance state."
}

variable "extra_snapshot_labels" {
  type        = map(string)
  default     = {}
  description = "Additional Hetzner labels for the snapshot. Keys of the built-in labels (creator, microos-snapshot, kube-hetzner/*) are rejected."

  validation {
    condition = alltrue([
      for key, value in var.extra_snapshot_labels :
      can(regex("^([a-z0-9]([-a-z0-9]*[a-z0-9])?([.][a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", key))
      && can(regex("^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$", value))
      && !contains(["creator", "microos-snapshot"], key)
      && !can(regex("^kube-hetzner/", key))
    ])
    error_message = "The extra_snapshot_labels must use valid Hetzner label keys and values and must not override the built-in labels."
  }
}

variable "extra_server_labels" {
  type        = map(string)
  default     = {}
  description = "Hetzner labels for the temporary build server and its temporary SSH key, so that an external reaper can recognise the leftovers of an interrupted build."

  validation {
    condition = alltrue([
      for key, value in var.extra_server_labels :
      can(regex("^([a-z0-9]([-a-z0-9]*[a-z0-9])?([.][a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", key))
      && can(regex("^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$", value))
    ])
    error_message = "The extra_server_labels must use valid Hetzner label keys and values."
  }
}

locals {
  snapshot_build_timestamp = formatdate("YYYYMMDD-hhmmss", timestamp())
  snapshot_build_id        = substr(replace(uuidv4(), "-", ""), 0, 12)

  opensuse_microos_official_x86_url            = "https://download.opensuse.org/tumbleweed/appliances/openSUSE-MicroOS.x86_64-ContainerHost-OpenStack-Cloud.qcow2"
  opensuse_microos_official_arm_url            = "https://download.opensuse.org/ports/aarch64/tumbleweed/appliances/openSUSE-MicroOS.aarch64-ContainerHost-OpenStack-Cloud.qcow2"
  opensuse_microos_x86_mirror_link_computed    = var.opensuse_microos_x86_mirror_link != "" ? var.opensuse_microos_x86_mirror_link : local.opensuse_microos_official_x86_url
  opensuse_microos_arm_mirror_link_computed    = var.opensuse_microos_arm_mirror_link != "" ? var.opensuse_microos_arm_mirror_link : local.opensuse_microos_official_arm_url
  opensuse_microos_x86_is_custom               = var.opensuse_microos_x86_mirror_link != "" && var.opensuse_microos_x86_mirror_link != local.opensuse_microos_official_x86_url
  opensuse_microos_arm_is_custom               = var.opensuse_microos_arm_mirror_link != "" && var.opensuse_microos_arm_mirror_link != local.opensuse_microos_official_arm_url
  opensuse_microos_x86_checksum_link_computed  = var.opensuse_microos_x86_checksum_link != "" ? var.opensuse_microos_x86_checksum_link : "${local.opensuse_microos_x86_mirror_link_computed}.sha256"
  opensuse_microos_x86_signature_link_computed = var.opensuse_microos_x86_signature_link != "" ? var.opensuse_microos_x86_signature_link : "${local.opensuse_microos_x86_mirror_link_computed}.sha256.asc"
  opensuse_microos_arm_checksum_link_computed  = var.opensuse_microos_arm_checksum_link != "" ? var.opensuse_microos_arm_checksum_link : "${local.opensuse_microos_arm_mirror_link_computed}.sha256"
  opensuse_microos_arm_signature_link_computed = var.opensuse_microos_arm_signature_link != "" ? var.opensuse_microos_arm_signature_link : "${local.opensuse_microos_arm_mirror_link_computed}.sha256.asc"
  # MicroOS publishes rolling aliases. The signed sidecars authenticate the
  # publisher; these separately reviewed exact-byte pins prevent silent alias
  # movement. A publisher refresh therefore fails closed until explicitly reviewed.
  opensuse_microos_reviewed_x86_sha256          = "5a6fb33ed982d2b0b79878660790fe2537412e9a3769a7cfcbcca3d23010e353"
  opensuse_microos_reviewed_arm_sha256          = "1b57c173579b4b42c7cd0b093e5e6876b3b530883d64cd68a98589bd3aaefa27"
  opensuse_microos_x86_expected_sha256_computed = local.opensuse_microos_x86_is_custom ? lower(var.opensuse_microos_x86_expected_sha256) : var.opensuse_microos_x86_expected_sha256 != "" ? lower(var.opensuse_microos_x86_expected_sha256) : local.opensuse_microos_reviewed_x86_sha256
  opensuse_microos_arm_expected_sha256_computed = local.opensuse_microos_arm_is_custom ? lower(var.opensuse_microos_arm_expected_sha256) : var.opensuse_microos_arm_expected_sha256 != "" ? lower(var.opensuse_microos_arm_expected_sha256) : local.opensuse_microos_reviewed_arm_sha256

  rancher_reviewed_k3s_selinux_sha256 = {
    "v1.6.stable.1" = "aaaf5a0632d77db8c5808c6d1097167c934602639d628526c1ec0bd9cb2dd745"
  }
  rancher_reviewed_rke2_selinux_sha256 = {
    "v0.22.stable.1" = "0c3b1184293a2f47482d6333aa183b91ed9351889925b55760208a37a1f68a39"
  }
  k3s_selinux_expected_sha256_computed  = var.k3s_selinux_expected_sha256 != "" ? lower(var.k3s_selinux_expected_sha256) : lookup(local.rancher_reviewed_k3s_selinux_sha256, var.k3s_selinux_version, "")
  rke2_selinux_expected_sha256_computed = var.rke2_selinux_expected_sha256 != "" ? lower(var.rke2_selinux_expected_sha256) : lookup(local.rancher_reviewed_rke2_selinux_sha256, var.rke2_selinux_version, "")

  # Trust anchors are reviewed and vendored so signature verification remains
  # available to air-gapped builders and cannot silently follow a remote key.
  opensuse_signing_key_fingerprint = "AD485664E901B867051AB15F35A2F86E29B700A4"
  rancher_signing_key_fingerprint  = "C8CFF216455126E9B9C918BE925EA29AE257814A"
  opensuse_signing_key_base64      = filebase64("${path.root}/keys/opensuse-project-signing-key.asc")
  rancher_signing_key_base64       = filebase64("${path.root}/keys/rancher-ci-signing-key.asc")
  image_verifier_base64            = filebase64("${path.root}/scripts/verify-microos-image.sh")
  rancher_rpm_installer_base64     = filebase64("${path.root}/scripts/install-verified-rancher-rpm.sh")
  rancher_rpm_verifier_base64      = filebase64("${path.root}/scripts/verify-rancher-rpm.sh")
  opensuse_microos_x86_image_path  = "/root/openSUSE-MicroOS.x86_64-ContainerHost-OpenStack-Cloud.qcow2"
  opensuse_microos_arm_image_path  = "/root/openSUSE-MicroOS.aarch64-ContainerHost-OpenStack-Cloud.qcow2"

  upgrade_packages_command = var.upgrade_packages ? "transactional-update --continue dup" : "true"

  kernel_package_list = var.kernel_type == "longterm" ? ["kernel-longterm"] : []

  # Preserve the package set previously baked into supported MicroOS snapshots.
  needed_packages = join(" ", concat(local.kernel_package_list, ["restorecond", "policycoreutils", "policycoreutils-python-utils", "setools-console", "audit", "bind-utils", "wireguard-tools", "fuse", "open-iscsi", "nfs-client", "xfsprogs", "cryptsetup", "lvm2", "git", "cifs-utils", "bash-completion", "mtr", "tcpdump", "udica", "qemu-guest-agent"], var.packages_to_install))

  # Read sysctl config if file path is provided, otherwise empty (base64 encoded for safe transfer)
  sysctl_config_content = var.sysctl_config_file != "" ? base64encode(file(var.sysctl_config_file)) : ""

  # Commands to write sysctl config if provided (decode base64)
  sysctl_commands = local.sysctl_config_content != "" ? "echo '${local.sysctl_config_content}' | base64 -d > /etc/sysctl.d/99-custom.conf" : ""

  kernel_switch_commands = var.kernel_type == "longterm" ? join("\n", [
    "zypper rm -y kernel-default",
    "zypper addlock kernel-default",
    "grub2-mkconfig -o /boot/grub2/grub.cfg"
  ]) : "true"

  download_and_verify_image = <<-EOT
    set -eu
    umask 077
    printf '%s' '${local.image_verifier_base64}' | base64 -d > /tmp/verify-microos-image.sh
    printf '%s' '${local.opensuse_signing_key_base64}' | base64 -d > /tmp/opensuse-project-signing-key.asc
    chmod 700 /tmp/verify-microos-image.sh
    OPENSUSE_SIGNING_KEY_FILE=/tmp/opensuse-project-signing-key.asc /tmp/verify-microos-image.sh
    [ -f "$VERIFIED_IMAGE_PATH" ] \
      || { echo "ERROR: signed checksum did not select the canonical appliance pathname" >&2; exit 1; }
  EOT

  # The rolling appliance currently ships cloud-init 25.1.3 with dhcpcd 10.5.2.
  # On Hetzner, dhcpcd obtains a lease but does not return from cloud-init's
  # ephemeral metadata-network command. Select cloud-init's bundled BusyBox
  # udhcpc client before the first boot so metadata discovery can complete.
  prepare_microos_first_boot = <<-EOT
    sync
    blockdev --rereadpt /dev/sda || true
    partprobe /dev/sda || true
    partx -u /dev/sda || true
    udevadm trigger --subsystem-match=block
    udevadm settle

    root_device=""
    for attempt in $(seq 1 30); do
      root_device="$(blkid -L ROOT 2>/dev/null || true)"
      case "$root_device" in
        /dev/sda*) break ;;
        *) root_device="" ;;
      esac
      partprobe /dev/sda || true
      partx -u /dev/sda || true
      udevadm settle
      sleep 1
    done
    [ -b "$root_device" ] \
      || { echo "ERROR: written MicroOS image is missing the ROOT block device" >&2; exit 1; }
    [ "$(blkid -s TYPE -o value "$root_device")" = "btrfs" ] \
      || { echo "ERROR: written MicroOS ROOT filesystem is not Btrfs" >&2; exit 1; }

    mount_dir="$(mktemp -d)"
    root_mounted=0
    local_mounted=0
    cleanup_first_boot_mounts() {
      set +e
      [ "$local_mounted" -eq 0 ] || umount "$mount_dir/usr/local"
      [ "$root_mounted" -eq 0 ] || umount "$mount_dir"
      rmdir "$mount_dir"
    }
    trap cleanup_first_boot_mounts EXIT HUP INT TERM

    mount "$root_device" "$mount_dir"
    root_mounted=1
    [ -d "$mount_dir/etc/cloud/cloud.cfg.d" ] \
      || { echo "ERROR: written MicroOS image is missing cloud-init configuration" >&2; exit 1; }
    [ -x "$mount_dir/usr/bin/busybox" ] \
      || { echo "ERROR: written MicroOS image is missing BusyBox" >&2; exit 1; }
    chroot "$mount_dir" /usr/bin/busybox --list | grep -Fxq udhcpc \
      || { echo "ERROR: written MicroOS BusyBox is missing the udhcpc applet" >&2; exit 1; }

    mount -o subvol=@/usr/local "$root_device" "$mount_dir/usr/local"
    local_mounted=1
    install -d -m 0755 "$mount_dir/usr/local/sbin"
    ln -snf /usr/bin/busybox "$mount_dir/usr/local/sbin/udhcpc"
    printf '%s\n' \
      'system_info:' \
      '  network:' \
      '    dhcp_client_priority: [udhcpc]' \
      > "$mount_dir/etc/cloud/cloud.cfg.d/10-kube-hetzner-dhcp-client.cfg"
    chmod 0644 "$mount_dir/etc/cloud/cloud.cfg.d/10-kube-hetzner-dhcp-client.cfg"
    [ "$(readlink "$mount_dir/usr/local/sbin/udhcpc")" = "/usr/bin/busybox" ] \
      || { echo "ERROR: failed to expose the verified udhcpc applet" >&2; exit 1; }
    grep -Fxq '    dhcp_client_priority: [udhcpc]' \
      "$mount_dir/etc/cloud/cloud.cfg.d/10-kube-hetzner-dhcp-client.cfg" \
      || { echo "ERROR: failed to configure cloud-init's udhcpc priority" >&2; exit 1; }

    sync
    umount "$mount_dir/usr/local"
    local_mounted=0
    umount "$mount_dir"
    root_mounted=0
    rmdir "$mount_dir"
    trap - EXIT HUP INT TERM
  EOT

  write_x86_image = <<-EOT
    set -ex
    echo 'MicroOS image loaded, writing to disk... '
    qemu-img convert -p -f qcow2 -O host_device '${local.opensuse_microos_x86_image_path}' /dev/sda
    ${local.prepare_microos_first_boot}
    echo 'done. Rebooting...'
    sleep 1 && udevadm settle && reboot
  EOT

  write_arm_image = <<-EOT
    set -ex
    echo 'MicroOS image loaded, writing to disk... '
    qemu-img convert -p -f qcow2 -O host_device '${local.opensuse_microos_arm_image_path}' /dev/sda
    ${local.prepare_microos_first_boot}
    echo 'done. Rebooting...'
    sleep 1 && udevadm settle && reboot
  EOT

  install_packages = <<-EOT
    set -ex
    echo "First reboot successful, installing needed packages..."
    ${local.upgrade_packages_command}
    transactional-update --continue pkg install -y ${local.needed_packages}
    transactional-update --continue shell <<-'EOF'
    set -euo pipefail
    set -x

    setenforce 0 || true

    set +x
    printf '%s' '${local.rancher_rpm_installer_base64}' | base64 -d > /var/tmp/install-verified-rancher-rpm.sh
    printf '%s' '${local.rancher_rpm_verifier_base64}' | base64 -d > /var/tmp/verify-rancher-rpm.sh
    printf '%s' '${local.rancher_signing_key_base64}' | base64 -d > /var/tmp/rancher-ci-signing-key.asc
    chmod 700 /var/tmp/install-verified-rancher-rpm.sh /var/tmp/verify-rancher-rpm.sh
    set -x

    derive_rpm_version() {
      local tag="$1"
      echo "$tag" | sed -E 's/^v//; s/\.(stable|latest|testing)\..*$//'
    }

    install_k3s_selinux() {
      # k3s-selinux tag "v1.6.stable.1" => RPM "k3s-selinux-1.6-1.sle.noarch.rpm"
      K3S_TAG="${var.k3s_selinux_version}"
      K3S_RPM_VERSION="$(derive_rpm_version "$K3S_TAG")"
      if [ -z "$K3S_RPM_VERSION" ]; then
        echo "ERROR: failed to derive k3s-selinux RPM version from tag '$K3S_TAG'" >&2
        exit 1
      fi

      K3S_PACKAGE="${var.k3s_selinux_package_name}"
      if [ -z "$K3S_PACKAGE" ]; then
        K3S_PACKAGE="k3s-selinux-$K3S_RPM_VERSION-1.sle.noarch.rpm"
      fi

      K3S_URL="https://github.com/k3s-io/k3s-selinux/releases/download/$K3S_TAG/$K3S_PACKAGE"
      RPM_URL="$K3S_URL" \
      EXPECTED_RELEASE_TAG="$K3S_TAG" \
      EXPECTED_PACKAGE_FILE="$K3S_PACKAGE" \
      EXPECTED_PACKAGE_NAME=k3s-selinux \
      EXPECTED_RPM_SHA256=${local.k3s_selinux_expected_sha256_computed} \
      RANCHER_RPM_VERIFIER_FILE=/var/tmp/verify-rancher-rpm.sh \
      RANCHER_SIGNING_KEY_FILE=/var/tmp/rancher-ci-signing-key.asc \
      RANCHER_SIGNING_KEY_FINGERPRINT=${local.rancher_signing_key_fingerprint} \
        /var/tmp/install-verified-rancher-rpm.sh
      zypper addlock k3s-selinux
    }

    install_rke2_selinux() {
      # rke2-selinux tag "v0.22.stable.1" => RPM "rke2-selinux-0.22-1.sle.noarch.rpm"
      RKE2_TAG="${var.rke2_selinux_version}"
      RKE2_RPM_VERSION="$(derive_rpm_version "$RKE2_TAG")"
      if [ -z "$RKE2_RPM_VERSION" ]; then
        echo "ERROR: failed to derive rke2-selinux RPM version from tag '$RKE2_TAG'" >&2
        exit 1
      fi

      RKE2_PACKAGE="${var.rke2_selinux_package_name}"
      if [ -z "$RKE2_PACKAGE" ]; then
        RKE2_PACKAGE="rke2-selinux-$RKE2_RPM_VERSION-1.sle.noarch.rpm"
      fi

      RKE2_URL="https://github.com/rancher/rke2-selinux/releases/download/$RKE2_TAG/$RKE2_PACKAGE"
      RPM_URL="$RKE2_URL" \
      EXPECTED_RELEASE_TAG="$RKE2_TAG" \
      EXPECTED_PACKAGE_FILE="$RKE2_PACKAGE" \
      EXPECTED_PACKAGE_NAME=rke2-selinux \
      EXPECTED_RPM_SHA256=${local.rke2_selinux_expected_sha256_computed} \
      RANCHER_RPM_VERIFIER_FILE=/var/tmp/verify-rancher-rpm.sh \
      RANCHER_SIGNING_KEY_FILE=/var/tmp/rancher-ci-signing-key.asc \
      RANCHER_SIGNING_KEY_FINGERPRINT=${local.rancher_signing_key_fingerprint} \
        /var/tmp/install-verified-rancher-rpm.sh
      zypper addlock rke2-selinux
    }

    case "${var.selinux_package_to_install}" in
      k3s)
        install_k3s_selinux
        ;;
      rke2)
        install_rke2_selinux
        ;;
      *)
        echo "ERROR: invalid selinux_package_to_install='${var.selinux_package_to_install}', expected one of: k3s, rke2" >&2
        exit 1
        ;;
    esac

    rm -f /var/tmp/install-verified-rancher-rpm.sh /var/tmp/verify-rancher-rpm.sh /var/tmp/rancher-ci-signing-key.asc

    restorecon -Rv /etc/selinux/targeted/policy
    restorecon -Rv /var/lib
    setenforce 1 || true

    ${local.sysctl_commands}
    ${local.kernel_switch_commands}
EOF
    sleep 1 && udevadm settle && reboot
  EOT

  verify_baked_selinux_state = <<-EOT
    verify_baked_selinux_package() {
      package_name="$1"
      package_file="$2"
      policy_name="$3"
      policy_file="$4"
      package_stem="$${package_file%.noarch.rpm}"
      version_release="$${package_stem#"$${package_name}"-}"
      expected_version="$${version_release%-*}"
      expected_release="$${version_release##*-}"
      expected_nevra="$${package_name}|0|$${expected_version}|$${expected_release}|noarch"
      actual_nevra="$(rpm -q --queryformat '%%{NAME}|%%{EPOCHNUM}|%%{VERSION}|%%{RELEASE}|%%{ARCH}' "$package_name")"

      [ "$actual_nevra" = "$expected_nevra" ] \
        || { echo "ERROR: baked SELinux package identity changed across the transactional reboot" >&2; exit 1; }
      rpm -V "$package_name" \
        || { echo "ERROR: baked SELinux package files changed across the transactional reboot" >&2; exit 1; }
      [ -s "$policy_file" ] \
        || { echo "ERROR: baked SELinux policy payload is missing after the transactional reboot" >&2; exit 1; }
      semodule -l | awk '{ print $1 }' | grep -qx "$policy_name" \
        || { echo "ERROR: baked SELinux policy is not loaded after the transactional reboot" >&2; exit 1; }
      [ "$(getenforce)" = "Enforcing" ] \
        || { echo "ERROR: SELinux is not enforcing after the transactional reboot" >&2; exit 1; }
    }

    case "${var.selinux_package_to_install}" in
      k3s)
        K3S_TAG="${var.k3s_selinux_version}"
        K3S_RPM_VERSION="$(echo "$K3S_TAG" | sed -E 's/^v//; s/\.(stable|latest|testing)\..*$//')"
        K3S_PACKAGE="${var.k3s_selinux_package_name}"
        if [ -z "$K3S_PACKAGE" ]; then
          K3S_PACKAGE="k3s-selinux-$K3S_RPM_VERSION-1.sle.noarch.rpm"
        fi
        verify_baked_selinux_package k3s-selinux "$K3S_PACKAGE" k3s /usr/share/selinux/packages/k3s.pp
        ;;
      rke2)
        RKE2_TAG="${var.rke2_selinux_version}"
        RKE2_RPM_VERSION="$(echo "$RKE2_TAG" | sed -E 's/^v//; s/\.(stable|latest|testing)\..*$//')"
        RKE2_PACKAGE="${var.rke2_selinux_package_name}"
        if [ -z "$RKE2_PACKAGE" ]; then
          RKE2_PACKAGE="rke2-selinux-$RKE2_RPM_VERSION-1.sle.noarch.rpm"
        fi
        verify_baked_selinux_package rke2-selinux "$RKE2_PACKAGE" rke2 /usr/share/selinux/packages/rke2.pp
        ;;
      *)
        echo "ERROR: invalid selinux_package_to_install='${var.selinux_package_to_install}' during verification" >&2
        exit 1
        ;;
    esac
  EOT

  finalize_snapshot = <<-EOT
    set -ex
    echo "Second reboot successful, verifying the baked SELinux state before final transactional cleanup..."
    ${local.verify_baked_selinux_state}

    transactional-update --continue shell <<-'EOF'
    set -eux

    rm -f /etc/ssh/ssh_host_*

    install -d -m 0755 /etc/NetworkManager
    [ ! -L /etc/NetworkManager/NetworkManager.conf ] \
      || { echo "ERROR: NetworkManager configuration path must not be a symbolic link" >&2; exit 1; }
    if [ ! -f /etc/NetworkManager/NetworkManager.conf ]; then
      install -m 0644 /dev/null /etc/NetworkManager/NetworkManager.conf
    fi

    timezone='${var.timezone}'
    zoneinfo="/usr/share/zoneinfo/$timezone"
    [ -e "$zoneinfo" ] \
      || { echo "ERROR: configured timezone is not present in the MicroOS zoneinfo database" >&2; exit 1; }
    resolved_zone="$(readlink -f -- "$zoneinfo")"
    case "$resolved_zone" in
      /usr/share/zoneinfo/*) ;;
      *) echo "ERROR: configured timezone resolves outside the zoneinfo database" >&2; exit 1 ;;
    esac
    ln -snf "$zoneinfo" /etc/localtime
    printf '%s\n' "$timezone" > /etc/timezone

    if find /etc/ssh -maxdepth 1 -name 'ssh_host_*' -print -quit | grep -q .; then
      echo "ERROR: SSH host keys were not removed from the committed snapshot" >&2
      exit 1
    fi
    [ -f /etc/NetworkManager/NetworkManager.conf ] && [ ! -L /etc/NetworkManager/NetworkManager.conf ] \
      || { echo "ERROR: NetworkManager configuration is missing from the committed snapshot" >&2; exit 1; }

    expected_zone="$(readlink -f -- "/usr/share/zoneinfo/$timezone")"
    persisted_zone="$(readlink -f -- /etc/localtime)"
    [ -n "$expected_zone" ] && [ "$persisted_zone" = "$expected_zone" ] \
      || { echo "ERROR: timezone symlink is missing from the committed snapshot" >&2; exit 1; }
    [ -f /etc/timezone ] && [ "$(cat /etc/timezone)" = "$timezone" ] \
      || { echo "ERROR: timezone name is missing from the committed snapshot" >&2; exit 1; }
EOF

    # /root is a persistent btrfs subvolume, outside the transactional root.
    # Remove Packer/Hetzner bootstrap keys on the live filesystem so they are
    # not inherited by every server created from the image snapshot.
    rm -f /root/.ssh/authorized_keys /root/.ssh/authorized_keys.kube-hetzner
    if [ -e /root/.ssh/authorized_keys ] || [ -e /root/.ssh/authorized_keys.kube-hetzner ]; then
      echo "ERROR: SSH authorized keys remain in the persistent root subvolume" >&2
      exit 1
    fi
    sleep 1 && udevadm settle
  EOT
}

# Source for the MicroOS x86 snapshot
source "hcloud" "microos-x86-snapshot" {
  image       = "ubuntu-24.04"
  rescue      = "linux64"
  location    = var.x86_location
  server_type = var.x86_server_type # disk size of >= 40GiB is needed to install the MicroOS image

  server_labels   = var.extra_server_labels
  ssh_keys_labels = var.extra_server_labels
  snapshot_labels = merge(var.extra_snapshot_labels, {
    microos-snapshot          = "yes"
    creator                   = "kube-hetzner"
    "kube-hetzner/os"         = "microos"
    "kube-hetzner/arch"       = "x86"
    "kube-hetzner/built-at"   = local.snapshot_build_timestamp
    "kube-hetzner/build-id"   = local.snapshot_build_id
    "kube-hetzner/k8s-distro" = var.selinux_package_to_install
  })
  snapshot_name = "OpenSUSE MicroOS x86 ${upper(var.selinux_package_to_install)} ${local.snapshot_build_timestamp}-${local.snapshot_build_id} by Kube-Hetzner"
  ssh_username  = "root"
  token         = var.hcloud_token
}

# Source for the MicroOS ARM snapshot
source "hcloud" "microos-arm-snapshot" {
  image       = "ubuntu-24.04"
  rescue      = "linux64"
  location    = var.arm_location
  server_type = var.arm_server_type # disk size of >= 40GiB is needed to install the MicroOS image

  server_labels   = var.extra_server_labels
  ssh_keys_labels = var.extra_server_labels
  snapshot_labels = merge(var.extra_snapshot_labels, {
    microos-snapshot          = "yes"
    creator                   = "kube-hetzner"
    "kube-hetzner/os"         = "microos"
    "kube-hetzner/arch"       = "arm"
    "kube-hetzner/built-at"   = local.snapshot_build_timestamp
    "kube-hetzner/build-id"   = local.snapshot_build_id
    "kube-hetzner/k8s-distro" = var.selinux_package_to_install
  })
  snapshot_name = "OpenSUSE MicroOS ARM ${upper(var.selinux_package_to_install)} ${local.snapshot_build_timestamp}-${local.snapshot_build_id} by Kube-Hetzner"
  ssh_username  = "root"
  token         = var.hcloud_token
}

# Build the MicroOS x86 snapshot
build {
  sources = ["source.hcloud.microos-x86-snapshot"]

  provisioner "shell" {
    environment_vars = [
      "IMAGE_URL=${local.opensuse_microos_x86_mirror_link_computed}",
      "CHECKSUM_URL=${local.opensuse_microos_x86_checksum_link_computed}",
      "SIGNATURE_URL=${local.opensuse_microos_x86_signature_link_computed}",
      "EXPECTED_IMAGE_ARCH=x86_64",
      "EXPECTED_IMAGE_SHA256=${local.opensuse_microos_x86_expected_sha256_computed}",
      "CUSTOM_IMAGE=${local.opensuse_microos_x86_is_custom ? 1 : 0}",
      "SIDECAR_URLS_EXPLICIT=${var.opensuse_microos_x86_checksum_link != "" && var.opensuse_microos_x86_signature_link != "" ? 1 : 0}",
      "MIRROR_AUTHORIZATION_HEADER=${var.opensuse_microos_x86_mirror_authorization_header}",
      "OPENSUSE_SIGNING_KEY_FINGERPRINT=${local.opensuse_signing_key_fingerprint}",
      "VERIFIED_IMAGE_PATH=${local.opensuse_microos_x86_image_path}",
    ]
    use_env_var_file = true
    inline           = [local.download_and_verify_image]
  }

  provisioner "shell" {
    inline            = [local.write_x86_image]
    expect_disconnect = true
  }

  provisioner "shell" {
    pause_before      = "5s"
    inline            = [local.install_packages]
    expect_disconnect = true
  }

  provisioner "shell" {
    pause_before = "5s"
    inline       = [local.finalize_snapshot]
  }
}

# Build the MicroOS ARM snapshot
build {
  sources = ["source.hcloud.microos-arm-snapshot"]

  provisioner "shell" {
    environment_vars = [
      "IMAGE_URL=${local.opensuse_microos_arm_mirror_link_computed}",
      "CHECKSUM_URL=${local.opensuse_microos_arm_checksum_link_computed}",
      "SIGNATURE_URL=${local.opensuse_microos_arm_signature_link_computed}",
      "EXPECTED_IMAGE_ARCH=aarch64",
      "EXPECTED_IMAGE_SHA256=${local.opensuse_microos_arm_expected_sha256_computed}",
      "CUSTOM_IMAGE=${local.opensuse_microos_arm_is_custom ? 1 : 0}",
      "SIDECAR_URLS_EXPLICIT=${var.opensuse_microos_arm_checksum_link != "" && var.opensuse_microos_arm_signature_link != "" ? 1 : 0}",
      "MIRROR_AUTHORIZATION_HEADER=${var.opensuse_microos_arm_mirror_authorization_header}",
      "OPENSUSE_SIGNING_KEY_FINGERPRINT=${local.opensuse_signing_key_fingerprint}",
      "VERIFIED_IMAGE_PATH=${local.opensuse_microos_arm_image_path}",
    ]
    use_env_var_file = true
    inline           = [local.download_and_verify_image]
  }

  provisioner "shell" {
    inline            = [local.write_arm_image]
    expect_disconnect = true
  }

  provisioner "shell" {
    pause_before      = "5s"
    inline            = [local.install_packages]
    expect_disconnect = true
  }

  provisioner "shell" {
    pause_before = "5s"
    inline       = [local.finalize_snapshot]
  }
}
