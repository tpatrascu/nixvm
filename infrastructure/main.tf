locals {
  root      = abspath("${path.module}/..")
  build_dir = "${local.root}/.build/${var.name}"
  guest_config = merge(var.guest, {
    sshPublicKeys = var.ssh_public_keys
    repoPath      = "/home/${var.guest.username}/dev/nixvm"
    dotfilesPath  = "/home/${var.guest.username}/.dotfiles"
    dotfilesLinks = var.dotfiles_links
    toolchains    = var.toolchains
    nfsMounts     = var.nfs_mounts
    gpu           = var.gpu
    streaming     = merge(var.streaming, { enabled = var.gpu.enabled && var.streaming.enabled })
  })
  connection = {
    name         = var.name
    uri          = var.libvirt_uri
    mac          = var.mac_address
    network      = var.network_name
    username     = var.guest.username
    uid          = var.guest.uid
    identity     = pathexpand(var.ssh_identity_file)
    guest_config = local.guest_config
  }
}

resource "terraform_data" "preflight" {
  triggers_replace = [var.libvirt_uri, var.network_name, jsonencode(var.gpu)]
  provisioner "local-exec" {
    command = "python3 \"$NIXVM_ROOT/scripts/nixvm.py\" preflight"
    environment = {
      NIXVM_ROOT       = local.root
      NIXVM_CONNECTION = jsonencode(local.connection)
    }
  }
}

# Creation-only image. Editing the host configuration never refreshes a live disk.
resource "terraform_data" "image" {
  triggers_replace = [var.name]
  depends_on       = [terraform_data.preflight]
  provisioner "local-exec" {
    command = "python3 \"$NIXVM_ROOT/scripts/nixvm.py\" build-image"
    environment = {
      NIXVM_ROOT       = local.root
      NIXVM_BUILD      = local.build_dir
      NIXVM_CONFIG     = jsonencode(local.guest_config)
      NIXVM_CONNECTION = jsonencode(local.connection)
      NIXVM_REPOSITORY = abspath(pathexpand(var.repository_path))
      NIXVM_DOTFILES   = abspath(pathexpand(var.dotfiles_path))
      NIXVM_DISK_GIB   = tostring(var.disk_gib)
    }
  }
}

resource "libvirt_volume" "root" {
  name       = "${var.name}.qcow2"
  pool       = var.storage_pool
  capacity   = var.disk_gib * 1024 * 1024 * 1024
  allocation = 0
  target     = { format = { type = "qcow2" } }
  create     = { content = { url = "${local.build_dir}/bootstrap.qcow2" } }
  depends_on = [terraform_data.image]
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [create]
  }
}

resource "libvirt_domain" "vm" {
  depends_on  = [terraform_data.preflight]
  name        = var.name
  type        = "kvm"
  memory      = var.memory_mib
  memory_unit = "MiB"
  vcpu        = var.vcpu
  running     = true
  autostart   = false
  cpu         = { mode = "host-passthrough" }
  features    = { acpi = true, apic = {} }
  os = {
    type          = "hvm"
    type_arch     = "x86_64"
    type_machine  = "q35"
    firmware      = "efi"
    firmware_info = { features = [{ name = "secure-boot", enabled = "no" }] }
    boot_devices  = [{ dev = "hd" }]
  }
  devices = {
    disks = [{
      device = "disk"
      driver = { name = "qemu", type = "qcow2", cache = "none" }
      source = { volume = { pool = var.storage_pool, volume = libvirt_volume.root.name } }
      target = { dev = "vda", bus = "virtio" }
    }]
    interfaces = [{
      mac    = { address = var.mac_address }
      model  = { type = "virtio" }
      source = { network = { network = var.network_name } }
    }]
    channels = concat([
      { source = { unix = {} }, target = { virt_io = { name = "org.qemu.guest_agent.0" } } }
      ], var.gpu.enabled ? [] : [
      { source = { spice_vmc = true }, target = { virt_io = { name = "com.redhat.spice.0" } } }
    ])
    serials     = [{ source = { pty = { path = "" } }, target = { port = 0 } }]
    consoles    = [{ source = { pty = { path = "" } }, target = { type = "serial", port = 0 } }]
    controllers = [{ type = "usb", model = "qemu-xhci" }]
    videos      = [{ model = { type = var.gpu.enabled ? "none" : "virtio" } }]
    graphics    = var.gpu.enabled ? [] : [{ spice = { auto_port = true, listen = "127.0.0.1" } }]
    hostdevs = var.gpu.enabled ? [for index, pci in var.gpu.pci_functions : {
      managed = true
      subsys_pci = {
        source = { address = {
          domain   = parseint(substr(pci, 0, 4), 16)
          bus      = parseint(substr(pci, 5, 2), 16)
          slot     = parseint(substr(pci, 8, 2), 16)
          function = parseint(substr(pci, 11, 1), 16)
        } }
      }
      rom = index == 0 && var.gpu.rom_file != null ? { file = var.gpu.rom_file } : null
    }] : []
  }
}

resource "terraform_data" "bootstrap" {
  triggers_replace = [libvirt_domain.vm.id, libvirt_volume.root.id]
  provisioner "local-exec" {
    command = "python3 \"$NIXVM_ROOT/scripts/nixvm.py\" bootstrap"
    environment = {
      NIXVM_ROOT       = local.root
      NIXVM_CONNECTION = jsonencode(local.connection)
      NIXVM_REPOSITORY = abspath(pathexpand(var.repository_path))
      NIXVM_DOTFILES   = abspath(pathexpand(var.dotfiles_path))
    }
  }
}
