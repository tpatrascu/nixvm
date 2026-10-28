#!/usr/bin/env python3
"""Host-side provisioning and explicit lifecycle commands. No private keys are copied."""
from __future__ import annotations

import argparse
import fnmatch
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
EXCLUDED_PARTS = {".git", ".terraform", ".aws", ".gnupg", ".build", ".runtime",
                  "node_modules", ".venv", "venv", "__pycache__", ".cache", "target"}
EXCLUDED_NAMES = ["*.tfstate*", "*.tfvars", "*.tfvars.json", "*.tfplan", ".env", ".env.*", "*.pem",
                  "*.key", "id_rsa*", "id_ed25519*", ".zcompdump*", "result", "result-*"]


def run(args, *, capture=False, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, **kwargs)


def output(args, **kwargs):
    return run(args, capture=True, **kwargs).stdout.strip()


def excluded(path):
    p = Path(path)
    return (any(part in EXCLUDED_PARTS for part in p.parts) or
            (".ssh" in p.parts and p.name != "config") or
            any(fnmatch.fnmatch(p.name, pat) for pat in EXCLUDED_NAMES))


def git_check(path):
    path = Path(path).expanduser().resolve()
    if output(["git", "-C", path, "rev-parse", "--show-toplevel"]) != str(path):
        raise ValueError(f"Repository path must be its working-tree root: {path}")
    run(["git", "-C", path, "rev-parse", "--verify", "HEAD"], capture=True)
    return path


def portable_zsh(text):
    """Adapt the known Ubuntu dotfiles without modifying the host checkout."""
    text = text.replace('export ZSH="$HOME/.oh-my-zsh"',
                        'export ZSH="${ZSH:-$HOME/.oh-my-zsh}"')
    text = re.sub(r'^source ["\']?\$ZSH/oh-my-zsh\.sh["\']?\s*$',
                  'if [[ -z ${NIXVM_ZSH_MANAGED:-} ]]; then source "$ZSH/oh-my-zsh.sh"; fi',
                  text, flags=re.M)
    text = text.replace('PKCS11_MODULE="/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so"',
                        'PKCS11_MODULE="${PKCS11_MODULE:-/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so}"')
    text = re.sub(r'^eval "\$\(/home/linuxbrew/\.linuxbrew/bin/brew shellenv zsh\)"$',
                        'if [[ -z ${NIXVM_ZSH_MANAGED:-} && -x /home/linuxbrew/.linuxbrew/bin/brew ]]; then\n'
                        '  eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv zsh)"\nfi', text, flags=re.M)
    return text


