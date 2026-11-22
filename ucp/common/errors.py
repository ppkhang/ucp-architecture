from enum import StrEnum


class ErrorCode(StrEnum):
    UNAUTHORIZED = "UCP_UNAUTHORIZED"
    SESSION_EXPIRED = "UCP_SESSION_EXPIRED"
    FORBIDDEN = "UCP_FORBIDDEN"
    NO_USER_LINK = "UCP_NO_USER_LINK"
    NO_CONSENT = "UCP_NO_CONSENT"
    AGENT_SUSPENDED = "UCP_AGENT_SUSPENDED"
    QUOTA_EXCEEDED = "UCP_QUOTA_EXCEEDED"
    RATE_LIMITED = "UCP_RATE_LIMITED"
    INSUFFICIENT_STOCK = "UCP_INSUFFICIENT_STOCK"
    PARTIAL_STOCK = "UCP_PARTIAL_STOCK"
    MAX_PER_USER = "UCP_MAX_PER_USER"
    CANNOT_CANCEL = "UCP_CANNOT_CANCEL"
    CHECKOUT_EXPIRED = "UCP_CHECKOUT_EXPIRED"
    PRODUCT_UNAVAILABLE = "UCP_PRODUCT_UNAVAILABLE"
    PAYMENT_FAILED = "UCP_PAYMENT_FAILED"
    BAD_REQUEST = "UCP_BAD_REQUEST"
    NOT_FOUND = "UCP_NOT_FOUND"
    INTERNAL_ERROR = "UCP_INTERNAL_ERROR"
    PLATFORM_UNAVAILABLE = "UCP_PLATFORM_UNAVAILABLE"
    PRICE_CHANGED = "UCP_PRICE_CHANGED"
    CONFIRMATION_REQUIRED = "UCP_CONFIRMATION_REQUIRED"
    IDEMPOTENCY_CONFLICT = "UCP_IDEMPOTENCY_CONFLICT"
    LINK_EXPIRED = "UCP_LINK_EXPIRED"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.SESSION_EXPIRED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NO_USER_LINK: 403,
    ErrorCode.NO_CONSENT: 403,
    ErrorCode.AGENT_SUSPENDED: 403,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INSUFFICIENT_STOCK: 409,
    ErrorCode.PARTIAL_STOCK: 409,
    ErrorCode.MAX_PER_USER: 409,
    ErrorCode.CANNOT_CANCEL: 409,
    ErrorCode.CHECKOUT_EXPIRED: 410,
    ErrorCode.PRODUCT_UNAVAILABLE: 410,
    ErrorCode.PAYMENT_FAILED: 402,
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.PLATFORM_UNAVAILABLE: 503,
    ErrorCode.PRICE_CHANGED: 409,
    ErrorCode.CONFIRMATION_REQUIRED: 403,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.LINK_EXPIRED: 403,
}


class UCPError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.code]

    def body(self) -> dict:
        return {"error": {"code": self.code.value, "message": self.message, "details": self.details}}
