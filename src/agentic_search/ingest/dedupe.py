"""Exact duplicates (content hash) and near-duplicates (MinHash over word 3-shingles)."""

from __future__ import annotations

import hashlib
import re

from datasketch import MinHash, MinHashLSH

NUM_PERM = 128


def content_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]


def shingles(text: str, k: int = 3) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def minhash(text: str) -> MinHash:
    m = MinHash(num_perm=NUM_PERM)
    for s in shingles(text):
        m.update(s.encode("utf-8"))
    return m


NEAR_DUP_THRESHOLD = 0.7  # an updated CV with one added role keeps ~75–90 % of its 3-shingles; unrelated CVs stay < 0.3


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def near_duplicate_groups(texts: dict[int, str], threshold: float = NEAR_DUP_THRESHOLD) -> dict[int, int]:
    """Return {doc_no: group_id} for every document that has at least one near-duplicate.

    Groups are connected components of pairs whose exact shingle Jaccard is >= threshold. LSH only
    proposes candidates and misses pairs just above its own threshold, so it is queried at
    threshold − 0.2. A group's id is its smallest doc_no.
    """
    lsh = MinHashLSH(threshold=max(0.2, threshold - 0.2), num_perm=NUM_PERM)
    hashes: dict[int, MinHash] = {}
    for doc_no, text in texts.items():
        m = minhash(text)
        hashes[doc_no] = m
        lsh.insert(str(doc_no), m)

    parent = {d: d for d in texts}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    shingle_cache: dict[int, set[str]] = {}

    def sh(d: int) -> set[str]:
        if d not in shingle_cache:
            shingle_cache[d] = shingles(texts[d])
        return shingle_cache[d]

    for doc_no, m in hashes.items():
        for other in lsh.query(m):
            o = int(other)
            if o < doc_no and jaccard(sh(o), sh(doc_no)) >= threshold:
                parent[find(o)] = find(doc_no)

    components: dict[int, list[int]] = {}
    for d in texts:
        components.setdefault(find(d), []).append(d)

    result: dict[int, int] = {}
    gid = 0
    for members in sorted(components.values(), key=min):
        if len(members) > 1:
            gid += 1
            for m in members:
                result[m] = gid
    return result
