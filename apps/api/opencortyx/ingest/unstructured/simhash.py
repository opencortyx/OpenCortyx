"""SimHash (Locality-Sensitive Hashing) for hyperscale near-duplicate document detection.

At a trillion-document scale, semantic vector search for "is this just a v2 of an existing doc?" is
computationally impossible. SimHash compresses a document into a tiny 64-bit fingerprint with the
key LSH property: two documents that are ~90% identical produce fingerprints that differ by only a
handful of bits. So "is X a version of Y?" becomes an O(1)-style integer Hamming-distance check,
never a heavy model call.

Deterministic by construction: token hashing uses blake2b (NOT Python's per-process-salted hash()),
so the same text always yields the same fingerprint across processes and restarts.
"""
import hashlib
import re

_WORD = re.compile(r"[A-Za-z0-9]+")


def _token_hash(token: str) -> int:
    """Stable 64-bit hash of a token (process-independent — blake2b, not salted hash())."""
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def _shingles(text: str, ngram: int = 1) -> dict:
    """Word shingles → {shingle: weight}. Word-level (ngram=1, bag-of-words) is the default: for
    DOCUMENT VERSION detection it keeps a lightly-edited 'v2' only a few bits from its 'v1' (empirically
    ~4 bits for a 10% edit) while leaving unrelated docs far away (~25 bits) — and it's robust to
    reordered paragraphs. Higher ngram makes the fingerprint phrase-sensitive (use for plagiarism)."""
    words = _WORD.findall((text or "").lower())
    if not words:
        return {}
    ngram = max(1, int(ngram))
    grams: dict = {}
    if len(words) < ngram:
        grams[" ".join(words)] = 1
        return grams
    for i in range(len(words) - ngram + 1):
        g = " ".join(words[i:i + ngram])
        grams[g] = grams.get(g, 0) + 1
    return grams


def simhash64(text: str, ngram: int = 1) -> int:
    """64-bit SimHash fingerprint of `text`. Empty/blank text → 0."""
    shingles = _shingles(text, ngram)
    if not shingles:
        return 0
    bit_votes = [0] * 64
    for shingle, weight in shingles.items():
        h = _token_hash(shingle)
        for i in range(64):
            if (h >> i) & 1:
                bit_votes[i] += weight
            else:
                bit_votes[i] -= weight
    fingerprint = 0
    for i in range(64):
        if bit_votes[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two 64-bit fingerprints (0 = identical)."""
    return bin((int(a) ^ int(b)) & ((1 << 64) - 1)).count("1")


def to_str(fingerprint: int) -> str:
    """Persist a fingerprint as a string (avoids 53-bit float precision loss in JSON/JS)."""
    return str(int(fingerprint))


def from_str(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
