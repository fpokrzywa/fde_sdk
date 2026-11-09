from shifde_core.adapter import PlatformAdapter


class AdapterRegistry:
    def __init__(self):
        self._adapters: dict[tuple[str, str], PlatformAdapter] = {}

    def register(
        self, application_kind: str, adapter_version: str, adapter: PlatformAdapter
    ) -> None:
        key = (application_kind, adapter_version)
        if key in self._adapters:
            raise ValueError("adapter is already registered")
        self._adapters[key] = adapter

    def get(self, application_kind: str, adapter_version: str) -> PlatformAdapter:
        try:
            return self._adapters[(application_kind, adapter_version)]
        except KeyError as exc:
            raise LookupError("requested adapter version is not registered") from exc
