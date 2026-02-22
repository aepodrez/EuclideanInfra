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
