"""Frozen, small code-retrieval benchmark plus provenance and import checks.

This is a deterministic regression fixture, not a production-quality estimate.
Relevance labels are written by hand from the code below before retrieving.
The baseline and candidate receive identical files, queries, and token budgets.
"""

from __future__ import annotations

import io
import json
import stat
import textwrap
import zipfile
from dataclasses import dataclass

import pytest

from agent_backend.forge.retrieval import CodeIndex

VERSION = "fixture-commerce-v1"


def source(value: str) -> str:
    return textwrap.dedent(value).lstrip("\n")


FILES = {
    "web/handlers.py": source('''
        """Purchase HTTP endpoints. Money movement belongs to the domain service."""
        import hashlib
        import json

        class RequestConflict(Exception):
            pass

        async def submit_purchase(request, database, payments):
            """Mobile retries after a timeout must not create a second charge."""
            customer = request.identity.customer_id
            retry_key = request.headers["Idempotency-Key"]
            payload = await request.json()
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            async with database.transaction() as transaction:
                previous = await transaction.find_request_for_update(customer, retry_key)
                if previous:
                    if previous.fingerprint != fingerprint:
                        raise RequestConflict("A retry key cannot be reused for a different payload")
                    return {"order_id": previous.order_id, "replayed": True}
                order = await payments.reserve(transaction, customer, payload)
                await transaction.save_request(customer, retry_key, fingerprint, order.id)
                await transaction.append_pending_event("order.accepted", {"order_id": order.id})
            return {"order_id": order.id, "replayed": False}

        async def get_purchase(request, database):
            customer = request.identity.customer_id
            return await database.find_order(customer, request.path_params["order_id"])
    '''),
    "domain/ledger.py": source('''
        """Ledger arithmetic uses decimal values, never binary floating point."""
        from decimal import ROUND_HALF_EVEN, Decimal

        CENT = Decimal("0.01")

        def invoice_total(lines, tax_rate):
            """Round currency with bankers' half-even rounding before saving totals."""
            subtotal = sum((Decimal(line["unit_price"]) * line["quantity"] for line in lines), Decimal(0))
            tax = (subtotal * Decimal(tax_rate)).quantize(CENT, rounding=ROUND_HALF_EVEN)
            return (subtotal + tax).quantize(CENT, rounding=ROUND_HALF_EVEN)

        async def refund_payment(database, payment_id, amount):
            """Refund balances and the pending notification change in one transaction."""
            amount = Decimal(amount).quantize(CENT, rounding=ROUND_HALF_EVEN)
            if amount <= 0:
                raise ValueError("Refund amount must be positive")
            async with database.transaction() as transaction:
                payment = await transaction.lock_payment(payment_id)
                if amount > payment.refundable_balance:
                    raise ValueError("Refund exceeds the remaining balance")
                await transaction.decrease_balance(payment_id, amount)
                await transaction.append_pending_event("payment.refunded", {
                    "payment_id": payment_id, "amount": str(amount),
                })
            return {"refunded": str(amount)}
    '''),
    "jobs/worker.py": source('''
        """Durable deliveries have visibility leases and a bounded attempt count."""
        from datetime import timedelta

        MAX_ATTEMPTS = 5

        def retry_delay(attempt):
            """Cap exponential backoff so a transient failure cannot sleep forever."""
            return min(60, 2 ** attempt)

        async def recover_expired_deliveries(database, now):
            """Return abandoned deliveries to the queue without resetting attempts."""
            for delivery in await database.expired_visibility_leases(now):
                await database.release(delivery.id, available_at=now, attempts=delivery.attempts)

        async def process_delivery(database, delivery, handler, now):
            try:
                await handler(delivery.payload)
            except Exception as failure:
                attempt = delivery.attempts + 1
                if attempt >= MAX_ATTEMPTS:
                    await database.dead_letter(delivery.id, reason=str(failure), attempts=attempt)
                    return "dead_lettered"
                available = now + timedelta(seconds=retry_delay(attempt))
                await database.release(delivery.id, available_at=available, attempts=attempt)
                return "retry_scheduled"
            await database.acknowledge(delivery.id)
            return "acknowledged"
    '''),
    "security/credentials.py": source('''
        """Session refresh values are stored as digests and rotated after use."""
        import hashlib
        import secrets

        def digest_token(token):
            return hashlib.sha256(token.encode()).hexdigest()

        async def rotate_refresh_token(database, presented_token):
            """Reuse of a spent refresh token revokes the entire session family."""
            async with database.transaction() as transaction:
                token = await transaction.lock_refresh(digest_token(presented_token))
                if token is None:
                    return {"error": "invalid_refresh"}
                if token.consumed_at is not None:
                    await transaction.revoke_family(token.family_id)
                    return {"error": "refresh_reuse_detected"}
                if token.expired:
                    return {"error": "refresh_expired"}
                replacement = secrets.token_urlsafe(32)
                await transaction.consume_refresh(token.id)
                await transaction.insert_refresh(token.family_id, digest_token(replacement))
            return {"refresh_token": replacement}
    '''),
    "infra/cache.py": source('''
        """A per-key lock coalesces a burst of reads while preserving stale values."""
        import asyncio
        import time

        class SharedValueCache:
            def __init__(self):
                self.values = {}
                self.refreshes = {}
                self.lock = asyncio.Lock()

            async def get(self, key, loader, ttl=30):
                """Concurrent requests share one refresh; stale data remains usable."""
                value = self.values.get(key)
                if value and value[1] > time.monotonic():
                    return value[0]
                async with self.lock:
                    future = self.refreshes.get(key)
                    if future is None:
                        future = asyncio.create_task(self._load(key, loader, ttl))
                        self.refreshes[key] = future
                if value:
                    return value[0]
                return await asyncio.shield(future)

            async def _load(self, key, loader, ttl):
                try:
                    value = await loader()
                    self.values[key] = (value, time.monotonic() + ttl)
                    return value
                finally:
                    self.refreshes.pop(key, None)
    '''),
    "io/importer.py": source('''
        """Validate all uploaded ZIP members before writing any source file."""
        import stat
        from pathlib import PurePosixPath

        def inspect_archive(archive):
            """Reject traversal, absolute paths, symbolic links, and expansion bombs."""
            total = 0
            accepted = []
            seen = set()
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Archive path escapes the destination")
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("Archive symbolic links are forbidden")
                if member.filename in seen:
                    raise ValueError("Duplicate archive member")
                seen.add(member.filename)
                total += member.file_size
                if total > 20 * 1024 * 1024:
                    raise ValueError("Expanded upload is too large")
                accepted.append(member)
            return accepted
    '''),
    "web/channels.py": source('''
        """Server-sent updates resume from a client cursor and stop on disconnect."""
        import asyncio
        import json

        async def follow_updates(request, database, task_id):
            """Reconnect after the last delivered event id instead of replaying everything."""
            cursor = int(request.headers.get("Last-Event-ID", "0"))
            while not await request.is_disconnected():
                rows = await database.events_after(task_id, cursor, limit=100)
                for row in rows:
                    cursor = row.id
                    yield "id: %s\\ndata: %s\\n\\n" % (row.id, json.dumps(row.payload))
                if await database.is_finished(task_id) and not rows:
                    return
                yield ": keepalive\\n\\n"
                await asyncio.sleep(0.5)

        async def cancel_operation(database, task_id):
            await database.request_cancel(task_id)
            return {"cancel_requested": True}
    '''),
    "infra/outbox.py": source('''
        """Persist an event with the business change, then publish committed rows."""
        async def publish_pending(database, message_bus):
            """Pending notifications become visible only after the transaction commits."""
            rows = await database.select_committed_pending(limit=100)
            for row in rows:
                try:
                    await message_bus.send(row.topic, row.payload, deduplication_id=row.id)
                except ConnectionError:
                    # Leave the pending row available for a later delivery attempt.
                    continue
                await database.mark_published(row.id)

        async def append_notification(transaction, topic, payload):
            """The caller supplies the transaction that changes the business balance."""
            return await transaction.append_pending_event(topic, payload)
    '''),
    "ui/live.js": source('''
        // A generation counter prevents old responses from replacing a newly selected view.
        let generation = 0;
        let activeController = null;
        let lastEventId = 0;

        export async function loadConversation(conversationId, render) {
          const ownGeneration = ++generation;
          if (activeController) activeController.abort();
          activeController = new AbortController();
          lastEventId = 0;
          const response = await fetch(`/conversations/${conversationId}`, {
            signal: activeController.signal,
          });
          const payload = await response.json();
          if (ownGeneration !== generation) return; // Drop a stale server reply.
          render(payload);
        }

        export function cancelLiveRead() {
          // Cancel the browser fetch; the server notices the disconnect and stops polling.
          generation += 1;
          if (activeController) activeController.abort();
        }

        export function rememberEvent(event) {
          lastEventId = Number(event.lastEventId || lastEventId);
          return { headers: { 'Last-Event-ID': String(lastEventId) } };
        }
    '''),
}


