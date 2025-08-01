import os
import re
import secrets

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError, InvalidHashError

from dotenv import load_dotenv

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

import jwt
from jwt.exceptions import InvalidTokenError

import psycopg
from psycopg.rows import class_row

from pydantic import AfterValidator, BaseModel, Field, EmailStr

from ..database import get_connection
from ..emails import send_otp_email
from ..utils import utcnow
from ..models import User

load_dotenv()

SECRET_KEY = os.environ['JWT_SECRET']
ALGORITHM = os.environ['JWT_ALGORITHM']

if ALGORITHM not in {"HS256"}:
    raise RuntimeError(f"JWT_ALGORITHM {ALGORITHM} not permitted")

ACCESS_TOKEN_EXPIRE_MINUTES = 30
OTP_ACCESS_TOKEN_EXPIRE_MINUTES = 5 # much shorter access for pw reset auth

OTP_LENGTH = 6 # digits in an OTP
OTP_VALID_MINUTES = 15
MAX_OTP_REQUESTS_PER_HOUR = 3

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")
ph = PasswordHasher()

router = APIRouter(prefix="/auth")

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

# ======= MODELS =======

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    email: str | None = None

class UserInDB(User):
    hashed_password: str

def check_valid_password(password: str):
    if len(password) < 8:
        raise ValueError('Password must be at least 8 characters')
    if not re.search(r'[A-Z]', password):
        raise ValueError('Password must contain uppercase letter')
    if not re.search(r'[a-z]', password):
        raise ValueError('Password must contain lowercase letter')
    if not re.search(r'\d', password):
        raise ValueError('Password must contain a number')
    return password

class Password(BaseModel):
    password: Annotated[str, AfterValidator(check_valid_password)]

# ======= USER/PW =======

def get_user(db_conn: psycopg.Connection[dict], email):
    UserFactory = class_row(UserInDB)
    with db_conn.cursor(row_factory=UserFactory) as cursor:
        match = cursor.execute(
                "SELECT * FROM users WHERE email = %s", (email,)
                ).fetchone()
        return match

def create_user(email, password) -> bool:
    """
    Attempt to create a new user, returning status.
    """
    with get_connection() as db_conn:
        # check if the email already exists
        user = get_user(db_conn, email)
        if user is not None:
            return False

        # if not, hash the pw and add it to the db
        hashed_password = ph.hash(password)

        cursor = db_conn.cursor()
        cursor.execute(
            "INSERT INTO users (email, hashed_password) VALUES (%s, %s)",
            (email, hashed_password)
            )
        return True

def update_pw_hash(email, new_hash):
    """Update a user's password hash in the database."""
    with get_connection() as db_conn:
        cursor = db_conn.cursor()
        cursor.execute(
            "UPDATE users SET hashed_password = %s WHERE email = %s",
            (new_hash, email)
            )

async def authenticate_user(db_conn, email, password) -> UserInDB | None:
    """
    Authenticate a user with email and password.
    
    Args:
        db_conn: Database connection
        email: User's email address
        password: User's plain text password
        
    Returns:
        UserInDB | None: User object if authentication successful, None otherwise
        
    Note:
        Automatically rehashes password if needed for security updates.
    """
    user = get_user(db_conn, email)

    if not user:
        # always run a hash, to prevent timing attacks
        ph.hash('')
        return None

    try:
        # always run a hash, to prevent timing attacks
        ph.verify(user.hashed_password, password)
    except VerifyMismatchError:
        # Normal bad-password case; just return False.
        return None
    except (InvalidHashError, VerificationError):
        # These indicate programmer or infrastructure problems.
        if user:
            print(f"Password verification failed for user_id={user.id}")
        # 2. Re-raise so FastAPI’s exception handler turns it into 500
        raise

    # login successful. since we have the cleartext pw,
    # check if the pw needs rehashing
    if user and ph.check_needs_rehash(user.hashed_password):
        update_pw_hash(user.email, ph.hash(password))

    return user

# ======= OTP =======

OTP_LENGTH = 6 # digits in an OTP
OTP_VALID_MINUTES = 15

MAX_OTP_REQUESTS_PER_HOUR = 3

class OTP(BaseModel):
    """Pydantic model for a One-Time Password."""
    code: str = Field(..., min_length=OTP_LENGTH, max_length=OTP_LENGTH)

