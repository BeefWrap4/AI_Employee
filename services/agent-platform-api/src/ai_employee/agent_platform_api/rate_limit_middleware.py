"""Re-export of the shared rate-limit middleware for backward compatibility.

The shared package lives at :mod:`packages.rate-limit.src.ai_employee.rate_limit`.
The original implementation moved there in R25-L.
"""

from ai_employee.rate_limit import (
    RateLimitMiddleware,
    install_rate_limiter,
)

__all__ = ["RateLimitMiddleware", "install_rate_limiter"]
