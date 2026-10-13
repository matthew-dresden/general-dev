# The variable surface every Terragrunt deployment writes to (Section 5.6 of
# repos/spec/devcontainer-platform.md). Every option this composition
# supports is declared here as an input; nothing below is chosen by editing
# this module. Grouped in three sections: the three create toggles, the
# network inputs (creation and replacement), the platform inputs (creation
# and replacement), and the compute inputs, followed by the two inputs
# shared across all three submodules.
#
# Each of the replacement inputs (var.resource_group_name, var.subnet_id,
# var.identity_id, var.relay_namespace, var.hybrid_connection) carries a
# pair of validation blocks rather than one: the toggle-false direction
# (the replacement is required) and the toggle-true direction (the
# replacement must be left unset). The second direction exists because
# silently ignoring a supplied replacement while also creating a new
# resource would leave the operator believing the identifier they named is
# in use when a new one was created instead.
#
# Every creation-side input a submodule needs when its toggle is true
# (var.vnet_cidr, var.subnet_cidr) carries a validation block asserting it
# is set in that case, so an omitted value fails at plan time naming the
# toggle and the input, rather than surfacing as a null-argument error
# inside a submodule's own validation expression that never names the
# missing root input.
#
# There are three toggles, not aws's four, by one structural decision: the
# azure security submodule is ONE platform unit (network security group and
# subnet association, user-assigned identity, Key Vault, relay namespace
# and hybrid connection, and their role assignments), so there is exactly
# one toggle for it -- var.create_platform -- where the aws composition
# needs two (create_security_group, create_iam_role) because its security
# unit's consumers split. Azure's compute module consumes three outputs of
# the platform unit (identity, namespace, hybrid connection), so the
# replacement surface is three inputs set or unset together, which the
# toggle's paired validations below express one input at a time.
#
# var.name_prefix additionally carries the azure resource-naming
# validation: lowercase alphanumeric characters and dashes, beginning with
# a letter, at most 18 characters, no consecutive dashes -- azure's vault
# naming law bans consecutive hyphens, and a prefix admitting them would
# pass the shape check and fail the vault's own creation at apply -- the
# bound that keeps the derived "-vault" suffix inside azure's 3-24
# vault-name law and the "-relay" suffix inside the namespace law. Every
# derived resource name in the composition is legal exactly when this
# validation passes.

# --- Create toggles (Section 5.6) --------------------------------------

variable "create_network" {
  description = <<-EOT
    Whether this deployment creates its own resource group, virtual network
    and subnet via the network submodule. Type bool, defaults to true.
    When false, no network is created and var.resource_group_name and
    var.subnet_id must both be supplied naming the existing resource group
    and subnet this deployment reuses instead (typically the
    resource_group_name and subnet_id outputs of a prior deployment of this
    module).
  EOT
  type        = bool
  default     = true
}

variable "create_platform" {
  description = <<-EOT
    Whether this deployment creates its own platform unit via the security
    submodule: the zero-inbound network security group and its subnet
    association, the user-assigned identity, the Key Vault, the relay
    namespace and hybrid connection, and their role assignments. Type bool,
    defaults to true. When false, no platform resource is created and
    var.identity_id, var.relay_namespace and var.hybrid_connection must all
    be supplied naming the existing identity and relay endpoints this
    deployment reuses instead.
  EOT
  type        = bool
  default     = true
}

variable "create_data_volume" {
  description = <<-EOT
    Whether the compute submodule creates, attaches and mounts a second
    data disk. Type bool, defaults to true. When false, no data disk is
    created or passed to the compute submodule and the instance is left
    with its root disk only; no replacement input is required because there
    is no identifier to substitute, only a disk the instance does without.
  EOT
  type        = bool
  default     = true
}

# --- Network inputs (Section 5.6) ---------------------------------------

variable "location" {
  description = <<-EOT
    Azure region the composition's resources are created in (for example
    "westeurope"). Type string, no default: always required, regardless of
    any toggle.
  EOT
  type        = string
}

