# V38 — Complete Member Workspace + Public Navigation

- Fixed the member dashboard JavaScript load failure caused by declaring `leads` twice (data array and rendered HTML string).
- Renamed the data collection to `leadRows`; member workspace now renders instead of remaining on “Loading your member workspace…”.
- Added renewal/upgrade shortcut to the member portal sidebar.
- Added hash-aware active navigation for Overview, Membership, Profile, Certificates, Payments, Documents and Activity.
- Added accessible public Membership dropdown containing Membership and Member Portal.
- Added public Contact dropdown containing Contact Us and Request a Quote.
- Removed the duplicate standalone Request a Quote navigation CTA because it now lives under Contact.
- Added responsive dropdown behavior and member workspace layout safeguards.
