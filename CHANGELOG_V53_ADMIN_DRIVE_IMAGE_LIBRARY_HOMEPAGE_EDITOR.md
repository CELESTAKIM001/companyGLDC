# GLDC V53 — Admin Drive Image Library + Homepage Editor

## Google Drive image library
- Added an Admin-only image library endpoint: `/api/admin/drive/images`.
- Scans the configured Google Drive folder and lists real PNG, JPG and JPEG files only.
- Every image receives a deterministic stable admin code such as `IMG-A1B2C3D4E5` derived from the Drive file ID.
- Added Admin-only image preview endpoint: `/api/admin/drive/image/<file_id>`.
- Admin can see image preview, filename, MIME type, size, modified time, Drive ID, generated image code and download action.
- `USE FOR HERO` sends the selected Drive file ID into the existing `home.hero` media slot editor.
- No fake or fabricated Drive files are created.

## Homepage editor
- Added a dedicated `Homepage Editor` Admin section.
- Editable fields:
  - Hero eyebrow
  - Main headline
  - Supporting text
  - Primary CTA label
  - Secondary CTA label
- Homepage now reads these values dynamically from MongoDB content records.
- Defaults remain:
  - `Land. Design. Development. Done Right.`
  - `Professional consultancy solutions for land development, planning and design — coordinated around practical project outcomes.`
- Saved content is written through the existing CSRF-protected Admin CMS endpoint.

## Security
- Drive image listing and preview remain behind `@admin_required`.
- Public website does not expose the Admin preview endpoint.
- Public hero images continue to use the existing published media route.
