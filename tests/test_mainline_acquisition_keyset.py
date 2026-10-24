"""OPT-04 SYNTHETIC SQL-expression export tests; no database, including SQLite.

The fake evaluates the actual SELECT predicates, ordering, projection and LIMIT.
PostgreSQL transaction assertions check API calls only, not real DB isolation.
"""

from __future__ import annotations

import hashlib
import operator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, func, literal_column, select, text
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    Grouping,
    TextClause,
    UnaryExpression,
)
from sqlalchemy.sql.selectable import Select

from ak_trading.acquisition import storage as sto
from ak_trading.testing import offline_guard

NANO = 1_900_000_000_000_000_000
SYMBOL = "SYNTHETIC.A"
EXPORT_FIELDS = {
    "datetime_nano", "content_sha256", "row_status", "evidence_version", "version_id",
}
COMPARISONS = {
    operators.eq: operator.eq,
    operators.ne: operator.ne,
    operators.gt: operator.gt,
    operators.ge: operator.ge,
    operators.lt: operator.lt,
    operators.le: operator.le,
}


@pytest.fixture(scope="module", autouse=True)
def candidate_binding(record_testsuite_property):
    root = Path(__file__).resolve().parents[1]
    expected = root / "src/ak_trading/acquisition/storage.py"
    assert Path.cwd().resolve() == root
    assert Path(sto.__file__).resolve() == expected
    offline_guard.require_installed()
    record_testsuite_property("candidate_root", str(root))
    record_testsuite_property("storage_path", str(expected))
    record_testsuite_property("storage_sha256", hashlib.sha256(expected.read_bytes()).hexdigest())
    record_testsuite_property("resource_class", "SYNTHETIC_NO_NETWORK_NO_SDK_NO_DB")
    record_testsuite_property("real_postgresql_isolation", "NOT_RUN")


def synthetic_row(nano, version, *, symbol=SYMBOL, duration=60, status="CLOSED"):
    return {
        "source_id": "SYNTHETIC",
        "source_symbol": symbol,
        "duration_seconds": duration,
        "datetime_nano": nano,
        "version_id": version,
        "content_sha256": f"{version:064x}",
        "row_status": status,
        "evidence_version": f"SYNTHETIC-evidence-{version}",
    }


def mixed_rows():
    # Deliberately unsorted; version_id falls at the next nanosecond.
    return [
        synthetic_row(NANO + 1, 2),
        synthetic_row(NANO, 47, status="PROVISIONAL"),
        synthetic_row(NANO - 1, 90),
        synthetic_row(NANO, 14, status="PROVISIONAL"),
        synthetic_row(NANO + 2, 91),
        synthetic_row(NANO, 31),
        synthetic_row(NANO, 11),
        synthetic_row(NANO + 3, 93),
        synthetic_row(NANO, 20, status="UNKNOWN"),
        synthetic_row(NANO, 12, symbol="SYNTHETIC.B"),
        synthetic_row(NANO + 1, 94, symbol="SYNTHETIC.B"),
        synthetic_row(NANO, 13, duration=300),
        synthetic_row(NANO + 1, 95, duration=300),
    ]


def evaluate(expression, row):
    """Interpret a closed subset, including every branch without short-circuiting."""
    if isinstance(expression, Grouping):
        return evaluate(expression.element, row)
    if isinstance(expression, BindParameter):
        assert expression.callable is None, "unsupported callable bind"
        return expression.value
    if isinstance(expression, Column) and expression.table is sto.bar_versions:
        return row[expression.key]
    if isinstance(expression, BooleanClauseList):
        values = [evaluate(clause, row) for clause in expression.clauses]
        if expression.operator is operators.and_:
            return all(values)
        if expression.operator is operators.or_:
            return any(values)
    if isinstance(expression, BinaryExpression) and expression.operator in COMPARISONS:
        return COMPARISONS[expression.operator](
            evaluate(expression.left, row), evaluate(expression.right, row)
        )
    raise AssertionError(f"unsupported SQL expression: {type(expression).__name__}")


def order_term(expression):
    descending = False
    if isinstance(expression, UnaryExpression):
        assert expression.modifier in (operators.asc_op, operators.desc_op), "unsupported ordering"
        descending = expression.modifier is operators.desc_op
        expression = expression.element
    assert isinstance(expression, Column) and expression.table is sto.bar_versions, "unsupported ordering"
    return expression, descending


class SyntheticResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return iter(self.rows)


