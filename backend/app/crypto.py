"""Content encryption: Telegram only ever holds ciphertext.

Format (per part):
    header  = b"OTS1" + salt(16)                                   20 bytes
    block_i = AES-256-GCM(subkey, nonce=i, aad=salt||i, pt_block)  len+16
with 1 MiB plaintext blocks and subkey = HKDF-SHA256(content_key, salt, "ots-part").
A fresh salt per part means block nonces can be plain counters. Blocks are
independently authenticated, so a byte range can be served by fetching and
decrypting only the blocks it touches (Range downloads stay cheap).

The content key is a random 32 bytes stored vault-encrypted in settings.
Losing the data volume without a copy of the key makes every encrypted file
unreadable, which Settings says loudly; export/import exists for that."""
import hashlib
import os
import secrets
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store

MAGIC = b"OTS1"
HEADER = 20
SALT = 16
TAG = 16
BLOCK = 1024 * 1024
CT_BLOCK = BLOCK + TAG


# ------------------------------------------------------------------ key
def key_id_of(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


async def get_key(db: AsyncSession) -> bytes | None:
    hex_key = await settings_store.get(db, "content.key")
    return bytes.fromhex(hex_key) if hex_key else None


async def ensure_key(db: AsyncSession) -> bytes:
    key = await get_key(db)
    if key is None:
        key = secrets.token_bytes(32)
        await settings_store.set(db, "content.key", key.hex())
        await settings_store.set(db, "content.key_id", key_id_of(key))
    return key


async def set_key(db: AsyncSession, key: bytes) -> None:
    if len(key) != 32:
        raise ValueError("key must be 32 bytes")
    await settings_store.set(db, "content.key", key.hex())
    await settings_store.set(db, "content.key_id", key_id_of(key))


async def encrypt_new_files(db: AsyncSession) -> bool:
    return await settings_store.get_bool(db, "content.encrypt_new", True)


def subkey(key: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"ots-part").derive(key)


def _nonce(i: int) -> bytes:
    return struct.pack(">IQ", 0, i)


def _aad(salt: bytes, i: int) -> bytes:
    return salt + struct.pack(">Q", i)


# ------------------------------------------------------------------ sizes
def ct_size(pt_size: int) -> int:
    blocks = (pt_size + BLOCK - 1) // BLOCK
    return HEADER + pt_size + TAG * blocks


def ct_block_offset(i: int) -> int:
    return HEADER + i * CT_BLOCK


def ct_range_for(pt_from: int, pt_len: int, pt_size: int) -> tuple[int, int, int]:
    """Ciphertext (offset, length) covering plaintext [pt_from, pt_from+pt_len),
    plus the index of the first block, so the caller can drop leading bytes."""
    if pt_len <= 0:
        return HEADER, 0, 0
    first = pt_from // BLOCK
    last = (pt_from + pt_len - 1) // BLOCK
    start = ct_block_offset(first)
    end = min(ct_block_offset(last + 1), ct_size(pt_size))
    return start, end - start, first


# ------------------------------------------------------------------ encrypt
class EncryptingReader:
    """Wraps a plaintext reader (RangeReader) and yields the encrypted part.
    Sequential; seek(0) restarts (Telethon's fallback path does that)."""

    def __init__(self, reader, pt_size: int, key: bytes, salt: bytes | None = None):
        self._src = reader
        self._pt_size = pt_size
        self.salt = salt or secrets.token_bytes(SALT)
        self._aead = AESGCM(subkey(key, self.salt))
        self.size = ct_size(pt_size)
        self._reset()

    def _reset(self) -> None:
        self._src.seek(0)
        self._buf = bytearray(MAGIC + self.salt)
        self._block = 0
        self._pos = 0
        self._pt_read = 0

    def _fill(self) -> bool:
        if self._pt_read >= self._pt_size:
            return False
        want = min(BLOCK, self._pt_size - self._pt_read)
        data = self._src.read(want)
        if len(data) != want:
            raise IOError("plaintext source ended early")
        self._buf += self._aead.encrypt(_nonce(self._block), data, _aad(self.salt, self._block))
        self._block += 1
        self._pt_read += want
        return True

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self._pos
        while len(self._buf) < n and self._fill():
            pass
        out = bytes(self._buf[:n])
        del self._buf[:n]
        self._pos += len(out)
        return out

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_END and offset == 0:
            return self.size
        if whence == os.SEEK_SET and offset == 0:
            self._reset()
            return 0
        if whence == os.SEEK_SET and offset == self._pos:
            return self._pos
        raise OSError("EncryptingReader only supports rewinding to 0")

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._src.close()

    @property
    def name(self) -> str:
        return getattr(self._src, "name", "part")


# ------------------------------------------------------------------ decrypt
class BlockDecryptor:
    """Feed ciphertext bytes starting at block `first`; get plaintext out."""

    def __init__(self, key: bytes, salt: bytes, first_block: int, pt_size: int):
        self._aead = AESGCM(subkey(key, salt))
        self._salt = salt
        self._i = first_block
        self._buf = bytearray()
        self._pt_size = pt_size

    def _block_len(self) -> int:
        remaining_pt = self._pt_size - self._i * BLOCK
        return min(BLOCK, remaining_pt) + TAG if remaining_pt > 0 else 0

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        while True:
            need = self._block_len()
            if need == 0 or len(self._buf) < need:
                break
            ct = bytes(self._buf[:need])
            del self._buf[:need]
            out += self._aead.decrypt(_nonce(self._i), ct, _aad(self._salt, self._i))
            self._i += 1
        return bytes(out)

    def finish(self) -> bytes:
        if self._buf:
            raise ValueError(f"{len(self._buf)} trailing ciphertext bytes")
        return b""


def parse_header(head: bytes) -> bytes:
    if len(head) < HEADER or head[:4] != MAGIC:
        raise ValueError("not an OTS encrypted part")
    return head[4:HEADER]


async def decrypt_range(manager, document, part, key: bytes, pt_from: int, pt_len: int, chunk: int = 512 * 1024):
    """Async generator: plaintext bytes [pt_from, pt_from+pt_len) of an
    encrypted part stored as `document` in the channel."""
    if pt_len <= 0:
        return
    salt = bytes.fromhex(part.enc_salt)
    ct_off, ct_len, first = ct_range_for(pt_from, pt_len, part.size)
    dec = BlockDecryptor(key, salt, first, part.size)
    skip = pt_from - first * BLOCK
    remaining = pt_len
    async for piece in manager.iter_download(document, ct_off, ct_len, chunk):
        pt = dec.feed(piece)
        if skip:
            drop = min(skip, len(pt))
            pt = pt[drop:]
            skip -= drop
        if not pt:
            continue
        if len(pt) > remaining:
            pt = pt[:remaining]
        remaining -= len(pt)
        yield pt
        if remaining <= 0:
            break
    dec.finish()
