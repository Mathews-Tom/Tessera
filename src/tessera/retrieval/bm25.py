"""BM25 keyword recall via SQLite FTS5.

``facets_fts`` is populated by the schema-level triggers at capture time,
so this module is pure read — a parameterised query per facet-type
request, returning top-``k`` rows scored by FTS5's built-in BM25
implementation. Deterministic tie-break is ``facets.id`` ascending so
two rows with identical BM25 rank always order the same way.

FTS5 query syntax uses double-quote escaping for phrase-wrapped literals.
The query builder wraps the user-supplied query in a single phrase so
agent-authored input containing operators (`AND`, `OR`, `*`, `"`) does
not get interpreted as FTS5 syntax — the agent expressed a bag-of-words
query, not a structured one.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlcipher3


@dataclass(frozen=True, slots=True)
class BM25Candidate:
    facet_id: int
    external_id: str
    facet_type: str
    content: str
    score: float
    rank: int


def search(
    conn: sqlcipher3.Connection,
    *,
    query_text: str,
    agent_id: int,
    facet_type: str,
    limit: int = 50,
) -> list[BM25Candidate]:
    """Return the top-``limit`` BM25 hits for ``query_text`` within one type.

    An empty or whitespace-only query returns an empty list rather than
    erroring; FTS5 would raise on the empty MATCH expression and the
    retrieval pipeline's recall-everything fallback belongs one layer up.
    """

    stripped = query_text.strip()
    if not stripped:
        return []
    if limit <= 0:
        raise ValueError(f"limit must be positive; got {limit}")
    fts_query = _quote_phrase(stripped)
    rows = conn.execute(
        """
        SELECT f.id, f.external_id, f.facet_type, f.content, bm25(facets_fts) AS score
        FROM facets_fts
        JOIN facets AS f ON f.id = facets_fts.rowid
        WHERE facets_fts MATCH ?
          AND f.is_deleted = 0
          AND f.agent_id = ?
          AND f.facet_type = ?
        ORDER BY score ASC, f.id ASC
        LIMIT ?
        """,
        (fts_query, agent_id, facet_type, limit),
    ).fetchall()
    return [
        BM25Candidate(
            facet_id=int(row[0]),
            external_id=str(row[1]),
            facet_type=str(row[2]),
            content=str(row[3]),
            score=float(row[4]),
            rank=idx,
        )
        for idx, row in enumerate(rows)
    ]


def _quote_phrase(query_text: str) -> str:
    """Tokenise query_text and join per-token quoted literals.

    Wrapping the entire query as one FTS5 phrase would enforce adjacency —
    ``retrieval pipeline`` would miss documents containing both words
    non-adjacent. Splitting on whitespace and quoting each token (with
    ``""`` doubling for literal inner quotes) gives bag-of-words recall
    with FTS5's default AND semantics across the resulting tokens, while
    still neutralising operator tokens (``AND``, ``OR``, ``NOT``, ``*``)
    the user did not mean as syntax.
    """

    tokens = [tok for tok in query_text.split() if tok]
    if not tokens:
        return '""'
    quoted: list[str] = []
    for tok in tokens:
        escaped = tok.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " ".join(quoted)
