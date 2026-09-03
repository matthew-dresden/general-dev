# The variable surface every Terragrunt deployment writes to (Section 5.6 of
# repos/spec/devcontainer-platform.md). Every option this composition
# supports is declared here as an input; nothing below is chosen by editing
# this module. Grouped in four sections: the four create toggles, the
# network inputs (creation and replacement), the security inputs (creation
# and replacement), and the compute inputs, followed by the two inputs
# shared across all three submodules.
#
# Each of the four replacement inputs (var.vpc_id, var.subnet_id,
# var.security_group_ids, var.iam_instance_profile_name) carries a pair of
# validation blocks rather than one: the toggle-false direction (the
# replacement is required) and the toggle-true direction (the replacement
# must be left unset). The second direction exists because silently
# ignoring a supplied replacement while also creating a new resource would
# leave the operator believing the identifier they named is in use when a
# new one was created instead.
#
# Every creation-side input a submodule needs when its toggle is true
# (var.vpc_cidr, var.subnet_cidr, var.availability_zone,
# var.egress_cidr_blocks, var.instance_name) carries a validation block
# asserting it is set in that case, so an omitted value fails at plan time
# naming the toggle and the input, rather than surfacing as a null-argument
# error inside a submodule's own validation expression that never names the
# missing root input.
#
# var.create_security_group and var.create_iam_role additionally carry a
# validation requiring them to be set to the same value. The security
# submodule creates the security group, the IAM role and the instance
# profile as a single unit with no internal toggle of its own (E5-F1-S3), so
# setting only one of these two root-level toggles to false would still
# create the infrastructure the false toggle was meant to disable.

# --- Create toggles (Section 5.6) --------------------------------------

variable "create_network" {
  description = <<-EOT
    Whether this deployment creates its own VPC, subnet, internet gateway
    and routes via the network submodule. Type bool, defaults to true. When
    false, no network is created and var.vpc_id and var.subnet_id must both
    be supplied naming the existing network this deployment reuses instead
    (typically the vpc_id and subnet_id outputs of a prior deployment of
    this module, proving the AC-10.11 substitution).
  EOT
  type        = bool
  default     = true
}

variable "create_security_group" {
  description = <<-EOT
    Whether this deployment creates its own zero-ingress security group via
    the security submodule. Type bool, defaults to true. When false, no
    security group is created and var.security_group_ids must be supplied
    naming the existing security groups the instance attaches to instead.
    Must be set to the same value as var.create_iam_role: the security
    submodule creates the security group, the IAM role and the instance
    profile as a single unit with no internal toggle of its own, so the two
    cannot be set independently without either creating an unrequested
    resource or discarding a supplied replacement while the disabled
    resource is still created.
  EOT
  type        = bool
  default     = true

  validation {
    condition     = var.create_security_group == var.create_iam_role
    error_message = "var.create_security_group and var.create_iam_role must be set to the same value: the security submodule creates the security group, IAM role and instance profile as a single unit with no internal toggle of its own, so setting only one of these two to false would still create the infrastructure the false toggle was meant to disable. Set both to true, or set both to false and supply var.security_group_ids and var.iam_instance_profile_name."
  }
}

variable "create_iam_role" {
  description = <<-EOT
    Whether this deployment creates its own IAM role and instance profile
    via the security submodule. Type bool, defaults to true. When false, no
    role or profile is created and var.iam_instance_profile_name must be
    supplied naming the existing instance profile the instance attaches to
    instead. Must be set to the same value as var.create_security_group: the
    security submodule creates the security group, the IAM role and the
    instance profile as a single unit with no internal toggle of its own, so
    the two cannot be set independently without either creating an
    unrequested resource or discarding a supplied replacement while the
    disabled resource is still created.
  EOT
  type        = bool
  default     = true
}

variable "create_data_volume" {
  description = <<-EOT
    Whether the compute submodule creates, attaches and mounts a second
    encrypted gp3 data volume. Type bool, defaults to true. When false, no
    data volume is created or passed to the compute submodule and the
    instance is left with its root volume only; no replacement input is
    required because there is no identifier to substitute, only a volume
    the instance does without.
  EOT
  type        = bool
  default     = true
}

# --- Network inputs (Section 5.6) ---------------------------------------

variable "vpc_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the VPC the network submodule creates (for example
    "10.0.0.0/16"). Type string, defaults to null (unset). Required when
    var.create_network is true; ignored otherwise.
  EOT
  type        = string
  default     = null

  validation {
    condition     = !var.create_network || var.vpc_cidr != null
    error_message = "var.vpc_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the VPC the network submodule creates."
  }
}

variable "subnet_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the public subnet the network submodule creates (for
    example "10.0.1.0/24"). Type string, defaults to null (unset). Required
    when var.create_network is true; ignored otherwise. Must be fully
    contained inside var.vpc_cidr, enforced by the network submodule's own
    validation.
  EOT
  type        = string
  default     = null

  validation {
    condition     = !var.create_network || var.subnet_cidr != null
    error_message = "var.subnet_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the public subnet the network submodule creates."
  }
}

