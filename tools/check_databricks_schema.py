"""Validate the generated manifest against an official CLI release's bundle schema."""

import argparse
import json
from pathlib import Path

import regex
from jsonschema import Draft202012Validator, ValidationError, validators
from shifde_contracts.platform_specs.databricks import DatabricksApplicationSpec
from shifde_databricks.source import deployment_bundle


def unicode_pattern(validator, pattern, instance, schema):
    # Databricks' Go schema uses Unicode property escapes unsupported by Python's re.
    if isinstance(instance, str) and not regex.search(pattern, instance):
        yield ValidationError("value does not match vendor pattern")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("schema", type=Path)
    args = parser.parse_args()
    schema = json.loads(args.schema.read_bytes())
    vendor_validator = validators.extend(Draft202012Validator, {"pattern": unicode_pattern})
    bundle = deployment_bundle(
        DatabricksApplicationSpec(),
        host="https://workspace.example",
        app_name="shifde-example",
        root_path="/Workspace/Shared/.shifde/example",
        warehouse_id="abc123",
    )
    vendor_validator(schema).validate(bundle)
    print("Generated deployment manifest matches the official Databricks bundle schema")
