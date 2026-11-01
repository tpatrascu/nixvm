{ lib, machine, pkgs, ... }:
let
  editors = machine.apps.editors or { };
  terminals = machine.apps.terminals or { };
  browsers = machine.apps.browsers or { };
  firefoxEnabled = browsers.firefox or true;
  defaultEditor = machine.defaultEditor or "nvim";
  defaultTerminal = machine.defaultTerminal or "kitty";
  defaultBrowser = machine.defaultBrowser or "firefox";
  browserDesktop = if defaultBrowser == "chromium"
    then "nixvm-chromium.desktop" else "nixvm-firefox.desktop";
  terminalDesktop = if defaultTerminal == "ghostty"
    then "com.mitchellh.ghostty.desktop" else "kitty.desktop";
  editorDesktop = if defaultEditor == "zed" then [ "dev.zed.Zed.desktop" ]
    else if defaultEditor == "vscode" then [ "code.desktop" ] else [ ];
  mitmproxyEnabled =
    (machine.mitmproxy.enabled or false)
    && builtins.pathExists ../../machines/mitmproxy-ca.pem;
  proxyAddress = lib.removePrefix "http://" machine.mitmproxy.proxyUrl;
  proxyHost = lib.removeSuffix ":8080" proxyAddress;
in {
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

  programs.gnome-shell = {
    enable = true;
    extensions = map (package: { inherit package; }) [
      pkgs.gnomeExtensions.appindicator
      pkgs.gnomeExtensions.dash-to-panel
      pkgs.gnomeExtensions.repeat-to-cycle
      pkgs.gnomeExtensions.tiling-assistant
    ];
  };

  programs.firefox = lib.mkIf firefoxEnabled {
    nativeMessagingHosts = [ pkgs.gnome-browser-connector ];
    policies.Certificates.ImportEnterpriseRoots = true;
    policies.Proxy = lib.mkIf mitmproxyEnabled {
      Mode = "manual";
      Locked = true;
      HTTPProxy = lib.removePrefix "http://" machine.mitmproxy.proxyUrl;
      SSLProxy = lib.removePrefix "http://" machine.mitmproxy.proxyUrl;
      UseHTTPProxyForAllProtocols = true;
      Passthrough = "localhost,127.0.0.1,::1";
    };
    policies.ExtensionSettings = {
      "chrome-gnome-shell@gnome.org" = {
        installation_mode = "force_installed";
        install_url = "https://addons.mozilla.org/firefox/downloads/latest/gnome-shell-integration/latest.xpi";
      };
      "addon@darkreader.org" = {
        installation_mode = "force_installed";
        install_url = "https://addons.mozilla.org/firefox/downloads/latest/darkreader/latest.xpi";
      };
      "uBlock0@raymondhill.net" = {
        installation_mode = "force_installed";
        install_url = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi";
      };
      "simple-translate@sienori" = {
        installation_mode = "force_installed";
        install_url = "https://addons.mozilla.org/firefox/downloads/latest/simple-translate/latest.xpi";
      };
      "{252ee273-8c8d-4609-b54d-62ae345be0a1}" = {
        installation_mode = "force_installed";
        install_url = "https://addons.mozilla.org/firefox/downloads/latest/indicatetls/latest.xpi";
      };
      "gdpr@cavi.au.dk" = {
        installation_mode = "force_installed";
        install_url = "https://addons.mozilla.org/firefox/downloads/latest/consent-o-matic/latest.xpi";
      };
    };
  };

  dconf.settings = {
    "system/proxy" = lib.mkIf mitmproxyEnabled {
      mode = "manual";
      ignore-hosts = [ "localhost" "127.0.0.0/8" "::1" ];
      use-same-proxy = true;
    };
    "system/proxy/http" = lib.mkIf mitmproxyEnabled {
      host = proxyHost;
      port = 8080;
    };
    "system/proxy/https" = lib.mkIf mitmproxyEnabled {
      host = proxyHost;
      port = 8080;
    };
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
      favorite-apps = [ browserDesktop terminalDesktop ]
        ++ editorDesktop
        ++ [ "org.gnome.Nautilus.desktop" ];
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
      animate-appicon-hover-animation-extent = lib.hm.gvariant.mkArray "{si}" [
        (lib.hm.gvariant.mkDictionaryEntry [ "PLANK" 4 ])
        (lib.hm.gvariant.mkDictionaryEntry [ "RIPPLE" 4 ])
        (lib.hm.gvariant.mkDictionaryEntry [ "SIMPLE" 1 ])
      ];
      appicon-margin = 4;
      dot-position = "BOTTOM";
      group-apps = true;
      intellihide = false;
      intellihide-animation-time = 10;
      intellihide-close-delay = 50;
      intellihide-revealed-hover-limit-size = true;
      intellihide-use-pointer-limit-size = true;
      intellihide-use-pressure = true;
      hotkeys-overlay-combo = "TEMPORARILY";
      panel-element-positions-monitors-sync = true;
      show-tooltip = false;
      show-window-previews = false;
      window-preview-title-position = "TOP";
      panel-size = 32;
    };
  };
}
