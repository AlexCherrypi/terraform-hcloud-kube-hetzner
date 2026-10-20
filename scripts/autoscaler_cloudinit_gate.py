#!/usr/bin/env python3
"""Size and structure gate for the autoscaler cloud-init (Hetzner user_data).

Renders the module at a base and a head revision and compares every autoscaler
pool's cloudInit, the exact string the cluster autoscaler hands to Hetzner as
server user_data (32 KiB limit). Rendering is real: `tofu test` (or
`terraform test`) plans the whole module with mocked hcloud, http, random,
ssh, kubernetes and helm providers, dummy inputs and the real cloudinit
provider, so the cloudInit comes from the module's own
`data.cloudinit_config` blocks and `autoscaler.yaml` template. All autoscaler
pools must share one OS, so each revision is rendered once with MicroOS and
once with Leap Micro pools. Nothing talks to Hetzner and no credential is
needed; init needs the module's providers plus hashicorp/external (use
--plugin-dir for an offline init). --cli terraform adds override_during =
plan to the mocks (Terraform >= 1.11).

The checks describe one change: the verified Kubernetes installer leaves
install-k8s-agent.sh for its own gzip+base64 write_files entry, and
/root/k8s_custom_policies.te is only written on Leap Micro pools. For every
k3s pool the gate is GREEN only if

  * write_files differ in exactly these two entries, every other entry and
    every other cloud-config key are byte-identical,
  * the new installer entry is gzip+base64, mode 0700, and decodes to the
    bytes of scripts/install-verified-kubernetes.sh,
  * install-k8s-agent.sh differs in the installer line only: same
    environment and arguments, `/bin/sh "$KH_INSTALLER"` on the written path,
    no cleanup trap,
  * cloudInit shrinks by --delta-min..--delta-max bytes and stays at or
    below --max-len (Leap Micro pools keep the policy, so they only need to
    stay within the 32 KiB limit),
  * the autoscaler manifest and config.json change nowhere else, the RKE2
    autoscaler renders are byte-identical, and the install commands of
    control planes, static agents, Robot nodes and the external join helper
    are unchanged.

Counter-tests run after the comparison and must all turn the gate RED: a real
render of the head with the installer entry removed from the template, plus
in-memory edits (entry removed, policy kept, path mismatch, installer bytes
changed, another entry changed), each caught by its own finding.

--registries-config and --extra-write-file only measure: they render the head
again with a registries.yaml (and an extra write_files entry injected into a
scratch copy of the template) and print the per-pool size cost.

--plan-json - runs the same comparison on `tofu show -json <plan>` from stdin,
using the before/after autoscaler manifest of configure_autoscaler[0]; the
plan must change nothing but that resource. The plan holds secrets in clear
text: this script prints only paths, counts, lengths and verdicts.

Exit code 0 = GREEN, 1 = RED.
"""

from __future__ import annotations

import argparse
import base64
import copy
import email
import email.policy
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - environment check
    sys.exit("autoscaler_cloudinit_gate.py needs PyYAML (python3 -m pip install pyyaml)")


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SOURCE = "scripts/install-verified-kubernetes.sh"
TEMPLATE = "templates/autoscaler-cloudinit.yaml.tpl"
TE = "/root/k8s_custom_policies.te"
AGENT = "/var/pre_install/install-k8s-agent.sh"
USER_DATA_LIMIT = 32768
CA_ADDRESS_SUFFIX = "terraform_data.configure_autoscaler[0]"

OLD_INSTALLER_LINE = re.compile(
    r"""KH_INSTALLER=\$\(mktemp /tmp/kh-kubernetes-install\.X+\) && trap 'rm -f "\$KH_INSTALLER"' EXIT"""
    r""" && printf '%s' '(?P<b64>[A-Za-z0-9+/=]+)' \| base64 --decode > "\$KH_INSTALLER\""""
    r""" && chmod 0700 "\$KH_INSTALLER" && (?P<call>.*)$"""
)
NEW_INSTALLER_LINE = re.compile(r"""KH_INSTALLER=(?P<path>/\S+) && (?P<call>.*)$""")
INSTALLER_CALL = re.compile(
    r"""^(?P<env>(?:[A-Z0-9_]+=\S+ )*)(?P<sh>/bin/sh )?"\$KH_INSTALLER" (?P<args>.*)$"""
)
INSTALLER_BLOCK = re.compile(
    r"""%\{ if verified_installer_script != "" ~\}.*?%\{ endif ~\}\n""", re.DOTALL
)

