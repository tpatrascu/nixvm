# NixOS development VM

A disposable libvirt workstation with an editable Git checkout inside the VM.
OpenTofu owns the virtual hardware; NixOS and Home Manager own the guest system
and user environment. The flake and provider dependencies are locked.

## Host preparation

The supported provisioning host is x86-64 Linux with local `qemu:///system`.
Install Nix with flakes enabled using the [Nix installation instructions](https://nixos.org/download/).
On Ubuntu, install the other tools:

```sh
sudo apt install qemu-kvm libvirt-daemon-system libvirt-clients qemu-utils \
  ovmf virt-viewer git rsync python3 openssh-client pcscd opensc yubikey-manager
sudo usermod -aG libvirt,kvm "$USER"
```

Log out and back in after changing groups. Install OpenTofu >=1.9 and <2.0.
Ensure the default NAT network exists and is active, and check the available
libvirt pools:

```sh
virsh -c qemu:///system list --all
virsh -c qemu:///system pool-list --all
virsh -c qemu:///system net-list --all
test -r /dev/kvm && test -w /dev/kvm
```

Starting the existing NAT network is an explicit host administration step:
`virsh net-start default`. OpenTofu creates a
dedicated `<VM name>-user` directory pool under
`/var/lib/libvirt/images/<VM name>-user`; the directory belongs to the user
running OpenTofu and grants access to the QEMU runtime group. This keeps the new
disk within libvirt's usual AppArmor image tree while avoiding the shared
default pool's root-only file ownership. The domain references the volume's
resolved file path so Ubuntu's per-domain AppArmor helper adds the QCOW2 to its
generated allowlist. Set `storage_owner_uid` to `id -u` and
`disk_permissions.gid` to the numeric group ID shown by `id libvirt-qemu` when
your host differs from Ubuntu's defaults. The pool path can be changed with
`vm_storage_pool_path`, but it must remain below `/var/lib/libvirt/images` for
the default Ubuntu AppArmor rules. This project does not replace libvirt's host
network or NFS server configuration.

The `uefi` input selects Ubuntu's standard `/usr/share/OVMF/OVMF_CODE_4M.fd`
and matching `OVMF_VARS_4M.fd` explicitly. Override those paths on other
distributions. This avoids firmware autoselection choosing a SEV/TDX ROM that
Ubuntu's `virt-aa-helper` may reject. Keep AppArmor enabled; inspect
`journalctl -u libvirtd -u virtqemud` for the underlying rejected file if domain
startup reports that an AppArmor profile could not be loaded.

The new volume is created as the OpenTofu user's UID and the QEMU runtime GID,
with mode `0660`; the pool directory is mode `0770`. Check host IDs with
`id -u` and `id libvirt-qemu`, and configure `storage_owner_uid` and
`disk_permissions.gid` accordingly. The VM uses one managed disk in the
dedicated pool. Reprovisioning does not replace it automatically.

Both this directory and `~/.dotfiles` should be Git working trees. Provisioning
copies each directory as-is, including `.git`, ignored files, and uncommitted
changes. Git worktrees have a host-specific `.git` pointer, so use a regular
clone instead. Review the contents before provisioning; secrets and caches are
copied too.

## Create the VM

```sh
cp infrastructure/devvm.tfvars.example infrastructure/devvm.tfvars
# Edit: replace ssh_public_keys with an actual key and set repository paths.
# Optionally set `name`; it is also the guest hostname and host /etc/hosts alias.
./nixvm init
./nixvm preflight
./nixvm plan
./nixvm up
./nixvm password
./nixvm reboot
```

If a failed earlier apply left `nixvm` defined in libvirt but absent from
`tofu state list`, confirm it is shut off and remove only that stale domain
definition before applying the pool migration:

```sh
virsh -c qemu:///system domstate nixvm
virsh -c qemu:///system undefine nixvm --keep-nvram
```

This removes only the stale domain definition; it leaves the managed VM disk
and NVRAM files in place for OpenTofu to reuse.

The example is intentionally not usable until its placeholder public key is
replaced. `ssh_public_keys` accepts multiple SSH public keys; add each key as a
separate list entry to retain access from more than one host or authenticator.
Set `ssh_identity_file` in `devvm.tfvars` to force a specific local private key
for Terraform provisioning; the SSH command then uses `IdentitiesOnly` and does
not fall back to other keys in the agent. If unset, OpenSSH may use the host
agent. The private key stays outside the guest and OpenTofu state.
Passwords are set interactively after provisioning and survive rebuilds. The
development user has passwordless sudo; root SSH and SSH password login are off.

`nixvm up` checks and prepares configured host NFS exports with confirmation
before installing packages or changing host services. OpenTofu then applies
the VM and its managed firewall test subnet; after apply, the `nixvm-firewall-test`
suite checks the live libvirt network and filter. Rerun those checks with
`./nixvm firewall-test`.

After apply, `nixvm up` asks before adding the current guest DHCP address to the
host's `/etc/hosts` under the configured VM name. Sunshine uses that name for
its trusted web UI origin, so this mapping lets the host resolve the VM name
even when the guest's DHCP address changes. The entry is marked as managed by
nixvm and refreshed on later `up` runs when the guest address changes.

1. Check host tools, KVM/libvirt access, and selected GPU devices.
2. Reuse the cached NixOS UEFI QCOW2 image, or build it on the first apply.
3. Resize its QCOW2 virtual capacity to the requested disk size and upload it.
4. Create the VM and discover its IPv4 DHCP lease by its configured MAC.
5. Copy each local directory into temporary staging, transfer it to the guest,
   write non-secret machine settings, and activate the NixOS/Home Manager
   configuration over SSH.

The host paths are interpreted relative to `infrastructure/` when relative.
Guest defaults are `~/nixvm` and `~/.dotfiles`, both on the VM disk.
Submodules and Git LFS assets are not initialized automatically; this initial
project does not use either. Configure them explicitly if your dotfiles do.

## VM network access

OpenTofu defines a host-side libvirt network filter and attaches it to the VM's
interface. Guest-initiated internet access remains unrestricted. The filter
blocks guest-initiated traffic to the standard special-use, private,
documentation, benchmarking, multicast, link-local, and reserved IPv4/IPv6
ranges unless explicitly allowed by the host configuration. It does not restrict
host-initiated connections to the VM, including SSH.

Configure `host_access`, `internal_network_deny`, and `internal_network_allow` in
`infrastructure/devvm.tfvars`. `host_access` lists exact host IPs and allowed
ports. The internal network deny list and per-IP allow rules are separate
variables, so changing the allow rules preserves the default deny CIDRs. Each
allow rule specifies one IP and its own allowed ports, over both TCP and UDP.
For example, add `2049` under
`host_access.ports` to let the VM reach a host's NFSv4 service. The
default `host_access` allows DNS to the dedicated VM NAT gateway and denies
other guest-initiated access to that host IP. Add any other host IPs the VM
could reach to `host_access.ips` so their access is limited to the configured
ports.

`internal_network_deny` defaults to the standard special-use, private,
documentation, benchmarking, multicast, link-local, and reserved IPv4/IPv6
ranges. If you override this list, include any defaults you still want denied
along with organization-specific CIDRs. Each `internal_network_allow` entry
allows selected ports to one IP within the denied ranges. Set that entry's
`all_ports = true` and `ports = []` to allow every protocol and port to that IP.
The allow list defaults to empty, so internal network access remains denied.
OpenTofu reserves the test network gateway for a single probe port so the live
test can demonstrate an allowed connection; the host listener exists only while
the test is running.
Apply changes with `tofu apply`.

The VM's primary interface uses a separate, per-VM libvirt NAT network named
`nixvm-<VM name>-nat`; it does not attach to libvirt's `default` network. Set
`vm_network_cidr` to choose its IPv4 subnet (default `192.168.124.0/24`). Keep
it distinct from `firewall_test_network_cidr` and any other host/libvirt
subnets. The network is managed by OpenTofu and removed by `tofu destroy`.

OpenTofu also configures the firewall test subnet on that NAT bridge. Set
`firewall_test_network_cidr` to choose its IPv4 subnet; the default is
`192.168.123.0/24`. Its gateway must be covered by `internal_network_deny` and
absent from `internal_network_allow`, otherwise OpenTofu rejects the apply.
This gives the guest and host a reachable test endpoint without adding a
second VM interface. The subnet is removed with the NAT network by
`tofu destroy`.

The `nixvm-firewall-test` suite runs after apply and checks that both configured
subnets are present on the NAT network, and that the live libvirt filter XML
matches the policy and is attached to the VM interface. Over SSH, it adds a
temporary route to the test gateway and attempts TCP connections to two host
listeners: the configured test port must connect, while the neighboring denied
port must time out. The filter grants the test gateway one narrow port
exception for this probe, over TCP and UDP; listeners run only during the
suite. The guest also sends UDP probes and the host confirms that only the
allowed port arrives.
Generated-policy tests also check per-IP TCP/UDP port rules, all-port rules,
host-access drops, IPv4/IPv6 CIDR drops, allow-before-deny ordering, and
invalid allow-list configurations.
Run `./nixvm firewall-test` to repeat these checks after a successful
`nixvm up`. The suite fails if the managed network or live filter is missing,
differs from the configured policy, is not attached, or does not enforce the
allow and deny connectivity cases.

Provisioning accepts a new SSH host key into `.runtime/<name>/known_hosts` on
first contact. Verify its fingerprint through the libvirt console if the host
network is not trusted. A changed key is rejected; inspect it before removing an
old entry following an intentional recreation. `./nixvm destroy` removes the
VM's `known_hosts` file after a successful destroy so a later VM created with
the same name can accept its new host key.

## Edit and rebuild

Inside the VM:

```sh
cd ~/nixvm
# Edit nix/modules/* or nix/machines/current.json.
nixvm-rebuild                  # switch
nixvm-rebuild boot             # activate at next boot
sudo nixos-rebuild switch --rollback
git diff
# Commit/push/pull using your normal Git workflow.
```

`nix/machines/current.json` is an ignored, non-secret machine instance file.
Keep your OpenTofu variables to regenerate it, or commit a sanitized machine
record and copy it to `current.json` when restoring that machine. Always use the
`path:` flake URL as shown: this deliberately includes that ignored instance file.
Ordinary Git-based flake URLs omit ignored/untracked configuration files.
Set your Git identity declaratively in the Home Manager module, for example
`programs.git.settings.user = { name = "Your Name"; email = "you@example.org"; };`.
Home Manager owns the global Git configuration; avoid rewriting it with
`git config --global`.

The flake lives in `nix/` so state, SSH material, and the infrastructure directory
are outside its source. Only public settings belong anywhere under `nix/`.
Host OpenTofu variable changes affect infrastructure, but normal applies do not
overwrite the guest machine settings or checkouts after bootstrap. Use
`./nixvm update-config` to stage the host's current `nix/` configuration and
applied machine settings into a temporary guest directory, then rebuild from
there without changing the guest checkout. Use `./nixvm rebuild` to build from
the guest checkout instead.

Common commands:

| Command | Action |
| --- | --- |
| `./nixvm up` | Prepare host NFS, apply VM/network resources, then check firewall policy |
| `./nixvm u` | Apply infrastructure, then stage and activate the host's NixOS config in the guest |
| `./nixvm firewall-test` | Check the managed test network and live firewall policy |
| `./nixvm plan` / `./nixvm destroy` | Review changes / destroy the managed VM and test network |
| `./nixvm ssh` | Interactive guest shell |
| `./nixvm ssh uname -a` | Execute a guest command |
| `./nixvm ip` | Print the VM's current IPv4 address |
| `./nixvm rebuild` / `./nixvm rollback` | Activate the guest checkout / previous generation |
| `./nixvm update-config` | Stage and activate the host's NixOS config in the guest |
| `./nixvm password` / `./nixvm reboot` | Change the guest password / reboot |
| `./nixvm start --wait-ssh` / `./nixvm s` | Start the VM and wait for SSH readiness |
| `./nixvm stop` | Gracefully stop the guest and wait up to 120 seconds |
| `./nixvm stop --timeout 300` | Allow up to five minutes for graceful shutdown |
| `./nixvm stop --force` | Abruptly power off a stuck VM; unsaved guest writes may be lost |
| `./nixvm reprovision` | Explicit bootstrap retry |
| `./nixvm nfs setup` / `./nixvm nfs cleanup` | Configure host exports / remove managed exports after confirmation |
| `./nixvm image remove` | Remove the default VM's generated image cache |
| `./nixvm image remove --name workvm` | Remove another VM's generated image cache |

The wrapper uses `infrastructure/devvm.tfvars` when present. Choose another
variable file with `-var-file=PATH` or `NIXVM_VAR_FILE=PATH`. OpenTofu's direct
commands remain available, but direct `tofu apply` skips the wrapper's live
firewall policy tests and host NFS setup. The NAT network and firewall test
subnet are managed by OpenTofu. `scripts/nixvm.py`
contains lower-level callbacks and helpers; use `./nixvm` for normal host
operations.

The optional root-level `nixvm_test_ed25519` key is Git-ignored but is copied
into the guest with the repository. Keep private identities outside repository
directories. To use it with host commands, set
`NIXVM_SSH_IDENTITY="$PWD/nixvm_test_ed25519"` in that command's environment;
its public key must also be in the guest's `ssh_public_keys` machine setting.
Never add the private key to Git, OpenTofu variables, or the guest.

Reprovisioning preserves existing guest checkouts and rebuilds from them. A
failed transfer stays in a sibling `.nixvm-staging` directory and can be retried;
an unknown destination without `.git` is rejected for manual inspection.
Normal reapply does not transfer repositories or run a rebuild.

All OpenTofu-managed resources can be destroyed with `tofu destroy`, including
the VM domain, disk volume, and dedicated storage pool. Destroying the disk
permanently removes the guest filesystem and any edits or data not backed up
elsewhere. Push both repositories and back up other needed data before
destroying. The generated bootstrap image in `.build/<VM name>/` is kept, so
the next `./nixvm up` can reuse it. Run `./nixvm image remove` to delete
the default VM's cache; add `--name <VM name>` for a custom VM. This only removes
the host-side cache and does not affect an existing VM disk. Remove the cache
before applying if you want to regenerate the bootstrap image with changed
image inputs such as SSH public keys. Host `.runtime/` is also a local artifact,
not a backup.
Do not commit OpenTofu state.

## Modules and dotfiles

`nix/modules/system/` holds base hardware/users, GNOME, NFS, GPU/streaming, and
smartcard services. `nix/modules/home/` holds user settings, mutable dotfile links,
GNOME preferences, token clients, applications, and independent Python/Node/LLVM/Rust modules.
Home Manager activates with `nixos-rebuild`; no separate activation is needed.

The Home Manager application module installs Zed, Kitty, and Oh My Pi. OMP uses
the upstream Linux x86_64 release binary at version 18.12.6, pinned by SHA-256
in `nix/packages/omp-binary.nix`, so rebuilding does not compile its source tree.
GNOME uses the packaged Adw-gtk3 dark theme and the packaged Vanilla-DMZ white
cursor theme; 0xProto Nerd Font is configured for Zed buffers, Zed's integrated
terminal, and Kitty. The dock favorites include Firefox, Kitty, Zed, and Files,
with launcher icons pointing directly at their packaged icon assets. The supported host GNOME extensions are installed
and enabled declaratively; Ubuntu Snap-specific extensions and Desktop Icons NG
are not available in the pinned NixOS package set.

All four language modules are enabled in the example. Toggle them with the
`toolchains` object or the guest machine JSON. Python includes pip and uv; use
`uv venv`/`python -m venv` for project packages. Node includes npm and Corepack.
LLVM includes Clang, clangd, LLDB, CMake, Ninja, and pkg-config. Rust includes
Cargo, rustfmt, Clippy, and rust-analyzer. Versions follow the committed lock file.

User-wide compilers do not provide every project's native library. Python wheels,
Node native addons, CMake projects, and downloaded ELF binaries may need explicit
Nix libraries, a project-specific shell, or packaging. Avoid global `sudo pip`
and `sudo npm`. Update inputs deliberately with `nix flake update --flake ./nix`,
then review the lock and rebuild; no automatic updates run.

Dotfiles are copied as-is. Make any VM-specific `.zshrc` compatibility changes
yourself. Oh My Zsh and Powerlevel10k come from Nix; `.p10k.zsh` links to the
editable checkout. Extend `dotfiles_links` for other files. Dotfiles are never
pulled automatically. Existing conflicting Home Manager destinations are not
overwritten.

## NFS

`nfs_mounts` is a list of remote NFS exports you define manually; each entry has
`server`, `export`, `mountpoint`, and optional mount options. Use
`host_nfs_exports` for host directories exported and mounted by the VM; each
entry maps an absolute host `host_path` to an absolute guest `guest_path`. The
guest uses `streaming.host_address` as the host's libvirt-network address; when
unset, it uses the dedicated VM NAT gateway.

For an `nfs_mounts` server whose IP falls within `internal_network_deny`, the
VM firewall automatically allows TCP port 2049 for NFSv4. You do not need to
add a matching `internal_network_allow` entry. Servers outside the deny ranges
already follow the general internet access policy.

When `host_nfs_exports` is nonempty, `./nixvm up` invokes host setup before
OpenTofu. The script detects the host distribution, prints the
package/service/export changes, and requires you to type `yes` before
proceeding. It supports Debian/Ubuntu,
Fedora/RHEL, Arch, and openSUSE families; it installs the NFS server package if
needed and writes `/etc/exports.d/nixvm-<name>.exports`. Source directories must
already exist. Exports are limited to the selected libvirt network's IPv4 CIDR;
other guests attached to that same network can also access them. The script
does not change host firewall rules, so allow TCP port 2049 from that subnet if
the host firewall is active. On SELinux-enforcing hosts, assign suitable NFS
export labels to the source directories yourself; the script does not change
SELinux policy. When `host_nfs_exports` is nonempty, OpenTofu checks
that the managed export file and NFS service are ready, and allows guest traffic
to host port 2049 in the VM's libvirt filter. Run `./nixvm nfs cleanup` to remove
the managed export file after confirmation; the package and service remain
installed. `./nixvm destroy` retains host exports.

Match guest UID/GID with the host owner for normal NFS AUTH_SYS permissions.
The NFSv4 automount does not block boot when the host export is down. Once in use,
the default hard mount can block I/O during server outages; it prioritizes write
integrity. Do not put the Nix store or either configuration checkout on NFS.

## GPU passthrough and streaming

See [GPU setup](docs/gpu.md). GPU passthrough is disabled by default. Select all
GPU PCI functions explicitly; no model or PCI address from the development host
is hardcoded. No script unbinds the host graphics driver or changes boot settings.

The accelerated profile uses GNOME Wayland, NVIDIA Vulkan, Sunshine KMS capture,
and NVENC, with Moonlight on the host. Connect a monitor or dummy plug to the
passed-through GPU. GNOME autologin and disabled idle locking support unattended
desktop startup; adjust these deliberately if the VM needs a locked desktop.

## YubiKey

See [YubiKey setup](docs/yubikey.md) for shared PIV/OpenPGP, browser certificates,
and explicit USB attachment for FIDO2. Forwarding starts only when requested and
ends with Ctrl-C; no background service or reconnect automation is installed.

## Validation

```sh
scripts/check.sh
# On a normal Nix builder, also build the image and both full system variants:
NIXVM_BUILD_CHECKS=1 scripts/check.sh
```

Mocked OpenTofu plans do not create VMs. Python tests cover VM lifecycle behavior
and preservation of existing guest checkouts.

Hardware acceptance after the first boot:

1. Verify SSH, GNOME autologin, tool versions, and `nixvm-rebuild`; test rollback.
2. Make uncommitted changes in both guest repositories, reapply OpenTofu, and
   confirm those changes remain. Interrupt a bootstrap transfer and retry.
3. Verify NFS read/write with matching ownership and boot with the export down.
4. With GPU enabled, run `./nixvm gpu-check`, pair Moonlight, and verify
   1080p60 video, audio, input, and successful streaming after reboot.
5. Verify GPG signing and PIV CLI/browser certificates in shared mode; attach
   USB, verify browser FIDO2, detach, and restart forwarding to verify recovery.
6. After saving work, recreate from the repositories and confirm the same setup.

This session can evaluate the locked configurations and validate/mock the
infrastructure. Actual image/system builds, VM boot, NFS, GPU capture, NVENC, and
physical token acceptance require a normal host with Nix, KVM/libvirt access,
appropriate hardware, and binary-cache/build access.
