# Trinetra

**Air-gapped satellite-imagery intelligence prototype for the Indian Army.**

Trinetra watches areas of interest in any weather and raises an alert only when the evidence holds up. Three
independent detectors (optical index change, SAR backscatter change, SAR coherence loss) must agree before a
change is surfaced, and terrain effects such as slope, shadow and snowmelt are normalised out first. Analysts
pick a region on an India theatre map and fly into a 3D workspace built from real Copernicus GLO-30 elevation
data. From there they run plain-language searches, review alerts, follow site timelines, save watches and hand
off tasking cues. Every decision is written to a hash-chained audit log, and nothing ever leaves the machine.

Built with React 18, Tailwind 3, react-three-fiber (three.js) and Vite.

> All alerts, sites and imagery chips are synthetic and marked DEMO DATA. The terrain elevation is real.

---

## Highlights

- **Real terrain, fully offline.** Seven areas of interest are rendered in 3D from actual Copernicus GLO-30
  (30 m) elevation tiles that ship inside the repository. After the first load the app makes zero network
  requests.
- **Evidence before verdict.** An alert is raised only when three independent detectors agree, so raw
  differencing noise (snow, crops, river braiding, shadow, registration error) is filtered out. A built-in view
  shows the 200 raw alerts collapsing to the 12 that actually matter.
- **Pick a region, work in it.** Choose a sector, or draw a box, polygon or circle on the theatre map, then fly
  into a 3D workspace of exactly that area.
- **Weather-proof.** When cloud cover is high the workspace switches the drape to a SAR-style view automatically
  (Monsoon mode), so analysis continues when optical imagery is useless.
- **Trust by design.** Every sign-in, decision, export and watch is recorded in a SHA-256 hash-chained audit log
  that can be verified in the UI. A tamper test shows the chain breaking if any past entry is edited.
- **Sovereign and self-contained.** No external CDNs, tiles, fonts or map tokens. A Content-Security-Policy in
  `index.html` blocks any off-origin request.

---

## Features

### Login and access

- Indian Army themed sign-in landing page with a live 3D render of the real Kargil-Drass terrain behind the card.
- Two sign-in methods: smart-card (read card, then a 6-digit PIN) and service number plus PIN, with quick demo
  access buttons for Analyst and Supervisor roles.
- Sign-in and sign-out are audited. This is a demo gate only: no credential is checked against a directory and no
  PIN is stored or transmitted.

### Theatre and region selection

- Full India shaded-relief map with named sectors, each showing its pending-alert count and last ingest age.
- Free-hand region drawing (box, polygon, circle) with a live readout of area in square kilometres and estimated
  tile count while you draw.
- Multi-area sectors (for example Northern Border) zoom in and offer an area picker.
- Smooth camera fly-in from the flat map into the 3D workspace.

### 3D terrain workspace

- Real GLO-30 heightfield mesh with an adjustable vertical exaggeration slider, north arrow, scale bar and reset.
- Layer toggles: optical, SAR, hillshade, slope colour ramp, contours, rivers and modelled routes, change pins
  and coverage gaps.
- Cursor readout of coordinates (WGS84, MGRS or Indian Grid), elevation and slope angle anywhere on the terrain.
- Change pins stand on the terrain, coloured by status and pulsing when new. Clicking one opens a compact
  inspector.
- Cross-section tool: draw a line across the terrain and read an elevation profile with nearby change sites
  projected onto it.
- Terrain-normalisation view that hatches steep-slope and shadow zones and animates the raw-to-confirmed funnel.
- Time scrubber along the bottom with cloud-gap pips and a change-density histogram, plus automatic Monsoon SAR
  switching.

### Semantic search (Ask), with real query results and image embeddings

This is the part my friend built out locally. The Ask bar now returns real results driven by image embeddings
rather than placeholder matches.

- Plain-language queries such as "new structures near river since 2023" are parsed live into editable chips for
  object, spatial relation and time.
- **Image embedding of tiles.** Each imagery tile is embedded into a feature vector so the system can compare
  tiles by visual content, not just by metadata.
- **Real results on a query.** Running a query ranks tiles by how well their embeddings match the query intent,
  and the matches appear as numbered footprints on the terrain and in a ranked results list.
- **Find more like this.** Selecting a result re-ranks the whole area by similarity, using both a visual
  similarity score and a context score (terrain, elevation, road proximity, past activity).
- **Custom detector.** Mark a few example tiles, train a lightweight probe, and re-scan the area to pull back new
  matches, which can be saved as a standing watch.
- A feasibility banner appears when a query asks for something below sensor resolution and offers proxies instead
  (for example disturbed ground for vehicles).

### Review, sites, watches and handoff

- Review queue with a before/after swipe slider, a date scrubber over clear looks, and a visual evidence panel
  (confidence ring, three detector dots, onset bracket, cues and full provenance).
- Site dossiers with a lifecycle timeline (clearing to construction to occupied to abandoned), imagery and SAR
  evidence, analyst decisions, a similar-sites map and a possible-related-activity graph.
- Watches list for saved regions, queries and custom detectors, each showing last run and new-hit count.
- Gaps and weekly digest: a collection-gap map shaded by days since the last usable clear look, plus a digest of
  top confirmed changes, new sites, sites gone quiet and data gaps.
- Handoff screen that builds a tasking-cue package (footprint, priority, best collection window, target asset)
  and a one-page brief, with grid references in WGS84, MGRS and Indian Grid.

