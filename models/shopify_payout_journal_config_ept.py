# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class ShopifyPayoutJournalConfigurationEpt(models.Model):
    _name = "shopify.payout.journal.config.ept"
    _description = "Shopify Payout Journal Configuration"

    instance_id = fields.Many2one('shopify.instance.ept', string="Instance")
    currency_id = fields.Many2one("res.currency", required=True, string="Currency")
    journal_id = fields.Many2one("account.journal", required=True, string="Bank Journal")

    _sql_constraints = [('unique_currency_instance', 'unique (instance_id, currency_id)',
                         "A journal is already configured for this currency.")]

    @api.constrains("currency_id", "journal_id")
    def _check_journal_currency_ept(self):
        for rec in self:
            journal_currency = (rec.journal_id.currency_id or rec.journal_id.company_id.currency_id)

            if journal_currency != rec.currency_id:
                raise ValidationError(_("Journal '%s' must have currency '%s'.") % (rec.journal_id.display_name,
                                                                                    rec.currency_id.display_name))
