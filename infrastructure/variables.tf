variable "name" {
  type    = string
  default = "nixvm"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,30}$", var.name))
    error_message = "Use a lowercase VM name with letters, digits and hyphens."
  }
}
variable "libvirt_uri" {
  type    = string
  default = "qemu:///system"
  validation {
    condition     = var.libvirt_uri == "qemu:///system"
    error_message = "This bootstrap workflow currently supports local qemu:///system only."
  }
}
variable "vcpu" {
  type    = number
  default = 4
  validation {
    condition     = var.vcpu >= 1 && floor(var.vcpu) == var.vcpu
    error_message = "vcpu must be a positive integer."
  }
}
variable "memory_mib" {
  type    = number
  default = 8192
  validation {
    condition     = var.memory_mib >= 2048 && floor(var.memory_mib) == var.memory_mib
    error_message = "Allocate at least 2048 MiB."
  }
}
variable "disk_gib" {
  type    = number
  default = 80
  validation {
    condition     = var.disk_gib >= 20 && floor(var.disk_gib) == var.disk_gib
    error_message = "Allocate at least 20 GiB."
  }
}
variable "vm_storage_pool_name" {
  description = "Dedicated libvirt pool name for the new VM disk. Defaults to <VM name>-user."
  type        = string
  default     = null
  validation {
    condition     = var.vm_storage_pool_name == null || can(regex("^[a-zA-Z0-9_.-]{1,64}$", var.vm_storage_pool_name))
    error_message = "vm_storage_pool_name may contain only letters, digits, underscore, dot, and hyphen."
  }
}
variable "vm_storage_pool_path" {
  description = "Directory for the dedicated pool. Keep it under /var/lib/libvirt/images for Ubuntu libvirt AppArmor access."
  type        = string
  default     = null
  validation {
    condition     = var.vm_storage_pool_path == null || (startswith(var.vm_storage_pool_path, "/var/lib/libvirt/images/") && !contains(split("/", var.vm_storage_pool_path), ".."))
    error_message = "vm_storage_pool_path must be inside /var/lib/libvirt/images and may not traverse parent directories."
  }
}
variable "storage_owner_uid" {
  description = "Host UID that owns the dedicated pool directory and VM volume, normally the user running OpenTofu."
  type        = number
  default     = 1000
  validation {
    condition     = var.storage_owner_uid >= 0 && floor(var.storage_owner_uid) == var.storage_owner_uid
    error_message = "storage_owner_uid must be a nonnegative integer."
  }
}
variable "disk_permissions" {
  description = "QEMU runtime group ID for access to the user-owned VM disk."
  type = object({
    gid = optional(number, 64055)
  })
  default = {}
  validation {
    condition     = var.disk_permissions.gid >= 0 && floor(var.disk_permissions.gid) == var.disk_permissions.gid
    error_message = "Disk GID must be a nonnegative integer for the host QEMU runtime group."
  }
}
variable "uefi" {
  description = "Host OVMF firmware paths. Explicit pflash avoids selecting SEV/TDX ROM firmware on Ubuntu."
  type = object({
    code          = optional(string, "/usr/share/OVMF/OVMF_CODE_4M.fd")
    vars_template = optional(string, "/usr/share/OVMF/OVMF_VARS_4M.fd")
    nvram_dir     = optional(string, "/var/lib/libvirt/qemu/nvram")
  })
  default = {}
  validation {
    condition     = alltrue([for path in [var.uefi.code, var.uefi.vars_template, var.uefi.nvram_dir] : startswith(path, "/")])
    error_message = "UEFI firmware and NVRAM paths must be absolute host paths."
  }
}
variable "vm_network_cidr" {
  description = "IPv4 subnet for this VM's dedicated libvirt NAT network."
  type        = string
  default     = "192.168.124.0/24"
  validation {
    condition = can(cidrnetmask(var.vm_network_cidr)) && try(
      tonumber(split("/", var.vm_network_cidr)[1]) >= 16 &&
      tonumber(split("/", var.vm_network_cidr)[1]) <= 30,
      false
    )
    error_message = "vm_network_cidr must be a valid IPv4 CIDR with a prefix from /16 through /30."
  }
}
variable "firewall_test_network_cidr" {
  description = "IPv4 test subnet configured on the VM's NAT bridge. Its gateway must remain in internal_network_deny."
  type        = string
  default     = "192.168.123.0/24"
  validation {
    condition = can(cidrnetmask(var.firewall_test_network_cidr)) && try(
      tonumber(split("/", var.firewall_test_network_cidr)[1]) >= 16 &&
      tonumber(split("/", var.firewall_test_network_cidr)[1]) <= 30,
      false
    )
    error_message = "firewall_test_network_cidr must be a valid IPv4 CIDR with a prefix from /16 through /30."
  }
}
variable "mac_address" {
  type    = string
  default = "52:54:00:dc:10:01"
  validation {
    condition = alltrue([
      can(regex("^52:54:00(:[0-9a-fA-F]{2}){3}$", var.mac_address)),
      lower(var.mac_address) != "52:54:00:dc:10:fe",
    ])
    error_message = "Use a unique QEMU MAC address beginning with 52:54:00, distinct from the firewall test interface MAC."
  }
}
variable "host_access" {
  description = "Guest-initiated access to host IPs. Ports apply to TCP and UDP; all_ports allows all IP protocols."
  type = object({
    ips       = list(string)
    ports     = list(number)
    all_ports = bool
  })
  default = {
    ips       = []
    ports     = [53]
    all_ports = false
  }
  validation {
    condition = alltrue([
      for ip in var.host_access.ips : can(cidrhost("${ip}/32", 0)) || can(cidrhost("${ip}/128", 0))
    ])
    error_message = "host_access.ips must contain individual IPv4 or IPv6 addresses."
  }
  validation {
    condition     = alltrue([for port in var.host_access.ports : port >= 1 && port <= 65535 && floor(port) == port])
    error_message = "host_access.ports must contain integer port numbers from 1 to 65535."
  }
  validation {
    condition     = !var.host_access.all_ports || length(var.host_access.ports) == 0
    error_message = "Set host_access.ports to an empty list when host_access.all_ports is true."
  }
}
variable "internal_network_deny" {
  description = "CIDR blocks denied for guest-initiated traffic unless an internal_network_allow rule permits access."
  type        = list(string)
  default = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b:1::/48", "100::/64",
    "100:0:0:1::/64", "2001::/23", "2001:2::/48", "2001:10::/28", "2001:db8::/32",
    "3fff::/20", "5f00::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
  ]
  validation {
    condition = alltrue([
      for cidr in var.internal_network_deny : can(cidrhost(cidr, 0))
    ])
    error_message = "internal_network_deny must contain valid IPv4 or IPv6 CIDR blocks."
  }
}
variable "internal_network_allow" {
  description = "Per-IP internal network allow rules. Ports apply to TCP and UDP; all_ports allows all IP protocols for that IP."
  type = list(object({
    ip        = string
    ports     = list(number)
    all_ports = bool
  }))
  default = []
  validation {
    condition = alltrue([
      for rule in var.internal_network_allow : can(cidrhost("${rule.ip}/32", 0)) || can(cidrhost("${rule.ip}/128", 0))
    ])
    error_message = "Each internal_network_allow rule must use an individual IPv4 or IPv6 address."
  }
  validation {
    condition = alltrue(flatten([
      for rule in var.internal_network_allow : [
        for port in rule.ports : port >= 1 && port <= 65535 && floor(port) == port
      ]
    ]))
    error_message = "Each internal_network_allow rule's ports must be integers from 1 to 65535."
  }
  validation {
    condition = alltrue([
      for rule in var.internal_network_allow : !rule.all_ports || length(rule.ports) == 0
    ])
    error_message = "Set an allow rule's ports to an empty list when all_ports is true."
  }
  validation {
    condition = length(distinct([
      for rule in var.internal_network_allow : rule.ip
    ])) == length(var.internal_network_allow)
    error_message = "Each IP may appear only once in internal_network_allow."
  }
}
variable "guest" {
  type = object({
    username = optional(string, "gigid")
    uid      = optional(number, 1000)
    gid      = optional(number, 1000)
    timezone = optional(string, "Europe/Bucharest")
    locale   = optional(string, "en_US.UTF-8")
    keyboard = optional(string, "us")
  })
  default = {}
  validation {
    condition = (
      can(regex("^[a-z_][a-z0-9_-]*$", var.guest.username)) &&
      var.guest.uid >= 1000 && floor(var.guest.uid) == var.guest.uid &&
      var.guest.gid >= 1000 && floor(var.guest.gid) == var.guest.gid
    )
    error_message = "Provide a valid username and integer UID/GID >= 1000."
  }
}
variable "ssh_public_keys" {
  description = "Public keys only. A matching private key stays on the host or its agent."
  type        = list(string)
  validation {
    condition     = length(var.ssh_public_keys) > 0 && length(distinct(var.ssh_public_keys)) == length(var.ssh_public_keys) && alltrue([for key in var.ssh_public_keys : can(regex("^(ssh-|ecdsa-|sk-)[^\\n]+$", key))])
    error_message = "Supply one or more unique, single-line SSH public keys."
  }
}
variable "ssh_identity_file" {
  description = "Optional host private-key path; only its path is stored, never the key contents."
  type        = string
  default     = ""
}
variable "repository_path" {
  type    = string
  default = ".."
}
variable "dotfiles_path" {
  type    = string
  default = "~/.dotfiles"
}
variable "dotfiles_links" {
  description = "Home-relative destination -> dotfiles-relative source."
  type        = map(string)
  default = {
    ".p10k.zsh"                  = ".p10k.zsh"
    ".config/zed/settings.json" = ".config/zed/settings.json"
    ".config/zed/tasks.json"    = ".config/zed/tasks.json"
    ".config/zed/debug.json"    = ".config/zed/debug.json"
  }
  validation {
    condition = alltrue(flatten([for dest, src in var.dotfiles_links : [
      !startswith(dest, "/") && !contains(split("/", dest), ".."),
      !startswith(src, "/") && !contains(split("/", src), ".."),
      !contains([".zshrc", ".ssh", ".gnupg"], dest)
    ]]))
    error_message = "Use relative paths without '..'; do not replace .zshrc, .ssh, or .gnupg."
  }
}
variable "toolchains" {
  type = object({
    python = optional(bool, true)
    node   = optional(bool, true)
    llvm   = optional(bool, true)
    rust   = optional(bool, true)
    jdk    = optional(bool, false)
    garmin = optional(bool, false)
  })
  default = {}
}
variable "nfs_mounts" {
  description = "Manually configured NFS exports on remote servers to mount in the guest."
  type = list(object({
    server     = string
    export     = string
    mountpoint = string
    options    = optional(list(string), ["rw", "hard", "timeo=50", "retrans=2"])
  }))
  default = []
  validation {
    condition = alltrue([for m in var.nfs_mounts :
      startswith(m.export, "/") && startswith(m.mountpoint, "/mnt/") &&
      !contains(split("/", m.mountpoint), "..") && m.server != ""
    ]) && length(distinct([for m in var.nfs_mounts : m.mountpoint])) == length(var.nfs_mounts)
    error_message = "NFS exports must be absolute, with distinct mountpoints under /mnt/."
  }
}
variable "host_nfs_exports" {
  description = "Host directories to export over NFS and mount in the guest."
  type = list(object({
    host_path  = string
    guest_path = string
    options    = optional(list(string), ["rw", "hard", "timeo=50", "retrans=2"])
  }))
  default = []
  validation {
    condition = alltrue([for export in var.host_nfs_exports :
      startswith(export.host_path, "/") && !contains(split("/", export.host_path), "..") &&
      startswith(export.guest_path, "/") && export.guest_path != "/" &&
      !contains(split("/", export.guest_path), "..")
    ]) && length(distinct([for export in var.host_nfs_exports : export.guest_path])) == length(var.host_nfs_exports)
    error_message = "Host NFS export sources and guest destinations must be absolute; guest destinations must be distinct and cannot be /."
  }
  validation {
    condition = length(setintersection(
      toset([for export in var.host_nfs_exports : export.guest_path]),
      toset([for m in var.nfs_mounts : m.mountpoint])
    )) == 0
    error_message = "Guest mountpoints must be unique across nfs_mounts and host_nfs_exports."
  }
}
variable "gpu" {
  type = object({
    enabled        = optional(bool, false)
    pci_functions  = optional(list(string), [])
    rom_file       = optional(string)
    open_driver    = optional(bool, true)
    driver_package = optional(string, "stable")
  })
  default = {}
  validation {
    condition = (
      (!var.gpu.enabled || length(var.gpu.pci_functions) > 0) &&
      length(distinct(var.gpu.pci_functions)) == length(var.gpu.pci_functions) &&
      alltrue([for pci in var.gpu.pci_functions : can(regex("^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\\.[0-7]$", pci))]) &&
      contains(["stable", "production", "beta"], var.gpu.driver_package)
    )
    error_message = "GPU passthrough needs unique PCI functions (0000:01:00.0) and a supported driver package."
  }
}
variable "streaming" {
  type = object({
    enabled        = optional(bool, true)
    encoder        = optional(string, "nvenc")
    autologin      = optional(bool, true)
    capture_output = optional(string, "")
    host_address   = optional(string)
  })
  default = {}
  validation {
    condition     = contains(["nvenc", "software"], var.streaming.encoder)
    error_message = "Choose nvenc or explicitly opt into software encoding."
  }
  validation {
    condition     = var.streaming.host_address == null || can(cidrnetmask("${var.streaming.host_address}/32"))
    error_message = "streaming.host_address must be the host's IPv4 address on the libvirt network."
  }
}
