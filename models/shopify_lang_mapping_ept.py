# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class ShopifyLangMappingEpt(models.Model):
    _name = 'shopify.lang.mapping.ept'
    _description = 'Shopify Language Mapping'
    _rec_name = 'shopify_locale'
    _order = 'is_primary desc, shopify_locale'

    shopify_instance_id = fields.Many2one(comodel_name='shopify.instance.ept', string='Instance', required=True,
                                          ondelete='cascade', index=True)
    shopify_locale = fields.Char(string='Shopify Locale Code', required=True,
                                 help="Shopify locale code, e.g. 'en', 'fr', 'de', 'ar' Find these in Shopify Admin → Settings → Languages.")
    odoo_lang_id = fields.Many2one(comodel_name='res.lang', string='Odoo Language', required=False,
                                   help="The Odoo language that corresponds to this Shopify locale.")
    is_primary = fields.Boolean(string='Primary Language?', default=False,
                                help=(
                                    "Mark the store's default language as Primary.\n"
                                    "Translations are pushed for all NON-primary languages.\n"
                                    "Exactly ONE mapping per instance should be marked Primary."
                                ),
                                )

    _unique_locale_per_instance = models.Constraint('unique(shopify_instance_id, shopify_locale)',
                                                'A Shopify locale can only be mapped once per instance.')

    @api.constrains('is_primary', 'shopify_instance_id')
    def _check_single_primary(self):
        for rec in self:
            if rec.is_primary:
                count = self.search_count([
                    ('shopify_instance_id', '=', rec.shopify_instance_id.id),
                    ('is_primary', '=', True),
                    ('id', '!=', rec.id),
                ])
                if count:
                    raise ValidationError(
                        "Only one language mapping per instance can be marked as Primary Language."
                    )
