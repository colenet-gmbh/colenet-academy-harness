// The Zentrale pane, opened by the /board pane's Zentrale button: what lies with the owner, what is
// red, who waits on what, who works on what, CI and the accounts; Swimlanes draws who waited on whom
// over the last 8 h or 24 h. It renders what `scripts/command_center/zentrale.py --json` and
// `scripts/flow_intervals.py --json` print (those scripts own every rule) and reads nothing else.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ZentraleDoc, ZentraleView } from '../../types'
import { clip } from './board'
import { LEGEND, lanes, sectionLines } from './zentrale'
import type { Interval } from './zentrale'

type On = Parameters<Register>[0]
type $ = EngineInterface

const PANE = 'we-zentrale'
const REFRESH_MS = 30_000
// A document older than two refreshes shows its age in warning colour: the pane does not pass off old data as now.
const STALE_MS = 2 * REFRESH_MS
// The fast read (board, logs, sessions) draws first; the full read (GitHub, main command up to 30 s,
// cswap up to 10 s) follows in the background and stays below the refresh interval, so two never overlap.
const FAST_TIMEOUT_MS = 15_000
const FULL_TIMEOUT_MS = 28_000

const view = atom({ plugin: 'we', key: 'zentraleView' } as const, null, { shape: 'zentrale-view-1' })
const isOpen = atom({ plugin: 'we', key: 'zentraleOpen' } as const, false)

// The engine reports no closed pane, and a draw may not write state. A drawn pane redraws on every
// read the refresh stores, so a read with no draw after it means the pane is closed: the refresh
// pauses until the pane is drawn again. One flag per read keeps a second read of a kind from starting.
export const zentrale = { fastLoading: false, fullLoading: false, lanesLoading: false, drawnAt: Number.NEGATIVE_INFINITY, loadedAt: Number.NEGATIVE_INFINITY }

const empty = (): ZentraleView => ({ at: 0, face: 'sections', hours: 8 })

async function readDoc($: $, board: string | undefined, fast: boolean) {
  const flag = fast ? 'fastLoading' : 'fullLoading'
  if (zentrale[flag]) return
  zentrale[flag] = true
  const at = await $.clock.now()
  zentrale.loadedAt = at
  try {
    const argv = ['python3', '-I', `${$.plugin.root}/scripts/command_center/zentrale.py`, '--json', ...(fast ? ['--fast'] : []), ...(board ? ['--board', board] : [])]
    const run = await $.process.run(argv, { timeoutMs: fast ? FAST_TIMEOUT_MS : FULL_TIMEOUT_MS })
    const doc = JSON.parse(run.stdout || '{}') as ZentraleDoc
    await update($, view, v => {
      const base = v ?? empty()
      // a fast read never replaces a full one that already arrived
      if (fast && base.doc && !base.doc.fast && base.at > at) return base
      return run.exitCode !== 0 || !doc.sections
        ? { ...base, ...(board ? { board } : {}), error: `zentrale.py exit ${run.exitCode}` }
        : { ...base, ...(board ? { board } : {}), doc, at, error: undefined }
    })
  } catch (err) {
    const error = err instanceof Error ? err.message : String(err)
    await update($, view, v => ({ ...(v ?? empty()), ...(board ? { board } : {}), error }))
  } finally {
    zentrale[flag] = false
  }
}

async function readLanes($: $, board: string | undefined) {
  if (zentrale.lanesLoading) return
  zentrale.lanesLoading = true
  const at = await $.clock.now()
  try {
    const argv = ['python3', '-I', `${$.plugin.root}/scripts/flow_intervals.py`, '--json', '--since', '1d', '--no-ci', ...(board ? ['--board', board] : [])]
    const run = await $.process.run(argv, { timeoutMs: FULL_TIMEOUT_MS })
    const doc = JSON.parse(run.stdout || '{}') as { intervals?: Interval[] }
    const next = { intervals: doc.intervals ?? [], at, ...(run.exitCode !== 0 ? { error: `flow_intervals.py exit ${run.exitCode}` } : {}) }
    await update($, view, v => ({ ...(v ?? empty()), lanes: next }))
  } catch (err) {
    const error = err instanceof Error ? err.message : String(err)
    await update($, view, v => ({ ...(v ?? empty()), lanes: { intervals: v?.lanes?.intervals ?? [], at, error } }))
  } finally {
    zentrale.lanesLoading = false
  }
}

// The fast read first, awaited, so the first draw has the board; the full read runs on unawaited.
async function load($: $, board: string | undefined) {
  await readDoc($, board, true)
  void readDoc($, board, false)
}

// The Zentrale button's work (a plugin's own $.command.run does not reach its own hook); `board` is the
// board file the /board pane shows.
export async function openZentrale($: $, board?: string): Promise<string> {
  await update($, isOpen, () => true)
  const opened = await $.ui.open({ id: PANE, title: 'Zentrale' })
  await load($, board)
  return opened.isPlaced ? 'Zentrale opened.' : `Zentrale waits undrawn: ${opened.reason}`
}

