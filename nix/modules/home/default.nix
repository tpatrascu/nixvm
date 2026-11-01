{ config, lib, pkgs, machine, ... }:
let
  mitmproxyEnabled =
    (machine.mitmproxy.enabled or false)
    && builtins.pathExists ../../machines/mitmproxy-ca.pem;
  proxyUrl = machine.mitmproxy.proxyUrl;
  noProxy = "localhost,127.0.0.1,::1";
  caBundle = "/etc/ssl/certs/ca-certificates.crt";
  proxyEnvironment = {
    http_proxy = proxyUrl;
    https_proxy = proxyUrl;
    all_proxy = proxyUrl;
    no_proxy = noProxy;
    HTTP_PROXY = proxyUrl;
    HTTPS_PROXY = proxyUrl;
    ALL_PROXY = proxyUrl;
    NO_PROXY = noProxy;
  };
  certificateEnvironment = {
    SSL_CERT_FILE = caBundle;
    REQUESTS_CA_BUNDLE = caBundle;
    CURL_CA_BUNDLE = caBundle;
    NODE_EXTRA_CA_CERTS = caBundle;
    NIX_SSL_CERT_FILE = caBundle;
    GIT_SSL_CAINFO = caBundle;
    GRPC_DEFAULT_SSL_ROOTS_FILE_PATH = caBundle;
  };
in {
  imports = [
    ./gnome.nix ./apps.nix ./dotfiles.nix ./yubikey.nix
    ./toolchains/python.nix ./toolchains/node.nix
    ./toolchains/llvm.nix ./toolchains/rust.nix
    ./toolchains/jdk.nix ./toolchains/garmin.nix
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
  } // lib.optionalAttrs mitmproxyEnabled (proxyEnvironment // certificateEnvironment);
  systemd.user.sessionVariables = lib.mkIf mitmproxyEnabled
    (proxyEnvironment // certificateEnvironment);
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
