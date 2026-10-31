{ lib, pkgs, machine, ... }:
{
  home.packages = lib.optionals (machine.toolchains.jdk or false) [ pkgs.jdk ];
}