// From the module's one session.start hook: the refresh while the pane is open (the full read; the
// swimlanes' intervals only while they show).
export async function startZentrale($: $) {
  $.clock.every(REFRESH_MS, () => {
    void read($, isOpen).then(async open => {
      if (!open || zentrale.drawnAt < zentrale.loadedAt) return
      const v = await read($, view)
      await readDoc($, v?.board, false)
      if (v?.face === 'lanes') await readLanes($, v.board)
    })
  })
}

const face = async ($: $, next: Partial<Pick<ZentraleView, 'face' | 'hours'>>) => {
  await update($, view, v => ({ ...(v ?? empty()), ...next }))
  const v = await read($, view)
  if (v?.face === 'lanes' && !v.lanes) await readLanes($, v.board)
}

export const registerZentrale = (on: On) => {
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const v = await read($, view)
    const width = Math.max(30, e.props.bodyColumns ?? e.viewport?.columns ?? 80)
    const now = await $.clock.now()
    zentrale.drawnAt = now
    if (!v?.doc && !v?.error) return <Text dimColor>Reading the Zentrale…</Text>
    const doc = v.doc
    const stale = now - v.at >= STALE_MS
    const stamp = doc ? `as of ${doc.built_at.slice(11, 16)} UTC${doc.fast ? ' (loading)' : ''}` : 'not read yet'
    const buttons = (
      <Box flexDirection="row" gap={2} marginTop={1}>
        <Button key="refresh" hotkey="r" label="Refresh" onPress={() => void load($, v.board)} />
        {v.face === 'lanes' ? (
          <>
            <Button key="sections" label="Sections" onPress={() => void face($, { face: 'sections' })} />
            <Button key="h8" hotkey="1" label="8 h" onPress={() => void face($, { hours: 8 })} />
            <Button key="h24" hotkey="2" label="24 h" onPress={() => void face($, { hours: 24 })} />
          </>
        ) : (
          <Button key="lanes" label="Swimlanes" onPress={() => void face($, { face: 'lanes' })} />
        )}
        <Text color={stale ? 'warning' : undefined} dimColor={!stale}>{stale ? `${stamp} (stale)` : stamp}</Text>
      </Box>
    )
    const head = [
      <Text key="title" bold>{clip(`Zentrale · ${doc?.project ?? '?'}`, width)}</Text>,
      ...(v.error ? [<Text key="error" color="error">{clip(`⚠ ${v.error}`, width)}</Text>] : []),
      ...(doc?.gaps ?? []).map((g, i) => <Text key={`gap-${i}`} color="warning">{clip(`⚠ ${g}`, width)}</Text>),
    ]
    if (v.face === 'lanes') {
      const names = (v.lanes?.intervals ?? []).map(i => i.who)
      const nameWidth = Math.min(18, Math.max(4, ...names.map(n => n.length)))
      const cols = Math.max(10, width - nameWidth - 1)
      const drawn = lanes(v.lanes?.intervals ?? [], now, v.hours, cols)
      return (
        <Box flexDirection="column">
          {head}
          <Text bold>{clip(`Swimlanes · last ${v.hours} h`, width)}</Text>
          {v.lanes?.error && <Text color="error">{clip(`⚠ ${v.lanes.error}`, width)}</Text>}
          {!v.lanes && <Text dimColor>Reading the intervals…</Text>}
          {v.lanes && !drawn.length && <Text dimColor>nothing</Text>}
          {drawn.map(lane => {
            const runs: { glyph: string; color?: string }[] = []
            for (const c of lane.cells) {
              const last = runs[runs.length - 1]
              if (last && last.color === c.color) last.glyph += c.glyph
              else runs.push({ ...c })
            }
            return (
              <Box key={`lane-${lane.who}`} flexDirection="row">
                <Text>{`${clip(lane.who, nameWidth).padEnd(nameWidth)} `}</Text>
                {runs.map((r, i) => <Text key={`r-${i}`} color={r.color}>{r.glyph}</Text>)}
              </Box>
            )
          })}
          <Text dimColor>{clip(LEGEND, width)}</Text>
          {buttons}
        </Box>
      )
    }
    return (
      <Box flexDirection="column">
        {head}
        {(doc?.header ?? []).map((line, i) => <Text key={`hd-${i}`} dimColor>{clip(line, width)}</Text>)}
        <Box flexDirection="column" marginTop={1}>
          {doc && sectionLines(doc).map(l => (
            <Box key={l.key} paddingLeft={l.indent ?? 0}>
              <Text color={l.color} bold={l.bold} dimColor={l.dim}>{clip(l.text, width - (l.indent ?? 0))}</Text>
            </Box>
          ))}
        </Box>
        {buttons}
      </Box>
    )
  })
}
