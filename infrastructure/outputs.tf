output "connection" {
  description = "Non-secret inputs for the host CLI. The guest IP is discovered by MAC from libvirt leases."
  value       = local.connection
}
output "vm_network" {
  description = "Dedicated NAT network used by the VM's primary interface."
  value = {
    name    = local.vm_network_name
    cidr    = local.vm_network.cidr
    gateway = local.vm_network.gateway
  }
}
output "guest_configuration" {
  value = local.guest_config
}
output "firewall_test_network" {
  description = "Test subnet on the VM NAT bridge used to validate the VM's internal-network deny policy."
  value = {
    name            = local.firewall_test_network_name
    bridge_network  = local.vm_network_name
    uri             = var.libvirt_uri
    cidr            = local.firewall_test_network.cidr
    gateway         = local.firewall_test_network.gateway
    prefix          = local.firewall_test_network.prefix
    allow_test_port = local.firewall_test_network.allow_test_port
    deny_test_port  = local.firewall_test_network.deny_test_port
  }
}
