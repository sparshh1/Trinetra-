import { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { AOIS, CHANGES, aoiById, searchCorpus } from '../data/mock'
import { useStore } from '../state/AppStore'
import { useHotkeys } from '../lib/useHotkeys'
import { Btn, Coord, Kbd, Label, Meter, MonoId, SectionHead } from '../components/ui/primitives'
import { Icon } from '../components/ui/Icon'
import { MapView, SceneChip } from '../components/imagery/Imagery'
import { mulberry32 } from '../lib/rng'

// ---------- query parsing: OBJECT / SPATIAL RELATION / TIME ----------
const TIME_RE = /\b(since\s+\d{4}|since\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*(?:\s+\d{4})?|in\s+the\s+last\s+\d+\s+(?:days|weeks|months)|last\s+\d+\s+(?:days|weeks|months)|after\s+(?:the\s+)?monsoon|before\s+(?:the\s+)?monsoon|this\s+(?:week|month|year)|in\s+\d{4})\b/i
const SPATIAL_RE = /\b((?:near|along|beside|within\s+\d+(?:\.\d+)?\s*(?:km|m)\s+of|close\s+to|adjacent\s+to|north\s+of|south\s+of|east\s+of|west\s+of|upstream\s+of|downstream\s+of)\s+(?:the\s+|a\s+|an\s+)?[a-z]+(?:\s+(?:crossing|bank|track|road|line|ridge|river|channel|slope|shelf))?)\b/i
const SUB_RESOLUTION = /\b(vehicles?|trucks?|cars?|tanks?|people|persons?|personnel|soldiers?|troops|tents?)\b/i

export function parseQuery(q) {
  let rest = ` ${q} `
  const chips = []
  const t = rest.match(TIME_RE)
  if (t) rest = rest.replace(t[0], ' ')
  const s = rest.match(SPATIAL_RE)
  if (s) rest = rest.replace(s[0], ' ')
  const obj = rest.replace(/\b(show|find|me|all|any|the|of|with|and|where|are|is)\b/gi, ' ').replace(/\s+/g, ' ').trim()
  if (obj) chips.push({ kind: 'OBJECT', text: obj })
  if (s) chips.push({ kind: 'SPATIAL RELATION', text: s[0].trim() })
  if (t) chips.push({ kind: 'TIME', text: t[0].trim() })
  return chips
}

const EXAMPLES = [
  'new structures near river since 2023',
  'cleared ground along the creek after monsoon',
  'tracks within 2 km of ridge in the last 60 days',
  'vehicles near the river crossing',
]

export function normQuery(q) {
  return q.trim().toLowerCase().replace(/\s+/g, ' ')
}

function ChipFace({ t, className }) {
  if (t.chip) {
    return <img src={`./${t.chip}`} alt={t.id} className={`object-cover bg-ink-900 ${className || ''}`} />
  }
  return (
    <SceneChip
      seed={t.seed}
      terrain={t.terrain}
      change={t.render}
      sensor={t.sensor === 'S1 SAR' ? 'sar' : 'optical'}
      size={192}
      lat={t.lat}
      lon={t.lon}
      box
      className={className}
    />
  )
}

const KIND_STYLE = {
  OBJECT: 'border-fg-muted text-fg-hi',
  'SPATIAL RELATION': 'border-sar/60 text-sar',
  TIME: 'border-amber-line text-amber',
}

export function QueryChips({ chips, onEdit, onRemove }) {
  const [editing, setEditing] = useState(null)
  const [draft, setDraft] = useState('')
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {chips.map((c, i) => (
        <span key={c.kind} className={`inline-flex items-center h-7 border bg-ink-950 ${KIND_STYLE[c.kind]}`}>
          <span className="px-1.5 h-full flex items-center text-[9.5px] tracking-[0.14em] border-r opacity-80" style={{ borderColor: 'inherit' }}>{c.kind}</span>
          {editing === i ? (
            <input
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => { onEdit(i, draft); setEditing(null) }}
              onKeyDown={(e) => { if (e.key === 'Enter') { onEdit(i, draft); setEditing(null) } if (e.key === 'Escape') setEditing(null) }}
              className="bg-transparent outline-none px-2 text-sm w-40"
            />
          ) : (
            <button className="px-2 text-sm hover:underline decoration-dotted underline-offset-4" onClick={() => { setEditing(i); setDraft(c.text) }} title="Click to edit">
              {c.text}
            </button>
          )}
          <button className="px-1.5 h-full opacity-60 hover:opacity-100" onClick={() => onRemove(i)} aria-label={`Remove ${c.kind}`}><Icon name="x" size={11} /></button>
        </span>
      ))}
    </div>
  )
}

