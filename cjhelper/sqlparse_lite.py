"""Read the SQL of a query activity: source tables, joins, filters and select expressions.

Spec, "Table selection": "SQL handling is deliberately limited in the PoC. The app reads
table names, join equalities, simple WHERE filters (equality and date range) and simple
select expressions (column, literal, ``CONCAT``, current date). Anything more complex is
reported, and the affected entry column gets a placeholder."

What is understood (docs/plan.md, "sqlparse_lite.py"):

- Sources: the FROM table and every JOIN table, written ``[Name]``, ``ENT.[Name]`` or
  ``Name``, with or without an alias. Table names arrive already resolved by the loader.
  A source whose row may be absent because of an outer join (the table a LEFT JOIN adds,
  the tables before a RIGHT JOIN, both sides of a FULL JOIN) is ``optional``; it is still
  read as an inner join, with a warning (spec, step 3: "A LEFT JOIN source still gets one row
  per contact (read as an inner join, as before)").
- Joins: ``column = column`` equalities in ON clauses, chained with AND.
- Filters: the WHERE clause split on top-level AND (parentheses around AND groups are
  flattened). ``column = literal`` on either side, ``column IS [NOT] NULL``, and comparisons
  between the current date and a column (``now >= column`` is a start bound, ``now <= column``
  an end bound, whichever side the column is on). ``now BETWEEN a AND b`` counts as both.
  An equality between the date part of a column and the current date moved by a whole number
  of days is a ``DateOffsetFilter`` (spec, step 3, "Columns the entry query derives and columns
  it copies"), see ``_match_date_offset``.
- Select: a column, a string or number literal, the current date (bare or with
  ``AT TIME ZONE``), ``FORMAT(<current date>, '<.NET format>')`` and ``CONCAT`` of these.
  ``SELECT *`` sets ``select_all``. Two columns of one table copied into each other's names
  (``M.Phone AS MobilePhone, M.MobilePhone AS Phone``) are read as written, with a warning.

The current date is ``GETDATE()``, ``GETUTCDATE()`` or ``SYSDATETIME()``; sqlglot reads
``CURRENT_TIMESTAMP`` exactly like ``GETDATE()``, so it is accepted too. In WHERE it may sit
inside ``CAST(... AS DATE)`` (or ``CONVERT(DATE, ...)``, or a DATETIME type) and ``AT TIME ZONE``.
Time zones are not kept: ``DateBoundFilter``, ``DateOffsetFilter`` and ``SelectCurrentDate`` carry none.

Nothing is guessed: whatever is not understood becomes an ``UnsupportedFilter``, a
``SelectUnsupported`` or a join left out, and every such case is also written in plain words
in ``ParsedQuery.warnings``. ``parse_query`` never raises on odd SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.dialects.tsql import TSQL

from cjhelper.model import (
    ColumnRef,
    DateBoundFilter,
    DateOffsetFilter,
    EqualityFilter,
    Filter,
    IsNullFilter,
    JoinEquality,
    ParsedQuery,
    Query,
    SelectColumn,
    SelectConcat,
    SelectCurrentDate,
    SelectExpr,
    SelectItem,
    SelectLiteral,
    SelectNull,
    SelectUnsupported,
    Table,
    TableRef,
    UnsupportedFilter,
)

__all__ = ["parse_query", "exclusion_joins", "dotnet_to_strftime", "unsupported_dotnet_tokens", "CURRENT_DATE_FUNCTIONS", "DOTNET_DATE_TOKENS"]


# T-SQL calls that return the current date and time (plan: "the current date").
CURRENT_DATE_FUNCTIONS = ("GETDATE", "GETUTCDATE", "SYSDATETIME")

# Casts that keep a value a date, accepted around both sides of a date bound.
_DATE_TYPES = (
    exp.DataType.Type.DATE,
    exp.DataType.Type.DATETIME,
    exp.DataType.Type.DATETIME2,
    exp.DataType.Type.SMALLDATETIME,
)

# DATEADD units that move a date by whole days, in days per unit, as sqlglot names them: it writes day, dd and d
# as DAY and week, wk and ww as WEEK, and keeps the other abbreviations. T-SQL's DATEADD gives dayofyear (dy, y)
# and weekday (dw, w) the meaning of day ("dayofyear, day, and weekday return the same value"). Month, quarter and
# year move a date by a number of days that depends on the date: not understood.
_DAY_UNITS = {
    "DAY": 1,
    "DAYOFYEAR": 1, "DY": 1, "Y": 1,
    "WEEKDAY": 1, "DW": 1, "W": 1,
    "WEEK": 7,
}

# DATEADD units shorter than a day, in seconds. They are understood on a date that keeps its time (GETDATE()),
# when the moves add up to whole days before the date part is taken; T-SQL refuses them on a DATE.
_TIME_UNITS = {
    "HOUR": 3600, "HH": 3600,
    "MINUTE": 60, "MI": 60, "N": 60,
    "SECOND": 1, "SS": 1, "S": 1,
}

_SECONDS_PER_DAY = 86400

# DATEADD takes an int amount; a date moved farther than the span of a T-SQL DATE (0001-01-01 to 9999-12-31)
# makes the query fail, so such a filter is not understood.
_INT_RANGE = (-2**31, 2**31 - 1)
_MAX_DAY_OFFSET = 3_652_058

# Parts of a SELECT this module reads. Any other part present is reported as ignored.
_READ_PARTS = ("expressions", "from_", "joins", "where")

# Plain names for the parts reported as ignored, keyed by sqlglot's argument name.
_PART_NAMES = {
    "with_": "WITH",
    "distinct": "DISTINCT",
    "limit": "TOP",
    "group": "GROUP BY",
    "having": "HAVING",
    "order": "ORDER BY",
    "into": "INTO",
    "pivots": "PIVOT",
    "options": "OPTION",
}

# The only table prefix Marketing Cloud SQL uses: ENT.[Name] reads a shared Data Extension.
_SHARED_PREFIX = "ENT"


class _TSQLKeepingFormat(TSQL):
    """T-SQL exactly as sqlglot reads it, except that ``FORMAT(...)`` stays a plain call.

    sqlglot rewrites ``FORMAT(date, '<.NET format>')`` into its own strftime form, which loses
    the format as written and misreads some .NET tokens (``tt`` stays as is, ``YYYY`` becomes
    a year). The plan keeps the .NET format in ``SelectCurrentDate`` and converts it later
    with ``dotnet_to_strftime``, so the text written in the SQL must survive parsing.
    """

    class Parser(TSQL.Parser):
        FUNCTIONS = {name: build for name, build in TSQL.Parser.FUNCTIONS.items() if name != "FORMAT"}


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def parse_query(query: Query, tables: dict[str, Table]) -> ParsedQuery:
    """Read one query into sources, joins, filters and select items.

    ``tables`` (by table name) is used to resolve unqualified columns and to check columns.
    Spec: "SQL handling is deliberately limited in the PoC [...] Anything more complex is
    reported". Plan: "Never raise on odd SQL: return what was understood and put the rest
    in ``warnings``". SQL that sqlglot cannot parse gives empty lists and one warning.
    """
    result = ParsedQuery(
        name=query.name,
        target_table=query.target_table,
        sources=[],
        joins=[],
        filters=[],
        select=[],
    )
    context = _Context(query.name, tables, result.sources, result.warnings)
    select = _parse_select(query.sql, context)
    if select is None:
        return result
    try:
        _read_select(select, context, result)
    except Exception as error:  # plan: never raise on odd SQL; keep what was read so far
        context.warn(
            f"il codice SQL è stato letto solo in parte a causa di un errore imprevisto "
            f"({type(error).__name__}: {error})"
        )
    return result


# .NET custom date and time format tokens converted by dotnet_to_strftime.
# Names (MMM, MMMM, ddd, dddd) and AM/PM (tt) come out in English, as Python writes them.
DOTNET_DATE_TOKENS: dict[str, str] = {
    "yyyy": "%Y",   # year, 4 digits
    "yy": "%y",     # year, 2 digits
    "MMMM": "%B",   # month name
    "MMM": "%b",    # month name, abbreviated
    "MM": "%m",     # month, 2 digits
    "dddd": "%A",   # day name
    "ddd": "%a",    # day name, abbreviated
    "dd": "%d",     # day of the month, 2 digits
    "HH": "%H",     # hour 00-23
    "hh": "%I",     # hour 01-12
    "mm": "%M",     # minute, 2 digits
    "ss": "%S",     # second, 2 digits
    "tt": "%p",     # AM / PM
}

_TOKEN_LETTERS = set("".join(DOTNET_DATE_TOKENS))


def unsupported_dotnet_tokens(fmt: str) -> list[str]:
    """The .NET date tokens in ``fmt`` that dotnet_to_strftime cannot convert (``d``, ``M``, ``H``,
    ``h``, ``m``, ``s``, ``fff``, ``z``, ``K`` and so on), in order of appearance; empty when all fit."""
    bad: list[str] = []
    i = 0
    letters = set("yMdHhmstfFzK")
    while i < len(fmt):
        char = fmt[i]
        if char in ("'", '"'):
            end = fmt.find(char, i + 1)
            i = len(fmt) if end == -1 else end + 1
        elif char == "\\":
            i += 2
        elif char in letters:
            end = i
            while end < len(fmt) and fmt[end] == char:
                end += 1
            run = fmt[i:end]
            if run not in DOTNET_DATE_TOKENS and run not in bad:
                bad.append(run)
            i = end
        else:
            i += 1
    return bad


def dotnet_to_strftime(fmt: str) -> str:
    """Convert a .NET date format, as written in ``FORMAT(GETDATE(), '...')``, to strftime.

    Supported tokens: see ``DOTNET_DATE_TOKENS`` (yyyy, yy, MMMM, MMM, MM, dddd, ddd, dd,
    HH, hh, mm, ss, tt). A run of the same letter is one token, so ``yyy`` is not read as
    ``yy`` plus ``y``. Literal text is kept: text between single or double quotes, a character
    after a backslash, and any other character; ``%`` is written as ``%%``.

    Anything else, including one-letter tokens (``d``, ``M``, ``H``, ``h``, ``m``, ``s``),
    fractions of a second (``fff``) and one-letter standard formats (``'d'``, ``'G'``),
    is passed through literally, with no error. Example: ``'yyyyMMdd_HHmm'`` -> ``'%Y%m%d_%H%M'``.
    """
    out: list[str] = []
    i = 0
    while i < len(fmt):
        char = fmt[i]
        if char in ("'", '"'):
            end = fmt.find(char, i + 1)
            if end == -1:
                end = len(fmt)
            out.append(_strftime_literal(fmt[i + 1:end]))
            i = end + 1
        elif char == "\\" and i + 1 < len(fmt):
            out.append(_strftime_literal(fmt[i + 1]))
            i += 2
        elif char in _TOKEN_LETTERS:
            end = i
            while end < len(fmt) and fmt[end] == char:
                end += 1
            run = fmt[i:end]
            out.append(DOTNET_DATE_TOKENS.get(run, _strftime_literal(run)))
            i = end
        else:
            out.append(_strftime_literal(char))
            i += 1
    return "".join(out)


def _strftime_literal(text: str) -> str:
    """Literal text for a strftime format: only ``%`` needs escaping."""
    return text.replace("%", "%%")


# ---------------------------------------------------------------------------
# Shared state while reading one query, and column resolution
# ---------------------------------------------------------------------------

@dataclass
class _Context:
    """What the readers share while reading one query. The lists are the result's own."""

    query_name: str
    tables: dict[str, Table]
    sources: list[TableRef]
    warnings: list[str]

    def warn(self, message: str) -> None:
        """Add a plain-words warning naming the query, once."""
        text = f"Query {self.query_name}: {message}"
        if text not in self.warnings:
            self.warnings.append(text)

    def resolve(self, column: exp.Column) -> ColumnRef:
        """Find the table of a column, keeping the column's spelling from the SQL.

        Plan: "alias -> table name; unqualified -> the unique source table having that
        column (case-insensitive, via ``Table.field``); otherwise ``ColumnRef("", col)``
        plus a warning". A bare table name used as qualifier gives that table.
        """
        name = column.name
        qualifier = column.table
        if qualifier:
            return self._resolve_qualified(qualifier, name)
        return self._resolve_unqualified(name)

    def _resolve_qualified(self, qualifier: str, name: str) -> ColumnRef:
        table = self._table_for_qualifier(qualifier)
        if table is None:
            self.warn(
                f"colonna {qualifier}.{name}: {qualifier} non è una tabella né un alias del "
                f"FROM/JOIN, quindi la tabella della colonna è sconosciuta"
            )
            return ColumnRef("", name)
        schema = self.tables.get(table)
        if schema is not None and schema.field(name) is None:
            self.warn(f"colonna {qualifier}.{name}: la tabella {table} non ha un campo {name}")
        return ColumnRef(table, name)

    def _table_for_qualifier(self, qualifier: str) -> Optional[str]:
        """An alias first, then a table name, both compared ignoring letter case."""
        wanted = qualifier.lower()
        for source in self.sources:
            if source.alias and source.alias.lower() == wanted:
                return source.table
        for source in self.sources:
            if source.table.lower() == wanted:
                return source.table
        return None

    def _resolve_unqualified(self, name: str) -> ColumnRef:
        owners: list[str] = []
        for source in self.sources:
            schema = self.tables.get(source.table)
            if schema is not None and schema.field(name) is not None and source.table not in owners:
                owners.append(source.table)
        if len(owners) == 1:
            return ColumnRef(owners[0], name)
        if owners:
            self.warn(
                f"colonna {name}: è presente in più tabelle del FROM/JOIN "
                f"({', '.join(owners)}), quindi la sua tabella è sconosciuta"
            )
        else:
            self.warn(
                f"colonna {name}: nessuna tabella nota del FROM/JOIN la contiene, "
                f"quindi la sua tabella è sconosciuta"
            )
        return ColumnRef("", name)


