from fastapi import HTTPException, status

import limits

from slowapi import Limiter
from slowapi.util import get_remote_address

ip_rate_limiter = Limiter(key_func=get_remote_address, default_limits=["5/second"])

# deploying on a single server, no need for
# redis for now
rate_limit_storage = limits.storage.MemoryStorage()
email_limiter = limits.strategies.MovingWindowRateLimiter(rate_limit_storage)

def email_rate_limit(limit_string: str, endpoint: str, email: str):
    """
    Rate limit by user email. Throws HTTP 429 on too many requests.
    """
    limit_intervals = limits.parse_many(limit_string)
    for interval in limit_intervals:
        valid = email_limiter.hit(interval, endpoint, email)
        if not valid:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS)
