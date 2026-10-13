# Compute module: the instance itself, its root and data disks, and the
# templated cloud-init that turns a bare Ubuntu host into the rootless
# docker engine this program talks to. The azure twin of
# provider/aws/modules/compute, mirroring its structure and its comment
# discipline. See Section 5.6 of repos/spec/devcontainer-platform.md.
#
# Deliberately absent, by design rather than by omission: no CIDR input, no
# data source looking up an image, a subnet or a resource group, and no
# public IP of any kind. Addressing belongs to the network module and the
# platform unit to the security module; this module consumes identifiers
# rather than deriving them, which is what allows both to be absent
# entirely when a caller supplies their own. The VM's network interface is
# simply never given a public IP -- azure has no flag to flip here, only a
# resource to attach or not attach, and this module attaches none. The
# instance reaches the internet outbound (package installs, image pulls,
# the relay listener's control channel) through the subnet's default
# egress, and is reachable from outside through the relay hybrid connection
# and nothing else.
#
# The bootstrap SSH key satisfies the azure VM API's own admin-credential
# requirement and nothing else: sshd is disabled in the rendered cloud-init
# below, the network security group admits no inbound traffic at all, and
# no public IP exists, so the key is inert by construction -- the azure
# counterpart of provider/aws's no-key-pair doctrine note. There is no
# password admin, ever.

locals {
  # The relay listener's script and its systemd unit, embedded into the
  # cloud-config as base64. filebase64 is a TERRAFORM function, so it must
  # run here and the result be passed INTO templatefile as a variable --
  # templatefile interpolates only its own named arguments and cannot call
  # terraform functions itself. The shipped files are
  # gd-relay-listener.py and gd-relay.service beside this main.tf; the
  # cloud-config writes them with cloud-init's `encoding: b64`.
  relay_listener_b64 = filebase64("${path.module}/gd-relay-listener.py")
  relay_unit_b64     = filebase64("${path.module}/gd-relay.service")
}

resource "tls_private_key" "bootstrap" {
  algorithm = "ED25519"
}

resource "azurerm_linux_virtual_machine" "this" {
  name                  = "${var.name_prefix}-vm"
  resource_group_name   = var.resource_group_name
  location              = var.location
  size                  = var.vm_size
  admin_username        = "gdbootstrap"
  network_interface_ids = [azurerm_network_interface.this.id]

  admin_ssh_key {
    username   = "gdbootstrap"
    public_key = tls_private_key.bootstrap.public_key_openssh
  }

  # The source image reference is the four-part azure URN the caller
  # supplies, split rather than re-typed: the root composition validates
  # the shape, and this module never resolves "the latest Ubuntu image" on
  # the caller's behalf, so a caller always knows exactly which image an
  # apply will launch (the same discipline provider/aws applies to var.ami).
  source_image_reference {
    publisher = split(":", var.os_image_urn)[0]
    offer     = split(":", var.os_image_urn)[1]
    sku       = split(":", var.os_image_urn)[2]
    version   = split(":", var.os_image_urn)[3]
  }

  # StandardSSD_LRS rather than Premium_LRS: the aws composition picked
  # gp3 for its root volume as the cost-appropriate general-purpose disk,
  # and StandardSSD_LRS is that tier on azure. The root disk carries the OS
  # and the cloud-config; everything the daemon writes lives on the data
  # disk below.
  os_disk {
    storage_account_type = "StandardSSD_LRS"
    caching              = "ReadWrite"
    disk_size_gb         = var.root_volume_size_gb
  }

  # azure always replaces the instance when custom_data changes -- there is
  # no in-place update for VM custom data -- which is the same
  # replacement-on-provisioning-change rule provider/aws's
  # user_data_replace_on_change documents, arrived at by the platform
  # rather than by a flag: a cloud-config edit produces a new instance,
  # never a mutated one.
  custom_data = base64encode(templatefile("${path.module}/cloud-init.yaml", {
    daemon_user          = var.docker_daemon_user
    data_root            = var.docker_data_root
    tls_listen_address   = var.docker_tls_listen_address
    tls_listen_port      = var.docker_tls_listen_port
    create_data_volume   = var.create_data_volume
    docker_repo_base_url = var.docker_repo_base_url
    docker_repo_channel  = var.docker_repo_channel
    relay_namespace      = var.relay_namespace
    hybrid_connection    = var.hybrid_connection
    relay_listener_b64   = local.relay_listener_b64
    relay_unit_b64       = local.relay_unit_b64
  }))

  # The user-assigned identity is how the VM reaches its vault (the
  # published TLS material) and its relay namespace (the listener's token)
  # without any credential on disk: the identity's roles are the security
  # module's grants, and the token comes from the instance metadata
  # endpoint at runtime.
  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }

  tags = merge(var.tags, {
    Name = var.name_prefix
  })
}

resource "azurerm_network_interface" "this" {
  name                = "${var.name_prefix}-nic"
  location            = var.location
  resource_group_name = var.resource_group_name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Dynamic"

    # No public_ip_address_id, by doctrine: see this file's header comment.
    # The instance is addressable from outside only through the relay.
  }

  tags = merge(var.tags, {
    Name = var.name_prefix
  })
}

# The data disk, its attachment and its LUN exist only when the caller opts
# in; a deployment that wants root-disk-only storage gets exactly that,
# with no azurerm_managed_disk or attachment declared at all rather than
# one that is merely unused.
resource "azurerm_managed_disk" "data" {
  count = var.create_data_volume ? 1 : 0

  name                 = "${var.name_prefix}-data"
  location             = var.location
  resource_group_name  = var.resource_group_name
  storage_account_type = "StandardSSD_LRS"
  create_option        = "Empty"
  disk_size_gb         = var.data_volume_size_gb

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-data"
  })
}

resource "azurerm_virtual_machine_data_disk_attachment" "data" {
  count = var.create_data_volume ? 1 : 0

  virtual_machine_id = azurerm_linux_virtual_machine.this.id
  managed_disk_id    = azurerm_managed_disk.data[0].id
  lun                = 0

  # Caching None for the daemon's data disk: docker's own writes are
  # transactional and the disk is not a read cache candidate, so routing
  # the attachment's cache in front of them would buy nothing and risk a
  # cache-vs-disk divergence the daemon's data cannot afford.
  caching = "None"
}
