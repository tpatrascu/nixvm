#!/usr/bin/env python3
"""Internal OpenTofu callbacks and helpers for the nixvm wrapper."""
from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
HOSTS_FILE = Path("/etc/hosts")
HOSTS_MARKER = "# Managed by nixvm"


def run(args, *, capture=False, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, **kwargs)


def output(args, **kwargs):
    return run(args, capture=True, **kwargs).stdout.strip()


def connection():
    if "NIXVM_CONNECTION" in os.environ:
        return json.loads(os.environ["NIXVM_CONNECTION"])
    return json.loads(output(["tofu", f"-chdir={ROOT / 'infrastructure'}", "output", "-json", "connection"]))


def virsh(c, *args, **kwargs):
    return output(["virsh", "--connect", c["uri"], *args], **kwargs)


def guest_ip(c):
    leases = virsh(c, "net-dhcp-leases", c["network"], "--mac", c["mac"])
    for word in leases.split():
        if "/" in word:
            try:
                ip = ipaddress.ip_interface(word).ip
                if ip.version == 4:
                    return str(ip)
            except ValueError:
                pass
    raise RuntimeError("No IPv4 DHCP lease yet for the VM MAC")


def wait_guest_ip(c, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return guest_ip(c)
        except RuntimeError as error:
            if str(error) != "No IPv4 DHCP lease yet for the VM MAC":
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for an IPv4 DHCP lease for {c['name']} on network {c['network']!r}"
                ) from error
            time.sleep(2)


def ssh_args(c, *, tty=False):
    runtime = ROOT / ".runtime" / c["name"]
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={runtime / 'known_hosts'}",
            "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
    identity = os.environ.get("NIXVM_SSH_IDENTITY") or c.get("identity")
    if identity:
        args += ["-i", str(Path(identity).expanduser()), "-o", "IdentitiesOnly=yes"]
    if tty:
        args += ["-t"]
    return args + [f"{c['username']}@{guest_ip(c)}"]


def remote(c, command, **kwargs):
    return run(ssh_args(c) + [command], **kwargs)


def spice(c, extra=()):
    """Open the local SPICE console and resize the guest to fit the viewer."""
    run(["virt-viewer", "-c", c["uri"], "--domain-name", c["name"], *extra,
         "--auto-resize", "always"])


def reset_guest_host_key(c):
    """Allow first-contact trust after an explicitly triggered bootstrap."""
    runtime = ROOT / ".runtime" / c["name"]
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    known_hosts = runtime / "known_hosts"
    known_hosts.touch(mode=0o600, exist_ok=True)
    run(["ssh-keygen", "-R", guest_ip(c), "-f", known_hosts], capture=True)


def wait_ssh(c, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            remote(c, "true", capture=True, stderr=subprocess.DEVNULL)
            return
        except (RuntimeError, subprocess.CalledProcessError):
            time.sleep(3)
    raise RuntimeError("Timed out waiting for SSH; inspect virsh console and the DHCP network")


def reboot_guest(c, timeout=600):
    """Reboot into the just-activated generation and verify GNOME starts."""
    remote(c, "sudo systemd-run --quiet --on-active=2s --collect "
           "/run/current-system/sw/bin/systemctl reboot")
    deadline = time.monotonic() + timeout
    disconnected = False
    while time.monotonic() < deadline:
        try:
            remote(c, "true", capture=True, stderr=subprocess.DEVNULL)
        except (RuntimeError, subprocess.CalledProcessError):
            disconnected = True
            break
        time.sleep(1)
    if not disconnected:
        raise RuntimeError("Guest did not disconnect after the scheduled reboot")
    wait_ssh(c, max(1, int(deadline - time.monotonic())))
    remote(c, "systemctl is-active graphical.target && "
           "systemctl is-active display-manager.service")


def domain_state(c):
    return virsh(c, "domstate", c["name"]).strip().lower()


def wait_domain_state(c, expected, timeout):
    deadline = time.monotonic() + timeout
    while True:
        state = domain_state(c)
        if state == expected:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for VM state {expected!r}; current state is {state!r}")
        time.sleep(1)


def start_vm(c, timeout=120, wait_for_ssh=False):
    state = domain_state(c)
    if state == "running":
        print(f"{c['name']} is already running.")
    elif state == "paused":
        validate_gpu_for_start(c)
        ensure_vfio_devices(c)
        virsh(c, "resume", c["name"])
        wait_domain_state(c, "running", timeout)
    elif state in {"shut off", "crashed"}:
        validate_gpu_for_start(c)
        ensure_vfio_devices(c)
        virsh(c, "start", c["name"])
        wait_domain_state(c, "running", timeout)
    else:
        raise RuntimeError(f"Cannot start {c['name']} while libvirt reports state {state!r}")
    if wait_for_ssh:
        wait_ssh(c, timeout)
        print("Guest SSH is ready.")


def stop_vm(c, timeout=120, force=False):
    state = domain_state(c)
    if state in {"shut off", "crashed"}:
        print(f"{c['name']} is already stopped (state: {state}).")
        return
    if force:
        virsh(c, "destroy", c["name"])
        wait_domain_state(c, "shut off", min(timeout, 30))
        print(f"{c['name']} was force-stopped.")
        return
    if state == "paused":
        virsh(c, "resume", c["name"])
    elif state != "running":
        raise RuntimeError(f"Cannot gracefully stop {c['name']} while libvirt reports state {state!r}")
    virsh(c, "shutdown", c["name"])
    try:
        wait_domain_state(c, "shut off", timeout)
    except RuntimeError as error:
        raise RuntimeError(f"{error}. Retry with 'stop --force' only if graceful shutdown is stuck") from error
    print(f"{c['name']} shut down gracefully.")


def build_image():
    if "NIXVM_CONNECTION" in os.environ:
        preflight(connection())
    settings = json.loads(os.environ["NIXVM_CONFIG"])
    build = Path(os.environ["NIXVM_BUILD"])
    build.mkdir(parents=True, exist_ok=True)
    image_cache = Path(os.environ.get("NIXVM_IMAGE_CACHE", ROOT / ".build" / "shared"))
    image_cache.mkdir(parents=True, exist_ok=True)
    bootstrap_settings = {
        key: settings[key] for key in (
            "username", "uid", "gid", "timezone", "locale", "keyboard", "sshPublicKeys"
        ) if key in settings
    }
    bootstrap_settings["hostname"] = "nixvm-bootstrap"
    bootstrap_settings["mitmproxy"] = {"enabled": False, "proxyUrl": ""}
    bootstrap_key = hashlib.sha256(json.dumps(bootstrap_settings, sort_keys=True).encode()).hexdigest()[:16]
    base_image = image_cache / f"bootstrap-{bootstrap_key}.qcow2"
    image = build / "bootstrap.qcow2"
    snapshot = build / "nix-source"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(ROOT / "nix", snapshot, ignore=shutil.ignore_patterns("current.json", "result*"))
    (snapshot / "machines/current.json").write_text(json.dumps(bootstrap_settings, indent=2) + "\n")
    proxy_ca = ROOT / ".runtime" / "shared" / "mitmproxy-ca-cert.pem"
    if proxy_ca.is_file():
        shutil.copyfile(proxy_ca, snapshot / "machines" / "mitmproxy-ca.pem")
    nix = ["nix", "--extra-experimental-features", "nix-command flakes"]
    if not base_image.is_file():
        result_link = image_cache / f"image-result-{os.getpid()}"
        run([*nix, "build", f"path:{snapshot}#bootstrap", "--out-link", result_link])
        images = list(result_link.rglob("*.qcow2"))
        if len(images) != 1:
            raise RuntimeError(f"Expected one QCOW2 image, found {len(images)}")
        temporary_base = image_cache / f"{base_image.name}.{os.getpid()}.tmp"
        shutil.copyfile(images[0], temporary_base)
        temporary_base.replace(base_image)
        result_link.unlink(missing_ok=True)
    print(f"Using shared bootstrap base image: {base_image}")
    shutil.copyfile(base_image, image)
    # Each VM gets its own writable copy and virtual disk capacity.
    run(["qemu-img", "resize", image, os.environ["NIXVM_DISK_GIB"] + "G"])

    build_workstation_cache(build, settings)


def build_workstation_cache(build, settings):
    """Build the selected workstation closure into the host Nix store."""
    snapshot = build / "workstation-source"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(ROOT / "nix", snapshot, ignore=shutil.ignore_patterns("current.json", "result*"))
    (snapshot / "machines/current.json").write_text(json.dumps(settings, indent=2) + "\n")
    proxy_ca = ROOT / ".runtime" / "shared" / "mitmproxy-ca-cert.pem"
    if proxy_ca.is_file():
        shutil.copyfile(proxy_ca, snapshot / "machines" / "mitmproxy-ca.pem")
    run(["nix", "--extra-experimental-features", "nix-command flakes",
         "build", f"path:{snapshot}#default", "--out-link", build / "workstation-result"])


def remove_image_cache(name):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
        raise ValueError("VM name must match [a-z][a-z0-9-]{0,30}")
    build = ROOT / ".build" / name
    if not build.exists():
        print(f"No build cache found for {name}.")
        return
    shutil.rmtree(build)
    print(f"Removed build cache for {name}: {build}")


def add_network_filter_rule(root, action, *, element, direction="out", priority,
                            statematch=None, **attributes):
    rule_attributes = {
        "action": action,
        "direction": direction,
        "priority": str(priority),
    }
    if statematch is not None:
        rule_attributes["statematch"] = str(statematch).lower()
    rule = ET.SubElement(root, "rule", rule_attributes)
    ET.SubElement(rule, element, {key: str(value) for key, value in attributes.items()})


def add_network_access_rules(root, access):
    if "allow" in access:
        rules = access["allow"]
    else:
        rules = [
            {"ip": address, "ports": access["ports"], "all_ports": access["all_ports"]}
            for address in access["ips"]
        ]
    for rule_config in rules:
        address = rule_config["ip"]
        ip = ipaddress.ip_address(address)
        if rule_config["all_ports"]:
            # The generic ipv6 element is matched at the bridge layer. The
            # all-ipv6 protocol element goes through ip6tables, whose libvirt
            # per-interface chains are unavailable on some nftables hosts.
            add_network_filter_rule(root, "accept", element="all" if ip.version == 4 else "ipv6",
                                    priority=100,
                                    dstipaddr=ip.compressed)
            continue
        for port in rule_config["ports"]:
            for transport in rule_config.get("protocols") or ("tcp", "udp"):
                add_network_filter_rule(
                    root, "accept", element=transport if ip.version == 4 else "ipv6", priority=100,
                    dstipaddr=ip.compressed,
                    **({} if ip.version == 4 else {"protocol": transport}),
                    dstportstart=port, dstportend=port,
                )


def existing_network_filter_uuid(uri, name):
    result = subprocess.run(
        ["virsh", "--connect", uri, "nwfilter-dumpxml", name],
        capture_output=True, text=True,
    )
    if result.returncode:
        message = (result.stdout + result.stderr).lower()
        if "failed to get nwfilter" in message or "no nwfilter" in message:
            return None
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)

    try:
        existing = ET.fromstring(result.stdout)
    except ET.ParseError as error:
        raise RuntimeError(f"Could not parse existing libvirt filter {name!r}.") from error
    if existing.get("name") != name:
        raise RuntimeError(f"Libvirt returned a different filter while looking up {name!r}.")
    uuid = existing.findtext("uuid")
    if not uuid:
        raise RuntimeError(f"Existing libvirt filter {name!r} has no UUID; refusing to replace it blindly.")
    return uuid


