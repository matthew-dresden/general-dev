# Every option this module supports is declared here as an input. No value a
# caller might reasonably want to change is a literal inside main.tf or
# user-data.yaml; see Section 5.6 of repos/spec/devcontainer-platform.md.
#
# Deliberately absent: no CIDR of any kind, and no variable a data source
# could resolve on the caller's behalf. This module never looks up an AMI --
# var.ami names one explicitly -- and never looks up a subnet or a VPC;
# addressing belongs entirely to the network module (E5-F1-S2).

variable "ami" {
  description = <<-EOT
    AMI identifier the instance boots from (for example
    "ami-0123456789abcdefg"). Required, no default, and never resolved by a
    data source: the module does not look up "the latest Ubuntu AMI" on the
    caller's behalf, so a caller always knows exactly which image an apply
    will launch.
  EOT
  type        = string
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type the instance is launched as (for example "t3.large").
    Required, no default: no instance size is a literal inside this module.
  EOT
  type        = string
}

variable "subnet_id" {
  description = <<-EOT
    Identifier of the subnet the instance is launched into (for example
    "subnet-0123456789abcdef0"). Required, no default: the module never
    looks up a subnet on the caller's behalf, so this is an identifier
    input, not a lookup key. Typically the subnet_id output of the network
    module (E5-F1-S2), but the module accepts any subnet identifier,
    including one this repository did not create.
  EOT
  type        = string
}

variable "security_group_ids" {
  description = <<-EOT
    Identifiers of the security groups attached to the instance's network
    interface (for example ["sg-0123456789abcdef0"]). Required, no default:
    the module never creates or looks up a security group on the caller's
    behalf. Typically the security_group_id output of the security module
    (E5-F1-S3), wrapped in a single-element list, but the module accepts
    any security group identifiers, including ones this repository did not
    create.
  EOT
  type        = list(string)
}

variable "iam_instance_profile_name" {
  description = <<-EOT
    Name of the IAM instance profile attached to the instance (for example
    "devcontainer-remote-prod-instance-profile"). Required, no default: the
    module never creates or looks up an instance profile on the caller's
    behalf, so an apply that omits this input fails at plan time naming it
    rather than launching an instance with no identity to register with
    SSM. Typically the iam_instance_profile_name output of the security
    module (E5-F1-S3).
  EOT
  type        = string
}

variable "name_prefix" {
  description = <<-EOT
    Prefix applied to the Name tag of the instance, its root volume and its
    data volume (for example "devcontainer-remote-prod"). Required, no
    default: no resource name is a literal inside this module.
  EOT
  type        = string
}

variable "root_volume_size_gb" {
  description = <<-EOT
    Size, in GiB, of the encrypted gp3 root volume (for example 50).
    Required, no default: no volume size is a literal inside this module.
  EOT
  type        = number
}

variable "create_data_volume" {
  description = <<-EOT
    Whether to create, attach and mount a second encrypted gp3 data volume
    at var.docker_data_root, which is where the workspace clone-in-volume
    and the rootless daemon's images and containers live. Defaults to true.
    When false, no aws_ebs_volume and no aws_volume_attachment are created,
    and the rendered user data skips the disk-format-and-mount step, so the
    daemon's data-root lives on the root volume instead.
  EOT
  type        = bool
  default     = true
}

variable "data_volume_size_gb" {
  description = <<-EOT
    Size, in GiB, of the encrypted gp3 data volume (for example 100). Read
    only when var.create_data_volume is true; ignored otherwise. Defaults
    to 100 so a caller enabling var.create_data_volume with its default
    true need not also size the volume explicitly.
  EOT
  type        = number
  default     = 100
}

variable "data_volume_device_name" {
  description = <<-EOT
    Device name the data volume is attached under (for example "/dev/sdf"),
    passed to the AWS API's block-device-mapping and read by the guest
    kernel; on Nitro-based instance types the guest re-maps this to an NVMe
    device name, which is why the rendered user data locates the volume by
    scanning for the one unformatted disk rather than trusting this literal
    device name inside the guest. Read only when var.create_data_volume is
    true. Defaults to "/dev/sdf", the device name AWS documents for a
    second EBS volume on a Nitro-based instance.
  EOT
  type        = string
  default     = "/dev/sdf"
}

