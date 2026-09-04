# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

import json
import logging
from datetime import datetime, timedelta, timezone
import time
import pytz

from dateutil import parser

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import html2plaintext
from markupsafe import Markup
from ..shopify.pyactiveresource.util import xml_to_dict
from .. import shopify
from ..shopify.pyactiveresource.connection import ClientError
from odoo.tools.float_utils import float_is_zero, float_compare
import re
import urllib.parse
from ..shopify_graphql.client import ShopifyGraphQLClient
from ..shopify_graphql.queries.order_export import OrderExportQueryHelper
from ..shopify_graphql.queries.order import OrderQueryHelper
# from ..shopify_graphql.queries.fulfillment import FulfillmentQueryHelper
from ..shopify_graphql.queries.metafield import MetafieldQueryHelper
from .. import shopify_graphql
utc = pytz.utc

# Shopify GraphQL Global ID (GID) prefixes
SHOPIFY_GID_ORDER_PREFIX = "gid://shopify/Order/"
SHOPIFY_GID_LINE_ITEM_PREFIX = "gid://shopify/LineItem/"
SHOPIFY_GID_PRODUCT_VARIANT_PREFIX = "gid://shopify/ProductVariant/"
SHOPIFY_GID_CUSTOMER_PREFIX = "gid://shopify/Customer/"

# Default fallback wait time (seconds) when Shopify rate-limit response omits Retry-After header
SHOPIFY_RETRY_AFTER_DEFAULT = 5

