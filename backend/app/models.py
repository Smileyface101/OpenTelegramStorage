"""ORM models. SQLite is the source of truth for the file index; the Telegram
channel is self-describing (JSON captions) so the index can be rebuilt."""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


def new_id() -> str:
    return uuid.uuid4().hex


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Session(Base):
    """Server-side login sessions; the cookie carries a random token whose
    SHA-256 is the primary key, so a DB leak does not leak usable cookies."""
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Setting(Base):
    """Key/value settings. Values flagged encrypted are vault ciphertext."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("owner_id", "parent_id", "name", name="uq_folder_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class FileStatus(str, enum.Enum):
    RECEIVING = "receiving"   # browser still sending; completed parts already flow to Telegram
    QUEUED = "queued"         # staged locally, waiting for the worker
    HASHING = "hashing"       # worker computing part hashes
    UPLOADING = "uploading"   # parts going to Telegram
    READY = "ready"           # fully in the channel, staging removed
    FAILED = "failed"         # retries exhausted; staging kept for retry


class File(Base):
    __tablename__ = "files"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_archive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    part_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[FileStatus] = mapped_column(Enum(FileStatus), default=FileStatus.QUEUED, nullable=False, index=True)
    staging_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    parts: Mapped[list["FilePart"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="FilePart.index"
    )


class FilePart(Base):
    __tablename__ = "file_parts"
    __table_args__ = (
        UniqueConstraint("file_id", "index", name="uq_part_index"),
        Index("ix_part_message", "message_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True, nullable=False)
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Streaming uploads: each part is staged in its own file and sent to the
    # channel as soon as `received` reaches `size`. NULL staging_path means the
    # part is read from the parent File's whole-file staging (archives).
    received: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    staging_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    file: Mapped[File] = relationship(back_populates="parts")

    @property
    def staged(self) -> bool:
        return self.staging_path is not None and self.received >= self.size


class UploadStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETE = "complete"


class Upload(Base):
    """A browser -> server resumable upload into the staging directory."""
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id", ondelete="SET NULL"), nullable=True)
    bundle_id: Mapped[str | None] = mapped_column(ForeignKey("bundles.id", ondelete="CASCADE"), index=True, nullable=True)
    # Plain (non-bundle) uploads stream straight into a File's parts.
    file_id: Mapped[str | None] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Relative path inside a bundle (e.g. "photos/2024/a.jpg") so a zipped
    # folder keeps its structure. NULL for plain uploads.
    rel_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[UploadStatus] = mapped_column(Enum(UploadStatus), default=UploadStatus.ACTIVE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Bundle(Base):
    """A set of uploads zipped server-side into one archive before transfer."""
    __tablename__ = "bundles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    compress: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
