"""Pinned, bounded decoding for compressed source artifacts."""

from __future__ import annotations

import gzip
import io
import zipfile
from dataclasses import dataclass

from .canonical import sha256_bytes
from .errors import QuarantineRequired


@dataclass(frozen=True, slots=True)
class DecodedPart:
    name: str
    codec: str
    codec_version: str
    content: bytes
    sha256: str


def decode_gzip(
    body: bytes,
    *,
    maximum_uncompressed_bytes: int,
    codec_version: str = "python-gzip-v1",
) -> DecodedPart:
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as stream:
        content = stream.read(maximum_uncompressed_bytes + 1)
    if len(content) > maximum_uncompressed_bytes:
        raise QuarantineRequired("gzip artifact exceeds configured uncompressed size limit")
    return DecodedPart("payload", "gzip", codec_version, content, sha256_bytes(content))


def decode_zip(
    body: bytes,
    *,
    maximum_parts: int,
    maximum_part_bytes: int,
    maximum_total_bytes: int,
    codec_version: str = "python-zip-v1",
) -> tuple[DecodedPart, ...]:
    output: list[DecodedPart] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = sorted((item for item in archive.infolist() if not item.is_dir()), key=lambda item: item.filename)
        if len(members) > maximum_parts:
            raise QuarantineRequired("zip artifact contains too many parts")
        for member in members:
            if member.filename.startswith(("/", "\\")) or ".." in member.filename.replace("\\", "/").split("/"):
                raise QuarantineRequired("zip artifact contains an unsafe member path")
            if member.file_size > maximum_part_bytes:
                raise QuarantineRequired("zip member exceeds configured size limit")
            with archive.open(member, "r") as stream:
                content = stream.read(maximum_part_bytes + 1)
            if len(content) > maximum_part_bytes:
                raise QuarantineRequired("zip member expanded beyond configured size limit")
            total += len(content)
            if total > maximum_total_bytes:
                raise QuarantineRequired("zip artifact exceeds configured total size limit")
            output.append(
                DecodedPart(
                    member.filename,
                    "zip",
                    codec_version,
                    content,
                    sha256_bytes(content),
                )
            )
    return tuple(output)

