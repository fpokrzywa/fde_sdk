import re
from pathlib import PurePosixPath

import yaml
from shifde_contracts.platform_specs.snowflake import SnowflakeApplicationSpec
from shifde_core.hashing import digest
from shifde_core.native import SNOWFLAKE_PROFILE

ADAPTER_VERSION = SNOWFLAKE_PROFILE["adapter"]
CLI_VERSION = SNOWFLAKE_PROFILE["cli"]
CONNECTOR_VERSION = SNOWFLAKE_PROFILE["sdk"]
TOOLCHAIN_DIGEST = digest(SNOWFLAKE_PROFILE)
TERMINAL_STEP = "application-access"


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,254}", value):
        raise ValueError("invalid Snowflake native identifier")
    return value.upper()


def quoted(value: str) -> str:
    return '"' + identifier(value) + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def prefix(spec):
    return "" if spec.project_root == "." else spec.project_root + "/"


def scaffold_files(spec: SnowflakeApplicationSpec) -> dict[str, bytes]:
    version = identifier(spec.release_version)
    manifest = {
        "manifest_version": 1,
        "version": {"name": version, "label": "SHIFDE " + version},
        "artifacts": {"setup_script": "setup.sql", "default_streamlit": "APP_PUBLIC.UI"},
    }
    setup = """CREATE APPLICATION ROLE IF NOT EXISTS APP_USER;
CREATE OR ALTER VERSIONED SCHEMA APP_PUBLIC;
CREATE OR REPLACE SECURE FUNCTION APP_PUBLIC.HEALTH() RETURNS VARCHAR LANGUAGE SQL AS $$ SELECT 'OK' $$;
CREATE OR REPLACE STREAMLIT APP_PUBLIC.UI FROM '/ui' MAIN_FILE = '/streamlit_app.py';
GRANT USAGE ON SCHEMA APP_PUBLIC TO APPLICATION ROLE APP_USER;
GRANT USAGE ON FUNCTION APP_PUBLIC.HEALTH() TO APPLICATION ROLE APP_USER;
GRANT USAGE ON STREAMLIT APP_PUBLIC.UI TO APPLICATION ROLE APP_USER;
"""
    files = {
        "manifest.yml": yaml.safe_dump(manifest, sort_keys=False).encode(),
        "setup.sql": setup.encode(),
        "ui/environment.yml": b"name: shifde_native_app\nchannels:\n  - snowflake\ndependencies:\n  - python=3.11\n  - streamlit\n  - snowflake-snowpark-python\n",
        "ui/streamlit_app.py": b"import streamlit as st\nfrom snowflake.snowpark.context import get_active_session\n\nst.title('SHIFDE Native App')\nsession = get_active_session()\nst.write('Native application ready')\n",
    }
    return {prefix(spec) + name: data for name, data in files.items()}


def validate_source(files: dict[str, bytes], spec):
    expected = scaffold_files(spec)
    managed = {prefix(spec) + key for key in ("manifest.yml", "setup.sql", "ui/environment.yml")}
    if not expected.keys() <= files.keys() or any(
        files[name] != expected[name] for name in managed
    ):
        raise ValueError("invalid managed Native App source")
    for name in files:
        local = name.removeprefix(prefix(spec))
        if name in managed or local == "README.md":
            continue
        if (
            not name.startswith(prefix(spec))
            or not local.startswith("ui/")
            or PurePosixPath(local).suffix.lower()
            not in {".py", ".png", ".jpg", ".svg", ".css", ".json", ".csv"}
        ):
            raise ValueError("unsupported source file; local hooks/configuration are forbidden")


def names_for(spec, target):
    return {
        key: identifier(target.bindings[binding])
        for key, binding in {
            "package": spec.package_binding,
            "application": spec.application_binding,
            "warehouse": spec.warehouse_binding,
        }.items()
    }


def native_id(kind, name, created, account):
    return f"snowflake:{kind}:{name}:" + digest([account, name, str(created)])
