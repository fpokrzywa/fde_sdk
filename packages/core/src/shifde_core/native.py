TERMINAL_STEPS = {
    "databricks.app/v1": "bundle-run",
    "snowflake.native-app/v1": "application-access",
    "fabric.solution/v1": "fabric-finalize",
    "fabric.iq/v1": "fabric-finalize",
}

SNOWFLAKE_PROFILE = {
    "adapter": "0.1.0",
    "cli": "3.33.0",
    "sdk": "5.2.0",
    "packager": "snowflake-managed-source/v1",
}

FABRIC_PROFILE = {"adapter": "0.1.0", "api": "v1", "packager": "fabric-source/v1"}


def terminal_step(kind: str) -> str:
    try:
        return TERMINAL_STEPS[kind]
    except KeyError:
        raise ValueError("unsupported native application kind") from None


def fabric_step(key: str, phase: str) -> str:
    from hashlib import sha256

    value = key + "-" + phase
    return (
        value
        if len(value) <= 64
        else key[:16] + "-" + sha256(key.encode()).hexdigest()[:16] + "-" + phase
    )
