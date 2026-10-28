{ lib, modulesPath, ... }:
{
  imports = [ (modulesPath + "/virtualisation/disk-image.nix") ];
  image = { format = "qcow2"; efiSupport = true; baseName = "nixvm-bootstrap"; };
  virtualisation.diskSize = 8192;
  # Image creation invokes QEMU; use TCG on builders without /dev/kvm.
}
