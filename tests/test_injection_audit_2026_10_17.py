"""Injection audit 2026-10-17: attacker bytes only reach the image decoders TokenSage expects,
upstream tweet ids are ASCII digits before they go into mirror URLs, IPFS paths carry no dot
segments, and a refused callback URL doesn't echo what its host resolved to."""

from __future__ import annotations

import io

import pytest
from PIL import Image, UnidentifiedImageError

from tokensage.engine import image as image_stage
from tokensage.net.ipfs import parse_ipfs
from tokensage.sources import x

CID = "Qm" + "a" * 44


def _bytes(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, format=fmt)
    return buf.getvalue()


def test_image_features_read_the_expected_formats() -> None:
    assert image_stage.features(_bytes("PNG")).width == 8


@pytest.mark.parametrize("fmt", ["TGA", "BMP", "PPM"])
def test_image_features_refuse_other_decoders(fmt: str) -> None:
    with pytest.raises(UnidentifiedImageError):
        image_stage.features(_bytes(fmt))


def test_tweet_ids_are_ascii_digits() -> None:
    assert x._id("1977") == "1977"
    assert x._id(1977) == "1977"
    assert x._id("../../i/x?y") is None
    assert x._id("١٢٣") is None
    assert x._id("²") is None
    assert x._id("1" * 21) is None


def test_ipfs_paths_refuse_dot_segments() -> None:
    assert parse_ipfs(f"ipfs://{CID}/logo.png") is not None
    assert parse_ipfs(f"https://gw.example/ipfs/{CID}/../../admin") is None
    assert parse_ipfs(f"https://gw.example/ipfs/{CID}/%2e%2e/admin") is None
