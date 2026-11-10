from uuid import uuid4


def test_operation_can_return_scaffold_archive():
    from shifde_contracts.operations import Operation

    source = {
        "id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "digest": "a" * 64,
        "size": 10,
        "format": "zip",
    }
    op = Operation.model_validate(
        {
            "tenant_id": source["tenant_id"],
            "operation_kind": "scaffold",
            "state": "succeeded",
            "result": {"result_kind": "scaffold", "value": source},
        }
    )
    assert op.result.value.digest == "a" * 64
