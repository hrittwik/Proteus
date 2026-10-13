FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir aioquic
WORKDIR /app
COPY server.py quic_server.py client.py mux_client.py learned.py ./
# self-signed cert for the QUIC server (client skips verification in this benchmark)
RUN openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
    -days 365 -subj "/CN=server" 2>/dev/null
