"""Explicit local lifecycle driver. Only plan/deploy/status/verify contact Databricks."""

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

from shifde_contracts.builds import BuildArtifact
from shifde_contracts.common import Target
from shifde_contracts.execution import ApplyReceipt
from shifde_contracts.plans import DeploymentPlan
from shifde_contracts.platform_specs.databricks import DatabricksApplicationSpec
from shifde_core.artifact_store import atomic_write, create_archive, read_archive
from shifde_core.hashing import canonical_bytes, digest
from shifde_core.service import LocalDeliveryService
from shifde_databricks.adapter import DatabricksAdapter
from shifde_databricks.clients import DatabricksCLI
from shifde_databricks.source import ADAPTER_VERSION, CLI_VERSION


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path(".shifde/state"))
    parser.add_argument("--cli", default="databricks")
    commands = parser.add_subparsers(dest="action", required=True)
    scaffold = commands.add_parser("scaffold")
    scaffold.add_argument("--output", type=Path, required=True)
    build = commands.add_parser("build")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--application-id", type=UUID, required=True)
    build.add_argument("--output", type=Path, default=Path(".shifde/artifact.json"))
    plan = commands.add_parser("plan")
    plan.add_argument("--artifact", type=Path, default=Path(".shifde/artifact.json"))
    plan.add_argument("--output", type=Path, default=Path(".shifde/plan.json"))
    deploy = commands.add_parser("deploy")
    deploy.add_argument("--artifact", type=Path, default=Path(".shifde/artifact.json"))
    deploy.add_argument("--plan", type=Path, default=Path(".shifde/plan.json"))
    deploy.add_argument("--output", type=Path, default=Path(".shifde/receipt.json"))
    for action in ("status", "verify"):
        command = commands.add_parser(action)
        command.add_argument("--receipt", type=Path, default=Path(".shifde/receipt.json"))
    args = parser.parse_args()
    target = Target.model_validate_json(args.target_file.read_bytes())
    service = LocalDeliveryService(
        args.state,
        tenant_id=target.tenant_id,
        target=target,
        adapter=DatabricksAdapter(cli=DatabricksCLI(args.cli)),
        toolchain_digest=digest(
            {
                "adapter": ADAPTER_VERSION,
                "cli": CLI_VERSION,
                "sdk": "0.163.0",
                "packager": "source/v1",
            }
        ),
    )
    spec = DatabricksApplicationSpec()
    result: BuildArtifact | DeploymentPlan | ApplyReceipt
    if args.action == "scaffold":
        if args.output.exists():
            raise ValueError("output directory already exists; choose a new directory")
        source = await service.scaffold(spec)
        for name, data in read_archive(service.artifacts.read(source, target.tenant_id)).items():
            atomic_write(args.output / name, data)
        print(f"Created source at {args.output}")
        return
    if args.action == "build":
        contents = {}
        for path in args.source.rglob("*"):
            if path.is_symlink():
                raise ValueError("source symlinks are forbidden")
            if path.is_file():
                contents[path.relative_to(args.source).as_posix()] = path.read_bytes()
        source = service.artifacts.put(target.tenant_id, create_archive(contents))
        result = await service.build(args.application_id, spec, source)
    elif args.action == "plan":
        result = await service.plan(BuildArtifact.model_validate_json(args.artifact.read_bytes()))
    elif args.action == "deploy":
        result = await service.deploy(
            BuildArtifact.model_validate_json(args.artifact.read_bytes()),
            DeploymentPlan.model_validate_json(args.plan.read_bytes()),
        )
    else:
        receipt = ApplyReceipt.model_validate_json(args.receipt.read_bytes())
        if args.action == "status":
            print((await service.observe(receipt)).model_dump_json(indent=2))
        else:
            verification = await service.verify(receipt)
            print(verification.model_dump_json(indent=2))
            if not verification.passed:
                raise SystemExit(1)
        return
    atomic_write(args.output, canonical_bytes(result))
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