# ---------------------------------------------------------------------------
# Statement level
# ---------------------------------------------------------------------------

def _parse_select(sql: str, context: _Context) -> Optional[exp.Select]:
    """Parse the SQL and return the SELECT to read, or None with one warning saying why."""
    try:
        statements = [s for s in sqlglot.parse(sql or "", read=_TSQLKeepingFormat) if s is not None]
    except Exception as error:  # sqlglot raises ParseError or TokenError; any failure means the same
        context.warn(f"non è stato possibile analizzare il codice SQL: {_parse_error_text(error)}")
        return None
    if not statements:
        context.warn("il codice SQL è vuoto")
        return None
    if len(statements) > 1:
        context.warn(f"il codice SQL contiene {len(statements)} istruzioni; viene letta solo la prima")
    statement = statements[0]
    if isinstance(statement, (exp.Union, exp.Intersect, exp.Except)):
        context.warn(
            f"la query combina più SELECT con {statement.key.upper()}; "
            f"viene letta solo la prima SELECT"
        )
        statement = _first_select(statement)
    if not isinstance(statement, exp.Select):
        context.warn(f"il codice SQL non è una SELECT (istruzione {statement.key.upper()}); non è stato letto nulla")
        return None
    return statement


# How sqlglot writes a token inside its error messages, e.g. "<Token token_type: ..., text: WHERE, ...>".
_TOKEN_IN_MESSAGE = re.compile(r"<Token token_type: [\w.]+, text: (.*?), line: .*?>")


