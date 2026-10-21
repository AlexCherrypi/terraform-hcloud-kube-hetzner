resource "random_string" "server" {
  length  = 3
  lower   = true
  special = false
  numeric = false
  upper   = false

  keepers = {
    # We re-create the apart of the name changes.
    name = var.name
  }
}

variable "network" {
  type = object({
    network_id = number
    ip         = string
    alias_ips  = list(string)
  })
  default = null
}

resource "hcloud_server" "server" {
  name               = local.name
  image              = var.os_snapshot_id
  server_type        = var.server_type
  location           = var.location
  ssh_keys           = var.ssh_keys
  firewall_ids       = local.effective_firewall_ids
  placement_group_id = var.placement_group_id
  backups            = var.backups
  delete_protection  = var.delete_protection
  rebuild_protection = var.delete_protection
  user_data          = data.cloudinit_config.config.rendered
  keep_disk          = var.keep_disk_size
  public_net {
    ipv4_enabled = !var.disable_ipv4
    ipv4         = var.disable_ipv4 ? null : var.primary_ipv4_id
    ipv6_enabled = !var.disable_ipv6
    ipv6         = var.disable_ipv6 ? null : var.primary_ipv6_id
  }

  dynamic "network" {
    for_each = var.private_ipv4 == null ? [1] : []
    content {
      network_id = var.network_id
      alias_ips  = []
    }
  }

  dynamic "network" {
    for_each = var.private_ipv4 == null ? [] : [1]
    content {
      network_id = var.network_id
      ip         = var.private_ipv4
      alias_ips  = []
    }
  }

  labels = var.labels

  # Prevent destroying the whole cluster if the user changes
  # any of the attributes that force to recreate the servers.
  lifecycle {
    ignore_changes = [
      location,
      ssh_keys,
      user_data,
      image,
    ]

    precondition {
      condition     = length(local.effective_firewall_ids) <= 5
      error_message = "Hetzner Cloud supports at most five firewalls per server."
    }

    precondition {
      condition     = alltrue([for firewall_id in local.effective_firewall_ids : firewall_id > 0 && firewall_id == floor(firewall_id)])
      error_message = "Firewall IDs must be positive integers."
    }

    precondition {
      condition     = length(data.cloudinit_config.config.rendered) <= 32768
      error_message = "Static node cloud-init user_data exceeds Hetzner Cloud's 32 KiB API limit. Reduce extra_write_files, extra_runcmd, registries_config, kubelet_config, or other embedded node configuration."
    }
  }

}

resource "terraform_data" "initial_readiness" {
  triggers_replace = {
    server_id = hcloud_server.server.id
  }

  connection {
    user           = "root"
    private_key    = var.ssh_private_key
    agent_identity = local.ssh_agent_identity
    host           = local.provisioner_connection_host
    port           = var.ssh_port

    bastion_host        = var.ssh_bastion.bastion_host
    bastion_port        = var.ssh_bastion.bastion_port
    bastion_user        = var.ssh_bastion.bastion_user
    bastion_private_key = var.ssh_bastion.bastion_private_key

    timeout = "10m"
  }

  provisioner "remote-exec" {
    inline = [
      "echo 'Waiting for system to become fully ready...'",

      # Wait until the system is fully booted. Leap Micro can leave
      # transactional-update.service failed after the first-boot update race;
      # that unit is non-critical here and is managed explicitly later.
      <<-EOT
      timeout 600 bash <<'EOF'
      cloud_final_diagnostics_shown=false
      while true; do
        state="$(systemctl is-system-running 2>/dev/null || true)"

        if [ "$state" = "running" ]; then
          exit 0
        fi

        if [ "$state" = "degraded" ]; then
          systemctl reset-failed transactional-update.service >/dev/null 2>&1 || true
          failed_units="$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | grep -vx 'transactional-update.service' || true)"

          if [ -z "$failed_units" ]; then
            exit 0
          fi

          echo "Waiting for system; failed units remain:"
          printf '%s\n' "$failed_units"
          if [ "$cloud_final_diagnostics_shown" = false ] && printf '%s\n' "$failed_units" | grep -qx 'cloud-final.service'; then
            cloud_final_diagnostics_shown=true
            echo "cloud-final.service failed; recent diagnostics follow:"
            systemctl status cloud-final.service --no-pager -l || true
            journalctl -u cloud-final.service -n 80 --no-pager || true
          fi
        else
          echo "Waiting for system... ($state)"
        fi

        sleep 3
      done
      EOF
      EOT
      ,

      "echo 'System is fully ready!'"
    ]
  }
}