function score(tile, chips, similarTo) {
  const r = mulberry32(tile.seed + chips.map((c) => c.text).join('').length)
  let s = tile.base * 0.6 + r() * 0.15
  const obj = chips.find((c) => c.kind === 'OBJECT')?.text ?? ''
  if (/struct|build|shelter|hut|compound/i.test(obj) && ['structure', 'shelter'].includes(tile.render)) s += 0.18
  if (/clear|ground|disturb|vehicle/i.test(obj) && ['clearing', 'disturbed'].includes(tile.render)) s += 0.18
  if (/track|road|route/i.test(obj) && tile.render === 'track') s += 0.2
  if (/berm|earth/i.test(obj) && tile.render === 'earthwork') s += 0.2
  if (chips.some((c) => /river|creek|channel/i.test(c.text))) s += (1 - tile.riverDist) * 0.12
  if (similarTo) {
    const ctx = (tile.context.terrain + tile.context.elevation + tile.context.road + tile.context.activity) / 4
    s = tile.visual * 0.6 + ctx * 0.4
  }
  return Math.min(0.99, s)
}

const ctxScore = (t) => (t.context.terrain + t.context.elevation + t.context.road + t.context.activity) / 4

export function ResultTile({ t, rank, selected, onSelect, onMore, marking, marked, onMark, similarMode }) {
  return (
    <div
      onClick={() => (marking ? onMark(t.id) : onSelect(t.id))}
      className={`relative panel cursor-pointer transition-colors ${selected ? 'border-fg-muted bg-ink-750' : 'hover:border-ink-400'} ${marked ? 'outline outline-1 outline-amber' : ''} ${t.isNew ? 'fade-in' : ''}`}
    >
      <div className="relative">
        <ChipFace t={t} className="aspect-square w-full" />
        <span className="absolute top-1.5 left-1.5 mono text-2xs px-1 bg-ink-950/85 text-fg-muted">#{rank}</span>
        <span className="absolute top-1.5 right-1.5 mono text-xs px-1.5 bg-ink-950/85 text-fg-hi">{t.score.toFixed(2)}</span>
        {marking && (
          <span className={`absolute bottom-1.5 right-1.5 w-5 h-5 border flex items-center justify-center ${marked ? 'bg-amber border-amber text-ink-900' : 'bg-ink-950/80 border-fg-muted'}`}>
            {marked && <Icon name="check" size={12} strokeWidth={2} />}
          </span>
        )}
        {t.isNew && <span className="absolute bottom-1.5 left-1.5 text-[9.5px] tracking-wider px-1 bg-amber text-ink-900 font-semibold">NEW HIT</span>}
      </div>
      <div className="p-2 space-y-1">
        <div className="flex items-center gap-2 text-2xs">
          <span className="mono text-fg">{t.date}</span>
          <span className={t.sensor === 'S1 SAR' ? 'text-sar' : 'text-fg-muted'}>{t.sensor}</span>
          <span className="flex-1" />
          <span className="text-fg-dim">{t.aoi}</span>
        </div>
        <div className="mono text-[10.5px] text-fg-muted truncate">{t.id}</div>
        {similarMode && t.context && (
          <div className="pt-1 space-y-1">
            <Meter label="Visual sim." value={t.visual} tone="bg-fg-muted" />
            <Meter label="Context" value={ctxScore(t)} tone="bg-teal" />
          </div>
        )}
        {!marking && (
          <button onClick={(e) => { e.stopPropagation(); onMore(t.id) }} className="w-full mt-1 h-6 text-2xs uppercase tracking-wider border border-ink-600 text-fg-muted hover:text-fg hover:border-ink-400 flex items-center justify-center gap-1.5">
            <Icon name="sparkle" size={11} /> Find more like this
          </button>
        )}
      </div>
    </div>
  )
}

