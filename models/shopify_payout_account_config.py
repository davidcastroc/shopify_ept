# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
from odoo import models, fields
from .shopify_payout_report_line_ept import SHOPIFY_PAYOUT_TRANSACTION_TYPES


class ShopifyPaymentReportEpt(models.Model):
    _name = "shopify.payout.account.config.ept"
    _description = "Shopify Account Configurations"

    # Shopify Payout Report
    instance_id = fields.Many2one('shopify.instance.ept', string="Instance")
    account_id = fields.Many2one('account.account', string="Account",
                                 help="The account used for this invoice.")
    transaction_type = fields.Selection(SHOPIFY_PAYOUT_TRANSACTION_TYPES, help="The type of the balance transaction",
                                        string="Balance Transaction Type")
    analytic_account_id = fields.Many2one('account.analytic.account', string="Analytic Account",
                                          help="Analytic account to set on the journal entry for this transaction type.")
