import time
import json
import logging
import ast
import pytz
from odoo import models, fields
import re
from ..shopify.pyactiveresource.connection import ClientError
from .. import shopify
from ..shopify_graphql.client import ShopifyGraphQLClient
from ..shopify_graphql.queries.metafield import MetafieldQueryHelper

utc = pytz.utc

_logger = logging.getLogger("Shopify Export Metafields Queue Line")


class ShopifyExportMetafieldsQueueLineEpt(models.Model):
    _name = "shopify.export.metafields.queue.line.ept"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "Shopify Export Metafields Queue Line"

    name = fields.Char()
    shopify_instance_id = fields.Many2one("shopify.instance.ept", string="Instance")
    last_process_date = fields.Datetime() 
    state = fields.Selection([("draft", "Draft"), ("failed", "Failed"), ("done", "Done"),
                              ("cancel", "Cancelled")],
                             default="draft")
    export_metafields_queue_id = fields.Many2one("shopify.export.metafields.queue.ept", required=True,
                                            ondelete="cascade", copy=False)
    common_log_lines_ids = fields.One2many("common.log.lines.ept",
                                           "shopify_export_metafields_queue_line_id",
                                           help="Log lines created against which line.")
    resource_type = fields.Selection(related="export_metafields_queue_id.resource_type", store=True)
    metafield_payload = fields.Text(string="Metafield Payload", help="It is used to store the GraphQL formatted metafield payload which is used to export metafield in Shopify.")

    def auto_export_metafields_queue_data(self):
        """
        This method is used to find export metafields queue which queue lines have state
        in draft and is_action_require is False.       
        """
        export_metafields_queue_obj = self.env["shopify.export.metafields.queue.ept"]
        export_metafields_queue_ids = []
        query = """
            UPDATE shopify_export_metafields_queue_ept
            SET is_process_queue = %s
            WHERE is_process_queue = %s
        """
        params = (False, True)

        self.env.cr.execute(query, params)
        self.env.cr.commit()
        query = """
            SELECT DISTINCT queue.id
            FROM shopify_export_metafields_queue_line_ept AS queue_line
            INNER JOIN shopify_export_metafields_queue_ept AS queue
            ON queue_line.export_metafields_queue_id = queue.id
            WHERE queue_line.state IN (%s) AND queue.is_action_require = %s
            GROUP BY queue.id
            ORDER BY queue.id
        """
        params = ('draft', False)

        self.env.cr.execute(query, params)

        export_metafields_queue_list = self.env.cr.fetchall()
        if not export_metafields_queue_list:
            return True

        export_metafields_queue_ids = [result[0] for result in export_metafields_queue_list]
        queues = export_metafields_queue_obj.browse(export_metafields_queue_ids)
        self.filter_export_metafields_queue_lines_and_post_message(queues)


    def filter_export_metafields_queue_lines_and_post_message(self, queues):
        """
        This method is used to post a message if the queue is process more than 3 times otherwise
        it calls the child method to process the export metafields queue line.
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        start = time.time()
        export_metafields_queue_process_cron_time = queues.shopify_instance_id.get_shopify_cron_execution_time(
            "shopify_ept.process_shopify_export_metafields_queue")
        for queue in queues:
            export_metafields_queue_line_ids = queue.export_metafields_queue_line_ids.filtered(lambda x: x.state == "draft")

            # For counting the queue crashes and creating schedule activity for the queue.
            queue.queue_process_count += 1
            if queue.queue_process_count > 3:
                queue.is_action_require = True
                note = "<p>Need to process this export metafields queue manually.There are 3 attempts been made by " \
                       "automated action to process this queue,<br/>- Ignore, if this queue is already processed.</p>"
                queue.message_post(body=note)
                if queue.shopify_instance_id.is_shopify_create_schedule:
                    common_log_line_obj.create_crash_queue_schedule_activity(queue, "shopify.export.metafields.queue.ept",
                                                                             note)
                continue

            self.env.cr.commit()
            export_metafields_queue_line_ids.process_export_metafields_queue_data()
            if time.time() - start > export_metafields_queue_process_cron_time - 60:
                return True

    def process_export_metafields_queue_data(self):
        """
        This method is used to processes export metafields queue lines.
        """
        queue_id = self.export_metafields_queue_id if len(self.export_metafields_queue_id) == 1 else False
        if queue_id:            
            query = """
                UPDATE shopify_export_metafields_queue_ept
                SET is_process_queue = %s
                WHERE is_process_queue = %s
            """
            params = (False, True)
            self.env.cr.execute(query, params)
            self.env.cr.commit()            
            self._prepare_data_and_export_metafields_by_graphql(self)     
        return True

    def _prepare_data_and_export_metafields_by_graphql(self, queue_lines):
        """
        This Method prepare the data required for the metafields export to shopify and export in shopify using the GraphQL API
        @params : queue_lines : all the metafields queueline associated with a queue.
    
        """
        instance = self.shopify_instance_id        
        common_log_line_obj = self.env['common.log.lines.ept']
        model = "shopify.export.metafields.queue.ept"
        # Prefer client-based GraphQL mutation via MetafieldQueryHelper
        client = instance.get_graphql_client()
        metafield_helper = MetafieldQueryHelper(client)        
        for line in queue_lines:
            payload = line.metafield_payload            
            if isinstance(payload, str):
                payload = payload.strip()
                if not payload:
                    payload = []
                else:
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        try:
                            payload = ast.literal_eval(payload)
                        except Exception:
                            _logger.error("Invalid metafields payload format: %s", payload)
                            msg= "Invalid metafields payload format for queue line ID: %s. Payload must be a valid JSON string or Python literal. Got: %s" % (line.id, payload)
                            common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                module="shopify_ept",
                                                                message=msg,
                                                                model_name=model,
                                                                shopify_export_metafields_queue_line_id=line.id)
                            line.write({"state": "failed"})
                            continue

            if isinstance(payload, dict):
                payload = [payload]
            if not isinstance(payload, list):
                _logger.error("Metafields payload must be list/dict/JSON string. Got: %s", type(payload))
                msg = "Metafields payload must be list/dict/JSON string for queue line ID: %s. Got type: %s" % (line.id, type(payload))
                common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,
                                                            module="shopify_ept",
                                                            message=msg,
                                                            model_name=model,
                                                            shopify_export_metafields_queue_line_id=line.id)
                line.write({"state": "failed"})
                continue
            # Find list of key of payload
            unique_keys = sorted({d.get("key") for d in payload if d.get("key")})
            # Find shopify.metafield.config.ept record for each unique key
            metafield_config_obj = self.env['shopify.metafield.config.ept']
            model_id=metafield_config_obj._get_model_by_owner_type(queue_lines.export_metafields_queue_id.resource_type)
            metafield_configs = metafield_config_obj.search([('key', 'in', unique_keys),('model_id', '=', model_id),('instance_id', '=', instance.id)]) 
            message=""
            for metafield in payload:
                key = metafield.get("key")
                metafield_config = metafield_configs.filtered(lambda x: x.key == key)
                if not metafield_config:
                    message = "No metafield configuration found for key '%s' in queue line ID: %s. Please create a configuration in Shopify Metafield Config." % (key, line.id)
                    common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                module="shopify_ept",
                                                                message=message,
                                                                model_name=model,
                                                                shopify_export_metafields_queue_line_id=line.id)
                    line.write({"state": "failed"})
                    continue
                value = metafield.get("value")
                validation_message = self.validate_shopify_metafield_value(value, metafield_config)
                if validation_message:
                    owner_id = metafield.get('ownerId').split("/")[-1] if metafield.get('ownerId') else "N/A"
                    resource_type = metafield_config.model_id.name if metafield_config.model_id else "N/A"
                    message += (
                        f"Validation Error for metafield key '{key}' with value '{value}' in queue line ID: {line.id}."
                        f"\n{resource_type} : {owner_id}"
                        f"\nError Details: {validation_message}\n\n"
                    )
            if message:  
                error_log = (
                    "System tried to validate metafield values for export but found errors.\n"
                    "Action Items:\n"
                    "- Check the metafield configuration for the keys used in this queue line under: Shopify → Configuration → Shopify Metafield Config.\n"
                    "- Ensure the values provided meet the required validations (length, format, type, etc.) as per the configuration.\n"
                    "- Correct the values in the queue line or update the configuration as needed.\n\n"
                )
                message = error_log + message
                
                common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,
                                                            module="shopify_ept",
                                                            message=message,
                                                            model_name=model,
                                                            shopify_export_metafields_queue_line_id=line.id)
                line.write({"state": "failed"})
                continue
            
            metafield_result = metafield_helper.set_metafields(payload)
            if metafield_result:
                req_error = metafield_result.get('data') and metafield_result.get('data').get(
                    'metafieldsSet') and metafield_result.get('data').get('metafieldsSet').get('userErrors')
                req_other_error = metafield_result.get('errors')
                if not req_error and req_other_error:
                    req_error = req_other_error
                if not req_error:
                    line.write({"state": "done"})
                else:
                    message = "Error while Export metafields for Queue: %s for instance: " \
                            "'%s'\nError: %s \n\n When an error is received while exporting metafields using the Shopify GraphQL API, \nthe system currently links the error message to" \
                            "the first queue line in the queue. However, \nthe actual error may be related to any queue line, not necessarily the first one. Since the Shopify API response does not specify which particular metafield" \
                            "item or data entry caused the error, it becomes difficult to \nidentify the exact queue line responsible for the" \
                            "issue. " % (queue_lines.export_metafields_queue_id, instance.name, str(req_error))
                    common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                module="shopify_ept",
                                                                message=message,
                                                                model_name=model,
                                                                shopify_export_metafields_queue_line_id=line.id)
                    line.write({"state": "failed"})
                    continue
        
            self.env.cr.commit()
        return True


    def validate_shopify_metafield_value(self, value, metafield_config):
        """
            Validate Odoo value against Shopify metafield validations.
        """
        value_type = metafield_config.value_type
        validations = metafield_config.validations or {}

        if isinstance(validations, list):
            validations = {v["name"]: v["value"] for v in validations}
        message = ""

        if value_type in ["single_line_text_field", "multi_line_text_field", "rich_text_field"]:
            value = str(value)
            if "min" in validations and len(value) < int(validations["min"]):
                message += f"Value is shorter than minimum allowed length ({validations['min']}). "
            if "max" in validations and len(value) > int(validations["max"]):
                message += f"Value exceeds maximum allowed length ({validations['max']}). "
            # Regex validation
            if "regex" in validations:                
                if not re.match(validations["regex"], str(value)):
                    message += f"Value '{value}' does not match Shopify regex validation." 

        elif value_type == "number_integer":
            value = int(value)

            if "min" in validations and value < int(validations["min"]):
                message += f"Value is smaller than minimum allowed ({validations['min']}). "

            if "max" in validations and value > int(validations["max"]):
                message += f"Value exceeds maximum allowed ({validations['max']}). "

        elif value_type == "number_decimal":
            value = float(value)

            if "min" in validations and value < float(validations["min"]):
                message += f"Value is smaller than minimum allowed ({validations['min']}). "

            if "max" in validations and value > float(validations["max"]):
                message += f"Value exceeds maximum allowed ({validations['max']}). "
            
            if "max_precision" in validations:
                max_precision = validations["max_precision"]
                try:
                    if isinstance(value, float) or isinstance(value, int):                   
                        decimal_part = str(value).split(".")[1]
                        if len(decimal_part) > int(max_precision):
                            message = f"Value exceeds max precision {max_precision}"
                except IndexError:
                    pass  # No decimal part, so it's valid for precision 
        return message