def build_network_filter(name, access, filter_uuid=None):
    """Build the libvirt nwfilter XML for the supplied access policy."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}-access", name):
        raise ValueError(f"Invalid libvirt network filter name: {name}")
    root = ET.Element("filter", {"name": name, "chain": "root"})
    if filter_uuid:
        ET.SubElement(root, "uuid").text = filter_uuid
    # clean-traffic contains an unconditional outbound IPv4 accept that would
    # shadow these destination rules. Keep its protections as separate refs,
    # then allow non-denied IPv4/IPv6 traffic at the end of our policy.
    for filter_name in (
        "no-mac-spoofing",
        "no-ip-spoofing",
        "allow-incoming-ipv4",
        "no-arp-spoofing",
    ):
        ET.SubElement(root, "filterref", {"filter": filter_name})
    add_network_filter_rule(root, "accept", element="mac", direction="inout",
                            priority=-500, protocolid="arp")
    ET.SubElement(root, "filterref", {"filter": "no-other-l2-traffic"})
    ET.SubElement(root, "filterref", {"filter": "qemu-announce-self-rarp"})
    add_network_filter_rule(root, "accept", element="all", priority=50,
                            state="ESTABLISHED,RELATED")
    # DHCP client requests use UDP 68 -> 67, including limited-broadcast
    # destinations that are otherwise denied by the internal-range policy.
    add_network_filter_rule(root, "accept", element="udp", priority=75,
                            srcportstart=68, srcportend=68,
                            dstportstart=67, dstportend=67)

    # Host allow rules are explicit and are evaluated before internal-network
    # allow rules; remaining traffic to listed host IPs is denied.
    add_network_access_rules(root, access["host"])

    # Explicit internal per-IP port rules may override the global web-port
    # block. Keep this exception narrow: all_ports does not include these
    # ports, and host_access never overrides the global block.
    blocked_ports = set(access["internal"].get("blocked_ports", []))
    for rule_config in access["internal"]["allow"]:
        ip = ipaddress.ip_address(rule_config["ip"])
        if rule_config["all_ports"]:
            continue
        for port in set(rule_config["ports"]).intersection(blocked_ports):
            for transport in rule_config.get("protocols") or ("tcp", "udp"):
                add_network_filter_rule(
                    root, "accept", element=transport if ip.version == 4 else "ipv6",
                    priority=85, dstipaddr=ip.compressed,
                    **({} if ip.version == 4 else {"protocol": transport}),
                    dstportstart=port, dstportend=port,
                )
    add_network_access_rules(root, access["internal"])

    # Apply global web-port blocks before broad per-IP allow rules, including
    # all_ports. The narrowly-scoped proxy exception above is evaluated first.
    for port in sorted(blocked_ports):
        for transport in ("tcp", "udp"):
            add_network_filter_rule(
                root, "drop", element=transport, priority=90,
                dstportstart=port, dstportend=port,
            )
            add_network_filter_rule(
                root, "drop", element="ipv6", priority=90,
                protocol=transport, dstportstart=port, dstportend=port,
            )

    host_ips = {ipaddress.ip_address(address).compressed for address in access["host"]["ips"]}
    internal_allow = access["internal"]["allow"]
    internal_ips = {
        ipaddress.ip_address(item["ip"]).compressed for item in internal_allow
        if not item.get("managed_allow")
    }
    overlap = host_ips.intersection(internal_ips)
    if overlap:
        raise ValueError("List host IPs only in host_access, not internal_network_allow: " + ", ".join(sorted(overlap)))
    for address in sorted(host_ips):
        ip = ipaddress.ip_address(address)
        add_network_filter_rule(
            root, "drop", element="all" if ip.version == 4 else "ipv6", priority=800,
            # IPv6 header rules use bridge matching and do not carry the
            # ip6tables state attribute; the rule-level switch keeps this
            # destination drop independent of conntrack.
            statematch=False if ip.version == 6 else None,
            dstipaddr=ip.compressed, **({"state": "NEW"} if ip.version == 4 else {}),
        )

    internal_ranges = [ipaddress.ip_network(cidr) for cidr in access["internal"]["deny_cidrs"]]
    for rule in internal_allow:
        if rule.get("managed_allow"):
            continue
        address = rule["ip"]
        ip = ipaddress.ip_address(address)
        if not any(ip.version == network.version and ip in network for network in internal_ranges):
            raise ValueError(f"Internal allow rule {address} is outside internal_network_deny")
    for network in internal_ranges:
        add_network_filter_rule(
            root, "drop", element="all" if network.version == 4 else "ipv6", priority=900,
            statematch=False if network.version == 6 else None,
            dstipaddr=network.network_address.compressed,
            dstipmask=network.prefixlen,
            **({"state": "NEW"} if network.version == 4 else {}),
        )
    add_network_filter_rule(root, "accept", element="ip", priority=999)
    add_network_filter_rule(root, "accept", element="ipv6", priority=999)
    return root


def define_network_filter_config(name, access, uri):
    filter_uuid = existing_network_filter_uuid(uri, name)
    root = build_network_filter(name, access, filter_uuid)

    with tempfile.NamedTemporaryFile(mode="wb", prefix="nixvm-nwfilter-", suffix=".xml") as xml:
        ET.ElementTree(root).write(xml, encoding="utf-8", xml_declaration=True)
        xml.flush()
        run(["virsh", "--connect", uri, "nwfilter-define", xml.name])
    action = "Updated" if filter_uuid else "Defined"
    print(f"{action} host network filter: {name}")


def define_network_filter(extra=()):
    if "NIXVM_FILTER_NAME" in os.environ:
        name = os.environ["NIXVM_FILTER_NAME"]
        access = json.loads(os.environ["NIXVM_NETWORK_ACCESS"])
        uri = json.loads(os.environ["NIXVM_CONNECTION"])["uri"]
    else:
        config = tofu_firewall_test_config(extra)
        name = config["filter_name"]
        access = config["filter_access"]
        uri = config["connection"]["uri"]
    define_network_filter_config(name, access, uri)


def tofu_firewall_test_config(extra):
    expression = (
        "jsonencode({filter_name = local.network_filter_name, "
        "filter_access = local.network_filter_access, "
        "vm_name = local.connection.name, "
        "connection = local.connection, "
        "vm_network = {name = local.vm_network_name, uri = var.libvirt_uri, "
        "cidr = local.vm_network.cidr, gateway = local.vm_network.gateway}, "
        "network = {name = local.firewall_test_network_name, "
        "uri = var.libvirt_uri, bridge_network = local.vm_network_name, "
        "cidr = local.firewall_test_network.cidr, "
        "gateway = local.firewall_test_network.gateway, "
        "primary_gateway = local.vm_network.gateway, "
        "primary_mac = local.mac_address, "
        "allow_test_port = local.firewall_test_network.allow_test_port, "
        "deny_test_port = local.firewall_test_network.deny_test_port, "
        "prefix = local.firewall_test_network.prefix}})"
    )
    result = run(["tofu", f"-chdir={ROOT / 'infrastructure'}", "console", *extra],
                 capture=True, input=expression + "\n").stdout.strip()
    try:
        return json.loads(json.loads(result))
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"Could not read firewall test configuration from OpenTofu: {result}") from error


def tofu_mitmproxy_config(extra):
    expression = "jsonencode(local.mitmproxy_config)"
    result = run(["tofu", f"-chdir={ROOT / 'infrastructure'}", "console", *extra],
                 capture=True, input=expression + "\n").stdout.strip()
    try:
        return json.loads(json.loads(result))
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"Could not read mitmproxy configuration from OpenTofu: {result}") from error


def tofu_guest_config(extra):
    expression = "jsonencode(local.guest_config)"
    result = run(["tofu", f"-chdir={ROOT / 'infrastructure'}", "console", *extra],
                 capture=True, input=expression + "\n").stdout.strip()
    try:
        return json.loads(json.loads(result))
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"Could not read guest configuration from OpenTofu: {result}") from error


def ensure_vm_nat(extra=()):
    config = tofu_mitmproxy_config(extra)
    if not config.get("enabled", True):
        return
    result = subprocess.run(
        ["virsh", "--connect", config["uri"], "net-info", config["network"]],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0 and re.search(r"^Active:\s+yes\s*$", result.stdout, re.MULTILINE):
        return
    print(f"Starting the libvirt NAT network {config['network']} before launching mitmproxy.")
    run(["tofu", f"-chdir={ROOT / 'infrastructure'}", "apply", *extra,
         "-target=libvirt_network.vm_nat", "-auto-approve"])


def mitmproxy_allow_hosts(inspect_domains):
    patterns = []
    for entry in inspect_domains:
        if entry.startswith("glob:"):
            host_pattern = fnmatch.translate(entry[5:].lower())
            if host_pattern.endswith(r"\Z") or host_pattern.endswith(r"\z"):
                host_pattern = host_pattern[:-2]
        elif entry.startswith("regex:"):
            host_pattern = entry[6:]
            if host_pattern.startswith(r"\A"):
                host_pattern = host_pattern[2:]
            elif host_pattern.startswith("^"):
                host_pattern = host_pattern[1:]
            if host_pattern.endswith(r"\Z"):
                host_pattern = host_pattern[:-2]
            elif host_pattern.endswith("$"):
                host_pattern = host_pattern[:-1]
        else:
            domain = entry.lower().rstrip(".")
            host_pattern = rf"(?:[^.]+\.)*{re.escape(domain)}"
        patterns.append(rf"(?i:^(?:{host_pattern})(?::[0-9]+)?$)")
    # An empty list means inspect none. mitmproxy's empty allow-hosts option
    # means inspect everything, so use a regex that cannot match instead.
    return "|".join(patterns) if patterns else r"(?!)"


def mitmproxy_addon_policy_source():
    return ROOT / "scripts" / "mitmproxy_policy.py"


def ensure_podman():
    if not shutil.which("podman"):
        values = host_os_release()
        distribution = values.get("PRETTY_NAME", values.get("ID", "unknown"))
        if shutil.which("apt-get") is None:
            raise RuntimeError(
                f"Podman is missing on {distribution}; install Podman and rerun nixvm up."
            )
        admin = host_admin_prefix()
        print(f"Installing Podman on {distribution} for the host user service.")
        run(admin + ["apt-get", "update"])
        run(admin + ["apt-get", "install", "-y", "podman"])
    if not shutil.which("podman"):
        raise RuntimeError("Podman installation completed but the podman command is still unavailable.")
    version = output(["podman", "--version"])
    match = re.search(r"\b(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (4, 4):
        raise RuntimeError(f"Podman Quadlet support is required (Podman 4.4 or newer); found {version!r}.")
    ensure_podman_subids()


def subid_entries(path):
    entries = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return entries
    for line in lines:
        fields = line.split(":")
        if len(fields) != 3:
            continue
        try:
            start, count = int(fields[1]), int(fields[2])
        except ValueError:
            continue
        if count > 0:
            entries.append((fields[0], start, start + count - 1))
    return entries


def has_subid_capacity(entries, username, uid, minimum=65536):
    ranges = sorted((start, end) for owner, start, end in entries
                    if owner in (username, str(uid)))
    total = 0
    last_end = -1
    for start, end in ranges:
        start = max(start, last_end + 1)
        if start <= end:
            total += end - start + 1
            last_end = end
    return total >= minimum


def free_subid_range(entries, size=65536):
    occupied = sorted((start, end) for _, start, end in entries)
    candidate = 100000
    maximum = 2**32 - 1
    for start, end in occupied:
        if candidate + size - 1 < start:
            return candidate, candidate + size - 1
        if candidate <= end:
            candidate = end + 1
    if candidate + size - 1 <= maximum:
        return candidate, candidate + size - 1
    raise RuntimeError("No free 65,536-ID subordinate range is available for rootless Podman.")


def ensure_podman_subids():
    """Configure subordinate IDs required to unpack normal rootless images."""
    username = output(["id", "-un"])
    account = pwd.getpwnam(username)
    uid_path, gid_path = Path("/etc/subuid"), Path("/etc/subgid")
    uid_entries, gid_entries = subid_entries(uid_path), subid_entries(gid_path)
    need_uid = not has_subid_capacity(uid_entries, username, account.pw_uid)
    need_gid = not has_subid_capacity(gid_entries, username, account.pw_gid)
    if not need_uid and not need_gid:
        return

    running = subprocess.run(
        ["podman", "ps", "-q"], check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if running.returncode:
        raise RuntimeError(
            "Could not check for running rootless Podman containers before changing "
            "subordinate IDs. Resolve the Podman error and rerun nixvm up."
        )
    if running.stdout.strip():
        raise RuntimeError(
            "Rootless Podman needs subordinate UID/GID ranges, but this account has "
            "running containers. Stop them before rerunning nixvm up; podman system "
            "migrate must stop rootless containers for the mapping change."
        )

    usermod = shutil.which("usermod")
    if not usermod:
        raise RuntimeError("Rootless Podman needs subordinate IDs, but usermod is unavailable.")
    uid_range = free_subid_range(uid_entries) if need_uid else None
    gid_range = free_subid_range(gid_entries) if need_gid else None
    print(f"Configuring subordinate IDs for rootless Podman user {username}.")
    # Podman requires the old pause namespace to be stopped before subid files
    # change; with no running containers this does not interrupt other workloads.
    run(["podman", "system", "migrate"])
    command = [usermod]
    if uid_range:
        command += ["--add-subuids", f"{uid_range[0]}-{uid_range[1]}"]
    if gid_range:
        command += ["--add-subgids", f"{gid_range[0]}-{gid_range[1]}"]
    command.append(username)
    run(host_admin_prefix() + command)


def systemd_unit_value(value):
    """Escape a filesystem path for a systemd/Quadlet key-value setting."""
    return str(value).replace("\\", "\\\\").replace(" ", "\\x20")


def mitmproxy_quadlet(config, policy_dir, web_password):
    safe_name = "nixvm-shared-mitmproxy"
    allow_hosts = mitmproxy_allow_hosts(config["inspect_domains"])
    return f"""[Unit]
