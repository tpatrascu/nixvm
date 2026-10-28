{ config, lib, pkgs, machine, modulesPath, ... }:
{
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];
  system.stateVersion = "26.05";
  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  nix.settings.trusted-users = [ "root" "@wheel" ];
  nixpkgs.config.allowUnfree = true;
  networking.hostName = machine.hostname;
  networking.networkmanager.enable = true;
  networking.firewall.enable = true;
  time.timeZone = machine.timezone;
  i18n.defaultLocale = machine.locale;
  console.keyMap = machine.keyboard;
  services.xserver.xkb.layout = machine.keyboard;

  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = false;
  boot.initrd.availableKernelModules = [ "virtio_pci" "virtio_blk" "virtio_scsi" "xhci_pci" ];
  boot.kernelParams = [ "console=tty0" "console=ttyS0,115200" ];
  boot.growPartition = true;
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
    autoResize = true;
  };
  fileSystems."/boot" = { device = "/dev/disk/by-label/ESP"; fsType = "vfat"; };

  users.mutableUsers = true; # passwd changes survive rebuilds; no password in the store.
  users.groups.${machine.username}.gid = machine.gid;
  users.users.${machine.username} = {
    isNormalUser = true;
    uid = machine.uid;
    group = machine.username;
    home = "/home/${machine.username}";
    extraGroups = [ "wheel" "networkmanager" "video" "render" ];
    shell = pkgs.zsh;
    openssh.authorizedKeys.keys = machine.sshPublicKeys;
  };
  # Home Manager's first activation needs a writable Nix profile directory.
  # A fresh image has not created one with nix profile install yet.
  systemd.tmpfiles.rules = [
    "d /home/${machine.username}/.local 0755 ${machine.username} ${machine.username} -"
    "d /home/${machine.username}/.local/state 0755 ${machine.username} ${machine.username} -"
    "d /home/${machine.username}/.local/state/nix 0755 ${machine.username} ${machine.username} -"
    "d /home/${machine.username}/.local/state/nix/profiles 0700 ${machine.username} ${machine.username} -"
  ];
  programs.zsh.enable = true;
  security.sudo.wheelNeedsPassword = false;
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
      StreamLocalBindUnlink = true;
    };
  };
  services.qemuGuest.enable = true;
  environment.systemPackages = with pkgs; [ git rsync vim curl python3 ];
}
