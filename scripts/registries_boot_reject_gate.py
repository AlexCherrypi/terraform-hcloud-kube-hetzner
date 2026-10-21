#!/usr/bin/env python3
"""Render gate for registries_boot_reject (boot-time reject of mirror ClusterIPs).

Plans the module at a base and a head revision with `tofu test -verbose
-json` and compares the complete plans, with the rendering, the mocks, the
dummy cluster and the running autoscaled node of
scripts/registries_ca_files_gate.py: three control planes, one static
agent, two autoscaler pools, no access to Hetzner and no credential. Every
variant of that gate is planned (k3s with MicroOS nodes, k3s with Leap Micro
nodes, RKE2, and k3s with a registries_config mirror on 10.43.0.50:5000),
each without and with one registry CA, because the unit shares the user_data
budget with the CA files.

Without registries_boot_reject the head must plan exactly like the base,
with and without a CA: identical resource_changes and output_changes (the
cloud-init of every static node and every autoscaler pool, every trigger),
no added or removed resource instance, every base resource with the same
configuration and depends_on, and every provisioner script local of the
base with the same sha256.

With entries the head may differ from the base only by:

  * one added write_files entry on every static node and in every
    autoscaler pool: /etc/systemd/system/kh-registries-boot-reject.service,
    mode "0644", keys content/path/permissions only, and content equal to
    the unit this gate renders on its own from the entries (oneshot,
    RemainAfterExit, ordered after network-pre.target, which openSUSE's
    nftables flush at boot (soft-reboot-cleanup.service) runs before, and
    before k3s, k3s-agent, rke2-server and rke2-agent,
    WantedBy=multi-user.target, a "-" before the command so that a failing
    nft never marks the unit failed, one nft transaction that
    recreates the table inet kh_registries_boot_reject with a filter output
    chain at priority filter and one "reject with tcp reset" rule per entry),
  * one added runcmd item, [systemctl, enable, --now, <unit>], after
    [systemctl, daemon-reload] and, in autoscaler pools, before the
    Kubernetes agent install script,
  * no other change of the cloud-config text,
  * every pool's cloudInit growing by --delta-min..--delta-max bytes (one
    entry) and staying at or below --max-len (MicroOS k3s pools) or 32 KiB,
  * one terraform_data.registries_boot_reject[0] per static node and one
    terraform_data.autoscaled_nodes_registries_boot_reject per autoscaled
    node, triggered by registries_boot_reject only, each running the update
    script once over remote-exec,
  * an update script that checks the nft transaction with nft -c, installs
    the same unit with install -m 0644 and a rename in /etc/systemd/system,
    removes its temporary copy with a trap and runs systemctl only for
    daemon-reload and to enable and restart its own unit.

Invalid input must fail the plan: an address outside service_ipv4_cidr or
enable_kube_proxy = false (terraform_data.validation_contract), an IPv6 or
IPv4-mapped address or a port outside 1..65535 (variable validation). A run
with enable_kube_proxy = false and no entries must pass, so the kube-proxy
failure comes from this input.

The report lists the user_data length of every pool and static node against
the 26,600 B cap of MicroOS pools, the 27,500 B cap during a two-CA bundle
and the 32,768 B Hetzner limit. The static user_data is unknown at plan
time, so its length is estimated from the cloud-config text and calibrated
on the pools of the same run. --ca-file with a bundle of two certificates
and --max-len 27500 measures a CA rotation.

Counter-tests run after the comparison and must all turn the gate RED:
in-memory edits of the head plans (unit entry missing, wrong mode, unit
altered, unit not ordered after network-pre.target, a failing nft that
marks the unit failed, runcmd item missing or after the install, a pool
over --max-len, an empty-input plan with an added resource or a changed
script, a restart of k3s in the update script, a trigger that watches
registries_config) and real renders of mutated head copies (unit mode 0600,
the rule hooked before the DNAT, a unit not ordered after network-pre.target,
a pool without the unit, the runcmd item after the agent install, a resource
without the empty-list guard, a blank line left by the empty runcmd entry, a
k3s restart in the update script, address and kube-proxy checks that always
pass).

Exit code 0 = GREEN, 1 = RED.
"""

from __future__ import annotations

import argparse
import base64
import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autoscaler_cloudinit_gate as cig  # noqa: E402 - shared render helpers
import registries_ca_files_gate as cag  # noqa: E402 - shared plan views

yaml = cig.yaml

UNIT_NAME = "kh-registries-boot-reject.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"
UNIT_KEYS = ["content", "path", "permissions"]
TABLE = "inet kh_registries_boot_reject"
RUNCMD_ITEM = ["systemctl", "enable", "--now", UNIT_NAME]
DAEMON_RELOAD = ["systemctl", "daemon-reload"]
AGENT_INSTALL = ["/bin/bash", "/var/pre_install/install-k8s-agent.sh"]
TEST_DIR = "zz_r0_gate"
MIME_HEAD = (b'Content-Type: multipart/mixed; boundary="MIMEBOUNDARY"\nMIME-Version: 1.0\r\n\r\n--MIMEBOUNDARY\r\n'
             b'Content-Disposition: attachment; filename="init.cfg"\r\nContent-Transfer-Encoding: 7bit\r\n'
             b'Content-Type: text/cloud-config\r\nMime-Version: 1.0\r\n\r\n')
