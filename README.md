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

Both this directory and `~/.dotfiles` must be genuine Git working trees with at
least one commit. Initialize them or clone their real remotes before provisioning.
Git worktrees are supported; their host-specific `.git` pointer is not copied.
Do not commit credentials: complete Git history is transferred along with the
current branch. Provisioning excludes common credential/cache files from the
working-tree overlay, but cannot erase secrets from Git history.

## Create the VM

```sh
cp infrastructure/devvm.tfvars.example infrastructure/devvm.tfvars
# Edit: replace ssh_public_keys with an actual key and set repository paths.
tofu -chdir=infrastructure init
scripts/nixvm.py preflight -var-file=devvm.tfvars
tofu -chdir=infrastructure plan -var-file=devvm.tfvars
tofu -chdir=infrastructure apply -var-file=devvm.tfvars
scripts/nixvm.py password
scripts/nixvm.py reboot
```

If a failed earlier apply left `devvm` defined in libvirt but absent from
`tofu state list`, confirm it is shut off and remove only that stale domain
definition before applying the pool migration:

```sh
virsh -c qemu:///system domstate devvm
virsh -c qemu:///system undefine devvm --keep-nvram
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

The first apply performs these ordered steps:

1. Check host tools, KVM/libvirt access, and selected GPU devices.
2. Build a minimal NixOS UEFI QCOW2 image with your user and SSH public keys.
3. Resize its QCOW2 virtual capacity to the requested disk size and upload it.
4. Create the VM and discover its IPv4 DHCP lease by its configured MAC.
5. Independently clone each local repository into staging, preserve history,
   restore its real Git remotes, and overlay dirty/untracked nonignored files.
6. Transfer the staged checkouts, write non-secret machine settings, and activate
   the complete NixOS/Home Manager configuration over SSH.

The host paths are interpreted relative to `infrastructure/` when relative.
Guest defaults are `~/dev/nixvm` and `~/.dotfiles`, both on the VM disk.
Submodules and Git LFS assets are not initialized automatically; this initial
project does not use either. Configure them explicitly if your dotfiles do.

Provisioning accepts a new SSH host key into `.runtime/<name>/known_hosts` on
first contact. Verify its fingerprint through the libvirt console if the host
network is not trusted. A changed key is rejected; inspect it before removing an
old entry following an intentional recreation.

## Edit and rebuild

Inside the VM:

```sh
cd ~/dev/nixvm
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
Host OpenTofu variable changes affect infrastructure, but **do not overwrite the
guest machine settings or checkouts after bootstrap**. Edit settings in the VM
and rebuild when changing its operating system configuration.

Host convenience commands:

| Command | Action |
| --- | --- |
| `scripts/nixvm.py ssh` | Interactive guest shell |
| `scripts/nixvm.py ssh uname -a` | Execute a guest command |
| `scripts/nixvm.py rebuild` | Activate the guest's current checkout |
| `scripts/nixvm.py rollback` | Activate the preceding system generation |
| `scripts/nixvm.py password` | Set/change the guest login password |
| `scripts/nixvm.py reboot` | Reboot the guest |
| `scripts/nixvm.py start --wait-ssh` | Start the VM and wait for SSH readiness |
| `scripts/nixvm.py stop` | Request graceful guest shutdown and wait up to 120 seconds |
| `scripts/nixvm.py stop --timeout 300` | Allow up to five minutes for graceful shutdown |
| `scripts/nixvm.py stop --force` | Abruptly power off a stuck VM; unsaved guest writes may be lost |
| `scripts/nixvm.py reprovision -var-file=devvm.tfvars` | Explicit bootstrap retry |

The optional root-level `nixvm_test_ed25519` key is Git-ignored and excluded
from repository transfers. To use it with host commands, set
`NIXVM_SSH_IDENTITY="$PWD/nixvm_test_ed25519"` in that command's environment;
its public key must also be in the guest's `ssh_public_keys` machine setting.
Never add the private key to Git, OpenTofu variables, or the guest.

Reprovisioning preserves existing guest Git directories and rebuilds from them.
A failed transfer stays in a sibling `.nixvm-staging` directory and can be
retried; an unknown destination without `.git` is rejected for manual inspection.
Normal reapply does not transfer repositories or run a rebuild.

All OpenTofu-managed resources can be destroyed with `tofu destroy`, including
the VM domain, disk volume, and dedicated storage pool. Destroying the disk
permanently removes the guest filesystem and any edits or data not backed up
elsewhere. Push both repositories and back up other needed data before
destroying. Host `.build/` and `.runtime/` are local artifacts, not backups.
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

The initial dotfiles copy receives a small, reviewable `.zshrc` portability edit:
guard host Homebrew initialization, accept Nix's OpenSC path, and avoid loading
Oh My Zsh twice. The host dotfiles checkout is untouched. Review/commit the diff
from the guest and pull it normally on the host. Oh My Zsh and Powerlevel10k come
from Nix; `.p10k.zsh` links to the editable checkout. Extend `dotfiles_links` for
other files. Runtime completion caches are excluded. Dotfiles are never pulled
automatically. Existing conflicting Home Manager destinations are not overwritten.
The portability adapter requires `.dotfiles/.zshrc` to be a regular file; it
rejects symlinks rather than risk rewriting a target outside the staged copy.

## NFS

Configure a host export separately, restrict it to the VM/network, and set
`nfs_mounts` in your variables. For the default libvirt NAT network the host is
usually `192.168.122.1`; verify your actual network. Each mount declares `server`,
`export`, a distinct `/mnt/...` mountpoint, and optional NFS options.

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

Mocked OpenTofu plans do not create VMs. Python tests exercise actual temporary
Git repositories, dirty/deleted/untracked files, credential exclusions, worktree
independence, dotfiles portability, and preservation of existing guest checkouts.

Hardware acceptance after the first boot:

1. Verify SSH, GNOME autologin, tool versions, and `nixvm-rebuild`; test rollback.
2. Make uncommitted changes in both guest repositories, reapply OpenTofu, and
   confirm those changes remain. Interrupt a bootstrap transfer and retry.
3. Verify NFS read/write with matching ownership and boot with the export down.
4. With GPU enabled, run `scripts/nixvm.py gpu-check`, pair Moonlight, and verify
   1080p60 video, audio, input, and successful streaming after reboot.
5. Verify GPG signing and PIV CLI/browser certificates in shared mode; attach
   USB, verify browser FIDO2, detach, and restart forwarding to verify recovery.
6. After saving work, recreate from the repositories and confirm the same setup.

This session can evaluate the locked configurations and validate/mock the
infrastructure. Actual image/system builds, VM boot, NFS, GPU capture, NVENC, and
physical token acceptance require a normal host with Nix, KVM/libvirt access,
appropriate hardware, and binary-cache/build access.
