FROM ghcr.io/home-assistant/base:latest

LABEL \
  io.hass.version="0.1.0" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64|armv7|armhf|i386"

RUN apk add --no-cache python3

COPY run.sh /run.sh
COPY app.py /app/app.py
COPY web /app/web
RUN chmod a+x /run.sh

CMD ["/run.sh"]
