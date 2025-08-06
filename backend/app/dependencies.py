from typing import Annotated

from fastapi import Depends

from .models import User
from .routers.auth import get_current_user, get_current_active_user

CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentActiveUser = Annotated[User, Depends(get_current_user)]
