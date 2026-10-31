{ config, lib, pkgs, machine, ... }:
{
  config = lib.mkIf machine.gpu.enabled {
    hardware.graphics.enable = true;
    # Use the VM's virtio display for GNOME Wayland scanout while keeping
    # NVIDIA as a PRIME render-offload GPU for Vulkan applications.
    services.xserver.videoDrivers = [ "nvidia" ];
    hardware.nvidia = {
      modesetting.enable = true;
      open = machine.gpu.open_driver;
      package = config.boot.kernelPackages.nvidiaPackages.${machine.gpu.driver_package};
    };
    hardware.nvidia.prime = {
      offload.enable = true;
      offload.enableOffloadCmd = true;
      # The libvirt virtio display is at 00:01.0; the passed-through MX450 is
      # at 05:00.0 inside this guest.
      intelBusId = "PCI:0@0:1:0";
      nvidiaBusId = "PCI:0@0:5:0";
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
        origin_web_ui_allowed = "lan";
        csrf_allowed_origins = "https://${machine.hostname}:47990";
        hevc_mode = 1;
        av1_mode = 1;
        audio_sink = "nixvm-audio";
      } // lib.optionalAttrs (machine.streaming.capture_output != "") {
        output_name = machine.streaming.capture_output;
      };
    };
    hardware.uinput.enable = machine.streaming.enabled;
    users.users.${machine.username}.extraGroups = lib.optional machine.streaming.enabled "uinput";
    # Permit the configured host to reach any guest service over the private VM
    # network. This also works when the firewall starts before DHCP.
    networking.firewall.extraCommands = lib.mkIf machine.streaming.enabled ''
      iptables -A nixos-fw -s ${lib.escapeShellArg machine.streaming.host_address} -j nixos-fw-accept
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
