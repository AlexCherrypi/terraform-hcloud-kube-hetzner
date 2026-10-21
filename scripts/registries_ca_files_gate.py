#!/usr/bin/env python3
"""Render gate for registries_ca_files (registry CA files on every node).

Plans the module at a base and a head revision with `tofu test -verbose
-json` and compares the complete plans. The rendering and the mocks are the
ones of scripts/autoscaler_cloudinit_gate.py: mocked hcloud, http, random,
ssh, kubernetes and helm providers, dummy inputs and the real cloudinit
provider. The dummy cluster has three control planes, one static agent and
two autoscaler pools. The mocked hcloud_servers data source returns no
servers, so a scratch-only override file replaces local.autoscaled_nodes
with one running autoscaled node. Every tree is planned four times: k3s
with MicroOS nodes, k3s with Leap Micro nodes, RKE2, and k3s with a
registries_config mirror that references the CA file. Nothing talks to
Hetzner and no credential is needed.

Without registries_ca_files the head must plan exactly like the base:

  * resource_changes and output_changes are identical. This covers the
    cloud-init of every static node (data.cloudinit_config.config), the
    autoscaler manifest with every pool's cloudInit, and the triggers of
    every provisioner resource, and it means no resource instance is added,
    so no new SSH run happens,
  * every base resource keeps its configuration (provisioners, triggers,
    count and for_each) and every depends_on entry, and every provisioner script
    local of the base keeps its sha256. The one exception is a
    registries_config whose configs.*.tls.ca_file references a file: the
    registries update scripts then gain exactly the ca_file guard (one block
    per reference, first step of the update branch, exit 1 when the file
    holds no certificate, no restart). That changes no planned value and
    starts no SSH run.
  * check.registries_ca_files_references warns (status fail) exactly when a
    tls.ca_file under the registries directory is not in
    registries_ca_files, and passes otherwise.

With one CA (--ca-file, or a throwaway ECDSA P-256 CA made with openssl)
the head may differ from the base only by:

  * exactly one added write_files entry on every static node and in every
    autoscaler pool: path next to registries.yaml, content equal to the PEM
    and permissions "0644", no other key. The cloud-config text gains only
    these lines and stays byte-identical otherwise,
  * every pool's cloudInit growing by --delta-min..--delta-max bytes and
    staying at or below --max-len,
  * one terraform_data.registries_ca_files[0] per static node and one
    terraform_data.autoscaled_nodes_registries_ca_files per autoscaled
    node, triggered by the CA files only, while the registries.yaml
    resources keep their triggers,
  * the registries.yaml resources depending on the CA resources of their
    module, and no CA resource depending on a registries.yaml resource
    (depends_on and resource references), so a combined change writes the
    CA file before registries.yaml restarts Kubernetes,
  * a CA update script that installs the PEM with install -m 0644, removes
    its temporary copy with a trap and restarts no service,
  * the same ca_file guard rule for the registries update scripts and the
    same reference check result, which now passes.

Counter-tests run after the comparison and must all turn the gate RED, each
through its own finding: in-memory edits of the head plans (CA entry
missing, wrong mode, extra key, CA content altered, another entry changed,
a CA entry at the wrong path, a restart in the CA update script, a changed
registries trigger, a CA trigger that also watches registries.yaml, a
missing CA resource, a pool over --max-len, an empty-input plan with a
new resource, changed cloud-init or a changed script, and a ca_file guard
that is missing, comes too late, checks another path, does not exit or
appears without a reference, a reference check that is missing, silent on
a dangling ca_file or warns without one, a CA ordering that is removed or
reversed, a lost depends_on entry) and real renders of mutated head copies
(entry mode 0600, a restart in the script, a CA resource without the
empty-map guard, CA entries or the ca_file guard rendered by a template
directive that leaves blank lines when the list is empty, a reference check
that always passes, registries resources without the CA ordering, the CA
ordering reversed).

--variants renders only some variants, for example to measure a bundle of
two CAs (a CA rotation) that does not fit the RKE2 pools. Such a partial run
skips the counter-tests and says so in its last line.

Exit code 0 = GREEN, 1 = RED.
"""

from __future__ import annotations

import argparse
import base64
import copy
import difflib
import email.message
import email.policy
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autoscaler_cloudinit_gate as cig  # noqa: E402 - shared render helpers

yaml = cig.yaml

CA_NAME = "registry-cache-ca.pem"
CA_KEYS = ["content", "path", "permissions"]
CA_DIRS = {"k3s": "/etc/rancher/k3s", "rke2": "/etc/rancher/rke2"}
TEST_DIR = "zz_r0_gate"
COPY_IGNORE = shutil.ignore_patterns(".terraform", "zz_r0_*")

HOST_CLOUDINIT = re.compile(r'^module\.(?:control_planes|agents)\["[^"]+"\]\.data\.cloudinit_config\.config$')
HOST_SERVER = re.compile(r'^(module\.(?:control_planes|agents)\["[^"]+"\])\.hcloud_server\.server$')
AUTOSCALED_REGISTRIES = re.compile(r'^terraform_data\.autoscaled_nodes_registries\["([^"]+)"\]$')
REGISTRIES_RESOURCE = re.compile(r'(^|\.)terraform_data\.(?:autoscaled_nodes_)?registries(\[|$)')
CONFIGURE_AUTOSCALER = "terraform_data.configure_autoscaler[0]"
REFERENCE_CHECK = "check.registries_ca_files_references"
REGISTRIES_SCRIPTS = ("k3s_registries_update_script", "rke2_registries_update_script", "k8s_registries_update_script")
GUARD_PAYLOAD = re.compile(r"^  CA_FILE=\$\(printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d\)$", re.MULTILINE)
GUARD_CHECK = re.compile(
    r"^  if ! grep -qs -- '-----BEGIN CERTIFICATE-----' \"\$CA_FILE\"; then\n    echo \"Error: [^\"\n]*\"\n"
    r"    exit 1\n  fi$", re.MULTILINE)
RESTART = re.compile(r"\b(systemctl|service|kill|pkill|killall|reboot|shutdown|k3s-killall\.sh|rke2-killall\.sh)\b")

REGISTRIES_CONFIG = (
    "mirrors:\n  docker.io:\n    endpoint:\n      - https://10.43.0.50:5000\n"
    'configs:\n  "10.43.0.50:5000":\n    tls:\n      ca_file: /etc/rancher/k3s/registry-cache-ca.pem\n'
)
# Variant -> (OS of the static agent and the autoscaler pools, distribution,
# extra variables, size limit). Control planes stay on MicroOS. The k3s
# MicroOS pools carry --max-len; Leap Micro pools keep the SELinux policy and
# RKE2 pools the inline installer, so they only need to fit the 32 KiB limit.
VARIANTS = {
    "microos": ("microos", "k3s", "", "max-len"),
    "leapmicro": ("leapmicro", "k3s", "", "user_data"),
    "rke2": ("microos", "rke2", '    kubernetes_distribution = "rke2"\n', "user_data"),
    "registries": ("microos", "k3s", f"    registries_config = {json.dumps(REGISTRIES_CONFIG)}\n", "max-len"),
}
CASES = ("empty", "ca")
# configs.*.tls.ca_file references of each variant's registries_config.
REFERENCES = {
    name: [config["tls"]["ca_file"] for config in yaml.safe_load(REGISTRIES_CONFIG)["configs"].values()]
    if "registries_config" in spec[2] else []
    for name, spec in VARIANTS.items()
}

