# The variable surface every Terragrunt deployment writes to (Section 5.6 of
# repos/spec/devcontainer-platform.md). Every option this composition
# supports is declared here as an input; nothing below is chosen by editing
# this module. Grouped in four sections: the three create toggles, the
# network inputs (creation and replacement), the platform inputs (creation
# and replacement), and the compute inputs, followed by the inputs shared
# across the submodules and the deploy-contract inputs.
#
# Each of the replacement inputs (var.subnet_id, var.sa_email) carries a
# pair of validation blocks rather than one: the toggle-false direction
# (the replacement is required) and the toggle-true direction (the
# replacement must be left unset). The second direction exists because
# silently ignoring a supplied replacement while also creating a new
# resource would leave the operator believing the identifier they named is
# in use when a new one was created instead.
#
# Every creation-side input a submodule needs when its toggle is true
# (var.vpc_cidr, var.subnet_cidr) carries a validation block asserting it
# is set in that case, so an omitted value fails at plan time naming the
# toggle and the input, rather than surfacing as a null-argument error
# inside a submodule's own validation expression that never names the
# missing root input.
#
# There are three toggles, matching the azure composition's count rather
# than aws's four, by one structural decision: the gcp platform submodule
# is ONE platform unit (the service account and the six per-instance
# secrets with their secretAccessor bindings), so there is exactly one
# toggle for it -- var.create_platform. The gcp compute module consumes one
# output of the platform unit (the service account email), so the
# replacement surface is one input set or unset, which the toggle's paired
# validations below express.
#
# var.instance_name and var.name_prefix additionally carry the gcp
# resource-naming validations: a gcp VM name (and the secrets prefix minted
# from the same sanitized value) must be a legal RFC1035 label -- lowercase
# letters, digits and dashes, beginning with a letter and ending
# alphanumerically -- and the prefix's own bound is the tighter derived-name
# law: the platform unit names the service account by junctioning "gd-" onto
# this prefix, and a service account id may be at most 30 characters, which
# is what pins the prefix at 27. Every derived resource name in the
# composition is legal exactly when these validations pass.

# --- Create toggles (Section 5.6) --------------------------------------

variable "create_network" {
  description = <<-EOT
    Whether this deployment creates its own virtual network, subnetwork and
    IAP firewall via the network submodule (with the Cloud NAT that gives
    the internal-only instance its outbound path). Type bool, defaults to
    true. When false, no network is created and var.subnet_id must be
    supplied naming the existing subnetwork this deployment reuses instead
    (typically the subnet_id output of a prior deployment of this module).
  EOT
  type        = bool
  default     = true
}

variable "create_platform" {
  description = <<-EOT
    Whether this deployment creates its own platform unit via the platform
    submodule: the service account the instance assumes and the six
    per-instance secrets (the three TLS material secrets, the checksum
    secret beside them, and the two configuration-payload secrets) with the
    secretAccessor bindings that let exactly this instance read them. Type
    bool, defaults to true. When false, no platform resource is created and
    var.sa_email must be supplied naming the existing service account this
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

variable "region" {
  description = <<-EOT
    GCP region the composition's regional resources are created in (for
    example "us-east1"). Type string, no default: always required,
    regardless of any toggle. The scaffold derives it from the zone (the
    zone with its final dash-letter removed) -- it is never chosen
    separately.
  EOT
  type        = string

  validation {
    # The shape the engine address record's region accessor enforces
    # (devcontainer_config.instances' region law), refused here at plan
    # time rather than at apply.
    condition     = can(regex("^[a-z0-9-]+$", var.region))
    error_message = "var.region (${var.region}) must be a gcp region of lowercase letters, digits and dashes (us-east1)."
  }
}

variable "vpc_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the virtual network the network submodule creates
    (for example "10.100.0.0/16"). Type string, defaults to null (unset).
    Required when var.create_network is true; ignored otherwise.
  EOT
  type        = string
  default     = null

  validation {
    condition     = !var.create_network || var.vpc_cidr != null
    error_message = "var.vpc_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the virtual network the network submodule creates."
  }
}

variable "subnet_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the subnetwork the network submodule creates (for
    example "10.100.1.0/24"). Type string, defaults to null (unset).
    Required when var.create_network is true; ignored otherwise. Must be
    fully contained inside var.vpc_cidr, enforced by the network
    submodule's own validation.
  EOT
  type        = string
  default     = null

  validation {
    condition     = !var.create_network || var.subnet_cidr != null
    error_message = "var.subnet_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the subnetwork the network submodule creates."
  }
}