@dataclass(frozen=True)
class RetrievalCase:
    identifier: str
    query: str
    relevant_paths: frozenset[str]


# Do not derive these labels from search results, filename heuristics, or the query.
CASES = (
    RetrievalCase("duplicate-charge", "Which code prevents a second charge when a mobile client repeats the same purchase after a timeout?", frozenset({"web/handlers.py"})),
    RetrievalCase("money-rounding", "Where are decimal currency values rounded using half-even before invoice totals are saved?", frozenset({"domain/ledger.py"})),
    RetrievalCase("abandoned-delivery", "When a consumer dies, where is an expired delivery returned to the queue without losing its attempt counter?", frozenset({"jobs/worker.py"})),
    RetrievalCase("spent-token", "How does reuse of an old refresh token revoke the whole session family?", frozenset({"security/credentials.py"})),
    RetrievalCase("shared-refresh", "Where do concurrent requests share a single refresh while the previous cached value stays usable?", frozenset({"infra/cache.py"})),
    RetrievalCase("unsafe-upload", "How are uploaded zip entries checked to stop traversal and symbolic links before any files are written?", frozenset({"io/importer.py"})),
    RetrievalCase("resume-updates", "Where does reconnect resume the event stream after the last delivered event id?", frozenset({"web/channels.py", "ui/live.js"})),
    RetrievalCase("committed-notification", "How are pending notifications made visible to the publisher only after the database transaction has committed?", frozenset({"infra/outbox.py"})),
    RetrievalCase("stale-response", "What stops an older server reply from replacing the view after the user selects a different conversation?", frozenset({"ui/live.js"})),
    RetrievalCase("poison-message", "Where are retry delays capped and a repeatedly failing delivery sent to a dead-letter destination?", frozenset({"jobs/worker.py"})),
    RetrievalCase("refund-notification", "Where do refund balances and pending notifications change in the same transaction, and where are those notifications later delivered?", frozenset({"domain/ledger.py", "infra/outbox.py"})),
    RetrievalCase("cancel-live-read", "When a live request is cancelled, where are both the browser fetch and the server polling stopped?", frozenset({"ui/live.js", "web/channels.py"})),
)


