from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .security import BCRYPT_MAX_BYTES


class Credentials(BaseModel):
    email: EmailStr = Field(max_length=320)
    password: str = Field(min_length=8, max_length=BCRYPT_MAX_BYTES)

    @field_validator("password")
    @classmethod
    def fits_bcrypt(cls, value: str) -> str:
        # max_length counts characters; bcrypt counts bytes ("é" is 2 bytes).
        if len(value.encode()) > BCRYPT_MAX_BYTES:
            raise ValueError(f"password must be at most {BCRYPT_MAX_BYTES} bytes")
        return value


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - the OAuth2 token type, not a secret


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    plan: str
