# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class MarketAutoWorkflowConfiguration(models.Model):
    """Market-specific auto workflow configuration for Shopify orders."""
    _name = "market.auto.workflow.configuration.ept"
    _description = "Market auto workflow configuration"

    @api.model
    def _default_payment_term(self):
        payment_term = self.env.ref("account.account_payment_term_immediate")
        return payment_term.id if payment_term else False

    @api.model
    def _default_shopify_order_status(self):
        order_status = self.env.ref("shopify_ept.unshipped", False)
        return order_status.id if order_status else False

    financial_status = fields.Selection([
        ("pending", "The finances are pending"),
        ("authorized", "The finances have been authorized"),
        ("partially_paid", "The finances have been partially paid"),
        ("paid", "The finances have been paid"),
        ("partially_refunded", "The finances have been partially refunded"),
        ("refunded", "The finances have been refunded"),
        ("voided", "The finances have been voided"),
    ], default="paid", required=True)
    auto_workflow_id = fields.Many2one("sale.workflow.process.ept", "Auto Workflow", required=True)
    payment_gateway_id = fields.Many2one("shopify.payment.gateway.ept", "Payment Gateway", ondelete="restrict", required=True)
    payment_term_id = fields.Many2one(
        "account.payment.term",
        string="Payment Term",
        default=_default_payment_term,
        required=True,
    )
    shopify_market_id = fields.Many2one(
        "shopify.market.ept",
        string="Shopify Market",
        required=True,
        ondelete="cascade",
    )
    shopify_instance_id = fields.Many2one(
        "shopify.instance.ept",
        string="Instance",
        related="shopify_market_id.shopify_instance_id",
        store=True,
        readonly=True,
    )
    active = fields.Boolean("Active", default=True)
    shopify_order_payment_status = fields.Many2one(
        "import.shopify.order.status",
        string="Shopify Order Status",
        default=_default_shopify_order_status,
        required=True,
    )

    _market_auto_workflow_unique_constraint = models.Constraint(
        "unique(financial_status,shopify_market_id,payment_gateway_id,shopify_order_payment_status)",
        "Financial status must be unique per market, gateway and order status.",
    )

    @api.constrains("auto_workflow_id", "shopify_market_id")
    def _check_auto_workflow_company(self):
        for rec in self:
            journal_company = rec.auto_workflow_id.sale_journal_id.company_id
            market_company = rec.shopify_market_id.company_id
            if journal_company and market_company and journal_company != market_company:
                raise ValidationError(
                    "The selected Auto Workflow uses a Sale Journal from a different company than the Shopify Market. "
                    "Please choose an Auto Workflow with the same company."
                )
