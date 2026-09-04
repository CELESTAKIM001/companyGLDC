# V49 — Strict Receipt & Membership Certificate Document Layout

## Receipt and Membership Certificate
- Official GLDC logo is centered in the top header above the primary title.
- Header logo is proportionally constrained to a maximum 60px-equivalent height.
- GLDC production palette is applied: `#032B88`, `#F0F4FA`, `#1B1B1B`, with `#D1D5DB` borders.
- Receipt details and certificate metadata use padded key/value tables with alternating light-blue rows and generous vertical spacing.
- Digital stamp is bounded inside its footer zone and kept below the requested 180px-equivalent maximum.
- Signature, stamp, QR and footer note occupy separate bounded areas and do not overlap.
- Digital stamp date remains generated at PDF creation from the configured application timezone, in `YYYY-MM-DD` format.
- Date overlay remains aligned to the supplied stamp reference: left `41.5%`, top `72.8%`, width `30%`, centered Courier Bold, GLDC blue.

## Existing V48 asset contract retained
- `static/assets/image_t055yg.png` — digital stamp.
- `static/assets/SIGN.png` — authorized signature.
- Assets are loaded server-side during PDF composition.
- Direct raw application access to those two paths is restricted to administrators.
