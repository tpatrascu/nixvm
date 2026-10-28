{ lib, pkgs, machine, ... }:
{ home.packages = lib.optionals machine.toolchains.rust (with pkgs; [ rustc cargo rustfmt clippy rust-analyzer ]); }
