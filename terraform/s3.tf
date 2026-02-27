resource "aws_s3_bucket" "pipeline_data" {
  bucket = local.s3_bucket_name
  tags   = local.common_tags
}

resource "aws_s3_bucket_versioning" "pipeline_data" {
  bucket = aws_s3_bucket.pipeline_data.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pipeline_data" {
  bucket = aws_s3_bucket.pipeline_data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "pipeline_data" {
  bucket = aws_s3_bucket.pipeline_data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "pipeline_data_bucket_policy" {
  statement {
    sid    = "AllowCloudWatchLogsExportGetBucketAcl"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_region.current.name}.amazonaws.com"]
    }

    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.pipeline_data.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }

  statement {
    sid    = "AllowCloudWatchLogsExportPutObject"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_region.current.name}.amazonaws.com"]
    }

    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.pipeline_data.arn}/manual-exports/*",
      "${aws_s3_bucket.pipeline_data.arn}/cloudwatch-logs/*"
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_s3_bucket_policy" "pipeline_data" {
  bucket = aws_s3_bucket.pipeline_data.id
  policy = data.aws_iam_policy_document.pipeline_data_bucket_policy.json
}

# Create folder prefixes as S3 objects
resource "aws_s3_object" "universe_folder" {
  bucket  = aws_s3_bucket.pipeline_data.id
  key     = "universe/"
  content = ""
  tags    = local.common_tags
}

resource "aws_s3_object" "data_ingress_folder" {
  bucket  = aws_s3_bucket.pipeline_data.id
  key     = "data-ingress/"
  content = ""
  tags    = local.common_tags
}

resource "aws_s3_object" "alpha_model_folder" {
  bucket  = aws_s3_bucket.pipeline_data.id
  key     = "alpha-model/"
  content = ""
  tags    = local.common_tags
}

resource "aws_s3_object" "portfolio_construction_folder" {
  bucket  = aws_s3_bucket.pipeline_data.id
  key     = "portfolio-construction/"
  content = ""
  tags    = local.common_tags
}

resource "aws_s3_object" "execution_model_folder" {
  bucket  = aws_s3_bucket.pipeline_data.id
  key     = "execution-model/"
  content = ""
  tags    = local.common_tags
}
