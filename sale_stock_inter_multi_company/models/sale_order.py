# Copyright 2017 Carlos Dauden - Tecnativa <carlos.dauden@tecnativa.com>
# Copyright 2018 Vicent Cubells - Tecnativa <vicent.cubells@tecnativa.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import operator as py_operator
from collections import defaultdict

from odoo import api, models

OPERATORS = {
    '<': py_operator.lt,
    '>': py_operator.gt,
    '<=': py_operator.le,
    '>=': py_operator.ge,
    '=': py_operator.eq,
    '!=': py_operator.ne
}


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        data_free_qty_multicompany = {}
        # Obtener en data_free_qty_multicompany la relacion de compañia y productos con sus cantidades disponible
        for line in self.order_line:
            free_qty_in_stock = line.product_id.with_context(
                {'warehouse': self.warehouse_id.id}).free_qty
            remaining_quantity = line.product_uom_qty - free_qty_in_stock
            if free_qty_in_stock > line.product_uom_qty:
                continue
            for warehouse in self.env['stock.warehouse'].sudo().search([('company_id', '!=', self.company_id.id)]):
                free_qty_product = line.product_id.with_context(
                    {'warehouse': warehouse.id, 'sale_multicompany': True}).free_qty
                if free_qty_product > 0 and remaining_quantity > 0:
                    available = 0
                    if remaining_quantity > free_qty_product:
                        available = free_qty_product
                        remaining_quantity = remaining_quantity - free_qty_product
                    else:
                        available = remaining_quantity
                        remaining_quantity = 0
                    if warehouse.company_id not in data_free_qty_multicompany:

                        data_free_qty_multicompany.update({
                            warehouse.company_id: {
                                line.product_id: {'cantidad': available, 'order_line': line}
                            }
                        })
                    elif line.product_id not in data_free_qty_multicompany[warehouse.company_id]:
                        data_free_qty_multicompany[warehouse.company_id][
                            line.product_id] = {'cantidad': available, 'order_line': line}
                    else:
                        data_free_qty_multicompany[warehouse.company_id][
                            line.product_id]['cantidad'] += available

        po_obj = self.env["purchase.order"]
        for data in data_free_qty_multicompany.keys():
            purchase_id = po_obj.create({"partner_id": data.partner_id.id})
            lines = []
            for product in data_free_qty_multicompany[data]:
                lines.append((0, 0,
                              {
                                  "name": product.display_name,
                                  "product_qty": data_free_qty_multicompany[data][product]['cantidad'],
                                  "product_id": product.id,
                                  "product_uom": data_free_qty_multicompany[data][product][
                                      'order_line'].product_uom.id,
                                  "sale_line_id": data_free_qty_multicompany[data][product]['order_line'].id
                              },))
            purchase_id.order_line = lines

        return super().action_confirm()


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    @api.depends(
        'product_id', 'customer_lead', 'product_uom_qty', 'product_uom', 'order_id.commitment_date',
        'move_ids', 'move_ids.forecast_expected_date', 'move_ids.forecast_availability')
    def _compute_qty_at_date(self):
        """ Compute the quantity forecasted of product at delivery date. There are
        two cases:
         1. The quotation has a commitment_date, we take it as delivery date
         2. The quotation hasn't commitment_date, we compute the estimated delivery
            date based on lead time"""
        treated = self.browse()
        # If the state is already in sale the picking is created and a simple forecasted quantity isn't enough
        # Then used the forecasted data of the related stock.move
        for line in self.filtered(lambda l: l.state == 'sale'):
            if not line.display_qty_widget:
                continue
            moves = line.move_ids.filtered(lambda m: m.product_id == line.product_id)
            line.forecast_expected_date = max(moves.filtered("forecast_expected_date").mapped("forecast_expected_date"),
                                              default=False)
            line.qty_available_today = 0
            line.free_qty_today = 0
            for move in moves:
                line.qty_available_today += move.product_uom._compute_quantity(move.reserved_availability,
                                                                               line.product_uom)
                line.free_qty_today += move.product_id.uom_id._compute_quantity(move.forecast_availability,
                                                                                line.product_uom)
            line.scheduled_date = line.order_id.commitment_date or line._expected_date()
            line.virtual_available_at_date = False
            treated |= line

        qty_processed_per_product = defaultdict(lambda: 0)
        grouped_lines = defaultdict(lambda: self.env['sale.order.line'])
        # We first loop over the SO lines to group them by warehouse and schedule
        # date in order to batch the read of the quantities computed field.
        ctx = self._context.copy()
        ctx.update({'sale_multicompany': True})
        for line in self.filtered(lambda l: l.state in ('draft', 'sent')):
            if not (line.product_id and line.display_qty_widget):
                continue
            grouped_lines[(line.warehouse_id.id, line.order_id.commitment_date or line._expected_date())] |= line

        for (warehouse, scheduled_date), lines in grouped_lines.items():
            product_qties = lines.mapped('product_id').with_context(to_date=scheduled_date, warehouse=False,
                                                                    sale_multicompany=True).read([
                'qty_available',
                'free_qty',
                'virtual_available',
            ])
            qties_per_product = {
                product['id']: (product['qty_available'], product['free_qty'], product['virtual_available'])
                for product in product_qties
            }
            for line in lines:
                line.scheduled_date = scheduled_date
                qty_available_today, free_qty_today, virtual_available_at_date = qties_per_product[line.product_id.id]
                line.qty_available_today = qty_available_today - qty_processed_per_product[line.product_id.id]
                line.free_qty_today = free_qty_today - qty_processed_per_product[line.product_id.id]
                line.virtual_available_at_date = virtual_available_at_date - qty_processed_per_product[
                    line.product_id.id]
                line.forecast_expected_date = False
                product_qty = line.product_uom_qty
                if line.product_uom and line.product_id.uom_id and line.product_uom != line.product_id.uom_id:
                    line.qty_available_today = line.product_id.uom_id._compute_quantity(line.qty_available_today,
                                                                                        line.product_uom)
                    line.free_qty_today = line.product_id.uom_id._compute_quantity(line.free_qty_today,
                                                                                   line.product_uom)
                    line.virtual_available_at_date = line.product_id.uom_id._compute_quantity(
                        line.virtual_available_at_date, line.product_uom)
                    product_qty = line.product_uom._compute_quantity(product_qty, line.product_id.uom_id)
                qty_processed_per_product[line.product_id.id] += product_qty
            treated |= lines
        remaining = (self - treated)
        remaining.virtual_available_at_date = False
        remaining.scheduled_date = False
        remaining.forecast_expected_date = False
        remaining.free_qty_today = False
        remaining.qty_available_today = False