MIME_TAIL = b"\r\n--MIMEBOUNDARY--\r\n"
CAP_TWO_CAS = 27500

# The production entry, and two entries to render the list.
ONE = [("10.43.0.50", 5000)]
TWO = [("10.43.0.50", 5000), ("10.43.0.51", 5443)]
# Head-only case -> (registry CA, entries); "empty" and "ca" also run at the base.
CASES = {"empty": (False, []), "ca": (True, []), "rule": (False, TWO), "carule": (True, ONE)}
BASE_CASES = ("empty", "ca")
# Runs that must fail the plan (microos only) -> (extra variables, expected failure).
NEGATIVE = {
    "neg_outside": ('    registries_boot_reject = [{ ip = "10.44.0.50", port = 5000 }]\n',
                    "terraform_data.validation_contract"),
    "neg_kube_proxy": ('    enable_kube_proxy = false\n    cni_plugin = "cilium"\n'
                       '    registries_boot_reject = [{ ip = "10.43.0.50", port = 5000 }]\n',
                       "terraform_data.validation_contract"),
    "neg_mapped": ('    registries_boot_reject = [{ ip = "::ffff:10.43.0.50", port = 5000 }]\n',
                   "var.registries_boot_reject"),
    "neg_port": ('    registries_boot_reject = [{ ip = "10.43.0.50", port = 65536 }]\n',
                 "var.registries_boot_reject"),
    "ctl_kube_proxy": ('    enable_kube_proxy = false\n    cni_plugin = "cilium"\n', None),
}

BOOT_REJECT_RESOURCE = re.compile(r'(^|\.)terraform_data\.(?:autoscaled_nodes_)?registries_boot_reject(\[|$)')
ALLOWED_SYSTEMCTL = {"systemctl daemon-reload", 'systemctl enable "$UNIT"', 'systemctl restart "$UNIT"'}
KUBERNETES_UNIT = re.compile(r"\b(k3s|rke2|containerd|kubelet)\b")
HEAD_OUTPUTS_TF = """# Written by scripts/registries_boot_reject_gate.py into a scratch copy only.
output "zz_r0_boot_reject" {
  value = {
    unit   = local.registries_boot_reject_unit
    script = local.registries_boot_reject_update_script
  }
}
"""


class GateError(Exception):
    """Raised when a render or an input cannot be processed."""


canon = cag.canon


def nft_script(entries: list[tuple[str, int]]) -> str:
    rules = "".join(f"ip daddr {ip} tcp dport {port} reject with tcp reset;" for ip, port in entries)
    return (f"table {TABLE};delete table {TABLE};table {TABLE}"
            f"{{chain output{{type filter hook output priority filter;{rules}}};}}")


def expected_unit(entries: list[tuple[str, int]]) -> str:
    """The unit as this gate expects it, built without the module."""

    return ("[Unit]\nAfter=network-pre.target\n"
            "Before=k3s.service k3s-agent.service rke2-server.service rke2-agent.service\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            f"ExecStart=-/usr/sbin/nft '{nft_script(entries)}'\n"
            "[Install]\nWantedBy=multi-user.target\n")


def hcl_entries(entries: list[tuple[str, int]]) -> str:
    return "[" + ", ".join(f'{{ ip = "{ip}", port = {port} }}' for ip, port in entries) + "]"


# ---------------------------------------------------------------- rendering


def test_file(args: argparse.Namespace, variants: tuple[str, ...], cases: tuple[str, ...], pem: str,
              negative: bool) -> str:
    dummy = cig.DUMMY_VARIABLES.replace('count = 0, os = "microos" }', 'count = 1, os = "OS" }')
    if dummy == cig.DUMMY_VARIABLES:
        raise GateError("static agent pool not found in the dummy variables")

    def run(name: str, pool_os: str, extra: str, expect: str | None = None) -> str:
        failures = f"\n  expect_failures = [{expect}]\n" if expect else ""
        return f"""
run "{name}" {{
  command = plan

  variables {{
{dummy.replace('"OS"', json.dumps(pool_os))}{cig.AUTOSCALER_NODEPOOLS.replace('"OS"', json.dumps(pool_os))}{extra}  }}
{failures}}}
"""

    runs = ""
    for variant in variants:
        pool_os, _, variant_extra, _ = cag.VARIANTS[variant]
        for case in cases:
            with_ca, entries = CASES[case]
            extra = variant_extra
            if with_ca:
                extra += f"    registries_ca_files = {{ {json.dumps(cag.CA_NAME)} = {json.dumps(pem)} }}\n"
            if entries:
                extra += f"    registries_boot_reject = {hcl_entries(entries)}\n"
            # The CA files gate expects the reference warning without the CA.
            expect = cag.REFERENCE_CHECK if not with_ca and cag.REFERENCES[variant] else None
            runs += run(f"{variant}_{case}", pool_os, extra, expect)
    if negative:
        for name, (extra, expect) in NEGATIVE.items():
            runs += run(name, "microos", extra, expect)
    return cig.test_file(args.cli, (), None) + runs