variable "vnet_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the virtual network the network submodule creates
    (for example "10.100.0.0/16"). Type string, defaults to null (unset).
    Required when var.create_network is true; ignored otherwise.
  EOT
  type        = string
  default     = null

  validation {
    condition     = !var.create_network || var.vnet_cidr != null
    error_message = "var.vnet_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the virtual network the network submodule creates."
  }
}

variable "subnet_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the subnet the network submodule creates (for
    example "10.100.1.0/24"). Type string, defaults to null (unset).
    Required when var.create_network is true; ignored otherwise. Must be
    fully contained inside var.vnet_cidr, enforced by the network
    submodule's own validation.
  EOT
  type        = string
  default     = null

  validation {
    condition     = !var.create_network || var.subnet_cidr != null
    error_message = "var.subnet_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the subnet the network submodule creates."
  }
}

variable "resource_group_name" {
  description = <<-EOT
    Name of an existing resource group this deployment deploys into (for
    example "devcontainer-remote-prod-rg"). Type string, defaults to null
    (unset). Required whenever var.create_network is false, since every
    resource lands in some resource group; consumed by the security and
    compute submodules. Must be left unset when var.create_network is true:
    this deployment creates its own resource group in that case, and
    supplying an existing one at the same time is ambiguous.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_network || var.resource_group_name != null
    error_message = "var.resource_group_name is required when var.create_network is false: supply the name of the existing resource group this deployment deploys into."
  }

  validation {
    condition     = !var.create_network || var.resource_group_name == null
    error_message = "var.resource_group_name must be left unset when var.create_network is true: this deployment creates its own resource group, and also supplying var.resource_group_name would leave it ambiguous which group is in use. Set var.create_network to false to reuse var.resource_group_name instead."
  }
}

variable "subnet_id" {
  description = <<-EOT
    Identifier of an existing subnet this deployment reuses (for example a
    full subnet resource id). Type string, defaults to null (unset).
    Required when var.create_network is false, in which case it is passed
    to the security (for the group association) and compute submodules.
    Must be left unset when var.create_network is true: this deployment
    creates its own subnet in that case, and supplying an existing one at
    the same time is ambiguous.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_network || var.subnet_id != null
    error_message = "var.subnet_id is required when var.create_network is false: supply the identifier of the existing subnet this deployment reuses."
  }

  validation {
    condition     = !var.create_network || var.subnet_id == null
    error_message = "var.subnet_id must be left unset when var.create_network is true: this deployment creates its own subnet, and also supplying var.subnet_id would leave it ambiguous which subnet is in use. Set var.create_network to false to reuse var.subnet_id instead."
  }
}

# --- Platform inputs (Section 5.6) --------------------------------------

variable "instance_name" {
  description = <<-EOT
    Name of the instance this deployment provisions (for example
    "devcontainer-remote-prod"), naming the relay hybrid connection the
    couple meets on. Type string, defaults to null (unset). Required when
    var.create_platform is true; ignored when it is false.
  EOT
  type        = string
  default     = null

  validation {
    condition     = !var.create_platform || var.instance_name != null
    error_message = "var.instance_name is required when var.create_platform is true: supply the name of the instance this deployment provisions, used to name the relay hybrid connection the couple meets on."
  }
}

variable "identity_id" {
  description = <<-EOT
    Identifier of an existing user-assigned managed identity this
    deployment reuses (for example a full identity resource id). Type
    string, defaults to null (unset). Required when var.create_platform is
    false, in which case it is passed straight to the compute submodule.
    Must be left unset when var.create_platform is true: this deployment
    creates its own identity in that case, and supplying an existing one at
    the same time is ambiguous.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_platform || var.identity_id != null
    error_message = "var.identity_id is required when var.create_platform is false: supply the identifier of the existing user-assigned identity this deployment reuses."
  }

  validation {
    condition     = !var.create_platform || var.identity_id == null
    error_message = "var.identity_id must be left unset when var.create_platform is true: this deployment creates its own identity, and also supplying var.identity_id would leave it ambiguous which identity is in use. Set var.create_platform to false to reuse var.identity_id instead."
  }
}

