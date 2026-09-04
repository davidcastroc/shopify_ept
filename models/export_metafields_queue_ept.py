import logging
import pytz
from odoo import models, fields, api
from datetime import datetime, timedelta
utc = pytz.utc

_logger = logging.getLogger("Shopify Export Metafields Queue")


class ShopifyExportMetafieldsQueueEpt(models.Model):
    _name = "shopify.export.metafields.queue.ept"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "Shopify Export Metafields Queue"

    name = fields.Char(size=120)
    shopify_instance_id = fields.Many2one("shopify.instance.ept", string="Instance")
    state = fields.Selection([("draft", "Draft"), ("partially_completed", "Partially Completed"),
                              ("completed", "Completed"), ("failed", "Failed")], default="draft",
                             compute="_compute_queue_state", store=True, tracking=True)
    export_metafields_queue_line_ids = fields.One2many("shopify.export.metafields.queue.line.ept",
                                                  "export_metafields_queue_id",
                                                  string="Export Metafields Queue Lines")
    common_log_lines_ids = fields.One2many("common.log.lines.ept", compute="_compute_log_lines")
    queue_line_total_records = fields.Integer(string="Total Records",
                                              compute="_compute_queue_line_record")
    queue_line_draft_records = fields.Integer(string="Draft Records",
                                              compute="_compute_queue_line_record")
    queue_line_fail_records = fields.Integer(string="Fail Records",
                                             compute="_compute_queue_line_record")
    queue_line_done_records = fields.Integer(string="Done Records",
                                             compute="_compute_queue_line_record")
    queue_line_cancel_records = fields.Integer(string="Cancelled Records",
                                               compute="_compute_queue_line_record")    
    is_process_queue = fields.Boolean("Is Processing Queue", default=False)
    running_status = fields.Char(default="Running...")
    is_action_require = fields.Boolean(default=False)
    queue_process_count = fields.Integer(string="Queue Process Times",
                                         help="it is used know queue how many time processed")
    resource_type = fields.Selection([
        ('PRODUCT', 'PRODUCT'),
        ('PRODUCTVARIANT', 'PRODUCT VARIANT'),
        ('ORDER', 'ORDER'),
        ('CUSTOMER', 'CUSTOMER')
          ], string='Resource Type',
          help="It is used to identify that for which resource metafield export queue line created")
    owner_ids = fields.Char(string="Owner Id", help="It is used to store the Shopify GraphQL owner id for which metafield export queue line created.")

    @api.depends('export_metafields_queue_line_ids.common_log_lines_ids')
    def _compute_log_lines(self):
        for line in self:
            line.common_log_lines_ids = line.export_metafields_queue_line_ids.common_log_lines_ids

    @api.depends(
        "export_metafields_queue_line_ids.state"
    )
    def _compute_queue_line_record(self):
        """
        This is used for count of total record of
        export_metafields queue line base on its state
        and it displays in the form view of
        export metafields queue.
        """
        for export_metafields_queue in self:
            queue_lines = (
                export_metafields_queue.export_metafields_queue_line_ids
            )
            export_metafields_queue.queue_line_total_records = (
                len(queue_lines)
            )
            export_metafields_queue.queue_line_draft_records = (
                len(
                    queue_lines.filtered(
                        lambda x: x.state == "draft"
                    )
                )
            )
            export_metafields_queue.queue_line_fail_records = (
                len(
                    queue_lines.filtered(
                        lambda x: x.state == "failed"
                    )
                )
            )
            export_metafields_queue.queue_line_done_records = (
                len(
                    queue_lines.filtered(
                        lambda x: x.state == "done"
                    )
                )
            )
            export_metafields_queue.queue_line_cancel_records = (
                len(
                    queue_lines.filtered(
                        lambda x: x.state == "cancel"
                    )
                )
            )

    @api.depends("export_metafields_queue_line_ids.state")
    def _compute_queue_state(self):
        """
        Computes queue state from different states of queue lines.
        """
        for record in self:
            if record.queue_line_total_records == record.queue_line_done_records + record.queue_line_cancel_records:
                record.state = "completed"
            elif record.queue_line_draft_records == record.queue_line_total_records:
                record.state = "draft"
            elif record.queue_line_total_records == record.queue_line_fail_records:
                record.state = "failed"
            else:
                record.state = "partially_completed"

    @api.model_create_multi
    def create(self, vals):
        """This method used to create a sequence for export_metafields queue.
        """
        for val in vals:
            sequence_id = self.env.ref("shopify_ept.seq_export_metafields_queue").ids
            if sequence_id:
                record_name = self.env["ir.sequence"].browse(sequence_id).next_by_id()
            else:
                record_name = "/"
            val.update({"name": record_name or ""})
        return super(ShopifyExportMetafieldsQueueEpt, self).create(vals)

    def create_export_metafields_queue(self, instance, export_metafields_data, resource_type):
        """
            Creates export metafields queues and adds queue lines in it.        
        """  
        ownerIds = [rec.get('ownerId').split('/')[-1] for rec in export_metafields_data]
        export_metafields_queue = self.shopify_create_export_metafields_queue(instance, resource_type, ownerIds)
        message = "Export Metafields Queue Created %s" % ', '.join(export_metafields_queue.mapped('name'))
        _logger.info(message)
        self.shopify_create_export_metafields_queue_line(export_metafields_data, instance, export_metafields_queue)
        self.env.cr.commit()
        
        return export_metafields_queue

    def shopify_create_export_metafields_queue(self, instance,resource_type,ownerIds):
        """
        This method used to create a export metafields queue.
        """
        product_queue_vals = {
            "shopify_instance_id": instance and instance.id or False,
            "resource_type": resource_type,
            "owner_ids": ','.join(ownerIds) if ownerIds else False
        }
        return self.create(product_queue_vals)

    def shopify_create_export_metafields_queue_line(self, data, instance, export_metafields_queue):
        """
            This method used to create a export metafields data queue line.
        """
        for i in range(0, len(data), 20):
            batch = data[i:i+20]
            export_metafields_queue_line_vals = {
                "shopify_instance_id": instance and instance.id or False,
                'name': "Export Metafield Queue Line",
                "metafield_payload": batch,
                "export_metafields_queue_id": export_metafields_queue and export_metafields_queue.id or False
            }
            self.env['shopify.export.metafields.queue.line.ept'].create(export_metafields_queue_line_vals)
        
        return True

    def prepare_export_metafield_queue_line(self, instance, shopify_templates):
        """
            This method is used to prepare export metafield queue line for product and product variant.
        """
        mapping_records = self.env["shopify.metafield.config.ept"].get_shopify_metafield_configs(
            instance.id, ["product.template", "product.product"], 'update'
        )
        if not mapping_records:
            return True

        self._prepare_and_create_metafield_queues(
            instance, shopify_templates, mapping_records, "product.template", "PRODUCT"
        )
        variants = shopify_templates.mapped("shopify_product_ids")
        if variants:
            self._prepare_and_create_metafield_queues(
                instance, variants, mapping_records, "product.product", "PRODUCTVARIANT"
            )

        queue_cron = self.env.ref("shopify_ept.process_shopify_export_metafields_queue")
        if not queue_cron.active:
            _logger.info("Active the Export metafields data process queue cron job")
            queue_cron.write(
                {'active': True, 'nextcall': datetime.now() + timedelta(seconds=120)})
        return True
    
    def prepare_export_order_metafield_queue_line(self, orders, instance):
        """
            This method is used to prepare metafield export queue line for sale order.
        """
        mapping_records = self.env["shopify.metafield.config.ept"].get_shopify_metafield_configs(
            instance.id, ["sale.order"], 'update'
        )
        if not mapping_records:
            return True

        self._prepare_and_create_metafield_queues(
            instance, orders, mapping_records, "sale.order", "ORDER"
        )       

        queue_cron = self.env.ref("shopify_ept.process_shopify_export_metafields_queue")
        if not queue_cron.active:
            _logger.info("Active the Export metafields data process queue cron job")
            queue_cron.write(
                {'active': True, 'nextcall': datetime.now() + timedelta(seconds=120)})
        return True

    def prepared_export_metafield_payload(self,orders,instance,model_name,resource_type):
        """
            This method is used to prepare metafield payload for sale order, customer
        """
        mapping_records = self.env["shopify.metafield.config.ept"].get_shopify_metafield_configs(
            instance.id, [model_name], 'update'
        )
        if not mapping_records:
            return [], ""
        metafields_payload = []
        odoo_fields = mapping_records.filtered(lambda x: x.model_id.model == model_name)
        metafield_configs = self.env['shopify.metafield.config.ept']
        main_message=""
        for rec in orders:
            owner_id = self._get_metafield_owner_id(rec, 'ORDER', instance)
            for field in odoo_fields:
                message=""
                value = self._get_metafield_value(rec, field, resource_type)

                # if not value:
                #     field_type=field.odoo_field_id.ttype
                #     value = "" if field_type in ['char', 'text', 'html'] else 0 if field_type in ['float',
                #                                                                                   'integer',
                #                                                                                   'monetary'] else False
                #
                if value:
                    validation_message = self.env['shopify.export.metafields.queue.line.ept'].validate_shopify_metafield_value(value, field)
                    if validation_message:
                        message += (
                            f"Validation Error for metafield key '{field.key}' with value '{value}'."
                            f"\n{resource_type} : {owner_id}"
                            f"\nError Details: {validation_message}\n\n"
                        )

                        main_message =main_message + message
                    if not message:
                        # Handle richtext value type
                        if field.value_type == "rich_text_field":
                            # Shopify expects richtext as a string, possibly HTML
                            value = metafield_configs.text_to_shopify_richtext(value)
                        # f"gid://shopify/{'Product' if resource_type == 'PRODUCT' else 'ProductVariant'}/{getattr(rec, 'shopify_tmpl_id', getattr(rec, 'variant_id', None))}",
                        # "ownerId": ownerId,
                        metafields_payload.append({
                            "namespace": field.namespace,
                            "key": field.key,
                            "type": field.value_type,
                            "value": value,
                        })
        if main_message:
            error_log = (
                "System tried to validate metafield values for export but found errors.\n"
                "Action Items:\n"
                "- Check the metafield configuration for the keys used in this queue line under: Shopify → Configuration → Shopify Metafield Config.\n"
                "- Ensure the values provided meet the required validations (length, format, type, etc.) as per the configuration.\n"
                "- Correct the values in the queue line or update the configuration as needed.\n\n"
            )
            main_message=error_log+main_message

        return metafields_payload,main_message

    def prepare_export_customer_metafield_queue_line(self, customers, instance):
        """
            This method is used to prepare metafield export queue line for customers.
        """
        mapping_records = self.env["shopify.metafield.config.ept"].get_shopify_metafield_configs(
            instance.id, ["res.partner"], 'update'
        )
        if not mapping_records:
            return True

        self._prepare_and_create_metafield_queues(
            instance, customers, mapping_records, "res.partner", "CUSTOMER"
        )       

        queue_cron = self.env.ref("shopify_ept.process_shopify_export_metafields_queue")
        if not queue_cron.active:
            _logger.info("Active the Export metafields data process queue cron job")
            queue_cron.write(
                {'active': True, 'nextcall': datetime.now() + timedelta(seconds=120)})
        return True

    def _prepare_and_create_metafield_queues(self, instance, records, mapping_records, model_name, resource_type):
        """
            Prepares metafield payloads based on the provided records and mapping, and creates export metafield queues in batches.
            Args:
                instance (object): The Shopify instance for which metafields are being exported.
                records (list): List of Odoo records (products or variants) to export metafields for.
                mapping_records (recordset): Mapping records containing metafield configuration and Odoo field mapping.
                model_name (str): Name of the Odoo model ('product.product', 'product.template', etc.).
                resource_type (str): Type of Shopify resource ('PRODUCT' or 'VARIANT').
            Returns:
                None
        """
        metafields_payload = []
        odoo_fields = mapping_records.filtered(lambda x: x.model_id.model == model_name)
        metafield_configs = self.env['shopify.metafield.config.ept']
        for rec in records:
            ownerId = self._get_metafield_owner_id(rec, resource_type, instance)
            for field in odoo_fields:               
                value= self._get_metafield_value(rec, field, resource_type)

                if value:
                    # Handle richtext value type
                    if field.value_type == "rich_text_field":
                        # Shopify expects richtext as a string, possibly HTML
                        value = metafield_configs.text_to_shopify_richtext(value)
                    # f"gid://shopify/{'Product' if resource_type == 'PRODUCT' else 'ProductVariant'}/{getattr(rec, 'shopify_tmpl_id', getattr(rec, 'variant_id', None))}",    
                    metafields_payload.append({
                        "ownerId": ownerId,
                        "namespace": field.namespace,
                        "key": field.key,
                        "type": field.value_type,
                        "value": value,
                    })               
        if metafields_payload:
            self.create_export_metafields_queue(instance, metafields_payload, resource_type)      
    def _get_metafield_owner_id(self, record, resource_type, instance): 
        """
            Retrieves the Shopify GraphQL owner ID for a given Odoo record based on the resource type.
            Args:
                record (object): The Odoo record (product, variant, order, or customer) for which to get the owner ID.
                resource_type (str): Type of Shopify resource ('PRODUCT', 'PRODUCTVARIANT', 'ORDER', 'CUSTOMER').
                instance (object): The Shopify instance to which the record is related.
            Returns:
                str: The Shopify GraphQL owner ID in the format "gid://shopify/ResourceType/ID".
        """
        if resource_type == "PRODUCT":
            return f"gid://shopify/Product/{getattr(record, 'shopify_tmpl_id', False)}"
        elif resource_type == "PRODUCTVARIANT":
            return f"gid://shopify/ProductVariant/{getattr(record, 'variant_id', False)}"
        elif resource_type == "ORDER":
            return f"gid://shopify/Order/{getattr(record, 'shopify_order_id', False)}"
        elif resource_type == "CUSTOMER":
            shopify_customer = self.env["shopify.res.partner.ept"].search([
                ('partner_id', '=', record.id),
                ('shopify_instance_id', '=', instance.id)
            ], limit=1)
            return f"gid://shopify/Customer/{getattr(shopify_customer, 'shopify_customer_id', False)}"
        return ""
    
    def _get_metafield_value(self, record, field, resource_type):
        """
            Retrieves the value for a metafield based on the Odoo record, field configuration, and resource type.
            Args:
                record (object): The Odoo record (product or variant) for which to get the metafield value.
                field (object): The metafield configuration record containing the Odoo field mapping and value type.
                resource_type (str): Type of Shopify resource ('PRODUCT' or 'VARIANT').
            Returns:
                The value to be exported for the metafield, formatted according to the value type.
        """
        
        # rec.product_tmpl_id[field.odoo_field_id.name] if resource_type == 'PRODUCT' else rec.product_id[field.odoo_field_id.name]
        if resource_type == "PRODUCT" and field.odoo_field_id:
            return getattr(record.product_tmpl_id, field.odoo_field_id.name, "")
        elif resource_type == "PRODUCTVARIANT":
            return getattr(record.product_id, field.odoo_field_id.name, "")
        elif resource_type == "ORDER" and field.odoo_field_id:
            return getattr(record, field.odoo_field_id.name, "")
        elif resource_type == "CUSTOMER" and field.odoo_field_id:
            return getattr(record, field.odoo_field_id.name, "")
        return ""

    @api.model
    def retrieve_dashboard(self, *args, **kwargs):
        """
            :param args:
            :param kwargs:
            :return:
        """
        dashboard = self.env['queue.line.dashboard']
        return dashboard.get_data(table='shopify.export.metafields.queue.line.ept')
