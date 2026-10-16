"""Object storage for uploaded files: Supabase Storage, or a local folder.

The database keeps only metadata (the ``files`` table). The bytes live here,
in a private bucket, and are read through short-lived signed URLs. Nothing in
this module is reachable from the browser except a signed URL.
"""

import hashlib
import hmac
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from app.core.errors import ServiceUnavailableError


class StorageError(Exception):
    """The storage service refused or failed an operation."""


@dataclass(frozen=True)
class SignedUrl:
    url: str
    expires_at: float  # Unix time


@dataclass(frozen=True)
class StoredObject:
    path: str
    modified_at: float  # Unix time


class ObjectStorage(Protocol):
    bucket: str

    def put(self, path: str, data: bytes, content_type: str) -> None: ...
    def signed_url(self, path: str, expires_in: int) -> SignedUrl: ...
    def delete(self, path: str) -> None: ...
    def list_objects(self) -> Iterator[StoredObject]: ...


class SupabaseStorage:
    """Supabase Storage REST API, called with the server-side secret key."""

    def __init__(
        self,
        supabase_url: str,
        secret_key: str,
        bucket: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = f"{supabase_url.rstrip('/')}/storage/v1"
        self.bucket = bucket
        # New-style keys (sb_secret_...) are not JWTs and go in apikey only;
        # a legacy service_role key is a JWT and is also sent as the bearer token.
        headers = {"apikey": secret_key}
        if not secret_key.startswith("sb_"):
            headers["Authorization"] = f"Bearer {secret_key}"
        self._client = httpx.Client(headers=headers, timeout=30, transport=transport)

    def _object(self, path: str) -> str:
        return f"{self._base}/object/{self.bucket}/{quote(path)}"

    def _call(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise StorageError("The file storage service could not be reached.") from exc
        if response.status_code >= 400:
            # The body may name the bucket or path; keep it out of client messages.
            raise StorageError(f"File storage answered {response.status_code}.")
        return response

    def put(self, path: str, data: bytes, content_type: str) -> None:
        self._call(
            "POST",
            self._object(path),
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "false"},
        )

    def signed_url(self, path: str, expires_in: int) -> SignedUrl:
        response = self._call(
            "POST",
            f"{self._base}/object/sign/{self.bucket}/{quote(path)}",
            json={"expiresIn": expires_in},
        )
        signed = response.json().get("signedURL") or response.json().get("signedUrl")
        if not signed:
            raise StorageError("File storage did not return a signed URL.")
        return SignedUrl(url=f"{self._base}{signed}", expires_at=time.time() + expires_in)

    def delete(self, path: str) -> None:
        self._call("DELETE", self._object(path))

    _PAGE = 1000

    def list_objects(self, prefix: str = "") -> Iterator[StoredObject]:
        """Every object under the prefix, walking folders (entries without an id)."""
        offset = 0
        while True:
            entries = self._call(
                "POST",
                f"{self._base}/object/list/{self.bucket}",
                json={"prefix": prefix, "limit": self._PAGE, "offset": offset},
            ).json()
            for entry in entries:
                path = f"{prefix}/{entry['name']}" if prefix else entry["name"]
                if entry.get("id") is None:
                    yield from self.list_objects(path)
                    continue
                stamp = entry.get("updated_at") or entry.get("created_at")
                modified = (
                    datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                    if stamp
                    else time.time()  # unknown age: treated as new, so never purged
                )
                yield StoredObject(path, modified)
            if len(entries) < self._PAGE:
                return
            offset += self._PAGE


# Signs local URLs. Restarting the server invalidates outstanding links, which is fine:
# they only ever live for minutes.
_LOCAL_SIGNING_KEY = secrets.token_bytes(32)


def _local_signature(path: str, expires: int) -> str:
    return hmac.new(_LOCAL_SIGNING_KEY, f"{path}\n{expires}".encode(), hashlib.sha256).hexdigest()


class LocalStorage:
    """Files in a folder on this machine. For development and tests, never production."""

    bucket = "local"

    def __init__(self, root: Path, public_base_url: str) -> None:
        self.root = root.resolve()
        self._base = public_base_url.rstrip("/")

    def resolve(self, path: str) -> Path:
        target = (self.root / path).resolve()
        if self.root not in target.parents:
            raise StorageError("Invalid object path.")
        return target

    def put(self, path: str, data: bytes, content_type: str) -> None:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def signed_url(self, path: str, expires_in: int) -> SignedUrl:
        expires = int(time.time()) + expires_in
        url = (
            f"{self._base}/api/v1/files/local/{quote(path)}"
            f"?expires={expires}&signature={_local_signature(path, expires)}"
        )
        return SignedUrl(url=url, expires_at=expires)

    def delete(self, path: str) -> None:
        self.resolve(path).unlink(missing_ok=True)

    def list_objects(self) -> Iterator[StoredObject]:
        if not self.root.is_dir():
            return
        for target in self.root.rglob("*"):
            if target.is_file():
                yield StoredObject(target.relative_to(self.root).as_posix(), target.stat().st_mtime)

    @staticmethod
    def signature_valid(path: str, expires: int, signature: str) -> bool:
        if expires < time.time():
            return False
        return hmac.compare_digest(_local_signature(path, expires), signature)


def unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "File storage is not configured on the server, so files cannot be stored yet."
    )