def evaluate(index: CodeIndex, mode: str) -> dict:
    """Macro Recall@3 and MRR@5 over distinct files, not repeated code chunks.

    The retrieval engine receives the same five-hit and 1800-token budget in every
    mode. Multiple chunks from one file count as one retrieved file; they cannot
    artificially inflate recall. MRR is zero if none of the five hits is relevant.
    """
    rows = []
    for case in CASES:
        response = index.search(case.query, limit=5, max_tokens=1800, mode=mode)
        ranked_paths = list(dict.fromkeys(hit.path for hit in response.hits))
        recall = len(set(ranked_paths[:3]) & case.relevant_paths) / len(case.relevant_paths)
        reciprocal_rank = next((1 / rank for rank, path in enumerate(ranked_paths[:5], 1) if path in case.relevant_paths), 0.0)
        rows.append({
            "query_id": case.identifier,
            "relevant_paths": sorted(case.relevant_paths),
            "retrieved_paths": ranked_paths,
            "recall_at_3": recall,
            "reciprocal_rank_at_5": reciprocal_rank,
        })
    return {
        "mode": mode,
        "query_count": len(rows),
        "recall_at_3": sum(row["recall_at_3"] for row in rows) / len(rows),
        "mrr_at_5": sum(row["reciprocal_rank_at_5"] for row in rows) / len(rows),
        "queries": rows,
    }