def stage_repository(source, destination, *, dotfiles=False):
    """Clone history (including worktrees), overlay edits, retain real remotes."""
    source = git_check(source)
    destination = Path(destination)
    remotes = {}
    for remote_name in output(["git", "-C", source, "remote"]).splitlines():
        fetch_url = output(["git", "-C", source, "remote", "get-url", remote_name])
        push_url = output(["git", "-C", source, "remote", "get-url", "--push", remote_name])
        if any(re.match(r"^https?://[^/]*@", url) for url in (fetch_url, push_url)):
            raise ValueError("Remove embedded HTTPS credentials from Git remotes before copying repositories")
        remotes[remote_name] = (fetch_url, push_url)
    refs = output(["git", "-C", source, "for-each-ref", "--format=%(objectname) %(refname)",
                   "refs/heads", "refs/remotes"])
    # Never let copied repository symlinks write outside the staging tree.
    files = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    ).decode().split("\0")
    run(["git", "clone", "--quiet", "--no-hardlinks", "--dissociate", source, destination])
    # Remove credentials/caches even if tracked; their presence in history is
    # still the owner's responsibility and is documented.
    for path in sorted(destination.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = path.relative_to(destination)
        if rel.parts[0] == ".git":
            continue
        if excluded(rel):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
    for name in files:
        if not name or excluded(name):
            continue
        src, dst = source / name, destination / name
        # Files beneath directory symlinks are not emitted by git ls-files.
        if dst.is_symlink():
            dst.unlink()
        if not src.exists() and not src.is_symlink():
            if dst.exists():
                dst.unlink()
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            dst.symlink_to(os.readlink(src))
        else:
            shutil.copy2(src, dst)
    for remote in output(["git", "-C", destination, "remote"]).splitlines():
        run(["git", "-C", destination, "remote", "remove", remote])
    for remote, (url, push) in remotes.items():
        run(["git", "-C", destination, "remote", "add", remote, url])
        if push != url:
            run(["git", "-C", destination, "remote", "set-url", "--push", remote, push])
    for line in refs.splitlines():
        commit, ref = line.split(" ", 1)
        # clone transfers local branch objects, but may not transfer stale remote
        # objects; retain every source ref whose object is present in the clone.
        available = subprocess.run(["git", "-C", str(destination), "cat-file", "-e", commit],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if available.returncode == 0:
            run(["git", "-C", destination, "update-ref", ref, commit])
    branch = subprocess.run(["git", "-C", str(source), "symbolic-ref", "--short", "HEAD"],
                            text=True, capture_output=True)
    if branch.returncode == 0 and "origin" in output(["git", "-C", source, "remote"]).splitlines():
        run(["git", "-C", destination, "config", f"branch.{branch.stdout.strip()}.remote", "origin"])
        run(["git", "-C", destination, "config", f"branch.{branch.stdout.strip()}.merge",
             f"refs/heads/{branch.stdout.strip()}"])
    if dotfiles and (destination / ".zshrc").is_file():
        shell = destination / ".zshrc"
        if shell.is_symlink():
            raise ValueError("Dotfiles .zshrc must be a regular file for the portability adapter")
        shell.write_text(portable_zsh(shell.read_text()))


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


def ssh_args(c, *, tty=False):
    runtime = ROOT / ".runtime" / c["name"]
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={runtime / 'known_hosts'}",
            "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
    if c.get("identity"):
        args += ["-i", str(Path(c["identity"]).expanduser()), "-o", "IdentitiesOnly=yes"]
    if tty:
        args += ["-t"]
    return args + [f"{c['username']}@{guest_ip(c)}"]


def remote(c, command, **kwargs):
    return run(ssh_args(c) + [command], **kwargs)


def wait_ssh(c, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            remote(c, "true", capture=True, stderr=subprocess.DEVNULL)
            return
        except (RuntimeError, subprocess.CalledProcessError):
            time.sleep(3)
    raise RuntimeError("Timed out waiting for SSH; inspect virsh console and the DHCP network")


def build_image():
    for key in ("NIXVM_REPOSITORY", "NIXVM_DOTFILES"):
        if key in os.environ:
            git_check(os.environ[key])
    if "NIXVM_CONNECTION" in os.environ:
        preflight(connection())
    settings = json.loads(os.environ["NIXVM_CONFIG"])
    build = Path(os.environ["NIXVM_BUILD"])
    build.mkdir(parents=True, exist_ok=True)
    snapshot = build / "nix-source"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(ROOT / "nix", snapshot, ignore=shutil.ignore_patterns("current.json", "result*"))
    (snapshot / "machines/current.json").write_text(json.dumps(settings, indent=2) + "\n")
    run(["nix", "build", f"path:{snapshot}#bootstrap", "--out-link", build / "image-result"])
    images = list((build / "image-result").rglob("*.qcow2"))
    if len(images) != 1:
        raise RuntimeError(f"Expected one QCOW2 image, found {len(images)}")
    shutil.copyfile(images[0], build / "bootstrap.qcow2")
    # An upload overwrites the QCOW2 header, including its virtual capacity.
    run(["qemu-img", "resize", build / "bootstrap.qcow2", os.environ["NIXVM_DISK_GIB"] + "G"])


def copy_checkout(c, source, destination, *, dotfiles=False, settings=None):
    quoted = shlex.quote(destination)
    exists = output(ssh_args(c) + [f"if test -d {quoted}/.git; then echo yes; fi"])
    if exists == "yes":
        print(f"Preserving existing guest checkout: {destination}")
        return
    # Never merge a fresh copy into an unknown/partially copied directory.
    remote(c, f"test ! -e {quoted} || {{ echo 'Destination exists without .git; inspect it before retrying' >&2; exit 1; }}")
    with tempfile.TemporaryDirectory(prefix="nixvm-stage-") as tmp:
        staged = Path(tmp) / "checkout"
        stage_repository(source, staged, dotfiles=dotfiles)
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
    wait_ssh(c)
    config = c["guest_config"]
    copy_checkout(c, os.environ["NIXVM_DOTFILES"], config["dotfilesPath"], dotfiles=True)
    copy_checkout(c, os.environ["NIXVM_REPOSITORY"], config["repoPath"], settings=config)
    flake = "path:" + config["repoPath"] + "/nix#devvm"
    remote(c, f"sudo nixos-rebuild switch --flake {shlex.quote(flake)}")
    print("Provisioned. Reboot with scripts/nixvm.py reboot to start GNOME cleanly.")


def preflight(c):
    failures = []
    for name in ["nix", "tofu", "virsh", "git", "ssh", "rsync", "qemu-img"]:
        if not shutil.which(name):
            failures.append(f"Missing host command: {name}")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        failures.append("/dev/kvm is missing or inaccessible")
    try:
        virsh(c, "list", "--all")
        virsh(c, "net-info", c["network"])
    except subprocess.CalledProcessError:
        failures.append("Cannot access libvirt or the configured network")
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
    parser.add_argument("command", choices=["build-image", "bootstrap", "preflight", "ssh", "forward",
                        "usb-attach", "usb-detach", "rebuild", "rollback", "password", "reboot",
                        "reprovision", "import-public-keys", "gpu-check", "sunshine-ui", "token-check"])
    parser.add_argument("--device", help="YubiKey USB port name or serial; mandatory if selection is ambiguous")
    args, extra = parser.parse_known_args()
    if args.command == "build-image":
        return build_image()
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
    elif args.command == "reprovision":
        run(["tofu", f"-chdir={ROOT / 'infrastructure'}", "apply", "-replace=terraform_data.bootstrap", *extra])
    elif args.command == "import-public-keys":
        public = subprocess.check_output(["gpg", "--export"])
        run(ssh_args(c) + ["gpg --no-autostart --import"], input=public.decode("latin1"), encoding="latin1")
    elif args.command == "gpu-check":
        remote(c, "nixvm-gpu-check")
    elif args.command == "token-check":
        print("Checking PIV through the selected PC/SC socket; protocol mismatch errors require compatible pcsc-lite versions.")
        remote(c, "nixvm-piv --list-slots")
    elif args.command == "sunshine-ui":
        print("Open https://localhost:47990 while this command runs.")
        run(ssh_args(c)[:-1] + ["-N", "-L", "127.0.0.1:47990:127.0.0.1:47990", ssh_args(c)[-1]])


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
    except KeyboardInterrupt:
        sys.exit(130)
