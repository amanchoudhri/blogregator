import datetime

from pydantic import BaseModel, EmailStr

class User(BaseModel):
    id: int
    email: EmailStr

class Blog(BaseModel):
    id: int
    name: str
    url: str
    last_checked: datetime.datetime | None
    scraping_successful: bool | None
