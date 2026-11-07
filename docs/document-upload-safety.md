# Document Upload Safety

Documents exposed to the extension must already be attached to an approved application package. The extension must reject arbitrary local paths and must not expose the full candidate document library.

The application side panel offers eligible PDF metadata for review. Explicit approval of a static Profile resume creates a package containing that exact artifact for the selected job. Generated documents require their existing package clearance. The byte download rechecks approval, ownership, job, document purpose and SHA-256 through a scoped short-lived token. An upload target must be identified and reviewed separately; ambiguous or custom targets stay manual.