Description=NixVM shared HTTPS inspection proxy
[Container]
ContainerName={safe_name}
Image={config['image']}
Network=pasta
Volume={systemd_unit_value(policy_dir)}:/etc/mitmproxy/nixvm:ro
Volume={safe_name}-data:/home/mitmproxy/.mitmproxy
PublishPort=0.0.0.0:8080:8080
PublishPort=127.0.0.1:8081:8081
Environment=NIXVM_MITMPROXY_POLICY=/etc/mitmproxy/nixvm/policy.json
Exec=mitmweb --mode regular@0.0.0.0:8080 --web-host 0.0.0.0 --web-port 8081 --set=web_open_browser=false --set=web_password={web_password} --set=confdir=/home/mitmproxy/.mitmproxy --set=show_ignored_hosts=true --allow-hosts="{allow_hosts}" -s /etc/mitmproxy/nixvm/mitmproxy_policy.py

[Service]
Restart=always

[Install]
WantedBy=default.target
"""


def remove_legacy_mitmproxy_services(quadlet_dir):
    """Retire pre-shared per-VM services that occupied gateway port 8080."""
    for unit_file in quadlet_dir.glob("nixvm-*-mitmproxy.container"):
        unit_name = unit_file.stem
        if unit_name == "nixvm-shared-mitmproxy":
            continue
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{unit_name}.service"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        f"{unit_name}-forwarder.service"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        unit_file.unlink(missing_ok=True)
        (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
         / "systemd" / "user" / f"{unit_name}-forwarder.service").unlink(missing_ok=True)
        if shutil.which("podman"):
            subprocess.run(["podman", "rm", "--force", unit_name], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def initialize_mitmproxy_ca(image, volume_name="nixvm-shared-mitmproxy-data"):
    """Generate the persistent mitmproxy CA before exposing its public cert."""
    run([
        "podman", "run", "--rm", "--volume",
        f"{volume_name}:/home/mitmproxy/.mitmproxy",
        "--entrypoint", "mitmdump", image,
        "--set", "confdir=/home/mitmproxy/.mitmproxy",
        "--no-server", "--rfile", "/dev/null",
    ])


def wait_tcp_listener(address, port, timeout=120):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, port), timeout=2):
                return
        except OSError as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"Timed out waiting for listener {address}:{port}: {last_error}")


def setup_host_mitmproxy(extra=()):
    config = tofu_mitmproxy_config(extra)
    if not config.get("enabled", True):
        print("mitmproxy is disabled for this VM; skipping host proxy setup.")
        return
    ensure_podman()
    if shutil.which("systemctl") is None or shutil.which("loginctl") is None:
        raise RuntimeError("The host must use systemd to run the Podman mitmproxy service.")

    user_name = output(["id", "-un"])
    admin = host_admin_prefix()
    linger = output(["loginctl", "show-user", user_name, "--property=Linger", "--value"])
    if linger != "yes":
        run(admin + ["loginctl", "enable-linger", user_name])

    data_dir = Path.home() / ".local" / "share" / "nixvm" / "shared" / "mitmproxy"
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    policy_dir = data_dir / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    policy_dir.chmod(0o755)
    password_file = data_dir / "web_password"
    if not password_file.exists():
        password_file.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        password_file.chmod(0o600)
    web_password = password_file.read_text(encoding="utf-8").strip()
    shutil.copyfile(mitmproxy_addon_policy_source(), policy_dir / "mitmproxy_policy.py")
    (policy_dir / "mitmproxy_policy.py").chmod(0o644)
    policy = {
        "blocked_domains": config["blocked_domains"],
        "allowed_domains": config["allowed_domains"],
        "deny_cidrs": config["deny_cidrs"],
    }
    (policy_dir / "policy.json").write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    (policy_dir / "policy.json").chmod(0o644)

    quadlet_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "containers" / "systemd"
    quadlet_dir.mkdir(parents=True, exist_ok=True)
    remove_legacy_mitmproxy_services(quadlet_dir)
    unit_name = "nixvm-shared-mitmproxy"
    unit_file = quadlet_dir / f"{unit_name}.container"
    user_unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
    user_unit_dir.mkdir(parents=True, exist_ok=True)
    unit_contents = mitmproxy_quadlet(config, policy_dir, web_password)
    if not unit_file.exists() or unit_file.read_text(encoding="utf-8") != unit_contents:
        unit_file.write_text(unit_contents, encoding="utf-8")
        unit_file.chmod(0o600)

    print(f"Pulling current mitmproxy image {config['image']}.")
    run(["podman", "pull", config["image"]])
    # mitmproxy can defer CA creation while every configured TLS host is passed
    # through. Initialize the shared volume explicitly so bootstrap can always
    # distribute its public CA certificate, even with an empty inspect list.
    container = "nixvm-shared-mitmproxy"
    initialize_mitmproxy_ca(config["image"], f"{container}-data")
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "restart", f"{unit_name}.service"])
    wait_tcp_listener(config["gateway"], 8080)
    wait_tcp_listener("127.0.0.1", 8081)

    runtime_dir = ROOT / ".runtime" / "shared"
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca_certificate = runtime_dir / "mitmproxy-ca-cert.pem"
    run(["podman", "cp", f"{container}:/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem", ca_certificate])
    ca_certificate.chmod(0o644)
    print("mitmproxy is running. Traffic viewer: http://127.0.0.1:8081")
    print(f"mitmweb password is stored in {password_file}")


def cleanup_host_mitmproxy(name):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
        raise ValueError(f"Invalid VM name for mitmproxy cleanup: {name!r}")
    unit_name = "nixvm-shared-mitmproxy"
    quadlet_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "containers" / "systemd"
    unit_file = quadlet_dir / f"{unit_name}.container"
    subprocess.run(["systemctl", "--user", "disable", "--now",
                    f"{unit_name}-forwarder.service"], check=False)
    if unit_file.exists():
        subprocess.run(["systemctl", "--user", "stop", f"{unit_name}.service"], check=False)
        unit_file.unlink()
    (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
     / "systemd" / "user" / f"{unit_name}-forwarder.service").unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    if shutil.which("podman"):
        subprocess.run(["podman", "rm", "--force", unit_name], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(Path.home() / ".local" / "share" / "nixvm" / "shared" / "mitmproxy",
                  ignore_errors=True)
    (ROOT / ".runtime" / "shared" / "mitmproxy-ca-cert.pem").unlink(missing_ok=True)
    print("Stopped the shared managed mitmproxy service; its Podman CA volume was retained.")


def ufw_probe_allow_commands(admin, config, marker):
    return [
        admin + ["ufw", "allow", "in", "from", config["vm_network"]["cidr"],
                 "to", config["network"]["gateway"], "port",
                 str(config["network"]["allow_test_port"]), "proto", protocol,
                 "comment", marker]
        for protocol in ("tcp", "udp")
    ]


def run_firewall_tests(extra):
    config = tofu_firewall_test_config(extra)
    # Direct `nixvm firewall-test` runs must refresh the host filter too; the
    # live comparison below should never inspect a stale definition.
    define_network_filter_config(config["filter_name"], config["filter_access"],
                                 config["connection"]["uri"])
    print("Waiting for guest SSH before running live firewall probes.")
    wait_ssh(config["connection"], timeout=300)
    firewall = tofu_host_firewall_config(extra)
    admin = host_admin_prefix() if firewall["method"] == "ufw" else None
    probe_marker = f"nixvm-{firewall['name']}-firewall-test"
    env = os.environ.copy()
    env["NIXVM_FIREWALL_TEST_CONFIG"] = json.dumps(config)
    try:
        if admin is not None:
            # The test listener is temporary and isn't one of the VM's services.
            # Open only its probe port while the live guest connectivity tests run.
            if shutil.which("ufw") is None:
                raise RuntimeError("host_firewall is set to 'ufw', but ufw is not installed on the host.")
            for number in ufw_managed_rule_numbers(admin, probe_marker):
                run(admin + ["ufw", "--force", "delete", str(number)])
            for command in ufw_probe_allow_commands(admin, config, probe_marker):
                run(command)
        run([sys.executable, "-m", "unittest", "discover", "-s", ROOT / "tests",
             "-p", "test_firewall.py", "-v"], env=env)
    finally:
        if admin is not None:
            for number in ufw_managed_rule_numbers(admin, probe_marker):
                run(admin + ["ufw", "--force", "delete", str(number)])


def tofu_host_nfs_config(extra):
    expression = (
        "jsonencode({name = var.name, uri = var.libvirt_uri, network = local.vm_network_name, "
        "network_cidr = local.vm_network.cidr, "
        "exports = var.host_nfs_exports})"
    )
    result = run(["tofu", f"-chdir={ROOT / 'infrastructure'}", "console", *extra],
                 capture=True, input=expression + "\n").stdout.strip()
    try:
        return json.loads(json.loads(result))
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"Could not read host_nfs_exports from OpenTofu: {result}") from error


def tofu_host_firewall_config(extra):
    expression = (
        "jsonencode({name = local.host_firewall_config.name, "
        "method = local.host_firewall_config.method, "
        "network_cidr = local.host_firewall_config.network_cidr, "
        "gateway = local.host_firewall_config.gateway, "
        "nfs_enabled = local.host_firewall_config.nfs_enabled, "
        "nfs_host = local.host_firewall_config.nfs_host, "
        "mitmproxy_enabled = local.host_firewall_config.mitmproxy_enabled})"
    )
    result = run(["tofu", f"-chdir={ROOT / 'infrastructure'}", "console", *extra],
                 capture=True, input=expression + "\n").stdout.strip()
    try:
        return json.loads(json.loads(result))
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"Could not read host firewall configuration from OpenTofu: {result}") from error


def ufw_managed_rule_numbers(admin, marker):
    result = subprocess.run(
        admin + ["ufw", "status", "numbered"], check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        raise RuntimeError(f"Could not inspect UFW rules: {result.stderr.strip()}")
    matching = []
    for line in result.stdout.splitlines():
        match = re.match(r"^\[\s*(\d+)\]\s+.*#\s*(\S+)\s*$", line)
        if match and match.group(2) == marker:
            matching.append(int(match.group(1)))
    return sorted(matching, reverse=True)


def configure_host_firewall(extra=()):
    config = tofu_host_firewall_config(extra)
    if config["method"] == "disabled":
        print("Host firewall integration is disabled; UFW was not changed.")
        return
    if config["method"] != "ufw":
        raise ValueError(f"Unsupported host firewall method: {config['method']!r}")
    if shutil.which("ufw") is None:
        raise RuntimeError("host_firewall is set to 'ufw', but ufw is not installed on the host.")

    admin = host_admin_prefix()
    marker = f"nixvm-{config['name']}"
    # Remove older rules for this VM first, so subnet and service changes do
    # not leave obsolete persistent allows behind. The marker scopes removal
    # to rules created by this NixVM profile.
    for number in ufw_managed_rule_numbers(admin, marker):
        run(admin + ["ufw", "--force", "delete", str(number)])

    rules = [
        (config["gateway"], 53, "udp"),
        (config["gateway"], 53, "tcp"),
    ]
    if config["mitmproxy_enabled"]:
        rules.append((config["gateway"], 8080, "tcp"))
    if config["nfs_enabled"]:
        rules.append((config["nfs_host"], 2049, "tcp"))

    for destination, port, protocol in rules:
        run(admin + ["ufw", "allow", "in", "from", config["network_cidr"],
                     "to", destination, "port", str(port), "proto", protocol,
                     "comment", marker])
    print(f"Configured persistent UFW rules for {config['name']}: "
          + ", ".join(f"{port}/{protocol}" for _, port, protocol in rules))


def host_os_release():
    values = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        parts = shlex.split(value)
        values[key] = parts[0] if parts else ""
    return values


def host_nfs_distro(values):
    distro_ids = {values.get("ID", "").lower(), *values.get("ID_LIKE", "").lower().split()}
    if distro_ids & {"debian", "ubuntu", "linuxmint", "pop"}:
        return "apt-get", "nfs-kernel-server", "nfs-kernel-server"
    if distro_ids & {"fedora", "rhel", "centos", "rocky", "almalinux", "ol"}:
        return ("dnf" if shutil.which("dnf") else "yum"), "nfs-utils", "nfs-server"
    if distro_ids & {"arch", "archarm", "manjaro", "endeavouros"}:
        return "pacman", "nfs-utils", "nfs-server"
    if distro_ids & {"suse", "opensuse"} or any(value.startswith("opensuse") for value in distro_ids):
        return "zypper", "nfs-kernel-server", "nfsserver"
    raise RuntimeError(
        f"Unsupported host distribution {values.get('PRETTY_NAME', values.get('ID', 'unknown'))!r}. "
        "Supported families: Debian/Ubuntu, Fedora/RHEL, Arch, and openSUSE."
    )


def nfs_network_cidrs(uri, network_name):
    xml = output(["virsh", "--connect", uri, "net-dumpxml", network_name])
    root = ET.fromstring(xml)
    cidrs = set()
    for node in root.findall("./ip"):
        address = node.get("address")
        if not address or ipaddress.ip_address(address).version != 4:
            continue
        mask = node.get("netmask") or node.get("prefix")
        if not mask:
            continue
        cidrs.add(ipaddress.ip_network(f"{address}/{mask}", strict=False).with_prefixlen)
    if not cidrs:
        raise RuntimeError(f"Could not find an IPv4 subnet in libvirt network {network_name!r}")
    return sorted(cidrs, key=lambda value: (ipaddress.ip_network(value).version, value))


def configured_nfs_network_cidrs(config):
    # The dedicated network is created by the same OpenTofu apply that runs
    # host setup, so use its configured subnet before it exists in libvirt.
    network_cidr = config.get("network_cidr")
    if network_cidr:
        network = ipaddress.ip_network(network_cidr, strict=False)
        if network.version != 4:
            raise ValueError("The VM NFS export network must be IPv4.")
        return [network.with_prefixlen]
    return nfs_network_cidrs(config["uri"], config["network"])


def validate_host_nfs_path(value):
    if any(char.isspace() or char in "#(),\\\"" for char in value):
        raise ValueError(f"Host NFS path contains characters that cannot be represented safely: {value!r}")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink() or not path.is_dir():
        raise ValueError(f"Host NFS source must be an existing absolute directory, not a symlink: {value}")
    return str(path)


def host_nfs_exports_file(name):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
        raise ValueError(f"Invalid VM name for NFS exports: {name}")
    return Path("/etc/exports.d") / f"nixvm-{name}.exports"


def host_nfs_exports_text(config, cidrs=None):
    if cidrs is None:
        cidrs = configured_nfs_network_cidrs(config)
    exports = config["exports"]
    sources = sorted({validate_host_nfs_path(export["host_path"]) for export in exports})
    clients = " ".join(f"{cidr}(rw,sync,root_squash,no_subtree_check)" for cidr in cidrs)
    return "# Managed by nixvm; edit host_nfs_exports in OpenTofu instead.\n" + "".join(
        f"{source} {clients}\n" for source in sources
    )


def check_host_nfs(config=None):
    if config is None:
        try:
            config = json.loads(os.environ["NIXVM_HOST_NFS_CONFIG"])
        except KeyError as error:
            raise RuntimeError("Host NFS check needs the selected OpenTofu configuration.") from error
    if not config["exports"]:
        exports_file = host_nfs_exports_file(config["name"])
        if exports_file.exists():
            raise RuntimeError(
                f"A managed host NFS export still exists at {exports_file}; set host_nfs_exports = [] "
                "and rerun the selected variable file with 'nixvm nfs cleanup' before applying."
            )
        print("No host_nfs_exports are configured; no host export check is needed.")
        return
    cidrs = configured_nfs_network_cidrs(config)
    exports_file = host_nfs_exports_file(config["name"])
    expected = host_nfs_exports_text(config, cidrs)
    if not exports_file.is_file() or exports_file.read_text() != expected:
        raise RuntimeError(
            f"Host NFS exports are not configured for these variables. Run "
            "'nixvm nfs setup' with the selected variable file in a terminal first."
        )
    values = host_os_release()
    _, _, service = host_nfs_distro(values)
    if shutil.which("systemctl") is None:
        raise RuntimeError("Host NFS is configured, but systemd is unavailable to check the NFS service.")
    try:
        run(["systemctl", "is-active", "--quiet", service])
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Host NFS service {service} is not active; run setup-host-nfs before applying.") from error
    print(f"Host NFS exports verified for {', '.join(cidrs)}.")


def confirm_host_nfs_plan():
    if not sys.stdin.isatty():
        raise RuntimeError("Host NFS setup requires an interactive terminal for confirmation; run this command directly in a terminal.")
    answer = input("Continue and apply these host NFS changes? Type 'yes' to proceed: ").strip().lower()
    if answer != "yes":
        raise RuntimeError("Host NFS setup cancelled; no changes were made.")


def host_admin_prefix():
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo") is None:
        raise RuntimeError("Host setup needs root privileges; run as root or install sudo.")
    return ["sudo"]


def configure_host_nfs(extra):
    config = tofu_host_nfs_config(extra)
    values = host_os_release()
    exports_file = host_nfs_exports_file(config["name"])
    if not config["exports"] and not exports_file.exists():
        print("No host_nfs_exports are configured and no managed host export file exists.")
        return

    if config["exports"]:
        package_manager, package, service = host_nfs_distro(values)
        cidrs = configured_nfs_network_cidrs(config)
        contents = host_nfs_exports_text(config, cidrs)
        if shutil.which("systemctl") is None:
            raise RuntimeError("Automatic host NFS setup requires systemd on the host.")
        if (exports_file.is_file() and exports_file.read_text() == contents
                and shutil.which("exportfs")):
            try:
                run(["systemctl", "is-active", "--quiet", service])
            except subprocess.CalledProcessError:
                pass
            else:
                print(f"Host NFS exports are already configured in {exports_file}.")
                return
        admin = host_admin_prefix()
        package_missing = shutil.which("exportfs") is None
        print(f"Detected host distribution: {values.get('PRETTY_NAME', values.get('ID', 'unknown'))}")
        print("The script will:")
        if package_missing:
            if package_manager == "apt-get":
                commands = [["apt-get", "update"], ["apt-get", "install", "-y", package]]
            elif package_manager in ("dnf", "yum"):
                commands = [[package_manager, "install", "-y", package]]
            elif package_manager == "pacman":
                commands = [["pacman", "-S", "--needed", "--noconfirm", package]]
            else:
                commands = [["zypper", "--non-interactive", "install", package]]
            for command in commands:
                print(f"  - run {' '.join(admin + command)}")
        else:
            print(f"  - keep the installed NFS server package {package}")
        print(f"  - write {exports_file} for libvirt network {config['network']} ({', '.join(cidrs)})")
        for export in config["exports"]:
            print(f"  - export host path {export['host_path']} and mount it in the VM at {export['guest_path']}")
        print(f"  - enable/start {service} and reload NFS exports")
        print("  - configure TCP port 2049 in UFW only when host_firewall = \"ufw\"")
        confirm_host_nfs_plan()

        if package_missing:
            if package_manager == "apt-get":
                run(admin + ["apt-get", "update"])
                run(admin + ["apt-get", "install", "-y", package])
            elif package_manager == "dnf":
                run(admin + ["dnf", "install", "-y", package])
            elif package_manager == "yum":
                run(admin + ["yum", "install", "-y", package])
            elif package_manager == "pacman":
                run(admin + ["pacman", "-S", "--needed", "--noconfirm", package])
            else:
                run(admin + ["zypper", "--non-interactive", "install", package])
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="nixvm-exports-") as staged:
            staged.write(contents)
            staged.flush()
            run(admin + ["install", "-d", "-m", "0755", "/etc/exports.d"])
            run(admin + ["install", "-m", "0644", staged.name, str(exports_file)])
        run(admin + ["systemctl", "enable", "--now", service])
        run(admin + ["exportfs", "-ra"])
        print(f"Configured host NFS exports in {exports_file}.")
        return

    print(f"Detected host distribution: {values.get('PRETTY_NAME', values.get('ID', 'unknown'))}")
    print(f"No host_nfs_exports remain; the script will remove {exports_file} and reload NFS exports.")
    confirm_host_nfs_plan()
    admin = host_admin_prefix()
    run(admin + ["rm", "-f", str(exports_file)])
    if shutil.which("exportfs"):
        run(admin + ["exportfs", "-ra"])
    print("Removed the managed NFS export file. The NFS server package and service were left installed.")


def copy_checkout(c, source, destination, *, settings=None):
    quoted = shlex.quote(destination)
    exists = output(ssh_args(c) + [f"if test -e {quoted}/.git; then echo yes; fi"])
    if exists == "yes":
        print(f"Preserving existing guest checkout: {destination}")
        return
    # Never merge a fresh copy into an unknown/partially copied directory.
    remote(c, f"test ! -e {quoted} || {{ echo 'Destination exists without .git; inspect it before retrying' >&2; exit 1; }}")
    with tempfile.TemporaryDirectory(prefix="nixvm-stage-") as tmp:
        staged = Path(tmp) / "checkout"
        shutil.copytree(source, staged, symlinks=True)
        if settings is not None:
            (staged / "nix/machines/current.json").write_text(json.dumps(settings, indent=2) + "\n")
        target = destination + ".nixvm-staging"
        remote(c, f"mkdir -p {shlex.quote(str(Path(destination).parent))} {shlex.quote(target)}")
        # rsync argv is generated without involving an interpolated shell on the host.
        ssh = ssh_args(c)
        run(["rsync", "-a", "--delete", "--protect-args", "-e", shlex.join(ssh[:-1]),
             str(staged) + "/", f"{ssh[-1]}:{target}/"])
        remote(c, f"mv -T {shlex.quote(target)} {quoted}")


def bootstrap():
    c = connection()
    # Bootstrap only runs on initial provisioning or an explicit reprovision.
    # A recreated NixOS disk can generate a new host key at the same DHCP IP.
    wait_guest_ip(c)
    reset_guest_host_key(c)
    wait_ssh(c)
    config = c["guest_config"]
    copy_checkout(c, os.environ["NIXVM_DOTFILES"], config["dotfilesPath"])
    copy_checkout(c, os.environ["NIXVM_REPOSITORY"], config["repoPath"], settings=config)
    sync_guest_mitmproxy_ca(c)
    # Refresh the host cache for the exact settings being provisioned, including
    # explicit reprovisions where the VM config may have changed since the
    # bootstrap image was originally built.
    build_workstation_cache(Path(os.environ["NIXVM_BUILD"]), config)
    copy_workstation_closure(c)
    flake = "path:" + config["repoPath"] + "/nix#nixvm"
    guest_ca = config["repoPath"].rstrip("/") + "/nix/machines/mitmproxy-ca.pem"
    local_ca = ROOT / ".runtime" / "shared" / "mitmproxy-ca-cert.pem"
    bundle = f"/tmp/nixvm-mitmproxy-ca-{os.getpid()}.pem" if local_ca.is_file() else None
    run_nixos_rebuild(c, flake, config,
                      ca_bundle=bundle if bundle else None,
                      ca_certificate=guest_ca if bundle else None)
    reboot_guest(c)
    print("Provisioned and rebooted into the graphical system. SSH and GNOME are ready.")


def copy_workstation_closure(c):
    """Import the host-built workstation closure into the guest Nix store."""
    result = Path(os.environ["NIXVM_BUILD"]) / "workstation-result"
    toplevel = str(result.resolve(strict=True))
    paths = output(["nix-store", "--query", "--requisites", toplevel]).splitlines()
    if not paths:
        raise RuntimeError(f"No store paths found in workstation closure {toplevel}")
    print(f"Copying {len(paths)} host-cached workstation store paths to the guest.")

    exporter = subprocess.Popen(["nix-store", "--export", *paths], stdout=subprocess.PIPE)
    assert exporter.stdout is not None
    try:
        importer = subprocess.run(
            ssh_args(c) + ["sudo nix-store --import"],
            stdin=exporter.stdout,
            check=False,
        )
    finally:
        exporter.stdout.close()
    export_status = exporter.wait()
    if importer.returncode:
        raise subprocess.CalledProcessError(importer.returncode, importer.args)
    if export_status:
        raise subprocess.CalledProcessError(export_status, exporter.args)


def run_nixos_rebuild(c, flake, settings, *, ca_bundle=None, ca_certificate=None):
    """Build with the managed proxy even when the active system predates it."""
    rebuild = "sudo nixos-rebuild switch --flake " + shlex.quote(flake)
    proxy = settings.get("mitmproxy", {})
    if not (proxy.get("enabled") and proxy.get("proxyUrl")):
        return remote(c, rebuild)

    proxy_url = proxy["proxyUrl"]
    ca_file = ca_bundle or "/etc/ssl/certs/ca-certificates.crt"
    runtime_environment = {
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "all_proxy": proxy_url,
        "no_proxy": "localhost,127.0.0.1,::1",
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "SSL_CERT_FILE": ca_file,
        "REQUESTS_CA_BUNDLE": ca_file,
        "CURL_CA_BUNDLE": ca_file,
        "NODE_EXTRA_CA_CERTS": ca_file,
        "NIX_SSL_CERT_FILE": ca_file,
        "GIT_SSL_CAINFO": ca_file,
        "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH": ca_file,
    }
    set_environment = "sudo systemctl set-environment " + shlex.join(
        [f"{key}={value}" for key, value in runtime_environment.items()]
    )
    unset_environment = "sudo systemctl unset-environment " + shlex.join(
        list(runtime_environment)
    )
    if ca_bundle:
        unset_environment += "; sudo rm -f -- " + shlex.quote(ca_bundle)
    rebuild = "sudo env NIX_REMOTE=local " + shlex.join(
        [f"{key}={value}" for key, value in runtime_environment.items()]
    ) + " nixos-rebuild switch --flake " + shlex.quote(flake)
    command = ["set -e"]
    if ca_bundle and ca_certificate:
        command.append(
            "sudo sh -c " + shlex.quote(
                "cat /etc/ssl/certs/ca-certificates.crt "
                + shlex.quote(ca_certificate)
                + " > " + shlex.quote(ca_bundle)
                + " && chmod 0644 " + shlex.quote(ca_bundle)
            )
        )
    command.extend([
        set_environment,
        f"trap {shlex.quote(unset_environment)} EXIT",
        "sudo systemctl restart nix-daemon.service",
        rebuild,
    ])
    return remote(c, "sh -c " + shlex.quote("\n".join(command)))


def hosts_text_with_vm_name(contents, address, hostname):
    """Add or refresh the managed VM hostname without changing user entries."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", hostname):
        raise ValueError(f"Invalid VM hostname for {HOSTS_FILE}: {hostname!r}")
    address = str(ipaddress.ip_address(address))
    lines = contents.splitlines()
    matching_lines = []
    for index, line in enumerate(lines):
        body, _, comment = line.partition("#")
        fields = body.split()
        if len(fields) < 2 or hostname not in fields[1:]:
            continue
        try:
            existing_address = str(ipaddress.ip_address(fields[0]))
        except ValueError:
            continue
        matching_lines.append((index, fields, comment, existing_address))

    entry = f"{address} {hostname} {HOSTS_MARKER}"
    if any(existing_address == address for _, _, _, existing_address in matching_lines):
        return contents, False

    for index, fields, comment, _ in reversed(matching_lines):
        remaining_aliases = [alias for alias in fields[1:] if alias != hostname]
        if remaining_aliases:
            replacement = " ".join([fields[0], *remaining_aliases])
            if comment:
                replacement += f" #{comment.lstrip()}"
            lines[index] = replacement
        else:
            lines[index] = None
    lines = [line for line in lines if line is not None]
    lines.append(entry)
    return "\n".join(lines) + ("\n" if contents.endswith("\n") or contents == "" else ""), True


