{ lib, machine, ... }:
{
  boot.supportedFilesystems = lib.optional (machine.nfsMounts != []) "nfs";
  fileSystems = builtins.listToAttrs (map (mount: {
    name = mount.mountpoint;
    value = {
      device = "${mount.server}:${mount.export}";
      fsType = "nfs4";
      options = mount.options ++ [
        "_netdev" "nofail" "x-systemd.automount" "x-systemd.idle-timeout=600"
        "x-systemd.mount-timeout=15s"
      ];
    };
  }) machine.nfsMounts);
}
