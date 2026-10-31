{ lib, pkgs, machine, ... }:
let
  omp = pkgs.callPackage ../../packages/omp-binary.nix { };
in
{
  home.packages = [
    pkgs.zed-editor
    pkgs.kitty
    pkgs.adw-gtk3
    pkgs.vanilla-dmz
    pkgs.nerd-fonts._0xproto
    omp
  ]
    ++ lib.optionals (machine.apps.codex or false) [ pkgs.codex ]
    ++ lib.optionals (machine.apps.codexDesktop or false) [ pkgs.chatgpt ]
    ++ lib.optionals (machine.apps.claudeCode or false) [ pkgs.claude-code ]
    ++ lib.optionals (machine.apps.opencode or false) [ pkgs.opencode ]
    ++ lib.optionals (machine.apps.vscode or false) [ pkgs.vscode ];

  xdg.desktopEntries = {
    kitty = {
      name = "Kitty";
      genericName = "Terminal emulator";
      exec = "kitty";
      icon = "${pkgs.kitty}/share/icons/hicolor/scalable/apps/kitty.svg";
      terminal = false;
      categories = [ "System" "TerminalEmulator" ];
    };
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

  home.file.".config/kitty/kitty.conf".text = ''
    font_family 0xProto Nerd Font
    font_size 12.0
    confirm_os_window_close 0
    enable_audio_bell no
  '';
}
