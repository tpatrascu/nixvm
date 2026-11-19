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
cp infrastructure/nixvm.tfvars.example infrastructure/nixvm.tfvars
# Edit: replace ssh_public_keys with an actual key and set repository paths.
./nixvm init
./nixvm preflight
./nixvm plan
./nixvm up
./nixvm password
./nixvm reboot
```

The VM name defaults to `nixvm`. Select another VM with `-n` or `--name`:
`./nixvm -n nixvm2 up` uses `infrastructure/nixvm2.tfvars` when that file
exists. The selector works on every command, for example
`./nixvm ssh -n nixvm2` or `./nixvm -n nixvm2 plan`. You can still pass
`-var-file=PATH` to choose another settings file. NixVM creates a matching
OpenTofu workspace and scopes the VM disk, network, build cache, SSH runtime
files, NFS export, state, and SPICE launcher to that name. Each VM gets its own
NAT network; default CIDRs are derived from the VM name and can be overridden
with `vm_network_cidr` and `firewall_test_network_cidr`. Its MAC is derived
from the VM name unless you set `mac_address`. Existing VMs must be destroyed
from their original workspace before being recreated in the named workspace;
no state migration is performed.

If a failed earlier apply left `nixvm` defined in libvirt but absent from
`TF_WORKSPACE=nixvm tofu -chdir=infrastructure state list`, confirm it is shut
off and remove only that stale domain
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
Set `ssh_identity_file` in `nixvm.tfvars` to force a specific local private key
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
3. Build the configured workstation closure on the host so available packages
   are fetched into the host Nix store before guest provisioning.
4. Resize the QCOW2 image to the requested disk size and upload it.
5. Create the VM and discover its IPv4 DHCP lease by its configured MAC.
6. Copy each local directory into temporary staging, transfer it to the guest,
   and write non-secret machine settings.
7. Import the host-cached workstation closure into the guest Nix store over SSH,
   then activate NixOS/Home Manager. The guest fetches any remaining paths from
   its configured Nix substituters.

The host paths are interpreted relative to `infrastructure/` when relative.
Guest defaults are `~/nixvm` and `~/.dotfiles`, both on the VM disk.
Submodules and Git LFS assets are not initialized automatically; this initial
project does not use either. Configure them explicitly if your dotfiles do.

## VM network access

OpenTofu defines a host-side libvirt network filter and attaches it to the VM's
interface. It blocks guest-initiated traffic to the standard special-use,
private, documentation, benchmarking, multicast, link-local, and reserved
IPv4/IPv6 ranges unless explicitly allowed by the host configuration. With
mitmproxy enabled, it also blocks guest egress to TCP and UDP destination ports
in `mitm_proxy_blocked_http_ports`, which defaults to `[80, 443, 8443, 8080,
8081, 8082]`. The only web-proxy exception is TCP/8080 to the host's managed
mitmproxy listener on the VM gateway. With mitmproxy disabled, direct web egress
is allowed. Host-initiated connections to the VM, including SSH, are not
restricted.

Configure `host_access`, `internal_network_deny`, and `internal_network_allow` in
`infrastructure/nixvm.tfvars`. `host_access` lists exact host IPs and separate
`tcp_ports` and `udp_ports` lists. DNS to the VM's libvirt gateway is always
allowed by the VM policy.
Enabled host services (gateway DNS, mitmproxy, and configured host NFS) also
receive automatic exceptions in the internal-network filter, so they do not
need duplicate `internal_network_allow` or `host_access` entries. The internal
network deny list and per-IP allow rules are separate
variables, so changing the allow rules preserves the default deny CIDRs. Each
internal allow rule specifies one IP and its own TCP and UDP destination ports.
For example, add `2049` to `host_access.tcp_ports` to let the VM reach a host's
NFSv4 service. The
default `host_access` denies other guest-initiated access to the gateway;
DNS and enabled service ports receive separate automatic exceptions.
Set `host_firewall = "ufw"` to have `nixvm up` add persistent UFW allows from
this VM's subnet for gateway DNS (TCP and UDP 53), enabled mitmproxy (TCP
8080), and host NFS exports (TCP 2049 when configured). Rules are scoped to
the VM subnet and service destination. Set `host_firewall = "disabled"` to
leave UFW unchanged; this is the default. The `ufw` choice requires UFW to be
installed and does not enable UFW itself.
Blocked web ports cannot be added to `host_access.tcp_ports` or
`host_access.udp_ports`. An explicit TCP or UDP port in
`internal_network_allow` overrides the global web-port block for that IP;
`all_ports` still leaves those ports blocked. Add any other host IPs the VM
could reach to `host_access.ips` so their access is limited to the configured
TCP and UDP ports.
Set `mitm_proxy_blocked_http_ports` to customize the TCP and UDP ports blocked
for direct web access while mitmproxy is enabled. The default list is
`[80, 443, 8443, 8080, 8081, 8082]`.

### Host-side HTTPS proxy

Mitmproxy is disabled by default. Set `mitmproxy_enabled = true` in the
selected tfvars file to enable host proxy setup and guest proxy/CA configuration.
With it disabled, the VM connects to web services directly, while its
internal-network denies remain in force.

When enabled, `./nixvm up` installs Podman on supported Ubuntu hosts if needed, enables
lingering for the current host user, and creates a Quadlet user service for the
official mitmproxy container. It provisions the rootless Podman subordinate ID
ranges when needed; if other rootless containers are running, stop them and
rerun `up` so Podman can migrate to the new mappings safely. It pulls the
configured image each time `up` runs and enables a persistent systemd user
timer that checks this container for registry updates daily. When the image
changes, Podman restarts the proxy service with the updated image. The default
`docker.io/mitmproxy/mitmproxy:latest` tag tracks the latest stable release.
One host service is shared by all VMs. The container uses Podman's
rootless pasta network and publishes proxy port 8080 on host interfaces so
each VM can reach it through its own network gateway. The mitmweb traffic
viewer is bound to host loopback at
[http://127.0.0.1:8081](http://127.0.0.1:8081).
Open the UI and log in automatically with:

```sh
./nixvm mitm open
```

This transfers the password directly to the local mitmweb service to establish
its authentication cookie; the password is not placed in the browser URL. To
print the shared UI password manually, run:

```sh
./nixvm mitm password
```

The shared password is stored with mode `0600` in
`~/.local/share/nixvm/shared/mitmproxy/web_password`.

The guest sends web requests through the host proxy at the VM network
gateway's port 8080. The mitmweb page on host port 8081 shows proxied requests
and is available only from the host itself. Configure the image, HTTPS
inspection, and hostname blocking through the `mitmproxy_policy` OpenTofu
variable below. The default image is `docker.io/mitmproxy/mitmproxy:latest`;
`nixvm up` pulls the configured image on each run.

```hcl
mitmproxy_policy = {
  inspect_domains = ["api.example.com", "glob:*.inspect.example.net"]
  blocked_domains = ["telemetry.example.net", "glob:*.ads.example.org"]
  allowed_domains = ["api.telemetry.example.net", "regex:trusted[0-9]+\\.example\\.com"]
  allowed_internal_hosts = [
    { hostname = "git.internal.example", ips = ["192.168.50.10"], ports = [443] },
  ]
}
```

Bare hostnames match themselves and their subdomains. Prefix an entry with
`glob:` to match a whole hostname using glob syntax (`*`, `?`, and character
ranges), or with `regex:` to match the whole hostname using a Python/RE2
compatible regular expression. Glob and regex patterns match the normalized
hostname as written; use `glob:*.example.com` to match subdomains. Entries in
`allowed_domains` override matching `blocked_domains`; the internal CIDR deny
policy still applies unless a hostname has an `allowed_internal_hosts` entry.
Those entries match one exact hostname and require DNS to resolve only to one
of the listed IPs on one of the listed ports (443 by default). They authorize
the proxy's upstream connection only; the VM's direct LAN firewall deny stays
in force. Add the hostname to `allowed_domains` too if a `blocked_domains`
pattern matches it. `inspect_domains` selects HTTPS hosts whose TLS is
decrypted; an empty list decrypts none. It accepts the same hostname, glob, and
regex forms. The VM trusts the proxy's generated CA, which `nixvm up` copies
into the guest configuration.
The mitmproxy add-on also denies connections whose destination resolves into
any CIDR in `internal_network_deny`, matching the guest-side filter. The host
CA private key stays in the Podman volume; only its public certificate is
copied to the guest. The mitmweb UI shows live flows and keeps them in memory.

`nixvm up` installs the CA and configures lowercase and uppercase proxy
environment variables for login sessions and system services, GNOME's desktop
proxy, Firefox's managed proxy policy, and CA bundle paths used by common TLS
clients. Loopback remains excluded from proxying. Direct web egress is blocked
by the libvirt filter, including QUIC over UDP/443, so applications that ignore
system proxy settings cannot connect directly on the standard web ports.
Certificate-pinned or mutual-TLS applications may need to be left out of
`inspect_domains`. On
`nixvm destroy`, the shared proxy stays available to other VMs. Its Podman
volume is retained so the CA can be reused.

To stop and remove the host-side mitmproxy service, its update timer, and its
generated policy, password, and public CA copy, run:

```sh
./nixvm mitm cleanup
```

The Podman volume containing the proxy CA is retained so a later `nixvm up`
can reuse it.

`internal_network_deny` defaults to the standard special-use, private,
documentation, benchmarking, multicast, link-local, and reserved IPv4/IPv6
ranges. If you override this list, include any defaults you still want denied
along with organization-specific CIDRs. Each `internal_network_allow` entry
allows selected TCP and UDP ports independently to one IP within the denied
ranges. The allow list defaults to empty, so internal network access remains
denied. `all_ports = true`
allows other ports to that IP while the managed web-port blocks remain in force.
To permit a blocked port to an internal address, list it in that protocol's
port list; for example, `{ ip = "10.0.0.25", tcp_ports = [443], udp_ports = [], all_ports = false }`
permits TCP HTTPS only to that IP.
OpenTofu reserves the test network gateway for a single probe port so the live
test can demonstrate an allowed connection; the host listener exists only while
the test is running.
The host proxy has one shared hostname policy; the last `up` run updates it from
that VM's `mitmproxy_policy` settings. Apply changes with `./nixvm up`.

The VM's primary interface uses a separate, per-VM libvirt NAT network named
`nixvm-<VM name>-nat`; it does not attach to libvirt's `default` network. Set
`vm_network_cidr` to choose its IPv4 subnet. The default is stable and
VM-specific (`192.168.124.0/24` for `nixvm`). Keep it distinct from
`firewall_test_network_cidr` and any other host/libvirt subnets. The network is
managed by OpenTofu and removed by `./nixvm destroy`.

OpenTofu also configures the firewall test subnet on that NAT bridge. Set
`firewall_test_network_cidr` to choose its IPv4 subnet. The default is
VM-specific (`192.168.123.0/24` for `nixvm`). Its gateway must be covered by
`internal_network_deny` and absent from `internal_network_allow`, otherwise
OpenTofu rejects the apply.
This gives the guest and host a reachable test endpoint without adding a
second VM interface. The subnet is removed with the NAT network by
`./nixvm destroy`.

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
VM's `known_hosts` file and its nixvm-managed `/etc/hosts` mapping after a
successful destroy. Host NFS exports remain configured.

## Edit and rebuild

Inside the VM, the repository checkout directory matches the selected VM name
(`~/nixvm` by default, or `~/workvm` when you pass `-n workvm`):

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
`./nixvm up` stages the host NixOS configuration and machine settings into a
temporary guest directory to activate the managed proxy URL and CA trust; it
does not overwrite the guest checkout. Use `./nixvm update-config` to stage
other host NixOS changes the same way. Use `./nixvm rebuild` to build from the
guest checkout instead.

Host-specific CA certificates are opt-in. Run `./nixvm copy-cas` to copy
administrator-installed public CA certificates from the host's local trust
anchor directories into the guest and activate them in NixOS trust. The command
recognizes Debian/Ubuntu's `/usr/local/share/ca-certificates`, Fedora/RHEL's
`/etc/pki/ca-trust/source/anchors`, Arch's
`/etc/ca-certificates/trust-source/anchors`, and openSUSE's
`/etc/pki/trust/anchors`. Set `NIXVM_HOST_CA_DIRS` to a path-separated list of
additional anchor directories for other host layouts. It imports certificate
files (`.crt`, `.pem`, `.cer`, `.der`), not the host's generated public root
bundle or private keys. The selected certificates are snapshotted under
`.runtime/shared/host-ca`; later `update-config` and bootstrap rebuilds reuse
that snapshot. Run `copy-cas` again to refresh or remove imported certificates.

Common commands:

| Command | Action |
| --- | --- |
| `./nixvm up` | Prepare enabled host services and selected host firewall rules, apply VM/network resources, activate guest config, then check firewall policy |
| `./nixvm u` | Same as `up` |
| `./nixvm firewall-test` | Check the managed test network and live firewall policy |
| `./nixvm plan` / `./nixvm destroy` | Review changes / destroy the managed VM and test network |
| `./nixvm ssh` | Interactive guest shell |
| `./nixvm ssh uname -a` | Execute a guest command |
| `./nixvm ip` | Print the VM's current IPv4 address |
| `./nixvm rebuild` / `./nixvm rollback` | Activate the guest checkout / previous generation |
| `./nixvm update-config` | Stage and activate the host's NixOS config in the guest |
| `./nixvm copy-cas` | Explicitly copy host custom CA certificates and activate guest trust |
| `./nixvm password` / `./nixvm reboot` | Change the guest password / reboot |
| `./nixvm start --wait-ssh` / `./nixvm s` | Start the VM and wait for SSH readiness |
| `./nixvm stop` | Gracefully stop the guest and wait up to 120 seconds |
| `./nixvm stop --timeout 300` | Allow up to five minutes for graceful shutdown |
| `./nixvm stop --force` | Abruptly power off a stuck VM; unsaved guest writes may be lost |
| `./nixvm reprovision` | Explicit bootstrap retry |
| `./nixvm nfs setup` / `./nixvm nfs cleanup` | Configure host exports / remove managed exports after confirmation |
| `./nixvm mitm open` | Open the host mitmweb UI and log in automatically |
| `./nixvm mitm o` | Alias for `./nixvm mitm open` |
| `./nixvm mitm password` | Print the shared mitmweb UI password |
| `./nixvm mitm cleanup` | Stop and remove the host mitmproxy service and generated setup files; retain its CA volume |
| `./nixvm image remove` | Remove the selected VM's image and workstation build cache |
| `./nixvm -n workvm image remove` | Remove another VM's image and workstation build cache |
| `./nixvm cache clean` | Collect unreachable paths from the host Nix store; keep profile generations |
| `./nixvm cache clean --delete-old` | Delete old host profile generations, then collect garbage |

The cache commands use `nix-collect-garbage` from the invoking user's `PATH`
and run it with `sudo`, so the Nix profile must be loaded in that shell.

The wrapper uses the selected VM's matching `infrastructure/<name>.tfvars`
when present, with `nixvm` as the default. Choose a different variable file
with `-var-file=PATH` or `NIXVM_VAR_FILE=PATH`. State is stored in the matching
local workspace under `infrastructure/terraform.tfstate.d/`. OpenTofu's direct
commands remain available, but they do not select a profile automatically.
Use `./nixvm` for normal operations; a direct `tofu apply` also skips the
wrapper's host NFS setup and verification, network filter refresh, and live
firewall policy tests. The `terraform_data.network_filter` and
`terraform_data.host_nfs` resources track their current inputs in place; the
wrapper runs the corresponding host operations before apply.
The NAT network and firewall test subnet are managed by OpenTofu. `scripts/nixvm.py`
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

All OpenTofu-managed resources for the selected profile can be destroyed with
`./nixvm destroy`, including
the VM domain, disk volume, and dedicated storage pool. Destroying the disk
permanently removes the guest filesystem and any edits or data not backed up
elsewhere. Push both repositories and back up other needed data before
destroying. Each VM's resized bootstrap copy and host-built workstation link in
`.build/<VM name>/` are kept for reuse. The shared bootstrap base is kept in
`.build/shared/`, and all builds use the host's shared Nix store;
`./nixvm destroy` does not delete or garbage-collect this host cache. Run
`./nixvm image remove` to delete the selected VM's build cache. This only
removes host-side files and does not affect an existing VM disk. The shared
bootstrap base is retained. Host `.runtime/` is also a local artifact,
not a backup.
Do not commit OpenTofu state.

## Modules and dotfiles

`nix/modules/system/` holds base hardware/users, GNOME, NFS, GPU/streaming, and
smartcard services. `nix/modules/home/` holds user settings, mutable dotfile links,
GNOME preferences, token clients, applications, and independent Python/Node/LLVM/Rust modules.
Home Manager activates with `nixos-rebuild`; no separate activation is needed.

The `apps` object groups software into `editors` (Zed and VS Code), `terminals`
(Kitty and Ghostty), `browsers` (Firefox and Chromium), and `coding_agents` (Oh
My Pi, Codex CLI, ChatGPT desktop, Claude Code, and OpenCode). Optional app
toggles default off, except Firefox, which defaults on for YubiKey-backed
browsing. The selected defaults also enable Kitty. Chromium and Ghostty are
disabled by default. Configure
`default_editor`, `default_terminal`, `default_shell`, `default_browser`, and
`default_coding_agent` to choose the corresponding defaults; selecting an
editor, terminal, browser, or coding agent enables it and pins its icon to the
GNOME dock. Defaults are Neovim, Kitty, Zsh, Firefox, and no coding agent. Set
`additional_nix_pkgs` to Nixpkgs attribute paths to add
packages to the guest, for example `"ripgrep"` or `"python3Packages.pip"`.
Firefox and Chromium use DuckDuckGo as their default search engine.
The default editor is Neovim; set `default_editor` to `"zed"` or `"vscode"`
to select and enable that editor.
ChatGPT uses a NixOS-compatible repackaging of OpenAI's signed Linux application because
Nixpkgs `pkgs.chatgpt` targets macOS. Its optional anonymous launch metric is disabled. OMP uses
the upstream Linux x86_64 release binary at version 18.12.6 when
`apps.coding_agents.omp` is enabled, pinned by SHA-256
in `nix/packages/omp-binary.nix`, so rebuilding does not compile its source tree.
GNOME uses the packaged Adw-gtk3 dark theme and the packaged Vanilla-DMZ white
cursor theme; 0xProto Nerd Font is configured for Zed buffers, Zed's integrated
terminal, and Kitty. The dock favorites include Firefox and Files, plus Kitty
and Zed when enabled, with launchers pointing at their packaged icons. The
supported host GNOME extensions are installed
and enabled declaratively; Ubuntu Snap-specific extensions and Desktop Icons NG
are not available in the pinned NixOS package set.

Python, Node, LLVM, Rust, JDK, Garmin, and TCC toolchains are disabled by default.
Enable them with the `toolchains` object in the selected variable file. Python
includes pip and uv; use `uv venv`/`python -m venv` for project packages. Node includes npm and
Corepack. LLVM includes Clang, clangd, LLDB, CMake, Ninja, and pkg-config. Rust
includes Cargo, rustfmt, Clippy, and rust-analyzer. TCC installs the Tiny C
Compiler. Versions follow the committed lock file.

The `jdk` and `garmin` toolkit toggles both install the Nix JDK (`pkgs.jdk`).
Garmin's
Connect IQ SDK Manager and SDK are downloaded through Garmin's [SDK page](https://developer.garmin.com/connect-iq/sdk/)
after accepting its developer agreement and signing in; Nix does not download
those licensed files. After enabling the Garmin toolkit, download and install
the SDK Manager yourself, then use it to fetch the Connect IQ SDK and device
simulators.

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

For every IP address configured as an `nfs_mounts` server, the VM firewall
automatically allows TCP port 2049 for NFSv4, including servers outside
`internal_network_deny`. You do not need to add a matching
`internal_network_allow` entry. Libvirt filters match IP addresses, so a
hostname does not get a generated exception.

When `host_nfs_exports` is nonempty, `./nixvm up` invokes host setup before
OpenTofu. The script detects the host distribution, prints the
package/service/export changes, and requires you to type `yes` before
proceeding. It supports Debian/Ubuntu,
Fedora/RHEL, Arch, and openSUSE families; it installs the NFS server package if
needed and writes `/etc/exports.d/nixvm-<name>.exports`. Source directories must
already exist. Exports are limited to the selected libvirt network's IPv4 CIDR;
other guests attached to that same network can also access them. With
`host_firewall = "ufw"`, `nixvm up` adds a persistent UFW allow for TCP 2049
from the VM subnet; with the default `disabled` setting, allow that port
yourself if the host firewall requires it. On SELinux-enforcing hosts, assign
suitable NFS export labels to the source directories yourself; the script does
not change SELinux policy. When `host_nfs_exports` is nonempty, OpenTofu checks
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
