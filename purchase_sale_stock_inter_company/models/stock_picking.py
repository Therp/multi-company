# Copyright 2018 Tecnativa - Carlos Dauden
# Copyright 2018 Tecnativa - Pedro M. Baeza
# Copyright 2026 Therp BV <https://therp.nl>.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import logging
from collections import defaultdict

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    intercompany_picking_id = fields.Many2one(comodel_name="stock.picking", copy=False)
    # Declare state with recursive=True to silence the framework warning:
    # "Field stock.picking.state should be declared with recursive=True"
    # raised because _compute_state depends on intercompany_picking_id.move_ids.state.
    state = fields.Selection(recursive=True)

    @api.depends("intercompany_picking_id.move_ids.state")
    def _compute_state(self):
        res = super()._compute_state()
        # If the picking is inter-company, it's an 'incoming'
        # type of picking, and it has not been validated nor canceled,
        # compute its state based on the linked outgoing picking's state.
        for picking in self.filtered(
            lambda pick: pick._is_intercompany_reception()
            and pick.state not in ["done", "cancel"]
        ):
            # intercompany_picking_id is set when the delivery is validated;
            # until then keep the receipt in 'waiting'.
            if not picking.intercompany_picking_id:
                picking.state = "waiting"
            elif picking.intercompany_picking_id.state not in ["done", "cancel"]:
                if picking.intercompany_picking_id.state in ["confirmed", "assigned"]:
                    picking.state = "waiting"
                else:
                    picking.state = picking.intercompany_picking_id.state
        return res

    def button_validate(self):
        # Block manual validation of intercompany receipts when the flag is set.
        if self.filtered(
            lambda picking: picking.company_id.block_po_manual_picking_validation
            and (
                picking._is_intercompany_reception()
                or picking._is_intercompany_return_delivery()
            )
            and picking.state in ["done", "waiting", "assigned"]
        ):
            raise UserError(
                self.env._(
                    "Manual validation of the picking is not allowed"
                    " in the destination company."
                )
            )
        return super().button_validate()

    def _action_done(self):
        res = super()._action_done()
        # Sync intercompany receipts for DropShip pickings
        # (delivery → customer/transit) and for return receptions of such deliveries.
        for picking in self.filtered(
            lambda pick: pick._is_intercompany_delivery()
            or pick._is_intercompany_return_reception()
        ):
            purchase = picking.sale_id.sudo().auto_purchase_order_id
            picking.sudo()._action_done_intercompany_actions(purchase)
        # Mirror intercompany returns (context key prevents infinite recursion).
        if not self.env.context.get("skip_intercompany_return_mirror"):
            for picking in self.filtered(lambda pick: pick._is_intercompany_return()):
                picking.sudo()._mirror_intercompany_return()
        return res

    def _action_done_intercompany_actions(self, purchase):
        self.ensure_one()
        try:
            dest_company = purchase.company_id
            intercompany_user = dest_company.intercompany_sale_user_id
            dest_pick = self._get_intercompany_destination_picking(purchase)
            if not dest_pick:
                raise UserError(
                    self.env._(
                        "No pending receipt picking found "
                        "for PO %(po)s to mirror %(pick)s",
                        po=purchase.name,
                        pick=self.name,
                    )
                )
            # Pin the relationship both ways so future syncs are stable.
            self.intercompany_picking_id = dest_pick
            dest_pick.intercompany_picking_id = self.id
            dest_picking = dest_pick.with_user(intercompany_user).with_company(
                dest_company
            )
            for move in self.move_ids:
                move_lines = move.move_line_ids.filtered(lambda x: x.quantity > 0)
                po_move_pending = self._get_intercompany_po_move(move, dest_picking)
                po_move_lines = po_move_pending.move_line_ids
                # Don't raise if there are no move_line_ids and the location is transit:
                # vendor locations bypass reservations, but transit locations require
                # move lines for lot/serial assignment.
                if not po_move_pending or (
                    not po_move_lines
                    and move.location_dest_id.usage != "transit"
                    and not self._is_intercompany_return_reception()
                ):
                    raise UserError(
                        self.env._(
                            "There's no corresponding line in PO %(po)s for assigning "
                            "qty from %(pick_name)s for product %(product)s",
                            po=purchase.name,
                            pick_name=self.name,
                            product=move.product_id.display_name,
                        )
                    )
                move_line_diff = len(move_lines) - len(po_move_lines)
                # Create extra move lines if the delivery has more lines
                # than the receipt
                # (e.g. delivery split by lot/serial, receipt still has one line).
                if move_line_diff > 0:
                    new_move_line_vals = []
                    for _index in range(move_line_diff):
                        vals = po_move_pending._prepare_move_line_vals()
                        new_move_line_vals.append(vals)
                    po_move_lines |= po_move_lines.create(new_move_line_vals)
                elif move_line_diff < 0:
                    # Remove surplus receipt lines when the delivery has fewer serials
                    # (backorder case: Odoo may have pre-generated more receipt lines).
                    po_move_lines[len(move_lines) :].unlink()
                    po_move_lines = po_move_lines[: len(move_lines)]
                # Mirror quantities and lots line-by-line.
                # zip stops at the shortest list, which is intentional here.
                for ml, po_ml in zip(move_lines, po_move_lines, strict=True):
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

    def _mirror_intercompany_return(self):
        """
        Mirror a validated return to the other company by creating and validating
        a corresponding return on the linked intercompany picking.
        Uses intercompany_picking_id for the link.
        """
        self.ensure_one()
        # Already mirrored — nothing to do.
        if self.intercompany_picking_id:
            return
        tracked_moves = self.move_ids.filtered(
            lambda m: m.product_id.tracking in ("lot", "serial")
        )
        source_mls_with_lots = self.move_ids.move_line_ids.filtered(
            lambda ml: ml.quantity > 0 and ml.lot_id
        )
        # Block if tracked products are present but no lot/serial info is available.
        if tracked_moves and not source_mls_with_lots:
            raise UserError(
                self.env._(
                    "Intercompany return mirroring requires lot/serial"
                    " numbers for tracked products."
                    "This return contains lot/serial tracked products,"
                    " but no lots/serials were set on the return lines."
                    " Please assign the lots/serials on the return first,"
                    " or process the return manually on the other company."
                )
            )
        # Find the original picking being returned.
        origin_picking = self._get_intercompany_return_origin_picking()
        if not origin_picking:
            return
        if not origin_picking.intercompany_picking_id:
            return
        dest_origin = origin_picking.intercompany_picking_id
        dest_company = dest_origin.company_id
        intercompany_user = dest_company.intercompany_sale_user_id
        # Aggregate quantities by product.
        qty_by_product = defaultdict(float)
        for move in self.move_ids:
            line_qty = sum(move.move_line_ids.mapped("quantity"))
            qty = line_qty if move.move_line_ids else move.quantity
            qty_by_product[move.product_id.id] += qty
        if not qty_by_product:
            _logger.warning(
                "Intercompany return mirroring skipped"
                " for picking %s: no quantities found",
                self.name,
            )
            return
        # Reuse an existing destination return if already linked to this return.
        existing_dest_return = (
            self.env["stock.picking"]
            .with_company(dest_company)
            .sudo()
            .search(
                [
                    ("company_id", "=", dest_company.id),
                    ("intercompany_picking_id", "=", self.id),
                ],
                limit=1,
            )
        )
        if existing_dest_return:
            _logger.info(
                "Intercompany return already mirrored for picking %s -> %s",
                self.name,
                existing_dest_return.name,
            )
            self.intercompany_picking_id = existing_dest_return
            return
        # Launch return wizard on the destination (original) picking.
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
        # Set return quantities per product.
        for line in wiz.product_return_moves:
            line.quantity = qty_by_product.get(line.product_id.id, 0.0)
        dest_return = wiz._create_return()
        dest_return = dest_return.with_company(dest_company).with_user(
            intercompany_user
        )
        # Link both return pickings.
        self.intercompany_picking_id = dest_return
        dest_return.intercompany_picking_id = self.id
        # Validate destination return without triggering a mirror-back loop.
        dest_return.action_confirm()
        # Mirror lot/serial info if the source return used tracked products.
        source_mls = self.move_ids.move_line_ids.filtered(
            lambda x: x.quantity > 0 and x.lot_id
        )
        if source_mls:
            lot_qty_by_product = {}
            for ml in source_mls:
                lot_qty_by_product.setdefault(ml.product_id, []).append(
                    (ml, ml.quantity)
                )
            for dest_move in dest_return.move_ids:
                if dest_move.product_id not in lot_qty_by_product:
                    continue
                line_vals = []
                for src_ml, qty in lot_qty_by_product[dest_move.product_id]:
                    vals = dest_move._prepare_move_line_vals()
                    vals.update(
                        {
                            "quantity": qty,
                            "picked": True,
                            "lot_id": src_ml.with_company(dest_company)
                            ._ensure_lot_multicompany()
                            .id,
                        }
                    )
                    line_vals.append((0, 0, vals))
                if line_vals:
                    dest_move.write({"move_line_ids": [(5, 0, 0)] + line_vals})
        for move in dest_return.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        dest_return.with_context(skip_intercompany_return_mirror=True).button_validate()

    def _get_product_intercompany_qty_done_dict(self, sale_move_lines, po_move_lines):
        """
        Return the total quantity done for the given sale/purchase move line pair.
        :param sale_move_lines: stock.move.line — delivery side
        :param po_move_lines: stock.move.line — receipt side
        :return: dict {product: quantity}
        """
        product = po_move_lines.product_id
        quantity = sum(sale_move_lines.mapped("quantity"))
        return {product: quantity}

    def _get_intercompany_return_origin_picking(self):
        """
        Return the single intercompany origin picking for this return,
        or empty recordset.
        Mirroring is only attempted when exactly ONE intercompany origin exists.
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

    def _get_intercompany_po_move(self, src_move, dest_picking):
        """
        Find the destination PO receipt move to mirror into for a given delivery move.
        Primary: follow sale_line → auto_purchase_line → move on dest_picking.
        Fallback: match by product on dest_picking (raises if ambiguous).
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
                    self.env._(
                        "Multiple candidate receipt moves found for product "
                        "%(product)s in picking %(pick)s. Candidates: %(candidates)s",
                        product=src_move.product_id.display_name,
                        pick=dest_picking.name,
                        candidates=", ".join(
                            candidates.mapped("product_id.display_name")
                        ),
                    )
                )
            po_move = candidates[:1]
        return po_move

    def _get_intercompany_destination_picking(self, purchase):
        """
        Select the destination PO receipt picking to mirror into for THIS SO picking.
        Partial deliveries can produce multiple open PO receipts; select ONE:
        1. Already linked via intercompany_picking_id.
        2. Best match by SO line → PO line → PO move overlap.
        3. An unlinked pending receipt.
        4. First pending receipt as last resort.
        """
        self.ensure_one()
        po_picking_pending = purchase.picking_ids.filtered(
            lambda p: p.state not in ["done", "cancel"]
        )
        # 1) Already linked.
        if self.intercompany_picking_id:
            return self.intercompany_picking_id
        # 2) Match through SO line → PO line → PO move.
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
            return max(
                matched_picks,
                key=lambda p: len(p.move_ids & linked_po_moves),
            )
        # 3) Prefer an unlinked pending receipt.
        unlinked = po_picking_pending.filtered(lambda p: not p.intercompany_picking_id)
        if unlinked:
            return unlinked[:1]
        # 4) Last resort.
        return po_picking_pending[:1]

    def _notify_picking_problem(self, purchase):
        """
        Schedule a warning activity when intercompany picking sync fails.
        """
        self.ensure_one()
        note = self.env._(
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
            user_id=(
                self.company_id.notify_user_id.id
                or self.sale_id.user_id.id
                or self.sale_id.team_id.user_id.id
                or SUPERUSER_ID,
            ),
        )

    def _is_intercompany_reception(self):
        """
        True if this picking is an intercompany inbound (receipt/transit-in) triggered
        by an intercompany sale order on the other side.
        """
        return (
            not self.return_id
            and self.location_id.usage in ["supplier", "transit"]
            and self.purchase_id.sudo().intercompany_sale_order_id
        )

    def _is_intercompany_delivery(self):
        """
        True if this picking is an intercompany outbound (delivery/transit-out) linked
        to an auto-generated purchase order in another company.
        """
        return (
            not self.return_id
            and self.location_dest_id.usage in ["customer", "transit"]
            and self.sale_id.sudo().auto_purchase_order_id
        )

    def _is_intercompany_return(self):
        """
        True if this picking is a return of an intercompany delivery.
        Already-linked pickings are excluded (already mirrored).
        """
        self.ensure_one()
        if self.intercompany_picking_id:
            return False
        return bool(self._get_intercompany_return_origin_picking())

    def _is_intercompany_return_reception(self):
        """
        Check if the picking is an inter-company return reception.
        :return: bool
        """
        return (
            self.return_id
            and self.location_id.usage in ["supplier", "transit"]
            and self.return_id._is_intercompany_delivery()
        )

    def _is_intercompany_return_delivery(self):
        """
        Check if the picking is an inter-company return delivery.
        :return: bool
        """
        return (
            self.return_id
            and self.location_dest_id.usage in ["customer", "transit"]
            and self.return_id._is_intercompany_reception()
        )
