mock_provider "libvirt" {}

variables {
  ssh_public_keys = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyNotARealKey validation"]
}

run "default_vm" {
  command = plan
  assert {
    condition     = libvirt_pool.vm.target.path == "/var/lib/libvirt/images/nixvm-user" && libvirt_pool.vm.target.permissions.owner == "1000" && libvirt_pool.vm.target.permissions.group == "64055" && libvirt_pool.vm.target.permissions.mode == "0770"
    error_message = "The dedicated pool must be user-owned and accessible to the host QEMU group."
  }
  assert {
    condition     = libvirt_volume.user_root.pool == "nixvm-user" && libvirt_volume.user_root.target.permissions.owner == "1000" && libvirt_volume.user_root.target.permissions.group == "64055" && libvirt_volume.user_root.target.permissions.mode == "0660" && libvirt_domain.vm.devices.disks[0].source.file.file == libvirt_volume.user_root.path
    error_message = "The VM disk must use the dedicated pool, be accessible to QEMU, and appear as a file path for AppArmor."
  }
  assert {
    condition     = libvirt_domain.vm.os.loader == "/usr/share/OVMF/OVMF_CODE_4M.fd" && libvirt_domain.vm.os.loader_type == "pflash" && libvirt_domain.vm.os.nv_ram.template == "/usr/share/OVMF/OVMF_VARS_4M.fd" && libvirt_domain.vm.os.firmware == null
    error_message = "Use explicit standard pflash firmware, not automatic SEV/TDX firmware selection."
  }
  assert {
    condition     = libvirt_domain.vm.memory == 8192 && libvirt_domain.vm.vcpu == 4
    error_message = "The default VM must have 8 GiB RAM and four vCPUs."
  }
  assert {
    condition     = libvirt_domain.vm.running && libvirt_domain.vm.devices.serials == null && libvirt_domain.vm.devices.consoles == null
    error_message = "Run the VM directly without PTY devices; SSH is its recovery path."
  }
  assert {
    condition     = length(libvirt_domain.vm.devices.hostdevs) == 0 && !output.guest_configuration.streaming.enabled
    error_message = "GPU/streaming must be inactive without an explicit GPU configuration."
  }
  assert {
    condition     = terraform_data.bootstrap.triggers_replace[0] == libvirt_domain.vm.uuid
    error_message = "VM start/stop cycles must not retrigger bootstrap; key it on the stable domain UUID."
  }
  assert {
    condition     = output.guest_configuration.repoPath == "/home/gigid/nixvm"
    error_message = "The editable guest checkout must be on the guest disk."
  }
  assert {
    condition     = output.connection.mac == "52:54:00:dc:10:01"
    error_message = "The existing nixvm profile must retain its current MAC address."
  }
}

run "tfvars_named_profile" {
  command = plan
  variables {
    name = "workvm"
  }
  assert {
    condition     = output.connection.name == "workvm" && output.guest_configuration.hostname == "workvm"
    error_message = "The selected VM name must become the VM name and guest hostname."
  }
  assert {
    condition     = output.guest_configuration.repoPath == "/home/gigid/workvm" && libvirt_volume.user_root.name == "workvm.qcow2"
    error_message = "The guest checkout and disk names must be scoped to the VM profile."
  }
  assert {
    condition     = output.vm_network.name == "nixvm-workvm-nat" && output.firewall_test_network.name == "workvm-firewall-test-subnet"
    error_message = "Both VM networks must be scoped to the VM profile."
  }
  assert {
    condition     = output.connection.mac != "52:54:00:dc:10:01" && startswith(output.connection.mac, "52:54:00:")
    error_message = "Named profiles must receive a stable profile-specific QEMU MAC address."
  }
}

run "multiple_ssh_keys" {
  command = plan
  variables {
    ssh_public_keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFirstTestKey test-one",
      "ecdsa-sha2-nistp384 AAAAE2VjZHNhLXNoYTItbmlzdHAzODQAAABhSecondTestKey test-two",
    ]
  }
  assert {
    condition     = length(output.guest_configuration.sshPublicKeys) == 2 && output.guest_configuration.sshPublicKeys[0] == var.ssh_public_keys[0] && output.guest_configuration.sshPublicKeys[1] == var.ssh_public_keys[1]
    error_message = "All supplied public keys must reach the NixOS guest configuration."
  }
}

run "reject_duplicate_ssh_keys" {
  command = plan
  variables {
    ssh_public_keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyNotARealKey validation",
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyNotARealKey validation",
    ]
  }
  expect_failures = [var.ssh_public_keys]
}

run "gpu_and_nfs" {
  command = plan
  variables {
    gpu = {
      enabled       = true
      pci_functions = ["0000:01:00.0", "0000:01:00.1"]
    }
    nfs_mounts = [{ server = "192.168.122.1", export = "/srv/dev", mountpoint = "/mnt/dev" }]
    toolchains = { python = false, node = false, llvm = false, rust = false }
  }
  assert {
    condition     = length(libvirt_domain.vm.devices.hostdevs) == 2 && libvirt_domain.vm.devices.hostdevs[0].subsys_pci.source.address.bus == 1
    error_message = "GPU functions must map to PCI hostdevs with hexadecimal addresses parsed correctly."
  }
  assert {
    condition     = output.guest_configuration.streaming.enabled && length(output.guest_configuration.nfsMounts) == 1 && !output.guest_configuration.toolchains.python
    error_message = "Guest settings must follow the typed OpenTofu inputs."
  }
}

run "reject_missing_gpu_addresses" {
  command = plan
  variables { gpu = { enabled = true } }
  expect_failures = [var.gpu]
}

run "reject_nfs_system_mount" {
  command = plan
  variables { nfs_mounts = [{ server = "host", export = "/srv/dev", mountpoint = "/home" }] }
  expect_failures = [var.nfs_mounts]
}
