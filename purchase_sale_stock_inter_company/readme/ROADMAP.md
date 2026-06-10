- The module is not yet fully robust in complex logistics flows, such as
multi-step receipts and multi-step deliveries.
Partial deliveries, backorders, and returns are supported and synchronized
between companies, but multi-step receipt and delivery flows could still be
improved further.
- Return mirroring currently focuses on quantities mostly.
  Lot/serial tracking is NOT fully supported for intercompany returns.
  Depending on routes and picking types, manual lot assignment may be
  required and return validation may fail for tracked products. Return mirroring supports tracked products only when lot/serials are set on the source return; otherwise mirroring is blocked.
- This module does not sync packages.
