"""Pure-stdlib BM25 retriever over corpus.jsonl (no third-party deps).

Each corpus line is a JSON object: {id, platform, feature, title, text, source}.
search(query, platform=None, k=5) returns the top-k scored docs.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus.jsonl")

_WORD = re.compile(r"[a-z0-9]+")


def _tok(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _load() -> list[dict]:
    docs = []
    with open(CORPUS) as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                d["_tokens"] = _tok(f"{d.get('title','')} {d.get('text','')} "
                                    f"{d.get('platform','')} {d.get('feature','')}")
                docs.append(d)
    return docs


_DOCS = _load()
_N = len(_DOCS)
_AVGLEN = sum(len(d["_tokens"]) for d in _DOCS) / max(_N, 1)
_DF: Counter = Counter()
for _d in _DOCS:
    for _t in set(_d["_tokens"]):
        _DF[_t] += 1


def _idf(term: str) -> float:
    n = _DF.get(term, 0)
    return math.log(1 + (_N - n + 0.5) / (n + 0.5))


def search(query: str, platform: str | None = None, k: int = 5) -> list[dict]:
    """BM25 over the corpus; optional case-insensitive platform substring filter."""
    q = _tok(query)
    k1, b = 1.5, 0.75
    scored = []
    for d in _DOCS:
        if platform and platform.lower() not in d.get("platform", "").lower():
            continue
        tf = Counter(d["_tokens"])
        dl = len(d["_tokens"])
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            f = tf[term]
            s += _idf(term) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / _AVGLEN))
        if s > 0:
            scored.append((s, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{
        "id": d["id"], "platform": d["platform"], "feature": d["feature"],
        "title": d["title"], "text": d["text"], "source": d["source"],
        "score": round(s, 3),
    } for s, d in scored[:k]]


if __name__ == "__main__":
    import sys
    qy = " ".join(sys.argv[1:]) or "cisco 8000 egress priority queue"
    for r in search(qy, k=4):
        print(f"[{r['score']}] {r['platform']} · {r['title']}\n    {r['text'][:120]}...")
