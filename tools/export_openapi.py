"""Export the HTTP contract without opening connections or starting the service."""

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import rsa
from shifde_api.main import create_app
from shifde_core.artifact_store import LocalArtifactStore
from shifde_hosted.auth import JWTVerifier
from shifde_hosted.persistence.database import Database
from shifde_hosted.service import ApplicationService


def export_openapi():
    database = Database("postgresql+psycopg://schema@127.0.0.1/schema")
    try:
        service = ApplicationService(
            database, LocalArtifactStore(Path(".shifde/schema-export")), credential_bindings={}
        )
        verifier = JWTVerifier(
            issuer="https://issuer.example",
            audience="https://delivery.example",
            keys={
                "schema": rsa.generate_private_key(
                    public_exponent=65537, key_size=2048
                ).public_key()
            },
        )
        document = create_app(service, verifier, public_url="https://delivery.example").openapi()
        Path("schemas/openapi.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
    finally:
        database.close()


if __name__ == "__main__":
    export_openapi()
