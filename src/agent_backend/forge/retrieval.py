"""Versioned, bounded local code retrieval: BM25 plus path and symbol fields.

This is lexical retrieval, not embedding-based semantic search. Python symbols
come from the standard-library AST; JavaScript/TypeScript symbols use a small
declaration recognizer and do not claim full language parsing.
"""

from __future__ import annotations

import ast
import hashlib
import io
import math
import re
import stat
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_FILES = 2000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_CHUNKS = 20000
EXCLUDED = {".git", ".codex", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
STOP_WORDS = frozenset("a an and are as at be by for from how i in is it of on or that the this to was what when where which with would we can do does should".split())
TOKEN_ESTIMATOR = "ceil(UTF-8 bytes / 3) for citation and source text; heuristic, not a model tokenizer"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text.encode("utf-8")) / 3)


def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    result = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]*|[\u3400-\u9fff]+", text.casefold()):
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            result.extend(word[index:index + 2] for index in range(max(1, len(word) - 1)))
            continue
        if word in STOP_WORDS:
            continue
        # Deliberately small normalization, shared by documents and queries.
        if len(word) > 5 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us")):
            word = word[:-1]
        result.append(word)
    return result


def safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Paths must be relative POSIX paths")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts):
        raise ValueError("Unsafe snapshot path")
    if len(value.encode()) > 2048:
        raise ValueError("Snapshot path too long")
    return PurePosixPath(*parts).as_posix()


def excluded(path: str) -> bool:
    return any(part.casefold() in EXCLUDED or part.casefold() == ".env" or part.casefold().startswith(".env.") for part in PurePosixPath(path).parts)


@dataclass(frozen=True)
class Symbol:
    name: str
    start_line: int
    end_line: int
    kind: str


@dataclass(frozen=True)
class SearchHit:
    path: str
    start_line: int
    end_line: int
    text: str
    score: float
    symbols: tuple[str, ...]
    source_version: str
    source_digest: str
    citation: str


@dataclass(frozen=True)
class SearchResponse:
    hits: list[SearchHit]
    estimated_tokens: int
    truncated: bool
    query_terms: tuple[str, ...]
    candidates_scored: int
    token_estimator: str = TOKEN_ESTIMATOR


@dataclass(frozen=True)
class Chunk:
    path: str
    start: int
    end: int
    text: str
    symbols: tuple[str, ...]


def python_symbols(source: str) -> list[Symbol]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    found = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def declaration(self, node, kind):
            name = ".".join([*self.scope, node.name])
            start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
            found.append(Symbol(name, start, node.end_lineno or node.lineno, kind))
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.declaration(node, "function")

        def visit_AsyncFunctionDef(self, node):
            self.declaration(node, "async_function")

        def visit_ClassDef(self, node):
            self.declaration(node, "class")

    Visitor().visit(tree)
    return sorted(found, key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name))


def javascript_symbols(source: str) -> list[Symbol]:
    pattern = re.compile(
        r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\s*\*?\s+|class\s+)([A-Za-z_$][\w$]*)"
        r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:\([^\n]*\)|[A-Za-z_$][\w$]*)\s*=>",
        re.MULTILINE,
    )
    lines = source.splitlines()
    declarations = [(source.count("\n", 0, match.start()) + 1, match.group(1) or match.group(2)) for match in pattern.finditer(source)]
    return [Symbol(name, start, min(len(lines), declarations[index + 1][0] - 1 if index + 1 < len(declarations) else len(lines)), "js_declaration") for index, (start, name) in enumerate(declarations)]


class BM25Field:
    def __init__(self, documents: list[list[str]]):
        self.lengths = [len(document) for document in documents]
        self.average = sum(self.lengths) / max(1, len(documents))
        self.postings: dict[str, dict[int, int]] = defaultdict(dict)
        for index, document in enumerate(documents):
            for term, frequency in Counter(document).items():
                self.postings[term][index] = frequency
        self.idf = {term: math.log(1 + (len(documents) - len(posting) + 0.5) / (len(posting) + 0.5)) for term, posting in self.postings.items()}

    def score(self, index: int, terms: list[str]) -> float:
        score = 0.0
        for term in terms:
            frequency = self.postings.get(term, {}).get(index, 0)
            if frequency:
                denominator = frequency + 1.2 * (0.25 + 0.75 * self.lengths[index] / max(1, self.average))
                score += self.idf[term] * frequency * 2.2 / denominator
        return score


