# The entire surface the root composition and its consumers see. No AMI,
# instance type or instance profile is declared or emitted here; those
# belong to the compute module (E5-F1-S4).

output "vpc_id" {
  description = "Identifier of the VPC this module creates."
  value       = aws_vpc.this.id
}

output "subnet_id" {
  description = "Identifier of the public subnet this module creates."
  value       = aws_subnet.public.id
}

output "route_table_id" {
  description = "Identifier of the public route table this module creates, associated with the subnet."
  value       = aws_route_table.public.id
}
