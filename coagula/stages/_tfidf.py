"""Dependency-free TF-IDF helpers shared by Relevance and Summarize.

The corpus is small (per-funnel: a handful of chunks for Relevance, a few
dozen sentences for Summarize) so a simple Python implementation is fast
enough and avoids pulling in scikit-learn or numpy.

Known limitation: TF-IDF mis-ranks on small corpora and short critical lines.
Correctness for must-keep content does NOT come from this ranker; it comes
from CRITICAL pinning at ingestion. See SPEC §6.4.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _tfidf_vec(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    if not tokens:
        return {}
    tf = Counter(tokens)
    inv_len = 1.0 / len(tokens)
    return {t: (count * inv_len) * idf.get(t, 0.0) for t, count in tf.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    # Iterate over the smaller dict for speed.
    if len(a) > len(b):
        a, b = b, a
    dot = 0.0
    for k, va in a.items():
        vb = b.get(k)
        if vb is not None:
            dot += va * vb
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def rank_against_query(docs: list[str], query: str) -> list[float]:
    """Return one cosine score per doc, ranking against the query."""
    if not docs:
        return []
    tokenized = [tokenize(d) for d in docs]
    query_tokens = tokenize(query)

    # Build IDF over docs + query so query terms get a non-zero weight even
    # when they appear in only one document.
    n = len(tokenized) + 1
    df: Counter[str] = Counter()
    for doc_tokens in tokenized:
        for t in set(doc_tokens):
            df[t] += 1
    for t in set(query_tokens):
        df[t] += 1
    idf = {t: math.log((n + 1) / (count + 1)) + 1.0 for t, count in df.items()}

    qv = _tfidf_vec(query_tokens, idf)
    return [_cosine(_tfidf_vec(toks, idf), qv) for toks in tokenized]


def centrality_scores(docs: list[str]) -> list[float]:
    """Average pairwise cosine similarity for each doc within the set."""
    n = len(docs)
    if n <= 1:
        return [0.0] * n
    tokenized = [tokenize(d) for d in docs]
    df: Counter[str] = Counter()
    for doc_tokens in tokenized:
        for t in set(doc_tokens):
            df[t] += 1
    idf = {t: math.log((n + 1) / (count + 1)) + 1.0 for t, count in df.items()}
    vecs = [_tfidf_vec(toks, idf) for toks in tokenized]
    out = [0.0] * n
    for i in range(n):
        s = 0.0
        for j in range(n):
            if i == j:
                continue
            s += _cosine(vecs[i], vecs[j])
        out[i] = s / (n - 1)
    return out
