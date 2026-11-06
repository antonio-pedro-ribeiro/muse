"""Kernel errors. Interfaces map them to exit codes / HTTP status codes."""


class KernelError(Exception):
    status = 500
    exit_code = 1

    def __init__(self, message: str, plugin: str | None = None) -> None:
        super().__init__(message)
        self.plugin = plugin


class NotFound(KernelError):
    status = 404


class Busy(KernelError):
    status = 409


class ConfigError(KernelError):
    status = 422
    exit_code = 2


class PluginUnavailable(ConfigError):
    pass


class DeviceNotFound(KernelError):
    status = 404
    exit_code = 3