def vm_hosts_entry_exists(contents, hostname):
    """Return whether /etc/hosts already maps any address to hostname."""
    for line in contents.splitlines():
        body = line.partition("#")[0]
        fields = body.split()
        if hostname in fields[1:]:
            return True
    return False


def remove_vm_hosts_entry(contents, hostname):
    """Remove only nixvm-managed host mappings for the selected hostname."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", hostname):
        raise ValueError(f"Invalid VM hostname for {HOSTS_FILE}: {hostname!r}")
    lines = contents.splitlines()
    changed = False
    for index, line in enumerate(lines):
        body, separator, comment = line.partition("#")
        fields = body.split()
        if (len(fields) < 2 or hostname not in fields[1:]
                or not separator or not comment.strip().startswith("Managed by nixvm")):
            continue
        aliases = [alias for alias in fields[1:] if alias != hostname]
        lines[index] = " ".join([fields[0], *aliases]) if aliases else None
        changed = True
    lines = [line for line in lines if line is not None]
    if not changed:
        return contents, False
    return "\n".join(lines) + ("\n" if contents.endswith("\n") or contents == "" else ""), True


def confirm_vm_hosts_change(hostname):
    if not sys.stdin.isatty():
        raise RuntimeError("Adding the VM hostname requires an interactive terminal for confirmation; run nixvm up directly in a terminal.")
    print(f"Add {hostname} to /etc/hosts (required for Sunshine)")
    answer = input(f"Add or update the managed {hostname} entry in /etc/hosts? Type 'yes' to proceed: ").strip().lower()
    if answer != "yes":
        raise RuntimeError("Hostname setup cancelled; /etc/hosts was not changed.")


def setup_host_hostname():
    c = connection()
    hostname = c["name"]
    address = wait_guest_ip(c)
    if HOSTS_FILE.is_symlink():
        raise RuntimeError(f"{HOSTS_FILE} is a symlink managed by the host OS; add the {hostname} mapping through that system's configuration.")
    contents = HOSTS_FILE.read_text(encoding="utf-8")
    updated, changed = hosts_text_with_vm_name(contents, address, hostname)
    if not changed:
        print(f"The host already resolves {hostname} to {address}; no hosts file change is needed.")
        return
    entry_exists = vm_hosts_entry_exists(contents, hostname)
    if not entry_exists:
        confirm_vm_hosts_change(hostname)
    mode = HOSTS_FILE.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="nixvm-hosts-", delete=False) as staged:
        staged.write(updated)
        staged_path = Path(staged.name)
    try:
        run(host_admin_prefix() + ["install", "-m", format(mode, "04o"), str(staged_path), str(HOSTS_FILE)])
    finally:
        staged_path.unlink(missing_ok=True)
    action = "Updated" if entry_exists else "Added"
    print(f"{action} {hostname} -> {address} in {HOSTS_FILE}.")


def cleanup_host_hostname(hostname):
    if HOSTS_FILE.is_symlink():
        raise RuntimeError(f"{HOSTS_FILE} is a symlink managed by the host OS; remove the {hostname} mapping through that system's configuration.")
    contents = HOSTS_FILE.read_text(encoding="utf-8")
    updated, changed = remove_vm_hosts_entry(contents, hostname)
    if not changed:
        print(f"No nixvm-managed {hostname} entry in {HOSTS_FILE}.")
        return
    mode = HOSTS_FILE.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="nixvm-hosts-", delete=False) as staged:
        staged.write(updated)
        staged_path = Path(staged.name)
    try:
        run(host_admin_prefix() + ["install", "-m", format(mode, "04o"), str(staged_path), str(HOSTS_FILE)])
    finally:
        staged_path.unlink(missing_ok=True)
    print(f"Removed managed {hostname} entry from {HOSTS_FILE}.")


def update_config(extra=()):
    """Build and activate the host checkout's NixOS config in the existing guest."""
    c = connection()
    # Connection outputs can lag current tfvars before apply or on an existing
    # VM. Evaluate the machine settings from the selected OpenTofu inputs.
    settings = tofu_guest_config(extra)
    c["guest_config"] = settings
    sync_guest_mitmproxy_ca(c)
    remote_stage = f"/tmp/nixvm-config-{os.getpid()}-{time.time_ns()}"
    with tempfile.TemporaryDirectory(prefix="nixvm-config-") as tmp:
        staged_nix = Path(tmp) / "nix"
        shutil.copytree(ROOT / "nix", staged_nix,
                        ignore=shutil.ignore_patterns("current.json", "result*"))
        ca_certificate = ROOT / ".runtime" / "shared" / "mitmproxy-ca-cert.pem"
        if ca_certificate.is_file():
            shutil.copyfile(ca_certificate, staged_nix / "machines" / "mitmproxy-ca.pem")
        (staged_nix / "machines/current.json").write_text(
            json.dumps(settings, indent=2) + "\n"
        )

        remote(c, f"mkdir -m 700 {shlex.quote(remote_stage)}")
        try:
            ssh = ssh_args(c)
            run(["rsync", "-a", "--delete", "--protect-args", "-e", shlex.join(ssh[:-1]),
                 str(staged_nix) + "/", f"{ssh[-1]}:{remote_stage}/nix/"])
            flake = f"path:{remote_stage}/nix#nixvm"
            guest_ca = f"{remote_stage}/nix/machines/mitmproxy-ca.pem"
            bundle = f"/tmp/nixvm-mitmproxy-ca-{os.getpid()}.pem" if ca_certificate.is_file() else None
            run_nixos_rebuild(c, flake, settings,
                              ca_bundle=bundle,
                              ca_certificate=guest_ca if bundle else None)
        finally:
            remote(c, f"rm -rf -- {shlex.quote(remote_stage)}")
    print("Activated the host NixOS configuration in the guest.")


