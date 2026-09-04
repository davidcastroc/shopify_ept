# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
from odoo import models, fields


class ShopifyCustomerSegmentMemberEpt(models.Model):
    _name = "shopify.customer.segment.member.ept"
    _description = "Shopify Customer Segment Member"
    _rec_name = "shopify_display_name"
    _order = "shopify_display_name"

    segment_id = fields.Many2one(comodel_name="shopify.customer.segment.ept", string="Segment", required=True,
                                 ondelete="cascade")
    shopify_member_id = fields.Char(string="Shopify Member GID", readonly=True)
    shopify_display_name = fields.Char(string="Shopify Display Name", readonly=True)
    first_name = fields.Char(string="First Name", readonly=True)
    last_name = fields.Char(string="Last Name", readonly=True)
    email = fields.Char(string="Email", readonly=True)
    last_order_id = fields.Char(string="Last Order GID", readonly=True)
    number_of_orders = fields.Integer(string="Number of Orders", readonly=True)
    amount_spent = fields.Float(string="Amount Spent", readonly=True, digits=(16, 2))
    currency_code = fields.Char(string="Currency", readonly=True)
    partner_id = fields.Many2one(comodel_name="res.partner", string="Odoo Customer", readonly=True,
                                 help="Matched Odoo partner based on email.")
