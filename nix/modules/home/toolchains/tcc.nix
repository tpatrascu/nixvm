{ lib, pkgs, machine, ... }:
{ home.packages = lib.optionals (machine.toolchains.tcc or false) [ pkgs.tinycc ]; }
