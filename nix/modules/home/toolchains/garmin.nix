{ lib, pkgs, machine, ... }:
{
  # Garmin's Connect IQ SDK Manager and SDK are downloaded from Garmin after
  # accepting its developer agreement. The Garmin toolkit provides its JDK.
  home.packages = lib.optionals (machine.toolchains.garmin or false) [ pkgs.jdk ];
}