function Landing({ onPick }) {
  return (
    <div className="flex-1 min-h-0 gridlines relative overflow-hidden">
      <svg className="absolute right-[-120px] top-1/2 -translate-y-1/2 opacity-[0.07]" width="720" height="720" viewBox="0 0 100 100" fill="none" stroke="#EEF2F5" strokeWidth="0.25">
        <circle cx="50" cy="50" r="48" /><circle cx="50" cy="50" r="34" /><circle cx="50" cy="50" r="20" />
        <path d="M50 0v100M0 50h100" />
        <path d="M50 12 83 70H17Z" strokeWidth="0.4" />
      </svg>
      <div className="absolute inset-0 flex flex-col justify-center px-16 max-w-[1100px]">
        <div className="label mb-6 flex items-center gap-3"><span className="mono">01</span><span className="w-10 h-px bg-fg-dim" />Trinetra · on-premises imagery intelligence</div>
        <h1 className="text-[40px] 2xl:text-[48px] leading-[1.14] font-extralight text-fg-hi tracking-tight">
          We watch everywhere, in any weather,
          <br />
          and only raise our hand when the <span className="font-normal">evidence holds up.</span>
        </h1>
        <div className="mt-10 grid grid-cols-4 gap-px bg-ink-600 border border-ink-600 max-w-[860px]">
          {[
            ['7,810 km²', '3 AOIs under watch'],
            ['78,100', 'tiles indexed, all offline'],
            ['3 of 3', 'independent detectors per alert'],
            ['200 → 12', 'raw alerts vs raised'],
          ].map(([v, l]) => (
            <div key={l} className="bg-ink-900 px-4 py-3">
              <div className="text-xl font-light num text-fg-hi">{v}</div>
              <div className="text-2xs text-fg-dim mt-0.5">{l}</div>
            </div>
          ))}
        </div>
        <div className="mt-10">
          <Label className="mb-2">Try a query</Label>
          <div className="flex flex-wrap gap-2">
            {EXAMPLES.map((e) => (
              <button key={e} onClick={() => onPick(e)} className="h-8 px-3 border border-ink-500 text-sm text-fg-muted hover:text-fg hover:border-fg-muted bg-ink-900">
                {e}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function ProbeStatus({ probe, total, aoi, hits, count, onSave }) {
  if (probe.phase === 'train') {
    return (
      <div className="flex items-center gap-3">
        <span className="w-2 h-2 bg-amber pulse" />
        <span className="text-sm text-fg">Training probe on {count} examples…</span>
        <span className="mono text-sm text-amber">{probe.t}s</span>
        <span className="flex-1" />
        <span className="text-2xs text-fg-dim">Linear probe on frozen on-prem embeddings · nothing leaves this workstation</span>
      </div>
    )
  }
  return (
    <div>
      <div className="flex items-center gap-3 text-sm">
        <span className={`w-2 h-2 ${probe.phase === 'done' ? 'bg-teal' : 'bg-amber pulse'}`} />
        <span className="text-fg">{probe.phase === 'done' ? 'Re-scan complete' : `Re-scanning ${aoi === 'ALL' ? 'all AOIs' : aoi}`}</span>
        <span className="mono text-fg-muted">{probe.scanned.toLocaleString()} / {total.toLocaleString()} tiles</span>
        <span className="flex-1" />
        <span className="text-fg-muted"><span className="mono text-amber">{hits}</span> new hits</span>
        {probe.phase === 'done' && <Btn size="sm" variant="outline" icon="watch" onClick={onSave}>Save as watch</Btn>}
      </div>
      <div className="h-1 bg-ink-600 mt-2"><div className="h-full bg-amber transition-all" style={{ width: `${(probe.scanned / total) * 100}%` }} /></div>
    </div>
  )
}

export default function Ask() {
  const { aoi: storeAoi, log, addWatch, watches } = useStore()
  const nav = useNavigate()
  const loc = useLocation()
  const [params, setParams] = useSearchParams()
  const inputRef = useRef(null)
  const [text, setText] = useState('')
  const [chips, setChips] = useState([])
  const [submitted, setSubmitted] = useState(false)
  const [aoi, setAoi] = useState(storeAoi)
  const [range, setRange] = useState('90d')
  const [sensor, setSensor] = useState('Any')
  const [maxCloud, setMaxCloud] = useState(20)
  const [selected, setSelected] = useState(null)
  const [similarTo, setSimilarTo] = useState(null)
  const [marking, setMarking] = useState(false)
  const [marked, setMarked] = useState([])
  const [probe, setProbe] = useState(null) // { phase: 'train' | 'scan' | 'done', t, scanned }
  const [detectorHits, setDetectorHits] = useState([])
  const [semIndex, setSemIndex] = useState(null)
  const [semError, setSemError] = useState(null)

  useEffect(() => setAoi(storeAoi), [storeAoi])
  useEffect(() => {
    let dead = false
    fetch('./semantic/index.json')
      .then((r) => {
        if (!r.ok) throw new Error(`semantic index ${r.status}`)
        return r.json()
      })
      .then((data) => { if (!dead) setSemIndex(data) })
      .catch((err) => { if (!dead) setSemError(err.message || 'index unavailable') })
    return () => { dead = true }
  }, [])
  useEffect(() => { inputRef.current?.focus() }, [loc.state?.focus])

  // Entry from the review queue ("F" = find similar).
  useEffect(() => {
    const sim = params.get('similar')
    if (!sim) return
    const c = CHANGES.find((x) => x.id === sim)
    if (c) {
      setText(`${c.type.toLowerCase()} like ${c.id}`)
      setChips([{ kind: 'OBJECT', text: c.type.toLowerCase() }, { kind: 'SPATIAL RELATION', text: `same context as ${c.id}` }])
      setSubmitted(true)
      setAoi(c.aoi)
      setSimilarTo(c.id)
    }
  }, [params])

  const live = useMemo(() => parseQuery(text), [text])
  const activeChips = submitted ? chips : live
  const infeasible = activeChips.some((c) => c.kind === 'OBJECT' && SUB_RESOLUTION.test(c.text))

  const corpus = useMemo(() => searchCorpus(aoi), [aoi])
  const queryKey = normQuery(text)
  const stored = semIndex?.queries?.[queryKey]?.results
  const unknownQuery = submitted && !similarTo && !!semIndex && !stored
  const waitingIndex = submitted && !similarTo && !semIndex && !semError
  const results = useMemo(() => {
    if (!submitted) return []
    // "Find more like this" stays on the mock corpus. It is not a CLOSP search.
    if (similarTo) {
      return corpus
        .filter((t) => sensor === 'Any' || t.sensor.startsWith(sensor))
        .filter((t) => t.cloud <= maxCloud)
        .map((t) => ({ ...t, score: score(t, chips, similarTo) }))
        .sort((a, b) => b.score - a.score)
        .slice(0, 12)
    }
    if (!stored) return []
    return stored.filter((t) => (
      (aoi === 'ALL' || t.aoi === aoi)
      && (sensor === 'Any' || t.sensor.startsWith(sensor))
      && t.cloud <= maxCloud
    ))
  }, [corpus, chips, submitted, similarTo, sensor, maxCloud, stored, aoi])
  const shown = [...detectorHits, ...results]
  const total = aoi === 'ALL' ? 78100 : aoiById(aoi).tiles

  const run = (q = text) => {
    setText(q)
    setChips(parseQuery(q))
    setSubmitted(true)
    setSimilarTo(null)
    setDetectorHits([])
    setProbe(null)
    if (params.get('similar')) setParams({})
    log('QUERY', aoi, q)
  }

  const more = (id) => {
    setSimilarTo(id)
    setSelected(id)
    log('FIND_SIMILAR', id, 'Re-ranked with visual and context similarity')
  }

  const trainProbe = () => {
    setMarking(false)
    setDetectorHits([])
    setProbe({ phase: 'train', t: 4, scanned: 0 })
    log('DETECTOR_TRAIN', `probe (${marked.length} examples)`, marked.join(', '))
  }

  useEffect(() => {
    if (!probe) return
    if (probe.phase === 'train') {
      if (probe.t <= 0) { setProbe({ phase: 'scan', t: 0, scanned: 0 }); return }
      const id = setTimeout(() => setProbe((p) => ({ ...p, t: p.t - 1 })), 1000)
      return () => clearTimeout(id)
    }
    if (probe.phase === 'scan') {
      if (probe.scanned >= total) { setProbe((p) => ({ ...p, phase: 'done' })); return }
      const id = setTimeout(() => {
        const next = Math.min(total, probe.scanned + Math.round(total / 14))
        const r = mulberry32(next)
        if (r() < 0.7) {
          const src = corpus[Math.floor(r() * corpus.length)]
          setDetectorHits((h) => [{
            ...src,
            id: `${src.id.slice(0, 5)}_${String(1000 + Math.floor(r() * 8999))}_${String(Math.floor(r() * 9999)).padStart(4, '0')}`,
            seed: src.seed + h.length * 3 + 11,
            score: 0.8 + r() * 0.17,
            isNew: true,
          }, ...h].slice(0, 8))
        }
        setProbe((p) => ({ ...p, scanned: next }))
      }, 280)
      return () => clearTimeout(id)
    }
  }, [probe, total, corpus])

  useHotkeys({ f: () => selected && more(selected) })

  const sel = shown.find((t) => t.id === selected)
  const mapAoi = aoi === 'ALL' ? (sel?.aoi ?? 'AOI-03') : aoi

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <div className="border-b border-ink-600 bg-ink-950 px-4 py-3 space-y-2.5 shrink-0">
        <form onSubmit={(e) => { e.preventDefault(); run() }} className="flex items-center gap-2">
          <div className="flex-1 flex items-center h-10 border border-ink-500 bg-ink-900 focus-within:border-fg-muted">
            <Icon name="ask" size={16} className="mx-3 text-fg-dim" />
            <input
              id="ask-input"
              ref={inputRef}
              value={text}
              onChange={(e) => { setText(e.target.value); setSubmitted(false) }}
              placeholder="Describe what to find, e.g. new structures near river since 2023"
              className="flex-1 bg-transparent outline-none text-base text-fg placeholder:text-fg-dim"
            />
            <Kbd className="mr-3">/</Kbd>
          </div>
          <Btn variant="primary" size="lg" type="submit">Search</Btn>
        </form>
        <div className="flex items-center gap-2 flex-wrap">
          <select className="select" value={aoi} onChange={(e) => setAoi(e.target.value)}>
            <option value="ALL">AOI · All</option>
            {AOIS.map((a) => <option key={a.id} value={a.id}>{a.id} · {a.name}</option>)}
          </select>
          <select className="select" value={range} onChange={(e) => setRange(e.target.value)}>
            {['30d', '90d', '1y', 'since 2023'].map((r) => <option key={r} value={r}>Dates · {r}</option>)}
          </select>
          <select className="select" value={sensor} onChange={(e) => setSensor(e.target.value)}>
            {['Any', 'S2', 'S1'].map((s) => <option key={s} value={s}>Sensor · {s === 'Any' ? 'Optical + SAR' : s === 'S2' ? 'Sentinel-2' : 'Sentinel-1 SAR'}</option>)}
          </select>
          <select className="select" value={maxCloud} onChange={(e) => setMaxCloud(+e.target.value)}>
            {[5, 10, 20, 40, 100].map((c) => <option key={c} value={c}>Max cloud · {c}%</option>)}
          </select>
          <span className="w-px h-5 bg-ink-600 mx-1" />
          {activeChips.length > 0 ? (
            <QueryChips
              chips={activeChips}
              onEdit={(i, v) => { const n = [...activeChips]; n[i] = { ...n[i], text: v }; setChips(n.filter((c) => c.text.trim())); setSubmitted(true) }}
              onRemove={(i) => { setChips(activeChips.filter((_, j) => j !== i)); setSubmitted(true) }}
            />
          ) : (
            <span className="text-xs text-fg-dim">Queries are parsed as you type into OBJECT · SPATIAL RELATION · TIME</span>
          )}
        </div>
      </div>

      {!submitted ? (
        <Landing onPick={run} />
      ) : (
        <div className="flex-1 min-h-0 grid grid-cols-[minmax(0,1fr)_400px] 2xl:grid-cols-[minmax(0,1fr)_480px]">
          <div className="min-h-0 flex flex-col">
            {infeasible && (
              <div className="mx-4 mt-3 flex gap-3 px-3 py-2.5 border border-amber-line bg-amber-dim text-sm shrink-0">
                <Icon name="eye" size={16} className="text-amber mt-0.5 shrink-0" />
                <div>
                  <div className="text-amber font-medium">Below resolution</div>
                  <div className="text-fg-muted text-xs mt-0.5">Individual vehicles are below 10 m resolution. Showing proxies such as disturbed ground, fresh tracks and cleared parking areas.</div>
                </div>
              </div>
            )}
            <div className="flex items-center gap-3 px-4 h-11 shrink-0">
              <SectionHead
                className="flex-1"
                title={similarTo ? `Re-ranked · more like ${similarTo}` : unknownQuery ? 'Query not in the precomputed set' : waitingIndex ? 'Loading precomputed index' : `${shown.length} ranked tiles`}
                right={
                  <div className="flex items-center gap-2">
                    {similarTo && <Btn size="sm" variant="ghost" onClick={() => setSimilarTo(null)}>Clear similarity</Btn>}
                    {!(probe && (probe.phase === 'train' || probe.phase === 'scan')) &&
                      (marking ? (
                        <>
                          <span className="text-2xs text-amber">Mark 2–5 example tiles · {marked.length} selected</span>
                          <Btn size="sm" variant="ghost" onClick={() => { setMarking(false); setMarked([]) }}>Cancel</Btn>
                          <Btn size="sm" variant="amber" disabled={marked.length < 2} onClick={trainProbe}>Train probe</Btn>
                        </>
                      ) : (
                        <Btn size="sm" variant="outline" icon="target" onClick={() => { setMarking(true); setMarked([]) }}>Build custom detector</Btn>
                      ))}
                  </div>
                }
              />
            </div>
            {similarTo && (
              <div className="mx-4 mb-3 text-2xs text-fg-muted flex items-center gap-4 shrink-0">
                <span className="flex items-center gap-1.5"><span className="w-3 h-1.5 bg-fg-muted" /> Visual similarity (embedding distance)</span>
                <span className="flex items-center gap-1.5"><span className="w-3 h-1.5 bg-teal" /> Context match: terrain, elevation, road proximity, past activity</span>
              </div>
            )}
            {probe && (
              <div className="mx-4 mb-3 panel brackets px-3 py-2.5 shrink-0 fade-in">
                <ProbeStatus
                  probe={probe} total={total} aoi={aoi} hits={detectorHits.length} count={marked.length}
                  onSave={() => addWatch({ id: `W-${String(watches.length + 1).padStart(2, '0')}`, name: `Custom detector · ${chips[0]?.text ?? 'probe'}`, kind: 'Custom detector', aoi: aoi === 'ALL' ? 'AOI-03' : aoi, lastRun: 'just now', hits: detectorHits.length, cadence: 'Every ingest' })}
                />
              </div>
            )}
            <div className="flex-1 overflow-auto px-4 pb-4">
              {semError && (
                <div className="mb-3 text-sm text-amber">Semantic index failed to load ({semError}).</div>
              )}
              {unknownQuery && (
                <div className="panel p-4 max-w-3xl">
                  <div className="text-sm text-fg">Only these four queries are in this precomputed set.</div>
                  <div className="flex flex-wrap gap-2 mt-3">
                    {EXAMPLES.map((e) => (
                      <button key={e} onClick={() => run(e)} className="h-8 px-3 border border-ink-500 text-sm text-fg-muted hover:text-fg hover:border-fg-muted bg-ink-900">
                        {e}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {waitingIndex && <div className="text-sm text-fg-muted">Loading precomputed index…</div>}
              {!unknownQuery && !waitingIndex && shown.length === 0 && submitted && (
                <div className="text-sm text-fg-muted">No chips match the current AOI, sensor, or cloud filter.</div>
              )}
              {!unknownQuery && (
                <div className="grid grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5 gap-3">
                  {shown.map((t, i) => (
                    <ResultTile
                      key={t.id}
                      t={t}
                      rank={i + 1}
                      selected={selected === t.id}
                      onSelect={setSelected}
                      onMore={more}
                      marking={marking}
                      marked={marked.includes(t.id)}
                      onMark={(id) => setMarked((m) => (m.includes(id) ? m.filter((x) => x !== id) : m.length < 5 ? [...m, id] : m))}
                      similarMode={!!similarTo}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>

          <aside className="border-l border-ink-600 min-h-0 flex flex-col bg-ink-850">
            <MapView
              aoiId={mapAoi}
              className="flex-1 min-h-0"
              markers={shown.filter((t) => t.aoi === mapAoi).map((t) => ({ id: t.id, lat: t.lat, lon: t.lon, tone: t.isNew ? 'amber' : selected === t.id ? 'fg' : 'teal', active: selected === t.id, label: `${t.id} · ${t.score.toFixed(2)}` }))}
              onMarkerClick={setSelected}
            />
            <div className="border-t border-ink-600 p-3 shrink-0 h-[196px]">
              {sel ? (
                <div className="flex gap-3 h-full">
                  <ChipFace t={sel} className="w-[170px] h-[170px] border border-ink-600 shrink-0" />
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <MonoId>{sel.id}</MonoId>
                    <Coord lat={sel.lat} lon={sel.lon} className="block text-fg-muted" />
                    {sel.context ? (
                      <div className="space-y-1 pt-1">
                        <Meter label="Terrain" value={sel.context.terrain} tone="bg-teal" />
                        <Meter label="Elevation" value={sel.context.elevation} tone="bg-teal" />
                        <Meter label="Road prox." value={sel.context.road} tone="bg-teal" />
                        <Meter label="Past activity" value={sel.context.activity} tone="bg-teal" />
                      </div>
                    ) : (
                      <div className="space-y-1 pt-1 text-2xs text-fg-muted">
                        <div>{sel.date} · <span className={sel.sensor === 'S1 SAR' ? 'text-sar' : ''}>{sel.sensor}</span></div>
                        <div className="mono">score {sel.score.toFixed(2)}{sel.clip != null ? ` · clip ${Number(sel.clip).toFixed(2)}` : ''}</div>
                        {sel.scene && <div className="mono truncate" title={sel.scene}>{sel.scene}</div>}
                      </div>
                    )}
                    <div className="flex gap-2 pt-1">
                      <Btn size="sm" icon="sparkle" onClick={() => more(sel.id)}>More like this <Kbd>F</Kbd></Btn>
                      <Btn size="sm" variant="ghost" onClick={() => nav('/review')}>Queue</Btn>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="h-full flex items-center justify-center text-xs text-fg-dim">Select a tile to see its context profile</div>
              )}
            </div>
          </aside>
        </div>
      )}
    </div>
  )
}