variable "docker_daemon_user" {
  description = <<-EOT
    Name of the dedicated, unprivileged Linux user the rootless docker
    daemon runs as (for example "dockerd"). This user is never added to a
    docker group on a rootful daemon: docker group membership on a rootful
    daemon is equivalent to host root (Section 1.5), which is exactly what
    D1 (Section 13) rejects. Defaults to "dockerd".
  EOT
  type        = string
  default     = "dockerd"
}

variable "docker_data_root" {
  description = <<-EOT
    Absolute path on the instance the rootless daemon uses as its
    data-root (for example "/mnt/docker-data"), where images, containers
    and volumes live. When var.create_data_volume is true this is also the
    mount point the rendered user data formats and mounts the data volume
    at. Required, no default: the daemon's data-root is settled as an
    input, not a literal, before the user data template is rendered.
  EOT
  type        = string
}

variable "docker_tls_listen_address" {
  description = <<-EOT
    Loopback IPv4 address the rootless daemon's mTLS listener binds to (for
    example "127.0.0.1"). Must be the loopback address: D2 (Section 13)
    reaches the daemon only through an SSM port forward that terminates on
    loopback, and the generated server certificate's SAN is IP:127.0.0.1
    (Section 5.5), so any other address both breaks certificate validation
    and removes the SSM boundary D2 depends on. Defaults to "127.0.0.1".
  EOT
  type        = string
  default     = "127.0.0.1"

  validation {
    condition     = var.docker_tls_listen_address == "127.0.0.1"
    error_message = "var.docker_tls_listen_address (${var.docker_tls_listen_address}) must be the loopback address 127.0.0.1: a daemon listener reachable off loopback removes the SSM boundary D2 (spec Section 13) depends on, and would not match the server certificate's IP:127.0.0.1 SAN (spec Section 5.5)."
  }
}

variable "docker_tls_listen_port" {
  description = <<-EOT
    TCP port the rootless daemon's mTLS listener binds to on loopback (for
    example 2376), which the SSM port forward in E6 targets and nothing
    else reaches. Defaults to 2376, the conventional Docker TLS port.
  EOT
  type        = number
  default     = 2376

  validation {
    condition     = var.docker_tls_listen_port > 0 && var.docker_tls_listen_port <= 65535
    error_message = "var.docker_tls_listen_port (${var.docker_tls_listen_port}) must be a valid TCP port between 1 and 65535."
  }
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
    Base URL of the Docker apt repository the rendered user data adds (for
    example "https://download.docker.com/linux/ubuntu"). Defaults to
    Docker's own public repository; a caller mirroring third-party
    packages internally overrides this rather than the template carrying a
    literal upstream URL.
  EOT
  type        = string
  default     = "https://download.docker.com/linux/ubuntu"
}

variable "docker_repo_channel" {
  description = <<-EOT
    Release channel of the Docker apt repository the rendered user data
    adds (for example "stable"). Defaults to "stable".
  EOT
  type        = string
  default     = "stable"
}

variable "tags" {
  description = <<-EOT
    Common resource tags merged onto the instance, its root volume and its
    data volume, in addition to each resource's own Name tag. Optional;
    defaults to an empty map when the caller has no additional tags to
    apply.
  EOT
  type        = map(string)
  default     = {}
}

variable "disable_api_termination" {
  description = <<-EOT
    Whether the EC2 API refuses to terminate this instance. Type bool,
    defaults to true, which is the posture a long-lived developer box wants:
    an accidental `terraform destroy` or console click cannot take it away.

    An ephemeral deployment that exists to be created, asserted against and
    destroyed in one session sets this false. Without that, `terragrunt
    destroy` cannot remove the instance it just created: the API rejects
    TerminateInstances with OperationNotPermitted, the internet gateway then
    fails to detach because the instance still holds a mapped public address,
    and clearing it takes two out-of-band `aws ec2 modify-instance-attribute`
    calls that no Terragrunt configuration can express. Exposing the flag is
    what makes teardown declarative rather than tribal knowledge, per this
    repository's input-driven-configuration standard.
  EOT
  type        = bool
  default     = true
}

variable "disable_api_stop" {
  description = <<-EOT
    Whether the EC2 API refuses to stop this instance. Type bool, defaults to
    true, for the same reason as var.disable_api_termination.

    Both flags must be cleared to destroy an instance, and this one only
    surfaces after the first is cleared: TerminateInstances reports the
    termination flag, and reports the stop flag only on the next attempt. A
    deployment that sets one and not the other still cannot be destroyed, so
    an ephemeral deployment sets both false.
  EOT
  type        = bool
  default     = true
}
