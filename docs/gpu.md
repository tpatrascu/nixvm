# NVIDIA passthrough and Moonlight

## Host preparation

Use a GPU dedicated to the VM and keep the host on another graphics device.
Confirm the selected model supports NVENC using NVIDIA's
[codec support matrix](https://developer.nvidia.com/video-encode-decode-support-matrix).
The MX450 visible during development is not a suitable NVENC example.

Enable VT-d/AMD-Vi in firmware. On Ubuntu, configure the appropriate IOMMU kernel
arguments (`intel_iommu=on` or `amd_iommu=on`, optionally `iommu=pt`) and early
`vfio-pci` binding for the selected GPU and companion functions. Update GRUB and
the initramfs and reboot. Host configuration is machine-specific; this repository
does not edit it. Use PCI addresses to distinguish identical GPUs rather than
blindly binding every device with the same vendor/product ID.

Inspect before enabling passthrough:

```sh
lspci -nnk
find /sys/kernel/iommu_groups/ -type l
scripts/nixvm.py preflight -var-file=devvm.tfvars
```

Preflight requires selected functions to be bound to `vfio-pci`, checks companion
functions and non-bridge IOMMU peers, and rejects a host boot VGA device. It never
detaches hardware. Group isolation, laptop firmware, reset support, and GPU ROM
requirements remain hardware-specific. Do not use ACS override as a default.
An optional `gpu.rom_file` is a host path accessible to libvirt/QEMU and applies
to the first selected function. Supply only a ROM suitable for that exact card.

## Configure the VM

Set `gpu.enabled`, every required `gpu.pci_functions` address, and driver options
before creating the VM. The default open NVIDIA kernel module targets Turing or
newer supported devices. For older supported cards, select compatible driver
settings; the stable driver is not a universal legacy-driver solution.

GPU mode uses UEFI/Q35, managed PCI host devices, and host CPU passthrough. SSH
and `virsh -c qemu:///system console devvm` remain available. SPICE guest agent
integration and the localhost SPICE display are enabled by default, including
with GPU passthrough; use `virt-viewer -c qemu:///system devvm` to connect.

When adding a GPU to an existing VM, first enable the matching guest driver and
streaming settings in `nix/machines/current.json` and rebuild. Shut down the VM,
prepare VFIO, then change/apply OpenTofu hardware settings. Changing hardware
variables does not recopy the configuration repository. Reboot after activation
to start a clean graphical session on the passed-through GPU.

## Streaming

Install Moonlight on the host using its
[official setup guide](https://github.com/moonlight-stream/moonlight-docs/wiki/Setup-Guide).
The guest runs Sunshine with NVIDIA KMS capture and CUDA/NVENC support. Follow
[Sunshine's documentation](https://docs.lizardbyte.dev/projects/sunshine/latest/)
for encoder/display diagnostics.

Set `streaming.host_address` to the host's IPv4 address on the libvirt network
(default `192.168.122.1`). When streaming is enabled, the guest firewall permits
all inbound IPv4 traffic from this address. UPnP is disabled and no public/LAN
port forwarding is created. The VM still uses NAT for outbound package downloads.

```s
./nixvm gpu-check
./nixvm reboot
```

Use the Moonlight native client to connect to the guest and view the Desktop
stream; the Sunshine web UI is for initial client pairing and server
administration, not for viewing the desktop. Add the guest's IP manually to
Moonlight and pair its PIN in Sunshine. Start the Desktop entry. Select
1920x1080, 60 FPS, H.264, and SDR in Moonlight and configure the guest display to
the same mode. Optionally set `streaming.capture_output` to the output identifier
listed by Sunshine. The server disables HEVC/AV1 in this initial profile. The VM
has an emulated ICH9 audio device, with playback available through the
SPICE connection. `nixvm-audio` PipeWire sink supplies Sunshine stream audio even
without a physical audio output; select it as the desktop's output if necessary.

`gpu-check` runs `nvidia-smi`, `vulkaninfo --summary`, and an actual one-second
NVENC encode. A failed NVENC initialization is a failed accelerated acceptance
check, not a reason to silently fall back. Explicitly set `streaming.encoder` to
`software` if you accept CPU encoding. Inspect `systemctl --user status sunshine`
and `journalctl --user -u sunshine` inside the graphical session for capture issues.

Sunshine streams a logged-in desktop; it does not provide an independent GNOME
remote-login session. Autologin, the GPU-connected display/dummy plug, KMS
permissions, and a successful reset after VM restart must all work. Display the
GNOME session on the passed-through GPU, not on an emulated recovery display.
