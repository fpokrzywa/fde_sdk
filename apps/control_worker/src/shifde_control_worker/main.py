import argparse
import asyncio

from shifde_api.main import service_from_settings
from shifde_hosted.config import Settings
from shifde_hosted.executor import OperationExecutor
from shifde_hosted.gateway import ExecutionGateway
from shifde_hosted.remote_adapter import GatewayAdapter
from shifde_hosted.runtime import TOOLCHAIN_DIGEST
from temporalio.client import Client, TLSConfig
from temporalio.worker import Worker

from shifde_control_worker.activities import ControlActivities, ExecutionActivities
from shifde_control_worker.dispatcher import Dispatcher
from shifde_control_worker.workflows import DeliveryWorkflow


async def connect(settings: Settings):
    tls: bool | TLSConfig = False
    if settings.temporal_tls:
        tls = TLSConfig(
            server_root_ca_cert=settings.temporal_tls_ca_file.read_bytes()
            if settings.temporal_tls_ca_file
            else None,
            client_cert=settings.temporal_tls_cert_file.read_bytes()
            if settings.temporal_tls_cert_file
            else None,
            client_private_key=settings.temporal_tls_key_file.read_bytes()
            if settings.temporal_tls_key_file
            else None,
        )
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        tls=tls,
        api_key=settings.temporal_api_key.get_secret_value() if settings.temporal_api_key else None,
    )


async def run(mode: str):
    settings = Settings()
    if mode == "all" and not settings.development:
        raise ValueError("combined worker mode is for development only")
    service = service_from_settings(settings)
    service.database.check_runtime()
    client = await connect(settings)
    if mode == "dispatcher":
        await Dispatcher(
            service.database,
            client,
            control_queue=settings.control_queue,
            execution_queue=settings.execution_queue,
            poll_interval=settings.poll_interval_seconds,
        ).run()
        return

    def no_native(_):
        raise RuntimeError("control workers cannot resolve native credentials")

    executor = OperationExecutor(
        service,
        adapter_factory=lambda _: (
            GatewayAdapter(ExecutionGateway(service)) if mode in ("execution", "all") else no_native
        ),
        toolchain_digest=TOOLCHAIN_DIGEST,
    )
    control = ControlActivities(executor)
    execution = ExecutionActivities(executor)
    control_worker = Worker(
        client,
        task_queue=settings.control_queue,
        workflows=[DeliveryWorkflow],
        activities=[control.record_failure],
        max_concurrent_activities=8,
    )
    execution_worker = Worker(
        client,
        task_queue=settings.execution_queue,
        activities=[execution.execute, execution.observe],
        max_concurrent_activities=4,
    )
    if mode == "control":
        await control_worker.run()
    elif mode == "execution":
        await execution_worker.run()
    else:
        async with control_worker, execution_worker:
            await Dispatcher(
                service.database,
                client,
                control_queue=settings.control_queue,
                execution_queue=settings.execution_queue,
                poll_interval=settings.poll_interval_seconds,
            ).run()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["control", "execution", "dispatcher", "all"])
    asyncio.run(run(parser.parse_args().mode))


if __name__ == "__main__":
    main()
