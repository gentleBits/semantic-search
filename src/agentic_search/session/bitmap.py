"""Roaring bitmaps ↔ the base64 text stored in rs_NN.json."""

from __future__ import annotations

import base64

from pyroaring import BitMap


def encode(bm: BitMap) -> str:
    return base64.b64encode(bm.serialize()).decode("ascii")


def decode(text: str) -> BitMap:
    return BitMap.deserialize(base64.b64decode(text))