def generate_otp() -> OTP:
    """Generate an OTP with the `secrets` stdlib."""
    digits = (
        str(secrets.randbelow(10)) for _ in range(OTP_LENGTH)
        )
    return OTP(code=''.join(digits))

def setup_user_otp(db_conn: psycopg.Connection[dict], user: User) -> OTP:
    """Create and store a new OTP for a user."""
    otp = generate_otp()
    otp_hash = ph.hash(otp.code)

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO otps (user_id, otp_hash, valid) VALUES (%s, %s, %s)",
            (user.id, otp_hash, True)
            )

    return otp

def is_rate_limited(db_conn: psycopg.Connection[dict], user_id: int) -> bool:
    """Check if user has exceeded rate limit for OTP requests."""
    # Check for OTPs created in the last hour
    one_hour_ago = utcnow() - timedelta(hours=1)
    
    with db_conn.cursor() as cur:
        count = cur.execute(
            """SELECT COUNT(*) FROM otps 
               WHERE user_id = %s AND created_at > %s""",
            (user_id, one_hour_ago)
        ).fetchone()['count'] # type: ignore (count always returns a row)
    
    return count >= MAX_OTP_REQUESTS_PER_HOUR

def send_otp(otp: OTP, email: str) -> bool:
    """
    Send the OTP to a user.
    """
    try:
        send_otp_email(otp.code, email)
        return True
    except Exception as e:
        return False
    

def is_valid_otp(db_conn: psycopg.Connection[dict], user: UserInDB | None, otp: OTP):
    """Verify if an OTP is valid for a user."""
    with db_conn.cursor() as cur:
        # still run a DB query if the user doesn't exist
        # to minimize timing attack potential
        user_id = user.id if user else -1

        # check for an active, unused otp for the given user
        expiry_time = utcnow() - timedelta(minutes=OTP_VALID_MINUTES)
        row = cur.execute(
            """SELECT otp_hash FROM otps WHERE (
                user_id = %s AND
                valid = TRUE AND
                created_at > %s)
            ORDER BY created_at DESC""", 
            (user_id, expiry_time)
            ).fetchone()

        # still run a hash if the user doesn't exist
        # or if they haven't requested an OTP,
        # to minimize timing attack potential
        if (not row) or (not user):
            ph.hash('')
            return False

        # check the provided OTP against the hash
        try:
            return ph.verify(row['otp_hash'], otp.code)
        except VerifyMismatchError:
            # Normal incorrect-OTP; just return False.
            return None
        except (InvalidHashError, VerificationError):
            # These indicate programmer or infrastructure problems.
            if user:
                print(f"Password verification failed for user_id={user.id}")
            # 2. Re-raise so FastAPI’s exception handler turns it into 500
            raise

def mark_otps_invalid(db_conn: psycopg.Connection[dict], user_id: int):
    """
    Mark all OTPs for a user from the past `OTP_VALID_MINUTES` as invalid.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE otps SET valid = FALSE WHERE user_id = %s AND created_at > %s",
            (user_id, utcnow() - timedelta(minutes=OTP_VALID_MINUTES))
        )

# ======= TOKENS =======

class TokenScope(StrEnum):
    ACCESS = 'access'
    RESET_PASSWORD = 'reset_password'

def _create_jwt(user: User, expires_delta: timedelta, scope: TokenScope):
    """Create a JWT for a specific user."""
    data = {
        "sub": user.email,
        "ver": user.jwt_version,
        "scope": scope.value,
        "exp": datetime.now(timezone.utc) + expires_delta
    }
    encoded_jwt = jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def create_access_token(user: User, expires_delta: timedelta):
    """Create a JWT scoped for full access."""
    return _create_jwt(user, expires_delta, TokenScope.ACCESS)

def create_reset_password_token(user: User, expires_delta: timedelta):
    """Create a JWT scoped only to reset password."""
    return _create_jwt(user, expires_delta, TokenScope.RESET_PASSWORD)

def decode_and_validate_token(token: Annotated[str, Depends(oauth2_scheme)]):
    """
    Decode a provided JWT and validate it.

    Checks that it has a valid user email in the 'sub' payload
    and the correct version in the 'ver' payload.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except InvalidTokenError:
        raise CREDENTIALS_EXCEPTION

    email = payload.get('sub')

    with get_connection() as db_conn:
        user = get_user(db_conn, email)

    if not user:
        raise CREDENTIALS_EXCEPTION

    version = payload.get('ver')
    if version != user.jwt_version:
        raise CREDENTIALS_EXCEPTION

    return payload, user

