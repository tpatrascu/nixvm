{ lib, pkgs, machine, ... }:
let
  omp = pkgs.callPackage ../../packages/omp-binary.nix { };
  antigravity = import ../../packages/antigravity.nix { inherit pkgs; };
  editors = machine.apps.editors or { };
  terminals = machine.apps.terminals or { };
  browsers = machine.apps.browsers or { };
  codingAgents = machine.apps.coding_agents or { };
  additionalNixPkgs = map (name:
    let
      package = lib.attrByPath (lib.splitString "." name) null pkgs;
    in
    if package == null || !(lib.isDerivation package) then
      throw "additional_nix_pkgs entry '${name}' does not resolve to a Nixpkgs derivation"
    else
      package
  ) (machine.additionalNixPkgs or [ ]);
in
{
  home.packages = [
    pkgs.adw-gtk3
    pkgs.vanilla-dmz
    pkgs.nerd-fonts._0xproto
  ]
    ++ lib.optionals (terminals.kitty or false) [ pkgs.kitty ]
    ++ lib.optionals (terminals.ghostty or false) [ pkgs.ghostty ]
    ++ lib.optionals (editors.zed or false) [ pkgs.zed-editor ]
    ++ lib.optionals (codingAgents.codex or false) [ pkgs.codex ]
    ++ lib.optionals (codingAgents.claudeCode or false) [ pkgs.claude-code ]
    ++ lib.optionals (codingAgents.opencode or false) [ pkgs.opencode ]
    ++ lib.optionals (editors.vscode or false) [ pkgs.vscode ]
    ++ lib.optionals (codingAgents.antigravity or false) [ antigravity.launcher ]
    ++ lib.optionals (browsers.chromium or false) [ pkgs.chromium ]
    ++ additionalNixPkgs;

  # Keep OMP at a user-writable path so its built-in `omp update` can replace
  # the standalone binary. Nix seeds it once; later Home Manager activations
  # preserve self-updates.
  home.activation = lib.optionalAttrs (codingAgents.omp or false) {
    installOmp = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      omp_path="$HOME/.local/bin/omp"
      if [ -L "$omp_path" ]; then
        resolved_path="$(readlink -f "$omp_path" || true)"
        case "$resolved_path" in
          /nix/store/*) rm -f "$omp_path" ;;
        esac
      fi
      if [ ! -e "$omp_path" ]; then
        install -d -m 0755 "$HOME/.local/bin"
        install -m 0755 ${lib.escapeShellArg (toString omp)} "$omp_path"
      fi
    '';
  } // lib.optionalAttrs (codingAgents.antigravity or false) {
    installAntigravity = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      antigravity_install_path="$HOME/.local/share/antigravity"
      if [ ! -x "$antigravity_install_path/antigravity" ]; then
        install -d -m 0755 "$HOME/.local/share"
        antigravity_stage="$(mktemp -d "$HOME/.local/share/.antigravity.XXXXXX")"
        trap 'rm -rf "$antigravity_stage"' EXIT
        cp -R --no-preserve=ownership ${lib.escapeShellArg (toString antigravity.app)}/. "$antigravity_stage/"
        chmod -R u+w "$antigravity_stage"
        if [ -e "$antigravity_install_path" ]; then
          mv "$antigravity_install_path" "$antigravity_install_path.broken-$(date +%s)"
        fi
        mv "$antigravity_stage" "$antigravity_install_path"
        trap - EXIT
      fi
    '';
  };

  xdg.desktopEntries = lib.optionalAttrs (terminals.kitty or false) {
    kitty = {
      name = "Kitty";
      genericName = "Terminal emulator";
      exec = "kitty";
      icon = "${pkgs.kitty}/share/icons/hicolor/scalable/apps/kitty.svg";
      terminal = false;
      categories = [ "System" "TerminalEmulator" ];
    };
  } // lib.optionalAttrs (editors.zed or false) {
    "dev.zed.Zed" = {
      name = "Zed";
      genericName = "Text Editor";
      exec = "zeditor %U";
      icon = "${pkgs.zed-editor}/share/icons/hicolor/512x512/apps/zed.png";
      terminal = false;
      categories = [ "Utility" "TextEditor" "Development" "IDE" ];
      mimeType = [ "text/plain" "application/x-zerosize" "x-scheme-handler/zed" ];
    };
  } // lib.optionalAttrs (codingAgents.antigravity or false) {
    antigravity = {
      name = "Antigravity";
      genericName = "AI-powered development environment";
      exec = "antigravity %F";
      icon = "antigravity";
      terminal = false;
      categories = [ "Utility" "TextEditor" "Development" "IDE" ];
      mimeType = [ "text/plain" "application/x-zerosize" ];
    };
  };

  home.file = lib.optionalAttrs (terminals.kitty or false) {
    ".config/kitty/kitty.conf".text = ''
      font_family 0xProto Nerd Font
      font_size 12.0
      confirm_os_window_close 0
      enable_audio_bell no
    '';
  };
}
