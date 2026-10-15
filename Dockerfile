ARG POSTGRES_MAJOR=16
FROM postgres:${POSTGRES_MAJOR}-bookworm

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && chown -R postgres:postgres /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY src ./src

USER postgres
ENTRYPOINT ["python3", "-m", "src.backup"]
CMD []
