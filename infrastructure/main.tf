locals {
  root      = abspath("${path.module}/..")
  build_dir = "${local.root}/.build/${var.name}"
  guest_config = merge(var.guest, {
    hostname       = var.name
    sshPublicKeys  = var.ssh_public_keys
    repoPath       = "/home/${var.guest.username}/${var.name}"
    dotfilesPath   = "/home/${var.guest.username}/.dotfiles"
    dotfilesLinks  = var.dotfiles_links
    apps           = var.apps
    toolchains     = var.toolchains
    nfsMounts      = var.nfs_mounts
    hostNfsExports = var.host_nfs_exports
    hostAddress    = coalesce(var.streaming.host_address, local.vm_network.gateway)
    mitmproxy = {
      enabled  = true
      proxyUrl = "http://${local.vm_network.gateway}:8080"
    }
    gpu            = var.gpu
    streaming = merge(var.streaming, {
      enabled      = var.gpu.enabled && var.streaming.enabled
      host_address = coalesce(var.streaming.host_address, local.vm_network.gateway)
    })
  })
  connection = {
    name         = var.name
    uri          = var.libvirt_uri
    mac          = local.mac_address
    network      = local.vm_network_name
    username     = var.guest.username
    uid          = var.guest.uid
    identity     = pathexpand(var.ssh_identity_file)
    guest_config = local.guest_config
  }
  host_nfs_config = {
    name         = var.name
    uri          = var.libvirt_uri
    network      = local.vm_network_name
    network_cidr = local.vm_network.cidr
    exports      = var.host_nfs_exports
  }
  vm_pool_name    = coalesce(var.vm_storage_pool_name, "${var.name}-user")
  vm_pool_path    = coalesce(var.vm_storage_pool_path, "/var/lib/libvirt/images/${local.vm_pool_name}")
  vm_network_name = "nixvm-${var.name}-nat"
  mac_address = var.mac_address != null ? var.mac_address : (
    var.name == "nixvm" ? "52:54:00:dc:10:01" :
    "52:54:00:${substr(md5(var.name), 0, 2)}:${substr(md5(var.name), 2, 2)}:${substr(md5(var.name), 4, 2)}"
  )
  vm_network = {
    cidr    = var.vm_network_cidr
    gateway = cidrhost(var.vm_network_cidr, 1)
    prefix  = tonumber(split("/", var.vm_network_cidr)[1])
  }
  network_filter_name        = "${var.name}-access"
  firewall_test_network_name = "${var.name}-firewall-test-subnet"
  firewall_test_network = {
    cidr            = var.firewall_test_network_cidr
    gateway         = cidrhost(var.firewall_test_network_cidr, 1)
    prefix          = tonumber(split("/", var.firewall_test_network_cidr)[1])
    allow_test_port = 49231
    deny_test_port  = 49232
  }
  network_filter_access = {
    host = {
      ips = distinct(concat([local.vm_network.gateway], var.host_access.ips,
      length(var.host_nfs_exports) > 0 ? [local.guest_config.hostAddress] : []))
      ports = var.host_access.all_ports ? [] : distinct(concat(var.host_access.ports,
      length(var.host_nfs_exports) > 0 ? [2049] : []))
      all_ports = var.host_access.all_ports
    }
    proxy = {
      ip   = local.vm_network.gateway
      port = 8080
    }
    internal = {
      deny_cidrs = var.internal_network_deny
      blocked_ports = [80, 443, 8443, 8080, 8081, 8082]
      # A short-lived host listener on the firewall test subnet gives the
      # guest connectivity probes one narrowly-scoped expected success case.
      # NFSv4 uses TCP/2049; allow it automatically for each configured remote
      # NFS server so users do not need a duplicate internal_network_allow rule.
      allow = concat(var.internal_network_allow, [
        for ip in distinct([for mount in var.nfs_mounts : mount.server if anytrue([
          for cidr in var.internal_network_deny : try(cidrcontains(cidr, mount.server), false)
          ])]) : {
          ip        = ip
          ports     = [2049]
          protocols = ["tcp"]
          all_ports = false
        }
        ], [{
          ip        = local.firewall_test_network.gateway
          ports     = [local.firewall_test_network.allow_test_port]
          all_ports = false
      }])
    }
  }
  mitmproxy_config = {
    name             = var.name
    uri              = var.libvirt_uri
    network          = local.vm_network_name
    gateway          = local.vm_network.gateway
    image            = var.mitmproxy_policy.image
    inspect_domains  = var.mitmproxy_policy.inspect_domains
    blocked_domains  = var.mitmproxy_policy.blocked_domains
    deny_cidrs       = var.internal_network_deny
  }
}

