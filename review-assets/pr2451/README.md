# Cluster details screenshots

Production UI rendered with synthetic fixture data; not a live deployment.
Source: `a24fc1d1bff8370da538a57d544435b676a4995d` (PR #2451).
Only the production SPA's styles are used; no screenshot-specific CSS.

| Image | Capture | Description |
| --- | --- | --- |
| [Desktop overview](desktop-overview.png) | 1440 × 1100 desktop viewport | Overview, Lifecycle and the grey Cluster version card, including Current, Upgrade history, node summaries and Settings. The next row is partially visible at the viewport bottom. |
| [Desktop networking](desktop-networking.png) | 1054 × 464 section crop from a 1440px desktop viewport | Networking beside External IP endpoints, with full-width Conditions below. |
| [Mobile version](mobile-version.png) | 342 × 632 card crop from a 390px mobile viewport | Cluster version header actions, node summaries and Settings after closing navigation using its normal toggle. This is a component capture, not a full mobile page. |

All identities, resource identifiers and responses are authored demo fixtures.
Hostnames use the reserved `.example.test` domain; the displayed SSH text is an
obvious nonfunctional `FAKE_PUBLIC_KEY` placeholder, not key material.
API/authentication responses are intercepted locally; no live backend is used.
PNG files contain only IHDR, IDAT and IEND chunks, with no ancillary metadata.