### Trust and audit

- SHA-256 hash-chained audit log with a Verify button and a tamper test that visibly breaks the chain.
- Capability envelope showing precision and recall per change type, terrain and sensor, and an honest list of
  what the system cannot detect at 30 m resolution.

### Interaction and polish

- Keyboard-first workflow: `J`/`K` to move, `C` confirm, `R` reject, `N` more data, `F` find similar, `/` to ask,
  `?` for help, and `1` to `9` to switch screens.
- Onboarding with a six-step spotlight tour, a scripted guided demo, and a help drawer.
- Fluid motion throughout (screen transitions, staggered lists, count-up numbers, sliding indicators, eased map
  zoom) that respects the operating system reduced-motion setting.

---

## Run

```bash
npm install
npm run dev          # http://localhost:5173
npm run build        # static bundle in dist/ (serve with: npm run preview)
```

At runtime the app makes no off-origin requests. Fonts are bundled, DEM tiles ship in `public/`, and the
Content-Security-Policy in `index.html` blocks anything else.

---

## DEM data (real, offline)

| Area | Location | Source | Relief |
| --- | --- | --- | --- |
| AOI-01 | Navi Mumbai (Thane Creek, Parsik ridge) | Copernicus GLO-30 plus WBM | -10 to 487 m |
| AOI-02 | Brahmaputra floodplain (Majuli) | Copernicus GLO-30 plus WBM | 76 to 122 m |
| AOI-03 | Ladakh plateau | Copernicus GLO-30 plus WBM | 4,250 to 6,638 m |
| AOI-04 | Uri to Kupwara (Jhelum valley), Northern Border | Copernicus GLO-30 plus WBM | 1,065 to 4,384 m |
| AOI-05 | Kargil to Drass, Northern Border | Copernicus GLO-30 plus WBM | 2,597 to 5,892 m |
| AOI-06 | Akhnoor to Samba (Chenab, Tawi, Shivaliks), Northern Border | Copernicus GLO-30 plus WBM | 245 to 999 m |
| AOI-07 | Amritsar to Wagah (Ravi, canals), Northern Border | Copernicus GLO-30 plus WBM | 207 to 249 m |
| Theatre | India shaded relief | Terrarium tiles z5 (SRTM/GMTED) | n/a |

The tiles are pre-converted once, on a connected machine, into offline heightmap files:

```bash
node scripts/fetch-dem.mjs         # writes public/dem/<AOI>.dem.bin / .wbm.bin / .json (skips ones already done; --force to redo)
python3 scripts/fetch_theatre.py   # writes public/theatre/relief.png and elev.bin
```

To add an area, add its bounding box to `scripts/fetch-dem.mjs` and to `AOIS` in `src/data/mock.js`. A sector can
hold several areas (`aois: [...]`, as in Northern Border); selecting it zooms the theatre and offers an area
picker. No political boundary line is drawn. If one is needed, drape an official Survey of India boundary layer as
an overlay. Regions with no offline tile (Central Plateau, Southern Ghats, or boxes drawn elsewhere) fall back to
a procedural DEM whose relief is seeded from the theatre elevation, and the UI labels it "DEM procedural".

Everything else is derived in the browser from the DEM (`src/lib/dem.js`, `src/lib/terrainLayers.js`): slope,
aspect, hillshade, contours, D8 drainage, a SAR-like drape from local incidence angle, the terrain-normalisation
mask, least-cost modelled routes (not surveyed roads), and the DEM-cut imagery chips.

---

## Screens and flow

The main path is Theatre to Region to 3D workspace, with a stepper for Region, Ask, Review, Site, Handoff.

| Route | Screen |
| --- | --- |
| `#/theatre` | India relief, sectors, box / polygon / circle drawing with live area and tile count, fly-in |
| `#/workspace` | 3D DEM terrain: layers, exaggeration, pins, inspector, Ask bar, profile tool, normalise funnel, time scrubber with monsoon auto-SAR |
| `#/review` | Review queue (swipe, date scrubber, visual evidence panel); `?view=raw` opens the 200 to 12 comparison |
| `#/ask` | Semantic search with embedding-driven results, find-more-like-this and custom detector |
| `#/sites/:id` | Site dossier with lifecycle, evidence, similar sites and related activity |
| `#/watches`, `#/gaps`, `#/handoff`, `#/audit` | Watches, collection gaps and digest, tasking handoff, audit log |

---

## Wiring a real backend

- `src/data/mock.js` holds API-shaped mock records. `src/state/AppStore.jsx` has one action per endpoint.
- `src/lib/dem.js`: `loadDem(id)` is the single place to swap in a tile server or GeoTIFF reader.
- `src/lib/imagery.js`: `renderScene()` is where real Sentinel or Cartosat chips replace the DEM-cut placeholders.
- `src/screens/Login.jsx`: `authenticate()` is where a real PKI or smart-card provider plugs in.
- Named components ready to wire: `TheatreMap`, `TerrainScene`, `LayerChips`, `Inspector`, `TimeScrubber`,
  `FunnelGraphic`, `CrossSection`, `GuidedTour`, `DemoDirector`, `HelpDrawer`.

---

## Disclaimer

This is a prototype for demonstration only. It is not an official Indian Army system and uses no official
insignia. All operational data shown is synthetic. Only the terrain elevation is real, sourced from the public
Copernicus GLO-30 dataset.
