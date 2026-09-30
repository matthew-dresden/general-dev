# The entire surface the root composition and the compute module (E5-F1-S4)
# see. No security group rule detail, no policy document and no role trust
# policy is exposed; those are this module's internal implementation.

output "security_group_id" {
  description = "Identifier of the zero-ingress security group this module creates."
  value       = aws_security_group.this.id
}

output "iam_role_name" {
  description = "Name of the IAM role this module creates."
  value       = aws_iam_role.this.name
}

output "iam_role_arn" {
  description = "ARN of the IAM role this module creates."
  value       = aws_iam_role.this.arn
}

output "iam_instance_profile_name" {
  description = "Name of the instance profile this module creates, which the compute module (E5-F1-S4) consumes by name to attach the role to the instance."
  value       = aws_iam_instance_profile.this.name
}
