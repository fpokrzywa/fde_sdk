class ServiceError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


class NotFound(ServiceError):
    def __init__(self):
        super().__init__("NOT_FOUND", 404)


class Forbidden(ServiceError):
    def __init__(self):
        super().__init__("FORBIDDEN", 403)
