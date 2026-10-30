{ pkgs, machine, ... }:
{
  systemd.defaultUnit = "graphical.target";
  services.xserver.enable = true;
  services.displayManager.gdm.enable = true;
  services.desktopManager.gnome.enable = true;
  services.displayManager.autoLogin = {
    enable = machine.streaming.autologin;
    user = machine.username;
  };
  services.spice-vdagentd.enable = true;
  security.rtkit.enable = true;
  services.pipewire = { enable = true; alsa.enable = true; pulse.enable = true; };
  environment.systemPackages = with pkgs; [ gnome-tweaks ];
}
