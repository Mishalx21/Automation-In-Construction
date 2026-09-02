# bnbc-web — runs the ACCEPTED BNBC rule checkers behind an HTTP API.
# No LLM: the agentic generation pipeline stays offline; this image carries
# only ifcopenshell + FastAPI.
# Build:  docker build -t bnbc-web .
# Run:    docker run -p 8001:8000 -v bnbcweb_data:/app/out bnbc-web
FROM python:3.12-slim

WORKDIR /app

COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# The engine pieces the web layer actually needs: the shared helper library,
# the accepted checkers, and the web package itself. NOT copied: rulebook/,
# rules/, models/, the agentic pipelines, the LLM config. Each checker dir is
# copied individually (Docker cannot glob-expand directory COPYs); add new
# accepted checkers here as they land.
COPY ifc_helpers/ ./ifc_helpers/
COPY rule-1.Egress_door_width_v1/ ./rule-1.Egress_door_width_v1/
COPY rule-2.Stair_riser_tread_v1/ ./rule-2.Stair_riser_tread_v1/
COPY rule-3.Fire_rating_v1/ ./rule-3.Fire_rating_v1/
COPY rule-4.Door_maneuvering_clearance_v1/ ./rule-4.Door_maneuvering_clearance_v1/
COPY rule-5.Dead_end_corridor_v1/ ./rule-5.Dead_end_corridor_v1/
COPY rule-6.Beam_span_depth_v1/ ./rule-6.Beam_span_depth_v1/
COPY rule-7.Column_dimension_slenderness_v1/ ./rule-7.Column_dimension_slenderness_v1/
COPY rule-8.Slab_thickness_v1/ ./rule-8.Slab_thickness_v1/
COPY rule-9.Floating_column_v1/ ./rule-9.Floating_column_v1/
COPY rule-10.Soft_storey_v1/ ./rule-10.Soft_storey_v1/
COPY bnbcweb/ ./bnbcweb/

# Uploads live here; mount a volume to persist them.
VOLUME /app/out

ENV BNBCWEB_DATA_ROOT=/app/out/bnbcweb \
    BNBCWEB_MAX_UPLOAD_MB=400

EXPOSE 8000
CMD ["uvicorn", "bnbcweb.app:app", "--host", "0.0.0.0", "--port", "8000"]
