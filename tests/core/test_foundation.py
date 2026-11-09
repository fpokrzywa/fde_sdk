import io
import zipfile
from datetime import timedelta
from uuid import uuid4

import pytest


def zip_bytes(names):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in names:
            archive.writestr(name, content)
    return stream.getvalue()


def test_artifact_store_checks_tenant_and_digest(tmp_path):
    from shifde_core.artifact_store import LocalArtifactStore

    store = LocalArtifactStore(tmp_path)
    tenant = uuid4()
    reference = store.put(tenant, zip_bytes([("app/app.py", "print('ok')")]))
    assert store.read(reference, tenant).startswith(b"PK")
    with pytest.raises(PermissionError):
        store.read(reference, uuid4())
    store.path(reference).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest"):
        store.read(reference, tenant)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/outside", "a\\b"])
def test_archive_validation_rejects_unsafe_names(tmp_path, name):
    from shifde_core.artifact_store import LocalArtifactStore

    data = zip_bytes([(name.replace("\\", "/"), "bad")])
    if "\\" in name:
        data = data.replace(name.replace("\\", "/").encode(), name.encode())
    with pytest.raises(ValueError):
        LocalArtifactStore(tmp_path).put(uuid4(), data)


def test_archive_validation_rejects_duplicate_and_symlink_entries(tmp_path):
    from shifde_core.artifact_store import LocalArtifactStore

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "../outside")
    with pytest.raises(ValueError, match="symlink"):
        LocalArtifactStore(tmp_path).put(uuid4(), stream.getvalue())


def test_journal_survives_restart_and_blocks_uncertain_replay(tmp_path):
    from shifde_core.journal import NeedsAttention, StepJournal

    journal = StepJournal(tmp_path / "journal.json", uuid4(), uuid4())
    journal.intent("deploy", {"digest": "a" * 64})
    restarted = StepJournal(journal.path, journal.tenant_id, journal.operation_id)
    with pytest.raises(NeedsAttention):
        restarted.intent("deploy", {"digest": "a" * 64})
    restarted.complete("deploy", {"native_id": "app-123"})
    assert StepJournal(journal.path, journal.tenant_id, journal.operation_id).completed(
        "deploy"
    ) == {"native_id": "app-123"}


def test_journal_refuses_another_tenant_or_operation(tmp_path):
    from shifde_core.journal import StepJournal

    journal = StepJournal(tmp_path / "journal.json", uuid4(), uuid4())
    journal.intent("deploy", {})
    with pytest.raises(PermissionError):
        StepJournal(journal.path, uuid4(), journal.operation_id)


def test_plan_binding_rejects_expired_plan_and_changed_bindings():
    from shifde_contracts.common import Target, utcnow
    from shifde_contracts.plans import DeploymentPlan, EnvironmentSnapshot, PlanStep
    from shifde_core.planning import bindings_digest, seal_plan, validate_plan

    target = Target(
        tenant_id=uuid4(),
        connection_id=uuid4(),
        host="https://workspace.example",
        bindings={"warehouse": "123"},
    )
    plan = seal_plan(
        DeploymentPlan(
            tenant_id=target.tenant_id,
            application_id=uuid4(),
            target_id=target.id,
            connection_revision=1,
            artifact_id=uuid4(),
            artifact_digest="a" * 64,
            spec_digest="b" * 64,
            bindings_digest=bindings_digest(target),
            adapter_version="0.1.0",
            toolchain_digest="c" * 64,
            snapshot=EnvironmentSnapshot(fingerprint="d" * 64),
            steps=[PlanStep(key="deploy", action="create", resource_key="app")],
            expires_at=utcnow() + timedelta(minutes=15),
            digest="0" * 64,
        )
    )
    validate_plan(plan, target)
    with pytest.raises(ValueError, match="binding"):
        validate_plan(plan, target.model_copy(update={"bindings": {"warehouse": "changed"}}))
    with pytest.raises(ValueError, match="expired"):
        validate_plan(plan, target, now=utcnow() + timedelta(minutes=16))
    with pytest.raises(ValueError, match="digest"):
        validate_plan(plan.model_copy(update={"steps": []}), target)


def test_policy_rejects_deletes_and_unknown_effects():
    from shifde_contracts.plans import PlanStep
    from shifde_core.policy import validate_changes

    for action in ["delete", "unknown"]:
        with pytest.raises(PermissionError):
            validate_changes([PlanStep(key="step", action=action, resource_key="app")])


def test_registry_cannot_silently_select_another_adapter_version():
    from shifde_core.registry import AdapterRegistry

    registry = AdapterRegistry()
    registry.register("databricks.app/v1", "0.1.0", object())
    with pytest.raises(LookupError):
        registry.get("databricks.app/v1", "0.2.0")


@pytest.mark.parametrize("name", ["app/app.yaml.", "app/NUL", "app/file ", "app/CON.txt"])
def test_archives_reject_windows_ambiguous_paths(tmp_path, name):
    from shifde_core.artifact_store import LocalArtifactStore

    with pytest.raises(ValueError):
        LocalArtifactStore(tmp_path).put(uuid4(), zip_bytes([(name, "bad")]))