def _parse_error_text(error: Exception) -> str:
    """The problem sqlglot reports, in one line, with tokens shown as their text."""
    details = getattr(error, "errors", None)
    if details:
        first = details[0]
        description = _TOKEN_IN_MESSAGE.sub(r"'\1'", str(first.get("description")))
        return (
            f"{description}, vicino a '{first.get('highlight')}' "
            f"alla riga {first.get('line')}, colonna {first.get('col')}"
        )
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


def _first_select(statement: exp.Expression) -> exp.Expression:
    """The leftmost SELECT of a UNION, INTERSECT or EXCEPT."""
    while isinstance(statement, (exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        statement = statement.this
    return statement


def _read_select(select: exp.Select, context: _Context, result: ParsedQuery) -> None:
    """Read the parts in an order where every column can be resolved against all sources."""
    _warn_ignored_parts(select, context)
    _read_sources(select, context)
    _read_joins(select, context, result)
    where = select.args.get("where")
    if where is not None:
        _read_filters(where.this, context, result)
    for table in _exclusion_joins(select, context, result):
        context.warn(
            f"la tabella {table} è esclusa con un LEFT JOIN e un filtro IS NULL: entrano solo i contatti che "
            f"non hanno righe in {table}. Il filtro è letto com'è scritto, e nessuna riga in inner join può "
            f"soddisfarlo; i contatti generati sono nuovi, quindi si considera che lo soddisfino"
        )
    _read_select_list(select, context, result)


def _warn_ignored_parts(select: exp.Select, context: _Context) -> None:
    """Report every part of the SELECT that is not read (TOP, DISTINCT, GROUP BY, ...)."""
    for key, value in select.args.items():
        if value and key not in _READ_PARTS:
            context.warn(f"la parte {_PART_NAMES.get(key, key.upper())} della query è ignorata")


# ---------------------------------------------------------------------------
# Sources and joins
# ---------------------------------------------------------------------------

def _read_sources(select: exp.Select, context: _Context) -> None:
    """Spec: source tables are "any table in the FROM or JOIN", with their aliases.

    A source whose row may be absent because of an outer join is ``optional`` (spec, step 3, "A LEFT JOIN
    source still gets one row per contact (read as an inner join, as before); the report says so"): the table
    a LEFT JOIN adds, every table before a RIGHT JOIN, and both sides of a FULL JOIN."""
    from_clause = select.args.get("from_")
    if from_clause is not None:
        _add_source(from_clause.this, context, optional=False)
    for join in select.args.get("joins") or []:
        side = str(join.side or "").upper()
        if side in ("RIGHT", "FULL"):
            context.sources[:] = [replace(source, optional=True) for source in context.sources]
        _add_source(join.this, context, optional=side in ("LEFT", "FULL"))


def _add_source(node: exp.Expression, context: _Context, optional: bool) -> None:
    if not _is_table(node):
        context.warn(f"{_sql(node)} nel FROM/JOIN non è una tabella e non è stato letto")
        return
    name = _canonical_table_name(node.name, context.tables)
    context.sources.append(TableRef(name, node.alias or None, optional))
    if node.catalog or (node.db and node.db.upper() != _SHARED_PREFIX):
        context.warn(f"il prefisso della tabella {name} è ignorato: {_sql(node)}")
    if node.args.get("pivots"):
        context.warn(f"il PIVOT sulla tabella {name} è ignorato")
    if name not in context.tables:
        context.warn(f"la tabella {name} non è nell'export, quindi non è possibile controllarne le colonne")


def _canonical_table_name(name: str, tables: dict[str, Table]) -> str:
    """The export's spelling of a table name when only the letter case differs."""
    if name in tables:
        return name
    for known in tables:
        if known.lower() == name.lower():
            return known
    return name


def _read_joins(select: exp.Select, context: _Context, result: ParsedQuery) -> None:
    """Plan: "joins: equalities from ON clauses (AND-chained); anything else -> warning"."""
    for join in select.args.get("joins") or []:
        if not _is_table(join.this):
            continue  # a subquery or APPLY: already reported by _read_sources
        label = join.this.name
        if join.side:
            context.warn(f"la tabella {label} è unita con un {join.side} JOIN; viene letta come inner join")
        condition = join.args.get("on")
        if condition is None:
            context.warn(f"la tabella {label} è unita senza una condizione ON, quindi non si conosce alcun "
                         f"campo di join")
            continue
        for part in _and_parts(condition):
            _read_join_part(part, context, result)


def _exclusion_joins(select: exp.Select, context: _Context, result: ParsedQuery) -> list[str]:
    """Tables joined with a LEFT JOIN whose columns the WHERE clause keeps only when they are NULL
    (``LEFT JOIN X ON ... WHERE X.key IS NULL``): an exclusion join. A contact enters only when it has
    no row in X. Needs the filters of ``result`` to be read already."""
    left_joined = [
        _canonical_table_name(join.this.name, context.tables)
        for join in select.args.get("joins") or []
        if _is_table(join.this) and str(join.side or "").upper() == "LEFT"
    ]
    tested_null = {f.column.table for f in result.filters if isinstance(f, IsNullFilter) and not f.negated}
    return [table for table in dict.fromkeys(left_joined) if table in tested_null]


def exclusion_joins(query: Query, tables: dict[str, Table]) -> list[str]:
    """The tables of ``query`` used as an exclusion join (``LEFT JOIN X ... WHERE X.key IS NULL``).

    The parser reads them as inner joins plus an IS NULL filter, which no row can meet; the entry
    check uses this list to treat the filter as met by contacts that have no row in the table.
    """
    parsed = parse_query(query, tables)
    context = _Context(query.name, tables, [], [])
    select = _parse_select(query.sql, context)
    return _exclusion_joins(select, context, parsed) if select is not None else []


def _read_join_part(part: exp.Expression, context: _Context, result: ParsedQuery) -> None:
    columns = _match_join_equality(part)
    if columns is None:
        context.warn(f"condizione di join non compresa, ignorata: {_sql(part)}")
        return
    left_column, right_column = columns
    left, right = context.resolve(left_column), context.resolve(right_column)
    if not left.table or not right.table:
        # An unknown table would link unrelated fields that only share a name.
        context.warn(f"condizione di join ignorata perché la tabella di una colonna è sconosciuta: {_sql(part)}")
        return
    result.joins.append(JoinEquality(left, right))


def _match_join_equality(node: exp.Expression) -> Optional[tuple[exp.Column, exp.Column]]:
    """``column = column``, else None."""
    if isinstance(node, exp.EQ):
        left, right = _without_parens(node.this), _without_parens(node.expression)
        if _is_plain_column(left) and _is_plain_column(right):
            return left, right
    return None


def _is_table(node: exp.Expression) -> bool:
    """A table name such as ``[Name]`` or ``ENT.[Name]``, not a subquery or a function."""
    return isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier)


# ---------------------------------------------------------------------------
# WHERE filters
# ---------------------------------------------------------------------------

def _read_filters(condition: exp.Expression, context: _Context, result: ParsedQuery) -> None:
    """Plan: "WHERE split on top-level AND"; parentheses around AND groups are flattened."""
    for part in _and_parts(condition):
        result.filters.extend(_read_filter(part, context))


def _read_filter(part: exp.Expression, context: _Context) -> list[Filter]:
    """One AND part -> its filters. Anything not understood, any OR group included,
    becomes an ``UnsupportedFilter`` with its text."""
    null_check = _match_null_check(part)
    if null_check is not None:
        column, negated = null_check
        return [IsNullFilter(context.resolve(column), negated)]
    equality = _match_equality(part)
    if equality is not None:
        column, value = equality
        return [EqualityFilter(context.resolve(column), value)]
    offset = _match_date_offset(part)
    if offset is not None:
        column, days = offset
        return [DateOffsetFilter(context.resolve(column), days)]
    bound = _match_date_bound(part)
    if bound is not None:
        column, which = bound
        return [DateBoundFilter(context.resolve(column), which)]
    date_range = _match_date_range(part)
    if date_range is not None:
        start, end = date_range
        return [
            DateBoundFilter(context.resolve(start), "start"),
            DateBoundFilter(context.resolve(end), "end"),
        ]
    text = _sql(part)
    context.warn(f"filtro non compreso: {text}")
    return [UnsupportedFilter(text)]


def _match_null_check(node: exp.Expression) -> Optional[tuple[exp.Column, bool]]:
    """Plan: "``col IS [NOT] NULL`` -> ``IsNullFilter``" (negated for IS NOT NULL)."""
    negated = isinstance(node, exp.Not)
    if negated:
        node = _without_parens(node.this)
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        column = _without_parens(node.this)
        if _is_plain_column(column):
            return column, negated
    return None


def _match_equality(node: exp.Expression) -> Optional[tuple[exp.Column, str]]:
    """Plan: "``col = 'lit'`` -> ``EqualityFilter``", the literal on either side.
    A number literal counts too; the value is kept as written, without quotes."""
    if not isinstance(node, exp.EQ):
        return None
    left, right = _without_parens(node.this), _without_parens(node.expression)
    for column, literal in ((left, right), (right, left)):
        value = _literal_text(literal)
        if _is_plain_column(column) and value is not None:
            return column, value
    return None


def _match_date_bound(node: exp.Expression) -> Optional[tuple[exp.Column, str]]:
    """Plan: a comparison between the current date and a column -> ``DateBoundFilter``,
    ``"start"`` when it means now >= column and ``"end"`` when now <= column, whichever side
    the column is on. Strict ``>`` and ``<`` give the same bounds."""
    if isinstance(node, (exp.GTE, exp.GT)):
        larger, smaller = node.this, node.expression
    elif isinstance(node, (exp.LTE, exp.LT)):
        larger, smaller = node.expression, node.this
    else:
        return None
    # The comparison now reads: larger >= smaller.
    if _is_current_date(larger, allow_cast=True):
        column = _date_column(smaller)
        if column is not None:
            return column, "start"   # now >= column
    if _is_current_date(smaller, allow_cast=True):
        column = _date_column(larger)
        if column is not None:
            return column, "end"     # column >= now, that is now <= column
    return None


def _match_date_range(node: exp.Expression) -> Optional[tuple[exp.Column, exp.Column]]:
    """``now BETWEEN a AND b`` means ``now >= a AND now <= b``: a start and an end bound."""
    if not isinstance(node, exp.Between) or not _is_current_date(node.this, allow_cast=True):
        return None
    start = _date_column(node.args.get("low"))
    end = _date_column(node.args.get("high"))
    if start is None or end is None:
        return None
    return start, end


@dataclass(frozen=True)
class _Moment:
    """What a date expression in a WHERE equality stands for: a column (``column`` None: the current date)
    moved by ``seconds``, either with its time of day or, when ``date_only``, as its date part (time 00:00).
    A ``date_only`` moment is always moved by whole days."""

    column: Optional[exp.Column]
    seconds: int
    date_only: bool


def _match_date_offset(node: exp.Expression) -> Optional[tuple[exp.Column, int]]:
    """Spec, step 3: "A filter comparing the date part of a column with the current date moved by a number of
    days (``CAST(CM.CreatedDate AS DATE) = CAST(DATEADD(day, -1, GETDATE()) AS DATE)``) is understood [...] The
    spelling with the current date cast first (``DATEADD(day, -2, CAST(GETDATE() AS DATE))``) is the same filter."

    Returns the column and the number of days when ``node`` is an equality, either way round, between the date
    part of a column and the date part of the current date moved by whole days, else None. The meaning decides,
    not the spelling (see ``_moment``): ``CAST(GETDATE() AS DATE)`` is 0 days, a week is 7 days, 48 hours taken
    before the date part are 2 days, and a column moved by days is moved back onto the other side. Both sides
    must be date parts: T-SQL compares ``CM.CreatedDate = CAST(GETDATE() AS DATE)`` with the full date and time
    (true only at midnight), so it stays unsupported. Only ``=``: an inequality against a moved date is not
    understood, and a comparison with the current date unmoved is a ``DateBoundFilter``."""
    if not isinstance(node, exp.EQ):
        return None
    try:
        left, right = _moment(node.this), _moment(node.expression)
    except RecursionError:  # an absurdly deep expression (thousands of nested terms) is not understood
        return None
    if left is None or right is None:
        return None
    for column_side, now_side in ((left, right), (right, left)):
        if column_side.column is None or now_side.column is not None:
            continue
        if not (column_side.date_only and now_side.date_only):
            return None
        seconds = now_side.seconds - column_side.seconds
        if seconds % _SECONDS_PER_DAY:
            return None
        days = seconds // _SECONDS_PER_DAY
        if abs(days) > _MAX_DAY_OFFSET:
            return None
        return column_side.column, days
    return None


def _moment(node: Optional[exp.Expression]) -> Optional[_Moment]:
    """Read a column or the current date under casts and ``DATEADD``; None for anything else.

    - ``GETDATE()`` and the other current-date calls, or a plain column: the value with its time.
    - ``CAST(x AS DATE)`` / ``CONVERT(DATE, x)``: the date part of ``x``, when ``x`` is moved by whole days
      (the date part of a value moved by 12 hours depends on the time of day: not understood).
    - A cast to DATETIME or DATETIME2 keeps the value; SMALLDATETIME rounds the time to the minute, so it is
      understood only on a date part.
    - ``DATEADD(unit, n, x)`` with ``n`` a whole number (``_whole_number``): ``x`` moved by ``n`` units, for
      the units of ``_DAY_UNITS``, and of ``_TIME_UNITS`` when ``x`` keeps its time.
    - Parentheses and ``AT TIME ZONE`` are passed through (time zones are not kept, as for the date bounds).
    """
    while isinstance(node, (exp.Paren, exp.AtTimeZone)):
        node = node.this
    if _is_current_date_call(node):
        return _Moment(None, 0, False)
    if _is_plain_column(node):
        return _Moment(node, 0, False)
    cast = _cast_parts(node)
    if cast is not None:
        to_type, operand = cast
        inner = _moment(operand)
        if inner is None:
            return None
        if to_type == exp.DataType.Type.DATE:
            if inner.date_only:
                return inner
            if inner.seconds % _SECONDS_PER_DAY:
                return None
            return _Moment(inner.column, inner.seconds, True)
        if to_type in (exp.DataType.Type.DATETIME, exp.DataType.Type.DATETIME2):
            return inner
        if to_type == exp.DataType.Type.SMALLDATETIME and inner.date_only:
            return inner
        return None
    if isinstance(node, exp.DateAdd):
        amount = _whole_number(node.expression)
        inner = _moment(node.this)
        if amount is None or inner is None or not _INT_RANGE[0] <= amount <= _INT_RANGE[1]:
            return None
        unit = node.text("unit").upper()
        if unit in _DAY_UNITS:
            delta = amount * _DAY_UNITS[unit] * _SECONDS_PER_DAY
        elif unit in _TIME_UNITS and not inner.date_only:
            delta = amount * _TIME_UNITS[unit]
        else:
            return None
        return _Moment(inner.column, inner.seconds + delta, inner.date_only)
    return None


def _cast_parts(node: Optional[exp.Expression]) -> Optional[tuple[exp.DataType.Type, exp.Expression]]:
    """``CAST(x AS type)`` (TRY_CAST too) or ``CONVERT(type, x[, style])`` -> (type, x), else None."""
    if isinstance(node, exp.Cast) and node.to is not None:
        return node.to.this, node.this
    if isinstance(node, exp.Convert) and isinstance(node.this, exp.DataType):
        return node.this.this, node.expression
    return None


# A whole number as written in SQL: digits only (1.5 and 1e3 are not whole numbers here).
_DIGITS = re.compile(r"[0-9]+")


def _whole_number(node: Optional[exp.Expression]) -> Optional[int]:
    """The value of a whole-number constant: a number literal, with parentheses, signs and ``+``, ``-``, ``*``
    between such constants (``-1``, ``(-1) * 7``). None for anything else, a column or a decimal included."""
    node = _without_parens(node)
    if isinstance(node, exp.Literal) and not node.is_string:
        return int(node.this) if _DIGITS.fullmatch(node.this) else None
    if isinstance(node, exp.Neg):
        value = _whole_number(node.this)
        return None if value is None else -value
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
        left, right = _whole_number(node.this), _whole_number(node.expression)
        if left is None or right is None:
            return None
        if isinstance(node, exp.Add):
            return left + right
        if isinstance(node, exp.Sub):
            return left - right
        return left * right
    return None


def _and_parts(condition: exp.Expression) -> list[exp.Expression]:
    """The parts of a condition chained with AND, in order, with no parentheses around them.

    A loop rather than recursion: the parser nests a long AND chain one level per AND.
    """
    parts: list[exp.Expression] = []
    pending = [condition]
    while pending:
        node = _without_parens(pending.pop())
        if isinstance(node, exp.And):
            pending.append(node.expression)  # read after everything on the left
            pending.append(node.this)
        else:
            parts.append(node)
    return parts


# ---------------------------------------------------------------------------
# Select list
# ---------------------------------------------------------------------------

def _read_select_list(select: exp.Select, context: _Context, result: ParsedQuery) -> None:
    """Plan: "``target_column`` = alias, else column name. ``SELECT *`` -> ``select_all=True``"."""
    if not select.expressions:
        context.warn("la SELECT non elenca alcuna colonna")
    for item in select.expressions:
        if isinstance(item, exp.Star):
            result.select_all = True
        elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
            result.select_all = True
            context.warn(f"{_sql(item)} è letto come SELECT * (la limitazione alla tabella non viene mantenuta)")
        else:
            target, node = _target_and_expression(item)
            if not target:
                context.warn(f"l'elemento della SELECT {_sql(item)} non ha un nome di colonna")
            result.select.append(SelectItem(target, _select_expr(node, target, context)))
    _warn_swapped_columns(result.select, context)


def _warn_swapped_columns(items: list[SelectItem], context: _Context) -> None:
    """Two plain columns of one table copied into each other's names (``M.Phone AS MobilePhone,
    M.MobilePhone AS Phone``) are read as written, but the pattern looks like a slip: one warning per pair,
    naming the first item's column (X) and target (Y). Names are compared ignoring letter case."""
    copies: dict[tuple[str, str, str], tuple[str, str]] = {}
    for item in items:
        if not isinstance(item.expr, SelectColumn) or not item.expr.source.table or not item.target_column:
            continue
        table, column, target = item.expr.source.table, item.expr.source.column, item.target_column
        if column.lower() == target.lower():
            continue
        mirror = copies.get((table, target.lower(), column.lower()))
        if mirror is not None:
            first_column, first_target = mirror
            context.warn(
                f"la query copia {first_column} in {first_target} e {first_target} in {first_column}: "
                f"verifica che lo scambio sia voluto"
            )
        copies.setdefault((table, column.lower(), target.lower()), (column, target))


def _target_and_expression(item: exp.Expression) -> tuple[str, exp.Expression]:
    """The target column name (alias, else the column's own name, else "") and the expression."""
    if isinstance(item, exp.Alias):
        return item.alias, item.this
    if _is_plain_column(item):
        return item.name, item
    return "", item


def _select_expr(node: exp.Expression, target: str, context: _Context) -> SelectExpr:
    """Plan: column -> ``SelectColumn``; string/number literal -> ``SelectLiteral``;
    ``FORMAT(<current date>, 'fmt')`` -> ``SelectCurrentDate(fmt)``; a bare current-date call,
    also wrapped in ``AT TIME ZONE`` -> ``SelectCurrentDate(None)``; ``CONCAT(...)`` ->
    ``SelectConcat``; anything else -> ``SelectUnsupported(text)``."""
    node = _without_parens(node)
    if _is_plain_column(node):
        return SelectColumn(context.resolve(node))
    if isinstance(node, exp.Null):
        return SelectNull()
    value = _literal_text(node)
    if value is not None:
        return SelectLiteral(value)
    if _is_current_date(node, allow_cast=False):
        return SelectCurrentDate(None)
    date_format = _current_date_format(node)
    if date_format is not None:
        bad = unsupported_dotnet_tokens(date_format)
        if bad:
            text = _sql(node)
            context.warn(f"colonna {target}: il formato data {date_format!r} usa token che il PoC non sa scrivere "
                         f"({', '.join(bad)}); la colonna riceve un placeholder")
            return SelectUnsupported(text)
        return SelectCurrentDate(date_format)
    if isinstance(node, exp.Concat):
        return SelectConcat(tuple(_select_expr(part, target, context) for part in node.expressions))
    text = _sql(node)
    label = f"colonna {target}" if target else "un elemento della SELECT"
    context.warn(f"{label}: espressione non compresa: {text}")
    return SelectUnsupported(text)


def _current_date_format(node: exp.Expression) -> Optional[str]:
    """The .NET format of ``FORMAT(<current date>, '<format>')`` as written, else None.
    The current date may carry ``AT TIME ZONE``; a third (culture) argument is not supported."""
    if not (isinstance(node, exp.Anonymous) and node.name.upper() == "FORMAT"):
        return None
    if len(node.expressions) != 2:
        return None
    value, date_format = node.expressions
    if not _is_current_date(value, allow_cast=False):
        return None
    return _string_text(date_format)


# ---------------------------------------------------------------------------
# Small helpers on sqlglot nodes
# ---------------------------------------------------------------------------

def _sql(node: exp.Expression) -> str:
    """The SQL text of a node for warnings and unsupported records, comments left out."""
    return node.sql(dialect=_TSQLKeepingFormat, comments=False)


def _without_parens(node: Optional[exp.Expression]) -> Optional[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _is_plain_column(node: Optional[exp.Expression]) -> bool:
    """A column reference such as ``C.active`` or ``[Mailing CG]``, not ``T.*``."""
    return isinstance(node, exp.Column) and isinstance(node.this, exp.Identifier)


def _string_text(node: Optional[exp.Expression]) -> Optional[str]:
    """The text of a string literal ('abc' or N'abc'), without quotes; None otherwise."""
    if isinstance(node, exp.Literal) and node.is_string:
        return node.this
    if isinstance(node, exp.National):
        return node.this
    return None


def _literal_text(node: Optional[exp.Expression]) -> Optional[str]:
    """The text of a string or number literal as written, without quotes; None otherwise."""
    text = _string_text(node)
    if text is not None:
        return text
    if isinstance(node, exp.Literal):
        return node.this
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        return "-" + node.this.this
    return None


def _is_current_date(node: Optional[exp.Expression], allow_cast: bool) -> bool:
    """GETDATE(), GETUTCDATE() or SYSDATETIME(), possibly inside parentheses and
    ``AT TIME ZONE``, and inside ``CAST(... AS DATE)`` when ``allow_cast`` is True."""
    return _is_current_date_call(_strip_date_wrappers(node, allow_cast))


def _is_current_date_call(node: Optional[exp.Expression]) -> bool:
    # sqlglot reads GETDATE(), SYSDATETIME() and CURRENT_TIMESTAMP as CurrentTimestamp,
    # and keeps GETUTCDATE() as a plain call.
    if isinstance(node, exp.CurrentTimestamp):
        return True
    return (
        isinstance(node, exp.Anonymous)
        and node.name.upper() in CURRENT_DATE_FUNCTIONS
        and not node.expressions
    )


def _date_column(node: Optional[exp.Expression]) -> Optional[exp.Column]:
    """A column, possibly inside parentheses, ``CAST(... AS DATE)`` or ``AT TIME ZONE``."""
    node = _strip_date_wrappers(node, allow_cast=True)
    return node if _is_plain_column(node) else None


def _strip_date_wrappers(node: Optional[exp.Expression], allow_cast: bool) -> Optional[exp.Expression]:
    """Remove parentheses, ``AT TIME ZONE`` and, when allowed, casts to a date type."""
    while True:
        if isinstance(node, (exp.Paren, exp.AtTimeZone)):
            node = node.this
        elif allow_cast and _is_date_cast(node):
            node = node.this if isinstance(node, exp.Cast) else node.expression
        else:
            return node


def _is_date_cast(node: Optional[exp.Expression]) -> bool:
    """``CAST(x AS DATE)`` or ``CONVERT(DATE, x)``, also with DATETIME, DATETIME2, SMALLDATETIME."""
    if isinstance(node, exp.Cast):  # TRY_CAST too
        return node.to is not None and node.to.this in _DATE_TYPES
    if isinstance(node, exp.Convert):
        return isinstance(node.this, exp.DataType) and node.this.this in _DATE_TYPES
    return False
