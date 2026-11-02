"""Persist Chinese word boundaries once at ingestion, rather than per query."""
import re
import jieba


def tokens(text):
    return [t.lower() for t in jieba.lcut(text) if t.strip() and re.search(r"\w", t)]


def fts_query(text):
    # Quote every token: imported/user text is never FTS syntax or SQL.
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in dict.fromkeys(tokens(text[:1000])) if len(t) <= 100)
