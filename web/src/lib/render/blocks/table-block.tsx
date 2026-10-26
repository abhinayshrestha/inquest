import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from '@tanstack/react-table'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import { useMemo, useState } from 'react'

import type { TableBlock as TableBlockData } from '@/lib/api'
import { cn } from 'cn'

import { BlockFrame } from './block-frame'
import { formatValue, unitLabel } from './format'

type Row = TableBlockData['rows'][number]
const helper = createColumnHelper<Row>()

// Sortable, read-only table of evidence values. Cells are text or numbers from the block;
// nothing is rendered as markup.
export function TableBlockView({
  block,
  onCite,
}: {
  block: TableBlockData
  onCite?: (ref: string) => void
}) {
  const [sorting, setSorting] = useState<SortingState>([])
  const columns = useMemo(
    () =>
      block.columns.map((c) =>
        helper.accessor((row) => row[c.key] ?? null, {
          id: c.key,
          header: () => {
            const u = unitLabel(c.unit)
            return u && c.kind === 'number' ? `${c.label} (${u})` : c.label
          },
          cell: (info) => {
            const value = info.getValue()
            return typeof value === 'number' ? formatValue(value) : (value ?? '—')
          },
          sortingFn: c.kind === 'number' ? 'basic' : 'alphanumeric',
          sortUndefined: 'last',
          meta: { numeric: c.kind === 'number' },
        }),
      ),
    [block.columns],
  )
  // TanStack Table v8 is not React Compiler-safe; this app does not use the compiler.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data: block.rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  return (
    <BlockFrame
      title={block.title}
      window={block.window}
      evidenceRefs={block.evidence_refs}
      note={block.note}
      onCite={onCite}
    >
      <div className="-mx-4 overflow-x-auto px-4">
        <table className="w-full min-w-max border-collapse text-sm">
          <thead>
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id} className="border-b">
                {group.headers.map((header) => {
                  const numeric = (header.column.columnDef.meta as { numeric?: boolean })?.numeric
                  const sorted = header.column.getIsSorted()
                  const Icon =
                    sorted === 'asc' ? ArrowUp : sorted === 'desc' ? ArrowDown : ArrowUpDown
                  return (
                    <th
                      key={header.id}
                      scope="col"
                      aria-sort={
                        sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : 'none'
                      }
                      className={cn(
                        'px-2 py-1.5 font-medium text-muted-foreground',
                        numeric ? 'text-right' : 'text-left',
                      )}
                    >
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className={cn(
                          'inline-flex items-center gap-1 rounded hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                          numeric && 'flex-row-reverse',
                        )}
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        <Icon aria-hidden className="size-3 opacity-60" />
                      </button>
                    </th>
                  )
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id} className="border-b last:border-b-0">
                {row.getVisibleCells().map((cell) => {
                  const numeric = (cell.column.columnDef.meta as { numeric?: boolean })?.numeric
                  return (
                    <td
                      key={cell.id}
                      className={cn(
                        'px-2 py-1.5',
                        numeric ? 'text-right tabular-nums' : 'max-w-xs truncate',
                      )}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </BlockFrame>
  )
}
