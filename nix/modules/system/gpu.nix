{ config, lib, pkgs, machine, ... }:
{
  config = lib.mkIf machine.gpu.enabled {
    hardware.graphics.enable = true;
    services.xserver.videoDrivers = [ "nvidia" ];
    hardware.nvidia = {
      modesetting.enable = true;
      open = machine.gpu.open_driver;
      package = config.boot.kernelPackages.nvidiaPackages.${machine.gpu.driver_package};
    };
    environment.systemPackages = with pkgs; [ vulkan-tools mesa-demos pciutils
      (writeShellApplication {
        name = "nixvm-gpu-check";
        runtimeInputs = [ (ffmpeg.override { withNvenc = true; }) coreutils ];
        text = ''
          /run/current-system/sw/bin/nvidia-smi
          ${vulkan-tools}/bin/vulkaninfo --summary
          ${lib.optionalString (machine.streaming.encoder == "nvenc") ''
            echo 'Testing actual NVENC initialization (not just encoder listing)...'
            ffmpeg -hide_banner -loglevel error -f lavfi -i color=size=1920x1080:rate=60 \
              -t 1 -c:v h264_nvenc -f null -
          ''}
        '';
      })
    ];
    services.sunshine = lib.mkIf machine.streaming.enabled {
      enable = true;
      autoStart = true;
      capSysAdmin = true;
      openFirewall = false;
      package = pkgs.sunshine.override { cudaSupport = true; };
      applications.apps = [{ name = "Desktop"; }];
      settings = {
        capture = "kms";
        encoder = machine.streaming.encoder;
        upnp = "disabled";
        origin_web_ui_allowed = "pc";
        hevc_mode = 1;
        av1_mode = 1;
        audio_sink = "nixvm-audio";
      } // lib.optionalAttrs (machine.streaming.capture_output != "") {
        output_name = machine.streaming.capture_output;
      };
    };
    hardware.uinput.enable = machine.streaming.enabled;
    users.users.${machine.username}.extraGroups = lib.optional machine.streaming.enabled "uinput";
    # The configured host IP also works when the firewall starts before DHCP.
    networking.firewall.extraCommands = lib.mkIf machine.streaming.enabled ''
      iptables -A nixos-fw -s ${lib.escapeShellArg machine.streaming.host_address} -p tcp -m multiport --dports 47984,47989,48010 -j nixos-fw-accept
      iptables -A nixos-fw -s ${lib.escapeShellArg machine.streaming.host_address} -p udp -m multiport --dports 47998:48000,48002,48010 -j nixos-fw-accept
    '';
    # A silent audio sink works even when the GPU has no HDMI audio function.
    services.pipewire.extraConfig.pipewire."90-nixvm-audio" = {
      "context.objects" = [{
        factory = "adapter";
        args = {
          "factory.name" = "support.null-audio-sink";
          "node.name" = "nixvm-audio";
          "node.description" = "NixVM audio";
          "media.class" = "Audio/Sink";
          "audio.position" = [ "FL" "FR" ];
        };
      }];
    };
  };
}