def sync_guest_mitmproxy_ca(c):
    """Keep the managed proxy CA in the guest checkout for ordinary rebuilds."""
    certificate = ROOT / ".runtime" / "shared" / "mitmproxy-ca-cert.pem"
    if not certificate.is_file():
        return
    target = c["guest_config"]["repoPath"].rstrip("/") + "/nix/machines/mitmproxy-ca.pem"
    remote(c, f"mkdir -p {shlex.quote(str(Path(target).parent))}")
    ssh = ssh_args(c)
    run(["rsync", "-a", "--protect-args", "-e", shlex.join(ssh[:-1]),
         str(certificate), f"{ssh[-1]}:{target}"])


def preflight(c):
    failures = []
    for name in ["nix", "tofu", "virsh", "git", "ssh", "rsync", "qemu-img"]:
        if not shutil.which(name):
            failures.append(f"Missing host command: {name}")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        failures.append("/dev/kvm is missing or inaccessible")
    try:
        virsh(c, "list", "--all")
    except subprocess.CalledProcessError:
        failures.append("Cannot access libvirt")
    failures.extend(gpu_preflight_failures(c))
    if failures:
        raise RuntimeError("Preflight failed:\n  " + "\n  ".join(dict.fromkeys(failures)))
    ensure_vfio_devices(c)
    print("Host preflight passed. GPU reset/NVENC/display still require guest verification.")


