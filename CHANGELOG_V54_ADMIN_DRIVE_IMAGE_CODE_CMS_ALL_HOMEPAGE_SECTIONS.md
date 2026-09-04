# GLDC V54 — Admin Drive Image Codes + Full Homepage/Insights Editor

## Purpose
Expands V53 so Admin controls the main public-facing content and can attach real Google Drive PNG/JPG/JPEG images by stable GLDC image code.

## Drive image library
- Admin endpoint lists real PNG/JPG/JPEG files from the configured Google Drive folder.
- Each image receives a deterministic code: `IMG-` + first 10 uppercase SHA-1 characters of the Drive file ID.
- The code is cached in `site_media_codes` together with the Drive file ID and MIME type.
- Admin can preview and download images and use the code in homepage sections, services and insights.

## Homepage Editor
Admin can now edit and apply globally:
- Hero eyebrow, headline, supporting text and CTA labels.
- Key facts/stat cards, including the requested `04`, `360°`, `100%`, and `KENYA` values.
- Services/consultancy disciplines: add/remove any number of cards, edit title, description, CTA, link, fallback icon and Drive image code.
- `A CLEARER WORKFLOW`: eyebrow, title, description, feature list and Drive image code.
- `OUR APPROACH`: eyebrow, title, description and Drive image code.
- `MISSION`: eyebrow, title, description and Drive image code.
- `START A PROJECT`: eyebrow, title, description, CTA and URL.

All homepage values are stored in MongoDB content records and read server-side on each public request, so saved changes apply to every visitor without a redeploy.

## Public image presentation
- Selected Drive images render through a public server-side image route.
- Service, workflow, approach, mission and insight images can be clicked to open a large lightbox view.
- The public site does not expose Google service-account credentials or Drive API credentials.

## Insights Editor
New Admin section allows:
- Create insights.
- Edit existing insights.
- Publish/archive/draft.
- Edit category, title, excerpt and content.
- Attach a Drive image using its GLDC image code.
- Delete insights.

Published insights render their attached Drive image on `/insights`.

## Validation
- `python -m py_compile app.py` passes.
- No local Flask/browser runtime claim is made because the local environment does not contain the full production dependency set.
