# Network module: exactly the network the remote engine needs, and nothing
# else. See Section 5.6 of repos/spec/devcontainer-platform.md.
#
# Deliberately absent, by design rather than by omission: no NAT gateway, no
# NAT instance, no security group, no ingress rule of any kind. The public
# subnet's public address exists to give the instance a return path for the
# connections it opens itself (SSM agent, package installs, image pulls); it
# is not there to be connected to. Ingress and egress are the security
# module's concern (E5-F1-S3), kept out of this module on purpose so "zero
# ingress rules" is a property a reader can confirm by reading this file.

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, {
    Name = var.name_prefix
  })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public"
  })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, {
    Name = var.name_prefix
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    # The well-known default-route destination, not a deployment-specific
    # address range: every public subnet's default route to its own
    # internet gateway uses this value, so it is not a candidate for
    # externalization the way var.vpc_cidr and var.subnet_cidr are.
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public"
  })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
