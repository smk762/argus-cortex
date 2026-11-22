from __future__ import annotations

import hashlib

from hypothesis import given
from hypothesis import strategies as st

import argus_cortex
from argus_cortex import content_key, sha256_hex


def test_known_digests() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@given(st.binary())
def test_matches_hashlib_and_the_blob_key(data: bytes) -> None:
    digest = sha256_hex(data)
    assert digest == hashlib.sha256(data).hexdigest() == content_key(data)
    assert len(digest) == 64 and digest == digest.lower()


def test_exported_at_the_top_level() -> None:
    assert "sha256_hex" in argus_cortex.__all__