variable "availability_zone" {
  description = <<-EOT
    AWS availability zone the network submodule's public subnet is created
    in (for example "us-east-1a"). Type string, defaults to null (unset).
    Required when var.create_network is true; ignored otherwise.
  EOT
  type        = string
  default     = null

  validation {
    condition     = !var.create_network || var.availability_zone != null
    error_message = "var.availability_zone is required when var.create_network is true: supply the AWS availability zone the network submodule's public subnet is created in."
  }
}

variable "vpc_id" {
  description = <<-EOT
    Identifier of an existing VPC this deployment reuses (for example
    "vpc-0123456789abcdef0"). Type string, defaults to null (unset).
    Required whenever var.create_network is false, regardless of the
    security toggles. It is consumed by the security submodule only when
    that submodule is instantiated (var.create_security_group or
    var.create_iam_role is true); when both are false, module.security has
    count = 0 and the value is not passed to anything. Must be left unset
    when var.create_network is true: this deployment creates its own VPC in
    that case, and supplying an existing one at the same time is ambiguous.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_network || var.vpc_id != null
    error_message = "var.vpc_id is required when var.create_network is false: supply the identifier of the existing VPC this deployment reuses."
  }

  validation {
    condition     = !var.create_network || var.vpc_id == null
    error_message = "var.vpc_id must be left unset when var.create_network is true: this deployment creates its own VPC, and also supplying var.vpc_id would leave it ambiguous which network is in use. Set var.create_network to false to reuse var.vpc_id instead."
  }
}

variable "subnet_id" {
  description = <<-EOT
    Identifier of an existing subnet this deployment reuses (for example
    "subnet-0123456789abcdef0"). Type string, defaults to null (unset).
    Required when var.create_network is false, in which case it is passed
    straight to the compute submodule. Must be left unset when
    var.create_network is true: this deployment creates its own subnet in
    that case, and supplying an existing one at the same time is ambiguous.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_network || var.subnet_id != null
    error_message = "var.subnet_id is required when var.create_network is false: supply the identifier of the existing subnet this deployment reuses."
  }

  validation {
    condition     = !var.create_network || var.subnet_id == null
    error_message = "var.subnet_id must be left unset when var.create_network is true: this deployment creates its own subnet, and also supplying var.subnet_id would leave it ambiguous which network is in use. Set var.create_network to false to reuse var.subnet_id instead."
  }
}

# --- Security inputs (Section 5.6) --------------------------------------

variable "egress_cidr_blocks" {
  description = <<-EOT
    IPv4 CIDR blocks the security submodule's egress rule permits traffic to
    (for example ["0.0.0.0/0"]). Type list(string), defaults to null
    (unset). Required and must not be empty when var.create_security_group
    or var.create_iam_role is true, since the security submodule creates the
    security group alongside the IAM role and instance profile as a single
    unit; ignored when both are false.
  EOT
  type        = list(string)
  default     = null

  validation {
    condition     = !(var.create_security_group || var.create_iam_role) || (var.egress_cidr_blocks != null && length(var.egress_cidr_blocks) > 0)
    error_message = "var.egress_cidr_blocks is required and must not be empty when var.create_security_group or var.create_iam_role is true: supply the IPv4 CIDR blocks the security submodule's egress rule permits traffic to."
  }
}

variable "instance_name" {
  description = <<-EOT
    Name of the instance this deployment provisions (for example
    "devcontainer-remote-prod"), keying the instance-scoped tier of the
    two-tier secret model the security submodule's inline IAM policy scopes
    read access to. Type string, defaults to null (unset). Required when
    var.create_security_group or var.create_iam_role is true; ignored when
    both are false.
  EOT
  type        = string
  default     = null

  validation {
    condition     = !(var.create_security_group || var.create_iam_role) || var.instance_name != null
    error_message = "var.instance_name is required when var.create_security_group or var.create_iam_role is true: supply the name of the instance this deployment provisions, used to scope the security submodule's inline IAM policy to this instance's Parameter Store prefix."
  }
}

variable "security_group_ids" {
  description = <<-EOT
    Identifiers of existing security groups this deployment reuses (for
    example ["sg-0123456789abcdef0"]). Type list(string), defaults to null
    (unset). Required and must not be empty when var.create_security_group
    is false, in which case it is passed straight to the compute submodule.
    Must be left unset when var.create_security_group is true: this
    deployment creates its own security group in that case, and supplying
    existing ones at the same time is ambiguous.
  EOT
  type        = list(string)
  default     = null

  validation {
    condition     = var.create_security_group || (var.security_group_ids != null && length(var.security_group_ids) > 0)
    error_message = "var.security_group_ids is required and must not be empty when var.create_security_group is false: supply the identifiers of the existing security groups this deployment reuses."
  }

  validation {
    condition     = !var.create_security_group || var.security_group_ids == null
    error_message = "var.security_group_ids must be left unset when var.create_security_group is true: this deployment creates its own security group, and also supplying existing ones would leave it ambiguous which group is in use. Set var.create_security_group to false to reuse var.security_group_ids instead."
  }
}

