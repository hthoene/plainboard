class ServiceError(Exception):
    """Domain error with an HTTP-ish status. Tools report the message back to the model."""

    status_code = 400

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class NotFound(ServiceError):
    status_code = 404