def render(tree: Path, args: argparse.Namespace, variants: tuple[str, ...], cases: tuple[str, ...], pem: str,
           names: list[str], head: bool) -> dict[str, Any]:
    """Plan one module tree; return the plan JSON per run name."""

    (tree / "zz_r0_override.tf").write_text(cag.OVERRIDE_TF, encoding="utf-8")
    hashes = "\n".join(f"    {name} = sha256(jsonencode(local.{name}))" for name in names)
    outputs = f'output "zz_r0_script_sha256" {{\n  sensitive = true\n  value = {{\n{hashes}\n  }}\n}}\n'
    if head and re.search(r"^ {0,2}registries_boot_reject_update_script\s*=",
                          (tree / "locals.tf").read_text(encoding="utf-8"), re.MULTILINE):
        outputs += HEAD_OUTPUTS_TF
    (tree / "zz_r0_outputs.tf").write_text(outputs, encoding="utf-8")
    (tree / TEST_DIR).mkdir()
    (tree / TEST_DIR / "render.tftest.hcl").write_text(test_file(args, variants, cases, pem, head), encoding="utf-8")
    init = [args.cli, "init", "-backend=false", "-input=false", "-no-color", f"-test-directory={TEST_DIR}"]
    if args.plugin_dir:
        init.append(f"-plugin-dir={args.plugin_dir}")
    env = dict(os.environ, TF_IN_AUTOMATION="1")
    result = subprocess.run(init, cwd=tree, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise GateError(f"init failed in {tree.name}:\n" + "\n".join(result.stdout.strip().splitlines()[-15:]))
    test = [args.cli, "test", "-no-color", "-verbose", "-json", f"-test-directory={TEST_DIR}"]
    result = subprocess.run(test, cwd=tree, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    plans, errors, failed = {}, [], []
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            errors.append(line)
            continue
        if message.get("type") == "test_plan":
            plans[message["@testrun"]] = message["test_plan"]
        elif message.get("type") == "test_run":
            # OpenTofu reports a finished run only; Terraform also reports progress.
            run = message.get("test_run", {})
            if run.get("progress", "complete") == "complete" and run.get("status") not in ("pass", "pending"):
                failed.append(f"run {message.get('@testrun', run.get('run'))}: {run.get('status')}")
        elif message.get("@level") == "error":
            errors.append(f"{message.get('@testrun', '-')}: {message.get('@message', '')}")
    want = {f"{variant}_{case}" for variant in variants for case in cases}
    if result.returncode != 0 or failed or not want <= set(plans):
        detail = "\n".join(failed + errors[-15:])
        raise GateError(f"test failed in {tree.name} (runs {sorted(plans)}):\n{detail}")
    if args.keep:
        (tree / "zz_r0_plans.json").write_text(json.dumps(plans), encoding="utf-8")
    return plans


# ---------------------------------------------------------------- checks


def check_same(label: str, base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    """Without entries the head must plan exactly like the base."""

    red: list[str] = []
    before, after = cag.changes(base), cag.changes(head)
    added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
    if added or removed:
        red.append(f"{label}: empty input: resource instances added {added}, removed {removed}")
    changed = sorted(a for a in set(before) & set(after) if canon(before[a]) != canon(after[a]))
    if changed:
        red.append(f"{label}: empty input: planned values changed: {changed[:6]}{' ...' if len(changed) > 6 else ''}")
    outputs_before, outputs_after = cag.outputs(base), cag.outputs(head)
    hashes_before = outputs_before.pop("zz_r0_script_sha256", {}) or {}
    hashes_after = outputs_after.pop("zz_r0_script_sha256", {}) or {}
    outputs_after.pop("zz_r0_boot_reject", None)
    if canon(outputs_before) != canon(outputs_after):
        red.append(f"{label}: empty input: outputs changed")
    scripts = sorted(name for name in hashes_before if hashes_after.get(name) != hashes_before[name])
    if scripts or not hashes_before:
        red.append(f"{label}: empty input: provisioner script changed: {scripts or 'no hashes'}")
    config_before = cag.config_resources(base["configuration"]["root_module"])
    config_after = cag.config_resources(head["configuration"]["root_module"])
    moved = sorted(a for a in config_before if canon(config_before[a]) != canon(config_after.get(a)))
    if moved:
        red.append(f"{label}: empty input: resource configuration changed: {moved}")
    depends_before = cag.config_depends_on(base["configuration"]["root_module"])
    depends_after = cag.config_depends_on(head["configuration"]["root_module"])
    lost = sorted(a for a in depends_before if canon(depends_before[a]) != canon(depends_after.get(a)))
    if lost:
        red.append(f"{label}: empty input: depends_on changed: {lost}")
    return red


def estimate(text: str) -> int:
    """user_data length of a cloud-config as the cloudinit provider encodes it, before calibration.

    zlib compresses a little better than the provider's Go gzip, so the
    estimate is corrected by the error measured on the autoscaler pools.
    """

    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
    raw = MIME_HEAD + text.encode() + MIME_TAIL
    return len(base64.b64encode(compressor.compress(raw) + compressor.flush()))


def calibration(plan_changes: dict[str, dict[str, Any]]) -> int:
    if cag.CONFIGURE_AUTOSCALER not in plan_changes:
        return 0
    pools = cag.pools(cag.autoscaler_template(plan_changes))
    return max(len(ci) - estimate(cig.cloud_config_text(ci)) for ci in pools.values())


def load(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def item_index(runcmd: list[Any], item: list[str]) -> int:
    return next((i for i, entry in enumerate(runcmd) if entry == item), -1)


def check_cloud_config(label: str, before: str, after: str, unit: str, pool: bool) -> list[str]:
    """after must be before plus the unit entry and its runcmd item, nothing else."""

    red: list[str] = []
    lines_before, lines_after = before.splitlines(keepends=True), after.splitlines(keepends=True)
    opcodes = [op for op in difflib.SequenceMatcher(None, lines_before, lines_after, autojunk=False).get_opcodes()
               if op[0] != "equal"]
    inserted = ["".join(lines_after[op[3]:op[4]]) for op in opcodes if op[0] == "insert"]
    if len(opcodes) != 2 or len(inserted) != 2:
        red.append(f"{label}: cloud-config text changed outside two inserted blocks ({len(opcodes)} edits)")
    document_before, document_after = yaml.safe_load(before), yaml.safe_load(after)
    files_before, files_after = document_before.get("write_files", []), document_after.get("write_files", [])
    paths_before = [entry.get("path") for entry in files_before]
    added = [entry.get("path") for entry in files_after if entry.get("path") not in paths_before]
    entry = next((e for e in files_after if e.get("path") == UNIT_PATH), None)
    if entry is None:
        red.append(f"{label}: unit entry missing at {UNIT_PATH}")
    if added != [UNIT_PATH]:
        red.append(f"{label}: added write_files {added}, want exactly {UNIT_PATH}")
    if entry is not None:
        if sorted(entry) != UNIT_KEYS:
            red.append(f"{label}: unit entry keys {sorted(entry)}, want {UNIT_KEYS}")
        if entry.get("permissions") != "0644":
            red.append(f"{label}: unit entry permissions {entry.get('permissions')!r}, want '0644'")
        if entry.get("content") != unit:
            red.append(f"{label}: unit content differs from the expected unit")
        if inserted and load(inserted[0]) != [entry]:
            red.append(f"{label}: first inserted text is not exactly the unit entry")
    rest = [e for e in files_after if e.get("path") != UNIT_PATH]
    if rest != files_before:
        red.append(f"{label}: write_files changed besides the unit entry")

    runcmd_before, runcmd_after = document_before.get("runcmd", []), document_after.get("runcmd", [])
    at = item_index(runcmd_after, RUNCMD_ITEM)
    if at < 0:
        red.append(f"{label}: runcmd item {RUNCMD_ITEM} missing")
    else:
        if runcmd_after[:at] + runcmd_after[at + 1:] != runcmd_before:
            red.append(f"{label}: runcmd changed besides the unit item")
        if len(inserted) == 2 and load(inserted[1]) != [RUNCMD_ITEM]:
            red.append(f"{label}: second inserted text is not exactly the runcmd item")
        reload = item_index(runcmd_after, DAEMON_RELOAD)
        if not 0 <= reload < at:
            red.append(f"{label}: runcmd item runs before systemctl daemon-reload")
        if pool:
            install = item_index(runcmd_after, AGENT_INSTALL)
            if not at < install:
                red.append(f"{label}: runcmd item runs after the agent install script (or no install script)")
    other_before = {k: v for k, v in document_before.items() if k not in ("write_files", "runcmd")}
    other_after = {k: v for k, v in document_after.items() if k not in ("write_files", "runcmd")}
    if other_before != other_after:
        keys = sorted(k for k in set(other_before) | set(other_after) if other_before.get(k) != other_after.get(k))
        red.append(f"{label}: cloud-config keys changed outside write_files and runcmd: {keys}")
    return red


def check_script(script: str, unit: str, entries: list[tuple[str, int]]) -> list[str]:
    red: list[str] = []
    for number, line in enumerate(script.splitlines(), 1):
        if cag.RESTART.search(line.replace(UNIT_NAME, "")) and line.strip() not in ALLOWED_SYSTEMCTL:
            red.append(f"update script restarts or stops a service (line {number}: {line.strip()})")
        if KUBERNETES_UNIT.search(line):
            red.append(f"update script names a Kubernetes service (line {number})")
    if f"UNIT={UNIT_NAME}\n" not in script or "UNIT_DIR=/etc/systemd/system\n" not in script:
        red.append(f"update script does not write {UNIT_PATH}")
    if f"/usr/sbin/nft -c '{nft_script(entries)}'\n" not in script:
        red.append("update script does not check the nft transaction with nft -c")
    payloads = re.findall(r"^printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d > \"\$UNIT_TMP\"$", script, re.MULTILINE)
    if [base64.b64decode(payload).decode() for payload in payloads] != [unit]:
        red.append("update script payload != expected unit")
    if not re.search(r"^trap 'rm -f \"\$UNIT_TMP\"[^']*' EXIT$", script, re.MULTILINE):
        red.append("update script has no trap that removes its temporary copy")
    if 'install -m 0644 -o root -g root "$UNIT_TMP" "$UNIT_DIR/.$UNIT.new"' not in script \
            or 'mv -fT "$UNIT_DIR/.$UNIT.new" "$UNIT_DIR/$UNIT"' not in script:
        red.append("update script does not install the unit with install -m 0644 and a rename")
    order = [script.find(f"{step}\n") for step in ('systemctl enable "$UNIT"', 'systemctl restart "$UNIT"')]
    if -1 in order or order != sorted(order) or order[0] < script.find("mv -fT"):
        red.append("update script does not enable and then restart its unit after installing it")
    return red


def check_wiring(configuration: dict[str, Any]) -> list[str]:
    """Each boot reject resource triggers on the input only and runs the update script once."""

    red: list[str] = []
    root = configuration["root_module"]
    resources = cag.config_resources(root)
    wanted = {
        "terraform_data.autoscaled_nodes_registries_boot_reject": "local.registries_boot_reject_update_script",
        "module.control_planes.terraform_data.registries_boot_reject": "var.registries_boot_reject_script",
        "module.agents.terraform_data.registries_boot_reject": "var.registries_boot_reject_script",
    }
    for address, script in wanted.items():
        resource = resources.get(address)
        if resource is None:
            red.append(f"boot reject resource {address} not configured")
            continue
        provisioners = [(p["type"], canon(p.get("expressions"))) for p in resource.get("provisioners", [])]
        if provisioners != [("remote-exec", canon({"inline": {"references": [script]}}))]:
            red.append(f"{address}: provisioners {provisioners}, want one remote-exec of {script}")
        triggers = resource.get("expressions", {}).get("triggers_replace")
        if canon(triggers) != canon({"references": ["var.registries_boot_reject"]}):
            red.append(f"{address}: trigger watches more than var.registries_boot_reject")
    for name in ("control_planes", "agents"):
        module = root["module_calls"][name]
        expressions = module["expressions"]
        if canon(expressions.get("registries_boot_reject")) != canon({"references": ["var.registries_boot_reject"]}) \
                or canon(expressions.get("registries_boot_reject_script")) \
                != canon({"references": ["local.registries_boot_reject_update_script"]}):
            red.append(f"module.{name} does not get registries_boot_reject and its update script")
    # Neither direction of an ordering with the registries.yaml resources.
    for name, module in [("", root)] + [(n, root["module_calls"][n]["module"]) for n in ("control_planes", "agents")]:
        graph, prefix = cag.dependency_graph(module), f"module.{name}." if name else ""
        for address in graph:
            if BOOT_REJECT_RESOURCE.search(address):
                path = cag.waits_for(graph, address, lambda dep: bool(cag.REGISTRIES_RESOURCE.search(dep)))
            elif cag.REGISTRIES_RESOURCE.search(address):
                path = cag.waits_for(graph, address, lambda dep: bool(BOOT_REJECT_RESOURCE.search(dep)))
            else:
                continue
            if path:
                red.append(f"{prefix}{address} is ordered against another registries resource: {' -> '.join(path)}")
    return red


def check_rule(label: str, case: str, base: dict[str, Any], head: dict[str, Any], args: argparse.Namespace,
               sizes: list[tuple[str, str, int, int]]) -> list[str]:
    """With entries the head may differ from the base only by the unit."""

    red: list[str] = []
    variant = label
    entries = CASES[case][1]
    label = f"{variant} {case}"
    size_limit = cag.VARIANTS[variant][3]
    max_len = args.max_len if size_limit == "max-len" else cig.USER_DATA_LIMIT
    unit = expected_unit(entries)
    before, after = cag.changes(base), cag.changes(head)

    expected = {f"{m.group(1)}.terraform_data.registries_boot_reject[0]"
                for a in before if (m := cag.HOST_SERVER.match(a))}
    hosts = len(expected)
    expected |= {f'terraform_data.autoscaled_nodes_registries_boot_reject["{m.group(1)}"]'
                 for a in before if (m := cag.AUTOSCALED_REGISTRIES.match(a))}
    if not hosts or len(expected) == hosts:
        red.append(f"{label}: no static node or no autoscaled node rendered")
    added, removed = set(after) - set(before), set(before) - set(after)
    if added != expected or removed:
        red.append(f"{label}: resource instances added {sorted(added)}, removed {sorted(removed)}, "
                   f"want exactly {sorted(expected)}")
    trigger = {"registries_boot_reject": [{"ip": ip, "port": port} for ip, port in entries]}
    for address in sorted(added):
        change = after[address]
        if change["actions"] != ["create"] or canon(change["after"].get("triggers_replace")) != canon(trigger):
            red.append(f"{label}: {address}: trigger {change['after'].get('triggers_replace')}, "
                       "want registries_boot_reject only")

    changed = sorted(a for a in set(before) & set(after) if canon(before[a]) != canon(after[a]))
    other = [a for a in changed if not (cag.HOST_CLOUDINIT.match(a) or a == cag.CONFIGURE_AUTOSCALER)]
    if other:
        red.append(f"{label}: planned values changed outside cloud-init: {other[:6]}")

    correction = calibration(after)
    host_configs = sorted(a for a in before if cag.HOST_CLOUDINIT.match(a))
    if len(host_configs) != hosts:
        red.append(f"{label}: {len(host_configs)} static cloud-init configs for {hosts} static nodes")
    for address in host_configs:
        if address not in after:
            continue
        red += check_cloud_config(f"{label} {address}", cag.host_content(before[address]),
                                  cag.host_content(after[address]), unit, pool=False)
        # The static user_data is unknown at plan time: estimate it.
        estimates = [estimate(cag.host_content(change[address])) + correction for change in (before, after)]
        sizes.append((label, f"{address} (estimated)", *estimates))
        if estimates[1] > cig.USER_DATA_LIMIT:
            red.append(f"{label} {address}: estimated user_data {estimates[1]} B > {cig.USER_DATA_LIMIT} B")

    if cag.CONFIGURE_AUTOSCALER not in before or cag.CONFIGURE_AUTOSCALER not in after:
        return red + [f"{label}: no autoscaler manifest rendered"]
    template_before, template_after = cag.autoscaler_template(before), cag.autoscaler_template(after)
    if cig.manifest_rest(template_before) != cig.manifest_rest(template_after):
        red.append(f"{label}: autoscaler manifest changed outside config.json/checksum")
    if cag.without_cloudinit(cig.config_jsons(template_before)[0]) \
            != cag.without_cloudinit(cig.config_jsons(template_after)[0]):
        red.append(f"{label}: config.json changed outside cloudInit")
    pools_before, pools_after = cag.pools(template_before), cag.pools(template_after)
    if sorted(pools_before) != sorted(pools_after) or not pools_after:
        return red + [f"{label}: autoscaler pools changed"]
    for name in sorted(pools_after):
        ci_before, ci_after = pools_before[name], pools_after[name]
        sizes.append((label, f"pool {name}", len(ci_before), len(ci_after)))
        red += check_cloud_config(f"{label} {name}", cig.cloud_config_text(ci_before),
                                  cig.cloud_config_text(ci_after), unit, pool=True)
        growth = len(ci_after) - len(ci_before)
        window = (args.delta_min, args.delta_max) if len(entries) == 1 else (args.delta_min, args.delta_max * 2)
        if len(ci_after) > max_len or not window[0] <= growth <= window[1]:
            red.append(f"{label} {name}: length {len(ci_before)} -> {len(ci_after)} "
                       f"(want <= {max_len}, growth {window[0]}..{window[1]})")

    hashes_before = cag.outputs(base).get("zz_r0_script_sha256") or {}
    hashes_after = cag.outputs(head).get("zz_r0_script_sha256") or {}
    scripts = sorted(name for name in hashes_before if hashes_after.get(name) != hashes_before[name])
    if scripts:
        red.append(f"{label}: entries changed provisioner scripts: {scripts}")
    boot_reject = cag.outputs(head).get("zz_r0_boot_reject") or {}
    if boot_reject.get("unit") != unit:
        red.append(f"{label}: local.registries_boot_reject_unit differs from the expected unit")
    red += [f"{label}: {finding}" for finding in check_script(boot_reject.get("script") or "", unit, entries)]
    red += [f"{label}: {finding}" for finding in check_wiring(head["configuration"])]
    return red


def check_variant(variant: str, base: dict[str, Any], head: dict[str, Any], args: argparse.Namespace,
                  sizes: list[tuple[str, str, int, int]]) -> list[str]:
    red = check_same(f"{variant} empty", base[f"{variant}_empty"], head[f"{variant}_empty"])
    red += check_same(f"{variant} ca", base[f"{variant}_ca"], head[f"{variant}_ca"])
    red += check_rule(variant, "rule", base[f"{variant}_empty"], head[f"{variant}_rule"], args, sizes)
    red += check_rule(variant, "carule", base[f"{variant}_ca"], head[f"{variant}_carule"], args, sizes)
    return red


# ---------------------------------------------------------------- counter-tests


def unit_edit(how: str) -> Callable[[str], str]:
    """Edit the unit entry or its runcmd item in a cloud-config text (counter-tests only)."""

    def edit(text: str) -> str:
        lines = text.splitlines(keepends=True)
        start = lines.index(f"- path: {UNIT_PATH}\n")
        end = next(i for i in range(start, len(lines)) if lines[i].startswith("  permissions: "))
        item = next(i for i, line in enumerate(lines) if line.startswith(f"- [systemctl, enable, --now, {UNIT_NAME}]"))
        if how == "missing":
            del lines[start:end + 1]
        elif how == "mode":
            lines[end] = lines[end].replace('"0644"', '"0600"')
        elif how == "content":
            exec_start = next(i for i in range(start, end) if "priority filter" in lines[i])
            lines[exec_start] = lines[exec_start].replace("priority filter", "priority -150")
        elif how == "fail-hard":
            exec_start = next(i for i in range(start, end) if "ExecStart=-/" in lines[i])
            lines[exec_start] = lines[exec_start].replace("ExecStart=-/", "ExecStart=/")
        elif how == "no-after":
            del lines[next(i for i in range(start, end) if lines[i].strip() == "After=network-pre.target")]
        elif how == "no-runcmd":
            del lines[item]
        elif how == "late-runcmd":
            moved = lines.pop(item)
            at = next(i for i, line in enumerate(lines) if "install-k8s-agent.sh" in line and line.startswith("- ")) + 1
            lines.insert(at, moved)
        return "".join(lines)
    return edit


def in_memory_counter_tests(head: dict[str, Any], base: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Return the names of counter-tests that did NOT turn the gate RED."""

    def rule(plan: dict[str, Any], max_len: int | None = None) -> list[str]:
        local_args = copy.copy(args)
        if max_len is not None:
            local_args.max_len = max_len
        return check_rule("microos", "carule", base["microos_ca"], plan, local_args, [])

    plan = head["microos_carule"]
    smallest = min(len(ci) for ci in cag.pools(cag.autoscaler_template(cag.changes(plan))).values())
    boot_reject = r'^module\.control_planes\["[^"]+"\]\.terraform_data\.registries_boot_reject\[0\]$'

    def edit_script(edit: Callable[[str], str]) -> dict[str, Any]:
        result = copy.deepcopy(plan)
        values = result["output_changes"]["zz_r0_boot_reject"]["after"]
        values["script"] = edit(values["script"])
        return result

    def watch_registries(after: dict[str, Any]) -> None:
        after["triggers_replace"]["registries"] = "mirrors: {}"

    cases = [
        ("pool: unit entry missing", lambda: rule(cag.edit_pool(plan, unit_edit("missing"))), "unit entry missing"),
        ("host: unit entry mode 0600", lambda: rule(cag.edit_host(plan, unit_edit("mode"))), "unit entry permissions"),
        ("pool: rule hooked before the DNAT", lambda: rule(cag.edit_pool(plan, unit_edit("content"))),
         "unit content differs"),
        ("host: unit not ordered after network-pre.target",
         lambda: rule(cag.edit_host(plan, unit_edit("no-after"))), "unit content differs"),
        ("pool: a failing nft marks the unit failed",
         lambda: rule(cag.edit_pool(plan, unit_edit("fail-hard"))), "unit content differs"),
        ("host: runcmd item missing", lambda: rule(cag.edit_host(plan, unit_edit("no-runcmd"))), "runcmd item"),
        ("pool: runcmd item after the agent install", lambda: rule(cag.edit_pool(plan, unit_edit("late-runcmd"))),
         "runs after the agent install script"),
        ("pool over --max-len", lambda: rule(plan, max_len=smallest - 1), "want <= "),
        ("update script restarts k3s", lambda: rule(edit_script(lambda s: s + "systemctl restart k3s\n")),
         "update script restarts or stops a service"),
        ("update script without nft -c", lambda: rule(edit_script(lambda s: s.replace("/usr/sbin/nft -c", "true"))),
         "nft -c"),
        ("trigger watches registries_config", lambda: rule(cag.edit_change(plan, boot_reject, watch_registries)),
         "want registries_boot_reject only"),
        ("resource missing", lambda: rule(cag.drop_change(plan, boot_reject)), "resource instances added"),
        ("empty input adds a resource",
         lambda: check_same("microos empty", base["microos_empty"], {
             **head["microos_empty"],
             "resource_changes": head["microos_empty"]["resource_changes"]
             + [next(r for r in plan["resource_changes"] if re.search(boot_reject, r["address"]))]}),
         "empty input: resource instances added"),
        ("empty input changes a provisioner script",
         lambda: check_same("microos empty", base["microos_empty"],
                            cag.change_script_hash(head["microos_empty"], "k8s_registries_update_script")),
         "empty input: provisioner script changed"),
        ("empty input changes a pool cloud-config",
         lambda: check_same("microos empty", base["microos_empty"],
                            cag.edit_pool(head["microos_empty"], lambda text: text + "\n")),
         "empty input: planned values changed"),
    ]
    failed = []
    for label, run, finding in cases:
        if not cig.counter_test(label, run(), finding):
            failed.append(label)
    return failed


MUTATIONS = {
    # label: (file, old, new, finding)
    "real render: unit mode 0600": (
        "locals.tf", 'permissions: \\"0644\\"\\n" : ""', 'permissions: \\"0600\\"\\n" : ""', "unit entry permissions"),
    "real render: rule hooked before the DNAT": (
        "locals.tf", "type filter hook output priority filter;", "type filter hook output priority -150;",
        "unit content differs"),
    "real render: unit not ordered after network-pre.target": (
        "locals.tf", "[Unit]\nAfter=network-pre.target\n", "[Unit]\n", "unit content differs"),
    "real render: autoscaler pool without the unit": (
        "autoscaler-agents.tf", "          local.cloudinit_write_files_registries_boot_reject,\n", "",
        "unit entry missing"),
    "real render: runcmd item after the agent install": (
        "templates/autoscaler-cloudinit.yaml.tpl",
        ("runcmd:\n\n${cloudinit_runcmd_common}\n",
         "# Start the Kubernetes agent install script\n- ['/bin/bash', '/var/pre_install/install-k8s-agent.sh']\n"),
        ("runcmd:\n\n- ['/bin/bash', '/var/pre_install/install-k8s-agent.sh']\n${cloudinit_runcmd_common}\n",
         "# Start the Kubernetes agent install script\n"),
        "runs after the agent install script"),
    "real render: resource without the empty-list guard": (
        "modules/host/main.tf", "count = length(var.registries_boot_reject) > 0 ? 1 : 0", "count = 1",
        "empty input: resource instances added"),
    "real render: empty runcmd entry leaves a blank line": (
        "locals.tf", "%{endif}\n${local.cloudinit_runcmd_registries_boot_reject}\nEOT",
        "%{endif}\n\n${local.cloudinit_runcmd_registries_boot_reject}\nEOT", "empty input: planned values changed"),
    "real render: k3s restart in the update script": (
        "locals.tf", 'systemctl restart "$UNIT"\nEOF', 'systemctl restart "$UNIT"\nsystemctl restart k3s\nEOF',
        "update script restarts or stops a service"),
    "real render: address check that always passes": (
        "validation-contract.tf", "local.service_ipv4_cidr_effective != null && try(", "true || try(",
        "run neg_outside: "),
    "real render: kube-proxy check that always passes": (
        "validation-contract.tf", "length(var.registries_boot_reject) == 0 || var.enable_kube_proxy",
        "length(var.registries_boot_reject) >= 0 || var.enable_kube_proxy",
        "run neg_kube_proxy: "),
}


def mutation_counter_tests(head_tree: Path, scratch: Path, base: dict[str, Any], names: list[str], pem: str,
                           args: argparse.Namespace) -> list[str]:
    failed = []
    for number, (label, (name, old, new, finding)) in enumerate(MUTATIONS.items()):
        tree = scratch / f"mutation-{number}"
        shutil.copytree(head_tree, tree, ignore=cag.COPY_IGNORE, symlinks=True)
        source = tree / name
        text = source.read_text(encoding="utf-8")
        pairs = list(zip(old, new)) if isinstance(old, tuple) else [(old, new)]
        if any(text.count(anchor) != 1 for anchor, _ in pairs):
            print(f"  counter-test {label}: mutation anchor not found")
            failed.append(label)
            continue
        for anchor, replacement in pairs:
            text = text.replace(anchor, replacement)
        source.write_text(text, encoding="utf-8")
        try:
            plans = render(tree, args, ("microos",), tuple(CASES), pem, names, head=True)
        except GateError as exc:
            if not cig.counter_test(label, [str(exc)], finding):
                failed.append(label)
            continue
        if not cig.counter_test(label, check_variant("microos", base, plans, args, []), finding):
            failed.append(label)
    return failed


# ---------------------------------------------------------------- main


def report(sizes: list[tuple[str, str, int, int]]) -> None:
    caps = (("26,600 cap", 26600), ("27,500 two-CA cap", CAP_TWO_CAS), ("32,768 limit", cig.USER_DATA_LIMIT))
    for label, what, before, after in sizes:
        left = ", ".join(f"{after_cap - after:+d} B to the {name}" for name, after_cap in caps)
        print(f"  {label} {what}: {before} -> {after} B (+{after - before} B; {left})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="80c7d16",
                        help="git ref of the module before the change")
    parser.add_argument("--head", default="HEAD", help="git ref after the change, or WORKTREE")
    parser.add_argument("--cli", default="tofu", choices=("tofu", "terraform"))
    parser.add_argument("--plugin-dir", help="provider plugin directory for an offline init")
    parser.add_argument("--ca-file", help="PEM CA certificate or bundle (default: a throwaway ECDSA P-256 CA)")
    parser.add_argument("--max-len", type=int, default=26600)
    parser.add_argument("--delta-min", type=int, default=100)
    parser.add_argument("--delta-max", type=int, default=450)
    parser.add_argument("--no-counter-test", action="store_true", help="skip the mutated real renders")
    parser.add_argument("--variants", default=",".join(cag.VARIANTS),
                        help="comma-separated variants to render; a subset skips the counter-tests")
    parser.add_argument("--keep", help="keep the scratch trees and plans in this new directory")
    args = parser.parse_args()
    variants = tuple(args.variants.split(","))
    if not variants or not set(variants) <= set(cag.VARIANTS):
        parser.error(f"--variants must be a subset of {','.join(cag.VARIANTS)}")
    partial = set(variants) != set(cag.VARIANTS)

    scratch = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="kh-boot-reject-gate-"))
    if args.keep:
        scratch.mkdir(parents=True)
    red: list[str] = []
    try:
        pem = Path(args.ca_file).read_text(encoding="utf-8") if args.ca_file else cag.dummy_ca(scratch)
        base_tree, head_tree = scratch / "base", scratch / "head"
        cig.export_tree(args.base, base_tree)
        cig.export_tree(args.head, head_tree)
        names = sorted(set(cag.script_locals(base_tree)) & set(cag.script_locals(head_tree)))
        print(f"rendering base {args.base} and head {args.head} ({args.cli} test -verbose -json, mocked cloud providers)")
        print(f"  CA: {len(pem)} B PEM, {len(names)} provisioner script locals hashed, "
              f"{len(NEGATIVE)} invalid-input runs at the head")
        base = render(base_tree, args, variants, BASE_CASES, pem, names, head=False)
        head = render(head_tree, args, variants, tuple(CASES), pem, names, head=True)
        print("invalid input: every negative run failed the plan as expected, the kube-proxy control run passed")
        for variant in variants:
            sizes: list[tuple[str, str, int, int]] = []
            variant_red = check_variant(variant, base, head, args, sizes)
            print(f"{variant}: {'no finding' if not variant_red else f'{len(variant_red)} findings'}; "
                  f"user_data lengths (base -> head):")
            report(sizes)
            for finding in variant_red:
                print(f"  RED {finding}")
            red += variant_red

        if red:
            print("counter-tests skipped: the head is already RED")
        elif partial:
            print("counter-tests skipped: they need all variants")
        else:
            print("counter-tests (each must be RED):")
            red += [f"counter-test stayed GREEN: {label}" for label in in_memory_counter_tests(head, base, args)]
            if not args.no_counter_test:
                red += [f"counter-test stayed GREEN: {label}"
                        for label in mutation_counter_tests(head_tree, scratch, base, names, pem, args)]
    except (GateError, cag.GateError, cig.GateError, subprocess.CalledProcessError, KeyError, ValueError,
            StopIteration) as exc:
        print(f"RED {type(exc).__name__}: {exc}")
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
    if partial and not red:
        print(f"GREEN (partial run: {', '.join(variants)}; no counter-tests)")
    else:
        print("GREEN" if not red else f"RED ({len(red)} findings)")
    return 0 if not red else 1


if __name__ == "__main__":
    sys.exit(main())