variable "subnet_id" {
  description = <<-EOT
    Identifier of an existing subnetwork this deployment reuses, in
    self-link or id form (for example the subnet_id output of a prior
    deployment of this module: projects/EXAMPLE/regions/EXAMPLE-REGION/
    subnetworks/EXAMPLE-subnet). Type string, defaults to null (unset).
    Required when var.create_network is false, in which case it is passed
    to the compute submodule as the interface's subnetwork. Must be left
    unset when var.create_network is true: this deployment creates its own
    subnetwork in that case, and supplying an existing one at the same time
    is ambiguous. The network the subnetwork belongs to is carried inside
    this identifier -- gcp names no network separately on the interface --
    so no second reuse input exists for it.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_network || var.subnet_id != null
    error_message = "var.subnet_id is required when var.create_network is false: supply the identifier of the existing subnetwork this deployment reuses."
  }

  validation {
    condition     = !var.create_network || var.subnet_id == null
    error_message = "var.subnet_id must be left unset when var.create_network is true: this deployment creates its own subnetwork, and also supplying var.subnet_id would leave it ambiguous which subnetwork is in use. Set var.create_network to false to reuse var.subnet_id instead."
  }
}

# --- Platform inputs (Section 5.6) --------------------------------------

variable "sa_email" {
  description = <<-EOT
    Email address of an existing service account this deployment reuses
    (for example "gd-EXAMPLE@EXAMPLE-project.iam.gserviceaccount.com").
    Type string, defaults to null (unset). Required when var.create_platform
    is false, in which case it is passed straight to the compute submodule
    as the account the instance assumes. Must be left unset when
    var.create_platform is true: this deployment creates its own service
    account in that case, and supplying an existing one at the same time is
    ambiguous. A reused account must already carry secretAccessor on the
    secrets the deployment reads -- this composition grants nothing outside
    the unit it creates.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_platform || var.sa_email != null
    error_message = "var.sa_email is required when var.create_platform is false: supply the email of the existing service account this deployment reuses."
  }

  validation {
    condition     = !var.create_platform || var.sa_email == null
    error_message = "var.sa_email must be left unset when var.create_platform is true: this deployment creates its own service account, and also supplying var.sa_email would leave it ambiguous which account is in use. Set var.create_platform to false to reuse var.sa_email instead."
  }
}

# --- Compute inputs -------------------------------------------------------

variable "instance_name" {
  description = <<-EOT
    Name of the virtual machine this deployment creates (for example
    "devcontainer-remote-prod"), already in its sanitized RFC1035 form --
    the scaffold writes instances.sanitize_gcp_name of the instance
    directory's name. Type string, no default: always required, regardless
    of any toggle.
  EOT
  type        = string

  validation {
    # A legal RFC1035 label of at most 32 characters (the bound
    # devcontainer_config.instances.GCP_NAME_MAX_LENGTH pins so the
    # platform's derived names stay legal): lowercase letter first, then
    # lowercase alphanumerics and dashes, ending alphanumerically.
    condition     = can(regex("^[a-z][a-z0-9-]{0,30}[a-z0-9]$", var.instance_name))
    error_message = "var.instance_name (${var.instance_name}) must be a legal RFC1035 label of 2 to 32 lowercase letters, digits and dashes, beginning with a letter and ending with a letter or digit -- the sanitized form the gcp VM is named by."
  }
}

variable "vm_size" {
  description = <<-EOT
    GCP machine type the instance is launched as (for example
    "e2-standard-2"). Type string, no default: always required, regardless
    of any toggle.
  EOT
  type        = string
}

variable "os_image" {
  description = <<-EOT
    GCP boot image the instance boots from, in project/family notation
    (for example "ubuntu-os-cloud/ubuntu-2404-lts-amd64"). Type string,
    defaults to the Canonical Ubuntu 24.04 LTS amd64 image: the documented
    default, paired with the amd64 e2 default machine type.
  EOT
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
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
    Size, in GiB, of the data disk (for example 30). Type number, defaults
    to 30. Read only when var.create_data_volume is true; ignored
    otherwise.
  EOT
  type        = number
  default     = 30
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
    IPv4 address the rootless daemon's mTLS listener binds to. Type string,
    defaults to "0.0.0.0" and must stay exactly that: the gcp doctrine is
    that the IAP tunnel terminates on the VM's nic0, so the daemon binds
    the interface address -- the boundary is the VPC's sole ingress rule
    (tcp/2376 from the IAP range), not a loopback bind. The compute
    submodule's own validation rejects any other value.
  EOT
  type        = string
  default     = "0.0.0.0"
}

variable "docker_tls_listen_port" {
  description = <<-EOT
    TCP port the rootless daemon's mTLS listener binds to (for example
    2376), which the IAP tunnel dials and the firewall's one rule admits.
    Type number, defaults to 2376, the conventional Docker TLS port.
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

# --- Deploy-contract and shared inputs -------------------------------------

