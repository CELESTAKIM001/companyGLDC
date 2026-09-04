# V32 — Admin Design Restore

## Fixed
- Restored fixed-width admin sidebar using `flex: 0 0` so the navigation cannot collapse when wide tables/cards are present.
- Made the admin console full-width and responsive.
- Prevented content panels and tables from forcing the sidebar to shrink.
- Added horizontal table scrolling for wide operational datasets.
- Improved responsive breakpoints for desktop, tablet and mobile.
- Added stronger card/panel spacing, shadows and visual hierarchy.
- Hid the public website header/footer on the authenticated admin console so the admin workspace has its own full-screen operating-system layout.
- Preserved all existing V31 routes, APIs, member visibility rules, certificates, QR verification, payments and Daraja functionality.
