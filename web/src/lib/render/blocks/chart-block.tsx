import { ChevronRight } from 'lucide-react'
import { useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import type { ChartBlock as ChartBlockData } from '@/lib/api'
import { cn } from 'cn'

import { BlockFrame } from './block-frame'
import { formatTime, formatValue, unitLabel } from './format'

// Groups with fewer spans than this are drawn lighter and labelled as small samples
// (same threshold the backend uses for its note).
const SPARSE = 30
const COLORS = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)']
const SPARSE_SUFFIX = '__sparse'

// Recharts colours legend text like its series; a light small-sample series would then be
// unreadable, so legend text uses the normal muted foreground.
function legendText(value: string) {
  return <span style={{ color: 'var(--muted-foreground)' }}>{value}</span>
}

function truncate(text: string, max = 26) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function describe(block: ChartBlockData): string {
  const s = block.series[0]!
  const values = block.points
    .map((p) => ({ x: p.x, v: p.values[s.key] }))
    .filter((p): p is { x: string; v: number } => typeof p.v === 'number')
  if (values.length === 0) return block.title
  const top = values.reduce((a, b) => (b.v > a.v ? b : a))
  if (block.kind === 'bar') {
    return `Bar chart, ${block.title}: ${values.length} ${block.x_label.toLowerCase()} values; highest ${top.x} at ${formatValue(top.v, s.unit)}.`
  }
  const low = values.reduce((a, b) => (b.v < a.v ? b : a))
  return `Line chart, ${block.title}: ${values.length} recorded points from ${formatTime(values[0]!.x)} to ${formatTime(values.at(-1)!.x)} UTC; range ${formatValue(low.v, s.unit)} to ${formatValue(top.v, s.unit)}, peak at ${formatTime(top.x)} UTC.`
}

