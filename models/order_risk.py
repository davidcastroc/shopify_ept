# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields
from datetime import datetime
from .. import shopify_graphql


class ShopifyOrderRisk(models.Model):
    _name = "shopify.order.risk"
    _description = 'Shopify Order Risk'

    name = fields.Char("Order Id", required=True)
    risk_id = fields.Char()
    cause_cancel = fields.Boolean(default=False)
    display = fields.Boolean(default=False)
    message = fields.Text()
    recommendation = fields.Selection([('cancel', 'This order should be cancelled by the merchant'),
                                       ('investigate',
                                        'This order might be fraudulent and needs further investigation'),
                                       ('accept', 'This check found no indication of fraud')
                                       ], default='accept')
    score = fields.Float()
    source = fields.Char()
    odoo_order_id = fields.Many2one("sale.order", string="Order")

    def shopify_create_risk_in_order(self, risk_result, order):
        """This method used to create a risk, if found risk in Shopify order when import orders from Shopify to Odoo.
            :param risk_result: Response of risk API call.
            :param order: Record of sale order.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 11/11/2019.
            Task Id : 157350
        """
        flag = True
        for risk_id in risk_result:
            risk = risk_id.to_dict()
            if risk.get('recommendation') != 'accept':
                flag = False
            vals = self.prepare_vals_for_risk_order(risk, order)
            self.create(vals)
        return flag

    def prepare_vals_for_risk_order(self, risk, order):
        """ This method is used to prepare a vals for the create record of risk order.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 16 October 2020 .
        """
        vals = {'name': risk.get('order_id'), 'risk_id': risk.get('id'),
                'cause_cancel': risk.get('cause_cancel'),
                'display': risk.get('display'),
                'message': risk.get('message'),
                'recommendation': risk.get('recommendation'),
                'score': risk.get('score'),
                'source': risk.get('source'),
                'odoo_order_id': order.id
                }
        return vals

    def shopify_create_risk_in_order_by_graphql(self, order_response, order):

        risky_orders = order_response.get('risk', {})
        if not risky_orders:
            instance = order.shopify_instance_id
            client = instance.get_graphql_client()
            risk_result = shopify_graphql.OrderQueryHelper(client).get_order([order_response.get("id")],
                                                                             specific_fields_key='returns_and_risks')
            risky_orders = risk_result and risk_result[0].get('risk', {})
        recommendation = risky_orders.get('recommendation') or ''
        if recommendation == 'NONE' or '':
            return
        risk_level = ''
        assessments = risky_orders.get('assessments')
        for assment in assessments:
            risk_level = assment.get('risk_level')
            if risk_level:
                break
        vals = self._prepare_risk_order_vals_by_graphql(order, recommendation.lower(), risk_level.lower())
        if vals:
            self.create(vals)
            if recommendation.lower() != 'accept':
                order.write({'is_risky_order': True})

    def _prepare_risk_order_vals_by_graphql(self, order, recommendation, risk_level):

        vals = {}
        if risk_level and recommendation:
            mesaage = (f'The Risk level for this order is {risk_level} and \nthe order recommedation is {recommendation} .'
                       f'Creating Risk Order at time {datetime.now()} \nThis Risk Order Record is created by GraphQL API')
            vals = {
                'name': order.shopify_order_id,
                'recommendation': recommendation.lower(),
                'odoo_order_id': order.id,
                'message': mesaage,
            }
        return vals