import os
import re
import secrets

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from smtplib import SMTPException
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError, InvalidHashError

from dotenv import load_dotenv

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

import jwt
from jwt.exceptions import InvalidTokenError

import psycopg
from psycopg.rows import class_row
from psycopg.errors import InterfaceError, DatabaseError, OperationalError

from pydantic import AfterValidator, BaseModel, Field, EmailStr

from ..database import get_connection
from ..rate_limit import ip_rate_limiter, email_rate_limit
from ..emails import send_otp_email, send_verification_email
from ..utils import utcnow
from ..models import User

load_dotenv()

SECRET_KEY = os.environ['JWT_SECRET']
ALGORITHM = os.environ['JWT_ALGORITHM']

if ALGORITHM not in {"HS256"}:
    raise RuntimeError(f"JWT_ALGORITHM {ALGORITHM} not permitted")

ACCESS_TOKEN_EXPIRE_MINUTES = 30
EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS = 24
OTP_ACCESS_TOKEN_EXPIRE_MINUTES = 5 # much shorter access for pw reset auth

EMAIL_VERIFICATION_TOKEN_LENGTH = 32 # bytes

OTP_LENGTH = 6 # digits in an OTP
OTP_VALID_MINUTES = 15
MAX_OTP_REQUESTS_PER_HOUR = 3

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="auth/token",
    refreshUrl="auth/refresh"
    )
ph = PasswordHasher()

DUMMY_HASH = ph.hash('')

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

def create_user(email: str, password: str) -> bool:
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

def dummy_hash_verification(attempt: str):
    """
    Run a dummy argon2 verification step to
    minimize timing attacks.
    """
    try:
        ph.verify(DUMMY_HASH, attempt)
    except:
        pass

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
        dummy_hash_verification(password)
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
            dummy_hash_verification(otp.code)
            return False

        # check the provided OTP against the hash
        try:
            return ph.verify(row['otp_hash'], otp.code)
        except VerifyMismatchError:
            # Normal incorrect-OTP; just return False.
            return False
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
    ADMIN = 'admin'
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

    if payload['scope'] not in (TokenScope.ACCESS, TokenScope.ADMIN):
        raise CREDENTIALS_EXCEPTION

    return user

async def get_current_active_user(token: Annotated[str, Depends(oauth2_scheme)]):
    """
    Extract and validate the current user from JWT token,
    ensuring their email is verified.
    """
    payload, user = decode_and_validate_token(token)

    if payload['scope'] not in (TokenScope.ACCESS, TokenScope.ADMIN):
        raise CREDENTIALS_EXCEPTION

    return user

async def get_current_admin_user(token: Annotated[str, Depends(oauth2_scheme)]):
    """
    Extract and validate the current admin-priviledged user from JWT token.
    """
    payload, user = decode_and_validate_token(token)

    if payload['scope'] != TokenScope.ADMIN:
        raise CREDENTIALS_EXCEPTION

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="User's email is not verified.")

    return user

# ======= ENDPOINTS =======

@router.post("/token")
@ip_rate_limiter.limit("10/minute;50/hour;500/day")
async def login_for_access_token(
    request: Request,
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
    email_rate_limit(
        "1/second; 10/minute; 30/hour",
        "/auth/token",
        form_data.username
        )
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

        scope = TokenScope.ADMIN if user.is_admin else TokenScope.ACCESS
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = _create_jwt(user, access_token_expires, scope)

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
    success = create_user(email, str(password))
    if success:
        return {"success": True, "message": "User created successfully"}
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists"
        )

@router.post("/email/send-verify")
@ip_rate_limiter.limit("5/hour; 10/day")
def request_email_verify(request: Request, user: Annotated[User, Depends(get_current_user)]):
    email_rate_limit("5/10minutes; 10/day", "/email/send-verify", user.email)
    with get_connection() as db_conn:
        if user.is_active:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "User's email is already verified."
                )
        token = secrets.token_urlsafe(EMAIL_VERIFICATION_TOKEN_LENGTH)
        
        created_at = utcnow()
        expires_at = created_at + timedelta(hours=EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)

        try:
            db_conn.execute("""
                INSERT INTO email_verification (token, user_id, created_at, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    token = EXCLUDED.token,
                    created_at = EXCLUDED.created_at,
                    expires_at = EXCLUDED.expires_at
                """, (token, user.id, created_at, expires_at))
            send_verification_email(token, user.email)
        except (InterfaceError, DatabaseError, OperationalError, SMTPException):
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Unable to send email. Please try again."
                )

        return {"message": "Verification email sent"}

@router.get("/email/verify")
@ip_rate_limiter.limit("10/hour; 20/day")
def confirm_email_verification(
    request: Request,
    token: str,
    user: Annotated[User, Depends(get_current_user)]
    ):
    try:
        bad_request_exc = HTTPException(status.HTTP_400_BAD_REQUEST, "Could not verify email.")
        with get_connection() as db_conn:
            issued_token = db_conn.execute(
                "SELECT * FROM email_verification WHERE user_id = %s",
                (user.id,)
                ).fetchone()

            if (issued_token is None):
                raise bad_request_exc

            wrong_token = issued_token['token'] != token
            expired = issued_token['expires_at'] < utcnow()

            if wrong_token or expired:
                raise bad_request_exc

            # put these two operations within one transaction
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET is_active = TRUE WHERE id = %s",
                    (user.id,)
                    )
                cur.execute(
                    "DELETE FROM email_verification WHERE user_id = %s",
                    (user.id,)
                    )

    except (OperationalError, DatabaseError, InterfaceError):
        raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Could not verify email, please try again."
                )
    return {"message": "Email verified successfully."}



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
@ip_rate_limiter.limit("3/minute;10/15minute;30/hour")
async def verify_otp(
    request: Request,
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
    email_rate_limit("1/second; 5/minute; 10/hour", "/auth/reset/verify", email)

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
        token = _create_jwt(user, expires, TokenScope.RESET_PASSWORD)
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