class SyntheticConnection:
    def __init__(self, engine):
        self.engine = engine
        self.active = False
        self.repeatable_read = False
        self.read_only = False

    def execution_options(self, **options):
        assert not self.active, "isolation must be set before entering the connection"
        assert options == {"isolation_level": "REPEATABLE READ"}
        self.repeatable_read = True
        self.engine.events.append("REPEATABLE READ")
        return self

    def __enter__(self):
        assert not self.active
        self.active = True
        self.engine.events.append("enter")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.active = False
        self.engine.events.append("exit")
        return False

    def execute(self, statement):
        assert self.active, "queries must share the active connection"
        if isinstance(statement, TextClause):
            assert statement.text == "SET TRANSACTION READ ONLY", "unsupported SQL text"
            assert self.engine.dialect.name == "postgresql" and self.repeatable_read
            assert not self.read_only and not self.engine.statements
            self.read_only = True
            self.engine.events.append(statement.text)
            return SyntheticResult([])
        assert isinstance(statement, Select), "only SELECT is allowed"
        if self.engine.dialect.name == "postgresql":
            assert self.repeatable_read and self.read_only, "read-only REPEATABLE READ required"
        assert len(self.engine.statements) < len(self.engine.rows) + 2, "pagination did not terminate"
        assert statement._offset_clause is None, "OFFSET is forbidden"
        assert statement._fetch_clause is None, "unsupported FETCH"
        assert statement._for_update_arg is None, "locking SELECT is forbidden"
        assert not statement._distinct, "unsupported DISTINCT"
        assert not statement._group_by_clauses and not statement._having_criteria, "unsupported aggregation"
        assert not statement._prefixes and not statement._suffixes, "unsupported SELECT modifiers"
        tables = statement.get_final_froms()
        assert len(tables) == 1 and tables[0] is sto.bar_versions, "unsupported FROM"
        columns = list(statement.selected_columns)
        probe = synthetic_row(0, 0)
        for column in columns:
            evaluate(column, probe)
        predicate = statement.whereclause
        if predicate is not None:
            evaluate(predicate, probe)  # Unknown expressions fail even for an empty table.
        terms = [order_term(term) for term in statement._order_by_clauses]
        limit = evaluate(statement._limit_clause, probe)
        assert type(limit) is int and limit > 0, "positive integer LIMIT required"
        rows = [
            row for row in self.engine.rows
            if predicate is None or evaluate(predicate, row)
        ]
        for column, descending in reversed(terms):
            rows.sort(key=lambda row: evaluate(column, row), reverse=descending)
        rows = rows[:limit]
        projected = [{column.key: evaluate(column, row) for column in columns} for row in rows]
        self.engine.statements.append(statement)
        self.engine.page_lengths.append(len(projected))
        self.engine.events.append("select")
        return SyntheticResult(projected)


class SyntheticEngine:
    def __init__(self, rows, *, dialect="postgresql"):
        self.rows = [dict(row) for row in rows]
        self.dialect = SimpleNamespace(name=dialect)
        self.events = []
        self.statements = []
        self.page_lengths = []
        self.connect_calls = 0
        self.connection = SyntheticConnection(self)

    def connect(self):
        self.connect_calls += 1
        assert self.connect_calls == 1, "export must not open another connection"
        self.events.append("connect")
        return self.connection


def query_comparisons(expression):
    if isinstance(expression, Grouping):
        return query_comparisons(expression.element)
    if isinstance(expression, BooleanClauseList):
        return [item for clause in expression.clauses for item in query_comparisons(clause)]
    assert isinstance(expression, BinaryExpression)
    assert isinstance(expression.left, Column) and isinstance(expression.right, BindParameter)
    return [(expression.left.key, expression.operator, expression.right.value)]


def assert_original_bounds(engine, symbol, duration, start, end):
    required = {
        ("source_symbol", operators.eq, symbol),
        ("duration_seconds", operators.eq, duration),
        ("datetime_nano", operators.ge, start),
        ("datetime_nano", operators.lt, end),
    }
    for statement in engine.statements:
        assert required <= set(query_comparisons(statement.whereclause))


