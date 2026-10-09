"""Canonical bibliographic identifiers; normalization never performs network I/O."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit


def _input(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 1024:
        raise ValueError("identifier must be nonblank text of at most 1024 bytes")
    return value.strip()


def _resolver_path(value: str, hosts: set[str]) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in hosts or parsed.username or parsed.password:
        raise ValueError("identifier URL must use its recognized public resolver")
    return unquote(parsed.path).lstrip("/")


def normalize_doi(value: str) -> str:
    value = re.sub(r"^doi:\s*", "", _input(value), flags=re.IGNORECASE)
    if value.lower().startswith(("http://", "https://")):
        value = _resolver_path(value, {"doi.org", "www.doi.org", "dx.doi.org"})
    if not re.fullmatch(r"10\.[0-9]{4,9}/[^\s\x00-\x1f\x7f]+", value):
        raise ValueError("invalid DOI; use its identifier or doi.org URL")
    return value.lower()


def normalize_arxiv(value: str) -> str:
    value = re.sub(r"^arxiv:\s*", "", _input(value), flags=re.IGNORECASE)
    if value.lower().startswith(("http://", "https://")):
        value = _resolver_path(value, {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}).rstrip("/")
        if not value.startswith(("abs/", "pdf/")):
            raise ValueError("arXiv URL must identify an abstract or PDF")
        pdf = value.startswith("pdf/")
        value = value[4:]
        if pdf:
            value = value.removesuffix(".pdf")
    modern = re.fullmatch(r"([0-9]{2})(0[1-9]|1[0-2])\.([0-9]{4,5})(v[1-9][0-9]*)?", value, flags=re.IGNORECASE)
    if modern:
        year, month, number, version = modern.groups()
        date = int(year + month)
        expected = 4 if 704 <= date <= 1412 else 5
        if len(number) != expected or int(number) == 0 or 700 <= date < 704:
            raise ValueError("arXiv sequence does not match its publication month")
        return f"{year}{month}.{number}{(version or '').lower()}"
    legacy = re.fullmatch(r"([a-z][a-z-]*)(?:\.([a-z]{2}))?/([0-9]{2})(0[1-9]|1[0-2])([0-9]{3})(v[1-9][0-9]*)?", value, flags=re.IGNORECASE)
    if legacy:
        archive, category, year, month, number, version = legacy.groups()
        date = int(year + month)
        if (date < 9107 and date > 703) or int(number) == 0:
            raise ValueError("legacy arXiv identifier is outside its publication period")
        return f"{archive.lower()}{'.' + category.upper() if category else ''}/{year}{month}{number}{(version or '').lower()}"
    raise ValueError("invalid arXiv identifier")


def arxiv_key(value: str) -> str:
    # Citation versions stay in metadata. Identity groups versions and redundant
    # legacy subject classes into the underlying preprint, without merging them.
    value = re.sub(r"v[1-9][0-9]*$", "", normalize_arxiv(value))
    return re.sub(r"\.[A-Z]{2}/", "/", value)


def normalize_isbn(value: str) -> str:
    from stdnum import isbn
    from stdnum.exceptions import ValidationError

    value = re.sub(r"^isbn(?:-?1[03])?\s*:\s*", "", _input(value), flags=re.IGNORECASE)
    try:
        return isbn.validate(value, convert=True)
    except ValidationError as error:
        raise ValueError("invalid ISBN length, prefix or checksum") from error


def normalize_pmid(value: str) -> str:
    value = re.sub(r"^pmid:\s*", "", _input(value), flags=re.IGNORECASE)
    if value.lower().startswith(("http://", "https://")):
        value = _resolver_path(value, {"pubmed.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"}).rstrip("/")
        value = value.removeprefix("pubmed/")
    if not re.fullmatch(r"[0-9]{1,12}", value) or int(value) == 0:
        raise ValueError("PMID must be a positive PubMed identifier")
    return str(int(value))


NORMALIZERS = {"doi": normalize_doi, "arxiv": normalize_arxiv, "isbn": normalize_isbn, "pmid": normalize_pmid}


def normalize_identifier(kind: str, value: str) -> str:
    if kind not in NORMALIZERS:
        raise ValueError("unknown reference identifier kind")
    return NORMALIZERS[kind](value)


def identifier_key(kind: str, value: str) -> str:
    return arxiv_key(value) if kind == "arxiv" else normalize_identifier(kind, value)
