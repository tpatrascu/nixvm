{ lib, machine, ... }:
{
  dconf.settings = {
    "org/gnome/desktop/interface" = { color-scheme = "prefer-dark"; };
    "org/gnome/desktop/input-sources".sources = [ (lib.hm.gvariant.mkTuple [ "xkb" machine.keyboard ]) ];
    "org/gnome/desktop/session".idle-delay = lib.hm.gvariant.mkUint32 0;
    "org/gnome/settings-daemon/plugins/power" = {
      sleep-inactive-ac-type = "nothing";
      sleep-inactive-battery-type = "nothing";
    };
    "org/gnome/desktop/screensaver".lock-enabled = false;
  };
}
