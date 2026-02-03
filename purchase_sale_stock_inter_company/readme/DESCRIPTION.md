This module is a glue module and is auto installed if
purchase_sale_inter_company, sale_stock and purchase_stock modules are
installed. Full purpose description can be found in
purchase_sale_inter_company.

In addition to the features provided by purchase_sale_inter_company, which
automatically creates inter-company Sale Orders from Purchase Orders, this module
extends the functionality by automatically validating the corresponding inter-company
receipts when the Delivery Order is confirmed. During this process, lot/serial numbers
and quantities are synchronized to ensure consistency across companies.

The configuration includes an option to specify a default Warehouse that will be
automatically assigned to Sale Orders generated from Purchase Orders addressed to
this company.

When Company A sends a product tracked by lot or serial number, a new
lot/serial number with the same name is created in Company B to match
it, if one doesn't already exist.

In addition to the existing behavior, this module has been improved to better support real-world inter-company stock flows:

Partial deliveries and backorders are now mirrored correctly between companies.
When a Delivery Order is partially validated on the Sale Order side, the corresponding
Purchase Order receipt will:

-receive only the delivered quantity,

-create a matching backorder receipt for the remaining quantity,

-and keep both sides aligned throughout the process.

Deterministic linking between Sale and Purchase pickings.
Each inter-company Delivery Order is now explicitly linked to exactly one corresponding
Purchase Order receipt via intercompany_picking_id.
This avoids incorrect behavior where multiple Purchase receipts could be updated
by a single Delivery Order in partial or backorder scenarios.

Improved robustness when delivery moves are not directly linked to purchase lines.
In some flows, delivery moves may not carry a purchase_line_id.
The synchronization logic now reliably falls back to the Sale-to-Purchase line link
to identify the correct destination receipt move.
