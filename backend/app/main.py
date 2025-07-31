from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status

from .blog import fetch_user_blogs
from .routers import auth

app = FastAPI()

app.include_router(auth.router)

@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.get("/blogs")
def read_blogs(user: Annotated[auth.User, Depends(auth.get_current_user)]):
    blogs = fetch_user_blogs(user)
    return {"user": user.email, "user_id": user.id, "blogs": blogs}