# Dummy inputs shaped like a production cluster: NAT router, control plane
# load balancer, three control planes, an amd64 and an arm64 autoscaler pool
# (one with a taint and kubelet args). All autoscaler pools must share one OS,
# so every tree is rendered twice: MicroOS and Leap Micro.
DUMMY_VARIABLES = """
    hcloud_token    = "kube-hetzner-cloudinit-gate-dummy-hcloud-token-0000000000000000"
    cluster_token   = "kube-hetzner-cloudinit-gate-dummy-cluster-token"
    ssh_public_key  = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKubeHetznerCloudinitGate cloudinit-gate"
    ssh_private_key = null
    cluster_name    = "gate"
    network_region  = "eu-central"
    nat_router = {
      server_type = "cx23"
      location    = "fsn1"
    }
    enable_control_plane_load_balancer = true
    control_plane_nodepools = [
      { name = "control-0", server_type = "cx33", location = "fsn1", labels = [], taints = [], count = 1, os = "microos" },
      { name = "control-1", server_type = "cx33", location = "fsn1", labels = [], taints = [], count = 1, os = "microos" },
      { name = "control-2", server_type = "cx33", location = "fsn1", labels = [], taints = [], count = 1, os = "microos" },
    ]
    agent_nodepools = [
      { name = "placeholder", server_type = "cax11", location = "fsn1", labels = [], taints = [], count = 0, os = "microos" },
    ]
    cluster_autoscaler_version       = "v1.36.1"
    k3s_channel                      = "v1.36"
    automatically_upgrade_kubernetes = true
    automatically_upgrade_os         = true
    preinstall_exec                  = ["timedatectl set-timezone Europe/Berlin"]
    ingress_controller               = "traefik"
    microos_x86_snapshot_id          = "100000001"
    microos_arm_snapshot_id          = "100000002"
    leapmicro_x86_snapshot_id        = "100000003"
    leapmicro_arm_snapshot_id        = "100000004"
"""

AUTOSCALER_NODEPOOLS = """
    autoscaler_nodepools = [
      { name = "worker-cx33", server_type = "cx33", location = "fsn1", min_nodes = 0, max_nodes = 5, os = "OS",
        labels = { "node.kubernetes.io/role" = "worker" }, taints = [] },
      { name = "housekeeping-cax21", server_type = "cax21", location = "fsn1", min_nodes = 0, max_nodes = 4, os = "OS",
        labels = { "node.kubernetes.io/role" = "housekeeping" },
        taints = [{ key = "housekeeping", value = "true", effect = "NoSchedule" }],
        kubelet_args = ["kube-reserved=cpu=50m,memory=300Mi,ephemeral-storage=1Gi", "system-reserved=cpu=250m,memory=300Mi"] },
    ]
"""
VARIANTS = ("microos", "leapmicro")

HCLOUD_RESOURCES = (
    "firewall", "floating_ip", "floating_ip_assignment", "load_balancer", "load_balancer_service",
    "load_balancer_target", "network", "network_route", "network_subnet", "placement_group",
    "primary_ip", "rdns", "server", "ssh_key", "volume",
)
MOCK_KUBECONFIG = (
    "apiVersion: v1\\nclusters:\\n- cluster:\\n    certificate-authority-data: eA==\\n"
    "    server: https://127.0.0.1:6443\\n  name: default\\ncontexts:\\n- context:\\n"
    "    cluster: default\\n    user: default\\n  name: default\\ncurrent-context: default\\n"
    "kind: Config\\nusers:\\n- name: default\\n  user:\\n    client-certificate-data: eA==\\n"
    "    client-key-data: eA==\\n"
)

