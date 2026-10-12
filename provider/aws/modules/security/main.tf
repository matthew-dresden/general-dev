# Security module: the zero-ingress security group the instance sits
# behind, the IAM role it assumes, and the instance profile that carries the
# role onto the instance. See Section 5.6 of
# repos/spec/devcontainer-platform.md.
#
# Deliberately absent, by design rather than by omission: no ingress rule of
# any kind. The transport is an SSM port forward (D2, Section 13): the
# session is established outbound by the SSM agent already running on the
# instance, and the docker daemon's TLS listener never leaves loopback, so
# nothing needs to reach this instance from outside. Revocation (D5) is
# performed by removing ssm:StartSession from the human/developer principal
# that opens the port forward (spec Section 3.6.2, "Who may open the
# tunnel"), not from the role below: that role trusts only ec2.amazonaws.com
# and never grants ssm:StartSession to begin with, so there is nothing on it
# to remove. What this role's narrow scope buys instead is blast-radius
# containment: an instance compromised while a session is open can read only
# its own and the shared Parameter Store prefix, and can write no parameter
# at all.
#
# data.aws_caller_identity, data.aws_region and data.aws_partition resolve
# the calling account, region and partition so the inline policy's and the
# managed-policy attachment's resource ARNs are exact rather than
# wildcarded, and so validate and plan succeed unchanged against a
# GovCloud or China partition rather than only the commercial partition.
# This module's own ARNs carry no hardcoded partition literal; it does not
# claim the trust policy's "ec2.amazonaws.com" principal is itself
# partition-portable (China uses "ec2.amazonaws.com.cn"), which is out of
# scope for AC-FUNC-003 (EC2-only trust) as written. None of the three data
# sources looks up a VPC, a subnet or an AMI, so this does not reintroduce
# the lookup AC-FUNC-006 rules out.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

resource "aws_security_group" "this" {
  vpc_id      = var.vpc_id
  description = "Zero-ingress security group for the remote devcontainer instance; see Section 3.6 of repos/spec/devcontainer-platform.md."

  egress {
    description = "Outbound only, to the destinations the caller supplies. Narrowing var.egress_cidr_blocks below what the instance needs to reach outbound costs it that connectivity: the SSM endpoints, the package repositories, and the container registry."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = var.egress_cidr_blocks
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-instance-sg"
  })
}

resource "aws_iam_role" "this" {
  name = "${var.name_prefix}-instance-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EC2AssumeRoleOnly"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-instance-role"
  })
}

resource "aws_iam_role_policy_attachment" "ssm_managed_instance_core" {
  role = aws_iam_role.this.name
  # AWS managed policy the SSM agent requires to register the instance and
  # accept a port-forwarding session; confirmed against AWS Systems Manager
  # documentation (setup-instance-permissions.html) rather than memory.
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "parameter_store_read" {
  name = "${var.name_prefix}-parameter-store-read"
  role = aws_iam_role.this.id

  # Two statements, one per tier of the two-tier secret model in Section 5.3:
  # the instance's own prefix, keyed by var.instance_name, and the shared
  # prefix every instance reads. Each Resource is the exact parameter-path
  # prefix for its tier; the trailing "/*" matches everything under that one
  # prefix and nothing outside it, which is what makes D12's two-tier
  # scoping an IAM boundary rather than a convention. Neither statement uses
  # "Resource": "*" or any prefix wider than these two.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InstanceScopedParameterRead"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/devcontainer/${var.instance_name}/*"
      },
      {
        Sid    = "SharedParameterRead"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/devcontainer/shared/*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "this" {
  name = "${var.name_prefix}-instance-profile"
  role = aws_iam_role.this.name

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-instance-profile"
  })
}
