class BusinessError(Exception):
    pass


class AccessDenied(BusinessError):
    pass


class Conflict(BusinessError):
    pass


class RetryLater(BusinessError):
    pass