class CodeIndex:
    def __init__(self, files: dict[str, str], *, source_version: str, skipped_binary: int = 0):
        if not isinstance(source_version, str) or not source_version.strip() or len(source_version) > 200 or any(ord(char) < 32 for char in source_version):
            raise ValueError("A bounded source_version is required")
        self.source_version = source_version
        self.files = files
        digest = hashlib.sha256()
        self.symbols: dict[str, list[Symbol]] = {}
        self.chunks: list[Chunk] = []
        for path, source in sorted(files.items()):
            digest.update(path.encode() + b"\0" + source.encode() + b"\0")
            lines = source.splitlines()
            if len(lines) > 50000:
                raise ValueError("A source file exceeds the 50000-line indexing limit")
            symbols = python_symbols(source) if path.endswith(".py") else javascript_symbols(source) if path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs")) else []
            self.symbols[path] = symbols
            spans = {(start, min(len(lines), start + 79)) for start in range(1, len(lines) + 1, 68)}
            for symbol in symbols:
                spans.add((symbol.start_line, min(symbol.end_line, symbol.start_line + 79)))
            for start, end in sorted(spans):
                text = "\n".join(lines[start - 1:end])
                if not text.strip():
                    continue
                names = tuple(symbol.name for symbol in symbols if symbol.start_line <= end and symbol.end_line >= start)
                self.chunks.append(Chunk(path, start, end, text, names))
                if len(self.chunks) > MAX_CHUNKS:
                    raise ValueError("Snapshot exceeds the indexing chunk limit")
        self.source_digest = digest.hexdigest()
        self.body = BM25Field([tokens(chunk.text) for chunk in self.chunks])
        self.paths = BM25Field([tokens(chunk.path) for chunk in self.chunks])
        self.names = BM25Field([tokens(" ".join(chunk.symbols)) for chunk in self.chunks])
        self.stats = {"files": len(files), "chunks": len(self.chunks), "symbols": sum(map(len, self.symbols.values())), "skipped_binary": skipped_binary}

    @classmethod
    def from_files(cls, files: dict[str, str | bytes], *, source_version: str) -> CodeIndex:
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            raise ValueError("Snapshot must contain at most 2000 files")
        accepted, total, binary = {}, 0, 0
        for raw_path, value in files.items():
            path = safe_path(raw_path)
            if excluded(path):
                continue
            if not isinstance(value, (str, bytes)):
                raise ValueError("File contents must be text or bytes")
            raw = value.encode("utf-8") if isinstance(value, str) else value
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("Snapshot exceeds byte limits")
            try:
                text = raw.decode("utf-8")
                if "\0" in text:
                    raise UnicodeError
            except UnicodeError:
                binary += 1
                continue
            accepted[path] = text
        return cls(accepted, source_version=source_version, skipped_binary=binary)

    @classmethod
    def from_zip(cls, archive: bytes, *, source_version: str) -> CodeIndex:
        if not isinstance(archive, bytes) or len(archive) > MAX_TOTAL_BYTES + MAX_FILES * 1024:
            raise ValueError("Invalid or oversized snapshot ZIP")
        files: dict[str, bytes] = {}
        total, seen = 0, set()
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                if len(zipped.infolist()) > MAX_FILES * 2:
                    raise ValueError("Too many ZIP entries")
                for entry in zipped.infolist():
                    path = safe_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
                    if entry.orig_filename != entry.filename or path in seen:
                        raise ValueError("Malformed or duplicate ZIP path")
                    seen.add(path)
                    kind = stat.S_IFMT(entry.external_attr >> 16)
                    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise ValueError("ZIP symlinks and special files are forbidden")
                    if entry.is_dir() or excluded(path):
                        continue
                    total += entry.file_size
                    if entry.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                        raise ValueError("Expanded ZIP exceeds limits")
                    with zipped.open(entry) as stream:
                        data = stream.read(MAX_FILE_BYTES + 1)
                    if len(data) != entry.file_size:
                        raise ValueError("ZIP size mismatch")
                    files[path] = data
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as error:
            raise ValueError("Invalid snapshot ZIP") from error
        return cls.from_files(files, source_version=source_version)

    def search(self, query: str, *, limit: int = 5, max_tokens: int = 1800, mode: str = "hybrid", candidate_budget: int = 2000, max_per_file: int = 2) -> SearchResponse:
        """Budget evidence rendered as citation + newline + source, not JSON metadata."""
        if not isinstance(query, str) or len(query) > 4096:
            raise ValueError("Query must contain at most 4096 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if any(not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum for value, maximum in [(max_tokens, 16000), (candidate_budget, 10000), (max_per_file, 20)]):
            raise ValueError("Invalid query budget")
        if not isinstance(mode, str) or mode not in {"hybrid", "lexical", "path"}:
            raise ValueError("Unknown retrieval mode")
        all_terms = list(dict.fromkeys(tokens(query)))
        terms = all_terms[:64]
        if not terms or not self.chunks:
            return SearchResponse([], 0, len(all_terms) > 64, tuple(terms), 0)
        fields = [self.body] if mode == "lexical" else [self.body, self.paths, self.names]
        candidates: dict[int, float] = defaultdict(float)
        if mode == "path":
            # Deliberately naive baseline: word overlap with paths, deterministic
            # zero-score ties. It does not get to inspect source or declarations.
            for index, chunk in enumerate(self.chunks):
                candidates[index] = len(set(terms) & set(tokens(chunk.path)))
        else:
            for field in fields:
                for term in terms:
                    for index in field.postings.get(term, {}):
                        candidates[index] += field.idf[term]
        chosen = sorted(candidates, key=lambda index: (-candidates[index], self.chunks[index].path, self.chunks[index].start))[:candidate_budget]
        scored = []
        for index in chosen:
            chunk = self.chunks[index]
            score = candidates[index] if mode == "path" else self.body.score(index, terms)
            if mode == "hybrid":
                score += 0.8 * self.paths.score(index, terms) + 1.5 * self.names.score(index, terms)
                if any(name.casefold() == query.strip().casefold() or name.split(".")[-1].casefold() == query.strip().casefold() for name in chunk.symbols):
                    score += 3.0
            if score > 0 or mode == "path":
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].path, item[1].start, item[1].end))
        hits, used, per_file = [], 0, Counter()
        truncated = len(all_terms) > 64 or len(candidates) > candidate_budget
        for score, chunk in scored:
            if len(hits) == limit:
                break
            if per_file[chunk.path] >= max_per_file:
                continue
            if any(hit.path == chunk.path and max(0, min(hit.end_line, chunk.end) - max(hit.start_line, chunk.start) + 1) >= 0.5 * min(hit.end_line - hit.start_line + 1, chunk.end - chunk.start + 1) for hit in hits):
                continue
            remaining = max_tokens - used
            lines, kept = chunk.text.splitlines(), []
            relative_start = 0
            whole_citation = f"{self.source_version}:{chunk.path}:L{chunk.start}-L{chunk.end}\n"
            if estimate_tokens(whole_citation + chunk.text) > remaining:
                line_scores = [sum(self.body.idf.get(term, 0) for term in set(tokens(line)) & set(terms)) for line in lines]
                anchor = max(range(len(lines)), key=lambda index: line_scores[index])
                # Spend the budget on the actual matching line first; optional
                # leading context must not displace the evidence itself.
                relative_start = anchor
                anchor_citation = f"{self.source_version}:{chunk.path}:L{chunk.start + anchor}-L{chunk.start + anchor}\n"
                if estimate_tokens(anchor_citation + lines[anchor]) > remaining:
                    truncated = True
                    continue
                for before in range(anchor - 1, max(-1, anchor - 3), -1):
                    candidate_citation = f"{self.source_version}:{chunk.path}:L{chunk.start + before}-L{chunk.start + anchor}\n"
                    if estimate_tokens(candidate_citation + "\n".join(lines[before:anchor + 1])) > remaining:
                        break
                    relative_start = before
            start_line = chunk.start + relative_start
            for line in lines[relative_start:]:
                candidate = "\n".join([*kept, line])
                end = start_line + len(kept)
                header = f"{self.source_version}:{chunk.path}:L{start_line}-L{end}\n"
                if estimate_tokens(header + candidate) > remaining:
                    break
                kept.append(line)
            if not kept or not "\n".join(kept).strip():
                truncated = True
                continue
            end = start_line + len(kept) - 1
            citation = f"{self.source_version}:{chunk.path}:L{start_line}-L{end}"
            snippet = "\n".join(kept)
            used += estimate_tokens(citation + "\n" + snippet)
            truncated |= len(kept) < len(lines)
            hits.append(SearchHit(chunk.path, start_line, end, snippet, round(score, 8), chunk.symbols, self.source_version, self.source_digest, citation))
            per_file[chunk.path] += 1
        return SearchResponse(hits, used, truncated, tuple(terms), len(chosen))
