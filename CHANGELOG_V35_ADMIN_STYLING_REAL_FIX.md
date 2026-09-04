# GLDC V35 — Admin Styling Real Fix

- Replaced view hiding with strongly scoped CSS on direct admin sections.
- Navigation now sets `display:none` inline on every inactive view and `display:block` on the selected view.
- Initial dashboard state is explicitly normalized on page load.
- Scoped admin `.main` sizing to prevent inherited public-site styles from collapsing the workspace.
- Admin sections are full-width and membership panels cannot appear beside Dashboard.
- Preserves V34 functionality and all prior production features.
