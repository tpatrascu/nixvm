{
  description = "Reproducible libvirt development workstation";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    chatgpt-linux.url = "github:bruglet/chatgpt-desktop-linux";
  };
  outputs = { self, nixpkgs, home-manager, chatgpt-linux }:
    let
      system = "x86_64-linux";
      machine = builtins.fromJSON (builtins.readFile (
        if builtins.pathExists ./machines/current.json
        then ./machines/current.json else ./machines/example.json
      ));
      mkSystem = bootstrap: settings: nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = { machine = settings; };
        modules = [
          ./modules/system/base.nix
        ] ++ (if bootstrap then [ ./modules/system/image.nix ] else [
          ./modules/system/gnome.nix
          ./modules/system/nfs.nix
          ./modules/system/gpu.nix
          ./modules/system/yubikey.nix
          chatgpt-linux.nixosModules.default
          ({ machine, ... }: {
            programs.codexDesktopLinux.enable = machine.apps.chatgpt or false;
            environment.sessionVariables.CODEX_LINUX_DISABLE_USAGE_REPORTING = "1";
          })
          home-manager.nixosModules.home-manager
          {
            home-manager.useGlobalPkgs = true;
            home-manager.useUserPackages = true;
            home-manager.extraSpecialArgs = { machine = settings; };
            home-manager.users.${settings.username} = import ./modules/home/default.nix;
          }
        ]);
      };
      bootstrap = mkSystem true machine;
      workstation = mkSystem false machine;
      minimal = mkSystem false (machine // {
        toolchains = { python = false; node = false; llvm = false; rust = false; jdk = false; garmin = false; };
      });
      pkgs = import nixpkgs { inherit system; config.allowUnfree = true; };
    in {
      nixosConfigurations = { nixvm = workstation; bootstrap = bootstrap; minimal = minimal; };
      packages.${system} = {
        bootstrap = bootstrap.config.system.build.image;
        default = workstation.config.system.build.toplevel;
      };
      formatter.${system} = pkgs.nixfmt;
      checks.${system} = {
        workstation = workstation.config.system.build.toplevel;
        toolchains-disabled = minimal.config.system.build.toplevel;
      };
    };
}
