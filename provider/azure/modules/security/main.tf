# Security module: the platform unit the remote engine sits behind -- the
# zero-inbound network security group and its subnet association, the
# user-assigned managed identity the instance assumes, the Key Vault that
# holds the instance's published TLS material, the Relay namespace and
# hybrid connection the docker API travels over, and the role assignments
# that wire the four together. The azure twin of provider/aws/modules/
# security, mirroring its structure and its comment discipline. See Section
# 5.6 of repos/spec/devcontainer-platform.md.
#
# Deliberately absent, by design rather than by omission: no inbound
# security rule of any kind. The security_rule list below is EMPTY, and
# that emptiness is the doctrine: the transport is the azure Relay hybrid
# connection (the transport_azure module's wire contract), whose listener
# dials OUTBOUND to the relay service and whose daemon listener never
# leaves the VM's loopback, so nothing needs to reach this instance from
# outside. Egress is left at azure's default (allow-all outbound), the
# minimal posture that still lets the VM reach the package repositories,
# the container registry, the relay service and its vault; narrowing it is
# a future input away, not a rule to delete. Revocation is performed on the
# Entra side -- removing the relay Sender role from a principal, or the
# operator's own access -- not from the resources below.
#
# The four roles the identity carries are exactly: Key Vault Secrets User
# on the vault (read the published TLS material -- never an Officer role,
# which could rewrite it), Azure Relay Listener on the namespace (hold the
# hybrid connection's listener end), and nothing else. The human-side role
# assignments come in through var.relay_sender_object_ids and
# var.keyvault_officer_object_ids, each granted exactly its one role; no
# for_each here grants anything wider.
#
# data.azurerm_client_config resolves the calling identity's tenant so the
# vault's tenant_id is the caller's rather than a typed literal, and so a
# vault created in one tenant cannot silently outlive a tenant move with a
# stale tenant pinned.
#
# The two derived names below come from the shared name_prefix: azure's law
# for a vault name is 3-24 alphanumerics and dashes (globally unique), and
# for a relay namespace name 6-50 alphanumerics and dashes (globally
# unique). The "-vault" and "-relay" suffixes keep both inside their laws
# exactly when var.name_prefix satisfies the root composition's own
# name_prefix validation (1-18 lowercase alphanumeric/dash characters
# beginning with a letter, not ending with a dash), which is where the
# length and charset rules are enforced -- not duplicated here.

data "azurerm_client_config" "current" {}

locals {
  vault_name           = "${var.name_prefix}-vault"
  relay_namespace_name = "${var.name_prefix}-relay"
}

resource "azurerm_network_security_group" "this" {
  name                = "${var.name_prefix}-nsg"
  location            = var.location
  resource_group_name = var.resource_group_name

  # The empty rule list IS the security property: zero inbound rules, so
  # the group removes what an unassociated subnet would inherit and the
  # internet-facing posture is deny by default (there is no public IP to
  # address from the internet anyway). One honest boundary remains: azure's
  # own built-in default rules stand beside this list, and AllowVnetInBound
  # admits vnet-internal traffic, so reachability from inside the vnet or a
  # peered network is governed by those defaults, not by this module --
  # narrowing that is a security_rule this composition has deliberately
  # never carried. Azure's default outbound rules remain, which is what the
  # instance's own outbound paths (package installs, image pulls, the relay
  # listener's control channel, the vault pulls) need. Any rule added here
  # is a deliberate, reviewed change to that doctrine, not a default.
  security_rule = []

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-nsg"
  })
}

resource "azurerm_subnet_network_security_group_association" "this" {
  subnet_id                 = var.subnet_id
  network_security_group_id = azurerm_network_security_group.this.id
}

resource "azurerm_user_assigned_identity" "this" {
  name                = "${var.name_prefix}-identity"
  location            = var.location
  resource_group_name = var.resource_group_name

  tags = merge(var.tags, {
    Name = var.name_prefix
  })
}

resource "azurerm_key_vault" "this" {
  name                = local.vault_name
  location            = var.location
  resource_group_name = var.resource_group_name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"
  # Authorization through azure RBAC only, never per-secret access
  # policies: every grant below is one azurerm_role_assignment, so the
  # vault's access is readable from the role assignments alone and no
  # policy exists that a later edit could widen in place.
  rbac_authorization_enabled = true

  # Azure's minimum retention. Destroying the deployment soft-deletes this
  # vault rather than erasing it, and the name stays reserved for these 7
  # days: a destroy followed by a redeploy with the same name_prefix fails
  # vault creation with a name-in-use error until the soft-deleted vault is
  # purged -- `az keyvault purge --name <name_prefix>-vault` -- or the
  # window lapses. The recoverability window is the same property: a stray
  # delete does not strand the daemon's certificate reference.
  soft_delete_retention_days = 7

  # Public network access stays enabled, and that is a deliberate boundary,
  # not an oversight: authorization is azure RBAC alone (no access
  # policies, and no keys -- the certificate material is pulled by the VM
  # over its managed identity) from any network that can reach the
  # endpoint, and what the vault holds makes that boundary material. One
  # instance's vault carries its published TLS material AND its shell-env
  # secrets -- every project credential the container's build and runtime
  # consume -- so the boundary's entire weight rests on the role
  # assignments this module declares: the VM identity's read-only Secrets
  # User, and the operators' Officer grant. A principal holding neither
  # role learns nothing from holding the vault's URI. The VM has no public
  # IP, so its own pulls leave the subnet over azure's egress path.
  # Network-wide private endpoints are a later hardening input away, not a
  # default this composition pretends to have.
  public_network_access_enabled = true

  tags = merge(var.tags, {
    Name = local.vault_name
  })
}

resource "azurerm_relay_namespace" "this" {
  name                = local.relay_namespace_name
  location            = var.location
  resource_group_name = var.resource_group_name
  sku_name            = "Standard"

  tags = merge(var.tags, {
    Name = local.relay_namespace_name
  })
}

resource "azurerm_relay_hybrid_connection" "this" {
  name                          = "gd-${var.instance_name}"
  resource_group_name           = var.resource_group_name
  relay_namespace_name          = azurerm_relay_namespace.this.name
  requires_client_authorization = true

  # requires_client_authorization is the wire contract: both halves of the
  # couple (the VM listener and the laptop sender) present Entra tokens for
  # their roles on every connection, so an unauthenticated dial is refused
  # by the service before any bridge exists.
}

# The VM identity's two grants, and the caller-supplied humans' two. Each
# for_each grants exactly one role to exactly the principals listed; an
# empty list creates nothing, so a deployment with no named senders or
# officers carries no human grants at all.
resource "azurerm_role_assignment" "identity_vault_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.this.principal_id
}

resource "azurerm_role_assignment" "identity_relay_listener" {
  scope                = azurerm_relay_namespace.this.id
  role_definition_name = "Azure Relay Listener"
  principal_id         = azurerm_user_assigned_identity.this.principal_id
}

resource "azurerm_role_assignment" "relay_sender" {
  for_each = toset(var.relay_sender_object_ids)

  scope                = azurerm_relay_namespace.this.id
  role_definition_name = "Azure Relay Sender"
  principal_id         = each.value
}

resource "azurerm_role_assignment" "keyvault_officer" {
  for_each = toset(var.keyvault_officer_object_ids)

  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = each.value
}