@pytest.mark.parametrize("page_size, page_lengths", [
    (1, [1, 1, 1, 1, 1, 1, 0]),
    (2, [2, 2, 2, 0]),
    (3, [3, 3, 0]),
])
def test_all_versions_survive_page_boundaries_and_other_rows_stay_isolated(page_size, page_lengths):
    engine = SyntheticEngine(mixed_rows())
    rows = sto.export_existing_versions(
        engine, source_symbol=SYMBOL, duration_seconds=60,
        nano_start=NANO, nano_end=NANO + 2, page_size=page_size,
    )
    assert [(row["datetime_nano"], row["version_id"]) for row in rows] == [
        (NANO, 11), (NANO, 14), (NANO, 20), (NANO, 31), (NANO, 47), (NANO + 1, 2),
    ]
    assert len({row["version_id"] for row in rows}) == 6
    assert all(set(row) == EXPORT_FIELDS for row in rows)
    assert [row["row_status"] for row in rows] == [
        "CLOSED", "PROVISIONAL", "UNKNOWN", "CLOSED", "PROVISIONAL", "CLOSED",
    ]
    originals = {row["version_id"]: row for row in mixed_rows()}
    for row in rows:
        assert row == {key: originals[row["version_id"]][key] for key in EXPORT_FIELDS}
    assert engine.page_lengths == page_lengths
    assert_original_bounds(engine, SYMBOL, 60, NANO, NANO + 2)
    assert engine.events == [
        "connect", "REPEATABLE READ", "enter", "SET TRANSACTION READ ONLY",
        *(["select"] * len(page_lengths)), "exit",
    ]
    assert not engine.connection.active


@pytest.mark.parametrize("page_size", [
    True, False, 0, -1, 1.0, 1.5, "2", None, [], {},
], ids=["true", "false", "zero", "negative", "integral-float", "float", "string", "none", "list", "dict"])
def test_invalid_page_size_is_rejected_before_connect(page_size):
    engine = SyntheticEngine(mixed_rows())
    with pytest.raises(sto.StorageError, match="page_size"):
        sto.export_existing_versions(
            engine, source_symbol=SYMBOL, duration_seconds=60,
            nano_start=NANO, nano_end=NANO + 2, page_size=page_size,
        )
    assert engine.connect_calls == 0


def test_reversed_range_is_rejected_before_connect():
    engine = SyntheticEngine(mixed_rows())
    with pytest.raises(sto.StorageError, match="nano"):
        sto.export_existing_versions(
            engine, source_symbol=SYMBOL, duration_seconds=60,
            nano_start=NANO + 2, nano_end=NANO, page_size=2,
        )
    assert engine.connect_calls == 0


@pytest.mark.parametrize("page_size", [1, 2, 3])
def test_empty_range_returns_empty(page_size):
    engine = SyntheticEngine(mixed_rows())
    assert sto.export_existing_versions(
        engine, source_symbol=SYMBOL, duration_seconds=60,
        nano_start=NANO, nano_end=NANO, page_size=page_size,
    ) == []


@pytest.mark.parametrize("rows", [[], mixed_rows()], ids=["empty-table", "other-symbols"])
def test_no_matching_rows_terminates_on_first_page(rows):
    engine = SyntheticEngine(rows)
    assert sto.export_existing_versions(
        engine, source_symbol="SYNTHETIC.MISSING", duration_seconds=60,
        nano_start=NANO, nano_end=NANO + 2, page_size=2,
    ) == []
    assert engine.page_lengths == [0]
    assert_original_bounds(engine, "SYNTHETIC.MISSING", 60, NANO, NANO + 2)
    assert not engine.connection.active


@pytest.mark.parametrize("page_size, expected_pages", [
    (4, [4, 2]), (6, [6, 0]), (7, [6]),
])
def test_exact_and_partial_final_pages_terminate(page_size, expected_pages):
    engine = SyntheticEngine(mixed_rows())
    rows = sto.export_existing_versions(
        engine, source_symbol=SYMBOL, duration_seconds=60,
        nano_start=NANO, nano_end=NANO + 2, page_size=page_size,
    )
    assert [row["version_id"] for row in rows] == [11, 14, 20, 31, 47, 2]
    assert engine.page_lengths == expected_pages
    assert_original_bounds(engine, SYMBOL, 60, NANO, NANO + 2)


@pytest.mark.parametrize("start", [-3, 0, 2**63 - 3])
def test_nano_bounds_remain_half_open_at_integer_edges(start):
    engine = SyntheticEngine([
        synthetic_row(start + 2, 3), synthetic_row(start, 5),
        synthetic_row(start - 1, 4), synthetic_row(start + 1, 1),
        synthetic_row(start, 2),
    ])
    rows = sto.export_existing_versions(
        engine, source_symbol=SYMBOL, duration_seconds=60,
        nano_start=start, nano_end=start + 2, page_size=1,
    )
    assert [(row["datetime_nano"], row["version_id"]) for row in rows] == [
        (start, 2), (start, 5), (start + 1, 1),
    ]
    assert engine.page_lengths == [1, 1, 1, 0]
    assert_original_bounds(engine, SYMBOL, 60, start, start + 2)


