{ lib, pkgs, machine, ... }:
let
  omp = pkgs.callPackage ../../packages/omp-binary.nix { };
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
    ++ lib.optionals (codingAgents.omp or false) [ omp ]
    ++ lib.optionals (editors.zed or false) [ pkgs.zed-editor ]
    ++ lib.optionals (codingAgents.codex or false) [ pkgs.codex ]
    ++ lib.optionals (codingAgents.claudeCode or false) [ pkgs.claude-code ]
    ++ lib.optionals (codingAgents.opencode or false) [ pkgs.opencode ]
    ++ lib.optionals (editors.vscode or false) [ pkgs.vscode ]
    ++ lib.optionals (browsers.chromium or false) [ pkgs.chromium ]
    ++ additionalNixPkgs;

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