variable "relay_namespace" {
  description = <<-EOT
    Fully qualified host name of an existing relay namespace this
    deployment reuses (for example
    "gd-example-relay.servicebus.windows.net"). Type string, defaults to
    null (unset). Required when var.create_platform is false, in which case
    it is passed straight to the compute submodule for the listener unit's
    environment file. Must be left unset when var.create_platform is true:
    this deployment creates its own namespace in that case, and supplying
    an existing one at the same time is ambiguous.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_platform || var.relay_namespace != null
    error_message = "var.relay_namespace is required when var.create_platform is false: supply the fully qualified host name of the existing relay namespace this deployment reuses."
  }

  validation {
    condition     = !var.create_platform || var.relay_namespace == null
    error_message = "var.relay_namespace must be left unset when var.create_platform is true: this deployment creates its own relay namespace, and also supplying var.relay_namespace would leave it ambiguous which namespace is in use. Set var.create_platform to false to reuse var.relay_namespace instead."
  }
}

variable "hybrid_connection" {
  description = <<-EOT
    Name of an existing hybrid connection this deployment reuses (for
    example "gd-devcontainer-remote-prod"), the rendezvous point the relay
    couple meets on. Type string, defaults to null (unset). Required when
    var.create_platform is false, in which case it is passed straight to
    the compute submodule for the listener unit's environment file. Must be
    left unset when var.create_platform is true: this deployment creates
    its own hybrid connection in that case, and supplying an existing one
    at the same time is ambiguous.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_platform || var.hybrid_connection != null
    error_message = "var.hybrid_connection is required when var.create_platform is false: supply the name of the existing hybrid connection this deployment reuses."
  }

  validation {
    condition     = !var.create_platform || var.hybrid_connection == null
    error_message = "var.hybrid_connection must be left unset when var.create_platform is true: this deployment creates its own hybrid connection, and also supplying var.hybrid_connection would leave it ambiguous which connection is in use. Set var.create_platform to false to reuse var.hybrid_connection instead."
  }
}

variable "relay_sender_object_ids" {
  description = <<-EOT
    Entra object ids of the principals granted the Azure Relay Sender role
    on the namespace the platform unit creates -- who may open the sender
    half of the couple. Type list(string), defaults to an empty list. Read
    only when var.create_platform is true; ignored otherwise.
  EOT
  type        = list(string)
  default     = []
}

variable "keyvault_officer_object_ids" {
  description = <<-EOT
    Entra object ids of the principals granted the Key Vault Secrets
    Officer role on the vault the platform unit creates -- the operators
    who publish the instance's TLS material and secrets. Type
    list(string), defaults to an empty list. Read only when
    var.create_platform is true; ignored otherwise.
  EOT
  type        = list(string)
  default     = []
}

# --- Compute inputs -------------------------------------------------------

variable "vm_size" {
  description = <<-EOT
    Azure VM size the instance is launched as (for example
    "Standard_D2pls_v5"). Type string, no default: always required,
    regardless of any toggle.
  EOT
  type        = string
}

variable "os_image_urn" {
  description = <<-EOT
    Azure image URN the instance boots from, in
    Publisher:Offer:Sku:Version notation. Type string, defaults to the
    Canonical Ubuntu 24.04 arm64 server image: the documented default,
    paired with an arm64-capable var.vm_size. An x64 VM size needs the x64
    server URN instead, since the size's architecture and the image's must
    match.
  EOT
  type        = string
  default     = "Canonical:ubuntu-24_04-lts:server-arm64:latest"
}

variable "root_volume_size_gb" {
  description = <<-EOT
    Size, in GiB, of the root disk (for example 30). Type number, no
    default: always required, regardless of any toggle.
  EOT
  type        = number
}

