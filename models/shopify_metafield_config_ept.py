from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import re
import json
from .. import shopify_graphql

import json
import logging
import time
from builtins import int
import pytz
from odoo import models, fields, api
from .. import shopify
from ..shopify.pyactiveresource.connection import ClientError

utc = pytz.utc
_logger = logging.getLogger("Shopify Metafield Configurations")



class ShopifyMetafieldConfig(models.Model):
    _name = "shopify.metafield.config.ept"
    _description = "Shopify Metafield Configuration"
    _rec_name = "name"
    _order = "instance_id, namespace, key"

    # ---------------------------------------------------------
    # Basic Information
    # ---------------------------------------------------------

    name = fields.Char(
        string="Name",
        required=True
    )

    instance_id = fields.Many2one(
        "shopify.instance.ept",
        string="Shopify Instance",
        required=True,
        ondelete="cascade"
    )

    namespace = fields.Char(
        string="Namespace",
        required=True,
        help="3–255 characters. Only alphanumeric, hyphen (-) and underscore (_)."
    )

    key = fields.Char(
        string="Key",
        required=True,
        help="2–64 characters. Only alphanumeric, hyphen (-) and underscore (_)."
    )

    value_type = fields.Char(
        string="Shopify Value Type",
        help="Example: single_line_text_field, number_integer, boolean"
    )

    # ---------------------------------------------------------
    # Owner / Resource Type
    # ---------------------------------------------------------

    model_id = fields.Many2one(
        "ir.model",
        string="Odoo Model (Owner Type)",
        help="Example: product.product, product.template, sale.order, res.partner"
    )

    active = fields.Boolean(
        default=True,
        string="Active"
    )

    # ---------------------------------------------------------
    # Odoo Field Mapping
    # ---------------------------------------------------------

    odoo_field_id = fields.Many2one(
        "ir.model.fields",
        string="Odoo Field",       
       
        help="Stored field only. Related/computed without store not allowed."
    )

    sync_direction = fields.Selection(
        [
            ('import', 'Import Only'),
            ('update', 'Export Only'),
            ('both', 'Import & Export')
        ],
        string="Sync Direction",    
        help="Select the synchronization direction for this metafield:\n\n"
             "Import Only: Import new and existing data from Shopify to Odoo.\n"
             "Export Only: Export new and existing data from Odoo to Shopify.\n"
             "Import & Export: Synchronize new and existing data in both directions between Shopify and Odoo."
    )

    # ---------------------------------------------------------
    # Advanced Configuration
    # ---------------------------------------------------------

    validations = fields.Json(
        string="Validation Rules",
        help="JSON validation rules from Shopify definition"
    )

    meta_field_id = fields.Char(
        string="Shopify Metafield Definition ID",
        help='Example: gid://shopify/MetafieldDefinition/123456'
    )

    description = fields.Char(string="Description")

    allowed_odoo_types = fields.Json(
        compute="_compute_allowed_odoo_types",
        store=False,
    )
    
    def _compute_allowed_odoo_types(self):

        SHOPIFY_TO_ODOO_MAP = {

            # TEXT
            "single_line_text_field": ["char"],
            "multi_line_text_field": ["text"],
            "rich_text_field": ["text"],

            # NUMBERS
            "number_integer": ["integer"],
            "number_decimal": ["float", "monetary"],

            # BOOLEAN
            "boolean": ["boolean"],

            # DATE
            "date": ["date"],
            "date_time": ["datetime"],

            # URL
            "url": ["char"],

        }

        for rec in self:
            types = SHOPIFY_TO_ODOO_MAP.get(rec.value_type or "", [])
            rec.allowed_odoo_types =types # ",".join(types)

    # ---------------------------------------------------------
    # SQL Constraints
    # ---------------------------------------------------------    
    _unique_namespace_key_instance = models.Constraint(
        'unique(instance_id, namespace, key, model_id)',        
        'A metafield configuration with the same Namespace, Key, Instance and Owner Type already exists.')
    

    @api.constrains('odoo_field_id', 'instance_id', 'model_id')
    def _check_odoo_field_unique(self):
        for rec in self:
            if rec.odoo_field_id:
                domain = [
                    ('odoo_field_id', '=', rec.odoo_field_id.id),
                    ('instance_id', '=', rec.instance_id.id),
                    ('model_id', '=', rec.model_id.id),
                    ('id', '!=', rec.id)
                ]
                count = self.search_count(domain)
                if count > 0:
                    raise ValidationError(
                        _("Odoo Field '%s' is already mapped in another metafield configuration for this instance and model.")
                        % rec.odoo_field_id.name
                    )    

    def import_shopify_metafield_configs(self, instance, owner_type=None, metafield_key=None):
        """ This method is used to import the Shopify metafield configurations from Shopify to Odoo.           
            Task_id: 47831 - Shopify Metafield Mapping Configuration
        """

        result = False
        create_ids=[]
        try:
            # if instance.use_graphql_api:
            client = instance.get_graphql_client()
            definitions=[]
            if not metafield_key:
                definitions = shopify_graphql.MetafieldQueryHelper(client).fetch_metafield_definitions(owner_type)
                result, create_ids = self._sync_metafield_definitions(definitions, instance, owner_type, metafield_key)         

            else:
                for key in metafield_key.split(","):
                    key = key.strip()
                    # Case 1: "custom.description1" → namespace="custom", key="description1"
                    # Case 2: "description1" → namespace="custom" (default), key="description1"
                    if "." in key:
                        namespace, key_name = key.split(".", 1)
                    else:
                        namespace, key_name = "custom", key

                    definition = shopify_graphql.MetafieldQueryHelper(client).get_definition_by_key(owner_type, key_name.strip(),namespace)
                    if definition:
                        definitions.append(definition[-1])
                result, create_ids = self._sync_metafield_definitions(definitions, instance, owner_type, metafield_key)             
        except Exception as error:
                _logger.error("Error importing Shopify metafield configs for instance %s: %s", instance.id, error)
        return result, create_ids

    def _sync_metafield_definitions(self, definitions, instance, owner_type, metafield_key):
        """ 
            This method is used to sync the Shopify metafield definitions with Odoo configurations.
            It will create/update the configurations based on the definitions and deactivate the ones which are removed from Shopify.
        """
        message=""
        if not definitions:
            message = _("No metafield definitions found to import for owner type '%s' and key '%s'.") % (owner_type or "N/A", metafield_key or _("ALL"))

        # Convert Shopify list to dict for quick lookup
        shopify_keys = {
            (d["namespace"], d["key"]): d
            for d in definitions
        }              

        existing_mappings = self.sudo().with_context(active_test=False).search([
            ("instance_id", "=", instance.id),
            ("model_id", "=", self._get_model_by_owner_type(owner_type)),
        ])

        existing_keys = {   
            (m.namespace, m.key): m
            for m in existing_mappings
        }

        # -------------------------------
        # 1️⃣ Create / Update
        # -------------------------------
        create_ids=[]
        for shopify_def in definitions:
            namespace = shopify_def.get("namespace")
            key = shopify_def.get("key")
            key_tuple = (namespace, key)
            mapping = existing_keys.get(key_tuple)

            values = self._prepare_config_vals(shopify_def,instance)

            if mapping:
                mapping.write(values)
            else:
                res=self.create(values)
                create_ids.append(res.id)

        # -------------------------------
        # 2️⃣ Deactivate Removed Ones
        # -------------------------------
        if not metafield_key:
            removed_keys = set(existing_keys.keys()) - set(shopify_keys.keys())

            for key_tuple in removed_keys:
                existing_keys[key_tuple].write({
                    "active": False
                })     

        return message, create_ids

    def _prepare_config_vals(self, definition,instance):
        """
            Prepare vals dictionary for shopify.metafield.config.ept
        """

        return {
            "name": definition.get("name"),
            "instance_id": instance.id,
            "namespace": definition.get("namespace"),
            "key": definition.get("key"),
            "value_type": definition.get("type", {}).get("name"),
            "model_id": self._get_model_by_owner_type(owner_type=definition.get("ownerType")),
            "description": definition.get("description"),
            "validations": definition.get("validations") or False,
            "meta_field_id": int(definition.get("id").split("/")[-1]) if definition.get("id") else False,
            "active": True if definition.get("namespace") =="custom" else False, # Auto-activate custom namespace, others manual review        
        }
    
    def _get_model_by_owner_type(self, owner_type):
        """ Helper method to map Shopify owner type to Odoo model ID """

        mapping = {
            'PRODUCT': self.env.ref('product.model_product_template').id,
            'PRODUCTVARIANT': self.env.ref('product.model_product_product').id,
            'CUSTOMER': self.env.ref('base.model_res_partner').id,
            'ORDER': self.env.ref('sale.model_sale_order').id,
        }
        return mapping.get(owner_type)

    def get_shopify_metafield_configs(self, instance_id, model_name, sync_direction):
        """ This method is used to get the Shopify metafield configurations for the given instance and owner type.           
            Task_id: 47831 - Shopify Metafield Mapping Configuration
        """
        domain = [("instance_id", "=", instance_id),                 
                  ("active", "=", True),
                  ("namespace", "!=", False),
                  ("key", "!=", False),
                  ("odoo_field_id", "!=", False),]
        if sync_direction:
            domain.append(("sync_direction", "in", [sync_direction, "both"]))
        if model_name:
            domain.append(("model_id.model", "in", model_name))  
            
        configs = self.search(domain)
        return configs

    def get_resource_metafield_by_owner(self, instance, owner_id):
        """ This method is used to fetch metafield values for a given Shopify resource (owner) using GraphQL API.           
            Task_id: 47831 - Shopify Metafield Mapping Configuration
        """
        result = {}
        try:
            client = instance.get_graphql_client()
            metafields = None
            retry_count = 0
            max_retries = 3
            while retry_count < max_retries:
                try:
                    metafields = shopify_graphql.MetafieldQueryHelper(client).get_metafields_by_owner(owner_id)
                    break
                except Exception as error:
                    # Handle Shopify GraphQL rate limit error
                    error_msg = str(error)
                    if "Throttled" in error_msg or "exceeded" in error_msg or "rate limit" in error_msg:
                        wait_time = 2 ** retry_count
                        _logger.warning("Shopify GraphQL rate limit exceeded. Retrying in %s seconds...", wait_time)
                        time.sleep(wait_time)
                        retry_count += 1
                        continue
                    else:
                        raise
            if metafields:
                result.update({'metafields': metafields})
        except Exception as error:
            message = error.message if hasattr(error, 'message') else str(error)
            model_name = self._name
            common_log_line_obj = self.env["common.log.lines.ept"]
            common_log_line_obj.create_common_log_line_ept(
            shopify_instance_id=instance.id,
            module="shopify_ept",
            message=message,
            model_name=model_name,
            log_type="Error",
            )
        return result

    def _get_owner_type(self):
        """ Helper method to get Shopify owner type from Odoo model ID """
        model_mapping = {
            self.env.ref('product.model_product_template').id: 'PRODUCT',
            self.env.ref('product.model_product_product').id: 'PRODUCTVARIANT',
            self.env.ref('sale.model_sale_order').id: 'ORDER',
            self.env.ref('base.model_res_partner').id: 'CUSTOMER',
        }
        return model_mapping.get(self.model_id.id, 'PRODUCT')
    
    def _convert_shopify_to_odoo(self, shopify_type, value, field):
        """ Helper method to convert Shopify metafield value to appropriate Odoo field type based on the configuration. """

        # if not value:
        #     return False

        field_type = field.ttype

        if shopify_type in ["single_line_text_field", "multi_line_text_field"]:
            return value

        if shopify_type == "rich_text_field":
            return self.shopify_richtext_to_text(value)

        if shopify_type == "number_integer":
            return int(value)

        if shopify_type == "number_decimal":
            return float(value)

        if shopify_type == "boolean":
            return value == "true"

        if shopify_type == "date":
            return fields.Date.to_date(value)

        if shopify_type == "date_time":
            return fields.Datetime.to_datetime(value)

        return value
    
    def _convert_odoo_to_shopify(self, value, shopify_type):

        if value is False:
            return ""

        if shopify_type in [
            "single_line_text_field",
            "multi_line_text_field"
        ]:
            return str(value)

        if shopify_type == "number_integer":
            return str(int(value))

        if shopify_type == "number_decimal":
            return str(float(value))

        if shopify_type == "boolean":
            return "true" if value else "false"

        if shopify_type == "date":
            return value.strftime("%Y-%m-%d")

        if shopify_type == "date_time":
            return value.strftime("%Y-%m-%dT%H:%M:%S")

        return str(value)

    def text_to_shopify_richtext(self, text):
        """Convert plain text to Shopify rich text JSON"""
        data = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [
                        {
                            "type": "text",
                            "value": text
                        }
                    ]
                }
            ]
        }
        return json.dumps(data)
    
    

    def shopify_richtext_to_text(self, value):
        """Extract plain text from Shopify rich text JSON"""        
        try:
            data = json.loads(value)
        except Exception:
            return value
        texts = []
        def _extract(node):
            if isinstance(node, dict):
                # collect text value
                if node.get("type") == "text" and node.get("value"):
                    texts.append(node.get("value"))
                # process children
                for child in node.get("children", []):
                    _extract(child)

            elif isinstance(node, list):
                for item in node:
                    _extract(item)

        _extract(data)

        return "\n".join(texts)