def gpu_preflight_failures(c):
    failures = []
    gpu = c.get("guest_config", {}).get("gpu", {})
    selected = set(gpu.get("pci_functions", []))
    if gpu.get("enabled"):
        for pci in sorted(selected):
            dev = Path("/sys/bus/pci/devices") / pci
            if not dev.exists():
                failures.append(f"PCI function not found: {pci}")
                continue
            group = dev / "iommu_group"
            if not group.exists():
                failures.append(f"No IOMMU group: {pci}")
                continue
            peers = sorted(p.name for p in (group / "devices").iterdir())
            driver = (dev / "driver").resolve().name if (dev / "driver").exists() else "none"
            print(f"{pci}: group {group.resolve().name}, driver={driver}, members={', '.join(peers)}")
            for peer in peers:
                peerdev = dev.parent / peer
                klass = (peerdev / "class").read_text().strip()
                if peer not in selected and not klass.startswith("0x0604"):
                    failures.append(f"Unassigned non-bridge IOMMU peer: {peer}")
            if (dev / "boot_vga").exists() and (dev / "boot_vga").read_text().strip() == "1":
                failures.append(f"{pci} is the host boot VGA device")
            for companion in dev.parent.glob(pci.rsplit(".", 1)[0] + ".*"):
                if companion.name not in selected:
                    failures.append(f"Include GPU companion PCI function: {companion.name}")
    return failures


