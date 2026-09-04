# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .. import shopify_graphql


class StockPicking(models.Model):
    """Inhetit the model to add the fields in this model related to connector"""
    _inherit = "stock.picking"

    updated_in_shopify = fields.Boolean(default=False)
    is_shopify_delivery_order = fields.Boolean("Shopify Delivery Order", default=False)
    shopify_instance_id = fields.Many2one("shopify.instance.ept", "Shopify Instance")
    is_cancelled_in_shopify = fields.Boolean("Is Cancelled In Shopify ?", default=False, copy=False,
                                             help="Use this field to identify shipped in Odoo but cancelled in Shopify")
    is_manually_action_shopify_fulfillment = fields.Boolean("Is Manually Action Required ?", default=False, copy=False,
                                                            help="Those orders which we may fail update fulfillment "
                                                                 "status, we force set True and use will manually take "
                                                                 "necessary actions")
    shopify_fulfillment_id = fields.Char(string='Shopify Fulfillment Id')
    shopify_return_id = fields.Char(string="Shopify Return Id", copy=False)
    is_shopify_return_picking = fields.Boolean(
        string="Shopify Return Picking", compute="_compute_is_shopify_return_picking"
    )
    is_shopify_update_shipment_visible = fields.Boolean(
        string="Is Update Shipment Button Visible ?",
        compute="_compute_is_shopify_update_shipment_visible"
    )

    @api.depends("move_ids.origin_returned_move_id")
    def _compute_is_shopify_return_picking(self):
        """
        This method is used to compute is the picking shopify return or not.
        """
        for picking in self:
            picking.is_shopify_return_picking = bool(picking.move_ids.filtered("origin_returned_move_id"))

    @api.depends("state", "is_shopify_delivery_order", "updated_in_shopify", "location_dest_id.usage")
    def _compute_is_shopify_update_shipment_visible(self):
        """
        This method decides whether the "Update Order Shipping Status" button should be visible.
        It should only be visible for transfers that deliver to the customer (outgoing/dropship,
        not incoming/internal), once done, for Shopify orders not yet updated in Shopify.
        """
        for picking in self:
            picking.is_shopify_update_shipment_visible = (
                picking.state == "done"
                and picking.is_shopify_delivery_order
                and not picking.updated_in_shopify
                and picking.location_dest_id.usage == "customer"
            )

    def manually_update_shipment(self):
        """
        This is used to manually update order fulfillment and tracking reference details to Shopify store.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 15 November 2021 .
        Task_id: 179263 - Analysis : Export order status
        """
        picking = self
        self.env['sale.order'].update_order_status_in_shopify(self.shopify_instance_id, picking_ids=picking)
        return True

    def _validate_return_export_shopify_graphql(self, sale_order: object, instance: object):
        """
        This method is used to validate the return picking before exporting the return.
        :param sale_order: Sale order object
        :param instance: Shopify instance object
        :return: str

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        if self.state != "done":
            raise UserError(_("Only done pickings can be exported as Shopify return."))
        if self.shopify_return_id:
            raise UserError(_("This return picking is already exported to Shopify."))
        if not self.is_shopify_return_picking:
            raise UserError(_("Only return pickings can be exported to Shopify return."))
        if not sale_order or not sale_order.shopify_order_id:
            raise UserError(_("Related Shopify sales order not found for this picking."))
        if not instance:
            raise UserError(_("Shopify instance not found for this picking/order."))
        if not instance.use_graphql_api:
            raise UserError(_("This Shopify instance is not configured to use GraphQL API."))
        return self._get_shopify_restock_location_gid_for_return(instance)

    def _get_shopify_restock_location_gid_for_return(self, instance: object):
        """
        This method is used to get restock location ID for return.
        :param instance: Shopify instance record
        :return: shopify location record

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        warehouse = self._get_destination_warehouse_for_return()
        if not warehouse:
            raise UserError(_("Destination warehouse not found for return picking %s.") % self.name)

        shopify_location = self.env["shopify.location.ept"].search(
            [
                ("instance_id", "=", instance.id),
                ("restock_warehouse_id", "=", warehouse.id),
                ("active", "=", True),
            ],
            limit=1
        )
        if not shopify_location or not shopify_location.shopify_location_id:
            raise UserError(
                _("Shopify restock location mapping not found for warehouse '%s'.\n"
                  "Please set 'Restock Warehouse' mapping in Shopify Locations.")
                % warehouse.display_name
            )
        return shopify_location

    def _get_related_shopify_sale_order_for_return(self):
        """
        This method is used to get related sale order for the return picking.
        :return: sale order object

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        sale_order = self.sale_id
        if not sale_order:
            sale_order = self.move_ids.mapped("origin_returned_move_id.sale_line_id.order_id")[:1]
        if not sale_order:
            sale_order = self.move_ids.mapped("sale_line_id.order_id")[:1]
        return sale_order

    @staticmethod
    def _get_return_move_quantity(move):
        """
        Return processed quantity for a return move across Odoo versions.
        :return: quantity

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        qty = move.quantity
        if qty:
            return qty
        if move.move_line_ids:
            line_qty = sum(move.move_line_ids.mapped("quantity"))
            if line_qty:
                return line_qty
        return move.product_uom_qty or 0.0

    def _get_destination_warehouse_for_return(self):
        """
        This method is used to get destination warehouse for return picking.
        :return: warehouse object

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        warehouse = self.picking_type_id.warehouse_id
        if warehouse:
            return warehouse
        warehouse = self.env["stock.warehouse"].search(
            [("lot_stock_id", "=", self.location_dest_id.id), ("company_id", "=", self.company_id.id)],
            limit=1
        )
        return warehouse

    def action_export_return_shopify_graphql(self):
        """
        This method is used to export return picking to Shopify.
        :return: True

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        sale_order_obj = self.env["sale.order"]
        for picking in self:
            sale_order = picking._get_related_shopify_sale_order_for_return()
            instance = picking.shopify_instance_id or (sale_order.shopify_instance_id if sale_order else False)
            try:
                restock_location = picking._validate_return_export_shopify_graphql(
                    sale_order=sale_order, instance=instance
                )

                # prepare return line items for Shopify returnCreate mutation
                client = instance.get_graphql_client()
                helper = shopify_graphql.ReturnQueryHelper(client)
                order_gid = f"gid://shopify/Order/{sale_order.shopify_order_id}"
                result = helper.get_returnable_fulfillments(order_gid=order_gid)
                sale_order_obj._raise_shopify_graphql_errors(helper.client, result, operation_name="returnableFulfillments")
                returnable_line_items = helper.get_returnable_line_items(result)
                return_line_items = picking._prepare_shopify_return_lines(returnable_line_items)

                # create Shopify return and process it with restock disposition
                return_input = {
                    "orderId": order_gid,
                    "returnLineItems": return_line_items
                }
                result = helper.return_create(return_input=return_input)
                sale_order_obj._raise_shopify_graphql_errors(helper.client, result, operation_name="returnCreate")

                return_data = result.get("data", {}).get("returnCreate", {}).get("return", {}) or {}
                return_gid = return_data.get("id")
                if not return_gid:
                    raise UserError(_("Shopify did not return a return ID after returnCreate."))
                picking.shopify_return_id = str(client._extract_id_from_gid(return_gid) or return_gid)
                picking._process_and_close_shopify_return(
                    helper=helper,
                    return_gid=return_gid,
                    requested_return_lines=return_line_items,
                    restock_location_gid=restock_location.shopify_location_id,
                )
                picking.message_post(
                    body=_("Shopify return created and processed successfully. Restock location: %s")
                    % restock_location.name
                )
            except UserError:
                raise
            except Exception as error:
                message = ("System tried to export return picking in Shopify but an unexpected error occurred.\n"
                           "Error: %s") % error
                if instance:
                    common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                   module="shopify_ept",
                                                                   message=message,
                                                                   model_name=self._name,
                                                                   order_ref=sale_order.name if sale_order else picking.name)
                raise UserError(message)
        return True

    def _prepare_shopify_return_lines(self, returnable_line_items_map: dict):
        """
        This method is used to prepare return line items for Shopify returnCreate mutation
        based on the return moves in the picking and the returnable line items returned by Shopify.
        :param returnable_line_items_map: returnable line items map from Shopify
        :return: return line items

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        requested_qty_by_shopify_line = {}
        return_moves = self.move_ids.filtered("origin_returned_move_id")
        if not return_moves:
            raise UserError(_("Only return pickings can be exported to Shopify return."))

        # request quantity by Shopify line id based on return moves in the picking
        for move in return_moves:
            sale_line = move.origin_returned_move_id.sale_line_id or move.sale_line_id
            shopify_line_id = sale_line.shopify_line_id if sale_line else False
            if not shopify_line_id:
                raise UserError(_("Shopify line mapping not found for returned product '%s'.")
                                % move.product_id.display_name)
            qty = int(round(self._get_return_move_quantity(move)))
            if qty <= 0:
                continue
            key = str(shopify_line_id)
            requested_qty_by_shopify_line[key] = requested_qty_by_shopify_line.get(key, 0) + qty

        if not requested_qty_by_shopify_line:
            raise UserError(_("No return quantities found to export in this picking."))

        # allocate return quantities to returnable line items from Shopify and prepare return line items
        return_line_items = []
        for shopify_line_id, requested_qty in requested_qty_by_shopify_line.items():
            returnable_items = returnable_line_items_map.get(shopify_line_id, [])
            if not returnable_items:
                raise UserError(_("No returnable Shopify fulfillment line found for Shopify line id %s.")
                                % shopify_line_id)

            remaining_qty = requested_qty
            for returnable in returnable_items:
                available_qty = int(returnable.get("quantity") or 0)
                if available_qty <= 0:
                    continue
                take_qty = min(remaining_qty, available_qty)
                if take_qty <= 0:
                    continue
                return_line_items.append({
                    "fulfillmentLineItemId": returnable.get("fulfillment_line_item_id"),
                    "quantity": take_qty,
                    "returnReason": "OTHER",
                    "returnReasonNote": _("Returned from Odoo Picking %s") % self.name,
                })
                remaining_qty -= take_qty
                if remaining_qty <= 0:
                    break

            if remaining_qty > 0:
                raise UserError(
                    _("Requested return quantity %s exceeds Shopify returnable quantity for Shopify line id %s.")
                    % (requested_qty, shopify_line_id)
                )
        return return_line_items

    @staticmethod
    def _build_return_process_line_inputs(return_details: dict, requested_return_lines: list,
                                          restock_location_gid: str):
        """
        This method is used to build return process line inputs
        :param return_details: return details
        :param requested_return_lines: requested return lines
        :param restock_location_gid: restock location gid
        :return: process line inputs

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        requested_total_qty = sum(int(line.get("quantity") or 0) for line in requested_return_lines)
        if requested_total_qty <= 0:
            return []

        # return line id and qty mapping from return details
        return_lines = []
        return_line_nodes = return_details.get("returnLineItems", {}).get("nodes", []) or []
        for node in return_line_nodes:
            line_id = node.get("id")
            qty = int(node.get("quantity") or 0)
            if line_id and qty > 0:
                return_lines.append({"id": line_id, "quantity": qty})

        reverse_lines = []
        reverse_orders = return_details.get("reverseFulfillmentOrders", {}).get("nodes", []) or []
        for reverse_order in reverse_orders:
            reverse_line_nodes = reverse_order.get("lineItems", {}).get("nodes", []) or []
            for node in reverse_line_nodes:
                line_id = node.get("id")
                qty = int(node.get("totalQuantity") or 0)
                if line_id and qty > 0:
                    reverse_lines.append({"id": line_id, "quantity": qty})

        if not return_lines or not reverse_lines:
            raise UserError(_("Unable to process Shopify return because return/reverse fulfillment lines are missing."))

        # prepare return process lines
        process_lines = []
        remaining_total = requested_total_qty
        return_idx = 0
        reverse_idx = 0
        while remaining_total > 0 and return_idx < len(return_lines) and reverse_idx < len(reverse_lines):
            return_line = return_lines[return_idx]
            reverse_line = reverse_lines[reverse_idx]
            take_qty = min(remaining_total, int(return_line["quantity"]), int(reverse_line["quantity"]))
            if take_qty <= 0:
                if int(return_line["quantity"]) <= 0:
                    return_idx += 1
                if int(reverse_line["quantity"]) <= 0:
                    reverse_idx += 1
                continue

            process_lines.append({
                "id": return_line["id"],
                "quantity": take_qty,
                "dispositions": [{
                    "quantity": take_qty,
                    "dispositionType": "RESTOCKED",
                    "locationId": f"gid://shopify/Location/{restock_location_gid}",
                    "reverseFulfillmentOrderLineItemId": reverse_line["id"],
                }],
            })
            remaining_total -= take_qty
            return_line["quantity"] = int(return_line["quantity"]) - take_qty
            reverse_line["quantity"] = int(reverse_line["quantity"]) - take_qty
            if int(return_line["quantity"]) <= 0:
                return_idx += 1
            if int(reverse_line["quantity"]) <= 0:
                reverse_idx += 1

        if remaining_total > 0:
            raise UserError(_("Could not allocate full return quantity in Shopify return process."))

        return process_lines

    def _process_and_close_shopify_return(self, helper: object, return_gid: str, requested_return_lines: list,
                                          restock_location_gid: str):
        """
        This method is used to process and close Shopify return after creating the return in Shopify.
        :param helper: Helper object for Shopify return processing
        :param return_gid: Shopify return gid
        :param requested_return_lines: Requested return lines for processing the return
        :param restock_location_gid: Restock location gid for processing the return

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        sale_order_obj = self.env['sale.order']
        details_result = helper.get_return_for_processing(return_gid=return_gid)
        sale_order_obj._raise_shopify_graphql_errors(helper.client, details_result, operation_name="getReturnForProcessing")
        return_details = details_result.get("data", {}).get("return", {}) or {}
        process_line_inputs = self._build_return_process_line_inputs(
            return_details=return_details,
            requested_return_lines=requested_return_lines,
            restock_location_gid=restock_location_gid,
        )
        if process_line_inputs:
            process_input = {
                "returnId": return_gid,
                "returnLineItems": process_line_inputs,
            }
            process_result = helper.return_process(return_process_input=process_input)
            sale_order_obj._raise_shopify_graphql_errors(helper.client, process_result, operation_name="returnProcess")

        close_result = helper.return_close(return_gid=return_gid)
        sale_order_obj._raise_shopify_graphql_errors(helper.client, close_result, operation_name="returnClose")
