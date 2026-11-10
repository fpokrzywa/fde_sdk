"""Bounded readiness check for the development/CI Temporal service."""

import asyncio
import os

from temporalio.client import Client


async def main():
    address = os.environ["SHIFDE_TEST_TEMPORAL_ADDRESS"]
    for attempt in range(30):
        try:
            client = await Client.connect(address)
            await client.service_client.check_health()
            return
        except Exception:
            if attempt == 29:
                raise
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
