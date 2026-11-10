"""Coordinator-side adapter facade: dispatch and wait, never load native credentials."""

import asyncio

from shifde_core.journal import NeedsAttention

from shifde_hosted.job_types import RESULTS


class GatewayAdapter:
    def __init__(self, gateway, *, poll_seconds=0.2):
        self.gateway, self.poll_seconds = gateway, poll_seconds

    async def _run(self, action, request, ctx):
        job = await asyncio.to_thread(self.gateway.enqueue, action, request, ctx)
        while True:
            outcome = await asyncio.to_thread(self.gateway.outcome, job)
            if outcome:
                if outcome["status"] != "succeeded":
                    if action == "apply":
                        raise NeedsAttention("isolated native job did not prove completion")
                    raise RuntimeError("isolated executor job failed")
                return RESULTS[action].model_validate(outcome["value"])
            await asyncio.sleep(self.poll_seconds)

    async def capabilities(self, ctx):
        return await self._run("capabilities", None, ctx)

    async def inspect(self, request, ctx):
        return await self._run("inspect", request, ctx)

    async def scaffold(self, request, ctx):
        return await self._run("scaffold", request, ctx)

    async def validate(self, request, ctx):
        return await self._run("validate", request, ctx)

    async def build(self, request, ctx):
        return await self._run("build", request, ctx)

    async def plan(self, request, ctx):
        return await self._run("plan", request, ctx)

    async def apply(self, request, ctx):
        return await self._run("apply", request, ctx)

    async def observe(self, request, ctx):
        return await self._run("observe", request, ctx)

    async def reconcile(self, request, ctx):
        return await self._run("reconcile", request, ctx)

    async def verify(self, request, ctx):
        return await self._run("verify", request, ctx)
