{ lib, machine, pkgs, ... }:
{
  # GNOME 50 no longer starts spice-vdagent's legacy XDG autostart entry.
  # Start its Xwayland session agent from the graphical user session instead.
  systemd.user.services.spice-vdagent = {
    Unit = {
      Description = "SPICE guest session agent";
      PartOf = [ "graphical-session.target" ];
      After = [ "graphical-session.target" ];
    };
    Service = {
      ExecStart = "${pkgs.spice-vdagent}/bin/spice-vdagent --foreground";
      Restart = "on-failure";
      RestartSec = 2;
    };
    Install.WantedBy = [ "graphical-session.target" ];
  };

  programs.gnome-shell.extensions = map (package: { inherit package; }) [
    pkgs.gnomeExtensions.appindicator
    pkgs.gnomeExtensions.battery-health-charging
    pkgs.gnomeExtensions.caffeine
    pkgs.gnomeExtensions.dash-to-panel
    pkgs.gnomeExtensions.repeat-to-cycle
    pkgs.gnomeExtensions.tiling-assistant
  ];

  dconf.settings = {
    "org/gnome/desktop/interface" = {
      color-scheme = "prefer-dark";
      enable-animations = true;
      text-scaling-factor = 1.22;
      gtk-enable-primary-paste = false;
      gtk-theme = "adw-gtk3-dark";
      icon-theme = "Adwaita";
      cursor-theme = "DMZ-White";
    };
    "org/gnome/desktop/input-sources" = {
      sources = [ (lib.hm.gvariant.mkTuple [ "xkb" machine.keyboard ]) ];
      xkb-options = [ "caps:swapescape" ];
    };
    "org/gnome/desktop/session".idle-delay = lib.hm.gvariant.mkUint32 0;
    "org/gnome/shell" = {
      enabled-extensions = [
        "appindicatorsupport@rgcjonas.gmail.com"
        "Battery-Health-Charging@maniacx.github.com"
        "caffeine@patapon.info"
        "dash-to-panel@jderose9.github.com"
        "repeat-to-cycle@alternateved.com"
        "tiling-assistant@leleat-on-github"
      ];
      favorite-apps = [
        "nixvm-firefox.desktop"
        "kitty.desktop"
        "dev.zed.Zed.desktop"
        "org.gnome.Nautilus.desktop"
      ];
    };
    "org/gnome/desktop/peripherals/mouse".speed = -0.5726495726495726;
    "org/gnome/desktop/peripherals/touchpad" = {
      send-events = "enabled";
      speed = 0.0;
      two-finger-scrolling-enabled = true;
    };
    "org/gnome/desktop/privacy".report-technical-problems = false;
    "org/gnome/desktop/wm/keybindings" = {
      "switch-to-workspace-down" = [ ];
      "switch-to-workspace-left" = [ ];
      "switch-to-workspace-right" = [ ];
      "switch-to-workspace-up" = [ ];
    };
    "org/gnome/mutter".edge-tiling = false;
    "org/gnome/mutter/keybindings" = {
      "toggle-tiled-left" = [ ];
      "toggle-tiled-right" = [ ];
    };
    "org/gnome/settings-daemon/plugins/color" = {
      night-light-enabled = true;
      night-light-schedule-automatic = false;
      night-light-schedule-from = 6.0;
      night-light-temperature = lib.hm.gvariant.mkUint32 3022;
    };
    "org/gnome/settings-daemon/plugins/power" = {
      sleep-inactive-ac-type = "nothing";
      sleep-inactive-battery-type = "nothing";
    };
    "org/gnome/desktop/screensaver".lock-enabled = false;
    "org/gnome/shell/extensions/dash-to-panel" = {
      appicon-margin = 4;
      dot-position = "BOTTOM";
      group-apps = true;
      intellihide = true;
      panel-size = 32;
    };
  };
}