variable "data_volume_size_gb" {
  description = <<-EOT
    Size, in GiB, of the data disk (for example 100). Type number,
    defaults to 100. Read only when var.create_data_volume is true;
    ignored otherwise.
  EOT
  type        = number
  default     = 100
}

variable "docker_daemon_user" {
  description = <<-EOT
    Name of the dedicated, unprivileged Linux user the rootless docker
    daemon runs as (for example "dockerd"). Type string, defaults to
    "dockerd".
  EOT
  type        = string
  default     = "dockerd"
}

variable "docker_data_root" {
  description = <<-EOT
    Absolute path on the instance the rootless daemon uses as its data-root
    (for example "/mnt/docker-data"). Type string, no default: always
    required, regardless of any toggle.
  EOT
  type        = string
}

variable "docker_tls_listen_address" {
  description = <<-EOT
    Loopback IPv4 address the rootless daemon's mTLS listener binds to (for
    example "127.0.0.1"). Type string, defaults to "127.0.0.1". Must be the
    loopback address; the compute submodule's own validation rejects any
    other value.
  EOT
  type        = string
  default     = "127.0.0.1"
}

variable "docker_tls_listen_port" {
  description = <<-EOT
    TCP port the rootless daemon's mTLS listener binds to on loopback (for
    example 2376). Type number, defaults to 2376, the conventional Docker
    TLS port.
  EOT
  type        = number
  default     = 2376
}

variable "docker_repo_base_url" {
  description = <<-EOT
    Base URL of the Docker apt repository the compute submodule's
    cloud-init adds (for example "https://download.docker.com/linux/ubuntu").
    Type string, defaults to Docker's own public repository.
  EOT
  type        = string
  default     = "https://download.docker.com/linux/ubuntu"
}

variable "docker_repo_channel" {
  description = <<-EOT
    Release channel of the Docker apt repository the compute submodule's
    cloud-init adds (for example "stable"). Type string, defaults to
    "stable".
  EOT
  type        = string
  default     = "stable"
}

# --- Shared across all three submodules ------------------------------------

variable "name_prefix" {
  description = <<-EOT
    Prefix used to name every resource this composition creates, across all
    three submodules, and applied to the Name tag of each (for example
    "devcontainer-remote-prod"). Azure resource names carry laws narrower
    than this value's own shape: a vault name is at most 24 characters and
    a relay namespace name at least 6, and both are derived by suffixing
    this value, so the bound below is the composition's one statement of
    what keeps every derived name legal. Type string, no default: always
    required, regardless of any toggle.
  EOT
  type        = string

  validation {
    # Lowercase letter first, then lowercase alphanumerics and dashes, 18
    # characters at most: the "-vault" suffix then lands inside azure's
    # 3-24 vault-name law and the "-relay" suffix inside the 6-50
    # namespace law. Vault names may not end in a dash, and neither may
    # this prefix for the same derived-name reason; nor may they carry
    # consecutive dashes, azure's own vault-naming ban -- a prefix such as
    # "gd--prod" passes the shape check but fails the vault's creation at
    # apply, so the refusal is made here where it fails at plan instead.
    condition     = can(regex("^[a-z][a-z0-9-]{0,17}$", var.name_prefix)) && !can(regex("-$", var.name_prefix)) && !can(regex("--", var.name_prefix))
    error_message = "var.name_prefix (${var.name_prefix}) must be 1 to 18 lowercase alphanumeric or dash characters, beginning with a letter, ending with a dash nowhere and carrying no consecutive dashes: the derived vault and relay namespace names append '-vault' and '-relay' and must stay inside azure's own naming laws, which ban consecutive hyphens."
  }
}

variable "tags" {
  description = <<-EOT
    Common resource tags merged onto every resource this composition
    creates, across all three submodules, in addition to each resource's
    own Name tag. Type map(string), defaults to an empty map when the
    caller has no additional tags to apply.
  EOT
  type        = map(string)
  default     = {}
}