variable "iam_instance_profile_name" {
  description = <<-EOT
    Name of an existing IAM instance profile this deployment reuses (for
    example "devcontainer-remote-prod-instance-profile"). Type string,
    defaults to null (unset). Required when var.create_iam_role is false, in
    which case it is passed straight to the compute submodule. Must be left
    unset when var.create_iam_role is true: this deployment creates its own
    role and profile in that case, and supplying an existing one at the same
    time is ambiguous.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.create_iam_role || var.iam_instance_profile_name != null
    error_message = "var.iam_instance_profile_name is required when var.create_iam_role is false: supply the name of the existing instance profile this deployment reuses."
  }

  validation {
    condition     = !var.create_iam_role || var.iam_instance_profile_name == null
    error_message = "var.iam_instance_profile_name must be left unset when var.create_iam_role is true: this deployment creates its own role and profile, and also supplying an existing one would leave it ambiguous which profile is in use. Set var.create_iam_role to false to reuse var.iam_instance_profile_name instead."
  }
}

# --- Compute inputs -------------------------------------------------------

variable "ami" {
  description = <<-EOT
    AMI identifier the instance boots from (for example
    "ami-0123456789abcdefg"). Type string, no default: always required,
    regardless of any toggle.
  EOT
  type        = string
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type the instance is launched as (for example "t3.large").
    Type string, no default: always required, regardless of any toggle.
  EOT
  type        = string
}

variable "root_volume_size_gb" {
  description = <<-EOT
    Size, in GiB, of the encrypted gp3 root volume (for example 50). Type
    number, no default: always required, regardless of any toggle.
  EOT
  type        = number
}

variable "data_volume_size_gb" {
  description = <<-EOT
    Size, in GiB, of the encrypted gp3 data volume (for example 100). Type
    number, defaults to 100. Read only when var.create_data_volume is true;
    ignored otherwise.
  EOT
  type        = number
  default     = 100
}

variable "data_volume_device_name" {
  description = <<-EOT
    Device name the data volume is attached under (for example "/dev/sdf").
    Type string, defaults to "/dev/sdf", the device name AWS documents for a
    second EBS volume on a Nitro-based instance. Read only when
    var.create_data_volume is true; ignored otherwise.
  EOT
  type        = string
  default     = "/dev/sdf"
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

variable "aws_cli_installer_url" {
  description = <<-EOT
    URL of the AWS CLI v2 installer archive the rendered user data downloads.
    The instance needs the CLI to read its own TLS material and secrets from
    Parameter Store through its instance role: the security module grants that
    role ssm:GetParameter on /devcontainer/<instance>/*, and nothing on a bare
    Ubuntu host can exercise it (Ubuntu 24.04 offers no awscli apt candidate).
    Defaults to Amazon's own published archive; a caller mirroring third-party
    downloads internally overrides this rather than the template carrying a
    literal upstream URL.
  EOT
  type        = string
  default     = "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
}

variable "docker_repo_base_url" {
  description = <<-EOT
    Base URL of the Docker apt repository the compute submodule's user data
    adds (for example "https://download.docker.com/linux/ubuntu"). Type
    string, defaults to Docker's own public repository.
  EOT
  type        = string
  default     = "https://download.docker.com/linux/ubuntu"
}

variable "docker_repo_channel" {
  description = <<-EOT
    Release channel of the Docker apt repository the compute submodule's
    user data adds (for example "stable"). Type string, defaults to
    "stable".
  EOT
  type        = string
  default     = "stable"
}

# --- Shared across all three submodules ------------------------------------

variable "name_prefix" {
  description = <<-EOT
    Prefix applied to the Name tag of every resource this composition
    creates, across all three submodules (for example
    "devcontainer-remote-prod"). Type string, no default: always required,
    regardless of any toggle.
  EOT
  type        = string
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

variable "disable_api_termination" {
  description = <<-EOT
    Whether the EC2 API refuses to terminate the instance. Type bool,
    defaults to true. Forwarded to the compute submodule, which documents why
    both this and var.disable_api_stop must be false for an ephemeral
    deployment to be destroyable by `terragrunt destroy` alone.
  EOT
  type        = bool
  default     = true
}

variable "disable_api_stop" {
  description = <<-EOT
    Whether the EC2 API refuses to stop the instance. Type bool, defaults to
    true. Forwarded to the compute submodule. Both flags must be false for a
    destroy to succeed; setting only one leaves the instance undestroyable.
  EOT
  type        = bool
  default     = true
}
