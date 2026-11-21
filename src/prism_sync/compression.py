"""Small crypto/compression helpers shared by the protocol modules."""

from __future__ import annotations

import lz4.frame
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def aes_ctr_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CTR with a 128-bit big-endian counter (``cryptography`` default)."""
    if len(key) != 16:
        raise ValueError(f"AES key must be 16 bytes, got {len(key)}")
    if len(iv) != 16:
        raise ValueError(f"AES IV must be 16 bytes, got {len(iv)}")

    decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def decompress_lz4_all(data: bytes) -> bytes:
    """Decode one or more concatenated LZ4 frames.

    ``lz4.frame.decompress`` only handles a single frame.  The game currently
    sends one frame per object, but concatenated frames are valid and cheap to
    support.
    """
    output = bytearray()
    remaining = data
    while remaining:
        decoder = lz4.frame.LZ4FrameDecompressor()
        output += decoder.decompress(remaining)
        if not decoder.eof:
            raise ValueError("truncated LZ4 frame")
        remaining = decoder.unused_data
    return bytes(output)
