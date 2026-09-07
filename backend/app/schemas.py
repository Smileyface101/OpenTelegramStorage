from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime


class SetupStatus(BaseModel):
    needs_admin: bool
    telegram_configured: bool
    telegram_connected: bool
    channel_configured: bool
    version: str


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserCreate(Credentials):
    role: str = "user"


class TelegramConnect(BaseModel):
    api_id: int
    api_hash: str = Field(min_length=16, max_length=64)
    bot_token: str = Field(min_length=20, max_length=128)


class ChannelSelect(BaseModel):
    chat_id: int


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: int | None = None

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.strip()
        if not v or "/" in v or "\\" in v:
            raise ValueError("invalid folder name")
        return v


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class Move(BaseModel):
    folder_id: int | None


class BulkMove(BaseModel):
    file_ids: list[str] = Field(default_factory=list, max_length=500)
    folder_ids: list[int] = Field(default_factory=list, max_length=500)
    target_folder_id: int | None = None


class UploadInit(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    mime_type: str | None = Field(default=None, max_length=128)
    folder_id: int | None = None
    bundle_id: str | None = None
    path: str | None = Field(default=None, max_length=1024)  # relative path inside a bundle
    member_index: int | None = Field(default=None, ge=0)     # position in the bundle manifest


class FolderEnsure(BaseModel):
    """Create (or find) a nested folder path like "photos/2024" under parent_id."""
    path: str = Field(min_length=1, max_length=2048)
    parent_id: int | None = None


class BundleMember(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)


class BundleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    folder_id: int | None = None
    compress: bool | None = None  # ignored: streaming archives are store-only
    members: list[BundleMember] = Field(min_length=1, max_length=100000)


class SettingsUpdate(BaseModel):
    part_size_mb: int | None = Field(default=None, ge=1)
    max_retries: int | None = Field(default=None, ge=0, le=20)
    compress_archives: bool | None = None
    upload_connections: int | None = Field(default=None, ge=1, le=16)


class UploadComplete(BaseModel):
    """Optional client-side digests: whole file plus one per part, computed by
    the browser while it read the file."""
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    part_sha256: list[str] | None = None


class ImportRequest(BaseModel):
    path: str = Field(default="", max_length=4096)   # relative to the import directory
    mode: str | None = None                            # file | zip | tree
    folder_id: int | None = None
    name: str | None = Field(default=None, max_length=255)
