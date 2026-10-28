{ config, lib, machine, ... }:
{
  home.file = lib.mapAttrs (destination: source: {
    source = config.lib.file.mkOutOfStoreSymlink "${machine.dotfilesPath}/${source}";
  }) machine.dotfilesLinks;
}
