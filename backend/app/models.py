import datetime

from pydantic import BaseModel, EmailStr, HttpUrl

class User(BaseModel):
    id: int
    email: EmailStr
    jwt_version: int

class Blog(BaseModel):
    id: int
    url: HttpUrl
    last_checked: datetime.datetime | None
    scraping_schema: str
    proposed_schema: str
    scraping_successful: bool | None
    refinement_attempts: int
    last_modified_by: int | None
    last_modified_at: datetime.datetime

class Topic(BaseModel):
    id: int
    name: str

class Post(BaseModel):
    id: int
    blog_id: int
    title: str
    url: HttpUrl
    reading_time: int
    summary: str
    publication_date: datetime.datetime
    discovered_date: datetime.datetime
    topics: list[Topic] = []
