{ config, lib, pkgs, machine, ... }:
{
  imports = [
    ./gnome.nix ./apps.nix ./dotfiles.nix ./yubikey.nix
    ./toolchains/python.nix ./toolchains/node.nix
    ./toolchains/llvm.nix ./toolchains/rust.nix
  ];
  home.username = machine.username;
  home.homeDirectory = "/home/${machine.username}";
  home.stateVersion = "26.05";
  programs.home-manager.enable = true;
  programs.git.enable = true;
  programs.direnv = { enable = true; nix-direnv.enable = true; };
  home.packages = [ pkgs.ripgrep pkgs.fd pkgs.jq pkgs.gnumake pkgs.kubectl ];
  home.sessionVariables = {
    EDITOR = "zed --wait";
    PKCS11_MODULE = "${pkgs.opensc}/lib/opensc-pkcs11.so";
    PCSCLITE_CSOCK_NAME = "/run/user/${toString machine.uid}/nixvm/pcsc.sock";
    NIXVM_REPOSITORY = machine.repoPath;
  };
  programs.zsh = {
    enable = true;
    oh-my-zsh = { enable = true; plugins = [ "git" "kubectl" ]; theme = ""; };
    initContent = ''
      export NIXVM_ZSH_MANAGED=1
      source ${pkgs.zsh-powerlevel10k}/share/zsh-powerlevel10k/powerlevel10k.zsh-theme
      [[ ! -r ${lib.escapeShellArg (machine.dotfilesPath + "/.zshrc")} ]] || source ${lib.escapeShellArg (machine.dotfilesPath + "/.zshrc")}
      export PKCS11_MODULE=${pkgs.opensc}/lib/opensc-pkcs11.so
    '';
  };
  home.file.".local/bin/nixvm-rebuild" = {
    executable = true;
    text = ''
      #!${pkgs.runtimeShell}
      set -eu
      exec sudo nixos-rebuild "''${1:-switch}" --flake ${lib.escapeShellArg ("path:" + machine.repoPath + "/nix#nixvm")}
    '';
  };
  home.sessionPath = [ "$HOME/.local/bin" "${machine.dotfilesPath}/scripts" ];
}
