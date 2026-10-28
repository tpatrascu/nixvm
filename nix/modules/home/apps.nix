{ pkgs, ... }:
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
  ];

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

  home.file.".config/zed/settings.json".text = builtins.toJSON {
    theme = "One Dark";
    buffer_font_family = "0xProto Nerd Font";
    buffer_font_size = 14;
    ui_font_family = "0xProto Nerd Font";
    ui_font_size = 14;
    terminal = { font_family = "0xProto Nerd Font"; };
  };
}