variable "project_id" {
  description = <<-EOT
    GCP project id every resource of this composition is created in, and
    the project the state bucket and the six secrets live in (for example
    "devcontainer-remote-prod"). Type string, no default: always required,
    regardless of any toggle. Carried explicitly on every resource -- the
    generated provider block stays empty -- so no resource can land in an
    ambient default project and diverge from the project the state bucket's
    digest was composed under.
  EOT
  type        = string

  validation {
    # The shape the engine address record's project accessor enforces
    # (devcontainer_config.instances.PROJECT_ID_PATTERN), refused here at
    # plan time rather than at apply.
    condition     = can(regex("^[a-z0-9-]{6,30}$", var.project_id))
    error_message = "var.project_id (${var.project_id}) must be a gcp project id of 6 to 30 lowercase letters, digits and dashes."
  }
}

variable "zone" {
  description = <<-EOT
    GCP zone the instance and its disks are created in (for example
    "us-east1-b"). Type string, no default: always required, regardless of
    any toggle. The region var.region names is the zone's own -- the
    scaffold derives one from the other and never lets them disagree.
  EOT
  type        = string

  validation {
    # The shape the engine address record's zone accessor enforces
    # (devcontainer_config.instances.ZONE_PATTERN): a region, a dash and a
    # final zone letter.
    condition     = can(regex("^[a-z0-9-]+-[a-z]$", var.zone))
    error_message = "var.zone (${var.zone}) must be a gcp zone of the form <region>-<letter> (us-east1-b)."
  }
}

variable "state_store_suffix" {
  description = <<-EOT
    The committed suffix of the instance's state bucket name, mirrored from
    remote-instances/root-gcp.hcl's own `state_store_suffix` local (the
    declaration devcontainer_config.state_store_gcp parses). Type string,
    defaults to the committed value. Read only by the state_bucket output's
    digest -- never by a resource -- because the state bucket itself is NOT
    terraform-managed: state_store_gcp owns it, and the root-gcp.hcl local
    is the declaration. Keep the two values equal; tests pin them.
  EOT
  type        = string
  default     = "q4m7zk"

  validation {
    condition     = can(regex("^[a-z0-9]+$", var.state_store_suffix))
    error_message = "var.state_store_suffix (${var.state_store_suffix}) must be a non-empty lowercase alphanumeric token, the committed suffix root-gcp.hcl declares."
  }
}

variable "name_prefix" {
  description = <<-EOT
    Prefix used to name every derived resource this composition creates,
    across all three submodules (for example "devcontainer-remote-prod"),
    already in its sanitized RFC1035 form -- the scaffold writes
    instances.sanitize_gcp_name of the instance directory's name, the same
    value the secrets prefix carries. Type string, no default: always
    required, regardless of any toggle.
  EOT
  type        = string

  validation {
    # Lowercase letter first, then lowercase alphanumerics and dashes,
    # ending alphanumerically, 3 to 27 characters: the platform unit names
    # the service account by junctioning "gd-" onto this prefix, and a gcp
    # service account id is 6 to 30 characters, which is exactly the bound
    # this shape pins. The network, subnetwork, router, NAT, firewall and
    # data-disk names derived with their own short suffixes all stay inside
    # gcp's 63-character resource-name law at any prefix this validation
    # admits.
    condition     = can(regex("^[a-z][a-z0-9-]{2,26}[a-z0-9]$", var.name_prefix))
    error_message = "var.name_prefix (${var.name_prefix}) must be 3 to 27 lowercase alphanumeric or dash characters, beginning with a letter and ending with a letter or digit: the derived service account id prepends 'gd-' and must stay inside gcp's 6-30 service-account-id law."
  }
}

variable "tags" {
  description = <<-EOT
    Common labels merged onto every resource this composition creates,
    across all three submodules (for example { Environment = "acme" }). Type
    map(string), defaults to an empty map when the caller has no additional
    labels to apply. gcp renders these as resource labels, whose law is
    narrower than azure tags': lowercase letters, digits, underscores and
    dashes, beginning and ending alphanumerically, 63 characters at most
    (values may be empty) -- the validation below enforces that ASCII law
    at plan time, the charset every value this platform writes already
    satisfies.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for key, value in var.tags : can(regex("^[a-z0-9_-]{1,63}$", key))
    ])
    error_message = "every key of var.tags must be a legal gcp label key: 1 to 63 lowercase letters, digits, underscores or dashes, beginning and ending alphanumerically."
  }

  validation {
    condition = alltrue([
      for key, value in var.tags : can(regex("^[a-z0-9_-]{0,63}$", value))
    ])
    error_message = "every value of var.tags must be a legal gcp label value: 0 to 63 lowercase letters, digits, underscores or dashes, beginning and ending alphanumerically."
  }
}
