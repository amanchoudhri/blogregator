from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status

from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded

from .routers import auth, blog, users
from .rate_limit import ip_rate_limiter

load_dotenv('/Users/amanchoudhri/aman/code/blogregator/backend/.env')

app = FastAPI()

app.include_router(auth.router)
app.include_router(blog.router)
app.include_router(users.router)

app.state.limiter = ip_rate_limiter

@app.exception_handler(RateLimitExceeded)
def rate_limit_exceeded_handler(request, exc):
    return _rate_limit_exceeded_handler(request, exc)

app.add_middleware(SlowAPIMiddleware)
