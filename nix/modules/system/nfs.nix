{ lib, machine, ... }:
let
  hostMounts = map (export: {
    server = machine.hostAddress;
    export = export.host_path;
    mountpoint = export.guest_path;
    options = export.options;
  }) machine.hostNfsExports;
  mounts = machine.nfsMounts ++ hostMounts;
in
{
  boot.supportedFilesystems = lib.optional (mounts != []) "nfs";
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
  }) mounts);
}
