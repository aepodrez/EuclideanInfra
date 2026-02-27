variable "project_name" {
  description = "Name of the project"
  type        = string
  default     = "euclidean"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "s3_bucket_name" {
  description = "Name of the S3 bucket for pipeline data. If empty, will be auto-generated using pattern: project-pipeline-env-accountid"
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR block for VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets"
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets"
  type        = list(string)
  default     = ["10.0.101.0/24", "10.0.102.0/24"]
}

variable "enable_cloudwatch_logs_archive_to_s3" {
  description = "Enable account-level CloudWatch Logs subscription to archive all logs to S3 through Firehose"
  type        = bool
  default     = true
}

variable "cloudwatch_logs_s3_prefix" {
  description = "Top-level S3 prefix where CloudWatch Logs archives are written"
  type        = string
  default     = "cloudwatch-logs"
}
