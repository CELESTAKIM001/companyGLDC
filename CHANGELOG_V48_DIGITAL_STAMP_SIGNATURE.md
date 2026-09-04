# V48 — Digital Stamp, System Date & Signature Assets

## Included
- PDF generators reference the exact repository assets:
  - `static/assets/image_t055yg.png` — official GLDC digital stamp
  - `static/assets/SIGN.png` — authorized signature
- The assets are loaded directly from the deployed repository at PDF generation time, so re-uploading the two files to GitHub will activate them without another code change.
- The stamp date is generated dynamically at PDF creation using `APP_TIMEZONE` (default `Africa/Nairobi`) and formatted as `YYYY-MM-DD`. No date is baked into the stamp image.
- Stamp date overlay follows the supplied 1024×1024 placement reference: left `41.5%`, top `72.8%`, width `30%`, centered Courier Bold, blue.
- Signature and stamp are used in membership certificates, invoices, payment receipts, and quotations.
- Footer zones were kept physically separated so QR, signature, stamp, labels and footer notes do not overlap.
- If either image is temporarily absent, PDF generation does not crash; a restrained fallback is rendered until the repository asset is restored.

## Asset visibility
The application does not expose the raw stamp or signature through an application download/view route. They are used server-side while composing PDFs. For strict filesystem-level secrecy, keep the source assets outside a public static directory; the current deployment contract intentionally references `static/assets/` because those are the paths used by the GitHub deployment.
