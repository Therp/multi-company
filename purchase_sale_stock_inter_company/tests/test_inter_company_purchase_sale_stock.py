# Copyright 2013-Today Odoo SA
# Copyright 2019-2019 Chafique DELLI @ Akretion
# Copyright 2018-2019 Tecnativa - Carlos Dauden
# Copyright 2020 ForgeFlow S.L. (https://www.forgeflow.com)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import Command
from odoo.exceptions import UserError

from odoo.addons.purchase_sale_inter_company.tests import (
    test_inter_company_purchase_sale as test_icps,
)

TestPurchaseSaleInterCompany = test_icps.TestPurchaseSaleInterCompany


class TestPurchaseSaleStockInterCompany(TestPurchaseSaleInterCompany):
    @classmethod
    def _configure_user(cls, user):
        res = super()._configure_user(user)
        # Add stock user group to the user
        # to prevent access errors during tests
        # When `stock_picking_batch` is installed,
        # the model stock.picking.batch
        # has access rights that restrict access to the group_stock_user
        user.groups_id |= cls.env.ref("stock.group_stock_user")
        return res

    @classmethod
    def _create_warehouse(cls, code, company):
        address = cls.env["res.partner"].create({"name": f"{code} address"})
        return cls.env["stock.warehouse"].create(
            {
                "name": f"Warehouse {code}",
                "code": code,
                "partner_id": address.id,
                "company_id": company.id,
            }
        )

    @classmethod
    def _create_serial_and_quant(cls, product, name, company, quant=True):
        lot = cls.lot_obj.create(
            {"product_id": product.id, "name": name, "company_id": company.id}
        )
        if quant:
            cls.quant_obj.create(
                {
                    "product_id": product.id,
                    "location_id": cls.warehouse_c.lot_stock_id.id,
                    "quantity": 1,
                    "lot_id": lot.id,
                }
            )
        return lot

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.lot_obj = cls.env["stock.lot"]
        cls.quant_obj = cls.env["stock.quant"]
        # Configure 2 Warehouse per company
        cls.warehouse_a = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company_a.id)]
        )
        cls.warehouse_b = cls._create_warehouse("CA-WB", cls.company_a)

        cls.warehouse_c = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company_b.id)]
        )
        cls.warehouse_d = cls._create_warehouse("CB-WD", cls.company_b)
        cls.company_b.warehouse_id = cls.warehouse_c
        cls.consumable_product = cls.env["product.product"].create(
            {
                "name": "Consumable Product",
                "type": "consu",
                "is_storable": False,
                "categ_id": cls.env.ref("product.product_category_all").id,
                "qty_available": 100,
            }
        )
        cls.stockable_product_serial = cls.env["product.product"].create(
            {
                "name": "Stockable Product Tracked by Serial",
                "type": "consu",
                "is_storable": True,
                "tracking": "serial",
                "categ_id": cls.env.ref("product.product_category_all").id,
            }
        )
        # Add quants for product tracked by serial to supplier
        cls.serial_1 = cls._create_serial_and_quant(
            cls.stockable_product_serial, "111", cls.env["res.company"]
        )
        cls.serial_2 = cls._create_serial_and_quant(
            cls.stockable_product_serial, "222", cls.company_b
        )
        cls.serial_3 = cls._create_serial_and_quant(
            cls.stockable_product_serial, "333", cls.company_b
        )
        cls.serial_4 = cls._create_serial_and_quant(
            cls.stockable_product_serial, "444", cls.company_b
        )
        cls.serial_5 = cls._create_serial_and_quant(
            cls.stockable_product_serial, "555", cls.company_b
        )

    def test_deliver_to_warehouse_a(self):
        self.purchase_company_a.picking_type_id = self.warehouse_a.in_type_id
        sale = self._approve_po()
        self.assertEqual(self.warehouse_a.partner_id, sale.partner_shipping_id)

    def test_deliver_to_warehouse_b(self):
        self.purchase_company_a.picking_type_id = self.warehouse_b.in_type_id
        sale = self._approve_po()
        self.assertEqual(self.warehouse_b.partner_id, sale.partner_shipping_id)

    def test_send_from_warehouse_c(self):
        self.company_b.warehouse_id = self.warehouse_c
        sale = self._approve_po()
        self.assertEqual(sale.warehouse_id, self.warehouse_c)

    def test_send_from_warehouse_d(self):
        self.company_b.warehouse_id = self.warehouse_d
        sale = self._approve_po()
        self.assertEqual(sale.warehouse_id, self.warehouse_d)

    def test_purchase_sale_stock_inter_company(self):
        self.purchase_company_a.notes = "Test note"
        sale = self._approve_po()
        self.assertEqual(
            sale.partner_shipping_id,
            self.purchase_company_a.picking_type_id.warehouse_id.partner_id,
        )
        self.assertEqual(sale.warehouse_id, self.warehouse_c)

    def test_sync_intercompany_picking_qty_with_backorder(self):
        self.product.type = "consu"
        self.company_a.sync_picking = True
        self.partner_company_b.company_id = False
        purchase = self.purchase_company_a
        sale = self._approve_po()
        sale_picking = sale.picking_ids[0]
        sale_picking.with_company(sale_picking.company_id).action_confirm()
        sale_picking.move_ids.quantity = 1.0
        sale_picking.move_ids.picked = True
        res_dict = sale_picking.with_company(sale_picking.company_id).button_validate()
        if isinstance(res_dict, dict) and "context" in res_dict:
            wizard = (
                self.env["stock.backorder.confirmation"]
                .with_context(**res_dict.get("context"))
                .create({})
            )
            wizard.process()
        sale_picking2 = sale.picking_ids.filtered(lambda p: p.state != "done")
        self.assertEqual(purchase.picking_ids[0].move_line_ids.quantity, 1)
        self.assertEqual(purchase.picking_ids[1].move_line_ids.quantity, 2)
        self.assertEqual(purchase.order_line.qty_received, 1)
        sale_picking2.move_ids.quantity = 2.0
        sale_picking2.with_company(sale_picking2.company_id).action_confirm()
        sale_picking2.with_company(sale_picking2.company_id).button_validate()
        self.assertEqual(purchase.picking_ids[0].move_line_ids.quantity, 1)
        self.assertEqual(purchase.picking_ids[1].move_line_ids.quantity, 2)
        self.assertEqual(purchase.order_line.qty_received, 3)

    def test_purchase_sale_with_two_products_no_backorder(self):
        self.product.type = "consu"
        self.partner_company_b.company_id = False
        self.product2 = self.env["product.product"].create(
            {"name": "Product 2", "type": "consu", "is_storable": True}
        )
        self.purchase_company_a.write(
            {
                "order_line": [
                    Command.create({"product_id": self.product2.id, "product_qty": 1}),
                ]
            }
        )
        sale = self._approve_po()
        sale_picking = sale.picking_ids
        self.assertEqual(len(sale.picking_ids), 1)
        sale_picking.with_company(sale_picking.company_id).action_confirm()
        for move in sale_picking.move_ids:
            move.quantity = move.product_uom_qty
        sale_picking.with_company(sale_picking.company_id).button_validate()
        self.assertEqual(len(self.purchase_company_a.picking_ids), 1)
        self.assertEqual(len(self.purchase_company_a.picking_ids.move_line_ids), 2)

    def test_sync_picking(self):
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True

        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        sale = self._approve_po(purchase)

        self.assertTrue(purchase.picking_ids)
        self.assertTrue(sale.picking_ids)

        po_picking_id = purchase.picking_ids
        so_picking_id = sale.picking_ids

        # check po_picking state
        self.assertEqual(po_picking_id.state, "waiting")

        # validate the SO picking
        so_picking_id.move_ids.quantity = 2

        self.assertNotEqual(po_picking_id, so_picking_id)
        self.assertNotEqual(
            po_picking_id.move_ids.quantity,
            so_picking_id.move_ids.quantity,
        )
        self.assertEqual(
            po_picking_id.move_ids.product_qty,
            so_picking_id.move_ids.product_qty,
        )
        wizard_data = so_picking_id.with_user(self.user_company_b).button_validate()
        wizard = (
            self.env["stock.backorder.confirmation"]
            .with_context(**wizard_data.get("context"))
            .create({})
        )
        wizard.process()

        # Quantities should have been synced
        self.assertNotEqual(po_picking_id, so_picking_id)
        self.assertEqual(
            po_picking_id.move_ids.quantity,
            so_picking_id.move_ids.quantity,
        )

        # Check picking state
        self.assertEqual(po_picking_id.state, so_picking_id.state)

        # A backorder should have been made for both
        self.assertTrue(len(sale.picking_ids) > 1)
        self.assertEqual(len(purchase.picking_ids), len(sale.picking_ids))

    def test_confirm_several_picking(self):
        """
        Ensure that confirming several picking is not broken
        """
        purchase_1 = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase_2 = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        sale_1 = self._approve_po(purchase_1)
        sale_2 = self._approve_po(purchase_2)
        pickings = sale_1.picking_ids | sale_2.picking_ids
        for move in pickings.move_ids:
            move.quantity = move.product_uom_qty
        pickings.button_validate()
        self.assertEqual(pickings.mapped("state"), ["done", "done"])

    def test_sync_picking_no_backorder(self):
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True

        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        sale = self._approve_po(purchase)

        self.assertTrue(purchase.picking_ids)
        self.assertTrue(sale.picking_ids)

        po_picking_id = purchase.picking_ids
        so_picking_id = sale.picking_ids

        # check po_picking state
        self.assertEqual(po_picking_id.state, "waiting")

        # validate the SO picking
        so_picking_id.move_ids.quantity = 2

        self.assertNotEqual(po_picking_id, so_picking_id)
        self.assertNotEqual(
            po_picking_id.move_ids.quantity,
            so_picking_id.move_ids.quantity,
        )
        self.assertEqual(
            po_picking_id.move_ids.product_qty,
            so_picking_id.move_ids.product_qty,
        )

        # No backorder
        wizard_data = so_picking_id.with_user(self.user_company_b).button_validate()
        wizard = (
            self.env["stock.backorder.confirmation"]
            .with_context(**wizard_data.get("context"))
            .create({})
        )
        wizard.with_user(self.user_company_b).process_cancel_backorder()
        self.assertEqual(so_picking_id.state, "done")
        self.assertEqual(po_picking_id.state, "done")

        # Quantity done should be the same on both sides, per product
        self.assertNotEqual(po_picking_id, so_picking_id)
        for product in so_picking_id.move_ids.mapped("product_id"):
            self.assertEqual(
                sum(
                    so_picking_id.move_ids.filtered(
                        lambda line, product=product: line.product_id == product
                    ).mapped("quantity")
                ),
                sum(
                    po_picking_id.move_ids.filtered(
                        lambda line, product=product: line.product_id == product
                    ).mapped("quantity")
                ),
            )

        # No backorder should have been made for both
        self.assertEqual(len(sale.picking_ids), 1)
        self.assertEqual(len(purchase.picking_ids), len(sale.picking_ids))

    def test_sync_picking_lot(self):
        """
        Test that the lot is synchronized on the moves
        by searching or creating a new lot in the company of destination
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True

        purchase = self._create_purchase_order(
            self.partner_company_b, self.stockable_product_serial
        )
        sale = self._approve_po(purchase)

        # validate the SO picking
        po_picking_id = purchase.picking_ids
        so_picking_id = sale.picking_ids

        so_move = so_picking_id.move_ids
        so_move.move_line_ids = [
            Command.clear(),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_1.id,
                    "picking_id": so_picking_id.id,
                },
            ),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_2.id,
                    "picking_id": so_picking_id.id,
                },
            ),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_3.id,
                    "picking_id": so_picking_id.id,
                },
            ),
        ]
        so_picking_id.button_validate()

        so_lots = so_move.mapped("move_line_ids.lot_id")
        po_lots = po_picking_id.mapped("move_ids.move_line_ids.lot_id")
        self.assertEqual(
            len(so_lots),
            len(po_lots),
            msg="There aren't the same number of lots on both moves",
        )
        self.assertEqual(
            so_lots, po_lots, msg="The lots of the moves should be the same"
        )
        self.assertEqual(
            so_lots.mapped("name"),
            po_lots.mapped("name"),
            msg="The lots should have the same name in both moves",
        )
        self.assertFalse(so_lots.company_id, msg="Lots should not have a company.")
        # create a new lot in the picking done
        move_line_vals = so_move._prepare_move_line_vals()
        move_line_vals.update({"lot_id": self.serial_4.id, "quantity": 1})
        new_move_line = self.env["stock.move.line"].create(move_line_vals)
        self.assertIn(
            self.serial_4.name,
            po_picking_id.mapped("move_ids.move_line_ids.lot_id.name"),
        )
        # change the lot in the picking done
        new_move_line.lot_id = self.serial_5
        self.assertIn(
            self.serial_5.name,
            po_picking_id.mapped("move_ids.move_line_ids.lot_id.name"),
        )
        self.assertNotIn(
            self.serial_4.name,
            po_picking_id.mapped("move_ids.move_line_ids.lot_id.name"),
        )

    def test_sync_picking_lot_with_transit_location(self):
        """
        Test that the lot is synchronized on the moves
        when using inter-company transit locations
        company B: Sale picking from Stock to Transit Location
        company A: Purchase picking from Transit Location to Stock
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        # Set inter-company locations on partners
        interco_location = self.env.ref("stock.stock_location_inter_company")
        self.partner_company_b.with_company(self.company_a).write(
            {
                "property_stock_customer": interco_location.id,
                "property_stock_supplier": interco_location.id,
            }
        )
        self.partner_company_a.with_company(self.company_b).write(
            {
                "property_stock_customer": interco_location.id,
                "property_stock_supplier": interco_location.id,
            }
        )

        purchase = self._create_purchase_order(
            self.partner_company_b, self.stockable_product_serial
        )
        sale = self._approve_po(purchase)

        # validate the SO picking
        po_picking_id = purchase.picking_ids
        so_picking_id = sale.picking_ids

        so_move = so_picking_id.move_ids
        so_move.move_line_ids = [
            Command.clear(),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_1.id,
                    "picking_id": so_picking_id.id,
                },
            ),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_2.id,
                    "picking_id": so_picking_id.id,
                },
            ),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_3.id,
                    "picking_id": so_picking_id.id,
                },
            ),
        ]
        so_picking_id.button_validate()
        self.assertEqual(so_picking_id.location_id.usage, "internal")
        self.assertEqual(so_picking_id.location_dest_id.usage, "transit")
        self.assertEqual(po_picking_id.location_id.usage, "transit")
        self.assertEqual(po_picking_id.location_dest_id.usage, "internal")

        so_lots = so_move.mapped("move_line_ids.lot_id")
        po_lots = po_picking_id.mapped("move_ids.move_line_ids.lot_id")
        self.assertEqual(
            len(so_lots),
            len(po_lots),
            msg="There aren't the same number of lots on both moves",
        )
        self.assertEqual(
            so_lots, po_lots, msg="The lots of the moves should be the same"
        )
        self.assertEqual(
            so_lots.mapped("name"),
            po_lots.mapped("name"),
            msg="The lots should have the same name in both moves",
        )
        self.assertFalse(so_lots.company_id, msg="Lots should not have a company.")

    def test_sync_picking_same_product_multiple_lines(self):
        """
        Picking synchronization should work even when there
        are multiple lines of the same product in the PO/SO/picking
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        self.company_b.sale_auto_validation = False

        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.order_line += purchase.order_line.copy({"product_qty": 2})
        sale = self._approve_po(purchase)
        sale.action_confirm()

        # validate the SO picking
        po_picking_id = purchase.picking_ids
        so_picking_id = sale.picking_ids

        # Set quantities done on the picking and validate
        for move in so_picking_id.move_ids:
            move.quantity = move.product_uom_qty
        so_picking_id.button_validate()

        self.assertEqual(
            po_picking_id.mapped("move_ids.quantity"),
            so_picking_id.mapped("move_ids.quantity"),
            msg="The quantities are not the same in both pickings.",
        )

    def test_block_manual_validation(self):
        """
        Test that the manual validation of the picking is blocked
        when the flag is set in the destination company
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        self.company_a.block_po_manual_picking_validation = True
        self.company_b.block_po_manual_picking_validation = True
        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.button_confirm()
        po_picking_id = purchase.picking_ids
        # The picking should be in waiting state
        self.assertEqual(po_picking_id.state, "waiting")
        # The manual validation should be blocked
        with self.assertRaisesRegex(
            UserError, "Manual validation of the picking is not allowed"
        ):
            po_picking_id.with_user(self.user_company_a).button_validate()

    def test_notify_picking_problem(self):
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        self.company_b.sale_auto_validation = False
        self.company_a.sync_picking_failure_action = "notify"
        self.company_b.sync_picking_failure_action = "notify"
        self.company_a.notify_user_id = self.user_company_a
        self.company_b.notify_user_id = self.user_company_b

        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase_2 = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.order_line += purchase.order_line.copy({"product_qty": 2})
        sale = self._approve_po(purchase)
        sale.action_confirm()

        # validate the SO picking
        so_picking_id = sale.picking_ids

        # Link to a new purchase order so it can trigger
        # `PO does not exist or has no receipts` in _sync_receipt_with_delivery
        sale.auto_purchase_order_id = purchase_2

        # Set quantities done on the picking and validate
        for move in so_picking_id.move_ids:
            move.quantity = move.product_uom_qty
        so_picking_id.button_validate()

        # Test that picking has an activity now
        self.assertTrue(len(so_picking_id.activity_ids) > 0)
        activity_warning = self.env.ref("mail.mail_activity_data_warning")
        warning_activity = so_picking_id.activity_ids.filtered(
            lambda a: a.activity_type_id == activity_warning
        )
        self.assertEqual(len(warning_activity), 1)

        # Test the user assigned to the activity
        self.assertEqual(
            warning_activity.user_id, so_picking_id.company_id.notify_user_id
        )

    def test_raise_picking_problem(self):
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        self.company_b.sale_auto_validation = False
        self.company_a.sync_picking_failure_action = "raise"
        self.company_b.sync_picking_failure_action = "raise"

        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase_2 = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.order_line += purchase.order_line.copy({"product_qty": 2})
        sale = self._approve_po(purchase)
        sale.action_confirm()

        # validate the SO picking
        so_picking_id = sale.picking_ids

        # Link to a new purchase order so it can trigger
        # `PO does not exist or has no receipts` in _sync_receipt_with_delivery
        sale.auto_purchase_order_id = purchase_2

        # Set quantities done on the picking and validate
        for move in so_picking_id.move_ids:
            move.quantity = move.product_uom_qty
        # no pending destination receipt picking exists to mirror into.
        with self.assertRaisesRegex(
            UserError, "No pending receipt picking found for PO"
        ):
            so_picking_id.button_validate()

    def test_sync_picking_multi_step(self):
        self.company_a.sync_picking = True
        self.warehouse_a.reception_steps = "two_steps"
        self.company_b.sync_picking = True
        self.warehouse_c.delivery_steps = "pick_ship"

        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        sale = self._approve_po(purchase)

        self.assertEqual(len(purchase.picking_ids), 1)
        # Only a single picking is created for the sale.
        # When this picking is validated, two pickings should be created:
        # one for the backorder and one for the delivery.
        self.assertEqual(len(sale.picking_ids), 1)
        # validate the SO internal picking
        so_internal_pick = sale.picking_ids

        so_internal_pick.move_ids.quantity = 2
        so_internal_pick.move_ids.picked = True
        wizard_data = so_internal_pick.with_user(self.user_company_b).button_validate()
        wizard = (
            self.env["stock.backorder.confirmation"]
            .with_context(**wizard_data.get("context"))
            .create({})
        )
        wizard.process()
        po_picking = purchase.picking_ids
        # check po_picking state
        self.assertEqual(po_picking.state, "waiting")

        # validate the SO picking
        so_picking = sale.picking_ids.filtered(
            lambda x: x.location_dest_id.usage == "customer"
        )
        so_picking.move_ids.quantity = 2
        so_picking.move_ids.picked = True
        self.assertNotEqual(po_picking, so_picking)
        self.assertNotEqual(
            po_picking.move_ids.quantity,
            so_picking.move_ids.quantity,
        )

        so_picking.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_picking.state, "done")
        po_internal_pick = po_picking.move_ids.move_dest_ids.picking_id
        # the move in the receipt should have a "next move" due to "two_steps"
        self.assertTrue(purchase.picking_ids.move_ids.move_dest_ids)
        self.assertEqual(len(sale.picking_ids), 3)  # Pick + Backorder + Delivery
        self.assertTrue(
            all((po_picking, po_internal_pick, so_picking, so_internal_pick))
        )
        # Quantities should have been synced
        self.assertNotEqual(po_picking, so_picking)
        self.assertEqual(
            po_picking.move_ids.quantity,
            so_picking.move_ids.quantity,
        )

        # Check picking state
        self.assertEqual(po_picking.state, so_picking.state)

        # An additional receipt should have been created for the PO
        self.assertEqual(len(purchase.picking_ids), 2)
        done_purchase_picking = purchase.picking_ids.filtered(
            lambda x: x.state == "done"
        )
        self.assertEqual(len(done_purchase_picking), 1)
        self.assertEqual(done_purchase_picking, po_picking)
        new_receipt_picking = done_purchase_picking._get_next_transfers()
        self.assertEqual(len(new_receipt_picking), 1)
        self.assertEqual(new_receipt_picking.state, "assigned")

    def test_sync_picking_multi_step_with_transit(self):
        """
        Test that the lot is synchronized on the moves
        when using inter-company transit locations
        and warehouses are configured with multi-step routes.
        company B: Sale picking
            Picking 1: from Stock to Packing
            Picking 2: from Packing to Transit Location
        company A: Purchase picking
            Picking 1: from Transit Location to Input
            Picking 2: from Input to Stock
        """
        self.company_a.sync_picking = True
        self.warehouse_a.reception_steps = "two_steps"
        self.company_b.sync_picking = True
        self.warehouse_c.delivery_steps = "pick_ship"
        # Set inter-company locations on partners
        interco_location = self.env.ref("stock.stock_location_inter_company")
        self.partner_company_b.with_company(self.company_a).write(
            {
                "property_stock_customer": interco_location.id,
                "property_stock_supplier": interco_location.id,
            }
        )
        self.partner_company_a.with_company(self.company_b).write(
            {
                "property_stock_customer": interco_location.id,
                "property_stock_supplier": interco_location.id,
            }
        )
        purchase = self._create_purchase_order(
            self.partner_company_b, self.stockable_product_serial
        )
        sale = self._approve_po(purchase)
        self.assertEqual(len(purchase.picking_ids), 1)
        # Only a single picking is created for the sale.
        # When this picking is validated, two pickings should be created:
        # one for the backorder and one for the delivery.
        self.assertEqual(len(sale.picking_ids), 1)
        # Check the locations
        self.assertEqual(purchase.picking_ids.location_id.usage, "transit")
        self.assertEqual(purchase.picking_ids.location_dest_id.usage, "internal")
        self.assertEqual(sale.picking_ids.location_id.usage, "internal")
        self.assertEqual(sale.picking_ids.location_dest_id.usage, "internal")
        self.assertEqual(sale.picking_ids.move_ids.location_final_id.usage, "transit")
        # validate the SO internal picking
        so_internal_pick = sale.picking_ids
        so_move = so_internal_pick.move_ids
        so_move.move_line_ids = [
            Command.clear(),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_1.id,
                    "picking_id": so_internal_pick.id,
                },
            ),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_2.id,
                    "picking_id": so_internal_pick.id,
                },
            ),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_3.id,
                    "picking_id": so_internal_pick.id,
                },
            ),
        ]
        so_internal_pick.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_internal_pick.state, "done")
        po_picking = purchase.picking_ids
        # check po_picking state
        self.assertEqual(po_picking.state, "waiting")
        # validate the SO picking
        so_picking = sale.picking_ids.filtered(
            lambda x: x.location_dest_id.usage == "customer"
        )
        so_picking.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_picking.state, "done")
        # The location at the picking level is set to Customer by the operation type,
        # but at the move level, it is Transit.
        self.assertEqual(so_picking.location_dest_id.usage, "customer")
        self.assertEqual(so_picking.move_ids.location_dest_id.usage, "transit")
        # the move in the receipt should have a "next move" due to "two_steps"
        self.assertTrue(purchase.picking_ids.move_ids.move_dest_ids)
        self.assertEqual(len(sale.picking_ids), 2)  # Pick + Delivery
        # Quantities should have been synced
        self.assertEqual(
            po_picking.move_ids.quantity,
            so_picking.move_ids.quantity,
        )
        # Check picking state
        self.assertEqual(po_picking.state, "done")
        new_receipt_picking = po_picking._get_next_transfers()
        self.assertEqual(len(new_receipt_picking), 1)
        self.assertEqual(new_receipt_picking.state, "assigned")
        # check the lots
        so_lots = so_move.mapped("move_line_ids.lot_id")
        po_lots = po_picking.mapped("move_ids.move_line_ids.lot_id")
        self.assertEqual(
            so_lots, po_lots, msg="The lots of the moves should be the same"
        )
        self.assertFalse(so_lots.company_id, msg="Lots should not have a company.")

    def test_sync_picking_lot_without_purchase_line_id(self):
        """
        Regression test:
        On the delivery side (SO picking), stock.move.purchase_line_id may be empty.
        Intercompany syncing must still find the destination PO move using the
        SO-PO link (sale_line.auto_purchase_line_id) and mirror lots correctly.
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        purchase = self._create_purchase_order(
            self.partner_company_b, self.stockable_product_serial
        )
        sale = self._approve_po(purchase)
        po_picking = purchase.picking_ids
        so_picking = sale.picking_ids
        so_move = so_picking.move_ids
        self.assertTrue(so_move.sale_line_id)
        self.assertTrue(so_move.sale_line_id.auto_purchase_line_id)
        # Reproduce the real-world issue:
        # outgoing delivery move has no purchase_line_id set.
        so_move.purchase_line_id = False
        self.assertFalse(
            so_move.purchase_line_id,
        )
        # Set serial move lines and validate the SO picking
        so_move.move_line_ids = [
            Command.clear(),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_1.id,
                    "picking_id": so_picking.id,
                },
            ),
            Command.create(
                {
                    "location_id": so_move.location_id.id,
                    "location_dest_id": so_move.location_dest_id.id,
                    "product_id": self.stockable_product_serial.id,
                    "product_uom_id": self.stockable_product_serial.uom_id.id,
                    "quantity": 1,
                    "lot_id": self.serial_2.id,
                    "picking_id": so_picking.id,
                },
            ),
        ]
        so_picking.with_company(so_picking.company_id).action_confirm()
        so_picking.with_company(so_picking.company_id).action_assign()
        so_picking.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_picking.state, "done")
        so_lots = so_move.mapped("move_line_ids.lot_id")
        po_lots = po_picking.mapped("move_ids.move_line_ids.lot_id")
        # lots are mirrored 1:1
        self.assertEqual(
            len(so_lots),
            len(po_lots),
        )
        self.assertEqual(
            so_lots,
            po_lots,
        )
        self.assertEqual(
            so_lots.mapped("name"),
            po_lots.mapped("name"),
        )

    def test_sync_picking_multiple_po_moves_raises(self):
        """
        Safeguard test
        the  product-based fallback finds multiple destination receipt moves,
        we must raise a clear UserError
        This protects cases where the destination picking contains duplicate
        receipt moves for the same product.
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        # Duplicate same product line
        purchase.order_line += purchase.order_line.copy({"product_qty": 1})
        sale = self._approve_po(purchase)
        po_picking = purchase.picking_ids
        so_picking = sale.picking_ids
        so_move = so_picking.move_ids[:1]
        self.assertTrue(so_move)
        # Force to use the product-based fallback.
        # remove the SO->PO link so _get_intercompany_po_move() can't use it.
        sale_line = so_move.sale_line_id
        self.assertTrue(sale_line)
        sale_line.auto_purchase_line_id = False
        self.assertFalse(
            sale_line.auto_purchase_line_id,
        )
        # Ensure there are indeed multiple candidate receipt moves for the same product
        candidates = po_picking.move_ids.filtered(
            lambda m: m.product_id == so_move.product_id
            and m.state not in ["done", "cancel"]
        )
        self.assertTrue(
            len(candidates) > 1,
        )
        # Set done qty so validation actually tries to sync
        for move in so_picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        with self.assertRaisesRegex(
            UserError, "Multiple candidate receipt moves found"
        ):
            so_picking.with_user(self.user_company_b).button_validate()

    def test_sync_picking_partial_delivery_backorder(self):
        """
        Regression test (partial deliveries/backorders)
        When the SO delivery is partially validated (creating a backorder),
        the PO receipt must mirror the same partial quantity and create a matching
        backorder receipt on the PO side (ΝΟΤ receive everything at once).
        Then, validating the SO backorder must complete the PO receipt quantities.
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        self.consumable_product.type = "consu"
        self.partner_company_b.company_id = False
        # Create PO with qty 10 in company A (destination)
        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.order_line.product_qty = 10.0
        # Approve, creates SO in source, company B
        sale = self._approve_po(purchase)
        # We expect one SO picking initially
        so_picking = sale.picking_ids
        self.assertEqual(len(so_picking), 1)
        so_picking = so_picking[0]
        # PO picking exists and should be waiting until sync
        self.assertTrue(purchase.picking_ids)
        self.assertEqual(purchase.picking_ids.state, "waiting")
        # validate SO picking partially and get a backorder
        so_picking.with_company(so_picking.company_id).action_confirm()
        so_picking.move_ids.quantity = 5.0
        so_picking.move_ids.picked = True
        res = so_picking.with_user(self.user_company_b).button_validate()
        # A backorder wizard is hidden in res
        wiz = (
            self.env["stock.backorder.confirmation"]
            .with_context(**res["context"])
            .create({})
        )
        wiz.process()
        dest_pick = so_picking.intercompany_picking_id
        self.assertTrue(dest_pick)
        self.assertEqual(
            dest_pick.intercompany_picking_id,
            so_picking,
        )
        # The PO receipt we mirrored into must be linked back to this SO picking.
        self.assertEqual(dest_pick.intercompany_picking_id, so_picking)
        # After partial validation:
        # PO should now have 2 receipts (done + waiting)
        # received qty should be 5
        self.assertEqual(purchase.order_line.qty_received, 5.0)
        self.assertEqual(len(purchase.picking_ids), 2)
        po_done = purchase.picking_ids.filtered(lambda p: p.state == "done")
        po_open = purchase.picking_ids.filtered(lambda p: p.state == "waiting")
        self.assertEqual(len(po_done), 1)
        self.assertEqual(len(po_open), 1)
        # The mirrored picking is the one that got done by the partial delivery.
        self.assertEqual(dest_pick, po_done)
        # link is stable
        self.assertEqual(so_picking.intercompany_picking_id, po_done)
        self.assertEqual(po_done.intercompany_picking_id, so_picking)
        # Quantities on PO side should split as 5 + 5
        self.assertEqual(po_done.move_line_ids.quantity, 5.0)
        self.assertEqual(po_open.move_line_ids.quantity, 5.0)
        # validate the SO backorder for remaining qty 5
        so_backorder = sale.picking_ids.filtered(lambda p: p.state != "done")
        self.assertEqual(len(so_backorder), 1)
        so_backorder = so_backorder[0]
        so_backorder.with_company(so_backorder.company_id).action_confirm()
        so_backorder.move_ids.quantity = 5.0
        so_backorder.move_ids.picked = True
        so_backorder.with_user(self.user_company_b).button_validate()
        # After validation, the SO backorder must be linked to the PO backorder receipt
        # TRIUMPH
        self.assertEqual(
            so_backorder.intercompany_picking_id,
            po_open,
        )
        # PO fully received and both PO pickings done -  TRIUMPH
        self.assertEqual(purchase.order_line.qty_received, 10.0)
        self.assertTrue(all(p.state == "done" for p in purchase.picking_ids))
        self.assertEqual(
            sorted(purchase.picking_ids.mapped("move_line_ids.quantity")), [5.0, 5.0]
        )

    def test_sync_picking_return_mirroring_basic(self):
        """
        Regression test:
        A return validated on one side of an intercompany delivery
        must create and validate the corresponding return on the other side,
        linking both return pickings via intercompany_picking_id.
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        self.company_b.sale_auto_validation = False
        # Create intercompany PO to SO
        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.order_line.product_qty = 2.0
        sale = self._approve_po(purchase)
        if sale.state in ("draft", "sent"):
            sale.action_confirm()
        so_picking = sale.picking_ids.filtered(
            lambda p: p.picking_type_id.code == "outgoing"
        )
        self.assertEqual(len(so_picking), 1)
        so_picking = so_picking[0]
        # Deliver full quantity (2)
        so_picking.action_confirm()
        for move in so_picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        so_picking.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_picking.state, "done")
        # delivery should be linked to a PO receipt
        po_receipt = so_picking.intercompany_picking_id
        self.assertTrue(po_receipt)
        self.assertEqual(po_receipt.intercompany_picking_id, so_picking)
        # Create a return from the SO picking for qty 1
        wiz = (
            self.env["stock.return.picking"]
            .with_context(
                active_id=so_picking.id,
                active_ids=so_picking.ids,
                active_model="stock.picking",
            )
            .create({})
        )
        wiz.product_return_moves.quantity = 1.0
        # Grab the picking
        so_return = wiz._create_return()
        self.assertTrue(so_return)
        self.assertEqual(so_return._name, "stock.picking")
        self.assertEqual(len(so_return), 1)
        self.assertNotEqual(so_return.id, so_picking.id)
        so_return = so_return[0]
        self.assertTrue(so_return)
        self.assertNotEqual(so_return, so_picking)
        # Validate the return picking
        so_return.action_confirm()
        for move in so_return.move_ids:
            move.quantity = move.product_uom_qty  # should be 1
            move.picked = True
        so_return.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_return.state, "done")
        # destination company got a mirrored return; YES
        dest_return = so_return.intercompany_picking_id
        self.assertTrue(dest_return, "Return picking must be mirrored and linked.")
        self.assertEqual(dest_return.intercompany_picking_id, so_return)
        self.assertEqual(dest_return.state, "done")
        self.assertEqual(sum(dest_return.move_ids.mapped("quantity")), 1.0)

    def test_sync_picking_return_mirroring_idempotent(self):
        """
        Safeguard test:
        Mirroring a return must be idempotent.
        If workflow allows for multiple calls
        to _mirror_intercompany_return and _action_done,
        we must avoid creating a second destination return picking,
        and respect current linkage
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        self.company_b.sale_auto_validation = False
        # Create intercompany PO to SO
        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.order_line.product_qty = 2.0
        sale = self._approve_po(purchase)
        if sale.state in ("draft", "sent"):
            sale.action_confirm()
        so_picking = sale.picking_ids.filtered(
            lambda p: p.picking_type_id.code == "outgoing"
        )
        self.assertEqual(len(so_picking), 1)
        so_picking = so_picking[0]
        # Deliver full qty (2)
        so_picking.action_confirm()
        for move in so_picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        so_picking.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_picking.state, "done")
        # delivery linked to PO receipt
        po_receipt = so_picking.intercompany_picking_id
        self.assertTrue(po_receipt)
        self.assertEqual(po_receipt.intercompany_picking_id, so_picking)
        # Create SO return qty 1
        wiz = (
            self.env["stock.return.picking"]
            .with_context(
                active_id=so_picking.id,
                active_ids=so_picking.ids,
                active_model="stock.picking",
            )
            .create({})
        )
        wiz.product_return_moves.quantity = 1.0
        so_return = wiz._create_return()
        self.assertTrue(so_return)
        self.assertEqual(len(so_return), 1)
        so_return = so_return[0]
        # Validate return triggers mirroring in _action_done
        so_return.action_confirm()
        for move in so_return.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        so_return.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_return.state, "done")
        # Mirror must exist
        dest_return = so_return.intercompany_picking_id
        self.assertTrue(dest_return)
        self.assertEqual(dest_return.intercompany_picking_id, so_return)
        # Count how many destination returns point back to this SO return
        dest_company = dest_return.company_id
        linked_before = (
            self.env["stock.picking"]
            .with_company(dest_company)
            .search_count(
                [
                    ("company_id", "=", dest_company.id),
                    ("intercompany_picking_id", "=", so_return.id),
                ]
            )
        )
        self.assertEqual(linked_before, 1)
        # Call mirror again, nothing should happen
        so_return.sudo()._mirror_intercompany_return()
        # Link must remain the same and no extra destination return created
        self.assertEqual(so_return.intercompany_picking_id, dest_return)
        self.assertEqual(dest_return.intercompany_picking_id, so_return)
        linked_after = (
            self.env["stock.picking"]
            .with_company(dest_company)
            .search_count(
                [
                    ("company_id", "=", dest_company.id),
                    ("intercompany_picking_id", "=", so_return.id),
                ]
            )
        )
        self.assertEqual(linked_after, 1)

    def test_sync_picking_return_mirroring_partial_with_backorder(self):
        """
        Regression test (returns with backorder):
        If an intercompany return is validated partially
        (creating a return backorder),
        each return picking must be mirrored  1 to 1 to the other company
        with matching qty, and linked via intercompany_picking_id.
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        self.company_b.sale_auto_validation = False  # keep flows explicit
        self.partner_company_b.company_id = False
        self.consumable_product.type = "consu"
        # Create intercompany PO to SO for qty 5 and deliver it fully
        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.order_line.product_qty = 5.0
        sale = self._approve_po(purchase)
        if sale.state in ("draft", "sent"):
            sale.action_confirm()
        so_delivery = sale.picking_ids.filtered(
            lambda p: p.picking_type_id.code == "outgoing"
        )
        self.assertEqual(len(so_delivery), 1)
        so_delivery = so_delivery[0]
        so_delivery.action_confirm()
        for move in so_delivery.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        so_delivery.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_delivery.state, "done")
        # delivery is linked to a PO receipt
        po_receipt = so_delivery.intercompany_picking_id
        self.assertTrue(po_receipt)
        self.assertEqual(po_receipt.intercompany_picking_id, so_delivery)
        # Create a return for full qty 5 from the SO delivery picking
        wiz = (
            self.env["stock.return.picking"]
            .with_context(
                active_id=so_delivery.id,
                active_ids=so_delivery.ids,
                active_model="stock.picking",
            )
            .create({})
        )
        # return everything so that partial validation creates a backorder
        for line in wiz.product_return_moves:
            line.quantity = 5.0
        so_return = wiz._create_return()
        self.assertTrue(so_return)
        self.assertEqual(len(so_return), 1)
        so_return = so_return[0]
        # validate the return partially (qty 2), get a return backorder
        so_return.action_confirm()
        for move in so_return.move_ids:
            move.quantity = 2.0
            move.picked = True
        res = so_return.with_user(self.user_company_b).button_validate()
        wiz_bo = (
            self.env["stock.backorder.confirmation"]
            .with_context(**res["context"])
            .create({})
        )
        wiz_bo.process()
        self.assertEqual(so_return.state, "done")
        # (mirrored) return 1 must exist and be done with qty 2
        dest_return_1 = so_return.intercompany_picking_id
        self.assertTrue(dest_return_1)
        self.assertEqual(dest_return_1.intercompany_picking_id, so_return)
        self.assertEqual(dest_return_1.state, "done")
        self.assertEqual(sum(dest_return_1.move_ids.mapped("quantity")), 2.0)
        # validate the SO return backorder (remaining qty 3)
        so_return_bo = sale.picking_ids.filtered(lambda p: p.state != "done")
        # find backorder picking from backorder_id
        so_return_bo = self.env["stock.picking"].search(
            [("backorder_id", "=", so_return.id), ("state", "!=", "done")],
            limit=1,
        )
        self.assertTrue(so_return_bo)
        so_return_bo.action_confirm()
        for move in so_return_bo.move_ids:
            move.quantity = move.product_uom_qty  # should be 3
            move.picked = True
        so_return_bo.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_return_bo.state, "done")
        # return 2 must exist and be done with qty 3
        dest_return_2 = so_return_bo.intercompany_picking_id
        self.assertTrue(dest_return_2)
        self.assertEqual(dest_return_2.intercompany_picking_id, so_return_bo)
        self.assertEqual(dest_return_2.state, "done")
        self.assertEqual(sum(dest_return_2.move_ids.mapped("quantity")), 3.0)
        # Both mirrored return pickings must be distinct
        self.assertNotEqual(dest_return_1.id, dest_return_2.id)

    def test_sync_picking_return_mirroring_multi_step_with_transit_backorder(self):
        """
        Regression test (returns + multi-step + transit + backorder):
        In intercompany flows using transit locations and multi-step routes,
        a partial return (creating a backorder) must be mirrored to the other company,
        and the return backorder must also be mirrored and linked 1 to 1 via
        intercompany_picking_id.
        """
        self.company_a.sync_picking = True
        self.company_b.sync_picking = True
        # Multi-step + transit setup
        self.warehouse_a.reception_steps = "two_steps"
        self.warehouse_c.delivery_steps = "pick_ship"
        interco_location = self.env.ref("stock.stock_location_inter_company")
        self.partner_company_b.with_company(self.company_a).write(
            {
                "property_stock_customer": interco_location.id,
                "property_stock_supplier": interco_location.id,
            }
        )
        self.partner_company_a.with_company(self.company_b).write(
            {
                "property_stock_customer": interco_location.id,
                "property_stock_supplier": interco_location.id,
            }
        )
        # Create intecompany PO to SO with qty 2
        purchase = self._create_purchase_order(
            self.partner_company_b, self.consumable_product
        )
        purchase.order_line.product_qty = 2.0
        sale = self._approve_po(purchase)
        # Validate SO flow fully so we have a done customer delivery picking
        if sale.state in ("draft", "sent"):
            sale.action_confirm()
        # In pick/ship, the outgoing delivery is typically created only AFTER
        # validating the internal picking. Validate internal first, then fetch
        # the next transfer (outgoing).
        so_pick = sale.picking_ids.filtered(
            lambda p: p.picking_type_id.code == "internal"
        )
        self.assertEqual(len(so_pick), 1)
        so_pick = so_pick[0]
        so_pick.action_confirm()
        for move in so_pick.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        so_pick.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_pick.state, "done")
        so_delivery_pick = so_pick._get_next_transfers()
        self.assertEqual(len(so_delivery_pick), 1)
        so_delivery_pick = so_delivery_pick[0]
        so_delivery_pick.action_confirm()
        for move in so_delivery_pick.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        so_delivery_pick.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_delivery_pick.state, "done")
        # Create RETURN with partial quantity 1, get a return backorder
        wiz = (
            self.env["stock.return.picking"]
            .with_context(
                active_id=so_delivery_pick.id,
                active_ids=so_delivery_pick.ids,
                active_model="stock.picking",
            )
            .create({})
        )
        # Create a return for the FULL qty (2).
        # The backorder is created by validating the return picking partially.
        wiz.product_return_moves.quantity = 2.0
        so_return_1 = wiz._create_return()
        self.assertTrue(so_return_1)
        so_return_1 = so_return_1[:1]
        so_return_1.action_confirm()
        for move in so_return_1.move_ids:
            # Validate only 1 out of 2 to force a return backorder
            move.quantity = 1.0
            move.picked = True
        res = so_return_1.with_user(self.user_company_b).button_validate()
        # there's a backorder wizard lurking in context
        bwiz = (
            self.env["stock.backorder.confirmation"]
            .with_context(**res["context"])
            .create({})
        )
        bwiz.process()
        self.assertEqual(so_return_1.state, "done")
        # Assert mirrored return exists and is linked
        dest_return_1 = so_return_1.intercompany_picking_id
        self.assertTrue(dest_return_1)
        self.assertEqual(dest_return_1.intercompany_picking_id, so_return_1)
        self.assertEqual(dest_return_1.state, "done")
        self.assertEqual(sum(dest_return_1.move_ids.mapped("quantity")), 1.0)
        # -Validate RETURN backorder (remaining qty 1)
        # Find the return backorder via backorder link
        so_return_back = self.env["stock.picking"].search(
            [
                ("backorder_id", "=", so_return_1.id),
                ("state", "!=", "done"),
            ],
            limit=1,
        )
        self.assertTrue(so_return_back)
        so_return_back.action_confirm()
        for move in so_return_back.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        so_return_back.with_user(self.user_company_b).button_validate()
        self.assertEqual(so_return_back.state, "done")
        # Assert mirrored return backorder exists and is linked
        dest_return_back = so_return_back.intercompany_picking_id
        self.assertTrue(dest_return_back)
        self.assertEqual(dest_return_back.intercompany_picking_id, so_return_back)
        self.assertEqual(dest_return_back.state, "done")
        self.assertEqual(sum(dest_return_back.move_ids.mapped("quantity")), 1.0)