resource "terraform_data" "preflight" {
  triggers_replace = [var.libvirt_uri, local.vm_network_name, jsonencode(var.gpu)]
  provisioner "local-exec" {
    command = "python3 \"$NIXVM_ROOT/scripts/nixvm.py\" preflight"
    environment = {
      NIXVM_ROOT       = local.root
      NIXVM_CONNECTION = jsonencode(local.connection)
    }
  }
}

# Use a dedicated NAT network for this VM. The default libvirt network is
# intentionally left untouched because this VM has a custom filter and test NIC.
resource "libvirt_network" "vm_nat" {
  name      = local.vm_network_name
  autostart = true
  forward   = { mode = "nat" }
  ips = [
    {
      address = local.vm_network.gateway
      prefix  = local.vm_network.prefix
      dhcp = {
        ranges = [{
          start = cidrhost(var.vm_network_cidr, 2)
          end   = cidrhost(var.vm_network_cidr, -2)
        }]
      }
    },
    # Keep the firewall probe subnet on the same bridge as the primary NIC.
    # This lets SSH probes reach the host-side test endpoint without a second
    # TAP device whose link state is unreliable on some libvirt hosts.
    {
      address = local.firewall_test_network.gateway
      prefix  = local.firewall_test_network.prefix
    },
  ]
  depends_on = [terraform_data.preflight]

  lifecycle {
    precondition {
      condition = (
        !cidrcontains(var.firewall_test_network_cidr, cidrhost(var.vm_network_cidr, 0)) &&
        !cidrcontains(var.vm_network_cidr, cidrhost(var.firewall_test_network_cidr, 0))
      )
      error_message = "vm_network_cidr and firewall_test_network_cidr must not overlap."
    }
    precondition {
      condition = anytrue([
        for cidr in var.internal_network_deny : !strcontains(cidr, ":") &&
        cidrcontains(cidr, local.firewall_test_network.gateway)
      ]) && !contains([for rule in var.internal_network_allow : rule.ip], local.firewall_test_network.gateway)
      error_message = "The firewall test gateway must be covered by internal_network_deny and must not appear in internal_network_allow."
    }
  }
}

# libvirt has no nwfilter resource in the pinned provider. Define the filter on
# the host with virsh; the domain attaches to this host-enforced policy below.
resource "terraform_data" "network_filter" {
  input = {
    name   = local.network_filter_name
    uri    = var.libvirt_uri
    access = local.network_filter_access
  }
  depends_on = [terraform_data.preflight]
  # Ignore the retired replacement trigger during migration from the previous
  # callback provisioner so existing instances can be updated in place.
  lifecycle {
    ignore_changes = [triggers_replace]
  }
}

# Host exports are prepared interactively with scripts/nixvm.py setup-host-nfs.
# This check keeps apply from activating guest mounts before the host export exists.
resource "terraform_data" "host_nfs" {
  input      = local.host_nfs_config
  depends_on = [terraform_data.preflight]
  lifecycle {
    ignore_changes = [triggers_replace]
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
    ignore_changes = [
      create, allocation, allocation_unit, capacity_unit,
      physical_unit, target, type,
    ]
  }
}

resource "libvirt_domain" "vm" {
  depends_on  = [terraform_data.preflight, terraform_data.network_filter, terraform_data.host_nfs, libvirt_network.vm_nat]
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
    interfaces = [
      {
        mac        = { address = local.mac_address }
        model      = { type = "virtio" }
        source     = { network = { network = local.vm_network_name } }
        filter_ref = { filter = local.network_filter_name }
      },
    ]
    channels = [
      { source = { unix = {} }, target = { virt_io = { name = "org.qemu.guest_agent.0" } } },
      { source = { spice_vmc = true }, target = { virt_io = { name = "com.redhat.spice.0" } } }
    ]
    controllers = [{ type = "usb", model = "qemu-xhci" }]
    # Keep a virtual display even with GPU passthrough. Laptop dGPUs often have
    # no physical display outputs wired to them, while Sunshine needs a screen
    # to capture; Vulkan applications can still use the passed-through GPU.
    # Keep 1080p available as the minimum mode; SPICE guest agent/viewer can
    # still auto-resize the guest display with the host viewer.
    videos = [{ model = { type = "virtio", resolution = { x = 1920, y = 1080 } } }]
    # Keep a host-local SPICE console available alongside Sunshine streaming,
    # using the same virtio display device.
    graphics = [{ spice = { auto_port = true, listen = "127.0.0.1" } }]
    # Provide guest audio through an emulated Intel HD Audio device and carry
    # playback over the existing SPICE connection.
    audios = [{ id = 1, spice = {} }]
    sounds = [{ model = "ich9" }]
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
      NIXVM_BUILD      = local.build_dir
      NIXVM_CONNECTION = jsonencode(local.connection)
      NIXVM_REPOSITORY = abspath(pathexpand(var.repository_path))
      NIXVM_DOTFILES   = abspath(pathexpand(var.dotfiles_path))
    }
  }
}
