variable "project_name" {
  description = "Name of the project"
  type        = string
  default     = "euclidean"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = ""
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

variable "operator_iam_username" {
  description = "IAM username to grant Step Functions execution read permissions for operational log exports"
  type        = string
  default     = "apodrez"
}

variable "github_cicd_iam_username" {
  description = "IAM username used by GitHub Actions CI/CD to push images and register ECS task definitions"
  type        = string
  default     = "github-cicd"
}

variable "notification_phone_number" {
  description = "E.164 phone number for pipeline SMS alerts (e.g. +15551234567). Leave empty to skip SMS subscription. UNUSED as of the email switch below — SMS requires toll-free/10DLC carrier registration (company verification), which doesn't fit a single-recipient personal alert; kept here in case SMS is revisited later."
  type        = string
  sensitive   = true
  default     = ""
}

variable "notification_email" {
  description = "Email address for pipeline alerts. Leave empty to skip the email subscription. AWS sends a confirmation link to this address after apply — it must be clicked before notifications start flowing."
  type        = string
  sensitive   = true
  default     = ""
}

variable "edgar_identity" {
  description = "EDGAR User-Agent identity string for SEC requests (e.g. 'Org Name contact@example.com')"
  type        = string
  sensitive   = true
  default     = ""
}

variable "openrouter_api_key" {
  description = "OpenRouter API key for Kimi K2.6 strong-model fallback"
  type        = string
  sensitive   = true
  default     = ""
}

