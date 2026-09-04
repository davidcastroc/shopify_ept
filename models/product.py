# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        """
        This method use to archive/unarchive shopify product templates base on odoo product templates.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 09/12/2019.
        :Task id: 158502
        """
        if 'active' in vals.keys():
            shopify_product_template_obj = self.env['shopify.product.template.ept']
            for template in self:
                shopify_templates = shopify_product_template_obj.sudo().search(
                    [('product_tmpl_id', '=', template.id)])
                if vals.get('active'):
                    shopify_templates = shopify_product_template_obj.sudo().search(
                        [('product_tmpl_id', '=', template.id), ('active', '=', False)])
                shopify_templates.sudo().write({'active': vals.get('active')})
        res = super(ProductTemplate, self).write(vals)
        return res

    shopify_template_count = fields.Integer(string='# Sales', compute='_compute_shopify_template_count')

    def _compute_shopify_template_count(self):
        shopify_product_template_obj = self.env['shopify.product.template.ept']
        for template in self:
            shopify_templates = shopify_product_template_obj.sudo().search([('product_tmpl_id', '=', template.id)])
            template.shopify_template_count = len(shopify_templates) if shopify_templates else 0


class ProductProduct(models.Model):
    _inherit = 'product.product'

    shopify_product_count = fields.Integer(string='# Sales Count', compute='_compute_shopify_product_count')
    
    def _compute_shopify_product_count(self):
        shopify_product_obj = self.env['shopify.product.product.ept']
        for product in self:
            shopify_products = shopify_product_obj.sudo().search([('product_id', '=', product.id)])
            product.shopify_product_count = len(shopify_products) if shopify_products else 0

    def write(self, vals):
        """
        This method use to archive/unarchive shopify product base on odoo product.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 30/03/2019.
        """
        if 'active' in vals.keys():
            shopify_product_product_obj = self.env['shopify.product.product.ept']
            for product in self:
                shopify_product = shopify_product_product_obj.sudo().search(
                    [('product_id', '=', product.id)])
                if vals.get('active'):
                    shopify_product = shopify_product_product_obj.sudo().search(
                        [('product_id', '=', product.id), ('active', '=', False)])
                shopify_product.sudo().write({'active': vals.get('active')})
        res = super(ProductProduct, self).write(vals)
        return res
