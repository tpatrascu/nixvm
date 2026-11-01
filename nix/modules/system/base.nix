{ config, lib, pkgs, machine, modulesPath, ... }:
let
  apps = machine.apps or { };
  browsers = apps.browsers or { };
  chromiumEnabled = (browsers.chromium or false)
    || (machine.defaultBrowser or "firefox") == "chromium";
  mitmproxyEnabled =
    (machine.mitmproxy.enabled or false)
    && builtins.pathExists ../../machines/mitmproxy-ca.pem;
  proxyUrl = machine.mitmproxy.proxyUrl;
  noProxy = "localhost,127.0.0.1,::1";
  caBundle = config.security.pki.caBundle;
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
  systemProxyEnvironment = proxyEnvironment // certificateEnvironment;
in {
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];
  system.stateVersion = "26.05";
  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  nix.settings.trusted-users = [ "root" "@wheel" ];
  nixpkgs.config.allowUnfree = true;
  networking.hostName = machine.hostname;
  environment.etc = lib.mkIf chromiumEnabled {
    "chromium/policies/managed/nixvm-search.json".text = builtins.toJSON {
      DefaultSearchProviderEnabled = true;
      DefaultSearchProviderName = "DuckDuckGo";
      DefaultSearchProviderKeyword = "duckduckgo.com";
      DefaultSearchProviderSearchURL = "https://duckduckgo.com/?q={searchTerms}";
    };
  };
  networking.networkmanager.enable = true;
  networking.proxy = lib.mkIf mitmproxyEnabled {
    httpProxy = proxyUrl;
    httpsProxy = proxyUrl;
    allProxy = proxyUrl;
    inherit noProxy;
  };
  environment.sessionVariables = lib.mkIf mitmproxyEnabled
    (proxyEnvironment // certificateEnvironment);
  # systemd services do not inherit a user's login environment. Set the same
  # defaults on PID 1 so system services and timers use the managed proxy too.
  systemd.settings.Manager.DefaultEnvironment = lib.mkIf mitmproxyEnabled
    (lib.concatStringsSep " " (lib.mapAttrsToList
      (name: value: "${name}=${value}") systemProxyEnvironment));
  nix.settings."ssl-cert-file" = lib.mkIf mitmproxyEnabled caBundle;
  security.pki.certificateFiles = lib.optionals mitmproxyEnabled
    [ ../../machines/mitmproxy-ca.pem ];
  networking.firewall.enable = true;
  time.timeZone = machine.timezone;
  i18n.defaultLocale = machine.locale;
  console.keyMap = machine.keyboard;
  services.xserver.xkb.layout = machine.keyboard;

  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = false;
  boot.initrd.availableKernelModules = [ "virtio_pci" "virtio_blk" "virtio_scsi" "xhci_pci" ];
  boot.kernelParams = [ "console=tty0" "console=ttyS0,115200" ];
  boot.growPartition = true;
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
    autoResize = true;
  };
  fileSystems."/boot" = { device = "/dev/disk/by-label/ESP"; fsType = "vfat"; };

  users.mutableUsers = true; # passwd changes survive rebuilds; no password in the store.
  users.groups.${machine.username}.gid = machine.gid;
  users.users.${machine.username} = {
    isNormalUser = true;
    uid = machine.uid;
    group = machine.username;
    home = "/home/${machine.username}";
    extraGroups = [ "wheel" "networkmanager" "video" "render" ];
    shell = if (machine.defaultShell or "zsh") == "zsh" then pkgs.zsh else pkgs.bashInteractive;
    openssh.authorizedKeys.keys = machine.sshPublicKeys;
  };
  # Home Manager's first activation needs a writable Nix profile directory.
  # A fresh image has not created one with nix profile install yet.
  systemd.tmpfiles.rules = [
    "d /home/${machine.username}/.local 0755 ${machine.username} ${machine.username} -"
    "d /home/${machine.username}/.local/state 0755 ${machine.username} ${machine.username} -"
    "d /home/${machine.username}/.local/state/nix 0755 ${machine.username} ${machine.username} -"
    "d /home/${machine.username}/.local/state/nix/profiles 0700 ${machine.username} ${machine.username} -"
  ];
  programs.zsh.enable = (machine.defaultShell or "zsh") == "zsh";
  security.sudo.wheelNeedsPassword = false;
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
      StreamLocalBindUnlink = true;
    };
  };
  services.qemuGuest.enable = true;
  environment.systemPackages = with pkgs; [
    git rsync vim neovim curl
    htop atop btop sysstat iotop iptraf-ng nettools
  ];
}
