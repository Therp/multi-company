# Copyright 2026 Tecnativa - Carlos Lopez

from odoo import Command, models


class ReturnPicking(models.TransientModel):
    _inherit = "stock.return.picking"

    def _create_return(self):
        new_picking = super()._create_return()
        if self._should_create_intercompany_return():
            return_wizard = self._create_intercompany_return_wizard(new_picking)
            intercompany_return = return_wizard.with_context(
                skip_create_returns=True
            )._create_return()
            # Link both returns right away so that validating either side syncs
            # into the right counterpart (see stock.picking._action_done).
            new_picking.sudo().intercompany_picking_id = intercompany_return
            intercompany_return.sudo().intercompany_picking_id = new_picking
        return new_picking

    def _should_create_intercompany_return(self):
        return self.picking_id._is_intercompany_delivery() and not self.env.context.get(
            "skip_create_returns", False
        )

    def _create_intercompany_return_wizard(self, new_picking):
        intercompany_picking = self.picking_id.intercompany_picking_id
        dest_company = intercompany_picking.sudo().company_id
        intercompany_user = dest_company.intercompany_sale_user_id
        vals = {"picking_id": intercompany_picking.id}
        # Work in the destination company only: the current user's allowed
        # companies must not leak into the intercompany user's environment.
        Wizard = self.env["stock.return.picking"].with_context(
            active_id=intercompany_picking.id,
            active_ids=intercompany_picking.ids,
            active_model="stock.picking",
            allowed_company_ids=dest_company.ids,
        )
        if intercompany_user:
            Wizard = Wizard.with_user(intercompany_user)
        else:
            Wizard = Wizard.sudo()
        return_wizard = Wizard.with_company(dest_company).create(vals)
        # stock_picking_return_lot compatibility
        if "lot_id" in return_wizard.product_return_moves._fields:
            exclude_moves = return_wizard.product_return_moves.filtered(
                lambda line: line.lot_id not in self.product_return_moves.lot_id
            )
        else:
            exclude_moves = return_wizard.product_return_moves.filtered(
                lambda line: line.product_id not in self.product_return_moves.product_id
            )
        return_wizard.product_return_moves = [
            Command.unlink(prm.id) for prm in exclude_moves
        ]
        for wizard_line in self.product_return_moves:
            dest_line = return_wizard.product_return_moves.filtered(
                lambda line, wizard_line=wizard_line: line.product_id
                == wizard_line.product_id
            )
            # stock_picking_return_lot compatibility
            if "lot_id" in wizard_line._fields:
                dest_line = dest_line.filtered(
                    lambda x, wizard_line=wizard_line: x.lot_id == wizard_line.lot_id
                )
            dest_line.write({"quantity": wizard_line.quantity})
        return return_wizard
