{ pkgs }:
let
  version = "2.8.0";
  runtimeLibraries = with pkgs; [
    stdenv.cc.cc.lib
    curl
    icu
    libunwind
    libuuid
    lttng-ust
    openssl
    zlib
    krb5
    glib
    gtk3
    nspr
    nss
    dbus
    at-spi2-atk
    cups
    expat
    libxkbcommon
    libx11
    libxcomposite
    libxdamage
    libxcb
    libxext
    libxfixes
    libxrandr
    cairo
    pango
    alsa-lib
    libgbm
    libglvnd
    udev
    libudev0-shim
    fontconfig
    libsecret
    libdbusmenu
    systemd
    wayland
  ];
  app = pkgs.stdenvNoCC.mkDerivation {
    pname = "antigravity-ide-unpacked";
    inherit version;
    src = pkgs.fetchurl {
      name = "antigravity-ide-${version}.tar.gz";
      url = "https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/2.8.0-6733682917113856/linux-x64/Antigravity%20IDE.tar.gz";
      hash = "sha256-e6o2uqsoXLRhAa+Vi4975jBUWNuaRNiz0JONYfOFl9c=";
    };
    sourceRoot = "Antigravity IDE";
    dontBuild = true;
    dontFixup = true;
    installPhase = ''
      mkdir -p "$out"
      cp -a . "$out/"
    '';
    meta = {
      description = "Google Antigravity IDE official Linux release";
      homepage = "https://antigravity.google/download/";
      license = pkgs.lib.licenses.unfree;
      platforms = [ "x86_64-linux" ];
    };
  };
  launcher = pkgs.writeShellScriptBin "antigravity-ide" ''
      # Keep Chromium's renderer sandbox enabled using NixOS's root-owned
      # helper. The IDE and its renderer processes still run as the desktop user.
      export CHROME_DEVEL_SANDBOX=/run/wrappers/bin/antigravity-chrome-sandbox
      export NIX_LD_LIBRARY_PATH=${pkgs.lib.makeLibraryPath runtimeLibraries}:''${NIX_LD_LIBRARY_PATH:-}
      antigravity_args=()
      if [ -n "''${NIXOS_OZONE_WL:-}" ] && [ -n "''${WAYLAND_DISPLAY:-}" ]; then
        antigravity_args+=(
          --ozone-platform-hint=auto
          --enable-features=WaylandWindowDecorations
          --enable-wayland-ime=true
          --wayland-text-input-version=3
        )
      fi
      exec "$HOME/.local/share/antigravity-ide/bin/antigravity-ide" "''${antigravity_args[@]}" "$@"
  '';
in
{
  inherit app launcher version;
}
