# Network module: exactly the network the remote engine needs, and nothing
# else. The azure twin of provider/aws/modules/network, mirroring its
# structure and its comment discipline. See Section 5.6 of
# repos/spec/devcontainer-platform.md.
#
# Deliberately absent, by design rather than by omission: no NAT gateway,
# no load balancer, no public IP resource, no network security group, and
# no security rule of any kind. The subnet exists to give the instance an
# outbound path -- the VM agent, the package installs, the container image
# pulls, the relay listener's outbound control channel -- and it is not
# there to be connected to: the instance this module serves gets no public
# IP (the compute module's doctrine note), so nothing on the internet can
# address it directly. Ingress posture is the security module's concern,
# which owns the network security group and its subnet association and
# keeps them out of this module on purpose, so "zero inbound rules" is a
# property a reader can confirm by reading that module's security_rule.

resource "azurerm_resource_group" "this" {
  # Azure has no VPC-shaped container to attach a Name tag to; the resource
  # group is the network module's outermost named object and every other
  # resource in the composition lands in it.
  name     = "${var.name_prefix}-rg"
  location = var.location

  tags = merge(var.tags, {
    Name = var.name_prefix
  })
}

resource "azurerm_virtual_network" "this" {
  name                = "${var.name_prefix}-vnet"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  address_space       = [var.vnet_cidr]

  tags = merge(var.tags, {
    Name = var.name_prefix
  })
}

resource "azurerm_subnet" "this" {
  name                 = "${var.name_prefix}-subnet"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.subnet_cidr]

  # The platform egress is pinned, not inherited: the compute module's
  # cloud-init -- the package_update/packages install, the curl of the
  # docker apt signing key, the container image pulls and the relay
  # listener's outbound control channel -- all depend on the subnet
  # carrying a default outbound path, and ARM API versions after
  # 2026-03-31 default new virtual networks to private (no default
  # egress), a default the azurerm provider's ~> 4.0 constraint would
  # otherwise adopt silently on a future provider upgrade. Stating the
  # value here makes the egress a declared property of this module on
  # whichever side of that date the resolved provider sits. Unlike AWS
  # there is no internet-gateway object to create or route table to wire
  # for an outbound-capable subnet, so none is declared here. The security
  # module associates the subnet's network security group; this module
  # associates nothing.
  default_outbound_access_enabled = true
}
