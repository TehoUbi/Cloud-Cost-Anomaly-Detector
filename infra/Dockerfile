# syntax=docker/dockerfile:1
#
# Lambda container image for the scheduled poll, driven by EventBridge.
#
# A container image rather than a zip: scikit-learn, numpy and asyncpg together
# are comfortably past the 250 MB unzipped limit for a layer-based deploy.
#
#   docker build -f infra/Dockerfile -t sentinel-poller .
#   docker run --rm -e COST_PROVIDER=synthetic sentinel-poller \
#     infra.lambda_handler.handler

FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY src ${LAMBDA_TASK_ROOT}/src
COPY scripts ${LAMBDA_TASK_ROOT}/scripts
COPY infra ${LAMBDA_TASK_ROOT}/infra
COPY migrations ${LAMBDA_TASK_ROOT}/migrations
COPY alembic.ini ${LAMBDA_TASK_ROOT}/

# Imports numpy and scikit-learn during the init phase, which Lambda does not
# bill and does not count against the invocation timeout. Deferring them to the
# first request instead adds seconds to every cold start.
ENV SENTINEL_WARM_IMPORTS=1

CMD ["infra.lambda_handler.handler"]
