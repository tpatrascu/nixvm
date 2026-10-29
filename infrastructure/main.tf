locals {
  root      = abspath("${path.module}/..")
  build_dir = "${local.root}/.build/${var.name}"
  guest_config = merge(var.guest, {
    sshPublicKeys  = var.ssh_public_keys
    repoPath       = "/home/${var.guest.username}/dev/nixvm"
    dotfilesPath   = "/home/${var.guest.username}/.dotfiles"
    dotfilesLinks  = var.dotfiles_links
    toolchains     = var.toolchains
    nfsMounts      = var.nfs_mounts
    hostNfsExports = var.host_nfs_exports
    hostAddress    = var.streaming.host_address
    gpu            = var.gpu
    streaming      = merge(var.streaming, { enabled = var.gpu.enabled && var.streaming.enabled })
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
  host_nfs_config = {
    name    = var.name
    uri     = var.libvirt_uri
    network = var.network_name
    exports = var.host_nfs_exports
  }
  vm_pool_name        = coalesce(var.vm_storage_pool_name, "${var.name}-user")
  vm_pool_path        = coalesce(var.vm_storage_pool_path, "/var/lib/libvirt/images/${local.vm_pool_name}")
  network_filter_name = "nixvm-${var.name}-access"
  network_filter_access = {
    host = {
      ips = distinct(concat(var.host_access.ips,
      length(var.host_nfs_exports) > 0 ? [var.streaming.host_address] : []))
      ports = var.host_access.all_ports ? [] : distinct(concat(var.host_access.ports,
      length(var.host_nfs_exports) > 0 ? [2049] : []))
      all_ports = var.host_access.all_ports
    }
    internal = {
      deny_cidrs = var.internal_network_deny
      allow      = var.internal_network_allow
    }
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

# libvirt has no nwfilter resource in the pinned provider. Define the filter on
# the host with virsh; the domain attaches to this host-enforced policy below.
resource "terraform_data" "network_filter" {
  triggers_replace = [
    local.network_filter_name,
    var.libvirt_uri,
    filesha256("${local.root}/scripts/nixvm.py"),
    jsonencode(local.network_filter_access)
  ]
  depends_on = [terraform_data.preflight]
  provisioner "local-exec" {
    command = "python3 \"$NIXVM_ROOT/scripts/nixvm.py\" define-network-filter"
    environment = {
      NIXVM_ROOT           = local.root
      NIXVM_CONNECTION     = jsonencode(local.connection)
      NIXVM_FILTER_NAME    = local.network_filter_name
      NIXVM_NETWORK_ACCESS = jsonencode(local.network_filter_access)
    }
  }
}

# Host exports are prepared interactively with scripts/nixvm.py setup-host-nfs.
# This check keeps apply from activating guest mounts before the host export exists.
resource "terraform_data" "host_nfs" {
  triggers_replace = [jsonencode(local.host_nfs_config), filesha256("${local.root}/scripts/nixvm.py")]
  depends_on       = [terraform_data.preflight]
  provisioner "local-exec" {
    command = "python3 \"$NIXVM_ROOT/scripts/nixvm.py\" check-host-nfs"
    environment = {
      NIXVM_ROOT            = local.root
      NIXVM_HOST_NFS_CONFIG = jsonencode(local.host_nfs_config)
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

resource "libvirt_pool" "vm" {
  name = local.vm_pool_name
  type = "dir"
  target = {
    path = local.vm_pool_path
    # The Terraform user owns the directory, while the QEMU runtime group can
    # traverse and create VM disks. Keep this under libvirt's standard images
    # tree so Ubuntu's generated AppArmor profiles can authorize it.
    permissions = {
      owner = tostring(var.storage_owner_uid)
      group = tostring(var.disk_permissions.gid)
      mode  = "0770"
    }
  }
  create = { autostart = true, start = true }
}

resource "libvirt_volume" "user_root" {
  name       = "${var.name}.qcow2"
  pool       = libvirt_pool.vm.name
  capacity   = var.disk_gib * 1024 * 1024 * 1024
  allocation = 0
  target = {
    format = { type = "qcow2" }
    permissions = {
      owner = tostring(var.storage_owner_uid)
      group = tostring(var.disk_permissions.gid)
      mode  = "0660"
    }
  }
  create     = { content = { url = "${local.build_dir}/bootstrap.qcow2" } }
  depends_on = [terraform_data.image, libvirt_pool.vm]
  lifecycle {
    ignore_changes = [create, target.permissions]
  }
}

resource "libvirt_domain" "vm" {
  depends_on  = [terraform_data.preflight, terraform_data.network_filter, terraform_data.host_nfs]
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
    type            = "hvm"
    type_arch       = "x86_64"
    type_machine    = "q35"
    loader          = var.uefi.code
    loader_type     = "pflash"
    loader_readonly = "yes"
    loader_secure   = "no"
    loader_format   = "raw"
    nv_ram = {
      nv_ram   = "${var.uefi.nvram_dir}/${var.name}_VARS.fd"
      template = var.uefi.vars_template
      format   = "raw"
    }
    boot_devices = [{ dev = "hd" }]
  }
  devices = {
    disks = [{
      device = "disk"
      driver = { name = "qemu", type = "qcow2", cache = "none" }
      # Use the resolved file path in domain XML. Ubuntu's AppArmor helper
      # omits disk permissions for pool/volume references, so QEMU receives
      # EACCES even when the volume's Unix owner and group are correct.
      source = { file = { file = libvirt_volume.user_root.path } }
      target = { dev = "vda", bus = "virtio" }
    }]
    interfaces = [{
      mac        = { address = var.mac_address }
      model      = { type = "virtio" }
      source     = { network = { network = var.network_name } }
      filter_ref = { filter = local.network_filter_name }
    }]
    channels = concat([
      { source = { unix = {} }, target = { virt_io = { name = "org.qemu.guest_agent.0" } } }
      ], var.gpu.enabled ? [] : [
      { source = { spice_vmc = true }, target = { virt_io = { name = "com.redhat.spice.0" } } }
    ])
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
  # libvirt's numeric domain ID changes after every start/stop. Use its stable
  # UUID so routine VM lifecycle commands never rerun repository bootstrap.
  triggers_replace = [libvirt_domain.vm.uuid, libvirt_volume.user_root.id]
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