# Preserve the historical state address. The parent module reconciles the
# timer after Kubernetes starts, alongside health-checker.service.
resource "terraform_data" "os_upgrade_timer" {
  triggers_replace = {
    server_id                = hcloud_server.server.id
    automatically_upgrade_os = tostring(var.automatically_upgrade_os)
  }
}

resource "hcloud_server_network" "extra_networks" {
  for_each = {
    for network_id in local.extra_network_ids : tostring(network_id) => network_id
  }

  server_id  = hcloud_server.server.id
  network_id = each.value
}

resource "terraform_data" "ssh_authorized_keys" {
  triggers_replace = {
    server_id                     = hcloud_server.server.id
    ssh_public_key                = sha1(var.ssh_public_key)
    ssh_additional_keys           = sha1(join("\n", var.ssh_additional_public_keys))
    ssh_authorized_keys_exclusive = tostring(var.ssh_authorized_keys_exclusive)
    reconciler                    = "identity-v1"
  }

  connection {
    user           = "root"
    private_key    = var.ssh_private_key
    agent_identity = local.ssh_agent_identity
    host           = local.provisioner_connection_host
    port           = var.ssh_port

    bastion_host        = var.ssh_bastion.bastion_host
    bastion_port        = var.ssh_bastion.bastion_port
    bastion_user        = var.ssh_bastion.bastion_user
    bastion_private_key = var.ssh_bastion.bastion_private_key
  }

  provisioner "file" {
    content = format(
      "%s\n",
      join("\n", distinct(compact(concat(
        [trimspace(var.ssh_public_key)],
        [for key in var.ssh_additional_public_keys : trimspace(key)]
      ))))
    )
    destination = "/tmp/authorized_keys"
  }

  provisioner "file" {
    source      = "${path.module}/../../scripts/reconcile-authorized-keys.sh"
    destination = "/tmp/reconcile-authorized-keys.sh"
  }

  provisioner "remote-exec" {
    inline = [
      <<-EOT
      set -eu
      trap 'rm -f /tmp/authorized_keys /tmp/reconcile-authorized-keys.sh' EXIT
      install -m 0755 /tmp/reconcile-authorized-keys.sh /usr/local/sbin/kube-hetzner-reconcile-authorized-keys
      /usr/local/sbin/kube-hetzner-reconcile-authorized-keys \
        /tmp/authorized_keys \
        /root/.ssh/authorized_keys \
        /root/.ssh/authorized_keys.kube-hetzner \
        ${var.ssh_authorized_keys_exclusive}
      EOT
      ,
    ]
  }

  depends_on = [terraform_data.initial_readiness]
}

resource "terraform_data" "registries" {
  triggers_replace = {
    registries = var.registries_config
  }

  connection {
    user           = "root"
    private_key    = var.ssh_private_key
    agent_identity = local.ssh_agent_identity
    host           = local.provisioner_connection_host
    port           = var.ssh_port

    bastion_host        = var.ssh_bastion.bastion_host
    bastion_port        = var.ssh_bastion.bastion_port
    bastion_user        = var.ssh_bastion.bastion_user
    bastion_private_key = var.ssh_bastion.bastion_private_key

  }

  provisioner "file" {
    content     = var.registries_config
    destination = "/tmp/registries.yaml"
  }

  provisioner "remote-exec" {
    inline = [var.registries_update_script]
  }

  # A registries.yaml that references a CA file needs the file first.
  depends_on = [terraform_data.initial_readiness, terraform_data.registries_ca_files]
}
moved {
  from = null_resource.registries
  to   = terraform_data.registries
}

