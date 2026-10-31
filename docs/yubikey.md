# Shared PIV/OpenPGP and USB FIDO2

## Shared mode

Start the host PC/SC service and ensure host GnuPG uses PC/SC rather than claiming
the USB CCID interface exclusively. Merge these settings into the host's
`~/.gnupg/scdaemon.conf` after reviewing existing configuration:

```text
disable-ccid
pcsc-shared
```

Restart the host scdaemon when no operation is active (`gpgconf --kill scdaemon`).
The project's scripts do not modify host GnuPG settings. Shared PC/SC serializes
card operations; PIV/OpenPGP applet switching can require retries. Do not run
concurrent card administration or key generation from multiple clients.

With Firefox closed in the guest:

```sh
scripts/nixvm.py forward
# Keep this foreground command running. In another host terminal:
scripts/nixvm.py import-public-keys
scripts/nixvm.py token-check
```

The command forwards the host GnuPG **restricted extra socket** to the socket
reported by the guest's `gpgconf`, and the host PC/SC socket to a private guest
runtime directory. No TCP PC/SC listener is created. Override a nonstandard host
socket with `NIXVM_HOST_PCSC`. No private keys or passwords are transferred.
Loss of the SSH connection removes access; run the command again to reconnect.

Host and guest pcsc-lite must have compatible IPC protocols. `token-check`
actually lists PKCS#11 slots over the selected socket; a protocol/version error
requires aligning pcsc-lite versions, not bypassing permissions. The default
supported path is same-architecture x86-64 Linux on both sides. Cross-architecture
PC/SC forwarding is not promised.

Guest commands:

```sh
nixvm-gpg --armor --detach-sign example.txt
nixvm-piv --list-slots
nixvm-piv --list-objects --type cert
nixvm-piv-ssh user@server
nixvm-firefox
```

For SSH Git remotes, use `GIT_SSH_COMMAND=nixvm-piv-ssh git push`, or set
`programs.git.settings.core.sshCommand = "nixvm-piv-ssh"` in Home Manager.

Git uses `nixvm-gpg` for GPG operations; configure your signing fingerprint and
enable commit signing when ready. Shared signing/decryption uses the host agent;
PIN prompts may appear on the host. The guest retains its own public keyring.
Its default GPG configuration disables agent autostart to preserve forwarding.

Firefox's enterprise policy registers the Nix-provided OpenSC PKCS#11 module.
Launch **Firefox (YubiKey mode)** or `nixvm-firefox` so it opens the selected
PC/SC socket. Trust/import your organization's CA certificates separately if
required for PIV client-certificate websites. Browser FIDO2 needs USB mode.

## Full USB mode

Close Firefox in the guest first, then on the host:

```sh
scripts/nixvm.py usb-attach
# If more than one key is present, explicitly select a USB port or serial:
# scripts/nixvm.py usb-attach --device 1-3
```

The host stops the project's active forwarding tunnel, switches guest token
clients to USB mode, and live-attaches exactly one Yubico USB device. Ambiguous
selection is rejected. **All interfaces move to the guest**, including PIV and
OpenPGP; the host loses access until detach. This is exclusive USB ownership,
not concurrent FIDO2 sharing. PIN entry and touches now belong to guest clients.

Use `nixvm-gpg` for USB OpenPGP, `nixvm-piv`/`nixvm-piv-ssh` for local PIV, and
reopen `nixvm-firefox` for PIV certificates or WebAuthn/FIDO2. USB OpenPGP uses a
separate guest GnuPG home and public-key import; it never starts an agent on the
forwarded shared-mode socket. Plain `gpg` remains configured for shared mode.

To return the key, close Firefox and run:

```sh
scripts/nixvm.py usb-detach
scripts/nixvm.py forward
```

Detach returns USB before making an SSH connection, so the key can authenticate
host SSH if needed. If Firefox prevents the guest mode reset, close it and retry
detach; the saved attachment record allows retry without detaching twice. USB
attachment is live-only and does not survive a guest shutdown. After unplugging
the key or an unexpected VM restart, inspect `virsh dumpxml nixvm`; retry detach
to reconcile a stale record and restore shared mode. Do not attach the same key
manually through another viewer while using these commands.

Reference behavior: [GnuPG agent forwarding](https://wiki.gnupg.org/AgentForwarding),
[OpenSC shared access](https://github.com/OpenSC/OpenSC/wiki/GnuPG-and-OpenSC), and
[libvirt USB host devices](https://libvirt.org/formatdomain.html#host-device-assignment).
