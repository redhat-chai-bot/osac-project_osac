# Cluster details review screenshots

Source: `f6fc017c84dd930d258612e0c69aca1b2882e422`.

Production UI rendered with synthetic fixture data; not a live deployment.
All authentication and API responses are synthetic; unexpected network requests are blocked.
Only production SPA styles are used, without reference styles or screenshot-specific changes.

- `desktop-overview.png`: 1440 × 1100 viewport showing the header and independent detail columns; the lower External IP card extends beyond this viewport.
- `desktop-networking.png`: detail-grid section capture at a 1440px browser viewport, including Networking, Conditions and both External IP endpoint addresses; not a full-page image.
- `mobile-version.png`: Cluster version card component capture at a 390px browser viewport; not the shared masthead or a full-page image.

The Lifecycle section shows Created without a raw creator identifier.
Node sets and Settings have separators directly below their titles.
The Networking/Conditions boundary has one structural separator.
Example external addresses use documentation ranges; hostnames use `.example.test` and the displayed public key is explicitly fake.
PNG ancillary metadata is removed losslessly; only IHDR, IDAT and IEND chunks remain.
