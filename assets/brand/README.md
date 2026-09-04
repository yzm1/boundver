# Boundver brand assets

SVG is the source of truth. The mark uses flat fills only:

- primary: `#6674F8`
- accent: `#08B8D1`
- monochrome dark: `#111936`
- monochrome light: `#FFFFFF`

| Asset | Use |
|---|---|
| `logo.svg` | Canonical full-color mark at 32px or larger |
| `logo-light.svg`, `logo-dark.svg` | Stable light/dark entry points; canonical colors are unchanged |
| `logo-small.svg`, `favicon.svg` | Simplified mark with an enlarged diamond below 32px |
| `logo-mono-dark.svg`, `logo-mono-light.svg` | One-color reproduction when color is unavailable |
| `boundver-lockup*.svg` | Mark plus wordmark where a horizontal identity is needed |
| `logo-{64,128,256,512,1024}.png` | Transparent raster exports for systems that cannot use SVG |
| `favicon-*.png` | 16px, 32px, and 48px raster favicon fallbacks |

Keep clearspace of at least half the diamond width around the visible mark.
That is 6.5 viewBox units for the primary mark. Do not stretch, rotate, add
effects, recolor arbitrarily, or alter the gap between the notch and diamond.

The PNG files are transparent exports rasterized from the corresponding SVG.
The pre-notch v0.14 mark is retained under `legacy/`; it is archival and
should not be used for new work.
