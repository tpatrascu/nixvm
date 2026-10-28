mock_provider "libvirt" {}

variables {
  ssh_public_keys = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyNotARealKey validation"]
}

run "default_vm" {
  command = plan
  assert {
    condition     = libvirt_domain.vm.memory == 8192 && libvirt_domain.vm.vcpu == 4
    error_message = "The default VM must have 8 GiB RAM and four vCPUs."
  }
  assert {
    condition     = length(libvirt_domain.vm.devices.hostdevs) == 0 && !output.guest_configuration.streaming.enabled
    error_message = "GPU/streaming must be inactive without an explicit GPU configuration."
  }
  assert {
    condition     = output.guest_configuration.repoPath == "/home/gigid/dev/nixvm"
    error_message = "The editable guest checkout must be on the guest disk."
  }
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