# Written into scratch copies only. A plan never writes files, so an external
# data source (read at plan time) hands the rendered values to this script.
DUMP_TF = """# Written by scripts/autoscaler_cloudinit_gate.py into a scratch copy only.
data "external" "zz_cloudinit_gate" {
  program = ["sh", "-c", "cat > \\"$0\\" && printf '{}'", "${path.root}/zz_cloudinit_gate_${local.first_nodepool_os}.json"]
  query = {
    rendered = jsonencode({
      autoscaler_yaml = local.autoscaler_yaml
      rke2_cloudinit  = [for config in data.cloudinit_config.autoscaler_config_rke2 : config.rendered]
      pools = [
        for index, pool in var.autoscaler_nodepools :
        { name = "${local.nodeConfigName}${pool.name}", os = local.autoscaler_nodepools_os[index] }
      ]
      unchanged = {
        install_k8s_agent                  = sha256(join("\\n", local.install_k8s_agent))
        install_k8s_server                 = sha256(join("\\n", local.install_k8s_server))
        install_rke2_agent                 = sha256(join("\\n", local.install_rke2_agent))
        k3s_install_agent_command          = sha256(local.k3s_install_agent_command)
        k3s_install_external_agent_command = sha256(local.k3s_install_external_agent_command)
        cloudinit_write_files_common       = sha256(local.cloudinit_write_files_common)
        cloudinit_runcmd_common            = sha256(local.cloudinit_runcmd_common)
      }
    })
  }
}
"""


COPY_IGNORE = shutil.ignore_patterns(".terraform", "zz_cloudinit_gate*")


class GateError(Exception):
    """Raised when a render or an input cannot be processed."""


# ---------------------------------------------------------------- rendering


def export_tree(ref: str, dest: Path) -> None:
    """Write the tracked files of ref (or of the working tree) to dest."""

    dest.mkdir(parents=True)
    if ref == "WORKTREE":
        files = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"], check=True, stdout=subprocess.PIPE
        ).stdout.split(b"\0")
        for name in filter(None, files):
            source, target = REPO_ROOT / name.decode(), dest / name.decode()
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                os.symlink(os.readlink(source), target)
            elif source.is_file():
                shutil.copy2(source, target)
        return
    archive = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", ref],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    archive_path = dest.parent / f"{dest.name}.tar"
    archive_path.write_bytes(archive)
    with tarfile.open(archive_path) as tar:
        tar.extractall(dest, filter="data")
    archive_path.unlink()


def test_file(cli: str, variants, registries_config: str | None) -> str:
    during = "  override_during = plan\n" if cli == "terraform" else ""
    hcloud = "".join(
        f'  mock_resource "hcloud_{name}" {{\n    defaults = {{ id = "4711" }}\n  }}\n'
        for name in HCLOUD_RESOURCES
    )
    registries = ""
    if registries_config is not None:
        registries = f"    registries_config = {json.dumps(registries_config)}\n"
    runs = "".join(
        f"""
run "render_{variant}" {{
  command = plan

  variables {{
{DUMMY_VARIABLES}{AUTOSCALER_NODEPOOLS.replace('"OS"', json.dumps(variant))}{registries}  }}
}}
"""
        for variant in variants
    )
    return f"""# Written by scripts/autoscaler_cloudinit_gate.py; dummy values only.
mock_provider "hcloud" {{
{during}{hcloud}  mock_resource "hcloud_load_balancer_network" {{
    defaults = {{ id = "4711" }}
  }}
  mock_data "hcloud_network" {{
    defaults = {{ ip_range = "10.0.0.0/8" }}
  }}
}}

mock_provider "http" {{
{during}}}

mock_provider "random" {{
{during}}}

mock_provider "ssh" {{
{during}  mock_resource "ssh_sensitive_resource" {{
    defaults = {{ result = "{MOCK_KUBECONFIG}" }}
  }}
}}

mock_provider "kubernetes" {{
{during}}}

mock_provider "helm" {{
{during}}}
{runs}"""


