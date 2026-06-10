#!/usr/bin/env bash
#
# Build and push the market-data Lambda container image to ECR.
#
# Run this ONCE before `terraform apply` (the Lambda resources reference :latest),
# and again whenever the scripts or dispatcher change.
#
# Prereqs: docker, awscli, and the ECR repo created by terraform
#   (terraform apply -target=aws_ecr_repository.market_data  the first time).
#
# Usage:
#   ./build_and_push.sh [REGION] [ACCOUNT_ID]
set -euo pipefail

REGION="${1:-us-east-1}"
ACCOUNT_ID="${2:-$(aws sts get-caller-identity --query Account --output text)}"
REPO="euclidean-market-data"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:latest"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# DataIngressModel is a sibling submodule: EuclideanInfra/lambdas/market_data -> ../../../DataIngressModel
DIM="$(cd "${HERE}/../../../DataIngressModel" && pwd)"

echo "==> Staging DataIngressModel code into build context"
rm -rf "${HERE}/DataDownloads" "${HERE}/utils"
mkdir -p "${HERE}/DataDownloads" "${HERE}/utils"
# Only the 9 scripts we run + their shared utils (keep the image small)
for f in CRSPDaily CRSPMonthly CRSPDistributions FamaFrenchDaily FamaFrenchMonthly \
         MarketReturns TreasuryBill3M VIX IPODates; do
  cp "${DIM}/DataDownloads/${f}.py" "${HERE}/DataDownloads/"
done
cp "${DIM}/utils/"*.py "${HERE}/utils/"

echo "==> Logging in to ECR (${REGION})"
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "==> Building image (linux/amd64)"
docker build --platform linux/amd64 -t "${IMAGE_URI}" "${HERE}"

echo "==> Pushing ${IMAGE_URI}"
docker push "${IMAGE_URI}"

echo "==> Cleaning staged code"
rm -rf "${HERE}/DataDownloads" "${HERE}/utils"

echo "Done. Image: ${IMAGE_URI}"
echo "If Lambdas already exist, force them to pick up the new image with:"
echo "  for j in vix crsp-daily crsp-monthly crsp-distributions fama-french-daily \\"
echo "           fama-french-monthly market-returns treasury-bill-3m ipo-dates; do"
echo "    aws lambda update-function-code --function-name euclidean-md-\$j --image-uri ${IMAGE_URI} --region ${REGION}; done"
