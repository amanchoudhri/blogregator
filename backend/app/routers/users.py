from typing import Annotated, Optional

from fastapi import APIRouter, Body, HTTPException, status
from psycopg.rows import class_row

from ..database import get_connection
from ..dependencies import CurrentActiveUser
from ..models import Blog, Post

router = APIRouter(prefix="/users")

@router.get("/me/following")
def get_followed_blogs(user: CurrentActiveUser):
    """List all blogs a user follows."""
    with get_connection() as conn:
        BlogFactory = class_row(Blog)
        with conn.cursor(row_factory=BlogFactory) as cursor:
            cursor.execute("""
            SELECT * FROM blogs WHERE id IN (
                SELECT blog_id FROM blog_users WHERE user_id = %s
                )
            """, (user.id,))
            blogs = cursor.fetchall()

    return {"blogs": blogs}

@router.post("/me/following")
def follow_blog(user: CurrentActiveUser, blog_id: Annotated[int, Body()]):
    """Follow a blog."""
    try: 
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                INSERT INTO blog_users (blog_id, user_id)
                VALUES (%s, %s)
                """, (blog_id, user.id))
    except Exception as e:
        print(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error: blog was not able to be followed. Please try again."
            )

@router.delete("/me/following/{blog_id}")
def unfollow_blog(user: CurrentActiveUser, blog_id: int):
    """Unfollow a blog."""
    try: 
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                DELETE FROM blog_users WHERE
                    blog_id = %s AND
                    user_id = %s
                    RETURNING *
                """, (blog_id, user.id))
                unfollowed = cursor.fetchone()
    except:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Blog was not able to be unfollowed. Please try again."
            )

    if unfollowed is not None:
        return {"message": "Blog unfollowed successfully."}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User does not follow blog."
            )

@router.get("/me/feed")
def get_posts(
    user: CurrentActiveUser,
    offset: int,
    limit: int,
    blog_id: Optional[int] = None,
    topic_id: Optional[int] = None
    ):
    """
    Get posts from blogs a user follows, optionally filtering by specific
    blogs.
    """
    try: 
        with get_connection() as conn:
            with conn.cursor(row_factory=class_row(Post)) as cursor:
                query = """
                SELECT p.*,
                    ARRAY_AGG(json_build_object('id', t.id, 'name', t.name))
                    FILTER (WHERE t.id IS NOT NULL) AS topics
                    FROM posts p
                JOIN blog_users bu ON p.blog_id = bu.blog_id
                LEFT JOIN post_topics pt ON pt.post_id = p.id
                LEFT JOIN topics t ON t.id = pt.topic_id
                WHERE bu.user_id = %(user_id)s
                    AND (%(blog_id)s::integer IS NULL OR p.blog_id = %(blog_id)s)
                    AND (%(topic_id)s::integer IS NULL OR pt.topic_id = %(topic_id)s)
                GROUP BY p.id
                ORDER BY p.publication_date DESC
                OFFSET %(offset)s LIMIT %(limit)s
                """
                
                params = {
                    'user_id': user.id,
                    'blog_id': blog_id,
                    'topic_id': topic_id,
                    'offset': offset,
                    'limit': limit
                }
                
                cursor.execute(query, params)
                posts = cursor.fetchall()
                return posts
    except Exception as e:
        print(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Feed was not able to be loaded. Please try again."
            )