def render(
    tree: Path, args: argparse.Namespace, registries_config: str | None = None, variants=VARIANTS
) -> dict[str, dict[str, Any]]:
    """Render one module tree; return the dumped values per OS variant."""

    (tree / "zz_cloudinit_gate.tf").write_text(DUMP_TF, encoding="utf-8")
    test_dir = tree / "zz_cloudinit_gate"
    test_dir.mkdir()
    dumps = {variant: tree / f"zz_cloudinit_gate_{variant}.json" for variant in variants}
    (test_dir / "render.tftest.hcl").write_text(
        test_file(args.cli, variants, registries_config), encoding="utf-8"
    )
    init = [args.cli, "init", "-backend=false", "-input=false", "-no-color", "-test-directory=zz_cloudinit_gate"]
    if args.plugin_dir:
        init.append(f"-plugin-dir={args.plugin_dir}")
    env = dict(os.environ, TF_IN_AUTOMATION="1")
    for command in (init, [args.cli, "test", "-no-color", "-test-directory=zz_cloudinit_gate"]):
        result = subprocess.run(
            command, cwd=tree, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        if result.returncode != 0:
            tail = "\n".join(result.stdout.strip().splitlines()[-25:])
            raise GateError(f"{' '.join(command[:2])} failed in {tree.name}:\n{tail}")
    rendered = {}
    for variant, path in dumps.items():
        if not path.is_file():
            raise GateError(f"{tree.name}: no {variant} render was written")
        rendered[variant] = json.loads(json.loads(path.read_text(encoding="utf-8"))["rendered"])
        if not args.keep:
            path.unlink()
    return rendered


# ---------------------------------------------------------------- decoding


def config_jsons(manifest: str) -> list[dict[str, Any]]:
    return [
        json.loads(base64.b64decode(value))
        for value in re.findall(r"config\.json: ([A-Za-z0-9+/=]+)", manifest)
    ]


def manifest_rest(manifest: str) -> str:
    manifest = re.sub(r"config\.json: [A-Za-z0-9+/=]+", "config.json: X", manifest)
    return re.sub(r"checksum/config: '[0-9a-f]+'", "checksum/config: X", manifest)


def cloud_config_text(cloudinit: str) -> str:
    message = email.message_from_bytes(gzip.decompress(base64.b64decode(cloudinit)))
    parts = [part for part in message.walk() if part.get_content_type() == "text/cloud-config"]
    if len(parts) != 1:
        raise GateError(f"{len(parts)} cloud-config parts in a cloudInit")
    return parts[0].get_payload(decode=True).decode()


def encode_cloud_config(text: str) -> str:
    """Re-encode an edited cloud-config like the cloudinit provider (counter-tests only)."""

    message = email.message.EmailMessage(policy=email.policy.compat32)
    message["Content-Type"] = 'multipart/mixed; boundary="MIMEBOUNDARY"'
    message["MIME-Version"] = "1.0"
    part = email.message.Message()
    part["Content-Transfer-Encoding"] = "7bit"
    part["Content-Type"] = "text/cloud-config"
    part["Mime-Version"] = "1.0"
    part.set_payload(text)
    message.attach(part)
    return base64.b64encode(gzip.compress(message.as_bytes(), mtime=0)).decode()


def entry_bytes(entry: dict[str, Any]) -> bytes:
    content = entry.get("content", "")
    encoding = entry.get("encoding", "")
    if encoding in ("gzip+base64", "gz+b64", "gzip+b64"):
        return gzip.decompress(base64.b64decode(content))
    if encoding in ("base64", "b64"):
        return base64.b64decode(content)
    return content.encode() if isinstance(content, str) else bytes(content)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- checks


def check_pool(
    name: str, leap: bool, before_ci: str, after_ci: str, installer_sha: str, base_installer_sha: str
) -> list[str]:
    red: list[str] = []
    before = yaml.safe_load(cloud_config_text(before_ci))
    after = yaml.safe_load(cloud_config_text(after_ci))
    files_before = {entry["path"]: entry for entry in before.get("write_files", [])}
    files_after = {entry["path"]: entry for entry in after.get("write_files", [])}
    agent_after = files_after.get(AGENT, {})

    removed = sorted(set(files_before) - set(files_after))
    added = sorted(set(files_after) - set(files_before))
    want_removed = [] if leap else [TE]
    if removed != want_removed:
        red.append(f"removed write_files {removed}, want {want_removed}")
    if leap and TE not in files_after:
        red.append("Leap Micro pool lost /root/k8s_custom_policies.te")
    if len(added) != 1:
        red.append(f"added write_files {added}, want exactly the installer")
    installer_path = added[0] if len(added) == 1 else None
    if installer_path is not None:
        entry = files_after[installer_path]
        if entry.get("encoding") != "gzip+base64" or str(entry.get("permissions")) != "0700":
            red.append(f"installer entry encoding/permissions {entry.get('encoding')}/{entry.get('permissions')}")
        if sha256(entry_bytes(entry)) != installer_sha:
            red.append("installer entry bytes != scripts/install-verified-kubernetes.sh")

    for path in sorted(set(files_before) & set(files_after)):
        if path == AGENT:
            continue
        meta_before = {k: v for k, v in files_before[path].items() if k != "content"}
        meta_after = {k: v for k, v in files_after[path].items() if k != "content"}
        if meta_before != meta_after or entry_bytes(files_before[path]) != entry_bytes(files_after[path]):
            red.append(f"write_files entry changed: {path}")
    other_before = {k: v for k, v in before.items() if k != "write_files"}
    other_after = {k: v for k, v in after.items() if k != "write_files"}
    if other_before != other_after:
        changed = sorted(k for k in set(other_before) | set(other_after) if other_before.get(k) != other_after.get(k))
        red.append(f"cloud-config keys changed outside write_files: {changed}")

    if AGENT not in files_before or not agent_after:
        return red + ["install-k8s-agent.sh missing"]
    lines_before = entry_bytes(files_before[AGENT]).decode().splitlines()
    lines_after = entry_bytes(agent_after).decode().splitlines()
    if len(lines_before) != len(lines_after):
        return red + [f"install-k8s-agent.sh line count {len(lines_before)} -> {len(lines_after)}"]
    changed_lines = [i for i, (a, b) in enumerate(zip(lines_before, lines_after)) if a != b]
    if len(changed_lines) != 1:
        return red + [f"install-k8s-agent.sh: {len(changed_lines)} changed lines, want 1"]
    old = OLD_INSTALLER_LINE.match(lines_before[changed_lines[0]])
    new = NEW_INSTALLER_LINE.match(lines_after[changed_lines[0]])
    if not old or not new:
        return red + ["install-k8s-agent.sh: changed line is not the installer call"]
    if sha256(base64.b64decode(old.group("b64"))) != base_installer_sha:
        red.append("base inline installer != base scripts/install-verified-kubernetes.sh")
    if new.group("path") != installer_path:
        red.append("KH_INSTALLER path != installer write_files path")
    old_call, new_call = INSTALLER_CALL.match(old.group("call")), INSTALLER_CALL.match(new.group("call"))
    if not old_call or not new_call:
        return red + ["installer call not recognised"]
    if (old_call.group("env"), old_call.group("args")) != (new_call.group("env"), new_call.group("args")):
        red.append("installer environment or arguments changed")
    if not new_call.group("sh"):
        red.append('installer is not called as /bin/sh "$KH_INSTALLER"')
    if "trap" in new.group("call"):
        red.append("installer line still sets a trap")
    return red


def compare(
    before: dict[str, Any], after: dict[str, Any], installer_sha: str, base_installer_sha: str,
    args: argparse.Namespace,
) -> tuple[list[str], list[tuple[str, int, int]]]:
    """Compare two renders; return RED findings and (pool, before, after) lengths."""

    red: list[str] = []
    if manifest_rest(before["autoscaler_yaml"]) != manifest_rest(after["autoscaler_yaml"]):
        red.append("autoscaler manifest changed outside config.json/checksum")
    configs_before, configs_after = config_jsons(before["autoscaler_yaml"]), config_jsons(after["autoscaler_yaml"])
    if len(configs_before) != 1 or len(configs_after) != 1:
        return red + [f"config.json count {len(configs_before)}/{len(configs_after)}, want 1/1"], []
    nodes_before, nodes_after = configs_before[0]["nodeConfigs"], configs_after[0]["nodeConfigs"]
    strip = lambda config: {  # noqa: E731 - local helper
        **{k: v for k, v in config.items() if k != "nodeConfigs"},
        "nodeConfigs": {n: {k: v for k, v in c.items() if k != "cloudInit"} for n, c in config["nodeConfigs"].items()},
    }
    if strip(configs_before[0]) != strip(configs_after[0]):
        red.append("config.json changed outside cloudInit")
    if sorted(nodes_before) != sorted(nodes_after):
        return red + ["pool names changed"], []
    if args.expect_pools is not None and len(nodes_after) != args.expect_pools:
        red.append(f"{len(nodes_after)} pools, want {args.expect_pools}")

    leap_pools = {pool["name"] for pool in after.get("pools", []) if pool["os"] == "leapmicro"}
    leap_pools |= set(args.leapmicro_pools)
    lengths = []
    for name in sorted(nodes_after):
        ci_before, ci_after = nodes_before[name]["cloudInit"], nodes_after[name]["cloudInit"]
        leap = name in leap_pools
        lengths.append((name, len(ci_before), len(ci_after)))
        red += [f"{name}: {finding}" for finding in check_pool(
            name, leap, ci_before, ci_after, installer_sha, base_installer_sha)]
        delta = len(ci_before) - len(ci_after)
        if leap:
            if len(ci_after) > USER_DATA_LIMIT:
                red.append(f"{name}: length {len(ci_after)} > {USER_DATA_LIMIT}")
        elif len(ci_after) > args.max_len or not args.delta_min <= delta <= args.delta_max:
            red.append(
                f"{name}: length {len(ci_before)} -> {len(ci_after)} "
                f"(want <= {args.max_len}, shrink {args.delta_min}..{args.delta_max})"
            )
    if "rke2_cloudinit" in before and before["rke2_cloudinit"] != after["rke2_cloudinit"]:
        red.append("RKE2 autoscaler cloud-init changed")
    for key, value in sorted(before.get("unchanged", {}).items()):
        if after.get("unchanged", {}).get(key) != value:
            red.append(f"local.{key} changed")
    return red, lengths


def report(label: str, red: list[str], lengths: list[tuple[str, int, int]]) -> None:
    for name, before, after in lengths:
        print(f"  {label} {name}: {before} -> {after} B (shrink {before - after} B)")
    for finding in red:
        print(f"  RED {finding}")


# ---------------------------------------------------------------- counter-tests


def edit_pool(rendered: dict[str, Any], pool: str, edit) -> dict[str, Any]:
    """Apply edit(cloud_config_dict) to one pool of a render (in memory)."""

    result = copy.deepcopy(rendered)
    config = config_jsons(result["autoscaler_yaml"])[0]
    document = yaml.safe_load(cloud_config_text(config["nodeConfigs"][pool]["cloudInit"]))
    edit(document)
    text = "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False, width=1 << 20)
    config["nodeConfigs"][pool]["cloudInit"] = encode_cloud_config(text)
    encoded = base64.b64encode(json.dumps(config).encode()).decode()
    result["autoscaler_yaml"] = re.sub(
        r"config\.json: [A-Za-z0-9+/=]+", lambda _: f"config.json: {encoded}", result["autoscaler_yaml"]
    )
    return result


def in_memory_counter_tests(
    before: dict[str, Any], after: dict[str, Any], installer_sha: str, base_installer_sha: str,
    args: argparse.Namespace,
) -> list[str]:
    """Return the names of counter-tests that did NOT turn the gate RED."""

    leap = {pool["name"] for pool in after.get("pools", []) if pool["os"] == "leapmicro"}
    pool = next(name for name in sorted(config_jsons(after["autoscaler_yaml"])[0]["nodeConfigs"]) if name not in leap)
    te_entry = next(e for e in yaml.safe_load(cloud_config_text(
        config_jsons(before["autoscaler_yaml"])[0]["nodeConfigs"][pool]["cloudInit"]))["write_files"] if e["path"] == TE)

    def installer(document):
        return next(e for e in document["write_files"] if str(e.get("permissions")) == "0700"
                    and e["path"].startswith("/var/pre_install/") and e["path"] != AGENT)

    def remove_installer(document):
        document["write_files"].remove(installer(document))

    def keep_policy(document):
        document["write_files"].append(copy.deepcopy(te_entry))

    def move_installer(document):
        installer(document)["path"] = "/var/pre_install/install-verified-kubernetes-moved.sh"

    def change_installer(document):
        entry = installer(document)
        entry["content"] = base64.b64encode(gzip.compress(entry_bytes(entry) + b"\n# changed\n", mtime=0)).decode()

    def change_other(document):
        document["write_files"][0]["permissions"] = "0644"

    # Each edit must be caught by its own structural finding, not only by the
    # length window (the re-encoded cloudInit has a different length anyway).
    failed = []
    for label, edit, finding in (
        ("installer entry removed", remove_installer, "added write_files"),
        ("policy kept on MicroOS", keep_policy, "removed write_files"),
        ("installer path mismatch", move_installer, "KH_INSTALLER path"),
        ("installer bytes changed", change_installer, "installer entry bytes"),
        ("other entry changed", change_other, "write_files entry changed"),
    ):
        red, _ = compare(before, edit_pool(after, pool, edit), installer_sha, base_installer_sha, args)
        caught = any(finding in item for item in red)
        print(f"  counter-test {label}: {'RED (expected)' if caught else 'not caught (gate is blind)'}")
        if not caught:
            failed.append(label)
    return failed


# ---------------------------------------------------------------- plan mode


def plan_mode(args: argparse.Namespace) -> int:
    try:
        plan = json.load(sys.stdin)
        changes = [c for c in plan["resource_changes"] if c["change"]["actions"] not in (["no-op"], ["read"])]
        summary = sorted(f"{c['address']} {','.join(c['change']['actions'])}" for c in changes)
        print("relevant changes:", summary)
        red = []
        if plan.get("errored") is not False:
            red.append("plan errored")
        if len(changes) != 1 or not changes[0]["address"].endswith(CA_ADDRESS_SUFFIX) \
                or changes[0]["change"]["actions"] != ["delete", "create"]:
            red.append("relevant changes != exactly configure_autoscaler[0] replace")
        change = next(c for c in plan["resource_changes"] if c["address"].endswith(CA_ADDRESS_SUFFIX))["change"]
        if change.get("after_unknown", {}).get("triggers_replace") not in (None, False, {}):
            red.append("autoscaler manifest unknown at plan time")
        before = {"autoscaler_yaml": change["before"]["triggers_replace"]["template"]}
        after = {"autoscaler_yaml": change["after"]["triggers_replace"]["template"]}
        installer_sha = sha256(Path(args.installer_source).read_bytes())
        base_sha = args.base_installer_sha256 or installer_sha
        compare_red, lengths = compare(before, after, installer_sha, base_sha, args)
        red += compare_red
        report("plan", red, lengths)
    except Exception as exc:  # noqa: BLE001 - fail closed, never print values
        print(f"RED plan parse error: {type(exc).__name__}")
        return 1
    print("GREEN" if not red else f"RED ({len(red)} findings)")
    return 0 if not red else 1


# ---------------------------------------------------------------- sizing


def measure(tree: Path, head: dict[str, Any], args: argparse.Namespace) -> None:
    registries = Path(args.registries_config).read_text(encoding="utf-8") if args.registries_config else None
    variants = []
    if registries is not None:
        variants.append(("+registries.yaml", registries, []))
    if args.extra_write_file:
        variants.append(("+registries.yaml+extra write_files" if registries else "+extra write_files",
                         registries, args.extra_write_file))
    baseline = {n: len(c["cloudInit"]) for n, c in config_jsons(head["autoscaler_yaml"])[0]["nodeConfigs"].items()}
    for label, registries_config, extra in variants:
        copy_dir = tree.parent / f"measure-{len(list(tree.parent.glob('measure-*')))}"
        shutil.copytree(tree, copy_dir, ignore=COPY_IGNORE, symlinks=True)
        if extra:
            template = copy_dir / TEMPLATE
            entries = []
            for item in extra:
                path, _, source = item.partition("=")
                body = Path(source).read_text(encoding="utf-8").rstrip("\n")
                indented = "\n".join(f"    {line}" for line in body.splitlines())
                entries.append(f"- path: {path}\n  permissions: \"0644\"\n  content: |\n{indented}\n")
            text = template.read_text(encoding="utf-8")
            marker = "${cloudinit_write_files_common}\n"
            if marker not in text:
                raise GateError("template marker for extra write_files not found")
            template.write_text(text.replace(marker, marker + "\n" + "\n".join(entries), 1), encoding="utf-8")
        rendered = render(copy_dir, args, registries_config, variants=("microos",))["microos"]
        print(f"measure {label}:")
        for name, config in sorted(config_jsons(rendered["autoscaler_yaml"])[0]["nodeConfigs"].items()):
            length = len(config["cloudInit"])
            print(f"  {name}: {baseline[name]} -> {length} B (+{length - baseline[name]} B, "
                  f"{USER_DATA_LIMIT - length} B left)")


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="802cba967ded22905c0053b12f7c53c320248db5",
                        help="git ref of the module before the change")
    parser.add_argument("--head", default="HEAD", help="git ref after the change, or WORKTREE")
    parser.add_argument("--cli", default="tofu", choices=("tofu", "terraform"))
    parser.add_argument("--plugin-dir", help="provider plugin directory for an offline init")
    parser.add_argument("--max-len", type=int, default=26000)
    parser.add_argument("--delta-min", type=int, default=5700)
    parser.add_argument("--delta-max", type=int, default=6300)
    parser.add_argument("--expect-pools", type=int)
    parser.add_argument("--leapmicro-pools", default="", help="plan mode: comma list of Leap Micro pool names")
    parser.add_argument("--no-counter-test", action="store_true", help="skip the broken-template render")
    parser.add_argument("--registries-config", help="measure: registries.yaml file to render on the head")
    parser.add_argument("--extra-write-file", action="append", default=[], metavar="PATH=FILE",
                        help="measure: inject a plain write_files entry into a scratch template copy")
    parser.add_argument("--keep", help="keep the scratch trees in this new directory")
    parser.add_argument("--plan-json", help="'-' reads `tofu show -json` from stdin instead of rendering")
    parser.add_argument("--installer-source", default=str(REPO_ROOT / INSTALLER_SOURCE),
                        help="plan mode: installer file of the head revision")
    parser.add_argument("--base-installer-sha256", help="plan mode: sha256 of the base installer if it differs")
    args = parser.parse_args()
    args.leapmicro_pools = [name for name in args.leapmicro_pools.split(",") if name]

    if args.plan_json is not None:
        if args.plan_json != "-":
            parser.error("--plan-json takes '-' (stdin) only")
        return plan_mode(args)

    scratch = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="kh-cloudinit-gate-"))
    if args.keep:
        scratch.mkdir(parents=True)
    red: list[str] = []
    try:
        base_tree, head_tree = scratch / "base", scratch / "head"
        export_tree(args.base, base_tree)
        export_tree(args.head, head_tree)
        installer_sha = sha256((head_tree / INSTALLER_SOURCE).read_bytes())
        base_installer_sha = sha256((base_tree / INSTALLER_SOURCE).read_bytes())
        print(f"rendering base {args.base} and head {args.head} ({args.cli} test, mocked cloud providers)")
        base, head = render(base_tree, args), render(head_tree, args)
        for variant in VARIANTS:
            compare_red, lengths = compare(base[variant], head[variant], installer_sha, base_installer_sha, args)
            if variant == "microos" and not lengths:
                compare_red.append("no MicroOS pool rendered")
            print(f"head vs base, {variant} pools:")
            report("pool", compare_red, lengths)
            red += [f"{variant}: {finding}" for finding in compare_red]

        if red:
            print("counter-tests skipped: the head is already RED")
        else:
            print("counter-tests (each must be RED):")
            blind = in_memory_counter_tests(base["microos"], head["microos"], installer_sha, base_installer_sha, args)
            red += [f"counter-test stayed GREEN: {label}" for label in blind]
        if not red and not args.no_counter_test:
            broken_tree = scratch / "broken"
            shutil.copytree(head_tree, broken_tree, ignore=COPY_IGNORE, symlinks=True)
            template = broken_tree / TEMPLATE
            text, count = INSTALLER_BLOCK.subn("", template.read_text(encoding="utf-8"))
            if count != 1:
                red.append("counter-test: installer block not found in the template")
            else:
                template.write_text(text, encoding="utf-8")
                broken = render(broken_tree, args, variants=("microos",))["microos"]
                broken_red, _ = compare(base["microos"], broken, installer_sha, base_installer_sha, args)
                caught = any("added write_files" in item for item in broken_red)
                print(f"  counter-test real render without installer entry: "
                      f"{'RED (expected)' if caught else 'not caught (gate is blind)'}")
                if not caught:
                    red.append("counter-test stayed GREEN: real render without installer entry")

        if args.registries_config or args.extra_write_file:
            measure(head_tree, head["microos"], args)
    except (GateError, subprocess.CalledProcessError, KeyError, ValueError, StopIteration) as exc:
        print(f"RED {type(exc).__name__}: {exc}")
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
    print("GREEN" if not red else f"RED ({len(red)} findings)")
    return 0 if not red else 1


if __name__ == "__main__":
    sys.exit(main())