# Separate from terraform_data.registries: a CA-only change rewrites the CA
# files and never restarts Kubernetes.
resource "terraform_data" "registries_ca_files" {
  count = length(var.registries_ca_files) > 0 ? 1 : 0

  triggers_replace = {
    registries_ca_files = var.registries_ca_files
  }

  connection {
    user           = "root"
    private_key    = var.ssh_private_key
    agent_identity = local.ssh_agent_identity
    host           = local.provisioner_connection_host
    port           = var.ssh_port

    bastion_host        = var.ssh_bastion.bastion_host
    bastion_port        = var.ssh_bastion.bastion_port
    bastion_user        = var.ssh_bastion.bastion_user
    bastion_private_key = var.ssh_bastion.bastion_private_key
  }

  provisioner "remote-exec" {
    inline = [var.registries_ca_update_script]
  }

  depends_on = [terraform_data.initial_readiness]
}

# Installs and starts the registries_boot_reject unit; restarts no
# Kubernetes service.
resource "terraform_data" "registries_boot_reject" {
  count = length(var.registries_boot_reject) > 0 ? 1 : 0

  triggers_replace = {
    registries_boot_reject = var.registries_boot_reject
  }

  connection {
    user           = "root"
    private_key    = var.ssh_private_key
    agent_identity = local.ssh_agent_identity
    host           = local.provisioner_connection_host
    port           = var.ssh_port

    bastion_host        = var.ssh_bastion.bastion_host
    bastion_port        = var.ssh_bastion.bastion_port
    bastion_user        = var.ssh_bastion.bastion_user
    bastion_private_key = var.ssh_bastion.bastion_private_key
  }

  provisioner "remote-exec" {
    inline = [var.registries_boot_reject_script]
  }

  depends_on = [terraform_data.initial_readiness]
}

resource "terraform_data" "kubelet_config" {
  count = var.kubelet_config != "" ? 1 : 0

  triggers_replace = {
    kubelet_config = var.kubelet_config
  }

  connection {
    user           = "root"
    private_key    = var.ssh_private_key
    agent_identity = local.ssh_agent_identity
    host           = local.provisioner_connection_host
    port           = var.ssh_port

    bastion_host        = var.ssh_bastion.bastion_host
    bastion_port        = var.ssh_bastion.bastion_port
    bastion_user        = var.ssh_bastion.bastion_user
    bastion_private_key = var.ssh_bastion.bastion_private_key
  }

  provisioner "file" {
    content     = var.kubelet_config
    destination = "/tmp/kubelet-config.yaml"
  }

  provisioner "remote-exec" {
    inline = [var.kubelet_config_update_script]
  }

  depends_on = [terraform_data.initial_readiness]
}
moved {
  from = null_resource.kubelet_config
  to   = terraform_data.kubelet_config
}

resource "terraform_data" "audit_policy" {
  count = var.audit_policy_config != "" ? 1 : 0

  triggers_replace = {
    audit_policy = var.audit_policy_config
  }

  connection {
    user           = "root"
    private_key    = var.ssh_private_key
    agent_identity = local.ssh_agent_identity
    host           = local.provisioner_connection_host
    port           = var.ssh_port

    bastion_host        = var.ssh_bastion.bastion_host
    bastion_port        = var.ssh_bastion.bastion_port
    bastion_user        = var.ssh_bastion.bastion_user
    bastion_private_key = var.ssh_bastion.bastion_private_key
  }

  provisioner "file" {
    content     = var.audit_policy_config
    destination = "/tmp/audit-policy.yaml"
  }

  provisioner "remote-exec" {
    inline = [var.audit_policy_update_script]
  }

  depends_on = [terraform_data.initial_readiness]
}
moved {
  from = null_resource.audit_policy
  to   = terraform_data.audit_policy
}

resource "hcloud_rdns" "server" {
  count = (var.base_domain != "" && !var.disable_ipv4) ? 1 : 0

  server_id  = hcloud_server.server.id
  ip_address = coalesce(hcloud_server.server.ipv4_address, try(one(hcloud_server.server.network).ip, null))
  dns_ptr    = format("%s.%s", local.name, var.base_domain)
}

