# Market-data Lambda image: one image, dispatched by the JOB env var.
# Build context is staged by build_and_push.sh (which copies the DataIngressModel
# scripts + utils alongside this Dockerfile before building).
FROM public.ecr.aws/lambda/python:3.12

# Python deps (yfinance/pandas_datareader/openpyxl don't fit the zip+layer limit)
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# DataIngressModel code (staged into the build context by build_and_push.sh)
COPY DataDownloads/ ${LAMBDA_TASK_ROOT}/DataDownloads/
COPY utils/         ${LAMBDA_TASK_ROOT}/utils/

# Dispatcher
COPY lambda_entry.py ${LAMBDA_TASK_ROOT}/

CMD ["lambda_entry.lambda_handler"]
