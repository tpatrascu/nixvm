#!/usr/bin/env python3
"""Internal OpenTofu callbacks and helpers for the nixvm wrapper."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
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
        virsh(c, "resume", c["name"])
        wait_domain_state(c, "running", timeout)
    elif state in {"shut off", "crashed"}:
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
    image = build / "bootstrap.qcow2"
    if image.is_file():
        print(f"Using cached bootstrap image: {image}")
        run(["qemu-img", "resize", image, os.environ["NIXVM_DISK_GIB"] + "G"])
        return
    snapshot = build / "nix-source"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(ROOT / "nix", snapshot, ignore=shutil.ignore_patterns("current.json", "result*"))
    (snapshot / "machines/current.json").write_text(json.dumps(settings, indent=2) + "\n")
    run(["nix", "--extra-experimental-features", "nix-command flakes",
         "build", f"path:{snapshot}#bootstrap", "--out-link", build / "image-result"])
    images = list((build / "image-result").rglob("*.qcow2"))
    if len(images) != 1:
        raise RuntimeError(f"Expected one QCOW2 image, found {len(images)}")
    shutil.copyfile(images[0], image)
    # An upload overwrites the QCOW2 header, including its virtual capacity.
    run(["qemu-img", "resize", image, os.environ["NIXVM_DISK_GIB"] + "G"])


def remove_image_cache(name):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
        raise ValueError("VM name must match [a-z][a-z0-9-]{0,30}")
    build = ROOT / ".build" / name
    if not build.exists():
        print(f"No image cache found for {name}.")
        return
    shutil.rmtree(build)
    print(f"Removed image cache for {name}: {build}")


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
            for transport in ("tcp", "udp"):
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
    if not re.fullmatch(r"nixvm-[a-z][a-z0-9-]{0,30}-access", name):
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
    add_network_access_rules(root, access["internal"])

    host_ips = {ipaddress.ip_address(address).compressed for address in access["host"]["ips"]}
    internal_allow = access["internal"]["allow"]
    internal_ips = {ipaddress.ip_address(item["ip"]).compressed for item in internal_allow}
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


def define_network_filter():
    name = os.environ["NIXVM_FILTER_NAME"]
    access = json.loads(os.environ["NIXVM_NETWORK_ACCESS"])
    uri = json.loads(os.environ["NIXVM_CONNECTION"])["uri"]
    filter_uuid = existing_network_filter_uuid(uri, name)
    root = build_network_filter(name, access, filter_uuid)

    with tempfile.NamedTemporaryFile(mode="wb", prefix="nixvm-nwfilter-", suffix=".xml") as xml:
        ET.ElementTree(root).write(xml, encoding="utf-8", xml_declaration=True)
        xml.flush()
        run(["virsh", "--connect", uri, "nwfilter-define", xml.name])
    action = "Updated" if filter_uuid else "Defined"
    print(f"{action} host network filter: {name}")


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
        "primary_mac = var.mac_address, "
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


def run_firewall_tests(extra):
    config = tofu_firewall_test_config(extra)
    print("Waiting for guest SSH before running live firewall probes.")
    wait_ssh(config["connection"], timeout=300)
    env = os.environ.copy()
    env["NIXVM_FIREWALL_TEST_CONFIG"] = json.dumps(config)
    run([sys.executable, "-m", "unittest", "discover", "-s", ROOT / "tests",
         "-p", "test_firewall.py", "-v"], env=env)


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


def check_host_nfs():
    config = json.loads(os.environ["NIXVM_HOST_NFS_CONFIG"])
    if not config["exports"]:
        exports_file = host_nfs_exports_file(config["name"])
        if exports_file.exists():
            raise RuntimeError(
                f"A managed host NFS export still exists at {exports_file}; set host_nfs_exports = [] "
                "and run 'scripts/nixvm.py setup-host-nfs -var-file=devvm.tfvars' to remove it before applying."
            )
        print("No host_nfs_exports are configured; no host export check is needed.")
        return
    cidrs = configured_nfs_network_cidrs(config)
    exports_file = host_nfs_exports_file(config["name"])
    expected = host_nfs_exports_text(config, cidrs)
    if not exports_file.is_file() or exports_file.read_text() != expected:
        raise RuntimeError(
            f"Host NFS exports are not configured for these variables. Run "
            f"'scripts/nixvm.py setup-host-nfs -var-file=devvm.tfvars' in a terminal first."
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
        raise RuntimeError("Host NFS setup needs root privileges; run as root or install sudo.")
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
        print("  - leave host firewall settings unchanged; allow TCP port 2049 from the listed libvirt subnet if firewalled")
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
    flake = "path:" + config["repoPath"] + "/nix#devvm"
    remote(c, f"sudo nixos-rebuild switch --flake {shlex.quote(flake)}")
    reboot_guest(c)
    print("Provisioned and rebooted into the graphical system. SSH and GNOME are ready.")


def update_config():
    """Build and activate the host checkout's NixOS config in the existing guest."""
    c = connection()
    settings = c["guest_config"]
    remote_stage = f"/tmp/nixvm-config-{os.getpid()}-{time.time_ns()}"
    with tempfile.TemporaryDirectory(prefix="nixvm-config-") as tmp:
        staged_nix = Path(tmp) / "nix"
        shutil.copytree(ROOT / "nix", staged_nix,
                        ignore=shutil.ignore_patterns("current.json", "result*"))
        (staged_nix / "machines/current.json").write_text(
            json.dumps(settings, indent=2) + "\n"
        )

        remote(c, f"mkdir -m 700 {shlex.quote(remote_stage)}")
        try:
            ssh = ssh_args(c)
            run(["rsync", "-a", "--delete", "--protect-args", "-e", shlex.join(ssh[:-1]),
                 str(staged_nix) + "/", f"{ssh[-1]}:{remote_stage}/nix/"])
            flake = f"path:{remote_stage}/nix#devvm"
            remote(c, f"sudo nixos-rebuild switch --flake {shlex.quote(flake)}")
        finally:
            remote(c, f"rm -rf -- {shlex.quote(remote_stage)}")
    print("Activated the host NixOS configuration in the guest.")


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
    selected = set(c["guest_config"]["gpu"]["pci_functions"])
    if c["guest_config"]["gpu"]["enabled"]:
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
            if driver != "vfio-pci":
                failures.append(f"{pci} must already be bound to vfio-pci; preflight never detaches devices")
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
    if failures:
        raise RuntimeError("Preflight failed:\n  " + "\n  ".join(dict.fromkeys(failures)))
    print("Host preflight passed. GPU reset/NVENC/display still require guest verification.")


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
    parser.add_argument("command", choices=["build-image", "remove-image", "define-network-filter", "firewall-test", "check-host-nfs", "setup-host-nfs", "bootstrap", "preflight", "ssh", "forward",
                        "usb-attach", "usb-detach", "rebuild", "rollback", "password", "reboot", "start", "stop",
                        "reprovision", "import-public-keys", "gpu-check", "token-check", "update-config", "ip"])
    parser.add_argument("--name", default="devvm", help="VM name for remove-image (default: devvm)")
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
        return define_network_filter()
    if args.command == "firewall-test":
        return run_firewall_tests(extra)
    if args.command == "setup-host-nfs":
        return configure_host_nfs(extra)
    if args.command == "check-host-nfs":
        return check_host_nfs()
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
        update_config()
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
