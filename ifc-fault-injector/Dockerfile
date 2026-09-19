FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

COPY ifcfault/ ./ifcfault/
COPY ifcfaultweb/ ./ifcfaultweb/

# A mounted volume holds user uploads, generated scripts and output models.
VOLUME /app/out

ENV IFCFAULTWEB_DATA_ROOT=/app/out \
    IFCFAULTWEB_MAX_UPLOAD_MB=400

EXPOSE 8000
CMD ["uvicorn", "ifcfaultweb.app:app", "--host", "0.0.0.0", "--port", "8000"]
