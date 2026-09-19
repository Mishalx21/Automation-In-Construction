FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

# xeokit-convert runs in the container, so the browser receives compact XKT
# geometry instead of parsing a full IFC on Chrome's main thread.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && npm install --prefix /opt/xeokit @xeokit/xeokit-convert@1.1.25 \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY ifcfault/ ./ifcfault/
COPY ifcfaultweb/ ./ifcfaultweb/

# A mounted volume holds user uploads, generated scripts and output models.
VOLUME /app/out

ENV IFCFAULTWEB_DATA_ROOT=/app/out \
    IFCFAULTWEB_MAX_UPLOAD_MB=400 \
    IFCFAULTWEB_XKT_CONVERTER=/opt/xeokit/node_modules/@xeokit/xeokit-convert/convert2xkt.js

EXPOSE 8000
CMD ["uvicorn", "ifcfaultweb.app:app", "--host", "0.0.0.0", "--port", "8000"]
