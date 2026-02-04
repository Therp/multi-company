# Copyright 2018 Tecnativa - Carlos Dauden
# Copyright 2018 Tecnativa - Pedro M. Baeza
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import logging

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    intercompany_picking_id = fields.Many2one(comodel_name="stock.picking", copy=False)
    # to silence the warning
    # Field stock.picking.state should be declared with recursive=True
    state = fields.Selection(recursive=True)

    @api.depends("intercompany_picking_id.move_ids.state")
    def _compute_state(self):
        res = super()._compute_state()
        # If the picking is inter-company, it's an 'incoming'
        # type of picking, and it has not been validated nor canceled
        # we compute it's state based on the other picking state
        for picking in self.filtered(
            lambda pick: pick._is_intercompany_reception()
            and pick.state not in ["done", "cancel"]
        ):
            # intercompany_picking_id is set when the picking is validated
            # meanwhile, the state should remain in 'waiting'
            if not picking.intercompany_picking_id:
                picking.state = "waiting"
            elif picking.intercompany_picking_id.state not in ["done", "cancel"]:
                if picking.intercompany_picking_id.state in ["confirmed", "assigned"]:
                    picking.state = "waiting"
                else:
                    picking.state = picking.intercompany_picking_id.state
        return res

    def button_validate(self):
        # if the flag is set,
        # block the validation of the picking in the destination company
        if self.filtered(
            lambda picking: picking.company_id.block_po_manual_picking_validation
            and picking._is_intercompany_reception()
            and picking.state in ["done", "waiting", "assigned"]
        ):
            raise UserError(
                _(
                    "Manual validation of the picking is not allowed"
                    " in the destination company."
                )
            )
        return super().button_validate()

    def _action_done(self):
        res = super()._action_done()
        # Only DropShip pickings
        for picking in self.filtered(lambda pick: pick._is_intercompany_delivery()):
            purchase = picking.sale_id.sudo().auto_purchase_order_id
            picking.sudo()._action_done_intercompany_actions(purchase)
        # Intercompany return mirroring
        # add context key to beat looping
        if not self.env.context.get("skip_intercompany_return_mirror"):
            for picking in self.filtered(lambda pick: pick._is_intercompany_return()):
                picking.sudo()._mirror_intercompany_return()
        return res

    def _is_intercompany_return(self):
        """
        Identify return pickings that belong to an intercompany flow.
        It is definined by a non-falsy origin_returned_move_id.
        Mirror it only if the original picking was intercompany
        """
        self.ensure_one()
        # already linked, do not link again
        if self.intercompany_picking_id:
            return False
        # Mirror if a single origin exists. If multiple, then problem
        return bool(self._get_intercompany_return_origin_picking())

    def _get_intercompany_return_origin_picking(self):
        """
        Return the single origin picking that makes this return an intercompany return.
        A return picking may include moves that originate from multiple pickings.
        We only mirror when we can identify exactly ONE intercompany origin picking,
        i.e., exactly one origin picking has intercompany_picking_id set.
        """
        self.ensure_one()
        origin_pickings = self.move_ids.mapped("origin_returned_move_id.picking_id")
        intercompany_origins = origin_pickings.filtered("intercompany_picking_id")
        if len(intercompany_origins) != 1:
            if intercompany_origins:
                _logger.warning(
                    "Intercompany return mirroring skipped for picking %s: "
                    "multiple intercompany origin pickings found: %s",
                    self.name,
                    ", ".join(intercompany_origins.mapped("name")),
                )
            return self.browse()
        return intercompany_origins

    def _mirror_intercompany_return(self):
        """
        Mirror a validated return to the other company by creating and validating
        a corresponding return on the linked intercompany picking.
        Utilize intercompany_picking_id for the link.
        """
        self.ensure_one()
        # Do nothing if mirrored, silently
        if self.intercompany_picking_id:
            return
        # Find original picking being returned
        origin_picking = self._get_intercompany_return_origin_picking()
        if not origin_picking:
            return
        # if destination already has a return linked back to us, do nothing.
        existing_dest = self.env["stock.picking"].search(
            [("intercompany_picking_id", "=", self.id)],
            limit=1,
        )
        if existing_dest:
            _logger.info(
                "Intercompany return already mirrored for picking %s -> %s",
                self.name,
                existing_dest.name,
            )
            self.intercompany_picking_id = existing_dest
            return
        if not origin_picking.intercompany_picking_id:
            return
        dest_origin = origin_picking.intercompany_picking_id
        dest_company = dest_origin.company_id
        intercompany_user = dest_company.intercompany_sale_user_id
        # aggregate q by product, take move line quantities
        # as they are expected to be validated, fallback
        # to move.quantity
        qty_by_product = {}
        for move in self.move_ids:
            line_qty = sum(move.move_line_ids.mapped("quantity"))
            qty = line_qty if move.move_line_ids else move.quantity
            qty_by_product[move.product_id] = (
                qty_by_product.get(move.product_id, 0.0) + qty
            )
        if not qty_by_product:
            _logger.warning(
                "Intercompany return mirroring skipped"
                " for picking %s: no quantities found",
                self.name,
            )
            return
        # Launch return wizard on destination picking
        wiz = (
            self.env["stock.return.picking"]
            .with_user(intercompany_user)
            .with_company(dest_company)
            .with_context(
                active_id=dest_origin.id,
                active_ids=dest_origin.ids,
                active_model="stock.picking",
            )
            .create({})
        )
        # Configure return quantities
        for line in wiz.product_return_moves:
            line.quantity = qty_by_product.get(line.product_id, 0.0)
        dest_return = wiz._create_return()
        # Link both return pickings
        self.intercompany_picking_id = dest_return
        dest_return.intercompany_picking_id = self.id
        # Validate destination return without triggering mirror-back
        dest_return.action_confirm()
        for move in dest_return.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        dest_return.with_context(skip_intercompany_return_mirror=True).button_validate()

    def _get_product_intercompany_qty_done_dict(self, sale_move_lines, po_move_lines):
        """
        Get the total quantity done
        for the given sale move lines and purchase move lines.
        This is used to update the purchase order with the quantities
        received in the inter-company picking.
        :param sale_move_lines:
            browse_record(stock.move.line) Sale move lines to consider
        :param po_move_lines:
            browse_record(stock.move.line) Purchase move lines to consider
        :return: dict with product as key and total quantity done as value
        """
        product = po_move_lines.product_id
        quantity = sum(sale_move_lines.mapped("quantity"))
        res = {product: quantity}
        return res

    def _action_done_intercompany_actions(self, purchase):
        self.ensure_one()
        try:
            dest_company = purchase.company_id
            intercompany_user = dest_company.intercompany_sale_user_id
            dest_pick = self._get_intercompany_destination_picking(purchase)
            if not dest_pick:
                raise UserError(
                    _(
                        "No pending receipt picking found "
                        "for PO %(po)s to mirror %(pick)s"
                    )
                    % {"po": purchase.name, "pick": self.name}
                )
            # Pin the relationship both ways so future syncs are stable
            self.intercompany_picking_id = dest_pick
            dest_pick.intercompany_picking_id = self.id
            dest_picking = dest_pick.with_user(intercompany_user).with_company(
                dest_company
            )
            for move in self.move_ids:
                move_lines = move.move_line_ids.filtered(lambda x: x.quantity > 0)
                po_move_pending = self._get_intercompany_po_move(move, dest_picking)
                po_move_lines = po_move_pending.move_line_ids
                # Don’t raise an error
                # if there are no move_line_ids and the location is transit.
                # In vendor locations, reservations are bypassed,
                # but in transit locations,
                # we need to create the move lines to assign lots/serials.
                if not po_move_pending or (
                    not po_move_lines and move.location_dest_id.usage != "transit"
                ):
                    raise UserError(
                        _(
                            "There's no corresponding line in PO %(po)s for assigning "
                            "qty from %(pick_name)s for product %(product)s"
                        )
                        % (
                            {
                                "po": purchase.name,
                                "pick_name": self.name,
                                "product": move.product_id.display_name,
                            }
                        )
                    )
                move_line_diff = len(move_lines) - len(po_move_lines)
                # generate new move lines if needed
                # example: In purchase order of C1, we have 2 move lines
                # and in reception of C2,
                # we have 3 move lines(with lot or serial number)
                # then we need to create 1 more move line in purchase order of C1
                if move_line_diff > 0:
                    new_move_line_vals = []
                    for _index in range(move_line_diff):
                        vals = po_move_pending._prepare_move_line_vals()
                        new_move_line_vals.append(vals)
                    po_move_lines |= po_move_lines.create(new_move_line_vals)
                elif move_line_diff < 0:
                    # remove the extra move lines in the receipt of lot tracking product
                    # example:
                    # In the receipt, we have 3 move lines for 3 different serials,
                    # in the delivery we specify 2 serials.
                    # When validating the delivery and creating back order,
                    # Odoo generates 3 move lines in the receipt,
                    # so we need to remove 1 different move line in the receipt,
                    # otherwise it will cause an error
                    # saying that we need to assign a lot or serial
                    # for the remaining move line
                    po_move_lines[len(move_lines) :].unlink()
                    po_move_lines = po_move_lines[: len(move_lines)]
                # check and assign lots here
                # if len(move_lines) != (po_move_lines)
                # the zip will stop at the shortest list(only with quantity > 0)
                # list(zip([1, 2], [1, 2, 3, 4])) = [(1, 1), (2, 2)]
                # list(zip([1, 2, 3, 4], [1, 2])) = [(1, 1), (2, 2)]
                for ml, po_ml in zip(move_lines, po_move_lines, strict=True):
                    # Assuming the order of move lines is the same on both moves
                    # is risky but what would be a better option?
                    product_qty_done = self._get_product_intercompany_qty_done_dict(
                        ml, po_ml
                    )
                    po_ml.write(
                        {
                            "quantity": product_qty_done.get(po_ml.product_id) or 0,
                            "picked": True,
                        }
                    )
                    lot_id = ml.lot_id
                    if not lot_id:
                        continue
                    po_ml.lot_id = ml._ensure_lot_multicompany()
            if dest_company.sync_picking and self.state == "done":
                dest_picking.sudo().with_context(
                    cancel_backorder=bool(
                        self.env.context.get("picking_ids_not_to_backorder")
                    )
                )._action_done()
        except Exception:
            if purchase.company_id.sync_picking_failure_action == "raise":
                raise
            else:
                self._notify_picking_problem(purchase)

    def _get_intercompany_po_move(self, src_move, dest_picking):
        """Get destination PO move for this delivery move
        Try: src_move.sale_line_id.auto_purchase_line_id to its move(s) on dest_picking.
        Fallback: match by product on dest_picking. Raise if multiple
        """
        StockMove = self.env["stock.move"]
        po_move = StockMove
        sale_line = src_move.sale_line_id
        po_line = sale_line.auto_purchase_line_id if sale_line else False
        if po_line:
            po_move = po_line.move_ids.filtered(
                lambda m, ic_pick=dest_picking: m.picking_id == ic_pick
                and m.state not in ["done", "cancel"]
            )[:1]
        if not po_move:
            candidates = dest_picking.move_ids.filtered(
                lambda m: m.product_id == src_move.product_id
                and m.state not in ["done", "cancel"]
            )
            if len(candidates) > 1:
                raise UserError(
                    _(
                        "Multiple candidate receipt moves found for product "
                        "%(product)s in picking %(pick)s. Candidates: %(candidates)s"
                    )
                    % {
                        "product": src_move.product_id.display_name,
                        "pick": dest_picking.name,
                        "candidates": ", ".join(candidates.mapped("name")),
                    }
                )
            po_move = candidates[:1]
        return po_move

    def _get_intercompany_destination_picking(self, purchase):
        """
        Select the destination PO receipt picking to mirror into for THIS SO picking.
        Partial deliveries can create multiple open PO receipts.
        We must select ONE receipt per SO picking:
        - Prefer the receipt already linked via intercompany_picking_id
        - Else prefer a receipt containing PO moves linked to this SO's lines
        - Else fallback to an unlinked pending receipt
        - Else fallback to the first pending receipt
        """
        self.ensure_one()
        po_picking_pending = purchase.picking_ids.filtered(
            lambda p: p.state not in ["done", "cancel"]
        )
        # 1) already linked
        # Choose a single destination picking for this source picking
        # Partial deliveries create PO backorders, so a single
        # PO can have multiple open receipts.
        # We must select ONE destination receipt picking
        # to mirror into for THIS SO picking:
        # - prefer a PO receipt that is not yet linked -
        #  intercompany_picking_id not set),
        # - fallback to the first pending receipt if all are linked.
        if self.intercompany_picking_id:
            return self.intercompany_picking_id
        # 2) Match through SO line - PO line - PO move
        po_lines = self.move_ids.mapped("sale_line_id.auto_purchase_line_id")
        linked_po_moves = po_lines.mapped("move_ids").filtered(
            lambda m: m.state not in ["done", "cancel"]
        )
        matched_picks = po_picking_pending.filtered(
            lambda p: bool(p.move_ids & linked_po_moves)
        )
        if len(matched_picks) == 1:
            return matched_picks
        if matched_picks:
            # Prefer the receipt with the highest overlap
            return max(
                matched_picks,
                key=lambda p: len(p.move_ids & linked_po_moves),
            )
        # 3) Prefer an unlinked pending receipt
        unlinked = po_picking_pending.filtered(lambda p: not p.intercompany_picking_id)
        if unlinked:
            return unlinked[:1]
        # 4) Ultimately fallback to first pending receipt
        return po_picking_pending[:1]

    def _notify_picking_problem(self, purchase):
        """
        Create an activity to notify of a problem when syncing the intercompany picking.
        :param purchase: browse_record(purchase.order)
        """
        self.ensure_one()
        note = _(
            "Failure to confirm picking for PO %(purchase_name)s. "
            "Original picking %(picking_name)s still confirmed, please check "
            "the other side manually.",
            purchase_name=purchase.name,
            picking_name=self.name,
        )
        self.activity_schedule(
            "mail.mail_activity_data_warning",
            fields.Date.context_today(self),
            note=note,
            # Try to notify someone relevant
            user_id=(
                self.company_id.notify_user_id.id
                or self.sale_id.user_id.id
                or self.sale_id.team_id.user_id.id
                or SUPERUSER_ID,
            ),
        )

    def _is_intercompany_reception(self):
        """
        Check if the picking is an inter-company reception.
        :return: bool
        """
        return (
            self.location_id.usage in ["supplier", "transit"]
            and self.purchase_id.sudo().intercompany_sale_order_id
        )

    def _is_intercompany_delivery(self):
        """
        Check if the picking is an inter-company delivery.
        :return: bool
        """
        return (
            self.location_dest_id.usage in ["customer", "transit"]
            and self.sale_id.sudo().auto_purchase_order_id
        )
