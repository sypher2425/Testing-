from typing import Any


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, detail: Any | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


def not_found(message: str, detail: Any | None = None) -> AppError:
    return AppError(404, "not_found", message, detail)


def bad_request(message: str, detail: Any | None = None) -> AppError:
    return AppError(400, "bad_request", message, detail)


def conflict(message: str, detail: Any | None = None) -> AppError:
    return AppError(409, "conflict", message, detail)


def payload_too_large(message: str, detail: Any | None = None) -> AppError:
    return AppError(413, "payload_too_large", message, detail)


def unprocessable(message: str, detail: Any | None = None) -> AppError:
    return AppError(422, "unprocessable", message, detail)


def insufficient_storage(message: str, detail: Any | None = None) -> AppError:
    return AppError(507, "insufficient_storage", message, detail)
