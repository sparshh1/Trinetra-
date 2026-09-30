# CLOSP Ask semantic index

Checkpoint: `DarthReca/CLOSP-VL`
Device: `cpu`
Optical chips: 56
SAR chips: 56
Newest scene in the corpus: 2026-09-26

Gallery: `public/semantic/gallery/index.html` (contact sheets `s2.jpg`, `s1.jpg`, `all.jpg`).

`clip` is the CLOSP cosine (L2-normalized text and image embeddings, dot product) from this model run.
`score` is that cosine plus the small water, monsoon, or elevation term in `scripts/build_semantic_index.py`, clamped to 0.99.
Neither number was typed in.

Sentinel-2 chips are L2A, 13-band SSL4EO order (B10 left at zero because L2A has no cirrus band), divided by 10000 before the optical encoder.
Sentinel-1 chips are GRD RTC gamma0 VV and VH from Planetary Computer, converted to dB before the SAR encoder.

| Query | Object embedded | Top score | Sensors in top 12 |
| --- | --- | --- | --- |
| `new structures near river since 2023` | `new structures` | 0.0302 | S1 SAR, S2 MSI |
| `cleared ground along the creek after monsoon` | `cleared ground` | 0.2325 | S1 SAR, S2 MSI |
| `tracks within 2 km of ridge in the last 60 days` | `tracks` | 0.0506 | S1 SAR, S2 MSI |
| `vehicles near the river crossing` | `vehicles` | 0.1790 | S1 SAR, S2 MSI |
