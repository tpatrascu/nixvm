variable "name" {
  type    = string
  default = "devvm"
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
variable "network_name" {
  type    = string
  default = "default"
}
variable "mac_address" {
  type    = string
  default = "52:54:00:dc:10:01"
  validation {
    condition     = can(regex("^52:54:00(:[0-9a-fA-F]{2}){3}$", var.mac_address))
    error_message = "Use a unique QEMU MAC address beginning with 52:54:00."
  }
}
variable "guest" {
  type = object({
    username = optional(string, "gigid")
    uid      = optional(number, 1000)
    gid      = optional(number, 1000)
    hostname = optional(string, "devvm")
    timezone = optional(string, "Europe/Bucharest")
    locale   = optional(string, "en_US.UTF-8")
    keyboard = optional(string, "us")
  })
  default = {}
  validation {
    condition = (
      can(regex("^[a-z_][a-z0-9_-]*$", var.guest.username)) &&
      can(regex("^[a-z][a-z0-9-]{0,62}$", var.guest.hostname)) &&
      var.guest.uid >= 1000 && floor(var.guest.uid) == var.guest.uid &&
      var.guest.gid >= 1000 && floor(var.guest.gid) == var.guest.gid
    )
    error_message = "Provide a valid username/hostname and integer UID/GID >= 1000."
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
  description = "Home-relative destination -> dotfiles-relative source. Zsh uses the portable adapter instead."
  type        = map(string)
  default     = { ".p10k.zsh" = ".p10k.zsh" }
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
  })
  default = {}
}
variable "nfs_mounts" {
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
    host_address   = optional(string, "192.168.122.1")
  })
  default = {}
  validation {
    condition     = contains(["nvenc", "software"], var.streaming.encoder)
    error_message = "Choose nvenc or explicitly opt into software encoding."
  }
  validation {
    condition     = can(cidrnetmask("${var.streaming.host_address}/32"))
    error_message = "streaming.host_address must be the host's IPv4 address on the libvirt network."
  }
}
