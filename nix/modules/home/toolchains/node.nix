{ lib, pkgs, machine, ... }:
# Node.js includes npm and Corepack; adding pkgs.corepack separately collides
# with the corepack executable already shipped in the Node.js output.
{ home.packages = lib.optionals machine.toolchains.node [ pkgs.nodejs ]; }
