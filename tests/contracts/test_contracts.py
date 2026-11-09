from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError


def test_native_spec_discriminator_rejects_cross_platform_fields():
    from shifde_contracts.projects import parse_spec

    spec = parse_spec({"kind": "databricks.app/v1", "warehouse_binding": "warehouse"})
    assert spec.app_resource_key == "app"
    with pytest.raises(ValidationError):
        parse_spec({"kind": "databricks.app/v1", "ontology_binding": "ontology"})


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/secret", "a\\b", "a/../../b"])
def test_native_source_paths_cannot_escape_archive(path):
    from shifde_contracts.projects import parse_spec

    with pytest.raises(ValidationError):
        parse_spec({"kind": "databricks.app/v1", "bundle_root": path})


def test_fabric_dependency_graph_rejects_cycles_and_missing_items():
    from shifde_contracts.projects import parse_spec

    for dependencies in [["missing"], ["notebook"]]:
        with pytest.raises(ValidationError):
            parse_spec(
                {
                    "kind": "fabric.solution/v1",
                    "items": [
                        {
                            "key": "notebook",
                            "item_type": "Notebook",
                            "definition_path": "n.json",
                            "depends_on": dependencies,
                        }
                    ],
                }
            )


def test_terminal_operation_cannot_be_resumed():
    from shifde_contracts.operations import Operation

    op = Operation(tenant_id=uuid4(), operation_kind="build", state="succeeded")
    with pytest.raises(ValueError, match="transition"):
        op.transition("running")


def test_operation_requires_timezone_aware_timestamp():
    from shifde_contracts.operations import Operation

    with pytest.raises(ValidationError):
        Operation(tenant_id=uuid4(), operation_kind="build", created_at=datetime(2026, 11, 9))


def test_target_rejects_credentials_and_paths_in_host():
    from shifde_contracts.common import Target

    for host in [
        "http://workspace.example",
        "https://user:password@workspace.example",
        "https://workspace.example/api",
    ]:
        with pytest.raises(ValidationError):
            Target(tenant_id=uuid4(), connection_id=uuid4(), host=host)


def test_capabilities_do_not_treat_unknown_as_supported():
    from shifde_contracts.capabilities import Capability, CapabilityReport

    report = CapabilityReport(
        adapter_version="0.1.0",
        capabilities=[Capability(operation="deploy", status="unknown", reason="probe failed")],
        expires_at=datetime.now(UTC),
    )
    assert not report.supports("deploy")