def validate_gpu_for_start(c):
    failures = gpu_preflight_failures(c)
    if failures:
        raise RuntimeError("GPU preflight failed:\n  " + "\n  ".join(dict.fromkeys(failures)))


def ensure_vfio_devices(c):
    """Bind selected GPU functions to vfio-pci before starting or provisioning the VM."""
    gpu = c.get("guest_config", {}).get("gpu", {})
    if not gpu.get("enabled"):
        return
    for pci in sorted(gpu.get("pci_functions", [])):
        device = Path("/sys/bus/pci/devices") / pci
        if not device.exists():
            raise RuntimeError(f"Cannot bind missing PCI function to vfio-pci: {pci}")
        driver_link = device / "driver"
        driver = driver_link.resolve().name if driver_link.exists() else "none"
        # Libvirt can already have bound a managed passthrough device. Its
        # active kernel override is sufficient for starting this VM.
        kernel_override = device / "driver_override"
        if (driver == "vfio-pci" and kernel_override.is_file()
                and kernel_override.read_text().strip() == "vfio-pci"):
            continue
        driverctl = shutil.which("driverctl")
        override = None
        if driverctl:
            try:
                overrides = output([driverctl, "list-overrides"])
            except subprocess.CalledProcessError:
                # Libvirt's managed hostdev can bind an isolated GPU without
                # requiring a separate root driverctl session.
                node = "pci_" + pci.replace(":", "_").replace(".", "_")
                virsh(c, "nodedev-detach", node)
                if not driver_link.exists() or driver_link.resolve().name != "vfio-pci":
                    raise RuntimeError(f"Libvirt did not bind {pci} to vfio-pci.")
                continue
            for line in overrides.splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[0] == pci:
                    override = fields[1]
                    break
        if driver == "vfio-pci" and (not driverctl or override == "vfio-pci"):
            continue
        if not driverctl:
            raise RuntimeError(f"{pci} needs vfio-pci, but driverctl is not installed.")
        print(
            f"{pci} needs a persistent vfio-pci binding (current driver: {driver}); "
            f"running driverctl set-override {pci} vfio-pci.",
            flush=True,
        )
        run(host_admin_prefix() + [driverctl, "set-override", pci, "vfio-pci"])
        driver = driver_link.resolve().name if driver_link.exists() else "none"
        if driver != "vfio-pci":
            raise RuntimeError(f"driverctl did not bind {pci} to vfio-pci (current driver: {driver}).")