@pytest.mark.parametrize("symbol, duration, expected_ids", [
    (SYMBOL, 60, [11, 14, 20, 31, 47, 2]),
    ("SYNTHETIC.B", 60, [12, 94]),
    (SYMBOL, 300, [13, 95]),
])
def test_requested_symbol_and_duration_are_used_on_every_page(symbol, duration, expected_ids):
    engine = SyntheticEngine(mixed_rows())
    rows = sto.export_existing_versions(
        engine, source_symbol=symbol, duration_seconds=duration,
        nano_start=NANO, nano_end=NANO + 2, page_size=1,
    )
    assert [row["version_id"] for row in rows] == expected_ids
    assert engine.page_lengths == [1] * len(expected_ids) + [0]
    assert_original_bounds(engine, symbol, duration, NANO, NANO + 2)


def test_successor_predicate_is_strict_at_the_previous_full_page_key():
    engine = SyntheticEngine(mixed_rows())
    sto.export_existing_versions(
        engine, source_symbol=SYMBOL, duration_seconds=60,
        nano_start=NANO, nano_end=NANO + 2, page_size=2,
    )
    assert len(engine.statements) == 4
    for statement, last_nano, last_version in zip(
        engine.statements[1:], [NANO, NANO, NANO + 1], [14, 31, 2], strict=True,
    ):
        predicate = statement.whereclause
        assert not evaluate(predicate, synthetic_row(last_nano, last_version))
        assert not evaluate(predicate, synthetic_row(last_nano, last_version - 1))
        assert not evaluate(predicate, synthetic_row(last_nano - 1, 999))
        assert evaluate(predicate, synthetic_row(last_nano, last_version + 1))
    assert evaluate(engine.statements[1].whereclause, synthetic_row(NANO + 1, 1))


def test_synthetic_non_postgresql_connection_does_not_receive_postgresql_commands():
    engine = SyntheticEngine([synthetic_row(NANO, 1)], dialect="synthetic")
    rows = sto.export_existing_versions(
        engine, source_symbol=SYMBOL, duration_seconds=60,
        nano_start=NANO, nano_end=NANO + 1,
    )
    assert [row["version_id"] for row in rows] == [1]
    assert engine.events == ["connect", "enter", "select", "exit"]
    assert not engine.connection.active


def test_synthetic_evaluator_applies_actual_predicate_order_limit_and_projection():
    table = sto.bar_versions.c
    statement = (
        select(table.version_id)
        .where((table.datetime_nano > NANO) | ((table.datetime_nano == NANO) & (table.version_id > 14)))
        .order_by(table.datetime_nano.asc(), table.version_id.desc())
        .limit(2)
    )
    engine = SyntheticEngine([
        synthetic_row(NANO + 1, 2), synthetic_row(NANO, 20),
        synthetic_row(NANO - 1, 99), synthetic_row(NANO, 31),
        synthetic_row(NANO, 11), synthetic_row(NANO, 14),
    ], dialect="synthetic")
    with engine.connect() as connection:
        assert list(connection.execute(statement).mappings()) == [
            {"version_id": 31}, {"version_id": 20},
        ]


@pytest.mark.parametrize("statement", [
    select(sto.bar_versions.c.version_id).where(sto.bar_versions.c.version_id.in_([1, 2])).limit(1),
    select(sto.bar_versions.c.version_id).where(text("1 = 1")).limit(1),
    select(func.abs(sto.bar_versions.c.version_id)).limit(1),
    select(sto.bar_versions.c.version_id).order_by(func.abs(sto.bar_versions.c.version_id)).limit(1),
    select(sto.bar_versions.c.version_id).limit(literal_column("1")),
    select(sto.bar_versions.c.version_id).limit(1).offset(0),
], ids=["unknown-predicate", "text-predicate", "unknown-projection", "unknown-order", "unknown-limit", "offset"])
def test_synthetic_evaluator_rejects_unknown_expressions_even_without_rows(statement):
    engine = SyntheticEngine([], dialect="synthetic")
    with engine.connect() as connection:
        with pytest.raises(AssertionError, match="unsupported|forbidden"):
            connection.execute(statement)


def test_synthetic_evaluator_does_not_hide_unknown_branches():
    table = sto.bar_versions.c
    for expression in [
        (table.version_id == 0) | table.source_symbol.like("SYNTHETIC%"),
        (table.version_id != 0) & table.source_symbol.like("SYNTHETIC%"),
    ]:
        with pytest.raises(AssertionError, match="unsupported SQL expression"):
            evaluate(expression, synthetic_row(0, 0))
