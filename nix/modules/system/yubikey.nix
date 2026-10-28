{ pkgs, machine, ... }:
{
  services.pcscd.enable = true; # Used by USB mode; shared clients use another socket.
  hardware.gpgSmartcards.enable = true;
  services.udev.packages = [ pkgs.yubikey-personalization pkgs.libu2f-host ];
  environment.systemPackages = with pkgs; [ opensc pcsclite pcsc-tools yubikey-manager gnupg libfido2 ];
  systemd.tmpfiles.rules = [
    "d /home/${machine.username}/.local/state/nixvm 0700 ${machine.username} ${machine.username} -"
    "d /home/${machine.username}/.gnupg 0700 ${machine.username} ${machine.username} -"
  ];
}