def forward(c):
    socket = output(["gpgconf", "--list-dirs", "agent-extra-socket"])
    run(["gpgconf", "--launch", "gpg-agent"])
    pcsc = Path(os.environ.get("NIXVM_HOST_PCSC", "/run/pcscd/pcscd.comm"))
    if not pcsc.is_socket():
        raise RuntimeError("Start the host pcscd socket/service before forwarding")
    remote(c, "nixvm-token-mode shared")
    uid = c["uid"]
    guest_gpg = output(ssh_args(c) + ["gpgconf --list-dirs agent-socket"])
    remote(c, f"mkdir -p {shlex.quote(str(Path(guest_gpg).parent))}")
    pcsc_target = f"/run/user/{uid}/nixvm/pcsc.sock"
    print("Forwarding GPG and PC/SC; PIN dialogs may appear on the host. Ctrl-C stops forwarding.")
    # A separate stable SSH control socket allows USB attach/detach to stop and
    # restart this exact foreground tunnel without modifying SSH configuration.
    control = ROOT / ".runtime" / c["name"] / "forward-control"
    run(ssh_args(c)[:-1] + ["-M", "-S", str(control), "-N", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-R", f"{guest_gpg}:{socket}", "-R", f"{pcsc_target}:{pcsc}", ssh_args(c)[-1]])


def usb_device(selector):
    found = []
    for dev in Path("/sys/bus/usb/devices").iterdir():
        if not (dev / "idVendor").exists() or (dev / "idVendor").read_text().strip() != "1050":
            continue
        serial = (dev / "serial").read_text().strip() if (dev / "serial").exists() else ""
        if selector and selector not in [dev.name, serial]:
            continue
        found.append((dev, serial))
    if len(found) != 1:
        raise RuntimeError(f"Expected exactly one matching YubiKey, found {len(found)}; use --device USB-port-or-serial")
    dev, _ = found[0]
    root = ET.Element("hostdev", mode="subsystem", type="usb", managed="yes")
    source = ET.SubElement(root, "source")
    ET.SubElement(source, "address", bus=str(int((dev / "busnum").read_text())),
                  device=str(int((dev / "devnum").read_text())))
    return ET.tostring(root, encoding="unicode")


def usb(c, attach, selector=None):
    saved = ROOT / ".runtime" / c["name"] / "yubikey.xml"
    saved.parent.mkdir(parents=True, exist_ok=True)
    if attach:
        if saved.exists():
            raise RuntimeError("A key is already recorded as attached; detach it first")
        xml = usb_device(selector)
        control = saved.parent / "forward-control"
        if control.exists():
            run(ssh_args(c)[:-1] + ["-S", str(control), "-O", "exit", ssh_args(c)[-1]])
        remote(c, "nixvm-token-mode usb")
        saved.write_text(xml)
        try:
            virsh(c, "attach-device", c["name"], str(saved), "--live")
        except Exception:
            saved.unlink(missing_ok=True)
            remote(c, "nixvm-token-mode shared")
            raise
        print("USB attached. Run nixvm-gpg for local GPG, and reopen Firefox for PIV/FIDO2.")
    else:
        if not saved.exists():
            raise RuntimeError("No saved YubiKey attachment; inspect virsh dumpxml if it was attached manually")
        # Return USB first: the host may need this very key to authenticate SSH.
        # A retained record also permits retrying the guest mode reset after a
        # reboot or after Firefox initially prevented the reset.
        wanted = ET.fromstring(saved.read_text()).find("source/address")
        domain = ET.fromstring(virsh(c, "dumpxml", c["name"]))
        attached = any(
            int(address.get("bus", "0")) == int(wanted.get("bus")) and
            int(address.get("device", "0")) == int(wanted.get("device"))
            for address in domain.findall("./devices/hostdev[@type='usb']/source/address")
        )
        if attached:
            virsh(c, "detach-device", c["name"], str(saved), "--live")
        remote(c, "nixvm-token-mode shared")
        saved.unlink()
        print("Key returned to host. Run scripts/nixvm.py forward again to restore shared access.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build-image", "remove-image", "define-network-filter", "firewall-test", "check-host-nfs", "setup-host-nfs", "setup-host-firewall", "setup-host-mitmproxy", "cleanup-host-mitmproxy", "setup-hostname", "cleanup-hostname", "bootstrap", "preflight", "ssh", "spice", "forward", "ensure-vm-nat",
                        "usb-attach", "usb-detach", "rebuild", "rollback", "password", "reboot", "start", "stop",
                        "reprovision", "import-public-keys", "gpu-check", "token-check", "update-config", "ip"])
    parser.add_argument("--name", default=os.environ.get("NIXVM_PROFILE", "nixvm"),
                        help="VM name for remove-image (defaults to nixvm)")
    parser.add_argument("--device", help="YubiKey USB port name or serial; mandatory if selection is ambiguous")
    parser.add_argument("--timeout", type=int, default=120, help="Seconds to wait for start/stop transitions (default: 120)")
    parser.add_argument("--force", action="store_true", help="Abruptly destroy the VM instead of graceful shutdown (stop only)")
    parser.add_argument("--wait-ssh", action="store_true", help="Wait until guest SSH accepts connections after start")
    args, extra = parser.parse_known_args()
    if args.timeout < 1:
        parser.error("--timeout must be at least one second")
    if args.force and args.command != "stop":
        parser.error("--force is only valid with stop")
    if args.wait_ssh and args.command != "start":
        parser.error("--wait-ssh is only valid with start")
    if args.command == "build-image":
        return build_image()
    if args.command == "remove-image":
        return remove_image_cache(args.name)
    if args.command == "define-network-filter":
        return define_network_filter(extra)
    if args.command == "firewall-test":
        return run_firewall_tests(extra)
    if args.command == "setup-host-nfs":
        return configure_host_nfs(extra)
    if args.command == "setup-host-firewall":
        return configure_host_firewall(extra)
    if args.command == "setup-host-mitmproxy":
        return setup_host_mitmproxy(extra)
    if args.command == "ensure-vm-nat":
        return ensure_vm_nat(extra)
    if args.command == "cleanup-host-mitmproxy":
        return cleanup_host_mitmproxy(args.name)
    if args.command == "setup-hostname":
        return setup_host_hostname()
    if args.command == "cleanup-hostname":
        return cleanup_host_hostname(args.name)
    if args.command == "check-host-nfs":
        config = (json.loads(os.environ["NIXVM_HOST_NFS_CONFIG"])
                  if "NIXVM_HOST_NFS_CONFIG" in os.environ
                  else tofu_host_nfs_config(extra))
        return check_host_nfs(config)
    if args.command == "bootstrap":
        return bootstrap()
    if args.command == "preflight" and extra:
        # Evaluate variables without creating resources or requiring prior state.
        value = run(["tofu", f"-chdir={ROOT / 'infrastructure'}", "console", *extra],
                    capture=True, input="jsonencode(local.connection)\n").stdout.strip()
        c = json.loads(json.loads(value))
    else:
        c = connection()
    if args.command == "preflight":
        preflight(c)
    elif args.command == "ip":
        print(guest_ip(c))
    elif args.command == "ssh":
        run(ssh_args(c, tty=not extra) + extra)
    elif args.command == "spice":
        spice(c, extra)
    elif args.command == "forward":
        forward(c)
    elif args.command in ["usb-attach", "usb-detach"]:
        usb(c, args.command == "usb-attach", args.device)
    elif args.command == "password":
        run(ssh_args(c, tty=True) + ["sudo passwd " + shlex.quote(c["username"])])
    elif args.command == "rebuild":
        remote(c, "nixvm-rebuild switch")
    elif args.command == "rollback":
        remote(c, "sudo nixos-rebuild switch --rollback")
    elif args.command == "reboot":
        remote(c, "sudo systemctl reboot")
    elif args.command == "start":
        start_vm(c, timeout=args.timeout, wait_for_ssh=args.wait_ssh)
    elif args.command == "stop":
        stop_vm(c, timeout=args.timeout, force=args.force)
    elif args.command == "reprovision":
        run(["tofu", f"-chdir={ROOT / 'infrastructure'}", "apply", "-auto-approve",
             "-replace=terraform_data.bootstrap", *extra])
    elif args.command == "import-public-keys":
        public = subprocess.check_output(["gpg", "--export"])
        run(ssh_args(c) + ["gpg --no-autostart --import"], input=public.decode("latin1"), encoding="latin1")
    elif args.command == "gpu-check":
        remote(c, "nixvm-gpu-check")
    elif args.command == "update-config":
        update_config(extra)
    elif args.command == "token-check":
        print("Checking PIV through the selected PC/SC socket; protocol mismatch errors require compatible pcsc-lite versions.")
        remote(c, "nixvm-piv --list-slots")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
    except KeyboardInterrupt:
        sys.exit(130)
