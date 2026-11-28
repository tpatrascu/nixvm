{ pkgs }:
let
  version = "2.36.0";
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
    pname = "antigravity-unpacked";
    inherit version;
    src = pkgs.fetchurl {
      name = "antigravity-${version}.tar.gz";
      url = "https://storage.googleapis.com/antigravity-public/antigravity-hub/2.36.0-5596661662220288/linux-x64/Antigravity.tar.gz";
      hash = "sha256-b+QY8I52DAt/KXXfna7262y8R8ZRYSUTSeDOJVxgj7Y=";
    };
    sourceRoot = "Antigravity-x64";
    dontBuild = true;
    dontFixup = true;
    installPhase = ''
      mkdir -p "$out"
      cp -a . "$out/"
    '';
    meta = {
      description = "Google Antigravity official Linux release";
      homepage = "https://antigravity.google/download/";
      license = pkgs.lib.licenses.unfree;
      platforms = [ "x86_64-linux" ];
    };
  };
  launcher = pkgs.writeShellScriptBin "antigravity" ''
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
      exec "$HOME/.local/share/antigravity/antigravity" "''${antigravity_args[@]}" "$@"
  '';
in
{
  inherit app launcher version;
}
