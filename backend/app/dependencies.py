from typing import Annotated

from fastapi import Depends

from .models import User
from .routers.auth import get_current_user

CurrentUser = Annotated[User, Depends(get_current_user)]