# Written into scratch copies only. The mocked hcloud_servers data source
# returns no servers, and an override_data block cannot target one instance
# of it, so the running autoscaled nodes are replaced here.
OVERRIDE_TF = """# Written by scripts/registries_ca_files_gate.py into a scratch copy only.
locals {
  autoscaled_nodes = {
    "gate-worker-cx33-r0node" = {
      name         = "gate-worker-cx33-r0node"
      ipv4_address = "192.0.2.21"
      ipv6_address = ""
      network      = []
    }
  }
}
"""
HEAD_OUTPUTS_TF = """# Written by scripts/registries_ca_files_gate.py into a scratch copy only.
output "zz_registries_ca_update_script" {
  value = local.registries_ca_update_script
}
"""
SCRIPTS_OUTPUT_TF = "output \"zz_r0_registries_scripts\" {\n  value = {\n" + "".join(
    f"    {name} = local.{name}\n" for name in REGISTRIES_SCRIPTS) + "  }\n}\n"


class GateError(Exception):
    """Raised when a render or an input cannot be processed."""


def canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


# ---------------------------------------------------------------- rendering


def script_locals(tree: Path) -> list[str]:
    """Locals that feed provisioners or install commands in this tree."""

    text = (tree / "locals.tf").read_text(encoding="utf-8")
    # Locals sit at most two spaces deep; deeper matches are object keys.
    names = set(re.findall(r"^ {0,2}([a-z0-9_]+_script)\s*=", text, re.MULTILINE))
    names |= {"install_k8s_agent", "install_k8s_server", "install_rke2_agent",
              "k3s_install_agent_command", "k3s_install_external_agent_command"}
    return sorted(name for name in names if re.search(rf"^ {{0,2}}{name}\s*=", text, re.MULTILINE))


def dangling(variant: str, ca_files: dict[str, str]) -> list[str]:
    """ca_file references under the CA directory that ca_files does not provide."""

    ca_dir = CA_DIRS[VARIANTS[variant][1]]
    return [path for path in REFERENCES[variant]
            if path.startswith(f"{ca_dir}/") and path[len(ca_dir) + 1:] not in ca_files]


def test_file(
    args: argparse.Namespace, variants: tuple[str, ...], cases: tuple[str, ...], pem: str, reference_check: bool,
) -> str:
    dummy = cig.DUMMY_VARIABLES.replace('count = 0, os = "microos" }', 'count = 1, os = "OS" }')
    if dummy == cig.DUMMY_VARIABLES:
        raise GateError("static agent pool not found in the dummy variables")
    runs = ""
    for variant in variants:
        pool_os, _, extra, _ = VARIANTS[variant]
        for case in cases:
            ca = f"    registries_ca_files = {{ {json.dumps(CA_NAME)} = {json.dumps(pem)} }}\n" if case == "ca" else ""
            # tofu test turns a failed check block into an error; plan only warns.
            expect = ""
            if reference_check and dangling(variant, {CA_NAME: pem} if case == "ca" else {}):
                expect = f"\n  expect_failures = [{REFERENCE_CHECK}]\n"
            runs += f"""
run "{variant}_{case}" {{
  command = plan

  variables {{
{dummy.replace('"OS"', json.dumps(pool_os))}{cig.AUTOSCALER_NODEPOOLS.replace('"OS"', json.dumps(pool_os))}{extra}{ca}  }}
{expect}}}
"""
    return cig.test_file(args.cli, (), None) + runs


