output "connection" {
  description = "Non-secret inputs for the host CLI. The guest IP is discovered by MAC from libvirt leases."
  value       = local.connection
}
output "guest_configuration" {
  value = local.guest_config
}
