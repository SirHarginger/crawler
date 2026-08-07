from __future__ import annotations

import gzip
import io
import zipfile

import pytest

from screening_crawler.containers import decode_gzip, decode_zip
from screening_crawler.errors import QuarantineRequired


def _zip(name: str, content: bytes) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(name, content)
    return target.getvalue()


def test_gzip_decoding_is_bounded_and_versioned() -> None:
    compressed = gzip.compress(b"synthetic")
    result = decode_gzip(compressed, maximum_uncompressed_bytes=100)
    assert result.content == b"synthetic"
    assert result.codec_version == "python-gzip-v1"
    with pytest.raises(QuarantineRequired):
        decode_gzip(compressed, maximum_uncompressed_bytes=2)


def test_zip_decoding_rejects_unsafe_paths_and_size_growth() -> None:
    with pytest.raises(QuarantineRequired):
        decode_zip(
            _zip("../escape.xml", b"synthetic"),
            maximum_parts=2,
            maximum_part_bytes=100,
            maximum_total_bytes=100,
        )
    with pytest.raises(QuarantineRequired):
        decode_zip(
            _zip("safe.xml", b"synthetic"),
            maximum_parts=2,
            maximum_part_bytes=2,
            maximum_total_bytes=100,
        )

