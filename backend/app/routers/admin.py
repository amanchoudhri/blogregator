from fastapi import APIRouter, Depends

from psycopg.rows import class_row

from ..cli import process_blog
from ..database import get_connection
from ..models import Blog
from ..routers.auth import get_current_admin_user

router = APIRouter(prefix="/admin", dependencies=[Depends(get_current_admin_user)])

@router.get("/check-all-blogs")
def check_all_blogs():
    with get_connection() as db_conn:
        with db_conn.cursor(row_factory=class_row(Blog)) as cur:
            blogs = cur.execute("SELECT * FROM blogs WHERE scraping_successful = TRUE").fetchall()

        for blog in blogs:
            process_blog(db_conn, blog)