def make_zip(files) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files:
            archive.writestr(path, content)
    return output.getvalue()


@pytest.fixture
def index():
    return CodeIndex.from_files(FILES, source_version=VERSION)


def test_frozen_benchmark_compares_measured_hybrid_to_path(index, tmp_path, record_property):
    assert len(FILES) == 9 and len(CASES) == 12
    assert len({case.identifier for case in CASES}) == len(CASES)
    assert all(case.relevant_paths and case.relevant_paths <= FILES.keys() for case in CASES)
    report = {"source_version": VERSION, "rank_unit": "distinct_file", "max_hits": 5, "max_tokens": 1800,
              "results": [evaluate(index, mode) for mode in ("hybrid", "lexical", "path")]}
    # The artifact contains measured rankings and labels, allowing every aggregate
    # to be recomputed. No expected score is substituted for an observed score.
    (tmp_path / "retrieval-evaluation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("RETRIEVAL_EVALUATION=" + json.dumps(report, sort_keys=True))
    record_property("retrieval_evaluation", json.dumps(report, sort_keys=True))
    hybrid, _lexical, baseline = report["results"]
    assert hybrid["recall_at_3"] >= baseline["recall_at_3"]
    assert hybrid["mrr_at_5"] >= baseline["mrr_at_5"]
    assert hybrid["recall_at_3"] >= 0.5, "At least half the labelled evidence should be discoverable"
    assert hybrid["mrr_at_5"] > 0


@pytest.mark.parametrize("mode", ["hybrid", "lexical", "path"])
def test_every_hit_quotes_original_lines_and_version(index, mode):
    for case in CASES:
        response = index.search(case.query, limit=5, max_tokens=1800, mode=mode)
        assert 0 <= response.estimated_tokens <= 1800
        assert len(response.hits) <= 5
        for hit in response.hits:
            assert hit.path in FILES
            lines = FILES[hit.path].splitlines()
            assert 1 <= hit.start_line <= hit.end_line <= len(lines)
            assert hit.text.rstrip("\r\n") == "\n".join(lines[hit.start_line - 1:hit.end_line])
            assert hit.source_version == VERSION
            assert isinstance(hit.score, (int, float))
            assert hit.path in hit.citation and str(hit.start_line) in hit.citation


def test_python_ast_function_and_class_symbols_are_returned(index):
    function = index.search("rotate_refresh_token", limit=3, max_tokens=1800)
    assert function.hits and function.hits[0].path == "security/credentials.py"
    assert any("rotate_refresh_token" in symbol for hit in function.hits for symbol in hit.symbols)
    klass = index.search("SharedValueCache", limit=3, max_tokens=1800)
    assert klass.hits and klass.hits[0].path == "infra/cache.py"
    assert any("SharedValueCache" in symbol for hit in klass.hits for symbol in hit.symbols)


def test_javascript_export_symbol_is_searchable(index):
    response = index.search("loadConversation", limit=3, max_tokens=1800)
    assert response.hits and response.hits[0].path == "ui/live.js"
    assert any("loadConversation" in symbol for hit in response.hits for symbol in hit.symbols)


def test_zip_and_file_import_preserve_the_same_evidence():
    zipped = CodeIndex.from_zip(make_zip(FILES.items()), source_version=VERSION)
    plain = CodeIndex.from_files(FILES, source_version=VERSION)
    query = "rotate_refresh_token"
    from_zip = zipped.search(query, limit=5, max_tokens=1800)
    from_files = plain.search(query, limit=5, max_tokens=1800)
    def signature(response):
        return [(hit.path, hit.start_line, hit.end_line, hit.text, hit.source_version) for hit in response.hits]

    assert signature(from_zip) == signature(from_files)


def test_utf8_bytes_valid_nested_path_and_final_line_are_preserved():
    files = {"src/计费.py": "def charge_total():\n    # 金额必须使用十进制\n    return 42".encode()}
    current = CodeIndex.from_files(files, source_version="revision-2")
    response = current.search("charge_total", max_tokens=1800)
    assert response.hits
    hit = response.hits[0]
    assert hit.path == "src/计费.py" and hit.source_version == "revision-2"
    assert hit.end_line <= 3 and "return 42" in hit.text


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "src/../../escape.py", "C:/escape.py", "src\\..\\escape.py"])
def test_unsafe_file_paths_are_not_indexed(path):
    with pytest.raises(ValueError):
        CodeIndex.from_files({path: "def secret():\n    return 1\n"}, source_version=VERSION)


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "src/../../escape.py", "C:/escape.py", "src\\..\\escape.py"])
def test_zip_traversal_paths_are_rejected(path):
    with pytest.raises(ValueError):
        CodeIndex.from_zip(make_zip([("valid.py", "x = 1"), (path, "x = 2")]), source_version=VERSION)