async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]):
    """
    Extract and validate the current user from JWT token.
    """
    payload, user = decode_and_validate_token(token)

    if payload['scope'] != TokenScope.ACCESS:
        raise CREDENTIALS_EXCEPTION

    return user

# ======= ENDPOINTS =======

@router.post("/token")
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    """
    Authenticate user and return JWT access token.
    
    Args:
        form_data: OAuth2 form data containing username (email) and password
        
    Returns:
        Token: JWT access token
        
    Raises:
        HTTPException: If authentication fails
    """
    with get_connection() as db_conn:
        user = await authenticate_user(
            db_conn,
            form_data.username,
            form_data.password
            )
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(user, access_token_expires)
        return Token(access_token=access_token, token_type="bearer")

@router.post("/new")
async def user_signup(
    email: Annotated[EmailStr, Body()],
    password: Annotated[Password, Body()]
    ) -> dict:
    """
    Register a new user account.
    
    Args:
        email: User's email address
        password: User's password (validated for strength)
        
    Returns:
        dict: Success message
        
    Raises:
        HTTPException: If user with email already exists
    """
    success = create_user(email, password)
    if success:
        return {"success": True, "message": "User created successfully"}
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists"
        )

@router.post("/reset")
async def request_password_reset(email: Annotated[str, Body()]) -> None:
    """
    Initiate password reset process by sending OTP.
    
    Args:
        email: User's email address
        
    Note:
        - Silently succeeds if email doesn't exist (security best practice)
        - Rate limits OTP requests to prevent abuse
        - Invalidates previous OTPs before generating new one
    """
    with get_connection() as db_conn:
        user = get_user(db_conn, email)
        # if the user doesn't exist, ignore the reset request
        if not user:
            return

        # deactivate any OTPs requested previously
        mark_otps_invalid(db_conn, user.id)

        # if they've requested too many, raise an error
        if is_rate_limited(db_conn, user.id):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many password reset requests. Please try again later."
            )

        # generate and send a OTP
        otp = setup_user_otp(db_conn, user)
        send_otp(otp, email)

@router.post("/reset/verify")
async def verify_otp(
    email: Annotated[str, Body()],
    otp: Annotated[OTP, Body()]
    ) -> Token:
    """
    Verify OTP and return temporary access token for password reset.
    
    Args:
        email: User's email address
        otp: 6-digit OTP code
        
    Returns:
        Token: Short-lived JWT token for password reset
        
    Raises:
        HTTPException: If email or OTP is invalid
    """
    invalid_exception = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid email or OTP."
        )

    with get_connection() as db_conn:
        user = get_user(db_conn, email)

        if not is_valid_otp(db_conn, user, otp):
            raise invalid_exception

        if not user:
            raise invalid_exception

        # mark the OTP just used as invalid
        mark_otps_invalid(db_conn, user.id)

        expires = timedelta(
            minutes=OTP_ACCESS_TOKEN_EXPIRE_MINUTES
            )
        token = create_reset_password_token(user, expires)
        return Token(access_token=token, token_type="bearer")


@router.post("/reset/confirm")
async def reset_password(
    new_password: Annotated[Password, Body()],
    token: Annotated[str, Depends(oauth2_scheme)]
    ):
    """
    Reset user's password using temporary token from OTP verification.
    
    Args:
        new_password: New password (validated for strength)
        user: Current authenticated user (from temporary token)
        
    Note:
        Requires authentication via temporary token from /reset/verify endpoint.
    """
    # ensure the request is authenticated
    payload, user = decode_and_validate_token(token)

    # and that it has the correct scope
    if payload['scope'] != TokenScope.RESET_PASSWORD:
        raise CREDENTIALS_EXCEPTION

    new_pw_hash = ph.hash(new_password.password)
    update_pw_hash(user.email, new_pw_hash)

    # increment JWT version for the user to invalidate
    # previously issued tokens
    with get_connection() as db_conn:
        db_conn.execute(
            "UPDATE users SET jwt_version = jwt_version + 1 WHERE id = %s",
            (user.id,)
        )
