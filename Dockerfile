# Pin PYTHON_IMAGE to an approved digest in the release pipeline.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=production TZ=Asia/Seoul
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY api /app/api
COPY ml/features.py ml/scoring_util.py /app/ml/
COPY ml/models/*.pkl /app/ml/models/
COPY data/user_baseline.json /app/data/user_baseline.json
COPY web /app/web
COPY tools/manage.py /app/tools/manage.py
COPY deploy/start.py /app/deploy/start.py
COPY deploy/rds-ca.pem /run/certs/rds.pem
USER 10001
EXPOSE 8000
CMD ["python", "deploy/start.py"]
