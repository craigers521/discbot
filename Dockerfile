# botalert — personal stock checker (research.md §9).
#
# Single stage, pure Python: nothing to build for the pinned dependency
# set (flask, discord.py, python-dotenv, curl_cffi — the last a
# self-contained manylinux wheel bundling libcurl). Runtime deps only —
# dev deps (pytest, mypy) live in requirements-dev.txt and never enter
# the image.
FROM python:3.15-slim

# Install the pinned runtime deps first (stable layer).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The application package (includes the static dashboard, botalert/static/).
COPY botalert/ /app/botalert/

# Non-root runtime user owning the state volume.
RUN useradd -m botalert \
    && mkdir -p /data \
    && chown botalert:botalert /data
VOLUME /data
ENV STATE_FILE=/data/state.json PORT=8080

USER botalert
WORKDIR /app
EXPOSE 8080
CMD ["python", "-m", "botalert"]