function DataTable({ block }: { block: ChartBlockData }) {
  const showSamples = block.points.some((p) => p.sample_count !== null)
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <caption className="sr-only">Data for {block.title}</caption>
        <thead>
          <tr className="border-b text-muted-foreground">
            <th scope="col" className="px-2 py-1 text-left font-medium">
              {block.kind === 'line' ? 'Time (UTC)' : block.x_label}
            </th>
            {block.series.map((s) => (
              <th key={s.key} scope="col" className="px-2 py-1 text-right font-medium">
                {s.label}
                {unitLabel(s.unit) && ` (${unitLabel(s.unit)})`}
              </th>
            ))}
            {showSamples && (
              <th scope="col" className="px-2 py-1 text-right font-medium">
                Samples
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {block.points.map((p) => (
            <tr key={p.x} className="border-b last:border-b-0">
              <th scope="row" className="px-2 py-1 text-left font-normal">
                {block.kind === 'line' ? formatTime(p.x) : p.x}
              </th>
              {block.series.map((s) => (
                <td key={s.key} className="px-2 py-1 text-right tabular-nums">
                  {formatValue(p.values[s.key] ?? null)}
                </td>
              ))}
              {showSamples && (
                <td className="px-2 py-1 text-right tabular-nums">{p.sample_count ?? '—'}</td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function BarView({ block }: { block: ChartBlockData }) {
  const s = block.series[0]!
  const hasSparse = block.points.some((p) => p.sample_count !== null && p.sample_count < SPARSE)
  // Small-sample bars go into a second, lighter series stacked on the same axis, so each
  // category still has exactly one bar.
  const data = block.points.map((p) => {
    const sparse = p.sample_count !== null && p.sample_count < SPARSE
    return {
      x: p.x,
      n: p.sample_count,
      [sparse ? s.key + SPARSE_SUFFIX : s.key]: p.values[s.key] ?? null,
    }
  })
  const height = Math.max(160, block.points.length * 34 + 48)
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ left: 4, right: 16, top: 4, bottom: 4 }}>
          <CartesianGrid horizontal={false} strokeDasharray="3 3" className="stroke-border" />
          <XAxis
            type="number"
            tickFormatter={(v: number) => formatValue(v, s.unit)}
            tick={{ fontSize: 11, fill: 'var(--muted-foreground)' }}
          />
          <YAxis
            type="category"
            dataKey="x"
            width={150}
            tickFormatter={(v: string) => truncate(v)}
            tick={{ fontSize: 11, fill: 'var(--muted-foreground)' }}
          />
          <Tooltip
            cursor={{ fill: 'var(--accent)', opacity: 0.4 }}
            contentStyle={{
              background: 'var(--popover)',
              border: '1px solid var(--border)',
              fontSize: 12,
            }}
            formatter={(value, _name, item) => [
              `${formatValue(Number(value), s.unit)}${item.payload.n !== null ? ` (n = ${item.payload.n})` : ''}`,
              s.label,
            ]}
          />
          {hasSparse && <Legend wrapperStyle={{ fontSize: 11 }} formatter={legendText} />}
          <Bar
            dataKey={s.key}
            name={s.label}
            stackId="v"
            fill={COLORS[0]}
            isAnimationActive={false}
          />
          {hasSparse && (
            <Bar
              dataKey={s.key + SPARSE_SUFFIX}
              name={`${s.label} (n < ${SPARSE}, small sample)`}
              stackId="v"
              // A real lighter colour (not opacity) so the legend swatch matches the bars.
              fill="color-mix(in oklab, var(--chart-1) 35%, var(--card))"
              isAnimationActive={false}
            />
          )}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function LineView({ block }: { block: ChartBlockData }) {
  const unit = block.series[0]!.unit
  // The x axis spans the whole observation window, so missing coverage at either end is
  // visible; points are only those recorded, and gaps stay gaps (connectNulls off).
  const domain = [Date.parse(block.window.start), Date.parse(block.window.end)]
  const data = block.points.map((p) => ({ t: Date.parse(p.x), ...p.values }))
  const dots = block.points.length <= 40
  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ left: 4, right: 16, top: 8, bottom: 4 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={domain}
            tickFormatter={(v: number) => formatTime(v)}
            tick={{ fontSize: 11, fill: 'var(--muted-foreground)' }}
          />
          <YAxis
            width={64}
            tickFormatter={(v: number) => formatValue(v, unit)}
            tick={{ fontSize: 11, fill: 'var(--muted-foreground)' }}
          />
          <Tooltip
            labelFormatter={(v) => `${formatTime(Number(v))} UTC`}
            contentStyle={{
              background: 'var(--popover)',
              border: '1px solid var(--border)',
              fontSize: 12,
            }}
            formatter={(value, name) => {
              const series = block.series.find((s) => s.label === name)
              return [formatValue(Number(value), series?.unit ?? unit), name]
            }}
          />
          {block.series.length > 1 && (
            <Legend wrapperStyle={{ fontSize: 11 }} formatter={legendText} />
          )}
          {block.series.map((s, i) => (
            <Line
              key={s.key}
              type="linear"
              dataKey={s.key}
              name={s.label}
              stroke={COLORS[i % COLORS.length]}
              strokeWidth={2}
              dot={dots ? { r: 2 } : false}
              connectNulls={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export default function ChartBlockView({
  block,
  onCite,
}: {
  block: ChartBlockData
  onCite?: (ref: string) => void
}) {
  const [showData, setShowData] = useState(false)
  return (
    <BlockFrame
      title={block.title}
      window={block.window}
      evidenceRefs={block.evidence_refs}
      note={block.note}
      onCite={onCite}
    >
      <div role="img" aria-label={describe(block)}>
        {block.kind === 'bar' ? <BarView block={block} /> : <LineView block={block} />}
      </div>
      <Collapsible open={showData} onOpenChange={setShowData} className="mt-2">
        <CollapsibleTrigger className="inline-flex items-center gap-1 rounded text-xs text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
          <ChevronRight
            aria-hidden
            className={cn('size-3 transition-transform', showData && 'rotate-90')}
          />
          {showData ? 'Hide data' : 'Show data'}
        </CollapsibleTrigger>
        <CollapsibleContent className="mt-2">
          <DataTable block={block} />
        </CollapsibleContent>
      </Collapsible>
    </BlockFrame>
  )
}
