# SPDX-FileCopyrightText: The Docling Contributors
# SPDX-License-Identifier: MIT

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from docling_core.types.doc.document import (
    DoclingDocument,
    RichTableCell,
    TableCell,
    TableData,
    TableItem,
)

from docling.backend.latex.constants import (
    MACROS_ESCAPED,
    TABLE_MACROS_IGNORE,
    TABLE_MACROS_RULE,
    TABLE_MACROS_SPAN,
)
from docling.backend.utils.table_spans import (
    MAX_COLSPAN,
    MAX_ROWSPAN,
    clamp_span,
    table_width,
)

try:  # pragma: no cover - import-time guard
    from pylatexenc.latexwalker import (
        LatexCharsNode,
        LatexEnvironmentNode,
        LatexGroupNode,
        LatexMacroNode,
        LatexSpecialsNode,
    )
except ImportError:
    pass  # guarded by LatexDocumentBackend.__init__


@dataclass
class ParsedTable:
    data: TableData
    rich_cells: list[tuple[int, list[Any]]]


class TableHelperMixin:
    if TYPE_CHECKING:

        def _nodes_to_text(self, nodes: Any) -> str: ...
        def _contains_math(self, nodes: list) -> bool: ...
        def _process_nodes(
            self, nodes: Any, doc: DoclingDocument, parent: Any
        ) -> None: ...

    def _process_table_macro_node(
        self,
        n: LatexMacroNode,
        current_cell_nodes: list[Any],
        set_span_fn: Callable[..., None],
        finish_cell_fn: Callable[..., None],
        finish_row_fn: Callable[[], None],
    ):
        if n.macroname == "\\":  # Row break
            finish_row_fn()

        elif n.macroname in TABLE_MACROS_SPAN:
            # The context gives both macros their arguments: {cols}{spec}{text}
            # and [vpos]{rows}[bigstruts]{width}[vmove]{text}. The span applies
            # to the cell that the next & or \\ ends.
            args = n.nodeargd.argnlist if n.nodeargd else []
            if len(args) < 3 or args[-1] is None:
                current_cell_nodes.append(n)
                return
            span_arg = args[0] if n.macroname == "multicolumn" else args[1]
            try:
                span = int(self._nodes_to_text([span_arg]).strip())
            except (ValueError, TypeError):
                span = 1
            if n.macroname == "multicolumn":
                set_span_fn(col_span=clamp_span(span, MAX_COLSPAN))
            else:
                set_span_fn(row_span=clamp_span(span, MAX_ROWSPAN))
            content = args[-1]
            content_nodes = (
                content.nodelist if isinstance(content, LatexGroupNode) else [content]
            )
            for child in content_nodes or []:
                if (
                    isinstance(child, LatexMacroNode)
                    and child.macroname in TABLE_MACROS_SPAN
                ):
                    # e.g. \multicolumn{2}{c}{\multirow{2}{*}{Text}}
                    self._process_table_macro_node(
                        child,
                        current_cell_nodes,
                        set_span_fn,
                        finish_cell_fn,
                        finish_row_fn,
                    )
                else:
                    current_cell_nodes.append(child)

        elif n.macroname in TABLE_MACROS_RULE:
            pass
        elif n.macroname == "caption":
            pass
        elif n.macroname in TABLE_MACROS_IGNORE:
            pass
        elif n.macroname == "&":  # Cell break
            finish_cell_fn()
        elif n.macroname in MACROS_ESCAPED:
            current_cell_nodes.append(n)
        else:
            current_cell_nodes.append(n)

    def _parse_table(self, node: LatexEnvironmentNode) -> ParsedTable | None:
        # None marks a column that a \multicolumn cell before it covers.
        rows: list[list[tuple[TableCell, int, int, list[Any] | None] | None]] = []
        current_row: list[tuple[TableCell, int, int, list[Any] | None] | None] = []
        current_cell_nodes: list[Any] = []

        # The span that a \multicolumn or \multirow sets for the current cell.
        pending_span = {"col_span": 1, "row_span": 1}

        def set_span(col_span: int | None = None, row_span: int | None = None):
            if col_span is not None:
                pending_span["col_span"] = col_span
            if row_span is not None:
                pending_span["row_span"] = row_span

        def finish_cell():
            col_span = pending_span["col_span"]
            row_span = pending_span["row_span"]
            pending_span["col_span"] = pending_span["row_span"] = 1
            rich_nodes = (
                current_cell_nodes.copy()
                if self._contains_math(current_cell_nodes)
                else None
            )
            text = (
                ""
                if rich_nodes is not None
                else self._nodes_to_text(current_cell_nodes).strip()
            )
            cell = TableCell(
                text=text,
                start_row_offset_idx=0,
                end_row_offset_idx=0,
                start_col_offset_idx=0,
                end_col_offset_idx=0,
            )
            current_row.append((cell, col_span, row_span, rich_nodes))
            current_cell_nodes.clear()

            for _ in range(col_span - 1):
                current_row.append(None)

        def finish_row():
            # What follows the last \\ of a table is only whitespace and rules,
            # which must not become a row of one empty cell.
            spans_cell = pending_span != {"col_span": 1, "row_span": 1}
            if spans_cell or (
                current_cell_nodes
                and (
                    current_row
                    or self._contains_math(current_cell_nodes)
                    or self._nodes_to_text(current_cell_nodes).strip()
                )
            ):
                finish_cell()
            current_cell_nodes.clear()
            if current_row:
                rows.append(current_row[:])
            current_row.clear()

        if node.nodelist is None:
            return None

        for n in node.nodelist:
            if isinstance(n, LatexMacroNode):
                self._process_table_macro_node(
                    n,
                    current_cell_nodes,
                    set_span,
                    finish_cell,
                    finish_row,
                )
            elif isinstance(n, LatexCharsNode):
                text = n.chars
                if "&" in text:
                    parts = text.split("&")
                    for i, part in enumerate(parts):
                        if part:
                            current_cell_nodes.append(LatexCharsNode(chars=part))
                        if i < len(parts) - 1:
                            finish_cell()
                else:
                    current_cell_nodes.append(n)
            else:
                if isinstance(n, LatexSpecialsNode) and n.specials_chars == "&":
                    finish_cell()
                else:
                    current_cell_nodes.append(n)

        finish_row()

        if not rows:
            return None

        num_rows = len(rows)
        # Columns to the right of the last cell that starts in them hold no
        # cell of their own, so a span wider than the table does not widen it.
        num_cols = min(
            max(len(row) for row in rows),
            table_width(
                [
                    [(entry[1], entry[2]) for entry in row if entry is not None]
                    for row in rows
                ]
            ),
        )

        flat_cells = []
        rich_cells: list[tuple[int, list[Any]]] = []
        for i, row in enumerate(rows):
            for j in range(num_cols):
                if j < len(row):
                    entry = row[j]
                    if entry is None:
                        continue
                    cell, col_span, row_span, rich_nodes = entry
                else:
                    cell = TableCell(
                        text="",
                        start_row_offset_idx=0,
                        end_row_offset_idx=0,
                        start_col_offset_idx=0,
                        end_col_offset_idx=0,
                    )
                    col_span, row_span = 1, 1
                    rich_nodes = None

                cell.start_row_offset_idx = i
                cell.start_col_offset_idx = j

                cell.end_row_offset_idx = min(i + row_span, num_rows)
                cell.end_col_offset_idx = min(j + col_span, num_cols)

                if rich_nodes is not None:
                    rich_cells.append((len(flat_cells), rich_nodes))
                flat_cells.append(cell)

        return ParsedTable(
            data=TableData(
                num_rows=num_rows, num_cols=num_cols, table_cells=flat_cells
            ),
            rich_cells=rich_cells,
        )

    def _add_rich_table_cells(
        self, parsed: ParsedTable, doc: DoclingDocument, table: TableItem
    ) -> None:
        for index, nodes in parsed.rich_cells:
            cell_group = doc.add_group(parent=table)
            self._process_nodes(nodes, doc, parent=cell_group)
            cell = table.data.table_cells[index]
            table.data.table_cells[index] = RichTableCell(
                **cell.model_dump(exclude={"text"}),
                text="",
                ref=cell_group.get_ref(),
            )
