{ config, lib, pkgs, machine, ... }:
let
  state = "\"$HOME/.local/state/nixvm\"";
  modeTools = pkgs.writeShellApplication {
    name = "nixvm-token-mode";
    runtimeInputs = [ pkgs.coreutils pkgs.gnupg pkgs.procps ];
    text = ''
      mode="''${1:-shared}"
      case "$mode" in shared|usb) ;; *) echo 'Use shared or usb' >&2; exit 2 ;; esac
      mkdir -p ${state} "$XDG_RUNTIME_DIR/nixvm"
      chmod 700 ${state} "$XDG_RUNTIME_DIR/nixvm"
      # Firefox must be restarted to reopen PKCS#11 with the new socket.
      if pgrep -u "$(id -u)" -x firefox >/dev/null; then
        echo 'Close Firefox before changing token mode.' >&2
        exit 1
      fi
      gpgconf --homedir "$HOME/.local/state/nixvm/gnupg-usb" --kill all 2>/dev/null || true
      printf '%s\n' "$mode" > ${state}/token-mode
      if [ "$mode" = shared ]; then
        rm -f "$XDG_RUNTIME_DIR/nixvm/pcsc.sock"
      fi
    '';
  };
  gpg = pkgs.writeShellApplication {
    name = "nixvm-gpg";
    runtimeInputs = [ pkgs.gnupg pkgs.coreutils ];
    text = ''
      mode=$(cat ${state}/token-mode 2>/dev/null || echo shared)
      if [ "$mode" = usb ]; then
        export PCSCLITE_CSOCK_NAME=/run/pcscd/pcscd.comm
        usb_gnupg_home=${state}/gnupg-usb
        mkdir -p "$usb_gnupg_home"
        chmod 700 "$usb_gnupg_home"
        printf '%s\n' disable-ccid pcsc-shared > "$usb_gnupg_home/scdaemon.conf"
        printf '%s\n' 'pinentry-program ${pkgs.pinentry-gnome3}/bin/pinentry-gnome3' > "$usb_gnupg_home/gpg-agent.conf"
        # Public keys only; no host private key material is transferred.
        ${pkgs.gnupg}/bin/gpg --no-autostart --export | ${pkgs.gnupg}/bin/gpg --homedir "$usb_gnupg_home" --import >/dev/null 2>&1
        # Learn on-card key stubs before the first signing request in this home.
        ${pkgs.gnupg}/bin/gpg --homedir "$usb_gnupg_home" --card-status >/dev/null
        exec ${pkgs.gnupg}/bin/gpg --homedir "$usb_gnupg_home" "$@"
      fi
      exec ${pkgs.gnupg}/bin/gpg --no-autostart "$@"
    '';
  };
  firefox = pkgs.writeShellApplication {
    name = "nixvm-firefox";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      mode=$(cat ${state}/token-mode 2>/dev/null || echo shared)
      if [ "$mode" = usb ]; then
        export PCSCLITE_CSOCK_NAME=/run/pcscd/pcscd.comm
      else
        export PCSCLITE_CSOCK_NAME="$XDG_RUNTIME_DIR/nixvm/pcsc.sock"
      fi
      exec ${config.programs.firefox.finalPackage}/bin/firefox "$@"
    '';
  };
  piv = pkgs.writeShellApplication {
    name = "nixvm-piv";
    runtimeInputs = [ pkgs.opensc pkgs.coreutils ];
    text = ''
      mode=$(cat ${state}/token-mode 2>/dev/null || echo shared)
      if [ "$mode" = usb ]; then
        export PCSCLITE_CSOCK_NAME=/run/pcscd/pcscd.comm
      else
        export PCSCLITE_CSOCK_NAME="$XDG_RUNTIME_DIR/nixvm/pcsc.sock"
      fi
      exec pkcs11-tool --module ${pkgs.opensc}/lib/opensc-pkcs11.so "$@"
    '';
  };
  pivSsh = pkgs.writeShellApplication {
    name = "nixvm-piv-ssh";
    runtimeInputs = [ pkgs.openssh pkgs.coreutils ];
    text = ''
      mode=$(cat ${state}/token-mode 2>/dev/null || echo shared)
      if [ "$mode" = usb ]; then
        export PCSCLITE_CSOCK_NAME=/run/pcscd/pcscd.comm
      else
        export PCSCLITE_CSOCK_NAME="$XDG_RUNTIME_DIR/nixvm/pcsc.sock"
      fi
      exec ssh -I ${pkgs.opensc}/lib/opensc-pkcs11.so "$@"
    '';
  };
in {
  programs.gpg = { enable = true; settings.no-autostart = true; };
  services.gpg-agent.enable = false;
  programs.git.settings.gpg.program = "nixvm-gpg";
  programs.firefox = {
    enable = true;
    policies.SecurityDevices.Add."OpenSC PIV" = "${pkgs.opensc}/lib/opensc-pkcs11.so";
    profiles.default = { id = 0; isDefault = true; };
  };
  xdg.desktopEntries.nixvm-firefox = {
    name = "Firefox (YubiKey mode)";
    genericName = "Web Browser";
    exec = "${firefox}/bin/nixvm-firefox %U";
    icon = "${config.programs.firefox.finalPackage}/share/icons/hicolor/128x128/apps/firefox.png";
    terminal = false;
    categories = [ "Network" "WebBrowser" ];
    mimeType = [ "text/html" "x-scheme-handler/http" "x-scheme-handler/https" ];
  };
  xdg.mimeApps = {
    enable = true;
    defaultApplications = {
      "text/html" = "nixvm-firefox.desktop";
      "x-scheme-handler/http" = "nixvm-firefox.desktop";
      "x-scheme-handler/https" = "nixvm-firefox.desktop";
    };
  };
  home.packages = [ modeTools gpg firefox piv pivSsh ];
  # Ensure systemd/D-Bus applications get the forwarding socket, not only shells.
  systemd.user.tmpfiles.rules = [ "d %t/nixvm 0700 - - -" ];
  systemd.user.sessionVariables.PCSCLITE_CSOCK_NAME = "/run/user/${toString machine.uid}/nixvm/pcsc.sock";
}
