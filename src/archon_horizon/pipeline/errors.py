"""Public domain errors contain actionable, secret-free details."""


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def response(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.details}}