_logger = logging.getLogger("Shopify Order")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _get_shopify_order_status(self):
        """
        Set updated_in_shopify of order from the pickings.
        @author: Maulik Barad on Date 06-05-2020.
        """
        for order in self:
            if order.shopify_instance_id:
                pickings = order.picking_ids.filtered(lambda x: x.state != "cancel")
                if pickings:
                    outgoing_picking = pickings.filtered(
                        lambda x: x.location_dest_id.usage == "customer")
                    if outgoing_picking and all(outgoing_picking.mapped("updated_in_shopify")):
                        order.updated_in_shopify = True
                        continue
                if order.state != 'draft' and order.moves_count > 0:
                    move_ids = self.env["stock.move"].search([("picking_id", "=", False),
                                                              ("sale_line_id", "in", order.order_line.ids)])
                    state = set(move_ids.mapped('state'))
                    if len(set(state)) == 1 and 'done' in set(state):
                        order.updated_in_shopify = True
                        continue
                order.updated_in_shopify = False
                continue
            order.updated_in_shopify = False

    def _search_shopify_order_ids(self, operator, value):
        query = """ SELECT so.id FROM stock_picking sp
                    INNER JOIN stock_move sm ON sm.picking_id = sp.id
                    INNER JOIN stock_reference_move_rel smr ON smr.move_id = sm.id
                    INNER JOIN stock_reference_sale_rel srsr ON srsr.reference_id = smr.reference_id
                    INNER JOIN sale_order so ON so.id = srsr.sale_id
                    INNER JOIN stock_location sl ON sl.id = sp.location_dest_id AND sl.usage = 'customer'
                    WHERE sp.updated_in_shopify != TRUE AND sp.state != 'cancel'
                """
        if operator == 'in':
            query = """ SELECT so.id  FROM stock_picking sp INNER JOIN stock_move sm ON sm.picking_id = sp.id
                        INNER JOIN stock_reference_move_rel smr ON smr.move_id = sm.id
                        INNER JOIN stock_reference_sale_rel srsr ON srsr.reference_id = smr.reference_id
                        INNER JOIN sale_order so ON so.id = srsr.sale_id
                        INNER JOIN stock_location ON stock_location.id = sp.location_dest_id AND stock_location.usage = 'customer'
                        WHERE sp.updated_in_shopify = TRUE AND sp.state != 'cancel'
                        UNION ALL
                        SELECT so.id FROM sale_order as so
                        INNER JOIN sale_order_line as sl ON sl.order_id = so.id
                        INNER JOIN stock_move as sm ON sm.sale_line_id = sl.id
                        WHERE sm.picking_id IS NULL AND sm.state = 'done' AND so.shopify_instance_id IS NOT NULL
                    """

        self.env.cr.execute(query)
        results = self.env.cr.fetchall()
        order_ids = []
        for result_tuple in results:
            order_ids.append(result_tuple[0])
        order_ids = list(set(order_ids))
        return [('id', 'in', order_ids)]

    is_manual_order = fields.Boolean("Manual Order", default=False, copy=False,
                                     help="Used to identify that the order is manual sale order.")
    shopify_order_id = fields.Char("Shopify Order Ref", copy=False)
    shopify_order_number = fields.Char(copy=False)
    shopify_instance_id = fields.Many2one("shopify.instance.ept", "Shopify Instance", copy=False)
    shopify_market_id = fields.Many2one(
        "shopify.market.ept",
        string="Shopify Market",
        copy=False,
        help="Shopify market from which this order originated. "
             "Determines company, warehouse, pricelist and fiscal position.",
    )
    shopify_order_status = fields.Char(copy=False, tracking=True,
                                       help="Shopify order status when order imported in odoo at the moment order"
                                            "status in Shopify.")
    shopify_payment_gateway_id = fields.Many2one('shopify.payment.gateway.ept',
                                                 string="Payment Gateway", copy=False)
    risk_ids = fields.One2many("shopify.order.risk", 'odoo_order_id', "Risks", copy=False)
    shopify_location_id = fields.Many2one("shopify.location.ept", "Shopify Location", copy=False)
    checkout_id = fields.Char(copy=False)
    is_risky_order = fields.Boolean("Risky Order?", default=False, copy=False)
    updated_in_shopify = fields.Boolean("Updated In Shopify ?", compute=_get_shopify_order_status,
                                        search='_search_shopify_order_ids')
    closed_at_ept = fields.Datetime("Closed At", copy=False)
    canceled_in_shopify = fields.Boolean(default=False, copy=False)
    is_pos_order = fields.Boolean("POS Order ?", copy=False, default=False)
    is_service_tracking_updated = fields.Boolean("Service Tracking Updated", default=False, copy=False)
    is_shopify_multi_payment = fields.Boolean("Multi Payments?", default=False, copy=False,
                                              help="It is used to identify that order has multi-payment gateway or not")
    shopify_payment_ids = fields.One2many('shopify.order.payment.ept', 'order_id',
                                          string="Payment Lines")
    is_buy_with_prime_order = fields.Boolean("Buy with Prime Order", default=False, copy=False)
    shopify_return_payload_json = fields.Text("Shopify Return Payload", copy=False)
    source_link = fields.Char(string="Source Link")
    shopify_channel_id = fields.Many2one("shopify.channel.ept", "Shopify Channel", copy=False)

    _unique_shopify_order = models.Constraint('unique(shopify_instance_id,shopify_order_id,shopify_order_number)',
                                              "Shopify order must be Unique.")

    @api.model_create_multi
    def create(self, vals: list):
        """
        Flag orders without a Shopify order ID as manual orders by setting is_manual_order to True.
        :param vals: order values.
        :return: order records.

        :author: Karan Modasiya on Date 10-Feb-2026.
        """
        if isinstance(vals, dict):
            vals = [vals]
        for val in vals:
            if not val.get('shopify_order_id'):
                val['is_manual_order'] = True
        return super().create(vals)

    def prepare_shopify_customer_and_addresses(self, order_response, pos_order, instance, order_data_line):
        """
        Searches for existing customer in Odoo and creates in odoo, if not found.
        @author: Maulik Barad on Date 11-Sep-2020.
        """
        res_partner_obj = self.env["res.partner"]
        shopify_res_partner_obj = self.env["shopify.res.partner.ept"]
        message = False

        if pos_order:
            if order_response.get("customer"):
                partner = res_partner_obj.create_shopify_pos_customer(order_response, instance)
            else:
                partner = instance.shopify_default_pos_customer_id
            if not partner:
                message = ("It seems the imported order is a POS order, and the Shopify API did not provide customer details in the response.\n"
                            "Action Items:\n"
                            "- Set a default POS customer in the Shopify configuration.\n"
                            "- Go to: Shopify → Configuration → Settings → POS Customer.")
        else:
            if not any([order_response.get("customer", {}), order_response.get("billing_address", {}),
                        order_response.get("shipping_address", {})]):
                message = ("System tried to import the order: %s but Customer details is not available order response.\n"
                          "Action items:\n"
                          "- Verify the order in the Shopify store and set the proper customer in the existing order.\n"
                          "- Try to import the order from the operation wizard.") % (
                          order_response.get("order_number"))
            else:
                partner = order_response.get("customer") and shopify_res_partner_obj.with_context(
                    order_data_queue=True).shopify_create_contact_partner(
                    order_response.get("customer"), instance, order_data_line)
        if message:
            self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                        module="shopify_ept",
                                                                        message=message,
                                                                        model_name='sale.order',
                                                                        order_ref=order_response.get('name'),
                                                                        shopify_order_data_queue_line_id=order_data_line.id if order_data_line else False)
            order_data_line.write({"state": "failed", "processed_at": datetime.now()})
            _logger.info(message)
            return False, False, False

        if not partner:
            if order_data_line:
                order_data_line.write({"state": "failed", "processed_at": datetime.now()})
            return False, False, False

        if partner.parent_id:
            partner = partner.parent_id

        invoice_address = order_response.get("billing_address") and \
                          shopify_res_partner_obj.shopify_create_or_update_address(instance,
                                                                                   order_response.get(
                                                                                       "billing_address"), partner,
                                                                                   "invoice") or partner

        delivery_address = order_response.get("shipping_address") and \
                           shopify_res_partner_obj.shopify_create_or_update_address(instance,
                                                                                    order_response.get(
                                                                                        "shipping_address"), partner,
                                                                                    "delivery") or partner

        # Below condition as per the task 169257.
        if not partner and invoice_address and delivery_address:
            partner = invoice_address
        if not partner and not delivery_address and invoice_address:
            partner = invoice_address
            delivery_address = invoice_address
        if not partner and not invoice_address and delivery_address:
            partner = delivery_address
            invoice_address = delivery_address

        return partner, delivery_address, invoice_address

    def set_shopify_location_and_warehouse(self, order_response, instance, pos_order, sale_order):
        """
        This method sets shopify location and warehouse related to that location in order.
        @author: Maulik Barad on Date 11-Sep-2020.
        """
        shopify_location_obj = self.env["shopify.location.ept"]
        shopify_location = shopify_location_obj
        if order_response.get("location_id"):
            shopify_location_id = order_response.get("location_id")
        elif order_response.get("fulfillments"):
            shopify_location_id = order_response.get("fulfillments")[0].get("location_id") or \
                                  order_response['fulfillments'][0].get("location", {}).get("id")
        else:
            shopify_location_id = False

        if shopify_location_id:
            shopify_location = shopify_location_obj.search(
                [("shopify_location_id", "=", shopify_location_id),
                 ("instance_id", "=", instance.id)],
                limit=1)

        if shopify_location and shopify_location.warehouse_for_order:
            # Shopify location has an explicit warehouse mapping — use it
            if sale_order.shopify_market_id and sale_order.shopify_market_id.warehouse_id:
                warehouse_id = sale_order.shopify_market_id.warehouse_id.id
            else:
                warehouse_id = shopify_location.warehouse_for_order.id
        elif sale_order.shopify_market_id and sale_order.shopify_market_id.warehouse_id:
            # No location match — use the market's warehouse to keep company consistent
            warehouse_id = sale_order.shopify_market_id.warehouse_id.id
        else:
            # Final fallback: instance default warehouse
            warehouse_id = instance.shopify_warehouse_id.id

        if instance.import_buy_with_prime_shopify_order and sale_order.is_buy_with_prime_order:
            warehouse_id = instance.buy_with_prime_warehouse_id.id if instance.buy_with_prime_warehouse_id else warehouse_id

        return {"shopify_location_id": shopify_location and shopify_location.id or False,
                "warehouse_id": warehouse_id, "is_pos_order": pos_order}

    def create_shopify_order_lines(self, lines, order_response, instance):
        """
        This method creates sale order line and discount line for Shopify order.
        @author: Maulik Barad on Date 11-Sep-2020.
        """
        sale_order_line_obj = self.env["sale.order.line"]
        total_discount = order_response.get("total_discounts", 0.0)
        order_number = order_response.get("order_number")
        for line in lines:
            is_custom_line, is_gift_card_line, product = self.search_custom_tip_gift_card_product(line, instance)
            price = line.get("price")
            if instance.order_visible_currency:
                price = self.get_price_based_on_customer_visible_currency(line.get("price_set"), order_response, price)
            order_line = self.shopify_create_sale_order_line(line, product, line.get("current_quantity"),
                                                             product.name, price,
                                                             order_response)
            if is_gift_card_line:
                line_vals = {'is_gift_card_line': True}
                if line.get('name'):
                    line_vals.update({'name': line.get('name')})
                order_line.write(line_vals)

            if is_custom_line:
                order_line.write({'name': line.get('name')})

            if line.get('duties'):
                self.create_shopify_duties_lines(line.get('duties'), order_response, instance)

            if float(total_discount) > 0.0:
                discount_amount = self._get_shopify_discount_allocation_amount(instance, line, order_response)

                if discount_amount > 0.0:
                    _logger.info("Creating discount line for Odoo order(%s) and Shopify order is (%s)", self.name,
                                 order_number)
                    self.shopify_create_sale_order_line({}, instance.discount_product_id, 1,
                                                        product.name, float(discount_amount) * -1,
                                                        order_response, previous_line=order_line,
                                                        is_discount=True)
                    _logger.info("Created discount line for Odoo order(%s) and Shopify order is (%s)", self.name,
                                 order_number)
        # add gift card as product in sale order line
        final_transactions_results = self.prepare_final_list_of_transactions(order_response.get('transaction'))
        total_giftcard_price = 0.0
        total_giftcard_qty = 0
        for transaction in final_transactions_results:
            if transaction.get('gateway') == 'gift_card':
                total_giftcard_qty += 1
                total_giftcard_price += float(transaction.get('amount'))
                # if self.order_line.filtered(
                #         lambda line: line.product_id.id == product_id.id and abs(line.price_unit) == float(price)):
                #     continue
        if total_giftcard_price:
            product_id = instance.gift_card_product_id
            line_vals = self.prepare_vals_for_gift_card_sale_order_line(product_id, product_id.name,
                                                                        total_giftcard_price, total_giftcard_qty)
            sale_order_line_obj.create(line_vals)
            _logger.info("Gift card line for Odoo order(%s) and Shopify order is (%s)", self.name, order_number)

    def prepare_vals_for_gift_card_sale_order_line(self, product_id, product_name, price, order_qty):
        uom_id = product_id and product_id.uom_id and product_id.uom_id.id or False
        instance = self.shopify_instance_id
        price_unit = price / order_qty
        line_vals = {
            "product_id": product_id.id,
            "order_id": self.id,
            "company_id": self.company_id.id,
            "product_uom_id": uom_id,
            "name": "Gift card for " + str(product_name),
            "price_unit": float(price_unit) * -1,
            "product_uom_qty": order_qty
        }
        if instance.shopify_analytic_account_id:
            analytic_account = self.env['account.analytic.account']
            if instance.use_channel_analytic_account and instance.use_graphql_api:
                channel_handle = self.env.context.get('shopify_channel_handle') or False
                channel_app_id = self.env.context.get('shopify_channel_app_id') or False
                analytic_account = self.env["shopify.channel.ept"].get_channel_analytic(
                    instance, channel_handle, channel_app_id)
            if analytic_account:
                line_vals.update({'analytic_distribution': {analytic_account.id: 100}})
            else:
                analytic_distribution_dict = {}
                analytic_distribution_dict.update({instance.shopify_analytic_account_id.id: 100})
                line_vals.update({'analytic_distribution': analytic_distribution_dict})
        return line_vals

    def get_price_based_on_customer_visible_currency(self, price_set, order_response, price):
        """
        This method is used to set price based on customer visible currency.
        @author: Meera Sidapara on Date 16-June-2022.
        Task: 193010 - Shopify Multi currency changes
        """
        presentment_currency_code = order_response.get('presentment_currency') or order_response.get(
            'presentment_currency_code')
        if float(price_set['shop_money']['amount']) > 0.0 and price_set['shop_money'][
            'currency_code'] == presentment_currency_code:
            price = price_set['shop_money']['amount']
        elif float(price_set['presentment_money']['amount']) > 0.0 and price_set['presentment_money'][
            'currency_code'] == presentment_currency_code:
            price = price_set['presentment_money']['amount']
        return float(price)

    def create_shopify_duties_lines(self, duties_line, order_response, instance):
        """
        Creates duties lines for shopify orders.
        @author: Meera Sidapara on Date 17-June-2022.
        """
        order_number = order_response.get("order_number")
        product = instance.duties_product_id if instance.duties_product_id else False
        # add duties
        for duties in duties_line:
            duties_amount = 0.0
            order_currency = self.pricelist_id.currency_id.name

            price_set = duties.get("price_set", {})
            presentment_money = price_set.get("presentment_money", {})
            shop_money = price_set.get("shop_money", {})

            if order_currency == presentment_money.get("currency_code"):
                duties_amount = float(presentment_money.get("amount", 0.0))
            elif order_currency == shop_money.get("currency_code"):
                duties_amount = float(shop_money.get("amount", 0.0))

            if instance.order_visible_currency:
                duties_amount = self.get_price_based_on_customer_visible_currency(duties.get("price_set"),
                                                                                  order_response,
                                                                                  duties_amount)

            if float(duties_amount) > 0.0:
                _logger.info("Creating duties line for Odoo order(%s) and Shopify order is (%s)", self.name,
                             order_number)
                self.shopify_create_sale_order_line(duties, instance.duties_product_id, 1,
                                                    product.name, float(duties_amount),
                                                    order_response, is_duties=True)
                _logger.info("Created duties line for Odoo order(%s) and Shopify order is (%s)", self.name,
                             order_number)

    def search_custom_tip_gift_card_product(self, line, instance):
        """
        Search the products of the custom option, Tip, and Gift card product..
        @author: Haresh Mori on Date 12-June-2021.
        Task: 172889 - TIP order import
        """
        is_custom_line = False
        is_gift_card_line = False
        product = False
        if not line.get('product_id'):
            if line.get('sku'):
                product = self.env["product.product"].search(
                    [("default_code", "=", line.get("sku"))], limit=1
                ) or self.env["shopify.product.product.ept"].search(
                    [
                        ("shopify_instance_id", "=", instance.id),
                        ("default_code", "=", line.get("sku")),
                    ],
                    limit=1,
                ).product_id

            if not product:
                if line.get('requires_shipping'):
                    product = instance.custom_storable_product_id
                else:
                    product = instance.custom_service_product_id
            is_custom_line = True
        if line.get('name') == 'Tip':
            product = instance.tip_product_id
            is_custom_line = True
        if line.get('gift_card') or line.get('is_gift_card'):
            product = instance.gift_card_product_id
            is_gift_card_line = True
        else:
            if not is_custom_line:
                shopify_product = self.search_shopify_product_for_order_line(line, instance)
                product = shopify_product.product_id

        return is_custom_line, is_gift_card_line, product

    def create_shopify_shipping_lines(self, order_response, instance):
        """
        Creates shipping lines for shopify orders.
        @author: Maulik Barad on Date 11-Sep-2020.
        """
        delivery_carrier_obj = self.env["delivery.carrier"]
        order_number = order_response.get("order_number")
        for line in order_response.get("shipping_lines", []):
            carrier = delivery_carrier_obj.shopify_search_create_delivery_carrier(line, instance)
            shipping_product = instance.shipping_product_id
            if carrier:
                self.write({"carrier_id": carrier.id})
                shipping_product = carrier.product_id
            # Some order in If shipping carrier is not there and Shipping amount is there then create shipping line.
            # Changes suggested by dipesh sir.
            if shipping_product:
                if float(line.get("price")) > 0.0:
                    shipping_price = line.get("price")
                    if instance.order_visible_currency:
                        shipping_price = self.get_price_based_on_customer_visible_currency(line.get("price_set"),
                                                                                           order_response,
                                                                                           shipping_price)
                    order_line = self.shopify_create_sale_order_line(line, shipping_product, 1,
                                                                     shipping_product.name or line.get("title"),
                                                                     shipping_price,
                                                                     order_response, is_shipping=True)
                discount_amount = self._get_shopify_discount_allocation_amount(instance, line, order_response)
                if discount_amount > 0.0:
                    _logger.info("Creating discount line for Odoo order(%s) and Shopify order is (%s)", self.name,
                                 order_number)
                    self.shopify_create_sale_order_line({}, instance.discount_product_id, 1,
                                                        shipping_product.name, float(discount_amount) * -1,
                                                        order_response, previous_line=order_line,
                                                        is_discount=True)
                    _logger.info("Created discount line for Odoo order(%s) and Shopify order is (%s)", self.name,
                                 order_number)

    def import_shopify_orders(self, order_data_lines, instance):
        """
        This method used to create a sale orders in Odoo.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 11/11/2019.
        Task Id : 157350
        @change: By Maulik Barad on Date 21-Sep-2020.
        @change: By Meera Sidapara on Date 27-Oct-2021 for Task Id : 179249.
        """
        order_risk_obj = self.env["shopify.order.risk"]
        common_log_line_obj = self.env["common.log.lines.ept"]
        ORDER_COMMIT_BATCH_SIZE = 5  # Number of orders to process before committing to DB
        order_ids = []
        commit_count = 0

        instance.connect_in_shopify()

        for order_data_line in order_data_lines:
            if commit_count == ORDER_COMMIT_BATCH_SIZE:
                self.env.cr.commit()
                commit_count = 0
            commit_count += 1
            order_data = order_data_line.order_data
            order_response = json.loads(order_data)

            order_number = order_response.get("order_number")
            shopify_financial_status = order_response.get("financial_status")
            _logger.info("Started processing Shopify order(%s) and order id is(%s)", order_number,
                         order_response.get("id"))

            date_order = self.convert_order_date(order_response)
            if str(instance.import_order_after_date) > date_order:
                message = ("Order %s was not imported into Odoo due to a configuration mismatch.\n"
                           "Received order date: %s \n"
                           "Action Items:\n"
                           "- Check the Order After Date in Shopify configuration and ensure it includes this order date.\n"
                           "- Go to: Shopify → Configuration → Settings.") % (order_number, date_order)
                _logger.info(message)
                common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                               message=message,
                                                               model_name='sale.order',
                                                               order_ref=order_response.get("name"),
                                                               shopify_order_data_queue_line_id=order_data_line.id if order_data_line else False)
                order_data_line.write({'state': 'failed', 'processed_at': datetime.now()})
                continue

            # --- Sales Channel exclusion filter ---
            # Extract channel info from the order response so we can check whether
            # the channel's "Exclude Orders from Import" flag is enabled.
            channel_info = (order_response.get("channel_information") or order_response.get("channelInformation") or {})
            channel_defination = (channel_info.get("channel_definition") or channel_info.get("channelDefinition") or {})
            channel_handle = channel_defination.get("handle") or False
            channel_app_raw = channel_info.get("app") or {}
            channel_app_id = channel_app_raw.get("id") or False
            if channel_app_id and str(channel_app_id).startswith("gid://"):
                try:
                    channel_app_id = int(str(channel_app_id).split("/")[-1])
                except Exception:
                    pass
            if self.env["shopify.channel.ept"].is_channel_excluded_for_order_ept(instance, channel_handle, channel_app_id):
                message = (
                    "Order %s (Shopify id: %s) was skipped because its sales channel "
                    "(handle='%s', app_id=%s) is configured to exclude orders from import.\n"
                    "To import orders from this channel, disable the "
                    "'Exclude Orders from Import' flag on the corresponding Shopify Sales Channel record."
                ) % (order_number, order_response.get("id"), channel_handle, channel_app_id)
                _logger.info(message)
                common_log_line_obj.create_common_log_line_ept(
                    shopify_instance_id=instance.id, module="shopify_ept",
                    message=message, model_name='sale.order',
                    order_ref=order_response.get("name"),
                    shopify_order_data_queue_line_id=order_data_line.id if order_data_line else False,
                )
                order_data_line.write({'state': 'cancel', 'processed_at': datetime.now()})
                continue
            # --- End sales channel filter ---

            sale_order = self.search_existing_shopify_order(order_response, instance, order_number)

            # Update SO metafields values
            if sale_order and instance.enable_metafield_sync and instance.use_graphql_api:
                self.set_metafield_values_in_order(order_response, instance, sale_order)

            if sale_order:
                order_data_line.write({"state": "done", "processed_at": datetime.now(),
                                       "sale_order_id": sale_order.id, "order_data": False})
                _logger.info("Done the Process of order Because Shopify Order(%s) is exist in Odoo and Odoo order is("
                             "%s)", order_number, sale_order.name)
                continue

            pos_order = order_response.get("source_name", "") == "pos"
            partner, delivery_address, invoice_address = self.prepare_shopify_customer_and_addresses(
                order_response, pos_order, instance, order_data_line)
            if not partner:
                continue

            lines = order_response.get("line_items")
            if self.check_mismatch_details(lines, instance, order_number, order_data_line):
                _logger.info("Mismatch details found in this Shopify Order(%s) and id (%s)", order_number,
                             order_response.get("id"))
                order_data_line.write({"state": "failed", "processed_at": datetime.now()})
                continue

            sale_order = self.shopify_create_order(instance, partner, delivery_address, invoice_address,
                                                   order_data_line, order_response, lines, order_number)

            # Update SO metafields values
            if instance.enable_metafield_sync and instance.use_graphql_api:
                self.set_metafield_values_in_order(order_response, instance, sale_order)


            if not sale_order:
                message = "Configuration missing in Odoo while importing Shopify Order(%s) and id (%s)" % (
                    order_number, order_response.get("id"))
                _logger.info(message)
                common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                               message=message,
                                                               model_name='sale.order',
                                                               order_ref=order_response.get('name'),
                                                               shopify_order_data_queue_line_id=order_data_line.id if order_data_line else False)
                continue
            order_ids.append(sale_order.id)

            location_vals = self.set_shopify_location_and_warehouse(order_response, instance, pos_order, sale_order)

            if instance.is_delivery_multi_warehouse:
                warehouses = sale_order.order_line.filtered(lambda line_item: line_item.warehouse_id_ept).mapped(
                    'warehouse_id_ept')
                if warehouses and len(set(warehouses.ids)) == 1:
                    location_vals.update({"warehouse_id": warehouses.id})

            sale_order.write(location_vals)

            # ── Apply market language to customer partner ─────────────────────
            # This ensures the customer's language is set correctly for
            # documents (invoices, delivery slips) generated from this market.
            market = sale_order.shopify_market_id
            if market and market.lang_id:
                lang_code = market.lang_id.code
                # Only update if the partner's language differs, to avoid
                # unnecessary writes on existing customers.
                if sale_order.partner_id.lang != lang_code:
                    sale_order.partner_id.write({"lang": lang_code})
                    _logger.info(
                        "Set language '%s' on partner '%s' from market '%s'.",
                        lang_code, sale_order.partner_id.name, market.name,
                    )
            elif instance.shopify_lang_id:
                # Fallback: apply instance language if partner has none set
                lang_code = instance.shopify_lang_id.code
                if not sale_order.partner_id.lang:
                    sale_order.partner_id.write({"lang": lang_code})

            if sale_order.shopify_order_status != "fulfilled":
                if instance.use_graphql_api:
                    order_risk_obj.shopify_create_risk_in_order_by_graphql(order_response, sale_order)
                else:
                    risk_result = shopify.OrderRisk().find(order_id=order_response.get("id"))
                    if risk_result:
                        order_risk_obj.shopify_create_risk_in_order(risk_result, sale_order)
                        risk = sale_order.risk_ids.filtered(lambda x: x.recommendation != "accept")
                        if risk:
                            sale_order.is_risky_order = True

            _logger.info("Starting auto workflow process for Odoo order(%s) and Shopify order is (%s)",
                         sale_order.name, order_number)
            message = ""
            try:
                context = dict(self.env.context)
                if not self.env.context.get('shopify_order_financial_status'):
                    context.update({'shopify_order_financial_status': order_response.get(
                        "financial_status")})
                context.update({'order_data_line': order_data_line})
                sale_order = sale_order.with_context(**context)

                # Resolve created_by once — avoids repeating the two-level chain.
                queue = order_data_line and order_data_line.shopify_order_data_queue_id
                created_by = 'Scheduled Action' if (queue and queue.created_by == "scheduled_action") \
                    else self.env.user.name

                if sale_order.shopify_order_status == "fulfilled":
                    sale_order.auto_workflow_process_id.shipped_order_workflow_ept(sale_order)
                    # Below code add for create partially/fully refund
                    message = self.create_shipped_order_refund(shopify_financial_status, order_response, sale_order,
                                                               created_by)
                    if instance.use_graphql_api:
                        message = self.check_and_create_return_picking(order_response, sale_order, created_by)
                elif not sale_order.is_risky_order:
                    if sale_order.shopify_order_status == "partial":
                        sale_order.process_order_fullfield_qty(order_response)
                        sale_order.with_context(shopify_order_financial_status=order_response.get(
                            "financial_status")).process_orders_and_invoices_ept()
                        # Below code add for create partially/fully refund
                        message = self.create_shipped_order_refund(shopify_financial_status, order_response, sale_order,
                                                                   created_by)
                    else:
                        sale_order.with_context(shopify_order_financial_status=order_response.get(
                            "financial_status")).process_orders_and_invoices_ept()


            except Exception as error:
                if order_data_line:
                    order_data_line.write({"state": "failed", "processed_at": datetime.now(),
                                           "sale_order_id": sale_order.id})
                message = "Receive error while process auto invoice workflow, Error is:  (%s)" % (error)
                _logger.info(message)
                common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                               message=message,
                                                               model_name=self._name,
                                                               order_ref=order_response.get("name"),
                                                               shopify_order_data_queue_line_id=order_data_line.id if order_data_line else False)
                continue
            _logger.info("Done auto workflow process for Odoo order(%s) and Shopify order is (%s)", sale_order.name,
                         order_number)

            if message:
                common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                               message=message,
                                                               model_name=self._name,
                                                               order_ref=order_response.get("name"),
                                                               shopify_order_data_queue_line_id=order_data_line.id if order_data_line else False)
                order_data_line.write({'state': 'failed', 'processed_at': datetime.now()})
            else:
                order_data_line.write({"state": "done", "processed_at": datetime.now(),
                                       "sale_order_id": sale_order.id, "order_data": False})
            _logger.info("Processed the Odoo Order %s process and Shopify Order (%s)", sale_order.name, order_number)

        return order_ids

    def set_metafield_values_in_order(self, order_response, instance, order):
        """
            This method is used to set metafield values in sale order.
        """
        mappings = self.env['shopify.metafield.config.ept'].get_shopify_metafield_configs(
            instance.id, ["sale.order"], 'import'
        )
        if not mappings or not order:
            return True

        order_metafields = order_response.get("metafields", []) #order_response['metafields']
        # if not order_metafields:
        #     return True

        metafield_dict = {(m["namespace"], m["key"]): m["value"] for m in order_metafields}
        vals = {}
        for mapping in mappings.filtered(lambda m: m.odoo_field_id and m.model_id.model == "sale.order"):
            key_tuple = (mapping.namespace, mapping.key)
            value = metafield_dict.get(key_tuple)
            field_name = mapping.odoo_field_id.name
            field_type = mapping.odoo_field_id.ttype

            if value is None:
                vals[field_name] = "" if field_type in ['char', 'text', 'html'] else 0 if field_type in ['float', 'integer', 'monetary'] else False
            else:
                vals[field_name] = mapping._convert_shopify_to_odoo(mapping.value_type, value, mapping.odoo_field_id)
        if vals:
            order.write(vals)
        return True
    
    

    def validate_and_paid_invoices_ept(self, work_flow_process_record):
        """
        According to the workflow configuration, It will create invoices, validate them and register payment.
        @param : work_flow_process_record: Record of auto invoice workflow.
        """
        self.ensure_one()
        if not self.shopify_instance_id:
            return super(SaleOrder, self).validate_and_paid_invoices_ept(work_flow_process_record)
        if work_flow_process_record.create_invoice:
            if work_flow_process_record.invoice_date_is_order_date:
                if self.check_fiscal_year_lock_date_ept():
                    return True
            invoiceable_lines = self.with_context(
                shopify_filter_already_invoiced_lines=True,
            )._get_invoiceable_lines(False)
            invoices = self.env['account.move']
            if invoiceable_lines:
                if work_flow_process_record.sale_journal_id:
                    invoices = self.with_context(
                        journal_ept=work_flow_process_record.sale_journal_id,
                        shopify_filter_already_invoiced_lines=True,
                    )._create_invoices(final=True)
                else:
                    invoices = self.with_context(
                        shopify_filter_already_invoiced_lines=True,
                    )._create_invoices(final=True)
            self.validate_invoice_ept(invoices)
            if self.shopify_instance_id and self.env.context.get(
                    'shopify_order_financial_status') and self.env.context.get(
                'shopify_order_financial_status') == 'pending':
                return False
            if work_flow_process_record.register_payment:
                self.paid_invoice_ept(invoices)
        return True

    def process_with_tracking_stock_move(self, stock_move):
        """
        This Method use for search lot and write to move line.
        @author: Nilam Kubavat @Emipro Technologies Pvt. Ltd on date 3rd July 2023.
        """
        # move_ids = order.order_line.move_ids.filtered(lambda m: m.state not in ['done', 'cancel'])
        # for stock_move in move_ids:
        for move_line in stock_move.move_line_ids:
            if move_line.product_id.tracking != 'none':
                if not move_line.lot_id:
                    lot_id = self.env['stock.lot'].search([('product_id', '=', move_line.product_id.id),
                                                           ('company_id', '=', self.company_id.id),
                                                           ('product_qty', '>', 0)],
                                                          limit=1)
                    if lot_id:
                        move_line.write({'lot_id': lot_id.id})
        # if stock_move.quantity_done == 0:
        stock_move.sudo()._action_assign()
        stock_move.sudo()._set_quantity_done(stock_move.product_uom_qty)
        try:
            stock_move.picked = True
            stock_move.with_context(is_connector=True)._action_done()
        except Exception as e:
            error_msg = "Stock move is not done for Order #%s | Product: [%s] %s | Reason: %s" % (
                self.shopify_order_number or self.name,
                stock_move.product_id.default_code or '',
                stock_move.product_id.name,
                str(e)
            )
            _logger.exception(error_msg)
            return error_msg
        return None

    def import_shopify_cancel_order(self, instance, from_date, to_date):
        """ This method is used if Shopify orders imported in odoo and after Shopify store in some orders are canceled
            then this method cancel imported orders and created a log note.
            @param : instance,from_date,to_date
            @return: True
            @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 16 March 2022.
            Task_id: 185873
        """
        shopify_order_data_queue_obj = self.env["shopify.order.data.queue.ept"]
        instance.connect_in_shopify()
        order_ids = shopify_order_data_queue_obj.shopify_order_request(instance, from_date, to_date, order_type="any")
        for order in order_ids:
            order_data = order.to_dict()
            if order_data.get('cancel_reason'):
                message = ""
                if order_data.get('cancel_reason') == "customer":
                    message = "Customer changed/canceled Order"
                elif order_data.get('cancel_reason') == "fraud":
                    message = "Fraudulent order"
                elif order_data.get('cancel_reason') == "inventory":
                    message = "Items unavailable"
                elif order_data.get('cancel_reason') == "declined":
                    message = "Payment declined"
                elif order_data.get('cancel_reason') == "other":
                    message = "Other"
                sale_order = self.search_existing_shopify_order(order_data, instance, order_data.get("order_number"))
                if sale_order and sale_order.state != 'cancel':
                    sale_order.write({'canceled_in_shopify': True})
                    sale_order.message_post(
                        body=_("The reason for the order cancellation on this Shopify store is that %s.", message))
                    sale_order.cancel_shopify_order()
        instance.last_cancel_order_import_date = to_date - timedelta(days=2)
        return True

    def create_shipped_order_refund(self, shopify_financial_status, order_response, sale_order, created_by):
        """ This method is used to create partially or fully refund in shopify order.
            @param : self
            @return: message
            @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 27 November 2021 .
            Task_id: 179249
        """
        message = ""
        if shopify_financial_status in ("refunded", "partially_refunded") and order_response.get("refunds"):
            is_need_create_refund = False
            for refund in order_response.get('refunds'):
                for transaction in refund.get('transactions'):
                    if transaction.get('kind') == 'refund' and transaction.get('status') == 'success':
                        is_need_create_refund = True

            if is_need_create_refund:
                message = sale_order.create_shopify_partially_refund(order_response.get("refunds"),
                                                                     order_response.get('name'), created_by,
                                                                     shopify_financial_status)
            self.prepare_vals_shopify_multi_payment_refund(order_response.get("refunds"), sale_order)
        return message

    def prepare_vals_shopify_multi_payment_refund(self, order_refunds, order):
        """ This method is used to manage multi payment wise remaining refund amount.
            @param : order_refunds,order
            @return: True
            @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 15 Feb 2022.
            Task_id: 183797
        """
        for refund in order_refunds:
            for transaction in refund.get('transactions'):
                for payment_record in order.shopify_payment_ids:
                    if payment_record.payment_gateway_id.name == transaction.get('gateway'):
                        total_amount = payment_record.remaining_refund_amount - float(transaction.get('amount'))
                        payment_record.write({'remaining_refund_amount': abs(total_amount)})
        return True

    def search_existing_shopify_order(self, order_response, instance, order_number):
        """ This method is used to search the existing shopify order.
            @param : self
            @return: sale_order
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 27 October 2020 .
            Task_id: 167537
        """

        sale_order = self.search([("shopify_order_id", "=", order_response.get("id")),
                                  ("shopify_instance_id", "=", instance.id),
                                  ("shopify_order_number", "=", order_number)])
        if not sale_order:
            sale_order = self.search([("shopify_instance_id", "=", instance.id),
                                      ("client_order_ref", "=", order_response.get("name"))])

        return sale_order

    def check_mismatch_details(self, lines, instance, order_number, order_data_queue_line):
        """This method used to check the mismatch details in the order lines.
            @param : self, lines, instance, order_number, order_data_queue_line
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 11/11/2019.
            Task Id : 157350
        """
        shopify_product_template_obj = self.env["shopify.product.template.ept"]
        common_log_line_obj = self.env["common.log.lines.ept"]
        mismatch = False

        for line in lines:
            shopify_variant = self.search_shopify_variant(line, instance)
            if shopify_variant:
                continue
            # Below lines are used for the search gift card product, Task 169381.
            if line.get('gift_card', False) or line.get('is_gift_card', False):
                product = instance.gift_card_product_id or False
                if product:
                    continue
                message = ("System tried to import the order: %s but in the order there are Gift card details but "
                           "Gift card products has been deleted\n"
                           "Action items:\n"
                           "- Upgrade the Shopify connector in odoo. so it will create Gift Card product in odoo\n"
                           "- Try to reprocess the order data queue ") % order_number
                common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                               message=message,
                                                               model_name='sale.order', order_ref=order_number,
                                                               shopify_order_data_queue_line_id=order_data_queue_line.id if order_data_queue_line else False)
                mismatch = True
                break

            if not shopify_variant:
                line_variant_id = line.get("variant_id", False)
                line_product_id = line.get("product_id", False)
                product_sku = line.get("sku")
                create_product_not_exist_log = False
                if line_product_id and line_variant_id:
                    shopify_product_template_obj.shopify_sync_products(False, line_product_id,
                                                                       instance,
                                                                       order_data_queue_line)
                    shopify_variant = self.search_shopify_variant(line, instance)
                    if not shopify_variant:
                        mismatch = True
                        create_product_not_exist_log = True
                elif not line_product_id and not line_variant_id and product_sku:
                    odoo_product = self.env["product.product"].search(
                        [("default_code", "=", product_sku)], limit=1
                    ) or self.env["shopify.product.product.ept"].search(
                        [
                            ("shopify_instance_id", "=", instance.id),
                            ("default_code", "=", product_sku),
                        ],
                        limit=1,
                    ).product_id
                    if not odoo_product:
                        mismatch = True
                        create_product_not_exist_log = True
                if create_product_not_exist_log:
                    message = "Product [%s][%s] not found for Order %s" % (
                        product_sku, line.get("name"), order_number)
                    common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                   module="shopify_ept", message=message,
                                                                   model_name='sale.order', order_ref=order_number,
                                                                   shopify_order_data_queue_line_id=order_data_queue_line.id if order_data_queue_line else False)
                    break
        return mismatch

    def search_shopify_variant(self, line, instance):
        """ This method is used to search the Shopify variant.
            :param line: Response of order line.
            @return: shopify_variant.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 19 October 2020 .
            Task_id: 167537
        """
        shopify_variant = False
        shopify_product_obj = self.env["shopify.product.product.ept"]
        sku = line.get("sku") or False
        if line.get("variant_id", None):
            shopify_variant = shopify_product_obj.search(
                [("variant_id", "=", line.get("variant_id")),
                 ("shopify_instance_id", "=", instance.id), ('exported_in_shopify', '=', True)])
        if not shopify_variant and sku:
            shopify_variant = shopify_product_obj.search(
                [("default_code", "=", sku),
                 ("shopify_instance_id", "=", instance.id), ('exported_in_shopify', '=', True)])
        return shopify_variant

    def _search_payment_gateway_ept(self, order_response):
        """
        :param order_response: dict – raw Shopify order payload.
        :return: str – gateway name.
        """
        gateway = "no_payment_gateway"
        payment_gateway_names = order_response.get('payment_gateway_names')
        if payment_gateway_names and payment_gateway_names[0]:
            if len(payment_gateway_names) == 1:
                gateway = payment_gateway_names[0]
            else:
                for transaction in order_response.get('transaction') or []:
                    if "Cash on Delivery" in transaction.get("gateway", ""):
                        gateway = transaction.get("gateway")
                    elif (transaction.get('gateway') != 'gift_card'
                          and transaction.get("status") == 'success'):
                        gateway = transaction.get("gateway")
                if self.env.context.get('shopify_instance') and self.env.context.get('shopify_queue_line') and self.env.context.get('shopify_order'):
                    instance = self.env.context.get('instance')
                    queue_line = self.env.context.get('queue_line')
                    order = self.env.context.get('order')
                    order.check_update_payment_workflow(instance, queue_line, order_response)
        return gateway

    def shopify_create_order(self, instance, partner, shipping_address, invoice_address,
                             order_data_queue_line, order_response, lines, order_number):
        """This method used to create a sale order and it's line.
            @param : self, instance, partner, shipping_address, invoice_address,order_data_queue_line, order_response
            @return: order
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 12/11/2019.
            Task Id : 157350
            @change : if configuration on for Create delivery fee than going to create order line for delivery fee
            @change by : Nilam Kubavat at 09-Aug-2022 for task id : 197829
        """
        payment_gateway_obj = self.env["shopify.payment.gateway.ept"]
        gateway = self._search_payment_gateway_ept(order_response)
        shopify_market = self._get_market_for_order_ept(instance, order_response)

        # if shopify_market:
        #     payment_gateway, workflow, payment_term = \
        #         payment_gateway_obj.shopify_search_create_gateway_workflow_market(instance, order_data_queue_line, order_response,
        #                                                                 gateway,shopify_market)
        # else:
        #     payment_gateway, workflow, payment_term = \
        #         payment_gateway_obj.shopify_search_create_gateway_workflow(instance, order_data_queue_line, order_response,
        #                                                                 gateway)
        payment_gateway, workflow, payment_term = self._resolve_payment_gateway_workflow_ept(instance,
                                                                                             order_data_queue_line,
                                                                                             order_response,
                                                                                             gateway, shopify_market)


        if not all([payment_gateway, workflow]):
            return False

        order_vals = self.prepare_shopify_order_vals(instance, partner, shipping_address,
                                                     invoice_address, order_response,
                                                     payment_gateway,
                                                     workflow,shopify_market)
                                                     
        order_vals.update({'payment_term_id': payment_term and payment_term.id or False})
        is_create_order = self.check_sale_order_validation(instance, order_response, order_vals, order_data_queue_line)
        if not is_create_order:
            return False
        if len(order_response.get('payment_gateway_names')) > 1:
            payment_vals = self.prepare_vals_shopify_multi_payment(instance, order_data_queue_line, order_response,
                                                                   payment_gateway, workflow, shopify_market)
            if not payment_vals:
                message = (f"No payment vals Found Order transaction for Shopify Order({order_number}) for multiple"
                           f" payment. Payment might be pending or fully refunded.")
                _logger.info(message)
            order_vals.update({'shopify_payment_ids': payment_vals, 'is_shopify_multi_payment': True})
        payments = []
        if len(order_response.get('payment_gateway_names')) > 1 and order_response.get('financial_status') != 'voided':
            for transaction in order_response.get('transaction'):
                if "Cash on Delivery" in transaction.get("gateway") or transaction.get('status') == 'success':
                    payments.append(transaction.get("gateway"))
            if len(payments) > 1:
                payment_vals = self.prepare_vals_shopify_multi_payment(instance, order_data_queue_line, order_response,
                                                                       payment_gateway, workflow, shopify_market)
                if not payment_vals:
                    message = (f"No payment vals Found Order transaction for Shopify Order({order_number}) for "
                                 f"multiple payment. Payment might be pending or fully refunded.")
                    _logger.info(message)
                order_vals.update({'shopify_payment_ids': payment_vals, 'is_shopify_multi_payment': True})

        # ── Multi-company: create order in the correct company context ────────
        target_company_id = order_vals.get("company_id")
        if target_company_id and target_company_id != self.env.company.id:
            order = self.with_company(target_company_id).create(order_vals)
        else:
            order = self.create(order_vals)

        # Extract channel info from order_response and pass via context to all
        # line-creation helpers so no DB field is needed on sale.order.
        channel_info = order_response.get("channel_information") or order_response.get("channelInformation") or {}
        channel_def = channel_info.get("channel_definition") or channel_info.get("channelDefinition") or {}
        channel_handle = channel_def.get("handle") or False
        # app_id is the reliable cross-type identifier: channel.app.id == order channelInformation.app.id
        channel_app_raw = channel_info.get("app") or {}
        channel_app_id = channel_app_raw.get("id") or False
        if channel_app_id and str(channel_app_id).startswith("gid://"):
            try:
                channel_app_id = int(str(channel_app_id).split("/")[-1])
            except Exception:
                pass
            
        order_with_ctx = order.with_context(
            shopify_channel_handle=channel_handle,
            shopify_channel_app_id=channel_app_id,
        )

        _logger.info("Creating order lines for Odoo order(%s) and Shopify order is (%s).", order.name, order_number)
        order_with_ctx.create_shopify_order_lines(lines, order_response, instance)

        _logger.info("Created order lines for Odoo order(%s) and Shopify order is (%s)", order.name, order_number)

        order_with_ctx.create_shopify_shipping_lines(order_response, instance)
        _logger.info("Created Shipping lines for order (%s).", order.name)

        if instance.is_delivery_fee:
            order_with_ctx.create_shopify_Delivery_Fee_lines(order_response, instance)
            _logger.info("Created Delivery Fee for order (%s).", order.name)

        if instance.is_delivery_multi_warehouse:
            self.set_line_warehouse_based_on_location(order, instance, order_response)
        # self.set_fulfilment_order_id_and_fulfillment_line_id(order, instance, order_response)
        return order
    
    def _resolve_payment_gateway_workflow_ept(self, instance, order_data_queue_line, order_response, gateway,
                                              shopify_market=False):
        """
        Resolve payment gateway, workflow, and payment term for Shopify order import.

        :param instance: shopify.instance.ept record
        :param order_data_queue_line: shopify.order.data.queue.line.ept record
        :param order_response: dict (Shopify order payload)
        :param gateway: str (gateway name from Shopify order/transaction)
        :param shopify_market: shopify.market.ept record (optional)
        :return: tuple(payment_gateway, workflow, payment_term)
        """        
        if not shopify_market and instance.is_shopify_market_enabled:            
            common_log_line_obj = self.env["common.log.lines.ept"]
            error_messages = []
            error_messages.append(
                " Shopify Market could not be determined for this order. The system attempted to find a matching market based on the order's attributes (e.g., shipping address, currency) but was unsuccessful. \n"
                "Action Items:\n"
                "- Review the order's shipping address and currency to ensure they match the criteria defined in your Shopify Market configurations.\n"
            )
            for message in error_messages:
                common_log_line_obj.create_common_log_line_ept(
                    shopify_instance_id=instance.id,
                    message=message,
                    module="shopify_ept",
                    model_name='sale.order',
                    order_ref=order_response.get('name'),
                    shopify_order_data_queue_line_id=order_data_queue_line.id if order_data_queue_line else False
                )
            return False, False, False
        payment_gateway_obj = self.env["shopify.payment.gateway.ept"]
        if shopify_market:
            return payment_gateway_obj.shopify_search_create_gateway_workflow_market(
                instance, order_data_queue_line, order_response, gateway, shopify_market
            )
        
        return payment_gateway_obj.shopify_search_create_gateway_workflow(
            instance, order_data_queue_line, order_response, gateway
        )



    def check_sale_order_validation(self, instance, order_response, order_vals, order_data_queue_line):
        """
        This method use for Check customer, Order Date, price list, warehouse and picking policy available in Order
        Response.
        :param order_vals:
        @author: Yagnik Joshi on Date 28-12-2023.
        """
        is_create_order = True
        common_log_line_obj = self.env["common.log.lines.ept"]
        error_messages = []

        if order_response.get('shipping_lines', []):
            shipping_product = instance.shipping_product_id

            if not shipping_product:
                is_create_order = False
                error_messages.append(
                    " When creating a new delivery method, the system encountered an issue as it could not find the shipping product in the instance configuration.  " \
                    " \n - This resulted in the failure of the system to create the new delivery method. \n - To resolve this issue, please follow these steps: %s." \
                    " \n 1 Go to Shopify >> Instance >> Default Products.  \n 2 Review whether the shipping product is set. \n 3 If already set, ensure that it is active in Odoo. ")

        if not order_vals.get('pricelist_id'):
            is_create_order = False
            error_messages.append(
                " The order import operation failed because the price list configuration was not found in the instance configuration. " \
                " \n To resolve this issue, navigate to Shopify >> Configuration >> Settings, select instance and configure Instance Price list")

        if not order_vals.get('warehouse_id'):
            is_create_order = False
            error_messages.append(
                " The order import operation failed because the warehouse configuration was not found in the instance configuration. " \
                " \n To resolve this issue, navigate to Shopify >> Configuration >> Settings, select instance and configure warehouse")

        if not order_vals.get('picking_policy'):
            is_create_order = False
            error_messages.append(
                " The order import operation failed because the shipping policy configuration was not found in the auto invoice workflow configuration. " \
                " \n To resolve this issue, navigate to Shopify >> Configuration >> Financial Status. Review whether Auto workflow is configured, and within Auto workflow, ensure that the shipping policy is also configured.")

        # Create a log for each error message
        for message in error_messages:
            common_log_line_obj.create_common_log_line_ept(
                shopify_instance_id=instance.id,
                message=message,
                module="shopify_ept",
                model_name='sale.order',
                order_ref=order_response.get('name'),
                shopify_order_data_queue_line_id=order_data_queue_line.id if order_data_queue_line else False
            )

        return is_create_order

    def set_fulfilment_order_id_and_fulfillment_line_id(self, order, picking):
        """
        This method sets order line warehouse based on Shopify Location.
        @author:Meera Sidapara @Emipro Technologies Pvt. Ltd on date 07 September 2022.
        Task Id : 199989 - Fulfillment location wise order
        """
        shopify_order_id = order.shopify_order_id
        move_ids = picking.move_ids
        stock_moves = move_ids.filtered(lambda move: move.shopify_fulfillment_line_id)
        backorders = picking.backorder_ids.filtered(lambda order: not order.updated_in_shopify)
        if stock_moves and backorders:
            self.set_backorder_fulfillment_data(backorders, stock_moves)
        fulfillment_order_data = []
        fulfillment_order = False
        if not stock_moves:
            try:
                fulfillment_order = shopify.fulfillment.FulfillmentOrders.find(order_id=int(shopify_order_id))
                for order_data in fulfillment_order:
                    order_data = order_data.to_dict()
                    if order_data.get('status') != 'closed':
                        fulfillment_order_data.append(order_data)
            except Exception as error:
                _logger.info("Error in Request of shopify fulfillment order for the fulfilment. Error: %s", error)
            for data in fulfillment_order_data:
                for line in data.get('line_items'):
                    if isinstance(data.get('delivery_method'), dict) and data.get('delivery_method').get(
                            'method_type') == 'none':
                        order_line = order.order_line.filtered(
                            lambda line_item: line_item.shopify_line_id == str(line.get('line_item_id')))
                        if order_line:
                            order_line.write(
                                {'shopify_fulfillment_order_id': line.get('fulfillment_order_id'),
                                 'shopify_fulfillment_line_id': line.get('id'),
                                 'shopify_fulfillment_order_status': data.get('status')})
                            self.env.cr.commit()
                        # continue
                    stock_move = move_ids.filtered(
                        lambda move: not move.shopify_fulfillment_line_id and move.sale_line_id
                                     and move.sale_line_id.shopify_line_id == str(line.get('line_item_id')))
                    if stock_move:
                        stock_move.write(
                            {'shopify_fulfillment_order_id': line.get('fulfillment_order_id'),
                             'shopify_fulfillment_line_id': line.get('id'),
                             'shopify_fulfillment_order_status': data.get('status')})
                        self.env.cr.commit()
                        if backorders:
                            self.set_backorder_fulfillment_data(backorders, stock_move)
        return fulfillment_order

    def set_backorder_fulfillment_data(self, backorder, stock_moves):
        """
        This method sets backorder Fulfillment data.
        @author: Nilam Kubavat @Emipro Technologies Pvt. Ltd on date 08 August 2023.
        Task Id : 240507
        """
        for stock_move in stock_moves.filtered(lambda move: move.shopify_fulfillment_order_status == 'in_progress'):
            backorder_move = backorder.move_ids.filtered(
                lambda move_line: not move_line.shopify_fulfillment_line_id
                                  and move_line.product_id.id == stock_move.product_id.id)
            backorder_move.write({'shopify_fulfillment_order_id': stock_move.shopify_fulfillment_order_id,
                                  'shopify_fulfillment_line_id': stock_move.shopify_fulfillment_line_id,
                                  'shopify_fulfillment_order_status': stock_move.shopify_fulfillment_order_status})
        return True

    def set_line_warehouse_based_on_location(self, order, instance, order_response):
        """
        This method sets order line warehouse based on Shopify Location.
        @author:Meera Sidapara @Emipro Technologies Pvt. Ltd on date 07 September 2022.
        Task Id : 199989 - Fulfillment location wise order
        """
        shopify_location_obj = self.env['shopify.location.ept']
        shopify_order_id = order.shopify_order_id
        fulfillment_data = order_response.get('fulfillment_data', []) or order_response.get('fulfillment_orders', [])
        if not fulfillment_data:
            shopify_order = shopify.Order().find(shopify_order_id)
            try:
                order_response["fulfillment_data"] = shopify_order.get('fulfillment_orders')
            except ClientError as error:
                if hasattr(error,
                           "response") and error.response.code == 429 and error.response.msg == "Too Many Requests":
                    time.sleep(int(float(error.response.headers.get('Retry-After', SHOPIFY_RETRY_AFTER_DEFAULT))))
                    order_response["fulfillment_data"] = shopify_order.get('fulfillment_orders')
        fulfillment_data = order_response.get('fulfillment_data') or order_response.get('fulfillment_orders', [])
        for data in fulfillment_data:
            shopify_location_id = (data.get('assigned_location_id') or
                                   data.get('assigned_location', {}).get('location', {}).get('id'))
            line_item_ids = [str(line.get('line_item_id')) for line in data.get('line_items')]
            if 'None' in line_item_ids:
                line_item_ids = [str(line.get('line_item').get('id')) for line in data.get('line_items')]
            order_line = order.order_line.filtered(lambda line_item: line_item.shopify_line_id in line_item_ids)
            line_warehouse_id = shopify_location_obj.search(
                [('shopify_location_id', '=', shopify_location_id)]).warehouse_for_order
            order_line.write(
                {'warehouse_id_ept': line_warehouse_id.id if line_warehouse_id else instance.shopify_warehouse_id.id})
        return True

    def create_shopify_Delivery_Fee_lines(self, order_response, instance):
        """
        Creates Delivery Fee lines for shopify orders.
        @author: Nilam Kubavat @Emipro Technologies Pvt. Ltd on date 09-Aug-2022
        Task Id : 197829
        """
        shipping_product = instance.shipping_product_id
        for line in order_response.get("tax_lines", []):
            if line.get('title') == instance.delivery_fee_name:
                delivery_fee_price = line.get("price")
                if instance.order_visible_currency:
                    delivery_fee_price = self.get_price_based_on_customer_visible_currency(line.get("price_set"),
                                                                                           order_response,
                                                                                           delivery_fee_price)
                order_line = self.shopify_create_sale_order_line(line, shipping_product, 1,
                                                                 line.get('title'),
                                                                 delivery_fee_price,
                                                                 order_response)
                order_line.name = line.get('title')

    def _get_market_for_order_ept(self, instance, order_response):
        """
        Identify the Shopify market for an order using priority lookup:
        1. Country code from billing_address
        2. Presentment / shop currency (via market's own pricelist currency)
        3. Primary market
        4. None → caller uses instance defaults

        :return: shopify.market.ept record or empty recordset
        """
        market_obj = self.env["shopify.market.ept"]
        if instance.is_shopify_market_enabled:            
            country_code = (order_response.get("billing_address") or {}).get("country_code", "")
            currency_code = (
                order_response.get("presentment_currency")
                or order_response.get("currency")
                or ""
            )
            return market_obj.find_market_for_order_ept(
                instance,
                country_code=country_code,
                currency_code=currency_code,
            )
        return market_obj

    def prepare_shopify_order_vals(self, instance, partner, shipping_address,
                                   invoice_address, order_response, payment_gateway,
                                   workflow, shopify_market=False):
        """
        This method used to Prepare a order vals.
        Market-aware: if a shopify.market.ept record is found, its company,
        warehouse, pricelist and fiscal position override the instance defaults.
        @param : self, instance, partner, shipping_address,invoice_address, order_response, payment_gateway,workflow
        @return: order_vals
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13/11/2019.
        Task Id : 157350

        Market behavior:
        - If a market is identified and it has `company_id`, the order uses that market's
          company-specific configuration (warehouse, pricelist, fiscal position).
        - If a market is found but not fully configured (no `company_id`), the method
          safely falls back to instance defaults.
        - This ensures backward compatibility while enabling market-wise segregation.
        """
        date_order = self.convert_order_date(order_response)

        # ── Market lookup ────────────────────────────────────────────────────
        # Returns a shopify.market.ept record if any markets are synced and one
        # matches the order's country/currency. Returns empty recordset otherwise.
        # The condition `market and market.company_id` guards ALL market overrides —
        # if a market is found but has no company configured, the instance defaults
        # are used exactly as before (full backward-compatibility).
        if not shopify_market:
            shopify_market = self._get_market_for_order_ept(instance, order_response)

        # Resolve company, warehouse, pricelist, fiscal position from market or instance.
        # IMPORTANT: only override when market.company_id is explicitly set.
        # A market without company_id = not fully configured → use instance defaults.
        shopify_market_id = False
        if shopify_market and shopify_market.company_id:
            # Market is configured with a company — use ALL configuration from the market.
            # Each field must be explicitly set on the market by the admin.
            # If a required field is missing, leave it False so that
            # check_sale_order_validation() will catch it and fail the queue line.
            company_id = shopify_market.company_id.id
            warehouse_id = shopify_market.warehouse_id.id if shopify_market.warehouse_id else False
            pricelist_id = shopify_market.pricelist_id if shopify_market.pricelist_id else False
            fiscal_position_id = shopify_market.fiscal_position_id.id if shopify_market.fiscal_position_id else False
            shopify_market_id = shopify_market.id
        else:
            # No market or unconfigured market → full original behaviour
            company_id = instance.shopify_company_id.id if instance.shopify_company_id else False
            warehouse_id = instance.shopify_warehouse_id.id if instance.shopify_warehouse_id else False
            pricelist_id = self.shopify_set_pricelist(order_response=order_response, instance=instance)
            fiscal_position_id = False

        # ── Market Auto Workflow override ─────────────────────────────────────
        # If the market has its own auto workflow configured, use it instead of
        # the workflow resolved from the payment gateway / financial status mapping.
        # This allows different markets (e.g., US vs EU) to have different
        # invoicing/payment policies.
        # effective_workflow = (
        #     market.auto_workflow_id if market and market.auto_workflow_id else workflow
        # )

        ordervals = {
            "company_id": company_id,
            "partner_id": partner.ids[0],
            "partner_invoice_id": invoice_address.ids[0],
            "partner_shipping_id": shipping_address.ids[0],
            "warehouse_id": warehouse_id,
            "date_order": date_order,
            "state": "draft",
            "pricelist_id": pricelist_id.id if pricelist_id else False,
            "team_id": instance.shopify_section_id.id if instance.shopify_section_id else False,
            "shopify_market_id": shopify_market_id,
        }

        if fiscal_position_id:
            ordervals["fiscal_position_id"] = fiscal_position_id

        order_response_vals = self.prepare_order_vals_from_order_response(order_response, instance, workflow,
                                                                          payment_gateway)
        ordervals.update(order_response_vals)
        if not instance.is_use_default_sequence:
            if instance.shopify_order_prefix:
                name = "%s_%s" % (instance.shopify_order_prefix, order_response.get("name"))
            else:
                name = order_response.get("name")
            ordervals.update({"name": name})
        return ordervals

    def create_or_search_sale_tag(self, tag):
        crm_tag_obj = self.env['crm.tag']
        exists_tag = crm_tag_obj.search([('name', '=ilike', tag)], limit=1)
        if not exists_tag:
            exists_tag = crm_tag_obj.create({'name': tag})
        return exists_tag.id

    def convert_order_date(self, order_response):
        """ This method is used to convert the order date in UTC and formate("%Y-%m-%d %H:%M:%S").
            :param order_response: Order response
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 19 October 2020 .
            Task_id: 167537
        """
        if order_response.get("created_at", False):
            order_date = order_response.get("created_at", False)
            date_order = parser.parse(order_date).astimezone(utc).strftime("%Y-%m-%d %H:%M:%S")
        else:
            date_order = time.strftime("%Y-%m-%d %H:%M:%S")
            date_order = str(date_order)

        return date_order

    def prepare_order_vals_from_order_response(self, order_response, instance, workflow, payment_gateway):
        """ This method is used to prepare vals from the order response.
            :param order_response: Response of order.
            :param instance: Record of instance.
            :param workflow: Record of auto invoice workflow.
            :param payment_gateway: Record of payment gateway.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 19 October 2020 .
            Task_id: 167537
            @change : pass tag_ids on vals by Nilam Kubavat for task id : 190111 at 19/05/2022
        """
        utm_source, utm_medium, utm_campaign = self.set_utm_source_medium_campaign(order_response)
        channel_info = order_response.get("channel_information") or order_response.get("channelInformation") or {}
        channel_def = channel_info.get("channel_definition") or channel_info.get("channelDefinition") or {}
        channel_handle = channel_def.get("handle") or False
        channel_app_raw = channel_info.get("app") or {}
        channel_app_id = channel_app_raw.get("id") or False
        if channel_app_id and str(channel_app_id).startswith("gid://"):
            try:
                channel_app_id = int(str(channel_app_id).split("/")[-1])
            except Exception:
                pass
        shopify_sales_channel = self.env['shopify.channel.ept'].search(
            [('shopify_instance_id', '=', instance.id), '|', ('app_id', '=', str(channel_app_id)),
             ('handle', '=', channel_handle), ], order='app_id desc', limit=1)
        tag_ids = []
        if order_response.get("tags"):
            tags_data = order_response.get("tags")
            normalized_tags_list = []
            if isinstance(tags_data, str):
                if tags_data.strip():
                    normalized_tags_list = [tag.strip() for tag in tags_data.split(',')]
            elif isinstance(tags_data, list):
                normalized_tags_list = [tag.strip() for tag in tags_data if tag and tag.strip()]
            for tag in normalized_tags_list:
                tag_ids.append(self.create_or_search_sale_tag(tag))
        order_vals = {
            "checkout_id": order_response.get("checkout_id"),
            "note": order_response.get("note") if order_response.get("note") else '',
            "shopify_order_id": order_response.get("id"),
            "shopify_order_number": order_response.get("order_number"),
            "shopify_payment_gateway_id": payment_gateway and payment_gateway.id or False,
            "shopify_instance_id": instance.id,
            "shopify_order_status": order_response.get("fulfillment_status") or "unfulfilled",
            "picking_policy": workflow.picking_policy or False,
            "auto_workflow_process_id": workflow and workflow.id,
            "client_order_ref": order_response.get("name"),
            # "analytic_account_id": instance.shopify_analytic_account_id.id if instance.shopify_analytic_account_id else False,
            "tag_ids": tag_ids,
            "source_id": utm_source and utm_source.id or False,
            "medium_id": utm_medium and utm_medium.id or False,
            "campaign_id": utm_campaign and utm_campaign.id or False,
            "is_buy_with_prime_order": order_response.get("buy_with_prime") or False,
            "origin": order_response.get("source_identifier") or False,
            "source_link": order_response.get("source_url") or False,
            "shopify_channel_id": shopify_sales_channel.id if shopify_sales_channel else False,
        }
        if self.env["ir.config_parameter"].sudo().get_param("shopify_ept.use_default_terms_and_condition_of_odoo"):
            order_vals = self.prepare_order_note_with_customer_note(order_vals)
        return order_vals

    def create_and_done_stock_move_ept(self, order_line, customers_location, bom_line=False, vendor_location=False):
        """
        Based on the order line, it will create a stock move and done it.
        @param : order_line: Single record of sale order line.
        @param : customers_location: Browsable record of Customer location.
        @param : bom_line: If mrp is install and product has kit type then pass the bom lines of it.
        @param : vendor_location: Browsable record of vendor location.
        """
        if not self.shopify_instance_id:
            return super(SaleOrder, self).create_and_done_stock_move_ept(order_line, customers_location, bom_line,
                                                                         vendor_location)
        if bom_line:
            product = bom_line[0].product_id
            product_qty = bom_line[1].get('qty', 0) * order_line.product_uom_qty
            product_uom = bom_line[0].product_uom_id
        else:
            product = order_line.product_id
            product_qty = order_line.product_uom_qty
            product_uom = order_line.product_uom_id

        if product and product_qty and product_uom:
            vals = self.prepare_val_for_stock_move_ept(product, product_qty, product_uom, vendor_location,
                                                       customers_location, order_line, bom_line)
            queue_id = self.env.context.get('active_ids')
            if not queue_id and 'order_data_line' in self.env.context:
                line_record = self.env.context.get('order_data_line')
                queue_id = [line_record.shopify_order_data_queue_id.id]
            order_response_data = {}
            if queue_id:
                order_data_line = self.env['shopify.order.data.queue.line.ept'].search(
                    [('shopify_order_data_queue_id', 'in', queue_id), ('shopify_order_id', '=', self.shopify_order_id)],
                    limit=1)
                if order_data_line and order_data_line.order_data:
                    order_response_data = json.loads(order_data_line.order_data)
            fulfillments = order_response_data.get('fulfillments', [])
            for fulfillment in fulfillments:
                shopify_line_item_ids = [str(line.get('id')) for line in fulfillment.get('line_items', []) if
                                         line.get('id')]
                if not shopify_line_item_ids:
                    shopify_line_item_ids = [str(line.get('line_item').get('id')) for line in
                                             fulfillment.get('fulfillment_line_items', []) if
                                             line.get('line_item') and line.get('line_item').get('id')]
                if not order_line.shopify_line_id or order_line.shopify_line_id not in shopify_line_item_ids:
                    continue
                # Get tracking company: REST uses 'tracking_company' field; GraphQL uses 'tracking_info[].company'
                tracking_company = fulfillment.get('tracking_company') or next(
                    (info.get('company') for info in fulfillment.get('tracking_info', []) if info.get('company')),
                    None)
                if tracking_company:
                    carrier = self.env['delivery.carrier'].search([('shopify_tracking_company', '=', tracking_company),
                                                                   ('company_id', 'in',
                                                                    [self.shopify_instance_id.shopify_company_id.id,
                                                                     False])], limit=1)
                    if carrier:
                        vals.update({'carrier_id': carrier.id})
                # Get tracking number: REST uses 'tracking_number' field; GraphQL uses 'tracking_info[].number'
                tracking_number = fulfillment.get('tracking_number') or next(
                    (info.get('number') for info in fulfillment.get('tracking_info', []) if info.get('number')),
                    None)
                if tracking_number:
                    vals.update({'tracking_reference': tracking_number})
                break
            stock_move = self.env['stock.move'].create(vals)
            stock_move.reference = _('Auto processed move : %s') % product.display_name
            stock_move._action_assign()
            stock_move._set_quantity_done(product_qty)
            if stock_move.state != "assigned" and self.is_buy_with_prime_order and not self.shopify_instance_id.Force_transfer_move_of_buy_with_prime_orders:
                return True
            if product.tracking == 'none':
                stock_move.sudo().picked = True
                stock_move.with_context(is_connector=True)._action_done()
            else:
                res = self.process_with_tracking_stock_move(stock_move)
                if res is not None:
                    order_data_line = self.env.context.get('order_data_line')
                    product_ref = '[%s] %s' % (product.default_code,
                                               product.name) if product.default_code else product.name
                    message = 'Stock move is not done for Order %s | Product: %s | Reason: %s' % (self.name,
                                                                                                  product_ref, res)
                    self.env["common.log.lines.ept"].create_common_log_line_ept(
                        shopify_instance_id=self.shopify_instance_id.id, module="shopify_ept",
                        message=message,
                        model_name='sale.order', order_ref=self.shopify_order_id,
                        shopify_order_data_queue_line_id=order_data_line.id if order_data_line else False)
                    order_data_line.write({'state': 'failed', 'processed_at': datetime.now()})
        return True

    def set_utm_source_medium_campaign(self, order_response):
        """
        This method use to find or create utm source medium and campaign.
        @author: Yagnik Joshi @Emipro Technologies Pvt. Ltd on date 24th Feb 2023.
        landing_site: The URL for the page where the buyer landed when they entered the shop.
        Task id: 218869
        """
        utm_source_obj = self.env['utm.source']
        utm_campaign_obj = self.env['utm.campaign']
        utm_medium_obj = self.env['utm.medium']
        utm_source = False
        utm_medium = False
        utm_campaign = False
        utm_dict = {}
        if order_response.get('landing_site') and order_response.get('landing_site').find('utm') >= 0:
            UTM_data = {sub for sub in order_response.get('landing_site')[1:-1].split("&")}
            for utm_split in UTM_data:
                if utm_split.find('utm') >= 0 and utm_split.find('=') >= 0:
                    utm_dict.update({utm_split.split("=")[0]: utm_split.split("=")[1]})

            if utm_dict.get('utm_source'):
                utm_source = self._find_or_create_utm_record(utm_source_obj, utm_dict.get('utm_source'))

            if utm_dict.get('utm_medium'):
                utm_medium = self._find_or_create_utm_record(utm_medium_obj, utm_dict.get('utm_medium'))

            if utm_dict.get('utm_campaign'):
                utm_campaign = self._find_or_create_utm_record(utm_campaign_obj, utm_dict.get('utm_campaign'))

        if not utm_source:
            utm_source = self.find_or_create_shopify_source(order_response.get('source_name'))

        return utm_source, utm_medium, utm_campaign

    def find_or_create_shopify_source(self, source):
        """
        This method is used to find or create shopify source in utm.source.
        @param source: Shopify order source
        @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 19 April 2022.
        Task_id: 187155
        """
        utm_source_obj = self.env['utm.source']
        source_id = utm_source_obj.search([('name', '=ilike', source)], limit=1)
        if not source_id:
            source_id = utm_source_obj.create({'name': source})
        return source_id

    def shopify_set_pricelist(self, instance, order_response):
        """
        Author:Bhavesh Jadav 09/12/2019 for the for set price list based on the order response currency because of if
        order currency different then the erp currency so we need to set proper pricelist for that sale order
        otherwise set pricelist based on instance configurations
        """
        currency_obj = self.env["res.currency"]
        pricelist_obj = self.env["product.pricelist"]
        order_currency = order_response.get(
            "presentment_currency") if instance.order_visible_currency else order_response.get("currency") or False
        if order_currency:
            currency = currency_obj.search([("name", "=", order_currency)])
            if instance.shopify_pricelist_id.currency_id.id == currency.id:
                return instance.shopify_pricelist_id
            if not currency:
                currency = currency_obj.search(
                    [("name", "=", order_currency), ("active", "=", False)])
            if currency:
                currency.write({"active": True})
                pricelist = pricelist_obj.search(
                    [("currency_id", "=", currency.id), ("company_id", "=", instance.shopify_company_id.id)],
                    limit=1)
                if pricelist:
                    return pricelist
                pricelist_vals = {"name": currency.name,
                                  "currency_id": currency.id,
                                  "company_id": instance.shopify_company_id.id}
                pricelist = pricelist_obj.create(pricelist_vals)
                return pricelist
            pricelist = pricelist_obj.search([("currency_id", "=", currency.id)], limit=1)
            return pricelist
        pricelist = instance.shopify_pricelist_id if instance.shopify_pricelist_id else False
        return pricelist

    def search_shopify_product_for_order_line(self, line, instance):
        """This method used to search shopify product for order line.
            @param : self, line, instance
            @return: shopify_product
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 14/11/2019.
            Task Id : 157350
        """
        shopify_product_obj = self.env["shopify.product.product.ept"]
        variant_id = line.get("variant_id")
        shopify_product = shopify_product_obj.search(
            [("shopify_instance_id", "=", instance.id), ("variant_id", "=", variant_id),
             ('exported_in_shopify', '=', True)], limit=1)
        if not shopify_product:
            shopify_product = shopify_product_obj.search([("shopify_instance_id", "=", instance.id),
                                                          ("default_code", "=", line.get("sku")),
                                                          ('exported_in_shopify', '=', True)], limit=1)
            shopify_product.write({"variant_id": variant_id})
        return shopify_product

    def shopify_create_sale_order_line(self, line, product, quantity, product_name, price,
                                       order_response, is_shipping=False, previous_line=False,
                                       is_discount=False, is_duties=False):
        """
        This method used to create a sale order line.
        @param : self, line, product, quantity,product_name, order_id,price, is_shipping=False
        @return: order_line_id
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 14/11/2019.
        Task Id : 157350
        """
        sale_order_line_obj = self.env["sale.order.line"]
        instance = self.shopify_instance_id
        line_vals = self.prepare_vals_for_sale_order_line(product, product_name, price, quantity)

        # Effective tax behaviour: market overrides instance if set
        market = self.shopify_market_id
        effective_tax_behaviour = (
            market.apply_tax_in_order if market and market.apply_tax_in_order
            else instance.apply_tax_in_order
        )

        order_line_vals = self.shopify_set_tax_in_sale_order_line(instance, line, order_response, is_shipping,
                                                                  is_discount, previous_line, line_vals,
                                                                  is_duties, effective_tax_behaviour)
        if is_discount:
            order_line_vals["name"] = "Discount for " + str(product_name)
            if previous_line:
                order_line_vals['shopify_related_line_id'] = f"discount_{previous_line.shopify_line_id}"
            if effective_tax_behaviour == "odoo_tax" and previous_line:
                # Inherit taxes from the parent order line using proper ORM command
                order_line_vals["tax_ids"] = [(6, 0, previous_line.tax_ids.ids)]

        if is_duties:
            order_line_vals["name"] = "Duties for " + str(product_name)
            if effective_tax_behaviour == "odoo_tax" and previous_line:
                order_line_vals["tax_ids"] = [(6, 0, previous_line.tax_ids.ids)]

        order_line_vals.update({
            "shopify_line_id": line.get("id"),
            "is_delivery": is_shipping,
        })
        order_line = sale_order_line_obj.create(order_line_vals)
        order_line.with_context(round=False)._compute_amount()
        return order_line

    def prepare_vals_for_sale_order_line(self, product, product_name, price, quantity):
        """ This method is used to prepare a vals to create a sale order line.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 19 October 2020 .
        """
        instance = self.shopify_instance_id
        uom_id = product and product.uom_id and product.uom_id.id or False
        line_vals = {
            "product_id": product and product.ids[0] or False,
            "order_id": self.id,
            "company_id": self.company_id.id,
            "product_uom_id": uom_id,
            # "name": product_name,
            "price_unit": price,
            "product_uom_qty": quantity
            # "order_qty": quantity,
        }
        if instance.shopify_analytic_account_id:
            analytic_account = self.env['account.analytic.account']
            if instance.use_channel_analytic_account and instance.use_graphql_api:
                channel_handle = self.env.context.get('shopify_channel_handle') or False
                channel_app_id = self.env.context.get('shopify_channel_app_id') or False
                analytic_account = self.env["shopify.channel.ept"].get_channel_analytic(
                    instance, channel_handle, channel_app_id)
            if analytic_account:
                line_vals.update({'analytic_distribution': {analytic_account.id: 100}})
            else:
                analytic_distribution_dict = {}
                analytic_distribution_dict.update({instance.shopify_analytic_account_id.id: 100})
                line_vals.update({'analytic_distribution': analytic_distribution_dict})
        return line_vals

    def shopify_set_tax_in_sale_order_line(self, instance, line, order_response, is_shipping, is_discount,
                                           previous_line, order_line_vals, is_duties,
                                           effective_tax_behaviour=None):
        """ This method is used to set tax in the sale order line base on tax configuration in the
            Shopify setting in Odoo.
            :param line: Response of sale order line.
            :param order_response: Response of order.
            :param is_shipping: It used to identify that it a shipping line.
            :param is_discount: It used to identify that it a discount line.
            :param is_duties: It used to identify that it a duties line.
            :param previous_line: Record of the previously created sale order line.
            :param order_line_vals: Prepared sale order line vals as the previous method.
            :param effective_tax_behaviour: 'odoo_tax' or 'create_shopify_tax'. If None,
                   falls back to instance.apply_tax_in_order.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id: 167537
        """
        # Resolve effective tax behaviour (market overrides instance)
        tax_behaviour = effective_tax_behaviour or instance.apply_tax_in_order

        if tax_behaviour == "create_shopify_tax":
            taxes_included = order_response.get("taxes_included") or False
            tax_ids = []
            if line and line.get("tax_lines"):
                if line.get("taxable") or is_shipping or is_duties:
                    tax_ids = self.shopify_get_tax_id_ept(instance,
                                                          line.get("tax_lines"),
                                                          taxes_included)
            elif not line and previous_line:
                # Before modification, connector set order taxes on discount line but as per connector design,
                # we are creating discount line base on sale order line so it should apply sale order line taxes
                # in discount line not order taxes. It creates a problem while the customer is using multi taxes
                # in sale orders. so set the previous line taxes on the discount line.
                tax_ids = [(6, 0, previous_line.tax_ids.ids)]
            order_line_vals["tax_ids"] = tax_ids
            # When the one order with two products one product with tax and another product
            # without tax and apply the discount on order that time not apply tax on discount
            # which is
            if is_discount and previous_line and not previous_line.tax_ids:
                order_line_vals["tax_ids"] = []
        return order_line_vals

    @api.model
    def shopify_get_tax_id_ept(self, instance, tax_lines, tax_included):
        """This method used to search tax in Odoo, If tax is not found in Odoo then it call child method to create a
            new tax in Odoo base on received tax response in order response.
            @return: tax_id
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 18/11/2019.
            Task Id : 157350
        """
        tax_id = []
        taxes = []
        # Use market company if available, otherwise fall back to instance warehouse's company.
        # This ensures taxes are searched/created in the correct company for multi-company setups.
        market = self.shopify_market_id if hasattr(self, 'shopify_market_id') else False
        if market and market.company_id:
            company = market.company_id
        else:
            company = instance.shopify_warehouse_id.company_id
        for tax in tax_lines:
            rate = float(tax.get("rate", 0.0))
            price = float(tax.get('price', 0.0))
            title = tax.get("title")
            rate = rate * 100
            if not float_is_zero(rate, precision_digits=2) and not float_is_zero(price, precision_digits=2):
                if tax_included:
                    name = "%s_(%s %s included)_%s" % (title, str(rate), "%", company.name)
                    price_include_override = "tax_included"
                else:
                    name = "%s_(%s %s excluded)_%s" % (title, str(rate), "%", company.name)
                    price_include_override = "tax_excluded"
                tax_id = self.env["account.tax"].search([("price_include_override", "=", price_include_override),
                                                         ("type_tax_use", "=", "sale"), ("amount", "=", rate),
                                                         ("name", "=", name), ("company_id", "=", company.id)], limit=1)
                if not tax_id:
                    tax_id = self.sudo().shopify_create_account_tax(instance, rate, price_include_override, company,
                                                                    name, market)
                if tax_id:
                    taxes.append(tax_id.id)
        if taxes:
            tax_id = [(6, 0, taxes)]
        return tax_id

    @api.model
    def shopify_create_account_tax(self, instance, value, price_include_override, company, name, market=False):
        """This method used to create tax in Odoo when importing orders from Shopify to Odoo.
            @param : self, value, price_included, company, name
            @return: account_tax_id
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 18/11/2019.
            Task Id : 157350
        """
        account_tax_obj = self.env["account.tax"]

        account_tax_id = account_tax_obj.create({"name": name, "amount": float(value),
                                                 "type_tax_use": "sale",
                                                 "price_include_override": price_include_override,
                                                 "company_id": company.id})

        config = market if instance.is_shopify_market_enabled and market else instance

        # Set tax accounts on repartition lines
        account_tax_id.mapped("invoice_repartition_line_ids").write({
            "account_id": config.invoice_tax_account_id.id if config.invoice_tax_account_id else False
        })

        account_tax_id.mapped("refund_repartition_line_ids").write({
            "account_id": config.credit_tax_account_id.id if config.credit_tax_account_id else False
        })

        # Set tax grid tags
        if config.shopify_tax_grid_id:
            tag_command = [(6, 0, config.shopify_tax_grid_id.ids)]
            account_tax_id.mapped("invoice_repartition_line_ids").write({"tag_ids": tag_command})
            account_tax_id.mapped("refund_repartition_line_ids").write({"tag_ids": tag_command})

        # Set/Create tax group
        if config.is_create_tax_group:
            tax_group = self.env["account.tax.group"].sudo().search([
                ("name", "=", name),
                ("company_id", "=", company.id),
            ], limit=1)

            if not tax_group:
                tax_group = self.env["account.tax.group"].sudo().create({
                    "name": name,
                    "company_id": company.id,
                    "tax_receivable_account_id": (
                        config.tax_group_receivable_account_id.id
                        if config.tax_group_receivable_account_id else False
                    ),
                    "tax_payable_account_id": (
                        config.tax_group_payable_account_id.id
                        if config.tax_group_payable_account_id else False
                    ),
                })

                if tax_group:
                    account_tax_id.tax_group_id = tax_group.id

        return account_tax_id

    def prepare_final_list_of_transactions(self, transactions):
        """ This method is used to prepare a final order transactions list.
            @author: Yagnik Joshi @Emipro Technologies Pvt. Ltd on date 2 May 2023.
        """
        final_transactions_result = []
        for result in transactions:
            if "Cash on Delivery" in result.get("gateway"):
                final_transactions_result.append(result)
            if result.get('kind') in ['void', 'capture', 'authorization', 'refund'] and result.get(
                    'status') == 'success' and result.get('parent_id'):
                dict_index = next((index for (index, transaction_data) in enumerate(final_transactions_result) if
                                   transaction_data["id"] == result.get('parent_id')), None)
                if dict_index is not None:
                    del final_transactions_result[dict_index]
            if result.get('kind') in ['capture', 'sale', 'authorization'] and result.get('status') == 'success':
                final_transactions_result.append(result)
        return final_transactions_result

    def prepare_vals_shopify_multi_payment(self, instance, order_data_queue_line, order_response,
                                           payments_gateway, workflow, shopify_market=False):
        """ This method is used to prepare a values for the multi payment.
            @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 16/11/2021 .
            Task_id:179257 - Manage multiple payment.
        """
        payment_gateway_obj = self.env["shopify.payment.gateway.ept"]
        payment_list_vals = []
        final_transactions_results = self.prepare_final_list_of_transactions(order_response.get('transaction'))
        for result in final_transactions_results:
            payment_transaction_id = result.get('id')
            gateway = result.get('gateway')
            amount = result.get('amount')
            # if order_response.get('payment_gateway_names')[0] == gateway:
            #     payment_list = (0, 0, {'payment_gateway_id': payment_gateway.id, 'workflow_id': workflow.id,
            #                            'amount': amount, 'payment_transaction_id': payment_transaction_id,
            #                            'remaining_refund_amount': amount})
            #     payment_list_vals.append(payment_list)
            #     continue
            # handle shopify market case
            #     payment_gateway, new_workflow, payment_term = \
            #         payment_gateway_obj.shopify_search_create_gateway_workflow(instance,
            #                                                                 order_data_queue_line,
            #                                                                 order_response,
            #                                                                 gateway)
            payment_gateway, new_workflow, payment_term = self._resolve_payment_gateway_workflow_ept(instance,
                                                                                                     order_data_queue_line,
                                                                                                     order_response,
                                                                                                     gateway,
                                                                                                     shopify_market)
            if not all([payment_gateway, new_workflow]):
                return False
            payment_list = (0, 0, {'payment_gateway_id': payment_gateway.id, 'workflow_id': new_workflow.id,
                                   'amount': amount, 'payment_transaction_id': payment_transaction_id,
                                   'remaining_refund_amount': amount})
            payment_list_vals.append(payment_list)
        return payment_list_vals

    @api.model
    def closed_at(self, instance):
        """
        This method is used to close orders in the Shopify store after the update fulfillment
        from Odoo to the Shopify store.
        """
        sales_orders = self.search([('warehouse_id', '=', instance.shopify_warehouse_id.id),
                                    ('shopify_order_id', '!=', False),
                                    ('shopify_instance_id', '=', instance.id),
                                    ('state', '=', 'done'), ('closed_at_ept', '=', False)],
                                   order='date_order')

        instance.connect_in_shopify()

        for sale_order in sales_orders:
            order = shopify.Order.find(sale_order.shopify_order_id)
            order.close()
            sale_order.write({'closed_at_ept': datetime.now()})
        return True

    def get_shopify_carrier_code(self, picking):
        """
        Gives carrier name from picking, if available.
        @author: Maulik Barad on Date 16-Sep-2020.
        """
        carrier_name = ""
        if picking.carrier_id:
            carrier_name = picking.carrier_id.shopify_tracking_company or picking.carrier_id.shopify_source \
                           or picking.carrier_id.name or ''
        return carrier_name

    def prepare_tracking_numbers_and_lines_for_fulfilment(self, picking):
        """
        This method prepares tracking numbers' list and list of dictionaries of shopify line id and
        fulfilled qty for that.
        @author: Maulik Barad on Date 17-Sep-2020.
        Migration done by Haresh Mori on October 2021
        """
        moves = picking.move_ids.filtered(lambda line: line.shopify_fulfillment_line_id)
        product_moves = moves.filtered(lambda x: x.sale_line_id.product_id.id == x.product_id.id and x.state == "done")
        if picking.mapped("move_line_ids.result_package_id").filtered(lambda l: l.tracking_no):
            tracking_numbers, line_items = self.prepare_tracking_numbers_and_lines_for_multi_tracking_order(
                moves, product_moves)
        else:
            tracking_numbers, line_items = self.prepare_tracking_numbers_and_lines_for_simple_tracking_order(
                moves, product_moves, picking)

        return tracking_numbers, line_items

    def prepare_tracking_numbers_and_lines_for_simple_tracking_order(self, moves, product_moves, picking):
        """ This method is used to prepare tracking numbers and line items for the simple tracking order.
            :param moves: Move lines of picking.
            :param product_moves: Filtered moves.
            @return: tracking_numbers, line_items
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id: 167537
        """
        tracking_numbers = []
        line_items = []
        for move in product_moves.filtered(lambda line: line.product_id.type in ['consu']):
            fulfillment_line_id = move.shopify_fulfillment_line_id

            line_items.append({"id": fulfillment_line_id, "quantity": int(move.product_qty)})
            tracking_numbers.append(picking.carrier_tracking_ref or "")

        kit_sale_lines = moves.filtered(
            lambda x: x.sale_line_id.product_id.id != x.product_id.id and x.state == "done").sale_line_id
        for kit_sale_line in kit_sale_lines:
            matched_moves = kit_sale_line.move_ids.filtered(lambda ml: ml.id in moves.ids)
            if not matched_moves:
                continue
            fulfillment_line_id = matched_moves[0].shopify_fulfillment_line_id
            sale_order = picking.sale_id
            all_sale_pickings = sale_order.picking_ids
            updated_pickings = all_sale_pickings.filtered(
                lambda p: p.updated_in_shopify and
                          fulfillment_line_id in p.move_ids.mapped('shopify_fulfillment_line_id')
            )
            if updated_pickings:
                continue
            if kit_sale_line.qty_delivered == kit_sale_line.product_uom_qty:
                line_items.append({"id": fulfillment_line_id, "quantity": int(kit_sale_line.qty_delivered)})
                tracking_numbers.append(picking.carrier_tracking_ref or "")
        return tracking_numbers, line_items

    def prepare_tracking_numbers_and_lines_for_multi_tracking_order(self, moves, product_moves):
        """ This method is used to prepare tracking numbers and line items for the simple tracking order.
            :param moves: Move lines of picking.
            :param product_moves: Filtered moves.
            @return: tracking_numbers, line_items
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id: 167537
        """
        tracking_numbers = []
        line_items = []
        for move in product_moves:
            total_qty = 0
            fulfillment_line_id = move.shopify_fulfillment_line_id

            for move_line in move.move_line_ids:
                tracking_no = move_line.result_package_id.tracking_no or ""
                total_qty += move_line.quantity
                tracking_numbers.append(tracking_no)

            line_items.append({"id": fulfillment_line_id, "quantity": int(total_qty)})

        kit_move_lines = moves.filtered(
            lambda x: x.sale_line_id.product_id.id != x.product_id.id and x.state == "done")
        existing_sale_line_ids = []
        for move in kit_move_lines:
            if move.sale_line_id.id in existing_sale_line_ids:
                continue

            fulfillment_line_id = move.shopify_fulfillment_line_id
            existing_sale_line_ids.append(move.sale_line_id.id)

            tracking_no = move.move_line_ids.result_package_id.mapped("tracking_no") or []
            tracking_no = tracking_no[0] if tracking_no else ""
            kit_sale_line = move.sale_line_id
            sale_order = move.picking_id.sale_id
            all_sale_pickings = sale_order.picking_ids
            updated_pickings = all_sale_pickings.filtered(
                lambda p: p.updated_in_shopify and
                          fulfillment_line_id in p.move_ids.mapped('shopify_fulfillment_line_id')
            )
            if updated_pickings:
                continue
            if kit_sale_line.qty_delivered == kit_sale_line.product_uom_qty:
                line_items.append({"id": fulfillment_line_id, "quantity": int(kit_sale_line.qty_delivered)})
                tracking_numbers.append(tracking_no)
        return tracking_numbers, line_items

    def update_order_status_in_shopify(self, instance, picking_ids=[]):
        """
        find the picking with below condition
            1. shopify_instance_id = instance.id
            2. updated_in_shopify = False
            3. state = Done
            4. location_dest_id.usage = customer
        get order line data from the picking and process on that. Process on only those products which type is
        not service get carrier_name from the picking get product qty from move lines. If one move having multiple
        move lines then total qty of all the move lines.
        shopify_line_id wise set the product qty_done set tracking details using shopify Fulfillment API update the
        order status
        @author: Maulik Barad on Date 16-Sep-2020.
        Task Id : 157905
        Migration done by Haresh Mori on October 2021
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        log_lines = []
        notify_customer = instance.notify_customer
        _logger.info(_("Update Order Status process start for '%s' Instance"), instance.name)

        if not picking_ids:
            picking_ids = self.shopify_search_picking_for_update_order_status(instance)
        if instance.use_graphql_api:
            self._update_order_status_graphql(instance, picking_ids, notify_customer, log_lines)
        else:
            instance.connect_in_shopify()
            for index, picking in enumerate(picking_ids, start=1):
                try:
                    carrier_name = self.get_shopify_carrier_code(picking)
                    sale_order = picking.sale_id

                    _logger.info("We are processing Sale order '%s' and Picking '%s'", sale_order.name, picking.name)
                    is_continue_process, order_response = self.request_for_shopify_order(sale_order)
                    if is_continue_process:
                        continue
                    fulfillment_order = self.set_fulfilment_order_id_and_fulfillment_line_id(sale_order, picking)

                    tracking_numbers, line_items = sale_order.prepare_tracking_numbers_and_lines_for_fulfilment(picking)

                    if not line_items:
                        message = ("System tried to update the shipping status from Odoo to the Shopify store for Order: %s, but the status was not updated.\n"
                                      "Possible reason:\n"
                                      "The product is a kit product in the delivery order, and shipping status will only be "
                                      "updated when all quantities are delivered. (Check 'Delivered' in the Sales Order line.)") % sale_order.name

                        _logger.info(message)
                        log_lines.append(
                            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                           module="shopify_ept", message=message,
                                                                           model_name=self._name,
                                                                           order_ref=sale_order.client_order_ref))
                        continue

                    if not fulfillment_order:
                        shopify_order_id = sale_order.shopify_order_id
                        fulfillment_order = shopify.fulfillment.FulfillmentOrders.find(order_id=int(shopify_order_id))
                    if fulfillment_order and len(fulfillment_order) > 1:
                        closed_fulfillments = []
                        for fulfillment in fulfillment_order:
                            # when some fulfillments are closed, not to request to fulfill again.
                            if fulfillment.attributes.get('status') == 'closed':
                                closed_fulfillments.append(str(fulfillment.id))
                        shopify_location_id, fulfillment_vals = self.prepare_vals_for_multiple_fulfillment(sale_order,
                                                                                                           tracking_numbers,
                                                                                                           picking,
                                                                                                           carrier_name,
                                                                                                           line_items,
                                                                                                           closed_fulfillments=closed_fulfillments,
                                                                                                           notify_customer=notify_customer)
                        if not shopify_location_id:
                            continue
                    else:
                        shopify_location_id = self.search_shopify_location_for_update_order_status(sale_order, instance,
                                                                                                   line_items,
                                                                                                   picking)

                        if not shopify_location_id:
                            continue

                        fulfillment_vals = self.prepare_vals_for_fulfillment(sale_order, shopify_location_id, tracking_numbers,
                                                                             picking, carrier_name, line_items, notify_customer)

                    is_create_mismatch, fulfillment_result, new_fulfillment = self.post_fulfilment_in_shopify(fulfillment_vals,
                                                                                                              sale_order,
                                                                                                              instance)
                    if is_create_mismatch:
                        continue

                    self.process_shopify_fulfilment_result(instance, fulfillment_result, order_response, picking, sale_order,
                                                           new_fulfillment)

                    sale_order.shopify_location_id = shopify_location_id
                    self.env.cr.commit()
                    _logger.info("Picking %s processed and committed successfully.", picking.name)
                except Exception as e:
                    _logger.error("Failed to update order status for picking : %s - Error : %s", picking.name, e)
                    # SAFETY NET: Resets the frozen PostgreSQL connection.
                    # This wipes out ONLY the broken data from this current picking.
                    # Prior successful pickings are already safely saved on the disk by the commit above!
                    self.env.cr.rollback()

        if log_lines and instance.is_shopify_create_schedule:
            message = []
            count = 0
            for log_line in log_lines:
                count += 1
                if count <= 5:
                    message.append('<' + 'li' + '>' + log_line.message + '<' + '/' + 'li' + '>')
            if count >= 5:
                message.append(
                    '<' + 'p' + '>' + 'Please refer the logline' + '  ' + log_line.name + '  '
                    + 'check it in more detail' + '<' + '/' + 'p' + '>')
            note = "\n".join(message)
            self.create_schedule_activity_against_loglines(log_lines, note)

        self.closed_at(instance)
        return True

    def request_for_shopify_order_graphql(self, sale_order):
        """
        Request sale order data from Shopify via GraphQL.
        If fulfillment_status is 'FULFILLED', mark pickings as updated.
        If order is cancelled, mark pickings as cancelled.
        """
        try:
            # Use your find_order method
            instance = sale_order.shopify_instance_id
            client = instance.get_graphql_client()
            fulfillment_helper = shopify_graphql.FulfillmentQueryHelper(client)
            # order_data = fulfillment_helper.find_order(sale_order)
            order_data = shopify_graphql.OrderQueryHelper(client).get_order([sale_order.shopify_order_id])
            if not order_data:
                return True, {}
            order_data = order_data[0]  # Assuming get_order returns a list of orders
            if order_data.get('fulfillment_status') == 'fulfilled':
                shopify_location_id = self.env["shopify.location.ept"].search([
                    ("warehouse_for_order", "=", sale_order.warehouse_id.id),
                    ("instance_id", "=", sale_order.shopify_instance_id.id)
                ], limit=1)
                if shopify_location_id:
                    sale_order.shopify_location_id = shopify_location_id.id
                _logger.info('Order %s already fulfilled.', sale_order.name)
                sale_order.picking_ids.filtered(lambda l: l.state == 'done').write({
                    'updated_in_shopify': True
                })
                return True, order_data

            # Check cancellation
            if order_data.get('cancelled_at') and order_data.get('cancel_reason'):
                sale_order.picking_ids.filtered(lambda l: l.state == 'done').write({'is_cancelled_in_shopify': True})
                return True, order_data

            # Fallback: Check if all fulfillment orders closed
            fulfillment_orders = order_data.get('fulfillment_orders', [])
            all_closed = all(fo.get('status') == 'closed' for fo in fulfillment_orders)
            if all_closed and fulfillment_orders:
                shopify_location_id = self.env["shopify.location.ept"].search([
                    ("warehouse_for_order", "=", sale_order.warehouse_id.id),
                    ("instance_id", "=", sale_order.shopify_instance_id.id)
                ], limit=1)
                if shopify_location_id:
                    sale_order.shopify_location_id = shopify_location_id.id
                return True, order_data
            return False, order_data
        except Exception as error:
            _logger.error("Error in request_for_shopify_order %s: %s", sale_order.name, error)
            return True, {}

    def _update_order_status_graphql(self, instance, picking_ids, notify_customer, log_lines):
        """
        Update order status using GraphQL API, handling backorders properly.
        Each picking (including backorders) is processed separately with correct quantities.
        Fixes 'list' object has no attribute 'get' error.
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        client = instance.get_graphql_client()
        fulfillment_helper = shopify_graphql.FulfillmentQueryHelper(client)

        for index, picking in enumerate(picking_ids, start=1):
            try:
                carrier_name = self.get_shopify_carrier_code(picking)
                sale_order = picking.sale_id

                _logger.info("Processing Sale order '%s' and Picking '%s'", sale_order.name, picking.name)

                is_continue_process, order_response = self.request_for_shopify_order_graphql(sale_order)
                if is_continue_process:
                    continue

                # Set fulfillment order IDs and line IDs
                fulfillment_order = self.set_fulfilment_order_id_and_fulfillment_line_id_graphql(sale_order, picking)

                # Prepare tracking numbers and line items
                tracking_numbers, line_items = sale_order.prepare_tracking_numbers_and_lines_for_fulfilment(picking)

                if not line_items:
                    message = (
                        f"No order lines found for update of order shipping status for order [{sale_order.name}]. "
                        "Or if the product is a kit product, it will only update when all quantity is delivered."
                    )
                    _logger.info(message)
                    log_lines.append(
                        common_log_line_obj.create_common_log_line_ept(
                            shopify_instance_id=instance.id,
                            module="shopify_ept",
                            message=message,
                            model_name=self._name,
                            order_ref=sale_order.client_order_ref
                        )
                    )
                    continue

                # Prepare fulfillment values for this picking
                if not fulfillment_order:
                    order = shopify_graphql.OrderQueryHelper(client).get_order([sale_order.shopify_order_id])
                    fulfillment_order = order and order[0].get('fulfillment_orders')
                if fulfillment_order and len(fulfillment_order) > 1:
                    closed_fulfillments = []
                    for fulfillment in fulfillment_order:
                        # when some fulfillments are closed, not to request to fulfill again.
                        if fulfillment.get('status') == 'closed':
                            closed_fulfillments.append(str(fulfillment.get('id')))
                    shopify_location_id, fulfillment_val = self.prepare_vals_for_multiple_fulfillment(sale_order,
                                                                                                      tracking_numbers,
                                                                                                      picking,
                                                                                                      carrier_name,
                                                                                                      line_items,
                                                                                                      closed_fulfillments=closed_fulfillments,
                                                                                                      notify_customer=notify_customer)
                    if not shopify_location_id or not fulfillment_val:
                        continue
                else:
                    shopify_location_id = self.search_shopify_location_for_update_order_status(
                        sale_order, instance, line_items, picking
                    )
                    if not shopify_location_id:
                        continue
                    fulfillment_val = self.prepare_vals_for_fulfillment(
                        sale_order, shopify_location_id, tracking_numbers, picking, carrier_name,
                        line_items, notify_customer
                    )
                    if not fulfillment_val:
                        continue

                # Normalize fulfillment_val to always be a dict or list of dicts
                if isinstance(fulfillment_val, list):
                    fulfillment_vals_to_send = fulfillment_val
                else:
                    fulfillment_vals_to_send = [fulfillment_val]

                # Create fulfillments one by one
                for val in fulfillment_vals_to_send:
                    try:
                        result = fulfillment_helper.create_fulfillment([val])
                        # GraphQL returns aliases like f0, f1, etc.
                        alias_key = "f0"
                        self._process_graphql_fulfillment_result(
                            result.get('data', {}).get(alias_key), picking, sale_order, instance, log_lines
                        )
                    except Exception as error:
                        message = f"Error creating fulfillment via GraphQL for picking {picking.name}: {str(error)}"
                        _logger.exception(message)
                        picking.write({'is_manually_action_shopify_fulfillment': True})
                        log_lines.append(
                            common_log_line_obj.create_common_log_line_ept(
                                shopify_instance_id=instance.id,
                                module="shopify_ept",
                                message=message,
                                model_name=self._name
                            )
                        )
                sale_order.shopify_location_id = shopify_location_id.id
                self.env.cr.commit()
                _logger.info("Picking %s processed and committed successfully.", picking.name)
            except Exception as e:
                _logger.error("Failed to update order status for picking : %s - Error : %s", picking.name, e)
                # SAFETY NET: Resets the frozen PostgreSQL connection.
                # This wipes out ONLY the broken data from this current picking.
                # Prior successful pickings are already safely saved on the disk by the commit above!
                self.env.cr.rollback()

        return True

    def _process_graphql_fulfillment_result(self, fulfillment_result, picking, sale_order, instance, log_lines):
        """
        Process a single GraphQL fulfillment result and update picking/backorders.
        """
        common_log_line_obj = self.env["common.log.lines.ept"]

        if not fulfillment_result:
            message = f"No fulfillment result received for order {sale_order.name}"
            _logger.error(message)
            log_lines.append(
                common_log_line_obj.create_common_log_line_ept(
                    shopify_instance_id=instance.id,
                    module="shopify_ept",
                    message=message,
                    model_name=self._name,
                    order_ref=sale_order.client_order_ref
                )
            )
            return False

        user_errors = fulfillment_result.get('userErrors', [])
        if user_errors:
            error_messages = [f"{err.get('field', '')}: {err.get('message', '')}" for err in user_errors]
            message = f"Fulfillment errors for order {sale_order.name}: {'; '.join(error_messages)}"
            _logger.error(message)
            log_lines.append(
                common_log_line_obj.create_common_log_line_ept(
                    shopify_instance_id=instance.id,
                    module="shopify_ept",
                    message=message,
                    model_name=self._name,
                    order_ref=sale_order.client_order_ref
                )
            )
            picking.write({'is_manually_action_shopify_fulfillment': True})
            return False

        fulfillment = fulfillment_result.get('fulfillment', {})
        if not fulfillment:
            message = f"No fulfillment data received for order {sale_order.name}"
            _logger.error(message)
            log_lines.append(
                common_log_line_obj.create_common_log_line_ept(
                    shopify_instance_id=instance.id,
                    module="shopify_ept",
                    message=message,
                    model_name=self._name,
                    order_ref=sale_order.client_order_ref
                )
            )
            picking.write({'is_manually_action_shopify_fulfillment': True})
            return False

        fulfillment_id = fulfillment.get('id', '')
        fulfillment_status = fulfillment.get('status', '')
        fulfillment_numeric_id = fulfillment_id.split('/')[-1] if fulfillment_id else ''

        _logger.info("Fulfillment created successfully for order %s: %s (Status: %s)",
                     sale_order.name, fulfillment_id, fulfillment_status)

        # Update picking
        picking.write({
            'updated_in_shopify': True,
            'shopify_fulfillment_id': fulfillment_numeric_id
        })
        # # Handle backorders separately
        # backorders = picking.backorder_ids.filtered(lambda order: not order.updated_in_shopify)
        # if backorders:
        #     for backorder in backorders:
        #         self._update_order_status_graphql(instance, [backorder], False, log_lines)

        return True

    def set_fulfilment_order_id_and_fulfillment_line_id_graphql(self, order, picking):
        """
        GraphQL version: Sets fulfillment order IDs and line IDs for the picking.
        Handles backorders correctly and ensures service products get updated.
        """
        move_ids = picking.move_ids
        stock_moves = move_ids.filtered(lambda move: move.shopify_fulfillment_line_id)
        backorders = picking.backorder_ids.filtered(lambda bo: not bo.updated_in_shopify)

        if stock_moves and backorders:
            self.set_backorder_fulfillment_data(backorders, stock_moves)

        fulfillment_order_data = []
        fulfillment_order = False

        if not stock_moves:
            try:
                client = order.shopify_instance_id.get_graphql_client()
                # fulfillment_helper = FulfillmentQueryHelper(client)
                # fo_response = fulfillment_helper.get_fulfillment_orders(order.shopify_order_id)
                fo_response = shopify_graphql.OrderQueryHelper(client).get_order([order.shopify_order_id])
                if fo_response and 'errors' not in fo_response:
                    order_data = fo_response[0]  # Assuming get_order returns a list of orders
                    fulfillment_orders = order_data.get('fulfillment_orders', [])
                    for fo in fulfillment_orders:
                        if fo.get('status') != 'closed':
                            fulfillment_order_data.append(fo)
            except Exception as error:
                _logger.info("Error in GraphQL request for fulfillment orders: %s", error)
            for data in fulfillment_order_data:
                fo_id = data.get('id', '')
                fo_status = data.get('status', '').lower()
                line_items = data.get('line_items', [])
                for line in line_items:
                    fl_id = line.get('id', '')
                    line_item = line.get('line_item', {})
                    line_item_id = line_item.get('id', '')
                    # Update order lines (service products)
                    order_line = order.order_line.filtered(
                        lambda line: str(line.shopify_line_id) == str(line_item_id)
                    )
                    if order_line:
                        order_line.write({
                            'shopify_fulfillment_order_id': fo_id,
                            'shopify_fulfillment_line_id': fl_id,
                            'shopify_fulfillment_order_status': fo_status
                        })
                        self.env.cr.commit()
                    # Update stock moves if exists
                    stock_move = move_ids.filtered(
                        lambda move: not move.shopify_fulfillment_line_id
                                     and move.sale_line_id
                                     and str(move.sale_line_id.shopify_line_id) == str(line_item_id)
                    )
                    if stock_move:
                        stock_move.write({
                            'shopify_fulfillment_order_id': fo_id,
                            'shopify_fulfillment_line_id': fl_id,
                            'shopify_fulfillment_order_status': fo_status
                        })
                        self.env.cr.commit()
                    # Handle backorders
                    if backorders and stock_move:
                        self.set_backorder_fulfillment_data(backorders, stock_move)
        return fulfillment_order or fulfillment_order_data

    def prepare_vals_for_multiple_fulfillment(self, sale_order, tracking_numbers, picking, carrier_name, line_items,
                                              **kwargs):
        """
        This method is used to prepare a vals for the multiple fulfillment.
        @return: fulfillment_vals
        @author: Yagnik Joshi @Emipro Technologies Pvt. Ltd on date 15 December 2023 .
        """
        log_lines = []
        tracking_info = {}
        new_fulfillment_vals = []
        shopify_location_id = False
        shopify_location_obj = self.env["shopify.location.ept"]
        common_log_line_obj = self.env["common.log.lines.ept"]
        closed_fulfillments = kwargs.get('closed_fulfillments', [])
        notify_customer = kwargs.get('notify_customer', False)

        if carrier_name:
            tracking_info.update({"company": carrier_name})

        if tracking_numbers:
            tracking_info.update({"number": ','.join(set(tracking_numbers)), "url": picking.carrier_tracking_url or ''})

        for pick in picking:
            location_ids_mapping = {}
            for move in pick.move_ids:
                shopify_location_id = shopify_location_obj.search(
                    [('warehouse_for_order', '=', move.warehouse_id.id),
                     ("instance_id", "=", picking.shopify_instance_id.id)], limit=1)
                if shopify_location_id:
                    location_ids_mapping.setdefault(move.shopify_fulfillment_order_id,
                                                    shopify_location_id.shopify_location_id)
                else:
                    message = ("System tried to update the shipping status from the Odoo to Shopify store "
                               "but Order in the warehouse[%s] is not set the shopify location.\n"
                               "Action items:\n"
                               "- Verify the Shopify location under: Shopify → Configuration → Shopify Locations.\n"
                               "- If the locations are not available in Odoo, import them using the operation wizard.\n"
                               "- Set the order in the Warehouse in the shopify location ") % move.warehouse_id.name
                    _logger.info(message)
                    log_lines.append(
                        common_log_line_obj.create_common_log_line_ept(
                            shopify_instance_id=picking.shopify_instance_id.id,
                            module="shopify_ept", message=message,
                            model_name=self._name,
                            order_ref=sale_order.client_order_ref))
                    return False, False
            for order_id, location_id in location_ids_mapping.items():
                fulfillment_vals = {
                    "notify_customer": notify_customer,
                    "location_id": location_id,
                    "line_items_by_fulfillment_order": []
                }
                sale_line_ids = []
                fulfillment_vals_list = []
                for move in pick.move_ids:
                    if move.sale_line_id.id in sale_line_ids:
                        continue
                    if move.shopify_fulfillment_order_id in closed_fulfillments:
                        continue
                    if order_id and move.shopify_fulfillment_order_id == order_id:
                        sale_line_ids.append(move.sale_line_id.id)
                        fulfillable_quantity = self._get_shopify_fulfillable_quantity(line_items, move)
                        if fulfillable_quantity:
                            fulfillment_order_entry = {
                                "id": move.shopify_fulfillment_line_id,
                                "quantity": int(fulfillable_quantity)
                            }
                            fulfillment_vals_list.append(fulfillment_order_entry)
                if fulfillment_vals_list:
                    fulfillment_vals["line_items_by_fulfillment_order"].append({
                        'fulfillment_order_id': order_id,
                        'fulfillment_order_line_items': fulfillment_vals_list
                    })
                if tracking_info:
                    fulfillment_vals.update({"tracking_info": tracking_info})
                if len(fulfillment_vals["line_items_by_fulfillment_order"]) > 0:
                    new_fulfillment_vals.append(fulfillment_vals)
                if shopify_location_id:
                    # get service type product fulfillment data
                    service_product_sale_line_ids = sale_order.order_line.filtered(
                        lambda x: x.shopify_fulfillment_line_id and x.product_id.type == 'service'
                                  and not x.is_delivery and x.shopify_fulfillment_order_status != 'closed'
                                  and x.shopify_fulfillment_order_id not in closed_fulfillments)
                    if service_product_sale_line_ids:
                        service_fulfillment_data = self.prepare_vals_for_service_type_product_fulfillment(
                            service_product_sale_line_ids,
                            shopify_location_id, notify_customer)
                        new_fulfillment_vals.extend(service_fulfillment_data)
        return shopify_location_id, new_fulfillment_vals

    def _get_shopify_fulfillable_quantity(self, line_items, move):
        for line in line_items:
            if move.shopify_fulfillment_line_id == line.get('id'):
                return line.get('quantity')

    def shopify_search_picking_for_update_order_status(self, instance):
        """ This method is used to search picking for the update order status.
            @return: picking_ids(Records of picking)
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id: 167537
            Migration done by Haresh Mori on October 2021
        """
        location_obj = self.env["stock.location"]
        stock_picking_obj = self.env["stock.picking"]
        customer_locations = location_obj.search([("usage", "=", "customer")])
        picking_ids = stock_picking_obj.search([("shopify_instance_id", "=", instance.id),
                                                ("updated_in_shopify", "=", False),
                                                ("state", "=", "done"),
                                                ("location_dest_id", "in", customer_locations.ids),
                                                ('is_cancelled_in_shopify', '=', False)],
                                               order="create_date")
        return picking_ids

    def request_for_shopify_order(self, sale_order):
        """ This method is used to request for sale order in the shopify store and if order response has
            fufillment_status is fulfilled then continue the update order status for that picking.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id: 167537
            Migration done by Haresh Mori on October 2021
        """
        try:
            order = shopify.Order.find(sale_order.shopify_order_id)
            # Guard in case if there is Paginated collection
            if isinstance(order, list):
                order = order[0] if order else None
            if not order:
                return False, {}
            order_data = order.to_dict()
            if order_data.get('fulfillment_status') == 'fulfilled':
                shopify_location_id = self.env["shopify.location.ept"].search(
                    [("warehouse_for_order", "=", sale_order.warehouse_id.id),
                     ("instance_id", "=", sale_order.shopify_instance_id.id)], limit=1)
                sale_order.shopify_location_id = shopify_location_id
                _logger.info('Order %s is already fulfilled', sale_order.name)
                sale_order.picking_ids.filtered(lambda l: l.state == 'done').write({'updated_in_shopify': True})
                return True, order_data
            if order_data.get('cancelled_at') and order_data.get('cancel_reason'):
                sale_order.picking_ids.filtered(lambda l: l.state == 'done').write({'is_cancelled_in_shopify': True})
                return True, order_data
            return False, order_data
        except Exception:
            _logger.exception("Error in Request of shopify order for the fulfilment. Order: %s",
                              sale_order.shopify_order_id)
            return True, {}

    def search_shopify_location_for_update_order_status(self, sale_order, instance, line_items, picking):
        """ This method is used to search the shopify location for the update order status from Odoo to shopify store.
            @return: shopify_location_id
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id:167537
            Migration done by Haresh Mori on October 2021
        """
        shopify_location_obj = self.env["shopify.location.ept"]
        if instance.is_delivery_multi_warehouse:
            line_item_ids = [str(line.get('id')) for line in line_items]
            order_line = picking.move_ids.filtered(
                lambda line: line.shopify_fulfillment_line_id in line_item_ids).sale_line_id
            if order_line.warehouse_id_ept:
                shopify_location_id = shopify_location_obj.search(
                    [('warehouse_for_order', '=', order_line.warehouse_id_ept.id), ("instance_id", "=", instance.id)],
                    limit=1)
                if not shopify_location_id:
                    message = ("System tried to update the shipping status from the Odoo to Shopify store "
                               "but Order in the warehouse[%s] is not set the shopify location.\n"
                               "Action items:\n"
                               "- Verify the Shopify location under: Shopify → Configuration → Shopify Locations.\n"
                               "- If the locations are not available in Odoo, import them using the operation wizard.\n"
                               "- Set the order in the Warehouse in the shopify location ") % order_line.warehouse_id_ept.name
                    _logger.info(message)
                    self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                                module="shopify_ept",
                                                                                message=message,
                                                                                model_name=self._name,
                                                                                order_ref=sale_order.client_order_ref)
                    return False
            else:
                shopify_location_id = shopify_location_obj.search(
                    [('warehouse_for_order', '=', order_line.warehouse_id_ept.id), ("instance_id", "=", instance.id),
                     ("is_primary_location", "=", True)], limit=1)
            return shopify_location_id
        shopify_location_id = sale_order.shopify_location_id or False
        if not shopify_location_id:
            shopify_location_id = shopify_location_obj.search(
                [("warehouse_for_order", "=", sale_order.warehouse_id.id), ("instance_id", "=", instance.id),
                 ("is_primary_location", "=", True)])
            if not shopify_location_id:
                shopify_location_id = shopify_location_obj.search([("is_primary_location", "=", True),
                                                                   ("instance_id", "=", instance.id)])
            if not shopify_location_id:
                message = ("System tried to update shipping order status from Odoo to the Shopify store, but the primary location"
                              "was not found for the Shopify instance: %s.\n"
                              "Action Items:\n"
                              "- Verify the primary Shopify location under: Shopify → Configuration → Shopify Locations.\n"
                              "- If the primary locations are not available in Odoo, import them using the operation wizard") % instance.name
                _logger.info(message)
                self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                            module="shopify_ept",
                                                                            message=message,
                                                                            model_name=self._name,
                                                                            order_ref=sale_order.client_order_ref)
                return False

        return shopify_location_id

    def prepare_vals_for_fulfillment(self, sale_order, shopify_location_id, tracking_numbers, picking, carrier_name,
                                     line_items, notify_customer):
        """ This method is used to prepare a vals for the fulfillment.
            @return: fulfillment_vals
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id: 167537
            Migration done by Haresh Mori on October 2021
        """
        tracking_info = {}
        new_fulfillment_vals = []
        if carrier_name:
            tracking_info.update({"company": carrier_name})
        if tracking_numbers:
            tracking_info.update({"number": ','.join(set(tracking_numbers)), "url": picking.carrier_tracking_url or ''})
        fulfillment_vals = {
            "location_id": int(shopify_location_id.shopify_location_id),
            "notify_customer": notify_customer,
            "line_items_by_fulfillment_order": [
                {
                    "fulfillment_order_id": picking.move_ids[0].shopify_fulfillment_order_id,
                    "fulfillment_order_line_items": line_items
                }]
        }
        if tracking_info:
            fulfillment_vals.update({"tracking_info": tracking_info})
        new_fulfillment_vals.append(fulfillment_vals)

        # get service type product fulfillment data
        service_product_sale_line_ids = sale_order.order_line.filtered(
            lambda x: x.shopify_fulfillment_line_id and x.product_id.type == 'service'
                      and not x.is_delivery and x.shopify_fulfillment_order_status != 'closed')
        if service_product_sale_line_ids:
            service_fulfillment_data = self.prepare_vals_for_service_type_product_fulfillment(
                service_product_sale_line_ids,
                shopify_location_id,
                notify_customer)
            new_fulfillment_vals.extend(service_fulfillment_data)
        return new_fulfillment_vals

    def prepare_vals_for_service_type_product_fulfillment(self, service_product_sale_line_ids, shopify_location_id,
                                                          notify_customer):
        service_line_items = []
        for service_product_data in service_product_sale_line_ids:
            service_fulfillment_vals = {
                "location_id": int(shopify_location_id.shopify_location_id),
                "notify_customer": notify_customer,
                "line_items_by_fulfillment_order": [
                    {
                        "fulfillment_order_id": service_product_data.shopify_fulfillment_order_id,
                        "fulfillment_order_line_items": [{"id": service_product_data.shopify_fulfillment_line_id,
                                                          "quantity": int(service_product_data.product_qty)}]
                    }
                ]
            }
            service_line_items.append(service_fulfillment_vals)
        return service_line_items

    def post_fulfilment_in_shopify(self, fulfillment_vals, sale_order, instance):
        """ This method is used to post the fulfillment from Odoo to Shopify store.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 10 November 2020 .
            Task_id: 167930 - Update order status changes as per v13
            Migration done by Haresh Mori on October 2021
        """
        if not fulfillment_vals:
            message = f"No data is prepared for Update the order status for order : {sale_order.name}"
            _logger.info(message)
            return True, False, False
        new_fulfillment = False
        fulfillment_result = False
        for new_fulfillment_vals in fulfillment_vals:
            try:
                new_fulfillment = shopify.fulfillment.FulfillmentV2(new_fulfillment_vals)
                fulfillment_result = new_fulfillment.save()
                if not fulfillment_result:
                    return False, fulfillment_result, new_fulfillment
            except ClientError as error:
                if hasattr(error,
                           "response") and error.response.code == 429 and error.response.msg == "Too Many Requests":
                    time.sleep(int(float(error.response.headers.get('Retry-After', SHOPIFY_RETRY_AFTER_DEFAULT))))
                    fulfillment_result = new_fulfillment.save()
            except Exception as error:
                message = "%s" % str(error)
                _logger.info(message)
                self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                            module="shopify_ept",
                                                                            message=message,
                                                                            model_name=self._name,
                                                                            order_ref=sale_order.client_order_ref)
                return True, fulfillment_result, new_fulfillment

        return False, fulfillment_result, new_fulfillment

    def process_shopify_fulfilment_result(self, instance, fulfillment_result, order_response, picking, sale_order,
                                          new_fulfillment):
        """ This method is used to process fulfillment result.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 10 November 2020 .
            Task_id:167930 - Update order status changes as per v13
            Migration done by Haresh Mori on October 2021
        """
        if not fulfillment_result:
            if order_response.get('fulfillment_status') == 'partial':
                if not new_fulfillment.errors:
                    picking.write({'updated_in_shopify': True})
            else:
                picking.write({'is_manually_action_shopify_fulfillment': True})
            sale_order.write({'is_service_tracking_updated': False})
            message = "Order(%s) status not updated due to %s:" % (sale_order.name, new_fulfillment.errors.errors)
            _logger.info(message)
            self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                        module="shopify_ept",
                                                                        message=message,
                                                                        model_name=self._name,
                                                                        order_ref=sale_order.client_order_ref)
            return False

        fulfillment_id = ''
        if new_fulfillment:
            # shopify_fullment_result = xml_to_dict(new_fulfillment.to_xml())
            shopify_fulfillment_result = json.loads(new_fulfillment.to_json())
            if shopify_fulfillment_result:
                fulfillment_id = shopify_fulfillment_result.get('fulfillment').get('id') or ''
            for line_item in shopify_fulfillment_result.get('fulfillment')['line_items']:
                service_order_line = sale_order.order_line.filtered(
                    lambda x: x.shopify_line_id == str(line_item.get('id')) and x.product_id.type == 'service'
                              and not x.is_delivery and x.shopify_fulfillment_order_status != 'closed')
                if service_order_line:
                    service_order_line.write({'shopify_fulfillment_order_status': 'closed'})

        picking.write({'updated_in_shopify': True, 'shopify_fulfillment_id': fulfillment_id})

        return True

    @api.model
    def process_shopify_order_via_webhook(self, order_data, instance, update_order=False):
        """
        Creates order data queue and process it.
        This method is for order imported via create and update webhook.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 10-Jan-2020..
        @param order_data: Dictionary of order's data.
        @param instance: Instance of Shopify.
        @param update_order: If update order webhook id called.
        """
        order_queue_obj = self.env["shopify.order.data.queue.ept"]
        order_queue_line_obj = self.env["shopify.order.data.queue.line.ept"]
        queue_type = 'unshipped'
        if order_data.get('fulfillment_status') == 'fulfilled':
            queue_type = 'shipped'
        queue = order_queue_line_obj.create_order_data_queue_line([order_data],
                                                                  instance,
                                                                  queue_type,
                                                                  created_by='webhook')
        if queue:
            order_queue_cron = self.sudo().env.ref("shopify_ept.process_shopify_order_queue")
            if not order_queue_cron.active:
                _logger.info("Active the Order data process queue cron job")
                order_queue_cron.write({'active': True, 'nextcall': datetime.now() + timedelta(seconds=120)})
        if not update_order:
            order_queue_obj.browse(queue).order_data_queue_line_ids.process_import_order_queue_data()
        self.env.cr.commit()
        return True

    @api.model
    def update_shopify_order(self, queue_lines, created_by, instance):
        """
        This method will update order as per its status got from Shopify.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13-Jan-2020..
        @param queue_lines: Order Data Queue Line.
        @param created_by: Queue line Created by.
        @return: Updated Sale order.
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        orders = self
        QUEUE_LINE_COMMIT_BATCH_SIZE = 20  # Number of queue lines to process before committing to DB
        queueline_count = 0
        for queue_line in queue_lines:
            shopify_instance = queue_line.shopify_instance_id
            order_data = json.loads(queue_line.order_data)
            shopify_status = order_data.get("financial_status")
            shopify_tags = order_data.get("tags")
            shopify_note = order_data.get("note")

            order = self.search_existing_shopify_order(order_data, shopify_instance, order_data.get("order_number"))

            
            # Update SO metafields values
            if order and instance.enable_metafield_sync and instance.use_graphql_api:
                self.set_metafield_values_in_order(order_data, instance, order)

            if order and order.state == 'cancel':
                if shopify_tags:
                    self.update_tag_in_shopify_order(order, shopify_tags)
                if shopify_note:
                    order.write({"note": shopify_note if shopify_note else ''})

                _logger.info(
                    f"Order {order.name} (Shopify: {order_data.get('name')}) is already cancelled in Odoo. Skipping webhook processing to prevent duplicate operations. Queue line: {queue_line.name}")
                queue_line.write({'state': 'done', 'processed_at': datetime.now()})
                continue

            if not order:
                self.import_shopify_orders(queue_line, shopify_instance)
                order = self.search_existing_shopify_order(order_data, shopify_instance, order_data.get("order_number"))
                # self.env.cr.commit()
                try:
                    self.env.cr.commit()
                except Exception as error:
                    # Case: When a new product or product category is created during the order import by
                    # webhook, the user is public user which does not have the right to create this models
                    # records i.e. here if exception occurs then records will be created by odoo_bot user
                    if self.env.ref('base.public_user').id == self.env.user.id:
                        odoo_bot = self.env.ref('base.user_root')
                        message = (
                                      f"The user has been changed from %s to %s for processing this order({order.name}) due to access right issue during creation of "
                                      f"the New Records such as product or product category.") % (
                                      self.env.ref('base.public_user').name, odoo_bot.name)
                        _logger.info(message)
                        order.message_post(body=message)
                        for env in self.env.transaction.envs:
                            if env.uid == self.env.user.id:
                                env.uid = odoo_bot.id
                                break
                        self.env.cr.commit()
                if order:
                    queue_line.write({'state': 'done', 'processed_at': datetime.now()})
                return True
            try:
                need_to_done_queue = True
                if order_data.get('cancel_reason'):
                    need_to_done_queue = False
                    self.process_cancel_order_webhook_ept(order, instance, queue_line, order_data)

                if instance.customer_order_webhook:
                    need_to_done_queue = False
                    order.shopify_change_customer_in_order_webhook(instance, queue_line, order_data)

                if instance.add_new_product_order_webhook and order_data.get('fulfillment_status') != 'fulfilled':
                    need_to_done_queue = False
                    order.add_new_product_in_order_webhook_ept(instance, queue_line, order_data)

                if instance.update_qty_order_webhook and order_data.get('fulfillment_status') not in ['fulfilled',
                                                                                                      'partial']:
                    need_to_done_queue = False
                    order.update_qty_in_order_webhook_ept(instance, queue_line, order_data)

                if shopify_status == 'paid':
                    self.webhook_paid_workflow_process_ept(order, instance, queue_line, order_data, shopify_status)
                    need_to_done_queue = True

                if order_data.get('fulfillment_status') in (
                        'fulfilled', 'partial') and instance.ship_order_webhook and order_data.get('fulfillments'):
                    need_to_done_queue = False
                    self.process_order_fulfillment_ept(order, shopify_instance, order_data, queue_line)

                if shopify_status in ["refunded", "partially_refunded"] and order_data.get(
                        "refunds") and instance.refund_order_webhook:
                    need_to_done_queue = False
                    self.process_order_refund_data_ept(shopify_status, order_data, order, created_by, instance,
                                                       queue_line)
                    if instance.return_picking_order:
                        self.process_picking_return(shopify_status, order_data, order, created_by, instance,
                                                    queue_line)

                if shopify_tags:
                    self.update_tag_in_shopify_order(order, shopify_tags)

                if shopify_note:
                    order.write({"note": shopify_note if shopify_note else ''})

                if need_to_done_queue:
                    queue_line.write({'state': 'done', 'processed_at': datetime.now()})
            except Exception as error:
                message = "Receive error while process webhook flow, Error is:  (%s)" % (error)
                _logger.info(message)
                common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, message=message,
                                                               module="shopify_ept",
                                                               model_name='sale.order',
                                                               order_ref=order_data.get('name'),
                                                               shopify_order_data_queue_line_id=queue_line.id if queue_line else False)
                queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
            queueline_count += 1
            if queueline_count == QUEUE_LINE_COMMIT_BATCH_SIZE:
                queueline_count = 0
                _logger.info(
                    f'Commit the Data till the Queue line {queue_line.name} for Queue {queue_line.shopify_order_data_queue_id.name}')
                self.env.cr.commit()
        return orders

    def update_tag_in_shopify_order(self, order, shopify_tags):
        """
        This method is used for update tag in shopify orders.
        :param order:
        :param shopify_tags:
        :return:
        """
        tag_ids = []
        if isinstance(shopify_tags, list):
            normalized_tags_list = [tag.strip() for tag in shopify_tags if tag and tag.strip()]
        elif isinstance(shopify_tags, str) and shopify_tags.strip():
            normalized_tags_list = [tag.strip() for tag in shopify_tags.split(",")]
        else:
            normalized_tags_list = []
        for tag in normalized_tags_list:
            tag_ids.append(self.create_or_search_sale_tag(tag))
        order.write({"tag_ids": tag_ids})

    def process_picking_return(self, shopify_status, order_data, order, created_by, instance, queue_line):
        common_log_line_obj = self.env["common.log.lines.ept"]
        message = self.create_picking_return(shopify_status, order_data, order, created_by)
        if message:
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, message=message,
                                                           module="shopify_ept",
                                                           model_name='sale.order',
                                                           order_ref=order_data.get('name'),
                                                           shopify_order_data_queue_line_id=queue_line.id if queue_line else False)
            queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
        else:
            queue_line.state = "done"

    def create_picking_return(self, shopify_status, order_data, order, created_by):
        message = ""
        if shopify_status in ("refunded", "partially_refunded") and order_data.get("refunds"):
            is_need_create_return = False
            for refund in order_data.get('refunds'):
                for transaction in refund.get('transactions'):
                    if transaction.get('kind') == 'refund' and transaction.get('status') == 'success':
                        is_need_create_return = True

            if is_need_create_return:
                message = order.create_shopify_order_return(order_data.get("refunds"))
        return message

    def create_shopify_order_return(self, refunds_data):
        """
        This method use for create return base on refund data from shopify.
        @author: Nilam Kubavat @Emipro Technologies Pvt. Ltd on date 17 Jan 2024.
        Task_id: 6264
        """
        product_product_obj = self.env["product.product"]
        message = ""
        refund_line_items = self.prepare_refund_data(refunds_data)
        orig_move_ids = self.picking_ids.move_ids.move_orig_ids if self.picking_ids.move_ids.move_orig_ids else self.picking_ids.move_ids
        orig_done_picking_ids = orig_move_ids.picking_id.filtered(lambda picking: picking.state == "done")
        mrp_module = product_product_obj.search_installed_module_ept('mrp')
        if not orig_done_picking_ids and mrp_module:
            orig_done_picking_ids = orig_move_ids.move_dest_ids.picking_id.filtered(
                lambda picking: picking.state == "done")
        is_return = list(filter(lambda x: x.get('restock_type') == 'return', refunds_data[0].get('refund_line_items')))
        if not orig_done_picking_ids and is_return:
            message = "Done picking is not available, so return can't be generated."
        need_to_remove_lines = []
        for picking_id in orig_done_picking_ids:
            return_picking_ids = self.picking_ids.filtered(lambda x: "Return of" in x.origin)
            return_wiz = self.env['stock.return.picking'].with_context(
                active_ids=picking_id.ids,
                active_id=picking_id.ids[0],
                active_model='stock.picking'
            ).sudo().create({})
            for return_move_line in return_wiz.product_return_moves:
                refund_line = next(
                    (item for item in refund_line_items if item["product_id"] == return_move_line.product_id.id),
                    None)
                # if refund_line and return_move_line.product_id.id == refund_line["product_id"]:
                if refund_line:
                    qty_to_return = refund_line["quantity"]
                    existing_return_qty = return_picking_ids.move_ids.filtered(
                        lambda x: x.product_id.id == refund_line["product_id"]).mapped('product_uom_qty')
                    return_qty = sum(existing_return_qty)

                    if qty_to_return > return_qty:
                        return_move_line.write({
                            'quantity': qty_to_return - return_qty,
                            'to_refund': True
                        })
                    else:
                        need_to_remove_lines.append(return_move_line)
                else:
                    need_to_remove_lines.append(return_move_line)

            for need_to_remove_line in need_to_remove_lines:
                need_to_remove_line.unlink()
            if return_wiz.product_return_moves:
                res = return_wiz.action_create_returns()
                return_picking = self.env['stock.picking'].browse(res['res_id'])
                return_picking.message_post(
                    body=_("Return Picking is Generated by Webhook as Order is Refunded in Shopify."))
                if return_picking:
                    if self.shopify_instance_id.stock_validate_for_return:
                        return_picking.button_validate()
                        return_picking.message_post(body=_("Return Picking is Validate by Webhook."))
        return message

    def prepare_refund_data(self, refunds_data):
        refund_line_items = []
        line_item_reasons = {}

        for refund_data_line in refunds_data:
            return_data = refund_data_line.get("return") or {}
            return_line_items = return_data.get("return_line_items") or []
            for return_line in return_line_items:
                fulfillment_line_item = return_line.get("fulfillment_line_item") or {}
                line_item = fulfillment_line_item.get("line_item") or {}
                line_item_gid = line_item.get("id")
                if not line_item_gid:
                    continue
                reason_definition = return_line.get("return_reason_definition") or {}
                return_reason = reason_definition.get("name") or return_line.get("return_reason") or ""
                if return_reason.lower() == "other":
                    return_reason = return_line.get("return_reason_note") or return_reason
                line_item_reasons[line_item_gid] = {"return_reason": return_reason}

        for refund_data_line in refunds_data:
            for refund_line in refund_data_line.get("refund_line_items"):
                if refund_line.get("restock_type").lower() == "return":
                    shopify_line_id = refund_line.get("line_item_id",'')
                    refund_line_item_obj = refund_line.get("line_item", {})
                    if not shopify_line_id:
                        shopify_line_id = refund_line_item_obj.get("id") if refund_line_item_obj else ''

                    line_item_gid = refund_line_item_obj.get("id") if refund_line_item_obj else shopify_line_id
                    reason_data = line_item_reasons.get(line_item_gid, {})

                    product_id = self.order_line.filtered(lambda x: x.shopify_line_id == str(shopify_line_id)).product_id
                    bom_lines = self.check_for_bom_product(product_id)
                    for bom_line in bom_lines:
                        bom_product = bom_line[0].product_id
                        bom_product_qty = (bom_line[1].get('qty', 0)) * refund_line.get("quantity")
                        existing_entry = next(
                            (item for item in refund_line_items if item["product_id"] == bom_product.id),
                            None)
                        if existing_entry:
                            existing_entry["quantity"] += bom_product_qty
                        else:
                            refund_line_items.append({"quantity": bom_product_qty, "product_id": bom_product.id,
                                                      "return_reason": reason_data.get("return_reason", "")})
                    if not bom_lines:
                        existing_entry = next(
                            (item for item in refund_line_items if item["product_id"] == product_id.id),
                            None)
                        if existing_entry:
                            existing_entry["quantity"] += refund_line.get("quantity")
                        else:
                            refund_line_items.append(
                                {"quantity": refund_line.get("quantity"), "product_id": product_id.id,
                                 "return_reason": reason_data.get("return_reason", "")})
        return refund_line_items

    def process_cancel_order_webhook_ept(self, order, instance, queue_line, order_data):
        if order.state == 'cancel':
            _logger.info(
                f"Order {order.name} (Shopify: {order_data.get('name')}) is already cancelled in Odoo. "
                f"Skipping cancellation webhook processing. Queue line: {queue_line.name}")
            queue_line.write({'state': 'done', 'processed_at': datetime.now()})
            return True
        
        context = dict(order.env.context)
        context.update(
            {'shopify_status': order_data.get('financial_status'), 'order_data': order_data,
             'created_by': queue_line.shopify_order_data_queue_id.created_by,
             'queue_line': queue_line})
        cancelled = order.with_context(context).cancel_shopify_order()
        common_log_line_obj = self.env["common.log.lines.ept"]
        if not cancelled:
            picking_ids = order.picking_ids.filtered(lambda p: p.state == 'done')
            message = "The order {0} is canceled in Shopify, but the delivery order {1} has already been processed. Due to this reason, the system will not automatically cancel the order. You can take the following actions manually:\n \
                    1. Reserve Order: If the order has not been shipped to the customer from your warehouse yet, you can reserve the order.\n \
                    2. Cancel Order: You can manually cancel the order in Odoo.\n \
                    3. Create Credit Note: If an invoice has already been created, you can generate a credit note accordingly.".format(
                order.name, picking_ids[0].name)
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, message=message,
                                                           module="shopify_ept",
                                                           model_name='sale.order',
                                                           order_ref=order_data.get('name'),
                                                           shopify_order_data_queue_line_id=queue_line.id if queue_line else False)
            queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
        else:
            queue_line.state = "done"

    def process_order_refund_data_ept(self, shopify_status, order_data, order, created_by, instance, queue_line):
        common_log_line_obj = self.env["common.log.lines.ept"]
        message = self.create_shipped_order_refund(shopify_status, order_data, order, created_by)
        if message:
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, message=message,
                                                           module="shopify_ept",
                                                           model_name='sale.order',
                                                           order_ref=order_data.get('name'),
                                                           shopify_order_data_queue_line_id=queue_line.id if queue_line else False)
            queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
        else:
            queue_line.state = "done"

    def process_order_fulfillment_ept(self, order, shopify_instance, order_data, queue_line):
        message = ''
        fulfilled = False
        common_log_line_obj = self.env["common.log.lines.ept"]
        if order_data.get('fulfillment_status') == 'fulfilled':
            fulfilled = order.fulfilled_shopify_order(order_data)
        if order_data.get('fulfillment_status') == 'partial':
            fulfilled = order.partial_fulfilled_shopify_order(order_data, shopify_instance)
        if not fulfilled:
            message = "The order [%s] has been shipped in Shopify, but the system could not validate the delivery order due to inventory unavailability in Odoo. The automatic validation of delivery orders did not occur for the following reasons:\n 1.Inventory Unavailability: The inventory is not available in the Odoo warehouse, and the option to perform a force transfer is not enabled in the webhook configuration.\n 2.Product Traceability: The product traceability relies on lot numbers, and the inventory  is not in Odoo.\n If you have enabled the Force Transfer option for webhook configuration, and the product traceability is set to Lot/Serial while inventory is unavailable, the system will not process those delivery orders." % order_data.get(
                'name')
        if message:
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=shopify_instance.id, message=message,
                                                           module="shopify_ept",
                                                           model_name='sale.order',
                                                           order_ref=order_data.get('name'),
                                                           shopify_order_data_queue_line_id=queue_line.id if queue_line else False)
            queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
        else:
            queue_line.state = "done"

    def webhook_paid_workflow_process_ept(self, order, instance, queue_line, order_data, shopify_status):
        invoices = order.invoice_ids
        gateway = self.with_context(instance=instance, queue_line=queue_line, order=order)._search_payment_gateway_ept(order_data)
        shopify_market = self._get_market_for_order_ept(instance, order_data)
        payment_gateway, workflow, payment_term = self._resolve_payment_gateway_workflow_ept(
            instance, queue_line, order_data, gateway, shopify_market)
        if workflow:
            order.auto_workflow_process_id = workflow
            if order.state not in ["sale", "done", "cancel"] and workflow.validate_order:
                order.action_confirm()
            if order.invoice_status in ['no', 'to invoice']:
                order_lines = order.mapped('order_line').filtered(lambda l: l.product_id.invoice_policy == 'order')
                picking = order.picking_ids.filtered(
                    lambda p: p.location_dest_id.usage == 'customer' and p.state == 'done')
                delivery_lines = picking.move_line_ids.filtered(lambda l: l.product_id.invoice_policy == 'delivery')
                if workflow and delivery_lines and workflow.create_invoice:
                    _logger.info(f'Calling the invoice creation process')
                    order.with_context(shopify_order_financial_status=shopify_status).validate_and_paid_invoices_ept(
                        workflow)
                    queue_line.state = "done"
                elif not order_lines.filtered(
                        lambda l: l.product_id.type == 'consu' and l.product_id.is_storable) and len(
                    order.order_line) != len(order_lines.filtered(
                    lambda l: l.product_id.type in ['service', 'consu'] and not l.product_id.is_storable)):
                    queue_line.state = "done"
                else:
                    order.with_context(shopify_order_financial_status=shopify_status).validate_and_paid_invoices_ept(
                        workflow)
            elif order.invoice_status == 'invoiced' and workflow.register_payment:
                order.paid_invoice_ept(invoices)

    def add_new_product_in_order_webhook_ept(self, instance, queue_line, order_response):
        """
        This method is use to add new product in the order.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 16 October 2023 .
        """
        new_line_data = self.prepare_data_for_not_exist_product_in_order(order_response)
        if new_line_data:
            order_number = order_response.get("order_number")
            if self.check_mismatch_details(new_line_data, instance, order_number, queue_line):
                _logger.info("Mismatch details found in this Shopify Order(%s) and id (%s)", order_number,
                             order_response.get("id"))
                queue_line.write({"state": "failed", "processed_at": datetime.now()})
            else:
                _logger.info("Creating order lines for Odoo order(%s) and Shopify order is (%s).", self.name,
                             order_number)
                self.webhook_create_shopify_order_lines(new_line_data, order_response, instance)
                work_flow_process_record = self.auto_workflow_process_id
                if work_flow_process_record:
                    order_lines = self.mapped('order_line').filtered(lambda l: l.product_id.invoice_policy == 'order')
                    if not order_lines.filtered(
                            lambda l: l.product_id.type == 'consu' and l.product_id.is_storable) and len(
                        self.order_line) != len(order_lines.filtered(
                        lambda l: l.product_id.type in ['service', 'consu'] and not l.product_id.is_storable)):
                        return True
                    self.webhook_call_auto_invoice_workflow(work_flow_process_record)

    def prepare_data_for_not_exist_product_in_order(self, order_data):
        """
        This method is use to prepare not exsit data into the order
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 16 October 2023 .
        """
        new_line_data = []
        for response_line in order_data.get('line_items'):
            sl_id = response_line.get('id')
            if self.order_line.filtered(lambda ol: ol.shopify_line_id == str(sl_id)):
                continue
            new_line_data.append(response_line)
        return new_line_data

    def webhook_create_shopify_order_lines(self, lines, order_response, instance):
        total_discount = order_response.get("total_discounts", 0.0)
        order_number = order_response.get("order_number")
        for line in lines:
            is_custom_line, is_gift_card_line, product = self.search_custom_tip_gift_card_product(line, instance)
            price = line.get("price")
            if instance.order_visible_currency:
                price = self.get_price_based_on_customer_visible_currency(line.get("price_set"), order_response, price)
            order_line = self.shopify_create_sale_order_line(line, product, line.get("current_quantity"),
                                                             product.name, price,
                                                             order_response)
            if is_gift_card_line:
                line_vals = {'is_gift_card_line': True}
                if line.get('name'):
                    line_vals.update({'name': line.get('name')})
                order_line.write(line_vals)

            if is_custom_line:
                order_line.write({'name': line.get('name')})

            if line.get('duties'):
                self.create_shopify_duties_lines(line.get('duties'), order_response, instance)

            if float(total_discount) > 0.0:
                discount_amount = self._get_shopify_discount_allocation_amount(instance, line, order_response)
                if discount_amount > 0.0:
                    _logger.info("Creating discount line for Odoo order(%s) and Shopify order is (%s)", self.name,
                                 order_number)
                    self.shopify_create_sale_order_line({}, instance.discount_product_id, 1,
                                                        product.name, float(discount_amount) * -1,
                                                        order_response, previous_line=order_line,
                                                        is_discount=True)
                    _logger.info("Created discount line for Odoo order(%s) and Shopify order is (%s)", self.name,
                                 order_number)

    def shopify_change_customer_in_order_webhook(self, instance, queue_line, order_data):
        """
        This method is use to update the customer in the order based on the condition it will update.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13 October 2023 .
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        need_update_shipping_partner = False
        need_update_invoice_partner = False
        need_update_partner = False
        message = ""
        if self.state != 'draft' and self.picking_ids.filtered(
                lambda x: x.location_dest_id.usage == "customer" and x.state == "done"):
            message = "The user manually updated customer details in Shopify, but the system did not update them because an delivery order already done the system.\n The system will update customer details only under the following conditions:\n 1.The invoice has not been posted.\n 2.The delivery order has not been validated.\n You can take the following actions manually:\n 1.Manually Reserve Transfer: If the order has not actually been shipped to the customer, you can reserve the transfer manually.\n 2.Reset Sales Order to Draft: You have the option to reset the sales order to draft status. After doing so, you can modify the shipping address and then confirm the order again."
        pos_order = order_data.get("source_name", "") == "pos"
        partner, delivery_address, invoice_address = self.prepare_shopify_customer_and_addresses(
            order_data, pos_order, instance, queue_line)
        if not partner:
            return False
        if self.partner_id.id != partner.id:
            need_update_partner = True
        if self.partner_shipping_id.id != delivery_address.id:
            need_update_shipping_partner = True
        if self.partner_invoice_id.id != invoice_address.id:
            if self.state != 'draft' and self.invoice_ids:
                message = "The user manually updated customer details in Shopify, but the system did not update them because an invoice has already been posted in the system.\n The system will update customer details only under the following conditions:\n 1.The invoice has not been posted.\n 2.The delivery order has not been validated.\n You can take following actions Manually\n 1. Reset to Draft Invoice & Modify Invoice address"
            need_update_invoice_partner = True
        if message and need_update_partner:
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, message=message,
                                                           module="shopify_ept",
                                                           model_name='sale.order',
                                                           order_ref=order_data.get('name'),
                                                           shopify_order_data_queue_line_id=queue_line.id if queue_line else False)
            queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
        elif message and need_update_invoice_partner:
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, message=message,
                                                           module="shopify_ept",
                                                           model_name='sale.order',
                                                           order_ref=order_data.get('name'),
                                                           shopify_order_data_queue_line_id=queue_line.id if queue_line else False)
            queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
        elif message and need_update_shipping_partner:
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, message=message,
                                                           module="shopify_ept",
                                                           model_name='sale.order',
                                                           order_ref=order_data.get('name'),
                                                           shopify_order_data_queue_line_id=queue_line.id if queue_line else False)
            queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
        else:
            if need_update_partner:
                self.write({'partner_id': partner.id})
                note = "<p>Customer has updated via webhook</p>"
                self.message_post(body=note)
            if need_update_invoice_partner:
                self.write({'partner_invoice_id': invoice_address.id})
                note = "<p>Invoice Address has updated via webhook</p>"
                self.message_post(body=note)
            if need_update_shipping_partner:
                self.write({'partner_shipping_id': delivery_address.id})
                transfers = self.picking_ids.filtered(
                    lambda x: x.location_dest_id.usage == "customer" and x.state != ("done", "cancel"))
                transfers.write({'partner_id': delivery_address.id})
                note = "<p>Delivery Address has updated via webhook</p>"
                self.message_post(body=note)
            queue_line.state = "done"

    def update_qty_in_order_webhook_ept(self, instance, queue_line, order_data):
        """
        This method is use to update qty in the order.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13 October 2023 .
        """
        sale_line_obj = self.env['sale.order.line']
        common_log_line_obj = self.env["common.log.lines.ept"]
        response_data, shopify_line_ids = self.prepare_response_data_of_order_qty(order_data)
        discount_data = response_data.pop('discount_data')
        existing_order_qty_data = self.prepare_existing_order_data_of_qty(response_data)
        data = []
        is_updated_qty = False
        for shopify_line_id in shopify_line_ids:
            r_qty = response_data.get(shopify_line_id)
            e_o_qty = existing_order_qty_data.get(shopify_line_id) or 0.0
            if not e_o_qty and r_qty == e_o_qty:
                queue_line.write({'state': 'done', 'processed_at': datetime.now()})
                continue
            effective_qty = r_qty - e_o_qty
            if effective_qty == 0:
                queue_line.write({'state': 'done', 'processed_at': datetime.now()})
                continue
            order_line = sale_line_obj.search([('order_id', '=', self.id), ('shopify_line_id', '=', shopify_line_id)],
                                              limit=1)
            if effective_qty < 0:
                n_update_qty = -1 * r_qty
                if n_update_qty < 0:
                    n_update_qty = n_update_qty * -1
                delivered_qty = order_line.qty_delivered
                if n_update_qty < delivered_qty:
                    message = "The user manually adjusted the quantity in Shopify. However, it is not possible to automatically adjust the quantity in Odoo because the product %s has already been delivered in order  %s.\n \
You can take the following actions manually:\n 1. Reserve Order: If the order has not been shipped to the customer from your warehouse yet, you can reserve the order line with same quantity.\n 3. Create Credit Note: If an invoice has already been created, you can generate a credit note accordingly for that quantity." % (
                        order_line.product_id.default_code, self.name)
                    common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, message=message,
                                                                   module="shopify_ept",
                                                                   model_name='sale.order',
                                                                   order_ref=order_data.get('name'),
                                                                   shopify_order_data_queue_line_id=queue_line.id if queue_line else False)
                    queue_line.write({'state': 'failed', 'processed_at': datetime.now()})
                    continue
                data.append([1, order_line.id, {'product_uom_qty': n_update_qty}])
                is_updated_qty = True
            elif effective_qty > 0:
                total_qty = effective_qty + e_o_qty
                data.append([1, order_line.id, {'product_uom_qty': total_qty}])
                is_updated_qty = True
        if is_updated_qty and discount_data:
            discount_line_data = self.update_discount_price_in_order_line_webhook(discount_data, order_data)
        work_flow_process_record = self.auto_workflow_process_id
        if is_updated_qty and work_flow_process_record:
            queue_line.write({'state': 'done', 'processed_at': datetime.now()})
            order_lines = self.mapped('order_line').filtered(lambda l: l.product_id.invoice_policy == 'order')
            self.write({'order_line': data})
            if not order_lines.filtered(
                    lambda l: l.product_id.type == 'consu' and l.product_id.is_storable) and len(
                self.order_line) != len(order_lines.filtered(
                lambda l: l.product_id.type in ['service', 'consu'] and not l.product_id.is_storable)):
                return True
            if instance.update_qty_to_invoice_order_webhook:
                self.webhook_call_auto_invoice_workflow(work_flow_process_record)

    def webhook_call_auto_invoice_workflow(self, work_flow_process_record):
        """
        This method is use to call the auto invoice workflow process
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 17 October 2023 .
        """
        if work_flow_process_record.create_invoice:
            if work_flow_process_record.invoice_date_is_order_date:
                if self.check_fiscal_year_lock_date_ept():
                    return True
            invoiceable_lines = self.with_context(shopify_filter_already_invoiced_lines=True)._get_invoiceable_lines(
                False)
            invoices = self.env['account.move']
            if invoiceable_lines:
                if work_flow_process_record.sale_journal_id:
                    invoices = self.with_context(journal_ept=work_flow_process_record.sale_journal_id,
                                                 shopify_filter_already_invoiced_lines=True)._create_invoices(
                        final=True)
                else:
                    invoices = self.with_context(shopify_filter_already_invoiced_lines=True)._create_invoices(
                        final=True)
            self.validate_invoice_ept(invoices)
            if work_flow_process_record.register_payment:
                self.paid_invoice_ept(invoices)

    def update_discount_price_in_order_line_webhook(self, discount_data, order_response):
        """
        Zeros out the existing discount line's quantity and creates a new, financially
        accurate discount line using the existing helper method, ONLY IF the new
        total discount amount is greater than the current total discount.

        :param discount_data: Dict {discount_line_id_key: new_total_discount_amount}
        :param order_response: Dictionary containing the Shopify order response data.
        :returns: boolean indicating if any discount line was updated/created.
        """
        instance = self.shopify_instance_id
        is_updated = False

        existing_discount_lines = self.order_line.filtered(lambda ol: ol.shopify_related_line_id)
        original_order_lines = self.order_line - existing_discount_lines

        for shopify_line_id_key, new_total_discount_amount in discount_data.items():
            original_shopify_id = shopify_line_id_key.replace('discount_', '')
            discount_line_to_zero = existing_discount_lines.filtered(
                lambda ol: ol.shopify_related_line_id == shopify_line_id_key and not float_is_zero(ol.product_uom_qty,
                                                                                                   precision_digits=4)
            )
            original_line = original_order_lines.filtered(
                lambda ol: ol.shopify_line_id == original_shopify_id
            )
            if not discount_line_to_zero or not original_line:
                continue
            discount_line_record = discount_line_to_zero[0]
            original_product_line_record = original_line[0]  # The line used for 'previous_line' context
            current_total_discount = abs(discount_line_record.price_unit)
            if float_compare(new_total_discount_amount, current_total_discount, precision_digits=4) == 1:
                _logger.info(
                    "Discount increased detected for Shopify line ID %s. Zeroing out old line and creating new for order %s.",
                    original_shopify_id, self.name)
                if discount_line_record.product_uom_qty > 0:
                    discount_line_record.write({'product_uom_qty': 0.0})
                new_price_unit = float(new_total_discount_amount) * -1
                order_line = self.shopify_create_sale_order_line({}, instance.discount_product_id,
                                                                 1,  # Quantity is 1
                                                                 original_product_line_record.product_id.name,
                                                                 # Product name for line description
                                                                 new_price_unit, order_response,
                                                                 previous_line=original_product_line_record,
                                                                 is_discount=True
                                                                 )
                is_updated = True
            # If the discount decreased/removed, the update is skipped.
        return is_updated

    def prepare_response_data_of_order_qty(self, order_data):
        """
        This method is use to prepare quantity data as received into the response.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13 October 2023 .
        """
        response_data = {}
        shopify_line_ids = []
        discount_data = {}
        instance = self.shopify_instance_id
        for line in order_data.get('line_items'):
            line_id = line.get('id')
            if order_data.get('financial_status') == "refunded":
                qty = int(line.get('quantity'))
            elif line.get('fulfillment_status') == 'not_eligible' or not line.get('requires_shipping', True):
                # Non-shippable / service lines (e.g. Tip, digital products) always have
                # fulfillable_quantity=0 because they are never physically fulfilled.
                # Use current_quantity (quantity after any refunds/edits) instead.
                qty = int(line.get('current_quantity') or line.get('quantity') or 0)
            else:
                qty = int(line.get('fulfillable_quantity'))
            discount_amount = self._get_shopify_discount_allocation_amount(instance, line, order_data)
            if discount_amount > 0.0:
                discount_key = f"discount_{line_id}"
                discount_data[discount_key] = discount_amount
            if response_data.get(line_id):
                qty = qty + response_data.get(line_id)
                response_data.update({line_id: qty})
            else:
                response_data.update({line_id: qty})
            shopify_line_ids.append(line_id)
        response_data['discount_data'] = discount_data
        return response_data, shopify_line_ids

    def prepare_existing_order_data_of_qty(self, response_data):
        """
        This method is use to prepare data of existing order.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13 October 2023 .
        """
        data = {}
        for line in self.order_line.filtered(lambda ol: ol.shopify_line_id):
            line_id = int(line.shopify_line_id)
            qty = line.product_uom_qty
            if line.qty_delivered > 0:
                remaining = qty - line.qty_delivered
                qty = remaining + line.qty_delivered
            if data.get(line_id):
                qty = qty + data.get(line_id)
                data.update({line_id: qty})
            else:
                data.update({line_id: qty})
        return data

    def _get_shopify_discount_allocation_amount(self, instance, line, order_data):
        """
        This method is use to get the discount allocation amount for the order line.
        """
        discount_amount = 0.0
        for discount_allocation in line.get('discount_allocations', []):
            if instance.order_visible_currency:
                discount_total_price = self.get_price_based_on_customer_visible_currency(
                    discount_allocation.get("amount_set"), order_data, 0)
                if discount_total_price:
                    discount_amount += float(discount_total_price)
            else:
                discount_amount += float(discount_allocation.get("amount"))
        return discount_amount

    def cancel_shopify_order(self):
        """
        Cancelled the sale order when it is cancelled in Shopify Store with full refund.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13-Jan-2020..
        """
        reverse_date = time.strftime("%Y-%m-%d %H:%M:%S")
        reverse_move_date = str(reverse_date)
        if "done" in self.picking_ids.mapped("state"):
            for picking_id in self.picking_ids:
                picking_id.write({'updated_in_shopify': True})
                picking_id.message_post(
                    body=_("Order %s has been canceled in the Shopify store.", self.shopify_order_number))
            return False
        self.with_context(disable_cancel_warning=True).action_cancel()
        self.canceled_in_shopify = True
        self.write({'shopify_order_status': 'Canceled'})
        if "draft" in self.invoice_ids.mapped("state"):
            for invoice_id in self.invoice_ids:
                invoice_id.message_post(
                    body=_("Order %s has been canceled in the Shopify store.", self.shopify_order_number))
                invoice_id.button_cancel()

        # Calling the credit note creation process to prevent duplication creation of the refunds.
        context_dict = self.env.context
        shopify_status = context_dict.get('shopify_status')
        order_data = context_dict.get('order_data')
        if shopify_status in ["refunded", "partially_refunded"] and order_data.get(
                "refunds"):
            created_by = context_dict.get('created_by')
            queue_line = context_dict.get('queue_line')
            self.process_order_refund_data_ept(shopify_status, order_data, self, created_by,
                                               queue_line.shopify_instance_id,
                                               queue_line)
        # Check For Refunds created for order or not.
        refund_invoices = self.invoice_ids.filtered(
            lambda x: x.move_type == "out_refund" and x.state == "posted")
        if not refund_invoices:
            # Creating Refunds for invoces of the cancel orders.When refund is not done
            invoices = self.invoice_ids.filtered(lambda x: x.move_type == "out_invoice" and x.state == "posted")
            for invoice_id in invoices:
                move_reversal = self.env["account.move.reversal"].with_context(
                    {"active_model": "account.move", "active_ids": invoice_id.ids},
                    check_move_validity=False).create(
                    {"reason": "Cancel from shopify store",
                     "journal_id": invoice_id.journal_id.id, "date": reverse_move_date})
                move_reversal.reverse_moves()
                new_move = move_reversal.new_move_ids
                if new_move.state == 'draft':
                    new_move.with_context(is_shopify_reverse_move_ept=True).action_post()
        return True

    def fulfilled_shopify_order(self, order_data):
        """
        If order is not confirmed yet, confirms it first.
        Make the picking done, when order will be fulfilled in Shopify.
        This method is used for Update order webhook.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13-Jan-2020..
        """
        if self.state not in ["sale", "done", "cancel"]:
            self.action_confirm()
        return self.fulfilled_picking_for_shopify(self.picking_ids.filtered(
            lambda x: x.location_dest_id.usage == "customer" and x.state not in ("done", "cancel")), order_data)

    def fulfilled_picking_for_shopify(self, pickings, order_data=False):
        """
        It will make the pickings done.
        This method is used for Update order webhook.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13-Jan-2020..
        """
        fulfillment_data_id = ''
        carrier_id = False
        message = ''
        if order_data and self.shopify_instance_id:
            delivery_carrier = self.env['delivery.carrier']
            fulfillment_data = order_data.get('fulfillments')[-1] if order_data.get('fulfillments') else {}
            carrier_id = delivery_carrier.search_carrier_for_webhook_fulfillment(self.shopify_instance_id,
                                                                                 fulfillment_data)
            tracking_number = fulfillment_data.get('tracking_number')
            fulfillment_data_id = fulfillment_data.get('id')
        for picking in pickings.filtered(lambda x: x.state not in ['cancel', 'done']):
            if not self.shopify_instance_id.forcefully_reserve_stock_webhook:
                if picking.state != "assigned":
                    if picking.move_ids.move_orig_ids:
                        completed = self.fulfilled_picking_for_shopify(picking.move_ids.move_orig_ids.picking_id)
                        if not completed:
                            return False
                    picking.action_assign()
                    # # Add by Vrajesh Dt.01/04/2020 automatically validate delivery when import POS
                    # order in shopify
                    if picking.sale_id and (
                            picking.sale_id.is_pos_order or picking.sale_id.shopify_order_status == "fulfilled"):
                        for move_id in picking.move_ids.filtered(lambda move: not move.move_line_ids.result_package_id):
                            vals = self.prepare_vals_for_move_line(move_id, picking)
                            picking.move_line_ids.create(vals)
                        picking._action_done()
                        return True
                    if picking.state != "assigned":
                        return False
                self.transfer_validate_ept(picking)
                if picking.state == "done":
                    picking.message_post(body=_("Picking is done by Webhook as Order is fulfilled in Shopify."))
                    vals = {'updated_in_shopify': True, 'shopify_fulfillment_id': fulfillment_data_id}
                    if carrier_id:
                        vals.update({'carrier_id': carrier_id.id, 'carrier_tracking_ref': tracking_number})
                    picking.write(vals)
            else:
                if picking.state in ("done", "cancel"):
                    continue
                if picking.state == "assigned":
                    self.transfer_validate_ept(picking)
                    message = "Picking is done by Webhook as Order is fulfilled in Shopify."
                if picking.state not in ("assigned", "done") and all(
                        move.product_id.tracking == 'none' for move in picking.move_ids):
                    need_validate_transfer = False
                    for move in picking.move_ids.filtered(lambda move: not move.move_line_ids.result_package_id):
                        move._action_assign()
                        move._set_quantity_done(move.product_uom_qty)
                        need_validate_transfer = True
                    if need_validate_transfer:
                        message = "Picking is forcefully done by Webhook as Order is fulfilled in Shopify."
                        self.transfer_validate_ept(picking)
                elif picking.state not in ("assigned", "done") and any(
                        move.product_id.tracking != 'none' for move in picking.move_ids):
                    need_validate_transfer = False
                    for move in picking.move_ids.filtered(lambda move: not move.move_line_ids.result_package_id):
                        move.picked = False
                        move._action_assign()
                        # move._set_quantity_done(move.product_uom_qty)
                        need_validate_transfer = True
                    if need_validate_transfer and picking.state == 'assigned':
                        message = "Picking is forcefully done by Webhook as Order is fulfilled in Shopify."
                        self.transfer_validate_ept(picking)
                if picking.state == "done":
                    picking.message_post(body=_(message))
                    vals = {'updated_in_shopify': True, 'shopify_fulfillment_id': fulfillment_data_id}
                    if carrier_id:
                        vals.update({'carrier_id': carrier_id.id, 'carrier_tracking_ref': tracking_number})
                    picking.write(vals)
        return True

    def prepare_vals_for_move_line(self, move_id, picking):
        """ This method used to prepare a vals for move line.
            @return: vals
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id: 167537
        """
        vals = {
            'product_id': move_id.product_id.id,
            'product_uom_id': move_id.product_id.uom_id.id,
            'qty_done': move_id.product_uom_qty,
            'location_id': move_id.location_id.id,
            'picking_id': picking.id,
            'location_dest_id': move_id.location_dest_id.id,
        }
        return vals

    def create_shopify_partially_refund(self, refunds_data, order_name, created_by="", shopify_financial_status=""):
        """This method is used to check the required validation before create
            a partial refund and call child methods for a partial refund.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 17/05/2021.
            Task Id : 173066 - Manage Partial refund in the Shopify
        """
        account_move_obj = self.env['account.move']
        message = False
        if shopify_financial_status == "refunded":
            shopify_financial_status = "Refunded"
        else:
            shopify_financial_status = "Partially Refunded"
        if not self.invoice_ids:
            message = ("System tried to generate a refund, but no invoice was found for Order: %s in Odoo.\n"
                       "Action Items:\n"
                       "- Create and validate the invoice manually from the Sales Order.\n"
                       "- Reprocess the queue.") % order_name
            return message
        invoices = self.invoice_ids.filtered(lambda x: x.move_type == "out_invoice")
        for invoice in invoices:
            if not invoice.state == "posted":
                message = ("System tried to generate a refund, but the invoice for Order: %s is not in the Posted state.\n"
                              "Action Items:\n"
                              "- Create and validate the invoice manually from the Sales Order.\n"
                              "- Reprocess the queue.") % order_name
                return message
        refund_invoices = self.invoice_ids.filtered(lambda x: x.move_type == "out_refund" and x.state == "posted")
        if refund_invoices:
            total_refund_amount = 0.0
            for refund_invoice in refund_invoices:
                total_refund_amount += refund_invoice.amount_total
            if total_refund_amount == self.amount_total:
                return
        for refund_data_line in refunds_data:
            if (refund_data_line.get('refund_line_items') or refund_data_line.get('order_adjustments')
                    or refund_data_line.get('refund_shipping_lines')):
                existing_refund = account_move_obj.search([("shopify_refund_id", "=", refund_data_line.get('id')),
                                                           ("shopify_instance_id", "=", self.shopify_instance_id.id)])
                if existing_refund:
                    continue
                new_move, payment_id = self.with_context(
                    check_move_validity=False).create_move_and_delete_not_necessary_line(
                    refund_data_line, invoices, created_by, shopify_financial_status)
                if refund_data_line.get('order_adjustments') or refund_data_line.get('refund_shipping_lines'):
                    self.create_refund_adjustment_line(
                        refund_data_line.get('order_adjustments', []),
                        new_move,
                        refund_data_line.get('refund_shipping_lines', [])
                    )
                # new_move.with_context(check_move_validity=False)._recompute_dynamic_lines()
                new_move.with_context(**{'check_move_validity': False})._sync_dynamic_lines({'records': new_move})
                if not new_move.invoice_line_ids:
                    if payment_id:
                        payment_id.unlink()
                    new_move.unlink()
                    _logger.info(
                        f"For Shopify Refund {refund_data_line.get('id')}, No move lines created in odoo, deleting the credit note.")
                    continue
                if new_move.state == 'draft':
                    new_move.with_context(is_shopify_reverse_move_ept=True).action_post()
                    self.message_post(body=Markup(_(
                        "Credit note created <a href='#' data-oe-model='account.move' data-oe-id='%d'>%s</a> via %s") % (
                        new_move.id, new_move.name or new_move.highest_name or '/', created_by)))
                    new_move.message_post(body=Markup(
                        _("This credit note has been created via webhook: <a href='#' data-oe-model='sale.order' data-oe-id='%s'>%s</a>")) % (
                        self.id, self.name))
                    if payment_id:
                        if payment_id.amount != new_move.amount_total:
                            # Case : When an order adjustment is made, the payment amount and the credit note amount may differ.
                            #         In such scenarios, the payment amount should be adjusted to match the credit note amount.
                            payment_id.write({'amount': new_move.amount_total})
                        payment_id.action_post()
                        self.reconcile_payment_ept(payment_id, new_move)
        return message

    def create_move_and_delete_not_necessary_line(self, refunds_data, invoices, created_by, shopify_financial_status):
        """This method is used to create a reverse move of invoice and delete the invoice lines from the newly
            created move which product not refunded in Shopify.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 19/05/2021.
            Task Id : 173066 - Manage Partial refund in the Shopify
        """
        payment_id = False
        delete_move_lines = self.env['account.move.line']
        shopify_line_ids = []
        shopify_line_ids_with_qty = {}
        for refund_line in refunds_data.get('refund_line_items'):
            order_line_item_id = refund_line.get('line_item_id') or refund_line.get('line_item').get('id')
            shopify_line_ids.append(order_line_item_id)
            # shopify_line_ids_with_qty.update({refund_line.get('line_item_id'): refund_line.get('quantity')})
            refund_line_item_id = order_line_item_id
            if refund_line_item_id in shopify_line_ids_with_qty.keys():
                shopify_line_ids_with_qty.update(({
                    refund_line_item_id: shopify_line_ids_with_qty.get(refund_line_item_id) + refund_line.get(
                        'quantity')}))
            else:
                shopify_line_ids_with_qty.update({refund_line_item_id: refund_line.get('quantity')})

        refund_date = self.convert_order_date(refunds_data)
        move_reversal = self.env["account.move.reversal"].with_context(
            {"active_model": "account.move", "active_ids": invoices[0].ids}, check_move_validity=False).create(
            {"reason": "Partially Refunded from shopify" if len(refunds_data) > 1 else refunds_data.get("note"),
             "journal_id": invoices[0].journal_id.id, "date": refund_date})

        move_reversal.reverse_moves()
        new_move = move_reversal.new_move_ids
        # code for create payment for credit note
        if self.shopify_instance_id.credit_note_register_payment:
            payment_id = self.credit_note_register_payment(new_move)
        # code for create payment for credit note
        new_move.write({'is_refund_in_shopify': True, 'shopify_refund_id': refunds_data.get('id')})
        total_qty = 0.0
        total_sale_line_qty = 0.0
        need_to_apply_discount = True
        for new_move_line in new_move.invoice_line_ids:
            sale_line_qty = new_move_line.sale_line_ids.product_uom_qty
            shopify_line_id = new_move_line.sale_line_ids.shopify_line_id
            if need_to_apply_discount and new_move_line.product_id.id == self.shopify_instance_id.discount_product_id.id:
                new_move_line.price_unit = new_move_line.price_unit / total_sale_line_qty * total_qty
            elif shopify_line_id and int(shopify_line_id) not in shopify_line_ids:
                delete_move_lines += new_move_line
                need_to_apply_discount = False
                # delete_move_lines.compute_all_tax_dirty = True
            else:
                new_move_line.quantity = shopify_line_ids_with_qty.get(int(shopify_line_id))
                # new_move_line.compute_all_tax_dirty = True
                total_qty = new_move_line.quantity
                total_sale_line_qty = new_move_line.sale_line_ids.product_uom_qty
                need_to_apply_discount = True
                # self.set_price_based_on_refund(new_move_line)


        if delete_move_lines:
            # delete_move_lines.with_context(check_move_validity=False).write({'quantity': 0})
            delete_move_lines.with_context(check_move_validity=False).unlink()
            # new_move.with_context(check_move_validity=False)._recompute_dynamic_lines()
        return new_move, payment_id

    def credit_note_register_payment(self, new_move):
        """
        This Method is used for register payment for credit note
        """
        account_payment_obj = self.env['account.payment']
        instance_id = new_move.shopify_instance_id
        vals = self.shopify_prepare_credit_note_payment_dict(instance_id, new_move)
        vals.update({'amount': new_move.amount_total})
        payment_id = account_payment_obj.create(vals)
        return payment_id

    def shopify_prepare_credit_note_payment_dict(self, instance_id, new_move):
        """ This method use to prepare a vals dictionary for payment."""
        return {
            'journal_id': instance_id.credit_note_payment_journal.id if instance_id.credit_note_payment_journal.id else self.auto_workflow_process_id.journal_id.id,
            'memo': new_move.payment_reference,
            'currency_id': new_move.currency_id.id,
            'payment_type': 'outbound',
            'date': new_move.date,
            'partner_id': new_move.commercial_partner_id.id,
            'amount': new_move.amount_residual,
            'payment_method_id': self.auto_workflow_process_id.inbound_payment_method_id.id,
            'partner_type': 'customer'
        }

    def set_price_based_on_refund(self, move_line):
        """
        Calculate tax price based on quantity and set in move line amount.
        @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 01/07/2022.
        Task Id : 194381 - Shopify refund issue fix
        """
        total_adjust_amount = 0.0
        for line in move_line.sale_line_ids:
            if move_line.quantity != line.product_uom_qty:
                tax_dict = line.order_id.tax_totals
                sub_total_tax_dict = tax_dict.get('groups_by_subtotal').get('Untaxed Amount')
                total_tax_amount = 0.0
                if sub_total_tax_dict:
                    for tax in sub_total_tax_dict:
                        total_tax_amount += tax.get('tax_group_amount')
                    total_adjust_amount = total_tax_amount / line.product_uom_qty
        move_line.price_unit += total_adjust_amount
        return True

    def _get_refund_adjustment_amount(self, adjustment, amount_key='amount', amount_set_key='amount_set'):
        """Return the amount for a refund adjustment entry, respecting the visible currency setting.
            amount_key / amount_set_key let callers point at the right fields:
              - order_adjustments  → 'amount' / 'amount_set'  (REST and GQL)
              - refund_shipping_lines (GQL) → 'subtotal_amount' / 'subtotal_amount_set'
        """
        if self.shopify_instance_id.order_visible_currency:
            amount_set = adjustment.get(amount_set_key, {})
            shop_money = amount_set.get('shop_money', {})
            presentment_money = amount_set.get('presentment_money', {})
            presentment_currency = self.pricelist_id.currency_id.name
            if shop_money.get('currency_code') == presentment_currency and float(shop_money.get('amount', 0.0)) != 0.0:
                return float(shop_money.get('amount', 0.0))
            if presentment_money.get('currency_code') == presentment_currency and float(presentment_money.get('amount', 0.0)) != 0.0:
                return float(presentment_money.get('amount', 0.0))
        return float(adjustment.get(amount_key, 0.0))

    def create_refund_adjustment_line(self, order_adjustments, move_ids, refund_shipping_lines=None):
        """This method is used to create invoice lines in a new move to manage the adjustment refund.
            Non-shipping adjustments are summed into one line (no tax — adjustment product default).
            Shipping is created as a separate line copying the product and taxes from the original
            invoice shipping line so that tax amounts are correctly reflected on the credit note.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 19/05/2021.
            Task Id : 173066 - Manage Partial refund in the Shopify
        """
        instance = self.shopify_instance_id
        # REST puts shipping refunds inside order_adjustments (kind='shipping_refund');
        # GQL provides them separately in refund_shipping_lines.
        shipping_adjustments = [a for a in order_adjustments if a.get('kind') == 'shipping_refund']
        other_adjustments = [a for a in order_adjustments if a.get('kind') != 'shipping_refund']

        # A sale order line is only needed when the credit note has no regular line items already
        # linking it to the SO. When line items exist the link is already established via their
        # inherited sale_line_ids, and creating extra order lines produces phantom SO lines.
        # When only adjustments are present, create exactly one linking order line (not one per call).
        already_linked = any(line.sale_line_ids for line in move_ids.invoice_line_ids)
        so_line_created = False

        if other_adjustments:
            adjustment_product = instance.refund_adjustment_product_id or self.env.ref(
                'shopify_ept.shopify_refund_adjustment_product', False)
            adjustments_amount = sum(self._get_refund_adjustment_amount(a) for a in other_adjustments)
            if abs(adjustments_amount) > 0:
                total_amt = move_ids.amount_total + adjustments_amount
                adjustments_amount = (abs(adjustments_amount)
                                      if float_compare(total_amt, 0.0, precision_digits=2) < 0
                                      else -adjustments_amount)
                create_ol = not already_linked and not so_line_created
                self._create_refund_move_line(adjustment_product, adjustments_amount, move_ids,
                                              create_order_line=create_ol)
                if create_ol:
                    so_line_created = True

        # GQL shipping lines take priority; fall back to REST shipping_adjustments
        if refund_shipping_lines or shipping_adjustments:
            orig_invoices = self.invoice_ids.filtered(
                lambda x: x.move_type == "out_invoice" and x.state == "posted")
            shipping_inv_line = self.env['account.move.line']
            for inv in orig_invoices:
                line = inv.invoice_line_ids.filtered(
                    lambda l: l.sale_line_ids and any(sol.is_delivery for sol in l.sale_line_ids))
                if line:
                    shipping_inv_line = line[0]
                    break
            if shipping_inv_line:
                shipping_tax_ids = shipping_inv_line.tax_ids
                # When the tax is price-inclusive, price_unit must be the gross (net + tax) so
                # Odoo correctly extracts the tax portion. When exclusive, pass only the net and
                # Odoo adds the tax on top. Both paths produce the same credit note total.
                has_price_include_tax = any(t.price_include for t in shipping_tax_ids)
                if refund_shipping_lines:
                    # GQL: subtotal_amount is net (pre-tax); tax_amount is the tax portion
                    net_amount = sum(
                        self._get_refund_adjustment_amount(
                            sl, amount_key='subtotal_amount', amount_set_key='subtotal_amount_set')
                        for sl in refund_shipping_lines)
                    tax_amount = (sum(
                        self._get_refund_adjustment_amount(
                            sl, amount_key='tax_amount', amount_set_key='tax_amount_set')
                        for sl in refund_shipping_lines) if has_price_include_tax else 0.0)
                else:
                    # REST: amount is net (pre-tax), negative sign = refund direction
                    net_amount = sum(
                        abs(self._get_refund_adjustment_amount(adj))
                        for adj in shipping_adjustments)
                    tax_amount = (sum(
                        abs(self._get_refund_adjustment_amount(
                            adj, amount_key='tax_amount', amount_set_key='tax_amount_set'))
                        for adj in shipping_adjustments) if has_price_include_tax else 0.0)
                price_unit = net_amount + tax_amount
                if price_unit:
                    create_ol = not already_linked and not so_line_created
                    self._create_refund_move_line(
                        shipping_inv_line.product_id, price_unit, move_ids,
                        tax_ids=shipping_tax_ids, create_order_line=create_ol)

    def _create_refund_move_line(self, product, amount, move_ids, tax_ids=None, create_order_line=True):
        """Create a credit note line and optionally a linked sale order line.
            tax_ids: when provided (shipping case) they override the product default taxes;
                     when None (other-adjustments case) the product's configured taxes apply.
            create_order_line: set False when the SO link is already established via line items.
        """
        account_move_line_obj = self.env['account.move.line']
        instance = self.shopify_instance_id
        move_vals = {'product_id': product.id, 'quantity': 1, 'price_unit': amount,
                     'move_id': move_ids.id, 'partner_id': move_ids.partner_id.id,
                     'name': product.display_name}
        if tax_ids is not None:
            move_vals['tax_ids'] = [(6, 0, tax_ids.ids)]
        if instance.shopify_analytic_account_id:
            analytic_account = self.env['account.analytic.account']
            if instance.use_channel_analytic_account and instance.use_graphql_api:
                channel_handle = self.env.context.get('shopify_channel_handle') or False
                channel_app_id = self.env.context.get('shopify_channel_app_id') or False
                analytic_account = self.env["shopify.channel.ept"].get_channel_analytic(
                    instance, channel_handle, channel_app_id)
            if analytic_account:
                move_vals['analytic_distribution'] = {analytic_account.id: 200}
            else:
                move_vals['analytic_distribution'] = {instance.shopify_analytic_account_id.id: 200}
        new_move_vals = account_move_line_obj.new(move_vals)
        new_move_vals.with_context(round=False)._compute_totals()
        new_vals = account_move_line_obj._convert_to_write(
            {name: new_move_vals[name] for name in new_move_vals._cache})
        effective_tax_ids = tax_ids.ids if tax_ids is not None else new_move_vals.tax_ids.ids
        new_vals.update({'quantity': 1, 'price_unit': amount, 'tax_ids': [(6, 0, effective_tax_ids)]})
        move_line_id = account_move_line_obj.with_context(check_move_validity=False).create(new_vals)
        if create_order_line:
            order_line_vals = self.prepare_vals_for_sale_order_line(product, product.display_name, amount, 0)
            order_line_vals.update(
                {'invoice_lines': [(6, 0, [move_line_id.id])], 'tax_ids': [(6, 0, effective_tax_ids)]})
            self.env['sale.order.line'].create(order_line_vals)

    def _prepare_invoice(self):
        """This method used set a shopify instance in customer invoice.
            @param : self
            @return: inv_val
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20/11/2019.
            Task Id : 157911
        """
        inv_val = super(SaleOrder, self)._prepare_invoice()
        if self.shopify_instance_id:
            inv_val.update({'shopify_instance_id': self.shopify_instance_id.id,
                            'is_shopify_multi_payment': self.is_shopify_multi_payment})
        return inv_val

    def action_open_cancel_wizard(self):
        """This method used to open a wizard to cancel order in Shopify.
            @param : self
            @return: action
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20/11/2019.
            Task Id : 157911
        """
        view = self.env.ref('shopify_ept.view_shopify_cancel_order_wizard')
        context = dict(self.env.context)
        context.update({'active_model': 'sale.order', 'active_id': self.id, 'active_ids': self.ids})
        return {
            'name': _('Cancel Order In Shopify'),
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'shopify.cancel.refund.order.wizard',
            'views': [(view.id, 'form')],
            'view_id': view.id,
            'target': 'new',
            'context': context
        }

    def process_order_fullfield_qty(self, order_response):
        """ This method is used to search order line which product qty need to create stock move.
            :param order_response: Response of shopify order.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 31 December 2020 .
            Task_id: 169381 - Gift card order import changes
        """
        module_obj = self.env['ir.module.module']
        mrp_module = module_obj.sudo().search([('name', '=', 'mrp'), ('state', '=', 'installed')])
        lines = order_response.get("line_items")
        bom_lines = []
        for line in lines:
            shopify_line_id = line.get('id')
            sale_order_line = self.order_line.filtered(lambda order_line: int(
                order_line.shopify_line_id) == shopify_line_id and order_line.product_id.type != 'service')
            if not sale_order_line:
                continue
            fulfilled_qty = float(line.get('quantity')) - float(line.get('fulfillable_quantity'))
            if mrp_module:
                bom_lines = self.check_for_bom_product(sale_order_line.product_id)
            for bom_line in bom_lines:
                self.create_stock_move_of_fullfield_qty(sale_order_line, fulfilled_qty, bom_line)
            if fulfilled_qty > 0 and not mrp_module:
                self.create_stock_move_of_fullfield_qty(sale_order_line, fulfilled_qty)
        return True

    def create_stock_move_of_fullfield_qty(self, order_line, fulfilled_qty, bom_line=False):
        """ This method is used to create stock move which product qty is fullfield.
            :param order_line: Record of sale order line
            :param fulfilled_qty: Qty of product which needs to create a stock move.
            :param bom_line: Record of bom line
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 31 December 2020 .
            Task_id: 169381 - Gift card order import changes
        """
        stock_location_obj = self.env["stock.location"]
        customer_location = stock_location_obj.search([("usage", "=", "customer")], limit=1)
        if bom_line:
            product = bom_line[0].product_id
            product_qty = bom_line[1].get('qty', 0) * fulfilled_qty
            product_uom = bom_line[0].product_uom_id
        else:
            product = order_line.product_id
            product_qty = fulfilled_qty
            product_uom = order_line.product_uom_id
        if product and product_qty and product_uom:
            move_vals = self.prepare_val_for_stock_move(product, product_qty, product_uom, customer_location,
                                                        order_line)
            if bom_line:
                move_vals.update({'bom_line_id': bom_line[0].id})
            stock_move = self.env['stock.move'].create(move_vals)
            stock_move._action_assign()
            stock_move._set_quantity_done(fulfilled_qty)
            if product.tracking == 'none':
                stock_move.sudo().picked = True
                stock_move.with_context(is_connector=True)._action_done()
            else:
                res = self.process_with_tracking_stock_move(stock_move)
                if res is not None:
                    order_data_line = self.env.context.get('order_data_line')
                    product_ref = '[%s] %s' % (product.default_code, product.name) if product.default_code else product.name
                    message = 'Stock move is not done for Order %s | Product: %s | Reason: %s' % (self.name, product_ref, res)
                    self.env["common.log.lines.ept"].create_common_log_line_ept(
                        shopify_instance_id=self.shopify_instance_id.id, module="shopify_ept",
                        message=message,
                        model_name='sale.order', order_ref=self.shopify_order_id,
                        shopify_order_data_queue_line_id=order_data_line.id if order_data_line else False)
                    order_data_line.write({'state': 'failed', 'processed_at': datetime.now()})
        return True

    def prepare_val_for_stock_move(self, product, fulfilled_qty, product_uom, customer_location, order_line):
        """ Prepare vals for the stock move.
            @return vals
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 31 December 2020 .
            Task_id: 169381 - Gift card order import changes
        """
        vals = {
            # 'name': _('Auto processed move : %s') % product.display_name,
            'company_id': self.company_id.id,
            'product_id': product.id if product else False,
            'product_uom_qty': fulfilled_qty,
            'product_uom': product_uom.id if product_uom else False,
            'location_id': self.warehouse_id.lot_stock_id.id,
            'location_dest_id': customer_location.id,
            'state': 'confirmed',
            'sale_line_id': order_line.id
        }
        return vals

    def _get_invoiceable_lines(self, final=False):
        """Inherited base method to manage tax rounding in the invoice.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 14 May 2021.
            173531 - shopify tax rounding issue

        Also excludes lines already fully covered by posted customer invoices (out_invoice)
        when the context key 'shopify_filter_already_invoiced_lines' is True.
        This prevents duplicate invoicing after a refund/credit note resets qty_invoiced to 0,
        while still allowing genuinely new uninvoiced lines to be invoiced normally.

        @Changes By : Gopal Chouhan on date 29 april 2026
        """
        if self.shopify_instance_id:
            context = dict(self.env.context)
            context.update({'round': False})
            self.env = self.env(context=context)
        invoiceable_lines = super(SaleOrder, self)._get_invoiceable_lines(final)
        if self.env.context.get('shopify_filter_already_invoiced_lines'):
            invoiceable_lines = invoiceable_lines.filtered(
                lambda line: sum(
                    inv_line.quantity
                    for inv_line in line.invoice_lines
                    if inv_line.move_id.move_type == 'out_invoice'
                    and inv_line.move_id.state == 'posted'
                ) < line.product_uom_qty
            )
        return invoiceable_lines

    def paid_invoice_ept(self, invoices):
        """
        Override the common connector library method here to create separate payment records.
        Override by Meera Sidapara on date 16/11/2021.
        """
        self.ensure_one()
        account_payment_obj = self.env['account.payment']
        if self.is_shopify_multi_payment:
            for invoice in invoices:
                total_payment_sum = sum(
                    invoice.matched_payment_ids.filtered(lambda P: P.state in ['in_process', 'paid']).mapped('amount'))
                invoice_amount = invoice.amount_residual
                if (invoice_amount - total_payment_sum) > 0:
                    if invoice.amount_residual:
                        for payment in self.shopify_payment_ids:
                            if payment.payment_gateway_id.code != 'gift_card':
                                vals = invoice.prepare_payment_dict(payment.workflow_id)
                                vals.update({'amount': payment.amount})
                                payment_id = account_payment_obj.create(vals)
                                payment_id.action_post()
                                self.reconcile_payment_ept(payment_id, invoice)
            return True
        super(SaleOrder, self).paid_invoice_ept(invoices)

    def create_schedule_activity_against_loglines(self, log_lines, note):
        """
        Author : Meera Sidapara 27/10/2021 this method use for create schedule activity based on
        log book.
        :model: model use for the model
        :return: True
        Task Id: 179264
        """
        mail_activity_obj = self.env['mail.activity']
        ir_model_obj = self.env['ir.model']
        model_id = ir_model_obj.search([('model', '=', 'common.log.lines.ept')])
        if len(log_lines) > 0:
            for log_line in log_lines:
                activity_type_id = log_line and log_line.shopify_instance_id.shopify_activity_type_id.id
                date_deadline = datetime.strftime(
                    datetime.now() + timedelta(days=int(log_line.shopify_instance_id.shopify_date_deadline)),
                    "%Y-%m-%d")
                for user_id in log_line.shopify_instance_id.shopify_user_ids:
                    mail_activity = mail_activity_obj.search([('res_model_id', '=', model_id.id),
                                                              ('user_id', '=', user_id.id),
                                                              # ('res_name', '=', log_line.name),
                                                              ('activity_type_id', '=', activity_type_id)])
                    note_2 = "<p>" + note + '</p>'
                    if not mail_activity or mail_activity.note != note_2:
                        vals = {'activity_type_id': activity_type_id, 'note': note,
                                # 'summary': log_line.name,
                                'res_id': log_line.id, 'user_id': user_id.id or self._uid,
                                'res_model_id': model_id.id, 'date_deadline': date_deadline}
                        try:
                            mail_activity_obj.create(vals)
                        except Exception as error:
                            _logger.info("Unable to create schedule activity, Please give proper "
                                         "access right of this user :%s  ", user_id.name)
                            _logger.info(error)
        return True

    def action_order_ref_redirect(self):
        """
        This method is used to redirect Woocommerce order in WooCommerce Store.
        @author: Meera Sidapara on Date 31-May-2022.
        @Task: 190111 - Shopify APP features
        """
        self.ensure_one()
        url = '%s/admin/orders/%s' % (self.shopify_instance_id.shopify_host, self.shopify_order_id)
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }

    def partial_fulfilled_shopify_order(self, order_data, shopify_instance):
        """
        This method is use to allow partial fulfulled.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 12 October 2023 .
        """
        message = ''
        delivery_carrier = self.env['delivery.carrier']
        if self.state not in ["sale", "done", "cancel"]:
            self.action_confirm()
        for fulfillment_data in order_data.get('fulfillments'):
            picking_obj = self.env['stock.picking']
            fulfillment_data_id = fulfillment_data.get('id')
            if picking_obj.search([('shopify_fulfillment_id', '=', fulfillment_data_id),
                                   ('shopify_instance_id', '=', self.shopify_instance_id.id)]):
                continue
            fulfillment_product_data = {}
            for fulfillment_data_line in fulfillment_data.get('line_items'):
                sku = fulfillment_data_line.get('sku')
                quantity = int(fulfillment_data_line.get('quantity'))
                if fulfillment_product_data.get(sku):
                    quantity = quantity + fulfillment_product_data.get(sku)
                    fulfillment_product_data.update({sku: quantity})
                else:
                    fulfillment_product_data.update({sku: quantity})
            carrier_id = delivery_carrier.search_carrier_for_webhook_fulfillment(shopify_instance, fulfillment_data)
            tracking_number = fulfillment_data.get('tracking_number')
            for transfer in self.picking_ids.filtered(
                    lambda x: x.location_dest_id.usage == "customer" and x.state not in ("done", "cancel")):
                if not shopify_instance.forcefully_reserve_stock_webhook:
                    self.process_assigned_transfer_ept(transfer, fulfillment_product_data)
                    if transfer.state == "done":
                        message = "Picking is done by Webhook as Order is partial fulfilled in Shopify."
                        transfer.message_post(body=_(message))
                        vals = {'updated_in_shopify': True, 'shopify_fulfillment_id': fulfillment_data_id}
                        if carrier_id:
                            vals.update({'carrier_id': carrier_id.id, 'carrier_tracking_ref': tracking_number})
                        transfer.write(vals)
                    else:
                        return False
                else:
                    self.process_assigned_transfer_ept(transfer, fulfillment_product_data)
                    if transfer.state not in ("assigned", "done") and all(
                            move.product_id.tracking == 'none' for move in transfer.move_ids):
                        need_validate_picking = False
                        for move in transfer.move_ids.filtered(lambda move: not move.move_line_ids.result_package_id):
                            sku = move.product_id.default_code
                            if fulfillment_product_data.get(sku):
                                move._action_assign()
                                move._set_quantity_done(fulfillment_product_data.get(sku))
                                need_validate_picking = True
                        if need_validate_picking:
                            self.transfer_validate_ept(transfer)
                            message = "Picking is forcefully done by Webhook as Order is fulfilled in Shopify."
                    elif transfer.state == "assigned":
                        need_to_validate_picking = False
                        for move in transfer.move_ids.filtered(lambda move: not move.move_line_ids.result_package_id):
                            sku = move.product_id.default_code
                            if fulfillment_product_data.get(sku):
                                move._set_quantity_done(fulfillment_product_data.get(sku))
                                need_to_validate_picking = True
                            else:
                                move._set_quantity_done(0)
                        if need_to_validate_picking:
                            self.transfer_validate_ept(transfer)
                    if transfer.state == "done":
                        transfer.message_post(body=_(message))
                        vals = {'updated_in_shopify': True, 'shopify_fulfillment_id': fulfillment_data_id}
                        if carrier_id:
                            vals.update({'carrier_id': carrier_id.id, 'carrier_tracking_ref': tracking_number})
                        transfer.write(vals)
        return True

    def process_assigned_transfer_ept(self, transfer, fulfillment_product_data):
        """
        This method is use to process ready transfer while receive the partial fulfillment.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 12 October 2023 .
        """
        if all(move.product_id.tracking == 'none' for move in transfer.move_ids):
            need_to_validate_picking = False
            if transfer.state == 'assigned':
                for move in transfer.move_ids.filtered(lambda move: not move.move_line_ids.result_package_id):
                    sku = move.product_id.default_code
                    if fulfillment_product_data.get(sku):
                        move._set_quantity_done(fulfillment_product_data.get(sku))
                        need_to_validate_picking = True
                    else:
                        move._set_quantity_done(0)
            if need_to_validate_picking:
                self.transfer_validate_ept(transfer)

    def transfer_validate_ept(self, transfer):
        """
        This method is use to call button validate of transfer.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 12 October 2023 .
        """
        skip_sms = {"skip_sms": True}
        result = transfer.with_context(**skip_sms).button_validate()
        if isinstance(result, dict):
            dict(result.get("context")).update(skip_sms)
            context = result.get("context")  # Merging dictionaries.
            model = result.get("res_model", "")
            if model:
                record = self.env[model].with_context(context).create({})
                record.process()

    def _prepare_confirmation_values(self):
        """
        Inherited this method here for the webhook process. sale order data write in the picking date deadline
        and that deadline date write in the stock move as per default flow but the confirm sale order we
        update the order date in the sale order but in picking it is default so there need to set proper date otherwise
        getting issue while merge move process. def _merge_moves(self, merge_into=False) there merge move not found due to dead line date mismatch once
        update the quantity from the order
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 16 October 2023 .
        """
        res = super(SaleOrder, self)._prepare_confirmation_values()
        if self.shopify_instance_id:
            res.update({'date_order': self.date_order})
        return res

    def _normalize_name(self, name):
        """Normalize name for consistent comparison"""
        if not name:
            return ''
        name = urllib.parse.unquote(name)  # Decode URL-encoded values
        name = re.sub(r'[\\\/\s]+', ' ', name)  # Replace multiple slashes/backslashes/spaces with single space
        return name.strip()

    def _find_or_create_utm_record(self, model_obj, raw_name):
        """Common logic to find or create UTM source, medium, or campaign"""

        if not raw_name:
            return False
        normalized_name = self._normalize_name(raw_name)
        record = model_obj.search([('name', '=ilike', normalized_name)], limit=1)
        if record: return record
        all_records = model_obj.search([])
        for rec in all_records:
            if self._normalize_name(rec.name) == normalized_name:
                return rec
        return model_obj.create({'name': normalized_name})

    def check_and_create_return_picking(self, order_response, sale_order, created_by):
        """
        This method is used to create return.
        :param order_response:
        :param sale_order:
        :param created_by:
        :return:
        """
        message = ""
        stock_move_obj = self.env['stock.move']
        move_ids = stock_move_obj.search(
            [('picking_id', '=', False), ('sale_line_id', 'in', sale_order.order_line.ids), ('state', '=', 'done')])
        if not move_ids or sale_order.shopify_order_status != 'fulfilled':
            message = "Move is not available, so return can't be generated."
        return_data = order_response.get('returns', [])
        return_data_dict = self.prepare_return_data(return_data, sale_order)
        need_to_remove_lines = []
        return_wiz = self.env['stock.return.picking'].with_context(
            active_ids=sale_order.ids,
            active_id=sale_order.ids[0],
            active_model='sale.order',
            default_sale_order_ept_id=sale_order.ids[0]
        ).sudo().create({})
        return_wiz._onchange_sale_order_id()
        return_picking_ids = self.picking_ids.filtered(
            lambda x: x.picking_type_code == 'incoming' and x.state != 'cancel'
        )
        for return_move_line in return_wiz.product_return_moves:
            refund_line = next(
                (item for item in return_data_dict if item["product_id"] == return_move_line.product_id.id),
                None)
            if refund_line:
                qty_to_return = refund_line["quantity"]
                existing_return_qty = return_picking_ids.move_ids.filtered(
                    lambda x: x.product_id.id == refund_line["product_id"]).mapped('product_uom_qty')
                return_qty = sum(existing_return_qty)
                if qty_to_return > return_qty:
                    return_move_line.write({
                        'quantity': qty_to_return - return_qty,
                        'to_refund': True
                    })
                else:
                    need_to_remove_lines.append(return_move_line)
            else:
                need_to_remove_lines.append(return_move_line)
        for need_to_remove_line in need_to_remove_lines:
            need_to_remove_line.unlink()
        if return_wiz.product_return_moves:
            res = return_wiz.create_returns_ept()
            return_picking = self.env['stock.picking'].browse(res['res_id'])
            return_picking.updated_in_shopify = True
            return_picking.message_post(
                body=_("Return Picking is Generated by Webhook for Stock moves as Order is Returned in Shopify."))
            if return_picking:
                # if self.shopify_instance_id.stock_validate_for_return:
                return_picking.button_validate()
                for move in return_picking.move_ids:
                    matched = next((r for r in return_data_dict if r["product_id"] == move.product_id.id),None)
                    if matched and matched.get("return_reason"):
                        move.write({'shopify_return_reason': matched["return_reason"]})
                return_picking.message_post(body=_("Return Picking is Validate by Webhook."))

        return message

    def prepare_return_data(self, returns_data, sale_order):
        """
        Prepare return data dictionary of product and its quantity
        based on Shopify return response.
        """
        return_data_dict = []

        def add_product_qty(product, qty, return_reason=""):
            for item in return_data_dict:
                if item["product_id"] == product.id:
                    item["quantity"] += qty
                    if not item.get("return_reason"):
                        item["return_reason"] = return_reason
                    return
            return_data_dict.append({"product_id": product.id, "quantity": qty, "return_reason": return_reason})

        for return_data in returns_data:
            for line in return_data.get("return_line_items", []):
                qty = line.get("quantity") or 0
                if qty <= 0:
                    continue
                # Fetch return reason from returnReasonDefinition (GraphQL)
                reason_def = line.get("return_reason_definition") or {}
                return_reason = reason_def.get("name") or line.get("return_reason") or ""
                if return_reason.lower() == "other":
                    return_reason = line.get("return_reason_note") or return_reason
                fulfillment_line = line.get("fulfillment_line_item") or {}
                line_item = fulfillment_line.get("line_item") or {}
                shopify_line_id = str(line_item.get("id") or "")

                if not shopify_line_id:
                    continue

                order_line = sale_order.order_line.filtered(lambda l: l.shopify_line_id == shopify_line_id)
                if not order_line:
                    continue

                product = order_line.product_id

                # Handle BOM products
                bom_lines = self.check_for_bom_product(product)
                if bom_lines:
                    for bom_line in bom_lines:
                        add_product_qty(bom_line[0].product_id, bom_line[1].get("qty", 0) * qty, return_reason)
                else:
                    add_product_qty(product, qty, return_reason)

        return return_data_dict

    def import_shopify_order_returns_ept(self, instance, from_date, to_date):
        """Import Shopify order returns for date range and process in Odoo."""
        queue_obj = self.env["shopify.order.data.queue.ept"]
        from_filter, to_filter = queue_obj.convert_dates_by_timezone(instance, from_date, to_date)

        helper = shopify_graphql.OrderQueryHelper(
            instance.get_graphql_client(),
            order_visible_currency=instance.order_visible_currency
        )

        payloads = helper.get_order_returns_by_date(
            updated_at_min=from_filter,
            updated_at_max=to_filter,
            allowed_statuses=["RETURNED", "PARTIALLY_RETURNED"],
        )

        main_summary = {
            "total": len(payloads),
            "processed": 0,
            "failed": 0,
            "skipped": 0,
            "created_pickings": 0,
            "no_data": not bool(payloads),
            "processed_so_ref": [],
            "skipped_so_ref": [],
            "failed_so_ref": []
        }
        if not payloads:            
            return main_summary

        processed_return_ids = set()
        for payload in payloads:                     
            summary = self._process_shopify_order_return_payload_ept(instance, payload, processed_return_ids)  
            main_summary["created_pickings"] += summary.get("created_pickings", 0)
            main_summary["processed_so_ref"].extend(summary.get("processed_so_ref", []))
            main_summary["skipped_so_ref"].extend(summary.get("skipped_so_ref", []))
            main_summary["failed_so_ref"].extend(summary.get("failed_so_ref", []))


        processed_refs = ", ".join(filter(None, list(set(main_summary.get("processed_so_ref", []))))) or ""
        skipped_refs = ", ".join(filter(None, list(set(main_summary.get("skipped_so_ref", []))))) or ""
        failed_refs = ", ".join(filter(None, list(set(main_summary.get("failed_so_ref", []))))) or ""

        result_message = _(
                f"Shopify Return Import Completed.\n\n"
                f"Instance: {instance.name}\n"
                f"Execution Time: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}\n\n"
                f"Created Return Pickings: {main_summary.get('created_pickings', 0)}\n"
                f"Processed Orders: {processed_refs}\n"
                f"Skipped Orders: {skipped_refs}\n"
                f"Failed Orders: {failed_refs}")

        self._log_shopify_return_issue_ept(
                instance,
                result_message,
                return_id="N/A",
                so_ref=False,
            )
        instance.write({"last_return_sync_date": to_date})  # Update last sync date to avoid duplicate imports in next run
        return True

    def update_shopify_return_id_on_picking_ept(self, picking, refund_data):
        """
        This method updates the Shopify return ID on the picking record. 
        It takes the existing return IDs from the picking, merges them with the new return IDs from the refund data, 
        and updates the picking's shopify_return_id field with the combined list of unique return IDs.
        """
        return_ids = []

        for refund_line in refund_data:
            if refund_line.get("return"):
                return_id = refund_line.get("return").get("id")
                return_ids.append(return_id)

        existing_ids = [
            rid.strip() for rid in (picking.shopify_return_id or "").split(",") if rid and rid.strip()
        ]

        merged_ids = list(set(existing_ids + return_ids))

        return_id = str(merged_ids[0]) if len(merged_ids) == 1 else ",".join(merged_ids)
        picking.write({'shopify_return_id': return_id})
        return True

    def _process_shopify_order_return_payload_ept(self, instance, payload, processed_return_ids):
        """
        This method processes a single Shopify order return payload. It attempts to find the corresponding sale order in Odoo, checks the order's state and financial status, and creates return pickings if applicable.
        The method updates the summary dictionary with the results of processing the return.
        """
        summary = {
            "total": len(payload),
            "processed": 0,
            "failed": 0,
            "skipped": 0,
            "created_pickings": 0,
            "no_data": not bool(payload),
            "processed_so_ref": [],
            "skipped_so_ref": [],
            "failed_so_ref": []
        }
        result = ""
        refund_data = payload.get("refund") or {}
        return_data = payload.get("return") or {}
        return_id = str(return_data.get("id") or "").strip()
        order_gid = payload.get("id")
        order_ref = payload.get("name")
        shopify_financial_status = payload.get("financial_status")
        # Find SO
        sale_order = self.search_existing_shopify_order(payload, instance, payload.get("order_number"))
        if not sale_order:
            self._log_shopify_return_issue_ept(
                instance,
                "Unable to locate Sale Order in Odoo for Shopify return import. Shopify Order: %s" % (order_ref or order_gid or "N/A"),
                return_id="N/A",
                so_ref=order_ref,
            )            
            summary["failed_so_ref"].append(order_ref or "")
            return summary

        if sale_order.state in ["cancel", "draft"]:
            self._log_shopify_return_issue_ept(
                instance,
                "Sale Order is in %s state; cannot process return." % sale_order.state,
                return_id="N/A",
                so_ref=sale_order.name,
            )           
            summary["skipped_so_ref"].append(sale_order.name or "") 
            return summary

        # If order is not fulfilled yet in Shopify then skip the return import as picking can be created only for fulfilled order
        if (shopify_financial_status in ["refunded", "partially_refunded"] or payload.get("return_status") == "RETURNED") and payload.get(
                    "refunds"):
            created_by = self.env.user.name                        
            # if payload.get("return_status") == "RETURNED" or payload['financial_status'] in ('refunded', 'partially_refunded'):   
            refund_data1 = payload.get("refunds")       

            sale_order._store_shopify_return_payload_on_order_ept(refund_data1, return_id)  
            for refund_data in payload.get("refunds"):
                is_create_return=False
                return_data = refund_data.get("return") or {}
                for refund_line in refund_data.get("refund_line_items"):
                    if refund_line.get("restock_type").lower() == "return":
                        is_create_return = True
                        break

                if not return_data and not is_create_return:
                    continue

                return_id = str(return_data.get("id") or "").strip()

                if not return_id and not is_create_return:
                    return_id = f"NO_RETURN_ID_{order_ref}"

                    self._log_shopify_return_issue_ept(
                        instance,
                        "Return data not found, but restock_type is 'return'. Proceeding without return_id.",
                        return_id=return_id,
                        so_ref=order_ref,
                    )
                    summary["failed_so_ref"].append(sale_order.name or "")

                if return_id in processed_return_ids:
                    # result = "skipped"
                    summary["skipped_so_ref"].append(sale_order.name or "")
                    continue
                processed_return_ids.add(return_id)

                existing_return_picking = self.env["stock.picking"].search([
                    ("sale_id", "=", sale_order.id),
                    ("shopify_instance_id", "=", instance.id),
                    ("picking_type_code", "=", "incoming"),
                    ("state", "!=", "cancel"),
                    ("shopify_return_id", "!=", False),
                ]).filtered(
                    lambda picking: return_id in [
                        rid.strip() for rid in (picking.shopify_return_id or "").split(",") if rid.strip()
                    ]
                )[:1]

                if existing_return_picking:
                    self._log_shopify_return_issue_ept(
                        instance,
                        "Return picking already exists for this Shopify return.",
                        return_id=return_id,
                        so_ref=sale_order.name,
                    )
                    # result = "skipped"
                    summary["skipped_so_ref"].append(sale_order.name or "")
                    continue
                message = sale_order._create_manual_return_picking_ept([refund_data], is_manual_process=True)
                if message:
                    self._log_shopify_return_issue_ept(
                        instance,
                        message,
                        return_id=return_id,
                        so_ref=order_ref,
                    )
                    # result = "failed"
                    summary["failed_so_ref"].append(sale_order.name or "")
                    continue              
                # result = "created"
                summary["created_pickings"] += 1
                summary["processed_so_ref"].append(sale_order.name or "")


            if instance.is_create_refund:
                message= self.create_shipped_order_refund(shopify_financial_status, payload, sale_order,
                                                                created_by)
                if message:
                    self._log_shopify_return_issue_ept(
                        instance,
                        message,
                        return_id=return_id,
                        so_ref=order_ref,
                    )                
                    summary["failed_so_ref"].append(sale_order.name or "") 

        return summary
    
    def _create_manual_return_picking_ept(self, refunds_data, is_manual_process=False):
        """
        This method is used to create return picking for the manually process of refunded order in shopify.

        """
        product_product_obj = self.env["product.product"]
        message = ""
        refund_line_items = self.prepare_refund_data(refunds_data)
        orig_move_ids = self.picking_ids.move_ids.move_orig_ids if self.picking_ids.move_ids.move_orig_ids else self.picking_ids.move_ids
        orig_done_picking_ids = orig_move_ids.picking_id.filtered(lambda picking: picking.state == "done")
        mrp_module = product_product_obj.search_installed_module_ept('mrp')
        if not orig_done_picking_ids and mrp_module:
            orig_done_picking_ids = orig_move_ids.move_dest_ids.picking_id.filtered(
                lambda picking: picking.state == "done")
        is_return = list(filter(lambda x: x.get('restock_type').lower() == 'return', refunds_data[0].get('refund_line_items')))
        if not orig_done_picking_ids and is_return:
            message = "Done picking is not available, so return can't be generated."
        need_to_remove_lines = []
        for picking_id in orig_done_picking_ids:
            return_picking_ids = self.picking_ids.filtered(lambda x: "Return of" in x.origin)
            return_wiz = self.env['stock.return.picking'].with_context(
                active_ids=picking_id.ids,
                active_id=picking_id.ids[0],
                active_model='stock.picking'
            ).sudo().create({})
            for return_move_line in return_wiz.product_return_moves:
                refund_line = next(
                    (item for item in refund_line_items if item["product_id"] == return_move_line.product_id.id),
                    None)
                # if refund_line and return_move_line.product_id.id == refund_line["product_id"]:
                if refund_line:
                    qty_to_return = refund_line["quantity"]
                    existing_return_qty = return_picking_ids.move_ids.filtered(
                        lambda x: x.product_id.id == refund_line["product_id"]).mapped('product_uom_qty')
                    return_qty = sum(existing_return_qty)

                    if is_manual_process:
                        total_move_qty = sum(return_wiz.product_return_moves.filtered(lambda x: x.product_id.id == refund_line["product_id"]).mapped('move_quantity'))
                        pending_return_qty = total_move_qty - return_qty
                        if pending_return_qty > 0:
                            return_qty = 0


                    if qty_to_return > return_qty:
                        return_move_line.write({
                            'quantity': qty_to_return - return_qty,
                            'to_refund': True
                        })
                    else:
                        need_to_remove_lines.append(return_move_line)
                else:
                    need_to_remove_lines.append(return_move_line)

            for need_to_remove_line in need_to_remove_lines:
                need_to_remove_line.unlink()
                
            if is_manual_process and not return_wiz.product_return_moves:
                message = "No return picking is generated for the manually process of refunded order as there is no refundable quantity for the products in the order."
            
            if return_wiz.product_return_moves:
                res = return_wiz.action_create_returns()
                return_picking = self.env['stock.picking'].browse(res['res_id'])
                body_msg = _("Return Picking is Generated for the manually process of refunded order.")
                return_picking.message_post(body=body_msg)
                if return_picking:
                    for move in return_picking.move_ids:
                        refund_line = next(
                            (item for item in refund_line_items if item["product_id"] == move.product_id.id), None)
                        if refund_line:
                            move.write({'shopify_return_reason': refund_line.get("return_reason", "")})
                    if is_manual_process:
                        return_picking.button_validate()
                        return_picking.message_post(body=_("Return Picking is Validate by Manual Process."))
                        self.update_shopify_return_id_on_picking_ept(return_picking, refunds_data)
                        
        return message 

    def _store_shopify_return_payload_on_order_ept(self, payload, return_id):        
        self.shopify_return_payload_json = json.dumps(payload, default=str)
        return True    

    def _log_shopify_return_issue_ept(self, instance, message, return_id=False, so_ref=False):
        complete_message = (
            f" Instance: {instance.name}  \n timestamp :{fields.Datetime.now()}"
            f"\n {message} \n" f"Sale Order Ref: {so_ref}"            
        )
        if not so_ref:  
            complete_message=f"{message}"            

        # _logger.error(complete_message)
        self.env["common.log.lines.ept"].create_common_log_line_ept(
            shopify_instance_id=instance.id,
            module="shopify_ept",
            message=complete_message,
            model_name='sale.order',
            order_ref=so_ref or False,
        )

    def _raise_shopify_graphql_errors(self, client, result, operation_name):
        """
        This method is used to raise User error if found any.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        error = client._raise_shopify_graphql_errors(result, operation_name=operation_name)
        if error:
            raise UserError(self.env._(error))

    def action_export_shopify_order_graphql(self):
        """
        Export order to Shopify using GraphQL API.
        Dispatches to draft-order or create-order flow based on context.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        for order in self:
            instance = order.shopify_instance_id
            if not instance:
                raise UserError(_("Shopify Instance is required to export the order."))
            if not instance.use_graphql_api:
                raise UserError(_("GraphQL API is not enabled for this Shopify Instance."))
            if order.shopify_order_id:
                raise UserError(_("This order is already exported to Shopify."))

            try:
                if instance.shopify_order_export_tax_policy == 'shopify_tax':
                    # To use shopify tax, draft order is created and completed.
                    order.export_shopify_order_graphql_draft_order()
                else:
                    # To use odoo tax amounts, direct order is created on shopify.
                    order.export_shopify_order_graphql_create_order()
                self.env.cr.commit()
            except UserError as error:
                common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                               message=str(error), model_name="sale.order",
                                                               order_ref=order.name)
                self.env.cr.commit()
                if self.env.context.get('cron_process', False):
                    continue
                raise
        # try:
        #     # filtered(lambda o: o.shopify_instance_id.shopify_order_export_tax_policy != 'shopify_tax')
        #     instance_ids= self.mapped('shopify_instance_id')
        #     for instance in instance_ids.filtered(lambda i: i.use_graphql_api and i.enable_metafield_sync):
        #         sale_orders = self.filtered(lambda o: o.shopify_instance_id == instance)
        #         self.env["shopify.export.metafields.queue.ept"].prepare_export_order_metafield_queue_line(sale_orders, instance)
        # except UserError as error:
        #         common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
        #                                                        message=str(error), model_name="sale.order",
        #                                                        order_ref=order.name)
        #         self.env.cr.commit()


    def export_shopify_order_graphql_create_order(self):
        """
        This method is used to export order to Shopify using GraphQL API.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        _logger.info(f"Exporting sale order to shopify: {self.name}")
        instance = self.shopify_instance_id
        instance.connect_in_shopify()

        # Prepare order data
        order_lines = self.order_line.filtered(lambda l: not l.display_type)
        if not order_lines:
            message = ("System tried to export the order but no order lines were found.\n"
                       "Action Items:\n"
                       "- Add at least one product line to the order and try again.")
            raise UserError(message)

        customer_gid = self._get_shopify_customer_gid(instance)
        if not customer_gid:
            message = ("System tried to export the order but no Shopify customer mapping was found.\n"
                       "Action Items:\n"
                       "- Create or import the Shopify customer in Odoo.\n"
                       "- Ensure the customer mapping exists for this instance.")
            raise UserError(message)

        shopify_currency = instance.shopify_pricelist_id.currency_id
        order_currency = self.currency_id
        line_items, shipping_lines, discount_total, missing_products = \
            self._prepare_shopify_order_lines_graphql_create_order(
            instance, order_lines, shopify_currency, order_currency
        )
        if missing_products:
            missing_products = sorted(set(missing_products))
            message = ("System tried to export the order but some products are not exported to Shopify.\n"
                       "Products: %s\nAction Items:\n"
                       "- Export these products to Shopify and try again.") % ", ".join(missing_products)
            raise UserError(message)
        if not line_items:
            message = ("System tried to export the order but no Shopify-mapped products were found.\n"
                       "Action Items:\n"
                       "- Ensure each order line product is exported to Shopify.")
            raise UserError(message)

        order_input = {
            "customerId": customer_gid,
            "phone": self.partner_id.phone,
            "lineItems": line_items,
            "currency": shopify_currency.name,
            "presentmentCurrency": order_currency.name,
        }
        shipping_address = self.partner_shipping_id._prepare_shopify_address_graphql()
        billing_address = self.partner_invoice_id._prepare_shopify_address_graphql()
        should_mark_paid = self._is_fully_invoiced_and_paid_for_shopify()
        if shipping_address:
            order_input["shippingAddress"] = shipping_address
        if billing_address:
            order_input["billingAddress"] = billing_address
        if shipping_lines:
            order_input["shippingLines"] = shipping_lines
        order_input["financialStatus"] = "PAID" if should_mark_paid else "PENDING"
        if discount_total > 0:
            order_input["discountCode"] = {
                "itemFixedDiscountCode": {
                    "amountSet": self._get_priceset_dict(discount_total, order_currency, shopify_currency),
                    "code": "Odoo Discount"
                }
            }

        if instance.enable_metafield_sync and instance.use_graphql_api:
            metafield_paylod,message = self.env["shopify.export.metafields.queue.ept"].prepared_export_metafield_payload(self,
                                                                                                                 instance,
                                                                                                                 "sale.order",
                                                                                                                 "ORDER")
            if metafield_paylod and not message:
                order_input['metafields'] = metafield_paylod

        try:
            # Export order to Shopify
            _logger.info(f"Order Input for Shopify GraphQL API: {order_input}")
            client = instance.get_graphql_client()
            helper = OrderExportQueryHelper(client)
            result = helper.order_create(order_input)
        except Exception as error:
            message = ("System tried to export the order but an unexpected error occurred.\n"
                       "Error: %s") % error
            raise UserError(message)

        # Check for errors in the response
        if result.get("errors"):
            error_message = "\n".join([str(error['message']) for error in result.get("errors", [])
                                       if error.get("message")])
            message = ("System tried to export the order but Shopify returned errors.\n"
                       f"Error: {error_message}")
            raise UserError(message)

        order_create = result.get("data", {}).get("orderCreate", {})
        user_errors = order_create.get("userErrors") or []
        if user_errors:
            message = ("System tried to export the order but Shopify returned user errors.\n"
                       "Error: %s") % user_errors
            raise UserError(message)

        order_data = order_create.get("order") or {}
        shopify_gid = order_data.get("id")
        if not shopify_gid:
            message = ("System tried to export the order but Shopify did not return an order ID.\n"
                       "Response: %s") % order_create
            raise UserError(message)

        # Save shopify data in odoo order
        self._set_shopify_order_details(client, shopify_gid, order_data, instance)
        return True

    def export_shopify_order_graphql_draft_order(self):
        """
        Export order to Shopify using draftOrderCreate + draftOrderComplete GraphQL flow.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        _logger.info(f"Exporting sale order to Shopify via draft flow: {self.name}")
        instance = self.shopify_instance_id
        instance.connect_in_shopify()

        # Prepare order data
        order_lines = self.order_line.filtered(lambda l: not l.display_type)
        if not order_lines:
            message = ("System tried to export the order but no order lines were found.\n"
                       "Action Items:\n"
                       "- Add at least one product line to the order and try again.")
            raise UserError(message)

        customer_gid = self._get_shopify_customer_gid(instance)
        if not customer_gid:
            message = ("System tried to export the order but no Shopify customer mapping was found.\n"
                       "Action Items:\n"
                       "- Create or import the Shopify customer in Odoo.\n"
                       "- Ensure the customer mapping exists for this instance.")
            raise UserError(message)

        shopify_currency = instance.shopify_pricelist_id.currency_id
        order_currency = self.currency_id
        line_items, shipping_line, total_disc, missing_prods = self._prepare_shopify_order_lines_graphql_draft_order(
            instance, order_lines, shopify_currency, order_currency
        )
        if missing_prods:
            message = ("System tried to export the order but some products are not exported to Shopify.\n"
                       "Products: %s\nAction Items:\n"
                       "- Export these products to Shopify and try again.") % ", ".join(sorted(set(missing_prods)))
            raise UserError(message)
        if not line_items:
            message = ("System tried to export the order but no Shopify-mapped products were found.\n"
                       "Action Items:\n"
                       "- Ensure each order line product is exported to Shopify.")
            raise UserError(message)

        draft_order_input = self._prepare_shopify_draft_order_input_graphql(
            customer_gid, line_items, shipping_line, total_disc, shopify_currency, order_currency, instance
        )

        try:
            # Create draft order on shopify
            _logger.info(f"Draft Order Input for Shopify GraphQL API: {draft_order_input}")
            client = instance.get_graphql_client()
            helper = OrderExportQueryHelper(client)
            create_result = helper.draft_order_create(draft_order_input)
            self._raise_shopify_graphql_errors(helper.client, create_result, operation_name="draftOrderCreate")

            draft_create_data = create_result.get("data", {}).get("draftOrderCreate", {}) or {}
            draft_order_id = (draft_create_data.get("draftOrder") or {}).get("id")
            if not draft_order_id:
                raise UserError(_("Shopify did not return a draft order id."))

            # Complete draft order on shopify
            complete_result = helper.draft_order_complete(draft_order_id)
            self._raise_shopify_graphql_errors(helper.client, complete_result, operation_name="draftOrderComplete")

            complete_data = complete_result.get("data", {}).get("draftOrderComplete", {}) or {}
            order_data = complete_data.get('draftOrder', {}).get('order', {}) or {}
            shopify_gid = order_data.get("id")
            if not shopify_gid:
                raise UserError(_("Shopify did not return order id after draft completion."))

            # update metafields after complete darft order in shopify
            if instance.enable_metafield_sync and instance.use_graphql_api:
                metafield_paylod, message = self.env[
                    "shopify.export.metafields.queue.ept"].prepared_export_metafield_payload(
                    self,
                    instance,
                    "sale.order",
                    "ORDER")
                if message:
                    raise UserError(message)
                for payload in metafield_paylod:
                    payload.update({"ownerId":shopify_gid})

                metafield_helper = MetafieldQueryHelper(client)
                metafield_result = metafield_helper.set_metafields(metafield_paylod)
                # req_error = metafield_result.get('data') and metafield_result.get('data').get(
                #     'metafieldsSet') and metafield_result.get('data').get('metafieldsSet').get('userErrors')
                self._raise_shopify_graphql_errors(helper.client, metafield_result, operation_name="Update Order Metafields")
                # if metafield_paylod and not message:
                #     update_input={"id": shopify_gid}
                #     update_input['metafields'] = metafield_paylod
                #     update_result = helper.order_update(update_input)
                #     self._raise_shopify_graphql_errors(helper.client, update_result, operation_name="orderUpdate")

            # Get line items if not returned in order data after draft completion
            order_helper = OrderQueryHelper(client)
            if not order_data.get("lineItems", {}).get("nodes"):
                order_id = client._extract_id_from_gid(shopify_gid)
                if order_id:
                    rest_orders = order_helper.get_order([str(order_id)], specific_fields_key="line_items")
                    if rest_orders:
                        order_data["lineItems"] = self._convert_rest_line_items_to_graphql_nodes(rest_orders[0])

            # Save shopify data in odoo order
            self._set_shopify_order_details(client, shopify_gid, order_data, instance)
            # fulfill order if deliveries are validated for odoo order
            self._sync_shopify_fulfillment_after_export(instance)
            return True
        except UserError:
            raise
        except Exception as error:
            message = ("System tried to export the order but an unexpected error occurred.\n"
                       "Error: %s") % error
            raise UserError(message)

    def _prepare_shopify_draft_order_input_graphql(self, customer_gid: str, line_items: list, shipping_line: object,
                                                   discount_total: float, shopify_currency: object,
                                                   order_currency: object, instance: object):
        """
        This method is used to prepare draft order input data.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        # Check if order is paid in odoo
        should_mark_paid = self._is_fully_invoiced_and_paid_for_shopify()
        
        # Prepare input data for draft order
        payment_terms_template_id = self._get_shopify_payment_terms_template_gid(instance)
        input_data = {
            "customerId": customer_gid,
            "lineItems": line_items,
            "tags": [tag.strip() for tag in self.tag_ids.mapped("name") if tag and tag.strip()],
            "email": self.partner_id.email or "",
            "phone": self.partner_id.phone or "",
            "note": html2plaintext(self.note or "").strip(),
        }
        shipping_address = self.partner_shipping_id._prepare_shopify_address_graphql()
        billing_address = self.partner_invoice_id._prepare_shopify_address_graphql()
        if shipping_address:
            input_data["shippingAddress"] = shipping_address
        if billing_address:
            input_data["billingAddress"] = billing_address
        if shipping_line:
            input_data["shippingLine"] = shipping_line
        if discount_total > 0:
            input_data["appliedDiscount"] = {
                "title": "Odoo Discount",
                "description": "Order discount from Odoo",
                "value": self._get_shop_money_amount(discount_total, order_currency, shopify_currency),
                "valueType": "FIXED_AMOUNT",
            }
        if not should_mark_paid and payment_terms_template_id:
            input_data["paymentTerms"] = {
                "paymentTermsTemplateId": payment_terms_template_id.shopify_payment_term_gid,
            }
            due_date = self._get_due_date_from_payment_term()
            due_date_utc = self._get_due_at_utc_from_due_date(due_date)
            if payment_terms_template_id.payment_term_type == 'FIXED':
                input_data["paymentTerms"]["paymentSchedules"] = {"dueAt": due_date_utc}
            if payment_terms_template_id.payment_term_type == 'NET':
                input_data["paymentTerms"]["paymentSchedules"] = {"issuedAt": due_date_utc}
        return input_data

    def _prepare_shopify_order_lines_graphql_draft_order(self, instance: object, order_lines: list,
                                                         shopify_currency: object, order_currency: object):
        """
        Prepare product lines, one consolidated shipping line and order-level discount for draft order export.
        Shopify tax mode: only unit price (untaxed) and quantity are exported for products.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        line_items = []
        shipping_amount = 0.0
        discount_total = 0.0
        missing_products = []
        discount_products = self._get_discount_products(instance)
        shopify_product_obj = self.env["shopify.product.product.ept"]

        product_candidate_lines = order_lines.filtered(
            lambda l: not l.display_type and not l.is_delivery and
            not (discount_products and l.product_id in discount_products) and
            not (getattr(l, "is_reward_line", False) and getattr(getattr(l, "reward_id", False), "reward_type", False) != "product")
        )
        odoo_shopify_product_map = self._get_odoo_shopify_product_mapping(instance, product_candidate_lines)

        for line in order_lines:
            amounts = self._get_line_amounts(line)
            # Check for shipping lines
            if line.is_delivery:
                shipping_amount += amounts.get("total_included", 0.0)
                continue

            # Check for discount and reward/coupon discount lines
            if discount_products and line.product_id in discount_products:
                discount_total += abs(amounts.get("total_included", 0.0))
                continue
            if getattr(line, "is_reward_line", False):
                reward = getattr(line, "reward_id", False)
                if reward and getattr(reward, "reward_type", False) != "product":
                    discount_total += abs(amounts.get("total_included", 0.0))
                    continue

            shopify_product = odoo_shopify_product_map.get(line, shopify_product_obj)
            if not shopify_product or not shopify_product.variant_id:
                missing_products.append(line.product_id.display_name)
                continue

            # Prepare line item data
            line_items.append({
                "variantId": f"{SHOPIFY_GID_PRODUCT_VARIANT_PREFIX}{shopify_product.variant_id}",
                "quantity": int(line.product_uom_qty),
                "priceOverride": {
                    "amount": self._get_shop_money_amount(line.price_reduce_taxexcl, order_currency, shopify_currency),
                    "currencyCode": shopify_currency.name,
                },
            })

        # Prepare shipping line data
        shipping_line = False
        if shipping_amount:
            shipping_line = {
                "title": "Shipping",
                "priceWithCurrency": {
                    "amount": self._get_shop_money_amount(shipping_amount, order_currency, shopify_currency),
                    "currencyCode": shopify_currency.name,
                }
            }
        return line_items, shipping_line, discount_total, missing_products

    def _is_fully_invoiced_and_paid_for_shopify(self):
        """
        This method is used to check if sale order is fully invoiced and paid.
        Only fully invoiced and paid orders are marked as paid in Shopify.
        :return: boolean

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        posted_invoices = self.invoice_ids.filtered(lambda inv: inv.move_type == "out_invoice" and inv.state == "posted")
        if self.invoice_status != "invoiced" or not posted_invoices:
            return False
        return all(inv.payment_state in ("paid", "in_payment") for inv in posted_invoices)

    def _get_shopify_payment_terms_template_gid(self, instance: object):
        """
        This method is used to get the Shopify payment terms template gid based on the payment term of the order.
        :param instance: Shopify instance record
        :return: payment term mapping

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        payment_term_map_obj = self.env["shopify.payment.terms.ept"]
        payment_term_map = payment_term_map_obj.search([
            ("instance_id", "=", instance.id), ("odoo_payment_term_id", "=", self.payment_term_id.id),
            ("odoo_payment_term_id", "!=", False),
        ], limit=1)
        if not payment_term_map:
            payment_term_map = payment_term_map_obj.search([
                ("instance_id", "=", instance.id),
                ("default", "=", True),
            ], limit=1)
        return payment_term_map

    def _try_mark_shopify_order_paid_graphql(self, helper: object, shopify_gid: str, instance: object,
                                             common_log_line_obj: object):
        """
        This method is used to mark the Shopify order as paid.
        :param helper: GraphQL helper object
        :param shopify_gid: Shopify order gid
        :param instance: Shopify instance record
        :param common_log_line_obj: Common log line object
        :return: None

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mark_result = helper.order_mark_as_paid(shopify_gid)
        if mark_result.get("errors"):
            error_message = "\n".join([str(error.get("message")) for error in mark_result.get("errors", [])
                                       if error.get("message")])
            message = ("Shopify order was exported but marking order as paid failed.\n"
                       "Error: %s") % error_message
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                           message=message, model_name="sale.order",
                                                           order_ref=self.name)
            return
        user_errors = (mark_result.get("data", {}).get("orderMarkAsPaid", {}) or {}).get("userErrors", [])
        if user_errors:
            error_message = "\n".join([str(error.get("message")) for error in user_errors if error.get("message")])
            message = ("Shopify order was exported but marking order as paid returned user errors.\n"
                       "Error: %s") % error_message
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                           message=message, model_name="sale.order",
                                                           order_ref=self.name)

    def _convert_rest_line_items_to_graphql_nodes(self, rest_order_data: dict):
        """
        This method is used to convert REST API line items to GraphQL nodes format.
        :param rest_order_data: order data
        :return: dict

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        nodes = []
        for line in rest_order_data.get("line_items", []):
            line_id = line.get("id")
            variant_id = line.get("variant_id")
            if not line_id:
                continue
            node = {
                "id": f"{SHOPIFY_GID_LINE_ITEM_PREFIX}{line_id}",
                "quantity": int(line.get("quantity") or 0),
                "title": line.get("title"),
            }
            if variant_id:
                node["variant"] = {"id": f"{SHOPIFY_GID_PRODUCT_VARIANT_PREFIX}{variant_id}"}
            nodes.append(node)
        return {"nodes": nodes}

    def _sync_shopify_fulfillment_after_export(self, instance: object):
        """
        This method is used to sync fulfillment status in Shopify after order export
        if there are done pickings with customer location.
        :param instance: Shopify instance record

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        done_pickings = self.picking_ids.filtered(
            lambda p: p.state == "done" and p.location_dest_id.usage == "customer" and not p.updated_in_shopify
        )
        if done_pickings:
            self.update_order_status_in_shopify(instance, picking_ids=done_pickings)

    def action_update_shopify_order_graphql(self):
        """
        Update existing Shopify order from Odoo sale order.
        Includes note/tags/customer details and line add/qty updates.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        instance = self.shopify_instance_id
        common_log_line_obj = self.env["common.log.lines.ept"]
        if not instance:
            raise UserError(_("Shopify Instance is required to update the order."))
        if not instance.use_graphql_api:
            raise UserError(_("GraphQL API is not enabled for this Shopify Instance."))
        if not self.shopify_order_id:
            raise UserError(_("Shopify order reference is required to update the order."))

        # Prepare data necessary data for update process
        discount_products = self._get_discount_products(instance)
        order_lines = self.order_line.filtered(lambda l: not l.display_type)
        invalid_service_lines = order_lines.filtered(
            lambda l: l.product_id.type == "service" and not l.is_delivery and l.product_id not in discount_products
            and not (getattr(l, "is_reward_line", False) and getattr(getattr(l, "reward_id", False), "reward_type", False) != "product")
        )
        if invalid_service_lines:
            names = ", ".join(invalid_service_lines.mapped("name"))
            raise UserError(
                _("Only shipping/discount service lines are allowed for Shopify update. Invalid lines: %s") % names
            )

        product_lines = order_lines.filtered(
            lambda l: not l.is_delivery and l.product_id.type != "service" and l.product_id not in discount_products
            and not (getattr(l, "is_reward_line", False) and getattr(getattr(l, "reward_id", False), "reward_type", False) != "product")
        )
        odoo_shopify_product_map = self._get_odoo_shopify_product_mapping(instance, product_lines)
        missing_products = []
        shopify_product_obj = self.env["shopify.product.product.ept"]
        for line in product_lines:
            product = odoo_shopify_product_map.get(line, shopify_product_obj)
            if not product or not product.variant_id:
                missing_products.append(line.product_id.display_name)
        if missing_products:
            message = ("System tried to update Shopify order but some products are not exported to Shopify.\n"
                       "Products: %s") % ", ".join(sorted(set(missing_products)))
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                           message=message, model_name="sale.order",
                                                           order_ref=self.name)
            raise UserError(message)

        try:
            # get current order data from shopify to start edit session
            client = instance.get_graphql_client()
            helper = OrderExportQueryHelper(client)
            order_gid = f"{SHOPIFY_GID_ORDER_PREFIX}{self.shopify_order_id}"
            fetch_result = helper.get_order_for_update(order_gid)
            self._raise_shopify_graphql_errors(helper.client, fetch_result, operation_name="get_order_for_update")
            shopify_order = fetch_result.get("data", {}).get("order") or {}
            if not shopify_order:
                raise UserError(_("Shopify order not found for update."))

            shopify_lines_by_id = {
                str(client._extract_id_from_gid(node.get("id"))): node
                for node in shopify_order.get("lineItems", {}).get("nodes", [])
                if node.get("id")
            }

            # start edit session and get calculated order with line ids for mapping
            begin_result = helper.order_edit_begin(order_gid)
            self._raise_shopify_graphql_errors(helper.client, begin_result, operation_name="orderEditBegin")
            begin_data = begin_result.get("data", {}).get("orderEditBegin", {})
            calculated_order = begin_data.get("calculatedOrder", {}) or {}
            calculated_order_id = calculated_order.get("id")
            if not calculated_order_id:
                raise UserError(_("Could not start Shopify order edit session."))

            # prepare order line data mapping
            calculated_line_ids_by_original = {}
            original_by_variant = {}
            for node in shopify_order.get("lineItems", {}).get("nodes", []):
                variant_gid = (node.get("variant") or {}).get("id")
                original_line_gid = node.get("id")
                if variant_gid and original_line_gid:
                    original_by_variant.setdefault(variant_gid, []).append(original_line_gid)
            calculated_by_variant = {}
            for node in calculated_order.get("lineItems", {}).get("nodes", []):
                variant_gid = (node.get("variant") or {}).get("id")
                calculated_line_gid = node.get("id")
                if variant_gid and calculated_line_gid:
                    calculated_by_variant.setdefault(variant_gid, []).append(calculated_line_gid)
            for variant_gid, original_line_gids in original_by_variant.items():
                calculated_line_gids = calculated_by_variant.get(variant_gid, [])
                for index, original_line_gid in enumerate(original_line_gids):
                    if index < len(calculated_line_gids):
                        original_line_id = str(client._extract_id_from_gid(original_line_gid))
                        calculated_line_ids_by_original[original_line_id] = calculated_line_gids[index]

            # update sale order line qty or new line changes to shopify
            line_changes_made = False
            for line in product_lines:
                shopify_product = odoo_shopify_product_map.get(line, shopify_product_obj)
                variant_gid = f"{SHOPIFY_GID_PRODUCT_VARIANT_PREFIX}{shopify_product.variant_id}"
                desired_qty = int(line.product_uom_qty)

                # new line on Shopify
                if not line.shopify_line_id:
                    add_result = helper.order_edit_add_variant(calculated_order_id, variant_gid, desired_qty)
                    self._raise_shopify_graphql_errors(helper.client, add_result, operation_name="orderEditAddVariant")
                    line_changes_made = True
                    continue

                shopify_line_id = str(line.shopify_line_id)
                shopify_line = shopify_lines_by_id.get(shopify_line_id)
                if not shopify_line:
                    # fallback add variant if mapping is stale
                    add_result = helper.order_edit_add_variant(calculated_order_id, variant_gid, desired_qty)
                    self._raise_shopify_graphql_errors(helper.client, add_result, operation_name="orderEditAddVariant")
                    line_changes_made = True
                    continue

                # update quantity
                if int(shopify_line.get("quantity") or 0) != desired_qty:
                    calculated_line_id = calculated_line_ids_by_original.get(shopify_line_id)
                    if not calculated_line_id:
                        raise UserError(
                            _("Could not map Shopify line item for quantity update in edit session.\n"
                              "Line: %s") % (line.name or line.product_id.display_name)
                        )
                    if not line._eligible_to_update_qty():
                        raise UserError(_("Quantity can not be updated for line with product %s as it has "
                                          "fulfilled quantity in Shopify." % line.product_id.display_name))
                    qty_result = helper.order_edit_set_quantity(calculated_order_id, calculated_line_id, desired_qty)
                    self._raise_shopify_graphql_errors(helper.client, qty_result, operation_name="orderEditSetQuantity")
                    line_changes_made = True

            # commit order in shopify to apply line changes
            committed_order = {}
            if line_changes_made:
                commit_result = helper.order_edit_commit(calculated_order_id=calculated_order_id)
                self._raise_shopify_graphql_errors(helper.client, commit_result, operation_name="orderEditCommit")
                committed_order = commit_result.get("data", {}).get("orderEditCommit", {}).get("order") or {}

            # Update note/tags/customer details after line edits
            update_input = {"id": order_gid}
            update_input["note"] = html2plaintext(self.note or "").strip() if self.note is not None else ""
            order_tags = [tag.strip() for tag in self.tag_ids.mapped("name") if tag and tag.strip()]
            update_input["tags"] = order_tags
            update_input["email"] = self.partner_id.email or ""
            update_input["phone"] = self.partner_id.phone or ""
            shipping_address = self.partner_shipping_id._prepare_shopify_address_graphql()
            if shipping_address:
                update_input["shippingAddress"] = shipping_address
            # if instance.enable_metafield_sync and instance.use_graphql_api:
            #     metafield_paylod , message1= self.env["shopify.export.metafields.queue.ept"].prepared_export_metafield_payload(self, instance,"sale.order","ORDER")
            #     if metafield_paylod and not message1:
            #         update_input['metafields']=metafield_paylod

            update_result = helper.order_update(update_input)
            self._raise_shopify_graphql_errors(helper.client, update_result, operation_name="orderUpdate")

            if instance.enable_metafield_sync and instance.use_graphql_api:
                metafield_paylod , msg= self.env["shopify.export.metafields.queue.ept"].prepared_export_metafield_payload(self, instance,"sale.order","ORDER")

                if msg:
                    raise UserError(msg)
                for payload in metafield_paylod:
                    payload.update({"ownerId":order_gid})

                metafield_helper = MetafieldQueryHelper(client)
                metafield_result = metafield_helper.set_metafields(metafield_paylod)
                # req_error = metafield_result.get('data') and metafield_result.get('data').get(
                #     'metafieldsSet') and metafield_result.get('data').get('metafieldsSet').get('userErrors')
                self._raise_shopify_graphql_errors(helper.client, metafield_result, operation_name="Update Order Metafields")


            # mark shopify order as paid if sale order is fully invoiced ad paid.
            if self._is_fully_invoiced_and_paid_for_shopify():
                self._try_mark_shopify_order_paid_graphql(helper, order_gid, instance, common_log_line_obj)

            # update shipment fulfillment status.
            self._sync_shopify_fulfillment_after_export(instance)

            if committed_order:
                self._set_shopify_order_line_details(committed_order, instance)
            self.message_post(body=_("Shopify order updated from Odoo order."))

            # if instance.enable_metafield_sync and instance.use_graphql_api:
            #     self.env["shopify.export.metafields.queue.ept"].prepare_export_order_metafield_queue_line(self, instance)
        except UserError:
            raise
        except Exception as error:
            message = ("System tried to update Shopify order but an unexpected error occurred.\n"
                       "Error: %s") % error
            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                           message=message, model_name="sale.order",
                                                           order_ref=self.name)
            raise UserError(message)
        return True

    def _get_odoo_shopify_product_mapping(self, instance: object, product_lines: list[object]):
        """
        This method retrieves the mapping of Odoo product IDs to Shopify variant GIDs for the given product lines.
        :param instance: The Shopify instance
        :param product_lines: sale order lines
        :return: shopify product mapping

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        shopify_product_obj = self.env["shopify.product.product.ept"]
        mapping = {}
        for line in product_lines:
            product = shopify_product_obj.search([("product_id", "=", line.product_id.id),
                                                  ("shopify_instance_id", "=", instance.id),
                                                  ("exported_in_shopify", "=", True)], limit=1)
            mapping[line] = product
        return mapping

    def _get_shopify_customer_gid(self, instance: object):
        """
        This method retrieves the Shopify customer GID for the order's partner.
        :param instance: The Shopify instance for which to retrieve the customer GID
        :return: The Shopify customer GID if found, otherwise False

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        partner = self.partner_id
        shopify_partner = self.env["shopify.res.partner.ept"].search(
            [("partner_id", "=", partner.id), ("shopify_instance_id", "=", instance.id)],
            limit=1
        )
        if not shopify_partner or not shopify_partner.shopify_customer_id:
            # sync odoo partner with shopify
            shopify_partner = partner.sync_shopify_customer_mapping_graphql(instance)
        if not shopify_partner or not shopify_partner.shopify_customer_id:
            return False
        return f"{SHOPIFY_GID_CUSTOMER_PREFIX}{shopify_partner.shopify_customer_id}"

    def _prepare_shopify_order_lines_graphql_create_order(self, instance: object, order_lines: list,
                                                          shopify_currency: object, order_currency: object):
        """
        This method prepares the line items and shipping lines for the Shopify order based on the Odoo order lines.
        It also calculates the total discount amount and identifies any products that are missing Shopify mappings.
        :param instance: The Shopify instance for which to prepare the order lines
        :param order_lines: The list of Odoo order lines to process
        :param shopify_currency: The currency of the Shopify store
        :param order_currency: The currency of the Odoo order
        :return: tuple

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        line_items = []
        shipping_lines = []
        discount_total = 0.0
        missing_products = []
        discount_products = self._get_discount_products(instance)
        shopify_product_obj = self.env["shopify.product.product.ept"]

        for line in order_lines:
            amounts = self._get_line_amounts(line)
            # Check for shipping lines
            if line.is_delivery:
                shipping_lines.append({
                    "title": line.name,
                    "priceSet": self._get_priceset_dict(amounts['total_included'], order_currency, shopify_currency),
                })
                continue

            # Check for discount and reward/coupon discount lines
            if discount_products and line.product_id in discount_products:
                discount_total += abs(amounts['total_included'])
                continue
            if getattr(line, "is_reward_line", False):
                reward = getattr(line, "reward_id", False)
                if reward and getattr(reward, "reward_type", False) != "product":
                    discount_total += abs(amounts['total_included'])
                    continue

            # Add to discount total
            if line.discount:
                line_before = amounts['total_excluded']
                line_after = line.price_subtotal
                if line_before > line_after:
                    discount_total += line_before - line_after

            shopify_product = shopify_product_obj.search(
                [("product_id", "=", line.product_id.id),
                 ("shopify_instance_id", "=", instance.id),
                 ("exported_in_shopify", "=", True)],
                limit=1
            )
            if not shopify_product or not shopify_product.variant_id:
                missing_products.append(line.product_id.display_name)
                continue

            # Prepare line item data
            variant_gid = f"{SHOPIFY_GID_PRODUCT_VARIANT_PREFIX}{shopify_product.variant_id}"
            item = {
                "variantId": variant_gid,
                "quantity": int(line.product_uom_qty),
                "priceSet": self._get_priceset_dict(line.price_reduce_taxexcl, order_currency, shopify_currency),
                "taxLines": []
            }

            # Add tax lines if taxes are present on the order line. Shopify needs tax amount and tax rate for each tax line.
            for tax_data in amounts.get("taxes_data", []):
                tax = tax_data.get("tax")
                tax_amount = tax_data.get("tax_amount", 0.0)
                if not tax or not tax_amount:
                    continue
                item["taxLines"].append(
                    {
                        "priceSet": self._get_priceset_dict(tax_amount, order_currency, shopify_currency),
                        "rate": (tax.amount / 100),
                        "title": "Tax",
                    }
                )
            line_items.append(item)

        return line_items, shipping_lines, discount_total, missing_products

    def _get_shop_money_amount(self, presentment_amount: float, presentment_currency: object, shop_currency: object):
        """
        This method converts the presentment amount to shop currency if needed.
        :param presentment_amount: The amount in presentment currency
        :param presentment_currency: The currency code of the presentment amount
        :param shop_currency: The currency code of the shop
        :return: The amount converted to shop currency if conversion is needed, otherwise the original amount
        :return: tuple

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        date_ref = fields.Date.to_date(self.date_order) or fields.Date.context_today(self)
        if presentment_currency != shop_currency:
            converted_amount = presentment_currency._convert(presentment_amount, shop_currency, self.company_id,
                                                             date_ref)
            return round(converted_amount, 2)
        return round(presentment_amount, 2)

    def _get_due_date_from_payment_term(self):
        """
        Get order due date based on the payment term configured on sale order.
        Returns the latest due date when payment term has multiple lines.
        :return: date ref

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        date_ref = fields.Date.to_date(self.date_order) or fields.Date.context_today(self)
        if not self.payment_term_id:
            return date_ref

        sign = 1 if self.amount_total >= 0 else -1
        terms = self.payment_term_id._compute_terms(
            date_ref=date_ref,
            currency=self.currency_id,
            company=self.company_id,
            tax_amount=self.amount_tax,
            tax_amount_currency=self.amount_tax,
            sign=sign,
            untaxed_amount=self.amount_untaxed,
            untaxed_amount_currency=self.amount_untaxed,
        )
        if not isinstance(terms, list):
            terms = [terms]
        due_dates = [term.get("date") for term in terms if term.get("date")]
        return max(due_dates) if due_dates else date_ref

    def _get_due_at_utc_from_due_date(self, due_date: datetime):
        """
        Build Shopify dueAt in UTC datetime format from a due date.
        Uses order time component when available.
        :param due_date: due date object

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not due_date:
            return None
        order_dt = fields.Datetime.to_datetime(self.date_order) if self.date_order else datetime.now(timezone.utc)
        due_dt = datetime.combine(due_date, order_dt.time())
        return due_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _get_priceset_dict(self, presentment_amount: float, presentment_currency: object, shop_currency: object):
        """
        This method converts the presentment amount to shop currency if needed.
        :param presentment_amount: The amount in presentment currency
        :param presentment_currency: The currency code of the presentment amount
        :param shop_currency: The currency code of the shop
        :return: dict

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if presentment_currency != shop_currency:
            converted_amount = presentment_currency._convert(presentment_amount, shop_currency, self.company_id,
                                                             self.date_order)
            shop_money_amount = round(converted_amount, 2)
        else:
            shop_money_amount = round(presentment_amount, 2)
        return {
            "shopMoney": {
                "amount": shop_money_amount,
                "currencyCode": shop_currency.name
            },
            "presentmentMoney": {
                "amount": round(presentment_amount, 2),
                "currencyCode": presentment_currency.name,
            }
        }

    def _get_discount_products(self, instance: object):
        """
        This method is used to prepare discount products.
        :param instance: The Shopify instance for which to prepare the discount products
        :return: list of discount products
        :return: tuple

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        discount_products = self.env['product.product']
        shopify_discount_product_id = instance.discount_product_id
        if shopify_discount_product_id:
            discount_products |= shopify_discount_product_id
        company_discount_product_id = self.company_id.sale_discount_product_id
        if company_discount_product_id:
            discount_products |= company_discount_product_id
        return discount_products

    @staticmethod
    def _get_line_amounts(line: object, qty: float = None, tax: object = None):
        """
        This method calculates the line amounts considering discounts and taxes.
        :param line: The sale order line for which to calculate the amounts
        :param qty: The quantity to consider for the calculation (if None, uses the line quantity)
        :param tax: The specific tax to consider for the calculation (if None, uses all taxes on the line)
        :return: A dictionary containing total_excluded and total_included
        :return: tuple

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        company = line.company_id
        base_line = line._prepare_base_line_for_taxes_computation()
        quantity = qty or base_line['quantity']
        taxes = tax or base_line['tax_ids']
        taxes_computation = taxes._get_tax_details(
            price_unit=base_line['price_unit'],
            quantity=quantity,
            precision_rounding=base_line['currency_id'].rounding,
            rounding_method=company.tax_calculation_rounding_method,
            product=base_line['product_id'],
            product_uom=base_line['product_uom_id'],
            special_mode=base_line['special_mode'],
            filter_tax_function=base_line['filter_tax_function'],
        )
        return taxes_computation

    def _set_shopify_order_details(self, client: object, shopify_gid: str, order_data: dict, instance: object):
        """
        This method sets the Shopify order details on the sale order record after successful export.
        :param client: The Shopify GraphQL client used for API calls
        :param shopify_gid: The Shopify GID of the created order
        :param order_data: The data of the created order returned by Shopify
        :param instance: The Shopify instance for which the order was created

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        payment_gateway_obj = self.env["shopify.payment.gateway.ept"]
        gateway = "no_payment_gateway"
        shopify_order_id = client._extract_id_from_gid(shopify_gid)
        shopify_order_number = order_data.get("number")
        financial_status = order_data.get("displayFinancialStatus")
        fulfillment_status = order_data.get("displayFulfillmentStatus") or "unfulfilled"
        payment_gateway_names = order_data.get("paymentGatewayNames", False) or "no_payment_gateway"
        if payment_gateway_names and payment_gateway_names[0]:
            if len(payment_gateway_names) == 1:
                gateway = payment_gateway_names[0]
        if isinstance(financial_status, str):
            financial_status = financial_status.lower()
        if isinstance(fulfillment_status, str):
            fulfillment_status = fulfillment_status.lower()

        order_response = {
            "name": shopify_order_number,
            "financial_status": financial_status,
            "fulfillment_status": fulfillment_status,
        }
        payment_gateway, workflow, _payment_term = payment_gateway_obj.shopify_search_create_gateway_workflow(
            instance, False, order_response, gateway
        )

        vals = {
            "shopify_instance_id": instance.id,
            "shopify_order_id": shopify_order_id,
            "shopify_order_number": shopify_order_number,
            "shopify_order_status": fulfillment_status,
            "shopify_payment_gateway_id": payment_gateway.id if payment_gateway else False,
            "auto_workflow_process_id": workflow.id if workflow else False,
        }
        self.write(vals)
        self._set_shopify_order_line_details(order_data, instance)
        self.message_post(body=_(f"Order exported to Shopify: {shopify_order_number or shopify_order_id}"))

    def _set_shopify_order_line_details(self, order_data: dict, instance: object):
        """
        This method sets Shopify line IDs on sale order lines after successful export.
        :param order_data: The data of the created order returned by Shopify
        :param instance: The Shopify instance for which the order was created

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        line_nodes = order_data.get("lineItems", {}).get("nodes", [])
        if not line_nodes:
            return
        shopify_product_obj = self.env["shopify.product.product.ept"]
        order_lines = self.order_line.filtered(lambda l: not l.display_type and not l.is_delivery)
        variant_line_map = {}
        for line in order_lines:
            shopify_product = shopify_product_obj.search(
                [("product_id", "=", line.product_id.id),
                 ("shopify_instance_id", "=", instance.id),
                 ("exported_in_shopify", "=", True)],
                limit=1
            )
            if not shopify_product or not shopify_product.variant_id:
                continue
            variant_key = str(shopify_product.variant_id)
            variant_line_map.setdefault(variant_key, []).append(line)

        for node in line_nodes:
            variant_gid = (node.get("variant") or {}).get("id")
            line_gid = node.get("id")
            variant_id = ShopifyGraphQLClient._extract_id_from_gid(variant_gid)
            line_id = ShopifyGraphQLClient._extract_id_from_gid(line_gid)
            if not variant_id or not line_id:
                continue
            lines_for_variant = variant_line_map.get(str(variant_id)) or []
            if not lines_for_variant:
                continue
            order_line = lines_for_variant.pop(0)
            order_line.write({"shopify_line_id": str(line_id)})
    
    def check_update_payment_workflow(self, instance, queue_line, order_data):
        """
        During the multi-payment order update webhook, checks whether each existing
        shopify.order.payment.ept line's workflow differs from what is resolved for
        the incoming Shopify transactions and updates it directly if changed.
        Only valid transactions (capture/sale/authorization) are considered via
        prepare_vals_shopify_multi_payment.
        """
        self.ensure_one()
        if not self.is_shopify_multi_payment or not self.shopify_payment_ids:
            return
        
        payment_vals_list = self.prepare_vals_shopify_multi_payment(
            instance, queue_line, order_data, False, False)
        
        existing_by_txn = {str(p.payment_transaction_id): p for p in self.shopify_payment_ids}
        
        for _, _, payment_vals in payment_vals_list:
            transaction_id = str(payment_vals.get('payment_transaction_id', ''))
            existing_payment_line = existing_by_txn.get(transaction_id)
            if not existing_payment_line:
                continue
            
            new_workflow_id = payment_vals.get('workflow_id')
            if existing_payment_line.workflow_id.id != new_workflow_id:
                existing_payment_line.write({'workflow_id': new_workflow_id})
                _logger.info("Workflow of shopify payment %s is updated for Order %s | txn %s: workflow %s -> %s",
                             existing_payment_line, self.name, transaction_id,
                             existing_payment_line.workflow_id.name, new_workflow_id)


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    shopify_line_id = fields.Char("Shopify Line", copy=False)
    is_gift_card_line = fields.Boolean(copy=False, default=False)
    shopify_fulfillment_order_id = fields.Char("Fulfillment Order ID")
    shopify_fulfillment_line_id = fields.Char("Fulfillment Line ID")
    shopify_fulfillment_order_status = fields.Char("Fulfillment Order Status")
    shopify_related_line_id = fields.Char(string='Shopify Related Order Line',
                                          help='Links discount/duties lines to the main product line for Shopify sync.')

    def unlink(self):
        """
        This method is used to prevent the delete sale order line if the order has a Shopify order.
        @author: Haresh Mori on date:17/06/2020
        """
        for record in self:
            if record.order_id.shopify_order_id:
                msg = _(
                    "You can not delete this line because this line is Shopify order line and we need "
                    "Shopify line id while we are doing update order status")
                raise UserError(msg)
        return super(SaleOrderLine, self).unlink()

    @api.onchange('product_uom_qty')
    def _onchange_product_uom_qty(self):
        """
        This method is used to show warning.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if self.shopify_line_id and not self._eligible_to_update_qty():
            message = _("You can update the quantity for this line in Odoo. However, since some quantity has already "
                        "been fulfilled in Shopify, the quantity in Shopify will not be changed.\n"
                        "To add more quantity, please create a new line for the same product with the desired amount.")
            return {'warning': {'title': _('Warning'), 'message': message}}

    def _eligible_to_update_qty(self):
        """
        This method is used to check if order line qty is updatable on shopify.
        :return: boolean

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        done_moves = self.move_ids.filtered(lambda line: line.picking_code == 'outgoing' and line.state == 'done' and
                                                         line.picking_id.shopify_fulfillment_id)
        if self.shopify_line_id and not done_moves:
            return True
        return False


class ImportShopifyOrderStatus(models.Model):
    _name = "import.shopify.order.status"
    _description = 'Order Status'

    name = fields.Char()
    status = fields.Char()
