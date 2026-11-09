from importlib.resources import files

import yaml
from shifde_contracts.platform_specs.databricks import DatabricksApplicationSpec

ADAPTER_VERSION = "0.1.0"
CLI_VERSION = "1.25.0"


def prefix(spec: DatabricksApplicationSpec) -> str:
    return "" if spec.bundle_root == "." else spec.bundle_root + "/"


def source_bundle(spec: DatabricksApplicationSpec) -> dict:
    return {
        "bundle": {"name": "shifde-app", "databricks_cli_version": f"={CLI_VERSION}"},
        "resources": {
            "apps": {
                spec.app_resource_key: {
                    "name": "shifde-app",
                    "source_code_path": "./app",
                }
            }
        },
        "targets": {spec.bundle_target: {}},
    }


def scaffold_files(spec: DatabricksApplicationSpec) -> dict[str, bytes]:
    resources = files("shifde_databricks").joinpath("templates")
    result = {
        "databricks.yml": yaml.safe_dump(source_bundle(spec), sort_keys=False).encode(),
        "app/app.py": resources.joinpath("app.py.txt").read_bytes(),
        "app/requirements.txt": resources.joinpath("requirements.txt").read_bytes(),
        "app/app.yaml": resources.joinpath("app.yaml").read_bytes(),
    }
    return {prefix(spec) + key: content for key, content in result.items()}


def validate_source(contents: dict[str, bytes], spec: DatabricksApplicationSpec) -> None:
    root = prefix(spec)
    expected = scaffold_files(spec)
    if not expected.keys() <= contents.keys():
        raise ValueError("source is missing required application files")
    # Never forward application-controlled hooks, includes, Python bundle code or targets.
    if yaml.safe_load(contents[root + "databricks.yml"]) != source_bundle(spec):
        raise ValueError("only the managed app-only bundle configuration is supported")
    if contents[root + "app/app.yaml"] != expected[root + "app/app.yaml"]:
        raise ValueError("the managed runtime configuration must be preserved")
    for name in contents:
        local = name.removeprefix(root)
        if name != root + "databricks.yml" and not (
            name.startswith(root) and local.startswith("app/")
        ):
            raise ValueError("source outside the managed app directory is forbidden")
        if any(part.startswith(".") for part in local.split("/")):
            raise ValueError("hidden configuration and secret files are forbidden")


def deployment_bundle(
    spec: DatabricksApplicationSpec, *, host: str, app_name: str, root_path: str, warehouse_id: str
) -> dict:
    return {
        "bundle": {"name": app_name, "databricks_cli_version": f"={CLI_VERSION}"},
        "workspace": {"host": host, "root_path": root_path},
        "resources": {
            "apps": {
                spec.app_resource_key: {
                    "name": app_name,
                    "source_code_path": "./app",
                    "resources": [
                        {
                            "name": "sql-warehouse",
                            "sql_warehouse": {"id": warehouse_id, "permission": "CAN_USE"},
                        }
                    ],
                }
            }
        },
        "targets": {spec.bundle_target: {}},
    }
