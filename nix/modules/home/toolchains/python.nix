{ lib, pkgs, machine, ... }:
{ home.packages = lib.optionals machine.toolchains.python [ pkgs.python3 pkgs.python3Packages.pip pkgs.uv ]; }
