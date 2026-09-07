"""Shared serializers."""
from app.models import File, FilePart, Folder, User
from app.transfers.worker import progress as _progress


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role.value, "is_active": u.is_active,
            "created_at": u.created_at}


def folder_out(f: Folder) -> dict:
    return {"id": f.id, "name": f.name, "parent_id": f.parent_id, "created_at": f.created_at}


def file_out(f: File, parts: list[FilePart] | None = None) -> dict:
    parts = parts if parts is not None else (f.parts or [])
    uploaded = sum(1 for p in parts if p.message_id is not None)
    live = _progress.active.get(f.id)
    done_bytes = sum(p.size for p in parts if p.message_id is not None)
    if live:
        done_bytes += live["sent"]
    return {
        "id": f.id, "name": f.name, "size": f.size, "mime_type": f.mime_type,
        "sha256": f.sha256, "is_archive": f.is_archive, "folder_id": f.folder_id,
        "status": f.status.value, "error": f.error, "retries": f.retries,
        "part_size": f.part_size, "parts_total": len(parts), "parts_uploaded": uploaded,
        "bytes_done": min(done_bytes, f.size), "created_at": f.created_at, "ready_at": f.ready_at,
        "current_part": live,
    }
