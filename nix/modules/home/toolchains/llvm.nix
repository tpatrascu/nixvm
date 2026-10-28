{ lib, pkgs, machine, ... }:
{ home.packages = lib.optionals machine.toolchains.llvm (with pkgs; [ clang clang-tools lldb cmake ninja pkg-config ]); }
