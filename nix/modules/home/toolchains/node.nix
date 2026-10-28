{ lib, pkgs, machine, ... }:
{ home.packages = lib.optionals machine.toolchains.node [ pkgs.nodejs pkgs.corepack ]; }
