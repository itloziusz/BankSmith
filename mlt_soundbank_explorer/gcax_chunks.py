from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .core import *
from .dsp import *
from .audio import *

@dataclass(frozen=True)
class ChildChunkInfo:
    header: int
    body: int
    size: int
    body_end: int
    next_header: int
    magic: bytes


def child_chunks(data: bytes, container_body: int, container_end: int) -> List[ChildChunkInfo]:
    """Parse gcax child chunks while preserving real inter-chunk padding.

    Real Sega/GameCube banks commonly fill gaps and the tail of a container
    with 0x55 (and occasionally 0x00). Those bytes are padding, not another
    chunk header. Keep the padding attached to the preceding child via the
    next_header field so repacking can preserve it byte-for-byte.
    """
    chunks: List[ChildChunkInfo] = []
    pos = int(container_body)
    container_end = int(container_end)

    def skip_padding(offset: int) -> int:
        while offset < container_end and data[offset] in (0x00, 0x55):
            offset += 1
        return offset

    pos = skip_padding(pos)
    while pos < container_end:
        if pos + 16 > container_end:
            tail = data[pos:container_end]
            if all(value in (0x00, 0x55) for value in tail):
                break
            raise ValueError("Truncated child chunk header in container")

        size = u32be(data, pos + 12)
        body = pos + 16
        body_end = body + size
        if body_end > container_end:
            raise ValueError(
                f"Child chunk {data[pos:pos + 8]!r} extends beyond its container"
            )

        next_header = skip_padding(body_end)
        chunks.append(
            ChildChunkInfo(
                header=pos,
                body=body,
                size=size,
                body_end=body_end,
                next_header=next_header,
                magic=data[pos:pos + 8],
            )
        )
        pos = next_header

    return chunks

def find_chunk(data: bytes, magic: bytes, start: int = 0) -> Tuple[int, int, int]:
    pos = data.find(magic, start)
    if pos < 0:
        raise ValueError(f"Missing chunk {magic!r}")
    if pos + 16 > len(data):
        raise ValueError(f"Truncated chunk header for {magic!r}")
    size = u32be(data, pos + 12)
    return pos, pos + 16, size


def find_child_chunk(data: bytes, magic: bytes, container_body: int, container_end: int) -> Tuple[int, int, int]:
    """Find a declared, aligned child chunk without scanning audio payload bytes."""
    for chunk in child_chunks(data, container_body, container_end):
        if chunk.magic == magic:
            return chunk.header, chunk.body, chunk.size
    raise ValueError(f"Missing child chunk {magic!r}")