def render(
    tree: Path, args: argparse.Namespace, variants: tuple[str, ...], cases: tuple[str, ...], pem: str,
    names: list[str],
) -> dict[str, Any]:
    """Plan one module tree; return the plan JSON per run name."""

    (tree / "zz_r0_override.tf").write_text(OVERRIDE_TF, encoding="utf-8")
    hashes = "\n".join(f"    {name} = sha256(jsonencode(local.{name}))" for name in names)
    outputs = f'output "zz_r0_script_sha256" {{\n  sensitive = true\n  value = {{\n{hashes}\n  }}\n}}\n'
    outputs += SCRIPTS_OUTPUT_TF
    # A head without the CA update script still renders, and fails the checks.
    if "ca" in cases and re.search(r"^ {0,2}registries_ca_update_script\s*=",
                                   (tree / "locals.tf").read_text(encoding="utf-8"), re.MULTILINE):
        outputs += HEAD_OUTPUTS_TF
    (tree / "zz_r0_outputs.tf").write_text(outputs, encoding="utf-8")
    (tree / TEST_DIR).mkdir()
    reference_check = any(re.search(r'^check "registries_ca_files_references"', path.read_text(encoding="utf-8"), re.M)
                          for path in tree.glob("*.tf"))
    (tree / TEST_DIR / "render.tftest.hcl").write_text(test_file(args, variants, cases, pem, reference_check),
                                                       encoding="utf-8")
    init = [args.cli, "init", "-backend=false", "-input=false", "-no-color", f"-test-directory={TEST_DIR}"]
    if args.plugin_dir:
        init.append(f"-plugin-dir={args.plugin_dir}")
    env = dict(os.environ, TF_IN_AUTOMATION="1")
    result = subprocess.run(init, cwd=tree, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise GateError(f"init failed in {tree.name}:\n" + "\n".join(result.stdout.strip().splitlines()[-15:]))
    test = [args.cli, "test", "-no-color", "-verbose", "-json", f"-test-directory={TEST_DIR}"]
    result = subprocess.run(test, cwd=tree, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    plans, errors = {}, []
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            errors.append(line)
            continue
        if message.get("type") == "test_plan":
            plans[message["@testrun"]] = message["test_plan"]
        elif message.get("@level") == "error":
            errors.append(message.get("@message", ""))
    want = {f"{variant}_{case}" for variant in variants for case in cases}
    if result.returncode != 0 or set(plans) != want:
        detail = "\n".join(errors[-15:])
        raise GateError(f"test failed in {tree.name} (runs {sorted(plans)}):\n{detail}")
    if args.keep:
        (tree / "zz_r0_plans.json").write_text(json.dumps(plans), encoding="utf-8")
    return plans


# ---------------------------------------------------------------- plan views


def changes(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    keep = ("actions", "after", "after_unknown", "after_sensitive")
    return {rc["address"]: {key: rc["change"].get(key) for key in keep} for rc in plan["resource_changes"]}


def config_resources(module: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    """Managed resource configuration by module path + address (depends_on dropped)."""

    found = {}
    for resource in module.get("resources", []):
        # Data sources only render; their output is compared in the plan.
        if resource.get("mode") == "managed":
            found[prefix + resource["address"]] = {k: v for k, v in resource.items() if k != "depends_on"}
    for name, call in module.get("module_calls", {}).items():
        found.update(config_resources(call.get("module", {}), f"{prefix}module.{name}."))
    return found


def config_depends_on(module: dict[str, Any], prefix: str = "") -> dict[str, list[str]]:
    """depends_on of every managed resource by module path + address."""

    found = {}
    for resource in module.get("resources", []):
        if resource.get("mode") == "managed":
            found[prefix + resource["address"]] = resource.get("depends_on", [])
    for name, call in module.get("module_calls", {}).items():
        found.update(config_depends_on(call.get("module", {}), f"{prefix}module.{name}."))
    return found


def references(value: Any) -> set[str]:
    """Every expression reference below value."""

    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "references" and isinstance(item, list):
                found.update(ref for ref in item if isinstance(ref, str))
            else:
                found |= references(item)
    elif isinstance(value, list):
        for item in value:
            found |= references(item)
    return found


def dependency_graph(module: dict[str, Any]) -> dict[str, set[str]]:
    """Resource -> resources of the same module it waits for (depends_on and references)."""

    def resource_of(ref: str) -> str | None:
        parts = re.sub(r"\[[^\]]*\]", "", ref).split(".")
        if parts[0] in ("var", "local", "module", "each", "count", "self", "path", "terraform"):
            return None
        return ".".join(parts[:3] if parts[0] == "data" else parts[:2])

    graph: dict[str, set[str]] = {}
    for resource in module.get("resources", []):
        edges = {re.sub(r"\[[^\]]*\]", "", dep) for dep in resource.get("depends_on", [])}
        edges |= {r for ref in references({k: v for k, v in resource.items() if k != "depends_on"})
                  if (r := resource_of(ref)) and r != resource["address"]}
        graph[resource["address"]] = edges
    return graph


def waits_for(graph: dict[str, set[str]], start: str, wanted: Callable[[str], bool]) -> list[str]:
    """Return a dependency path from start to a resource for which wanted is true, or []."""

    seen, stack = {start}, [[start]]
    while stack:
        path = stack.pop()
        for dep in sorted(graph.get(path[-1], ())):
            if wanted(dep):
                return path + [dep]
            if dep not in seen:
                seen.add(dep)
                stack.append(path + [dep])
    return []


def host_content(change: dict[str, Any]) -> str:
    return change["after"]["part"][0]["content"]


def autoscaler_template(plan_changes: dict[str, dict[str, Any]]) -> str:
    return plan_changes[CONFIGURE_AUTOSCALER]["after"]["triggers_replace"]["template"]


def pools(template: str) -> dict[str, str]:
    configs = cig.config_jsons(template)
    if len(configs) != 1:
        raise GateError(f"{len(configs)} config.json documents in the autoscaler manifest")
    return {name: config["cloudInit"] for name, config in configs[0]["nodeConfigs"].items()}


def without_cloudinit(config: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: v for k, v in config.items() if k != "nodeConfigs"},
        "nodeConfigs": {n: {k: v for k, v in c.items() if k != "cloudInit"} for n, c in config["nodeConfigs"].items()},
    }


def outputs(plan: dict[str, Any]) -> dict[str, Any]:
    return {name: change.get("after") for name, change in plan.get("output_changes", {}).items()}


# ---------------------------------------------------------------- checks


def check_cloud_config(label: str, before: str, after: str, path: str, pem: str) -> list[str]:
    """after must be before plus exactly one CA write_files entry."""

    red: list[str] = []
    lines_before, lines_after = before.splitlines(keepends=True), after.splitlines(keepends=True)
    opcodes = [op for op in difflib.SequenceMatcher(None, lines_before, lines_after, autojunk=False).get_opcodes()
               if op[0] != "equal"]
    inserted: list[str] = []
    if len(opcodes) != 1 or opcodes[0][0] != "insert":
        red.append(f"{label}: cloud-config text changed outside one inserted block ({len(opcodes)} edits)")
    else:
        inserted = lines_after[opcodes[0][3]:opcodes[0][4]]
    document_before, document_after = yaml.safe_load(before), yaml.safe_load(after)
    files_before = document_before.get("write_files", [])
    files_after = document_after.get("write_files", [])
    paths_before = [entry.get("path") for entry in files_before]
    added = [entry for entry in files_after if entry.get("path") not in paths_before]
    entry = next((e for e in files_after if e.get("path") == path), None)
    if entry is None:
        red.append(f"{label}: CA entry missing at {path}")
    if [e.get("path") for e in added] != [path]:
        red.append(f"{label}: added write_files {[e.get('path') for e in added]}, want exactly {path}")
    if entry is not None:
        if sorted(entry) != CA_KEYS:
            red.append(f"{label}: CA entry keys {sorted(entry)}, want {CA_KEYS}")
        if entry.get("permissions") != "0644":
            red.append(f"{label}: CA entry permissions {entry.get('permissions')!r}, want '0644'")
        if entry.get("content") != pem:
            red.append(f"{label}: CA entry content != PEM")
        if inserted and yaml.safe_load("".join(inserted)) != [entry]:
            red.append(f"{label}: inserted text is not exactly the CA entry")
    rest = [e for e in files_after if e.get("path") != path]
    if [e.get("path") for e in rest] != paths_before:
        red.append(f"{label}: write_files order or paths changed besides the CA entry")
    for entry_before, entry_after in zip(files_before, rest):
        if entry_before != entry_after:
            red.append(f"{label}: write_files entry changed: {entry_before.get('path')}")
    other_before = {k: v for k, v in document_before.items() if k != "write_files"}
    other_after = {k: v for k, v in document_after.items() if k != "write_files"}
    if other_before != other_after:
        keys = sorted(k for k in set(other_before) | set(other_after) if other_before.get(k) != other_after.get(k))
        red.append(f"{label}: cloud-config keys changed outside write_files: {keys}")
    return red


def check_ca_script(script: str, ca_dir: str, files: dict[str, str]) -> list[str]:
    red: list[str] = []
    for number, line in enumerate(script.splitlines(), 1):
        if RESTART.search(line):
            red.append(f"CA update script restarts or stops a service (line {number})")
    if not re.search(r"^trap 'rm -f \"\$CA_TMP\"[^']*' EXIT$", script, re.MULTILINE):
        red.append("CA update script has no trap that removes its temporary copy")
    if f"CA_DIR={ca_dir}\n" not in script:
        red.append(f"CA update script does not write to {ca_dir}")
    payloads = re.findall(r"^printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d > \"\$CA_TMP\"$", script, re.MULTILINE)
    decoded = [base64.b64decode(payload).decode() for payload in payloads]
    if decoded != list(files.values()):
        red.append("CA update script payloads != registries_ca_files")
    for name in files:
        if 'install -m 0644 -o root -g root "$CA_TMP" "$CA_DIR/.registries-ca-file.new"' not in script \
                or f'mv -fT "$CA_DIR/.registries-ca-file.new" "$CA_DIR/{name}"' not in script:
            red.append(f"CA update script does not install {name} with install -m 0644 and a rename")
    return red


def check_guard(label: str, base: dict[str, Any], head: dict[str, Any], references: list[str]) -> tuple[list[str], set[str]]:
    """The registries update scripts gain the ca_file guard, and only that.

    Returns the findings and the script locals that may change their sha256.
    """

    red: list[str] = []
    texts_before = outputs(base).get("zz_r0_registries_scripts") or {}
    texts_after = outputs(head).get("zz_r0_registries_scripts") or {}
    if sorted(texts_before) != sorted(REGISTRIES_SCRIPTS) or sorted(texts_after) != sorted(REGISTRIES_SCRIPTS):
        return [f"{label}: registries update scripts not rendered"], set()
    for name in REGISTRIES_SCRIPTS:
        before, after = texts_before[name], texts_after[name]
        if not references:
            if before != after:
                red.append(f"{label}: {name} changed without a ca_file reference")
            continue
        lines_before, lines_after = before.splitlines(keepends=True), after.splitlines(keepends=True)
        opcodes = [op for op in difflib.SequenceMatcher(None, lines_before, lines_after, autojunk=False).get_opcodes()
                   if op[0] != "equal"]
        if not opcodes:
            red.append(f"{label}: {name}: ca_file guard missing")
            continue
        if len(opcodes) != 1 or opcodes[0][0] != "insert":
            red.append(f"{label}: {name}: changed outside one inserted ca_file guard ({len(opcodes)} edits)")
            continue
        at, inserted = opcodes[0][1], "".join(lines_after[opcodes[0][3]:opcodes[0][4]])
        if at < 2 or lines_before[at - 1] != "else\n" or "No update required" not in lines_before[at - 2]:
            red.append(f"{label}: {name}: ca_file guard is not the first step of the update branch")
        checked = [base64.b64decode(payload).decode() for payload in GUARD_PAYLOAD.findall(inserted)]
        if checked != references:
            red.append(f"{label}: {name}: ca_file guard checks {checked}, want {references}")
        if len(GUARD_CHECK.findall(inserted)) != len(references) or len(inserted.splitlines()) != 5 * len(references):
            red.append(f"{label}: {name}: ca_file guard is not exactly one failing certificate check per reference")
        if RESTART.search(inserted):
            red.append(f"{label}: {name}: ca_file guard restarts or stops a service")
    return red, set(REGISTRIES_SCRIPTS) if references else set()


def check_references(label: str, plan: dict[str, Any], ca_files: dict[str, str]) -> list[str]:
    """The plan warns exactly when a ca_file under the CA directory is not in registries_ca_files."""

    missing = dangling(label, ca_files)
    want = "fail" if missing else "pass"
    found = [c for c in plan.get("checks", []) if c.get("address", {}).get("to_display") == REFERENCE_CHECK]
    status = found[0].get("status") if len(found) == 1 else None
    if status != want:
        return [f"{label}: {REFERENCE_CHECK} status {status!r}, want {want!r} (dangling {missing})"]
    return []


# Module call ("" = root) -> (registries.yaml resource, its CA resource).
ORDERING = {
    "control_planes": ("terraform_data.registries", "terraform_data.registries_ca_files"),
    "agents": ("terraform_data.registries", "terraform_data.registries_ca_files"),
    "": ("terraform_data.autoscaled_nodes_registries", "terraform_data.autoscaled_nodes_registries_ca_files"),
}


def check_ordering(configuration: dict[str, Any]) -> list[str]:
    """registries.yaml waits for the CA files of its module, never the other way round."""

    red: list[str] = []
    for name, (registries, ca) in ORDERING.items():
        module = configuration["root_module"]
        if name:
            module = module.get("module_calls", {}).get(name, {}).get("module", {})
        graph, prefix = dependency_graph(module), f"module.{name}." if name else ""
        if registries not in graph or ca not in graph:
            red.append(f"{prefix}{registries} or {prefix}{ca} not configured")
            continue
        if not waits_for(graph, registries, lambda dep, ca=ca: dep == ca):
            red.append(f"{prefix}{registries} does not depend on {prefix}{ca}")
        back = waits_for(graph, ca, lambda dep: bool(REGISTRIES_RESOURCE.search(dep)))
        if back:
            red.append(f"{prefix}{ca} depends on a registries resource: {' -> '.join(back)}")
    return red


def check_wiring(configuration: dict[str, Any]) -> list[str]:
    """The CA resources trigger on the CA files only, run the CA script and come first."""

    red: list[str] = check_ordering(configuration)
    resources = config_resources(configuration["root_module"])
    wanted = {
        "terraform_data.autoscaled_nodes_registries_ca_files": "local.registries_ca_update_script",
        "module.control_planes.terraform_data.registries_ca_files": "var.registries_ca_update_script",
        "module.agents.terraform_data.registries_ca_files": "var.registries_ca_update_script",
    }
    for address, script in wanted.items():
        resource = resources.get(address)
        if resource is None:
            red.append(f"CA resource {address} not configured")
            continue
        provisioners = [(p["type"], canon(p.get("expressions"))) for p in resource.get("provisioners", [])]
        if provisioners != [("remote-exec", canon({"inline": {"references": [script]}}))]:
            red.append(f"{address}: provisioners {provisioners}, want one remote-exec of {script}")
        triggers = resource.get("expressions", {}).get("triggers_replace")
        if canon(triggers) != canon({"references": ["var.registries_ca_files"]}):
            red.append(f"{address}: CA trigger watches more than var.registries_ca_files")
    for name in ("control_planes", "agents"):
        expressions = configuration["root_module"]["module_calls"][name]["expressions"]
        script_ref = canon({"references": ["local.registries_ca_update_script"]})
        files_ref = canon({"references": ["var.registries_ca_files"]})
        if canon(expressions.get("registries_ca_update_script")) != script_ref \
                or canon(expressions.get("registries_ca_files")) != files_ref:
            red.append(f"module.{name} does not get registries_ca_files and the CA update script")
    return red


def check_empty(label: str, base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    """Without registries_ca_files the head must plan exactly like the base."""

    red: list[str] = []
    before, after = changes(base), changes(head)
    added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
    if added or removed:
        red.append(f"{label}: empty input: resource instances added {added}, removed {removed}")
    changed = sorted(a for a in set(before) & set(after) if canon(before[a]) != canon(after[a]))
    if changed:
        red.append(f"{label}: empty input: planned values changed: {changed[:6]}{' ...' if len(changed) > 6 else ''}")
    outputs_before, outputs_after = outputs(base), outputs(head)
    hashes_before = outputs_before.pop("zz_r0_script_sha256", {}) or {}
    hashes_after = outputs_after.pop("zz_r0_script_sha256", {}) or {}
    outputs_after.pop("zz_registries_ca_update_script", None)
    outputs_before.pop("zz_r0_registries_scripts", None)
    outputs_after.pop("zz_r0_registries_scripts", None)
    if canon(outputs_before) != canon(outputs_after):
        red.append(f"{label}: empty input: outputs changed")
    guard_red, guarded = check_guard(label, base, head, REFERENCES[label])
    red += guard_red + check_references(label, head, {})
    scripts = sorted(name for name in hashes_before if hashes_after.get(name) != hashes_before[name])
    if set(scripts) - guarded or not hashes_before:
        red.append(f"{label}: empty input: provisioner script changed: {sorted(set(scripts) - guarded) or 'no hashes'}")
    config_before = config_resources(base["configuration"]["root_module"])
    config_after = config_resources(head["configuration"]["root_module"])
    moved = sorted(a for a in config_before if canon(config_before[a]) != canon(config_after.get(a)))
    if moved:
        red.append(f"{label}: empty input: resource configuration changed: {moved}")
    depends_before = config_depends_on(base["configuration"]["root_module"])
    depends_after = config_depends_on(head["configuration"]["root_module"])
    lost = sorted(a for a in depends_before if not set(depends_before[a]) <= set(depends_after.get(a, [])))
    if lost:
        red.append(f"{label}: empty input: depends_on entries lost: {lost}")
    return red


def check_ca(
    label: str, base: dict[str, Any], head: dict[str, Any], script: str, pem: str, args: argparse.Namespace,
) -> tuple[list[str], list[tuple[str, int, int]]]:
    """With one CA the head may differ from the base only by the CA."""

    red: list[str] = []
    _, distribution, _, size_limit = VARIANTS[label]
    max_len = args.max_len if size_limit == "max-len" else cig.USER_DATA_LIMIT
    ca_dir = CA_DIRS[distribution]
    path = f"{ca_dir}/{CA_NAME}"
    before, after = changes(base), changes(head)

    expected = {f"{m.group(1)}.terraform_data.registries_ca_files[0]" for a in before if (m := HOST_SERVER.match(a))}
    hosts = len(expected)
    expected |= {f'terraform_data.autoscaled_nodes_registries_ca_files["{m.group(1)}"]'
                 for a in before if (m := AUTOSCALED_REGISTRIES.match(a))}
    if not hosts or len(expected) == hosts:
        red.append(f"{label}: no static node or no autoscaled node rendered")
    added, removed = set(after) - set(before), set(before) - set(after)
    if added != expected or removed:
        red.append(f"{label}: CA resource instances added {sorted(added)}, removed {sorted(removed)}, "
                   f"want exactly {sorted(expected)}")
    for address in sorted(added):
        change = after[address]
        if change["actions"] != ["create"] or canon(change["after"].get("triggers_replace")) \
                != canon({"registries_ca_files": {CA_NAME: pem}}):
            red.append(f"{label}: {address}: CA trigger {sorted((change['after'].get('triggers_replace') or {}))}, "
                       "want registries_ca_files only")

    changed = sorted(a for a in set(before) & set(after) if canon(before[a]) != canon(after[a]))
    allowed = {a for a in changed if HOST_CLOUDINIT.match(a) or a == CONFIGURE_AUTOSCALER}
    for address in changed:
        if REGISTRIES_RESOURCE.search(address):
            red.append(f"{label}: registries trigger changed: {address}")
    other = [a for a in changed if a not in allowed]
    if other:
        red.append(f"{label}: planned values changed outside cloud-init: {other[:6]}")

    host_configs = sorted(a for a in before if HOST_CLOUDINIT.match(a))
    if len(host_configs) != hosts:
        red.append(f"{label}: {len(host_configs)} static cloud-init configs for {hosts} static nodes")
    for address in host_configs:
        if address not in after:
            continue
        red += check_cloud_config(f"{label} {address}", host_content(before[address]), host_content(after[address]),
                                  path, pem)

    lengths = []
    if CONFIGURE_AUTOSCALER not in before or CONFIGURE_AUTOSCALER not in after:
        return red + [f"{label}: no autoscaler manifest rendered"], lengths
    template_before, template_after = autoscaler_template(before), autoscaler_template(after)
    if cig.manifest_rest(template_before) != cig.manifest_rest(template_after):
        red.append(f"{label}: autoscaler manifest changed outside config.json/checksum")
    if without_cloudinit(cig.config_jsons(template_before)[0]) != without_cloudinit(cig.config_jsons(template_after)[0]):
        red.append(f"{label}: config.json changed outside cloudInit")
    pools_before, pools_after = pools(template_before), pools(template_after)
    if sorted(pools_before) != sorted(pools_after) or not pools_after:
        return red + [f"{label}: autoscaler pools changed"], lengths
    for name in sorted(pools_after):
        ci_before, ci_after = pools_before[name], pools_after[name]
        lengths.append((name, len(ci_before), len(ci_after)))
        red += check_cloud_config(f"{label} {name}", cig.cloud_config_text(ci_before), cig.cloud_config_text(ci_after),
                                  path, pem)
        growth = len(ci_after) - len(ci_before)
        if len(ci_after) > max_len or not args.delta_min <= growth <= args.delta_max:
            red.append(f"{label} {name}: length {len(ci_before)} -> {len(ci_after)} "
                       f"(want <= {max_len}, growth {args.delta_min}..{args.delta_max})")

    hashes_before = outputs(base).get("zz_r0_script_sha256") or {}
    hashes_after = outputs(head).get("zz_r0_script_sha256") or {}
    guard_red, guarded = check_guard(label, base, head, REFERENCES[label])
    red += guard_red + check_references(label, head, {CA_NAME: pem})
    scripts = sorted(name for name in hashes_before if hashes_after.get(name) != hashes_before[name])
    if set(scripts) - guarded:
        red.append(f"{label}: CA input changed provisioner scripts: {sorted(set(scripts) - guarded)}")
    red += [f"{label}: {finding}" for finding in check_ca_script(script, ca_dir, {CA_NAME: pem})]
    red += [f"{label}: {finding}" for finding in check_wiring(head["configuration"])]
    return red, lengths


# ---------------------------------------------------------------- counter-tests


def edit_host(plan: dict[str, Any], edit: Callable[[str], str]) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    rc = next(r for r in sorted(result["resource_changes"], key=lambda r: r["address"])
              if HOST_CLOUDINIT.match(r["address"]))
    rc["change"]["after"]["part"][0]["content"] = edit(rc["change"]["after"]["part"][0]["content"])
    return result


def encode_cloud_config(text: str) -> str:
    """Re-encode an edited cloud-config (counter-tests only; keeps non-ASCII text)."""

    message = email.message.EmailMessage(policy=email.policy.compat32)
    message["Content-Type"] = 'multipart/mixed; boundary="MIMEBOUNDARY"'
    message["MIME-Version"] = "1.0"
    part = email.message.Message()
    part["Content-Transfer-Encoding"] = "base64"
    part["Content-Type"] = "text/cloud-config"
    part.set_payload(base64.encodebytes(text.encode()).decode())
    message.attach(part)
    return base64.b64encode(gzip.compress(message.as_bytes(), mtime=0)).decode()


def edit_pool(plan: dict[str, Any], edit: Callable[[str], str]) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    rc = next(r for r in result["resource_changes"] if r["address"] == CONFIGURE_AUTOSCALER)
    template = rc["change"]["after"]["triggers_replace"]["template"]
    config = cig.config_jsons(template)[0]
    pool = sorted(config["nodeConfigs"])[0]
    text = edit(cig.cloud_config_text(config["nodeConfigs"][pool]["cloudInit"]))
    config["nodeConfigs"][pool]["cloudInit"] = encode_cloud_config(text)
    encoded = base64.b64encode(json.dumps(config).encode()).decode()
    rc["change"]["after"]["triggers_replace"]["template"] = re.sub(
        r"config\.json: [A-Za-z0-9+/=]+", lambda _: f"config.json: {encoded}", template)
    return result


def edit_change(plan: dict[str, Any], pattern: str, edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    edit(next(r for r in result["resource_changes"] if re.search(pattern, r["address"]))["change"]["after"])
    return result


def drop_change(plan: dict[str, Any], pattern: str) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    victim = next(r for r in result["resource_changes"] if re.search(pattern, r["address"]))
    result["resource_changes"].remove(victim)
    return result


def change_script_hash(plan: dict[str, Any], name: str) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    result["output_changes"]["zz_r0_script_sha256"]["after"][name] = "0" * 64
    return result


def edit_scripts(plan: dict[str, Any], edit: Callable[[str], str]) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    texts = result["output_changes"]["zz_r0_registries_scripts"]["after"]
    for name in REGISTRIES_SCRIPTS:
        texts[name] = edit(texts[name])
    return result


def guard_edit(how: str, guard: str = "") -> Callable[[str], str]:
    """Edit the ca_file guard of a registries update script (counter-tests only)."""

    def edit(text: str) -> str:
        lines = text.splitlines(keepends=True)
        if how == "add":
            at = next(i for i, line in enumerate(lines) if line == "else\n") + 1
            return "".join(lines[:at]) + guard + "".join(lines[at:])
        start = next(i for i, line in enumerate(lines) if line.startswith("  CA_FILE="))
        block = lines[start:start + 5]
        if how == "remove":
            del lines[start:start + 5]
        elif how == "late":
            del lines[start:start + 5]
            at = next(i for i, line in enumerate(lines) if i >= start and line.startswith("  rm -f ")) + 1
            lines[at:at] = block
        elif how == "path":
            payload = base64.b64encode(b"/etc/rancher/k3s/other-ca.pem").decode()
            lines[start] = GUARD_PAYLOAD.sub(lambda _: f"  CA_FILE=$(printf '%s' '{payload}' | base64 -d)", lines[start])
        elif how == "no-exit":
            lines.remove("    exit 1\n")
        return "".join(lines)
    return edit


def set_check(plan: dict[str, Any], status: str | None) -> dict[str, Any]:
    """Set the reference check status, or drop the check for None (counter-tests only)."""

    result = copy.deepcopy(plan)
    found = [c for c in result["checks"] if c["address"]["to_display"] == REFERENCE_CHECK]
    if status is None:
        result["checks"] = [c for c in result["checks"] if c not in found]
    for check in found:
        check["status"] = status
    return result


def edit_config(plan: dict[str, Any], module: str, address: str, edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Edit one resource of the plan configuration (counter-tests only)."""

    result = copy.deepcopy(plan)
    root = result["configuration"]["root_module"]
    resources = (root["module_calls"][module]["module"] if module else root)["resources"]
    edit(next(r for r in resources if r["address"] == address))
    return result


def ca_lines(text: str, path: str) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    start = lines.index(f"- path: {path}\n")
    return start, start + len(CA_KEYS)


def text_edit(path: str, how: str) -> Callable[[str], str]:
    def edit(text: str) -> str:
        lines = text.splitlines(keepends=True)
        start, end = ca_lines(text, path)
        if how == "remove":
            del lines[start:end]
        elif how == "mode":
            lines[start + 2] = lines[start + 2].replace('"0644"', '"0600"')
        elif how == "owner":
            lines.insert(end, "  owner: root:root\n")
        elif how == "content":
            # One base64 character of the JSON-encoded PEM.
            lines[start + 1] = lines[start + 1].replace("CERTIFICATE-----\\nMII", "CERTIFICATE-----\\nMIJ", 1)
        elif how == "path":
            lines[start] = lines[start].replace("/etc/rancher/rke2/", "/etc/rancher/k3s/")
        elif how == "other":
            index = next(i for i, line in enumerate(lines) if "PasswordAuthentication no" in line)
            lines[index] = lines[index].replace("PasswordAuthentication no", "PasswordAuthentication yes")
        return "".join(lines)
    return edit


def in_memory_counter_tests(
    renders: dict[str, dict[str, Any]], base: dict[str, Any], scripts: dict[str, str], pem: str,
    args: argparse.Namespace,
) -> list[str]:
    """Return the names of counter-tests that did NOT turn the gate RED."""

    path = {name: f"{CA_DIRS[spec[1]]}/{CA_NAME}" for name, spec in VARIANTS.items()}

    def run_ca(variant: str, plan: dict[str, Any], script: str | None = None, max_len: int | None = None) -> list[str]:
        local_args = copy.copy(args)
        if max_len is not None:
            local_args.max_len = max_len
        return check_ca(variant, base[f"{variant}_empty"], plan,
                        scripts[variant] if script is None else script, pem, local_args)[0]

    head = {variant: renders[f"{variant}_ca"] for variant in VARIANTS}
    smallest = min(len(ci) for ci in pools(autoscaler_template(changes(head["microos"]))).values())
    cp_registries = r'^module\.control_planes\["[^"]+"\]\.terraform_data\.registries$'

    def pool(variant: str, how: str) -> list[str]:
        return run_ca(variant, edit_pool(head[variant], text_edit(path[variant], how)))

    def host(variant: str, how: str) -> list[str]:
        return run_ca(variant, edit_host(head[variant], text_edit(path[variant], how)))

    def registries_trigger_changed(after: dict[str, Any]) -> None:
        after["triggers_replace"]["registries"] += "#"

    def ca_trigger_watches_registries(after: dict[str, Any]) -> None:
        after["triggers_replace"]["registries"] = ""

    def empty_with_ca_resource() -> dict[str, Any]:
        extra = [r for r in renders["microos_ca"]["resource_changes"] if "registries_ca_files" in r["address"]]
        return {**renders["microos_empty"],
                "resource_changes": renders["microos_empty"]["resource_changes"] + extra[:1]}

    guard_after = outputs(head["registries"])["zz_r0_registries_scripts"]["k3s_registries_update_script"]
    guard_before = outputs(base["registries_empty"])["zz_r0_registries_scripts"]["k3s_registries_update_script"]
    guard_lines = guard_after.splitlines(keepends=True)
    guard = "".join(line for op in difflib.SequenceMatcher(
        None, guard_before.splitlines(keepends=True), guard_lines, autojunk=False).get_opcodes()
        if op[0] == "insert" for line in guard_lines[op[3]:op[4]])

    def guarded(how: str) -> list[str]:
        return run_ca("registries", edit_scripts(head["registries"], guard_edit(how)))

    k3s_restart = scripts["microos"] + "systemctl restart k3s-agent\n"
    rke2_restart = scripts["rke2"].replace("set -eu\n", "set -eu\nsystemctl restart rke2-agent\n")
    cases = [
        ("microos: CA entry missing in a pool", lambda: pool("microos", "remove"), "CA entry missing"),
        ("microos: CA entry missing on a control plane", lambda: host("microos", "remove"), "CA entry missing"),
        ("microos: CA entry mode 0600", lambda: pool("microos", "mode"), "CA entry permissions"),
        ("leapmicro: CA entry gets an owner", lambda: host("leapmicro", "owner"), "CA entry keys"),
        ("microos: CA content altered", lambda: pool("microos", "content"), "CA entry content != PEM"),
        ("microos: other entry changed", lambda: pool("microos", "other"), "write_files entry changed"),
        ("rke2: CA entry at the k3s path", lambda: pool("rke2", "path"), "CA entry missing"),
        ("microos: restart in the CA-only SSH path",
         lambda: run_ca("microos", head["microos"], k3s_restart), "CA update script restarts"),
        ("rke2: restart in the CA-only SSH path",
         lambda: run_ca("rke2", head["rke2"], rke2_restart), "CA update script restarts"),
        ("registries: registries trigger changed",
         lambda: run_ca("registries", edit_change(head["registries"], cp_registries, registries_trigger_changed)),
         "registries trigger changed"),
        ("microos: CA trigger also watches registries.yaml",
         lambda: run_ca("microos", edit_change(head["microos"], "autoscaled_nodes_registries_ca_files",
                                               ca_trigger_watches_registries)),
         "CA trigger"),
        ("microos: CA resource missing on the autoscaled node",
         lambda: run_ca("microos", drop_change(head["microos"], "autoscaled_nodes_registries_ca_files")),
         "CA resource instances"),
        ("microos: pool over --max-len", lambda: run_ca("microos", head["microos"], max_len=smallest - 1), "want <= "),
        ("microos: empty input adds a CA resource",
         lambda: check_empty("microos", base["microos_empty"], empty_with_ca_resource()),
         "empty input: resource instances added"),
        ("microos: empty input changes a control plane cloud-init",
         lambda: check_empty("microos", base["microos_empty"],
                             edit_host(renders["microos_empty"], lambda text: text + "# changed\n")),
         "empty input: planned values changed"),
        ("registries: ca_file guard missing", lambda: guarded("remove"), "ca_file guard missing"),
        ("registries: ca_file guard after the backup step", lambda: guarded("late"),
         "ca_file guard is not the first step"),
        ("registries: ca_file guard checks another path", lambda: guarded("path"), "ca_file guard checks"),
        ("registries: ca_file guard does not exit", lambda: guarded("no-exit"),
         "ca_file guard is not exactly one failing certificate check"),
        ("microos: ca_file guard without a reference",
         lambda: check_empty("microos", base["microos_empty"],
                             edit_scripts(renders["microos_empty"], guard_edit("add", guard))),
         "changed without a ca_file reference"),
        ("registries: no warning for a dangling ca_file",
         lambda: check_empty("registries", base["registries_empty"], set_check(renders["registries_empty"], "pass")),
         "status 'pass', want 'fail'"),
        ("microos: warning without a dangling ca_file",
         lambda: run_ca("microos", set_check(head["microos"], "fail")), "status 'fail', want 'pass'"),
        ("registries: reference check missing",
         lambda: run_ca("registries", set_check(head["registries"], None)), "status None"),
        ("registries: control plane registries without the CA ordering",
         lambda: run_ca("registries", edit_config(head["registries"], "control_planes", "terraform_data.registries",
                                                  lambda r: r.update(depends_on=["terraform_data.initial_readiness"]))),
         "module.control_planes.terraform_data.registries does not depend on"),
        ("microos: autoscaled registries without the CA ordering",
         lambda: run_ca("microos", edit_config(head["microos"], "", "terraform_data.autoscaled_nodes_registries",
                                               lambda r: r.pop("depends_on"))),
         "terraform_data.autoscaled_nodes_registries does not depend on"),
        ("rke2: agent CA files wait for registries.yaml",
         lambda: run_ca("rke2", edit_config(head["rke2"], "agents", "terraform_data.registries_ca_files",
                                            lambda r: r["depends_on"].append("terraform_data.registries"))),
         "module.agents.terraform_data.registries_ca_files depends on a registries resource"),
        ("microos: empty input loses a depends_on entry",
         lambda: check_empty("microos", base["microos_empty"],
                             edit_config(renders["microos_empty"], "agents", "terraform_data.registries",
                                         lambda r: r.update(depends_on=["terraform_data.registries_ca_files"]))),
         "empty input: depends_on entries lost"),
        ("rke2: empty input changes a provisioner script",
         lambda: check_empty("rke2", base["rke2_empty"],
                             change_script_hash(renders["rke2_empty"], "k8s_registries_update_script")),
         "empty input: provisioner script changed"),
    ]
    failed = []
    for label, run, finding in cases:
        if not cig.counter_test(label, run(), finding):
            failed.append(label)
    return failed


MUTATIONS = {
    # label: (file, old, new, finding[, variant, default microos])
    "real render: CA entry mode 0600": (
        "locals.tf", '${jsonencode(pem)}\\n  permissions: \\"0644\\"\\n"', '${jsonencode(pem)}\\n  permissions: \\"0600\\"\\n"',
        "CA entry permissions"),
    "real render: restart in the CA update script": (
        "locals.tf", "%{endfor~}\nEOF", "systemctl restart k3s-agent\n%{endfor~}\nEOF", "CA update script restarts"),
    "real render: CA resource without the empty-map guard": (
        "modules/host/main.tf", "count = length(var.registries_ca_files) > 0 ? 1 : 0", "count = 1",
        "empty input: resource instances added"),
    "real render: ca_file guard leaves a blank line without references": (
        "locals.tf", "%{endfor~}\n  rm -f /etc/rancher/k3s/registries.yaml.bak",
        "%{endfor}\n  rm -f /etc/rancher/k3s/registries.yaml.bak", "changed without a ca_file reference"),
    "real render: reference check that always passes": (
        "validation-contract.tf", '!startswith(ca_file, "${local.registries_config_dir}/") || contains(',
        'true || contains(', "Missing expected failure", "registries"),
    "real render: host registries without the CA ordering": (
        "modules/host/main.tf", "depends_on = [terraform_data.initial_readiness, terraform_data.registries_ca_files]",
        "depends_on = [terraform_data.initial_readiness]",
        "module.control_planes.terraform_data.registries does not depend on"),
    "real render: autoscaled registries without the CA ordering": (
        "autoscaler-agents.tf", "  depends_on = [terraform_data.autoscaled_nodes_registries_ca_files]\n", "",
        "terraform_data.autoscaled_nodes_registries does not depend on"),
    "real render: CA ordering reversed": (
        "modules/host/main.tf",
        ("depends_on = [terraform_data.initial_readiness, terraform_data.registries_ca_files]",
         "inline = [var.registries_ca_update_script]\n  }\n\n  depends_on = [terraform_data.initial_readiness]\n"),
        ("depends_on = [terraform_data.initial_readiness]",
         "inline = [var.registries_ca_update_script]\n  }\n\n"
         "  depends_on = [terraform_data.initial_readiness, terraform_data.registries]\n"),
        "terraform_data.registries_ca_files depends on a registries resource"),
    "real render: CA entries rendered by a template directive": (
        "locals.tf", "  path: ${local.kubelet_config_file}\n%{endif}\nEOT",
        "  path: ${local.kubelet_config_file}\n%{endif}\n%{for name, pem in var.registries_ca_files}\n%{endfor}\nEOT",
        "empty input: planned values changed"),
}


def mutation_counter_tests(
    head_tree: Path, scratch: Path, base: dict[str, Any], names: list[str], pem: str, args: argparse.Namespace,
) -> list[str]:
    failed = []
    for number, (label, (name, old, new, finding, *rest)) in enumerate(MUTATIONS.items()):
        variant = rest[0] if rest else "microos"
        tree = scratch / f"mutation-{number}"
        shutil.copytree(head_tree, tree, ignore=COPY_IGNORE, symlinks=True)
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
            plans = render(tree, args, (variant,), CASES, pem, names)
        except GateError as exc:
            # A check that no longer fails where it must fails the test run.
            if not cig.counter_test(label, [str(exc)], finding):
                failed.append(label)
            continue
        red = check_empty(variant, base[f"{variant}_empty"], plans[f"{variant}_empty"])
        script = outputs(plans[f"{variant}_ca"]).get("zz_registries_ca_update_script") or ""
        red += check_ca(variant, base[f"{variant}_empty"], plans[f"{variant}_ca"], script, pem, args)[0]
        if not cig.counter_test(label, red, finding):
            failed.append(label)
    return failed


# ---------------------------------------------------------------- main


def dummy_ca(scratch: Path) -> str:
    """Write a throwaway ECDSA P-256 CA certificate; the key is deleted at once."""

    key, cert = scratch / "dummy-ca.key", scratch / "dummy-ca.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "3650", "-subj", "/CN=registries-ca-files-gate-dummy",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    key.unlink()
    return cert.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="bebe1630937c1f47b28dda16aa1be312f1ba2337",
                        help="git ref of the module before the change")
    parser.add_argument("--head", default="HEAD", help="git ref after the change, or WORKTREE")
    parser.add_argument("--cli", default="tofu", choices=("tofu", "terraform"))
    parser.add_argument("--plugin-dir", help="provider plugin directory for an offline init")
    parser.add_argument("--ca-file", help="PEM CA certificate to render (default: a throwaway ECDSA P-256 CA)")
    parser.add_argument("--max-len", type=int, default=26600)
    parser.add_argument("--delta-min", type=int, default=300)
    parser.add_argument("--delta-max", type=int, default=1200)
    parser.add_argument("--no-counter-test", action="store_true", help="skip the mutated real renders")
    parser.add_argument("--variants", default=",".join(VARIANTS),
                        help="comma-separated variants to render; a subset skips the counter-tests")
    parser.add_argument("--keep", help="keep the scratch trees and plans in this new directory")
    args = parser.parse_args()
    variants = tuple(args.variants.split(","))
    if not variants or not set(variants) <= set(VARIANTS):
        parser.error(f"--variants must be a subset of {','.join(VARIANTS)}")
    partial = set(variants) != set(VARIANTS)

    scratch = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="kh-registries-ca-gate-"))
    if args.keep:
        scratch.mkdir(parents=True)
    red: list[str] = []
    try:
        pem = Path(args.ca_file).read_text(encoding="utf-8") if args.ca_file else dummy_ca(scratch)
        base_tree, head_tree = scratch / "base", scratch / "head"
        cig.export_tree(args.base, base_tree)
        cig.export_tree(args.head, head_tree)
        names = sorted(set(script_locals(base_tree)) & set(script_locals(head_tree)))
        print(f"rendering base {args.base} and head {args.head} ({args.cli} test -verbose -json, mocked cloud providers)")
        print(f"  CA: {len(pem)} B PEM, {len(names)} provisioner script locals hashed")
        base = render(base_tree, args, variants, ("empty",), pem, names)
        head = render(head_tree, args, variants, CASES, pem, names)
        scripts = {variant: outputs(head[f"{variant}_ca"]).get("zz_registries_ca_update_script") or ""
                   for variant in variants}
        for variant in variants:
            distribution = VARIANTS[variant][1]
            empty_red = check_empty(variant, base[f"{variant}_empty"], head[f"{variant}_empty"])
            same = not empty_red
            print(f"{variant}: empty input {'plans exactly like the base' if same else 'differs from the base'} "
                  f"({len(base[f'{variant}_empty']['resource_changes'])} resource changes)")
            ca_red, lengths = check_ca(variant, base[f"{variant}_empty"], head[f"{variant}_ca"],
                                       scripts[variant], pem, args)
            added = len(set(changes(head[f"{variant}_ca"])) - set(changes(base[f"{variant}_empty"])))
            print(f"{variant}: with the CA {added} CA resources added, CA files in {CA_DIRS[distribution]}")
            for name, before, after in lengths:
                print(f"  pool {name}: {before} -> {after} B (+{after - before} B, {cig.USER_DATA_LIMIT - after} B left)")
            for finding in empty_red + ca_red:
                print(f"  RED {finding}")
            red += empty_red + ca_red

        if red:
            print("counter-tests skipped: the head is already RED")
        elif partial:
            print("counter-tests skipped: they need all variants")
        else:
            print("counter-tests (each must be RED):")
            red += [f"counter-test stayed GREEN: {label}"
                    for label in in_memory_counter_tests(head, base, scripts, pem, args)]
            if not args.no_counter_test:
                red += [f"counter-test stayed GREEN: {label}"
                        for label in mutation_counter_tests(head_tree, scratch, base, names, pem, args)]
    except (GateError, cig.GateError, subprocess.CalledProcessError, KeyError, ValueError, StopIteration) as exc:
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