@pytest.mark.parametrize("path", [".git/config", ".env", "src/.env.production", ".codex/auth.json", "node_modules/dependency.js", ".venv/package.py"])
@pytest.mark.parametrize("kind", ["files", "zip"])
def test_secrets_and_dependencies_are_safely_excluded(path, kind):
    files = {"src/visible.py": "def visible():\n    return 42", path: "ConfidentialSentinel"}
    current = (CodeIndex.from_files(files, source_version=VERSION) if kind == "files"
               else CodeIndex.from_zip(make_zip(files.items()), source_version=VERSION))
    assert current.search("ConfidentialSentinel", mode="hybrid").hits == []
    assert all(hit.path != path for hit in current.search("ConfidentialSentinel", mode="path").hits)


def test_zip_symlink_is_rejected():
    link = zipfile.ZipInfo("linked.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError):
        CodeIndex.from_zip(make_zip([(link, "../outside.py")]), source_version=VERSION)


def test_small_token_budget_and_hit_limit_are_observed(index):
    result = index.search("pending notification transaction", limit=2, max_tokens=32)
    assert len(result.hits) <= 2
    assert 0 <= result.estimated_tokens <= 32
    assert isinstance(result.truncated, bool)
    assert result.truncated, "Matching evidence omitted or clipped by a tiny budget must be declared"


@pytest.mark.parametrize("budget", [32, 60])
def test_small_budget_keeps_the_matching_line_inside_a_long_function(budget):
    lines = ["def reconcile_records(records):"]
    lines.extend(f"    padding_{number} = records" for number in range(35))
    matching_line = "    quarantine_expired_lease(records)"
    lines.append(matching_line)
    match_number = len(lines)
    lines.extend(f"    padding_{number} = records" for number in range(35, 70))
    lines.append("    return records")
    current = CodeIndex.from_files({"service.py": "\n".join(lines)}, source_version="long-function-v1")
    response = current.search("quarantine_expired_lease", limit=1, max_tokens=budget)
    assert response.hits and response.truncated
    assert response.estimated_tokens <= budget
    hit = response.hits[0]
    assert hit.start_line <= match_number <= hit.end_line
    assert matching_line in hit.text
    assert hit.text == "\n".join(lines[hit.start_line - 1:hit.end_line])


def test_blank_query_returns_no_irrelevant_code(index):
    result = index.search("   ", limit=5, max_tokens=1800)
    assert result.hits == []
    assert result.estimated_tokens == 0