resource "hcloud_rdns" "server_ipv6" {
  count = (var.base_domain != "" && !var.disable_ipv6) ? 1 : 0

  server_id  = hcloud_server.server.id
  ip_address = hcloud_server.server.ipv6_address
  dns_ptr    = format("%s.%s", local.name, var.base_domain)
}


data "cloudinit_config" "config" {
  gzip          = true
  base64_encode = true

  # Main cloud-config configuration file.
  part {
    filename     = "init.cfg"
    content_type = "text/cloud-config"
    content = templatefile(
      "${path.module}/templates/cloudinit.yaml.tpl",
      {
        hostname                     = local.name
        dns_servers                  = var.dns_servers
        has_dns_servers              = local.has_dns_servers
        sshAuthorizedKeysYaml        = yamlencode(local.ssh_authorized_keys)
        sshAuthorizedKeysContent     = format("%s\n", join("\n", local.ssh_authorized_keys))
        cloudinit_write_files_common = var.cloudinit_write_files_common
        cloudinit_runcmd_common      = var.cloudinit_runcmd_common
        metadata_route_repair_script = var.metadata_route_repair_script
        cloudinit_write_files_extra  = var.cloudinit_write_files_extra
        cloudinit_runcmd_extra       = var.cloudinit_runcmd_extra
        swap_size                    = var.swap_size
        os                           = var.os
        private_ipv4_default_route   = var.disable_ipv4
        public_ipv4_default_route    = !var.disable_ipv4
        public_ipv6_default_route    = !var.disable_ipv6
        network_gw_ipv4              = var.network_gw_ipv4
      }
    )
  }
}

resource "terraform_data" "zram" {
  triggers_replace = {
    zram_size = var.zram_size
  }

  connection {
    user           = "root"
    private_key    = var.ssh_private_key
    agent_identity = local.ssh_agent_identity
    host           = local.provisioner_connection_host
    port           = var.ssh_port

    bastion_host        = var.ssh_bastion.bastion_host
    bastion_port        = var.ssh_bastion.bastion_port
    bastion_user        = var.ssh_bastion.bastion_user
    bastion_private_key = var.ssh_bastion.bastion_private_key

  }

  provisioner "file" {
    content     = <<-EOT
#!/bin/bash

# Switching off swap
swapoff /dev/zram0

rmmod zram
    EOT
    destination = "/usr/local/bin/k3s-swapoff"
  }

  provisioner "file" {
    content     = <<-EOT
#!/bin/bash

# get the amount of memory in the machine
# load the dependency module
modprobe zram

# initialize the device with zstd compression algorithm
echo zstd > /sys/block/zram0/comp_algorithm;
echo ${var.zram_size} > /sys/block/zram0/disksize

# Creating the swap filesystem
mkswap /dev/zram0

# Switch the swaps on
swapon -p 100 /dev/zram0
    EOT
    destination = "/usr/local/bin/k3s-swapon"
  }

  # Setup zram if it's enabled
  provisioner "file" {
    content     = <<-EOT
[Unit]
Description=Swap with zram
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=true
ExecStart=/usr/local/bin/k3s-swapon
ExecStop=/usr/local/bin/k3s-swapoff

[Install]
WantedBy=multi-user.target
    EOT
    destination = "/etc/systemd/system/zram.service"
  }

  provisioner "remote-exec" {
    inline = concat(var.zram_size != "" ? [
      "chmod +x /usr/local/bin/k3s-swapon",
      "chmod +x /usr/local/bin/k3s-swapoff",
      "systemctl disable --now zram.service",
      "systemctl enable --now zram.service",
      ] : [
      "systemctl disable --now zram.service",
    ])
  }

  depends_on = [terraform_data.initial_readiness]
}

moved {
  from = null_resource.zram
  to   = terraform_data.zram
}

# Preserve the historical state address. The parent module owns service
# reconciliation now so it can order the remote action after Kubernetes starts.
resource "terraform_data" "os_upgrade_toggle" {
  triggers_replace = {
    os_upgrade_state     = var.automatically_upgrade_os ? "enabled" : "disabled"
    health_checker_state = "parent-post-install-v2"
    server_id            = hcloud_server.server.id
  }
}

moved {
  from = null_resource.os_upgrade_toggle
  to   = terraform_data.os_upgrade_toggle
}
