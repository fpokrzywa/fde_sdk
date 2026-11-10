# Build dependencies are exported with hashes from uv.lock; no application source is baked in.
FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS build
COPY .tools/executor-wheels/ /wheels/
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /wheels/requirements.txt \
 && /opt/venv/bin/pip install --no-cache-dir --no-deps /wheels/*.whl
RUN python - <<'PY'
import hashlib, io, urllib.request, zipfile
url = "https://github.com/databricks/cli/releases/download/v1.25.0/databricks_cli_1.25.0_linux_amd64.zip"
data = urllib.request.urlopen(url, timeout=60).read()
assert hashlib.sha256(data).hexdigest() == "6c4765fd34a39a08c13e138c9e38ce4b87d9de0114a3db592bc3773820589b3f"
from pathlib import Path
Path('/databricks').write_bytes(zipfile.ZipFile(io.BytesIO(data)).read('databricks'))
PY

FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chmod=0555 /databricks /usr/local/bin/databricks
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["/opt/venv/bin/python", "-m", "shifde_executor.runner"]
