# Outputs
output "s3_bucket_name" {
  description = "Name of the S3 bucket for pipeline data"
  value       = aws_s3_bucket.pipeline_data.id
}

output "s3_bucket_arn" {
  description = "ARN of the S3 bucket for pipeline data"
  value       = aws_s3_bucket.pipeline_data.arn
}

output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "IDs of the private subnets"
  value       = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  description = "IDs of the public subnets"
  value       = aws_subnet.public[*].id
}

output "ecs_cluster_arn" {
  description = "ARN of the ECS cluster"
  value       = aws_ecs_cluster.main.arn
}

output "ecs_cluster_name" {
  description = "Name of the ECS cluster"
  value       = aws_ecs_cluster.main.name
}

output "step_function_arn" {
  description = "ARN of the Step Functions state machine"
  value       = aws_sfn_state_machine.pipeline.arn
}

output "step_function_name" {
  description = "Name of the Step Functions state machine"
  value       = aws_sfn_state_machine.pipeline.name
}

output "ecs_security_group_id" {
  description = "ID of the ECS security group"
  value       = aws_security_group.ecs.id
}

# SSM Parameters for child infrastructure references
resource "aws_ssm_parameter" "s3_bucket_name" {
  name  = "/${var.project_name}/${var.environment}/s3_bucket_name"
  type  = "String"
  value = aws_s3_bucket.pipeline_data.id
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "s3_bucket_arn" {
  name  = "/${var.project_name}/${var.environment}/s3_bucket_arn"
  type  = "String"
  value = aws_s3_bucket.pipeline_data.arn
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "vpc_id" {
  name  = "/${var.project_name}/${var.environment}/vpc_id"
  type  = "String"
  value = aws_vpc.main.id
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "private_subnet_ids" {
  name  = "/${var.project_name}/${var.environment}/private_subnet_ids"
  type  = "String"
  value = join(",", aws_subnet.private[*].id)
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "public_subnet_ids" {
  name  = "/${var.project_name}/${var.environment}/public_subnet_ids"
  type  = "String"
  value = join(",", aws_subnet.public[*].id)
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "ecs_cluster_arn" {
  name  = "/${var.project_name}/${var.environment}/ecs_cluster_arn"
  type  = "String"
  value = aws_ecs_cluster.main.arn
  tags  = local.common_tags
}

resource "aws_ssm_parameter" "ecs_security_group_id" {
  name  = "/${var.project_name}/${var.environment}/ecs_security_group_id"
  type  = "String"
  value = aws_security_group.ecs.id
  tags  = local.common_tags
}
