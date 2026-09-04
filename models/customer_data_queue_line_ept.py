# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

import json
import logging
import time
from datetime import datetime

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger("Shopify Customer Queue Line")


class ShopifyCustomerDataQueueLineEpt(models.Model):
    """This model is used to handel the customer data queue line"""
    _name = "shopify.customer.data.queue.line.ept"
    _description = "Shopify Synced Customer Data Line"

    state = fields.Selection([("draft", "Draft"), ("failed", "Failed"), ("done", "Done"),
                              ("cancel", "Cancelled")], default="draft")
    shopify_synced_customer_data = fields.Char(string="Shopify Synced Data")
    shopify_customer_data_id = fields.Text(string="Customer ID")
    synced_customer_queue_id = fields.Many2one("shopify.customer.data.queue.ept",
                                               string="Shopify Customer",
                                               ondelete="cascade")
    last_process_date = fields.Datetime()
    shopify_instance_id = fields.Many2one("shopify.instance.ept", string="Instance")
    common_log_lines_ids = fields.One2many("common.log.lines.ept",
                                           "shopify_customer_data_queue_line_id",
                                           help="Log lines created against which line.")
    name = fields.Char(string="Customer", help="Shopify Customer Name")
    partner_id = fields.Many2one("res.partner", string="Odoo Partner", help="Partner created against this customer.")

    def shopify_create_multi_queue(self, customer_queue_id, customer_ids):
        """
        This method used to call child method for create a customer queue line.
        :param customer_queue_id: Record of the customer queue.
        :param customer_ids: 125 records of customer response.
        @author: Angel Patel @Emipro Technologies Pvt. Ltd on date 23/10/2019.
        :Task ID: 157065
        """
        if customer_queue_id:
            for result in customer_ids:
                if not isinstance(result, dict):
                    result = result.to_dict()
                self.shopify_customer_data_queue_line_create(result, customer_queue_id)
        return True

    def shopify_customer_data_queue_line_create(self, result, customer_queue_id):
        """
        This method used to create a customer queue line.
        :param result:Response of 1 customer.
        @author: Angel Patel @Emipro Technologies Pvt. Ltd on date 13/01/2020.
        """
        synced_shopify_customers_line_obj = self.env["shopify.customer.data.queue.line.ept"]
        name = "%s %s" % (result.get("first_name") or "", result.get("last_name") or "")
        customer_id = result.get("id")
        data = json.dumps(result)
        instance_id = customer_queue_id.shopify_instance_id.id
        existing_customer_data = synced_shopify_customers_line_obj.search(
            [('shopify_customer_data_id', '=', customer_id), ('shopify_instance_id', '=', instance_id),
             ('state', 'in', ['draft', 'failed'])])
        line_vals = {
            "synced_customer_queue_id": customer_queue_id.id,
            "shopify_customer_data_id": customer_id or "",
            "name": name.strip(),
            "shopify_synced_customer_data": data,
            "shopify_instance_id": instance_id,
            "last_process_date": datetime.now(),
        }
        if not existing_customer_data:
            return synced_shopify_customers_line_obj.create(line_vals)
        return existing_customer_data.write({'shopify_synced_customer_data': data})

    def shopify_create_multi_queue_export_partner(self, customer_queue_id: object, partner_ids: list[object]):
        """
        This method is used to create a customer queue line for export partner to Shopify.
        :param customer_queue_id: Record of the customer queue.
        :param partner_ids: Record of the partners which are exported to Shopify.
        :return: True

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if customer_queue_id:
            for partner in partner_ids:
                # prepare payload for export partner to Shopify
                payload = partner._prepare_shopify_customer_input_graphql()
                payload["addresses"].extend(partner._prepare_shopify_customer_addresses_graphql())
                for child in partner.child_ids:
                    payload["addresses"].append(child._prepare_shopify_address_graphql())
                # export partner to Shopify and create a queue line for each partner
                self.shopify_export_customer_data_queue_line_create(payload, partner, customer_queue_id)
        return True

    def shopify_export_customer_data_queue_line_create(self, payload: dict, partner: object, customer_queue_id: object):
        """
        This method is used to create a customer queue line for export partner to Shopify.
        :param payload: It is the data which is used to create or update the customer in Shopify.
        :param partner: Record of the partner which is exported to Shopify.
        :param customer_queue_id: Record of the customer queue.
        :return: boolean

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        synced_shopify_customers_line_obj = self.env["shopify.customer.data.queue.line.ept"]
        name = partner.display_name
        data = json.dumps(payload)
        instance_id = customer_queue_id.shopify_instance_id.id
        existing_customer_queue_line = synced_shopify_customers_line_obj.search([
            ('partner_id', '=', partner.id), ('shopify_instance_id', '=', instance_id),
            ('state', 'in', ['draft', 'failed'])
        ])
        line_vals = {
            "synced_customer_queue_id": customer_queue_id.id,
            "name": name.strip(),
            "shopify_synced_customer_data": data,
            "shopify_instance_id": instance_id,
            "last_process_date": datetime.now(),
            "partner_id": partner.id,
        }
        if not existing_customer_queue_line:
            return synced_shopify_customers_line_obj.create(line_vals)
        return existing_customer_queue_line.write(line_vals)

    @api.model
    def sync_shopify_customer_into_odoo(self):
        """
        This method is used to find customer queue which queue lines have state in draft and is_action_require is False.
        If cronjob has tried more than 3 times to process any queue then it marks that queue has need process to
        manually. It will be called from auto queue process cron.
        :author: Angel Patel @Emipro Technologies Pvt.Ltd on date 02/11/2019.
        :Task ID: 157065
        """
        shopify_customer_queue_obj = self.env["shopify.customer.data.queue.ept"]
        query = """
            SELECT DISTINCT queue.id, queue_line.create_date
            FROM shopify_customer_data_queue_line_ept AS queue_line
            INNER JOIN shopify_customer_data_queue_ept AS queue
            ON queue_line.synced_customer_queue_id = queue.id
            WHERE queue_line.state = %s AND queue.is_action_require = %s
            ORDER BY queue_line.create_date ASC
        """
        params = ('draft', 'False')
        self.env.cr.execute(query, params)

        customer_data_queue_list = self.env.cr.dictfetchall()
        customer_queue_ids = [queue_data.get('id') for queue_data in customer_data_queue_list]
        if customer_queue_ids:
            queues = shopify_customer_queue_obj.browse(customer_queue_ids)
            self.filter_customer_queue_lines_and_post_message(queues)
        return True

    def filter_customer_queue_lines_and_post_message(self, queues):
        """
        This method is used to post a message if the queue is process more than 3 times otherwise
        it calls the child method to process the customer queue line.
        :param queues: Record of the customer queues.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 16 October 2020.
        @change: By Maulik Barad on 25-Nov-2020. Task : 167734 - Changes of cron execution utilisation.
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        start = time.time()
        customer_queue_process_cron_time = queues.shopify_instance_id.get_shopify_cron_execution_time(
            "shopify_ept.process_shopify_customer_queue")

        for queue in queues:
            results = queue.synced_customer_queue_line_ids.filtered(lambda x: x.state == "draft")

            queue.queue_process_count += 1
            # queue.queue_process_count = 4
            if queue.queue_process_count > 3:
                queue.is_action_require = True
                note = _("<p>Need to process this customer queue manually.There are 3 attempts been made by " \
                         "automated action to process this queue,"
                         "<br/>- Ignore, if this queue is already processed.</p>")
                queue.message_post(body=note)
                if queue.shopify_instance_id.is_shopify_create_schedule:
                    common_log_line_obj.create_crash_queue_schedule_activity(queue, "shopify.customer.data.queue.ept",
                                                                             note)
                continue
            self.env.cr.commit()
            results.process_customer_queue_lines()
            if time.time() - start > customer_queue_process_cron_time - 60:
                return True

    def process_customer_queue_lines(self):
        """
        This method process the queue lines.
        """
        queues = self.synced_customer_queue_id

        start_time = time.time()
        for queue in queues:
            instance = queue.shopify_instance_id
            if instance.active:
                query = """
                    UPDATE shopify_customer_data_queue_ept
                    SET is_process_queue = %s
                    WHERE is_process_queue = %s
                """
                params = (False, True)
                self.env.cr.execute(query, params)
                self.env.cr.commit()

                self.customer_queue_commit_and_process(queue, instance)

                _logger.info("Customer Queue %s is processed.", queue.name)
            current_time = time.time()
            # Stop process if 12 minutes are passed since processing start.
            if current_time - start_time > 720:
                break

        return True

    def customer_queue_commit_and_process(self, queue, instance):
        """ This method is used to commit the customer queue line after 10 customer queue line process
            and call the child method to process the customer queue line.
            :param queue: Record of customer queue.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 17 October 2020 .
        """
        if queue.is_export_queue:
            return self.export_customer_queue_lines(queue, instance)
        company_id = False
        shopify_partner_obj = self.env["shopify.res.partner.ept"]
        commit_count = 0
        for line in self:
            commit_count += 1
            if commit_count == 10:
                queue.is_process_queue = True
                self.env.cr.commit()
                commit_count = 0

            customer_data = json.loads(line.shopify_synced_customer_data)
            main_partner = shopify_partner_obj.with_context(customer_data_queue=True).shopify_create_contact_partner(customer_data, instance, line)
            if main_partner:
                default_address = customer_data.get("default_address")
                default_address_id = default_address.get("id") if default_address else None
                for address in customer_data.get("addresses"):
                    if address.get("default"):
                        continue
                    elif address.get("id") == default_address_id and instance.use_graphql_api:
                        continue
                    shopify_partner_obj.shopify_create_or_update_address(instance, address, main_partner, "other")

                line.update(
                    {"state": "done", "last_process_date": datetime.now(), 'shopify_synced_customer_data': False})
                if instance.enable_metafield_sync and instance.use_graphql_api:
                    shopify_partner_obj.set_metafield_values_in_customer(customer_data, instance, main_partner)
            else:
                line.update({"state": "failed", "last_process_date": datetime.now()})
            queue.is_process_queue = False
        
        
    def export_customer_queue_lines(self, queue: object, instance: object):
        """
        This method is used to export the customer queue line to Shopify.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        current_date = fields.Datetime.now()
        shopify_partner_obj = self.env["shopify.res.partner.ept"]
        for line in self:
            queue.is_process_queue = True
            self.env.cr.commit()

            customer_data = json.loads(line.shopify_synced_customer_data)
            shopify_partner = shopify_partner_obj.search([('partner_id', '=', line.partner_id.id),
                                                          ('shopify_instance_id', '=', instance.id)], limit=1)
            if shopify_partner:
                line.update({"state": "done", "last_process_date": current_date})
            else:
                line._export_customer_to_shopify(instance, customer_data, current_date)
                
            
            queue.is_process_queue = False
            self.env.cr.commit()
        # if instance and instance.enable_metafield_sync and instance.use_graphql_api:
        #     customer_data=self.mapped('partner_id')
        #     # Prepared queue line vals for export metafield queue line and create queue line for each product template.
        #     self.env["shopify.export.metafields.queue.ept"].prepare_export_customer_metafield_queue_line(customer_data, instance)

    def _export_customer_to_shopify(self, instance: object, customer_data: dict, current_date: datetime):
        """
        This method is used to export the customer to Shopify.
        :param instance: Record of the instance.
        :param customer_data: It is the data which is used to create or update the customer in Shopify.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        res_partner_obj = self.env['res.partner']
        shopify_partner_obj = self.env["shopify.res.partner.ept"]
        addresses = customer_data.pop("addresses", [])
        email = (customer_data.get("email", "")).strip()
        partner = self.partner_id
        helper = res_partner_obj._get_customer_helper(instance)

        # Find customer in shopify by email
        response = partner._search_shopify_customer_by_email_graphql(helper, email=email)

        if not response:
            # Export customer to Shopify
            if instance and instance.enable_metafield_sync and instance.use_graphql_api:
                customer_payload,message =self.env["shopify.export.metafields.queue.ept"].prepared_export_metafield_payload(
            partner,instance,"res.partner","CUSTOMER"
                )
                if customer_payload and not message:
                    customer_data['metafields'] = customer_payload
            response = res_partner_obj._export_customer(instance, helper, customer_data)

        partner.write({'is_shopify_customer': True})
        shopify_customer_gid = response.get("id")
        shopify_customer_id = helper.client._extract_id_from_gid(shopify_customer_gid)
        self.shopify_customer_data_id = str(shopify_customer_id)

        # Export address to Shopify
        response = res_partner_obj._export_address(instance, helper, addresses, shopify_customer_gid)
        if isinstance(response, str):
            raise UserError(_(response))

        # Create record in shopify res partner
        shopify_partner = shopify_partner_obj.create({
            "partner_id": partner.id,
            "shopify_instance_id": instance.id,
            "shopify_customer_id": str(shopify_customer_id),
        })

        if shopify_partner:
            self.update({"state": "done", "last_process_date": current_date})
        else:
            self.update({"state": "failed", "last_process_date": current_date})
        return shopify_partner
