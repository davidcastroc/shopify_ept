# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
import re
import json
import logging
import time
import requests
import base64
import mimetypes
import collections
from datetime import datetime, timedelta, timezone

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from .. import shopify
from ..shopify.pyactiveresource.connection import ClientError
from ..shopify.pyactiveresource.connection import ResourceNotFound
from .. import shopify_graphql
from ..shopify_graphql.queries.inventory import InventoryQueryHelper
from ..shopify_graphql.queries.inventory_item import InventoryItemHelper

_logger = logging.getLogger("Shopify Product")


class ShopifyProductProductEpt(models.Model):
    _name = "shopify.product.product.ept"
    _description = "Shopify Product Product"
    _order = "sequence"

    sequence = fields.Integer("Position", default=1)
    name = fields.Char("Title")
    shopify_instance_id = fields.Many2one("shopify.instance.ept", "Instance", required=True)
    default_code = fields.Char()
    product_id = fields.Many2one("product.product", required=True)
    shopify_template_id = fields.Many2one("shopify.product.template.ept", required=True,
                                          ondelete="cascade")
    exported_in_shopify = fields.Boolean(default=False)
    variant_id = fields.Char()
    fix_stock_type = fields.Selection([("fix", "Fix"), ("percentage", "Percentage")])
    fix_stock_value = fields.Float()
    created_at = fields.Datetime()
    updated_at = fields.Datetime()
    inventory_item_id = fields.Char()
    check_product_stock = fields.Selection([("continue", "Allow"), ("deny", "Denied")],
                                           string="Sale out of stock products?",
                                           default="deny",
                                           help="If true than customers are allowed to place an order for the product"
                                                "variant when it is out of stock.")
    inventory_management = fields.Selection([("shopify", "Shopify tracks this product Inventory"),
                                             ("Dont track Inventory", "Don't track Inventory")],
                                            default="shopify",
                                            help="If you select 'Shopify tracks this product Inventory' than shopify"
                                                 "tracks this product inventory.if select 'Don't track Inventory' then"
                                                 "after we can not update product stock from odoo")
    active = fields.Boolean(default=True)
    shopify_image_ids = fields.One2many("shopify.product.image.ept", "shopify_variant_id")
    taxable = fields.Boolean(default=True)
    last_stock_update_date = fields.Datetime(readonly=True, help="It is used in export stock process.")
    fixed_stock_export = fields.Boolean()
    fixed_stock_export_value = fields.Float(digits=0)
    shopify_hsn_code = fields.Char(string="HSN Code")

    @api.constrains('shopify_hsn_code')
    def _check_hsn_code_format(self):
        for record in self:
            if record.shopify_hsn_code:
                # 1. Check if it is purely numeric
                if not record.shopify_hsn_code.isdigit():
                    raise ValidationError(_("HSN Code must contain only numbers."))

                # 2. Check the length (between 6 and 13 digits)
                length = len(record.shopify_hsn_code)
                if not (6 <= length <= 13):
                    raise ValidationError(_("HSN Code must be between 6 and 13 digits long."))

    @api.onchange('fixed_stock_export_value')
    def _onchange_fixed_export_stock_value(self):
        if self.fixed_stock_export_value < 0:
            raise UserError(_('There is no negative value allowed for the fixed export stock value.'))

    def toggle_active(self):
        """
        This method is used to archiving related shopify product template if there is only
        one active shopify product product(in shopify layer).
        It used while archive the products from shopify product product and also it archive the related shopify
        template.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 09/12/2019.
        :Task id: 158502
        """
        with_one_active = self.filtered(lambda x: len(x.shopify_template_id.shopify_product_ids) == 1)
        for product in with_one_active:
            product.shopify_template_id.toggle_active()
        return super(ShopifyProductProductEpt, self - with_one_active).toggle_active()

    def shopify_create_variant_product(self, result, instance, price):
        """
        This method called child to search the attribute in Odoo and based on attribute it's created a product
        template and variant.
        :param result: Response of product.
        :price: Product price
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 09/10/2019.
        """
        product_template_obj = self.env["product.template"]

        template_title = result.get("title", "")

        attrib_line_vals = self.shopify_prepare_attribute_vals(result)
        if attrib_line_vals:
            template_vals = {"name": template_title,
                             "type": "consu",
                             "attribute_line_ids": attrib_line_vals,
                             "is_storable": True}

            if self.env["ir.config_parameter"].sudo().get_param("shopify_ept.set_sales_description"):
                template_vals.update({"description_sale": result.get("body_html")})

            product_template = product_template_obj.create(template_vals)

            odoo_product = self.shopify_set_variant_sku(result, product_template, instance)

            if odoo_product:
                return product_template
        return False

    def shopify_prepare_attribute_vals(self, result):
        """This method use to prepare a attribute values list.
           :param result: Response of product.
           @return: attrib_line_vals(list of attribute vals)
           @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 22/10/2019.
        """
        product_attribute_obj = self.env["product.attribute"]
        product_attribute_value_obj = self.env["product.attribute.value"]
        attrib_line_vals = []
        for attrib in result.get("options"):
            attrib_name = attrib.get("name")
            attrib_values = attrib.get("values")
            attribute = product_attribute_obj.get_attribute(attrib_name, auto_create=True)[0]
            attr_val_ids = []

            for attrib_value in attrib_values:
                attribute_value = product_attribute_value_obj.get_attribute_values(attrib_value, attribute.id,
                                                                                   auto_create=True)
                if attribute_value:
                    attribute_value = attribute_value[0]
                    attr_val_ids.append(attribute_value.id)

            if attr_val_ids:
                attribute_line_ids_data = [0, False,
                                           {"attribute_id": attribute.id, "value_ids": [[6, False, attr_val_ids]]}]
                attrib_line_vals.append(attribute_line_ids_data)
        return attrib_line_vals

    def shopify_set_variant_sku(self, result, product_template, instance):
        """This method set the variant SKU based on the attribute and attribute value.
            @param : self, result, product_template, instance
            @return: True
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 10/10/2019.
        """
        for variation in result.get("variants"):
            variation_attributes = self.prepare_vals_for_variation_attributes(result, variation)

            template_attribute_value_ids = self.prepare_template_attribute_values_ids(variation_attributes,
                                                                                      product_template)

            odoo_product = self.search_odoo_product_and_set_sku_barcode(template_attribute_value_ids, variation,
                                                                        product_template)

        return odoo_product

    def prepare_vals_for_variation_attributes(self, result, variation):
        """ This method is used to prepare a val for variation attribute base on the receive response of the product.
            :param result: Response of product
            :param variation: variation as response as received in the product response.
            @return: variation_attributes
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id: 167537
        """
        variation_attributes = []
        option_name = []
        for options in result.get("options"):
            attrib_name = options.get("name")
            attrib_name and option_name.append(attrib_name)

        option1 = variation.get("option1", False)
        option2 = variation.get("option2", False)
        option3 = variation.get("option3", False)
        if option1 and (option_name and option_name[0]):
            variation_attributes.append({"name": option_name[0], "option": option1})
        if option2 and (option_name and option_name[1]):
            variation_attributes.append({"name": option_name[1], "option": option2})
        if option3 and (option_name and option_name[2]):
            variation_attributes.append({"name": option_name[2], "option": option3})

        return variation_attributes

    def prepare_template_attribute_values_ids(self, variation_attributes, product_template):
        """ This method is used to prepare a template attribute values ids list.
            @return: template_attribute_value_ids
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20 October 2020 .
            Task_id: 167537
        """
        template_attribute_value_ids = []
        product_attribute_obj = self.env["product.attribute"]
        product_attribute_value_obj = self.env["product.attribute.value"]
        product_template_attribute_value_obj = self.env["product.template.attribute.value"]
        odoo_attribute_lines = product_template.attribute_line_ids.filtered(
            lambda x: x.attribute_id.create_variant == "always")
        for variation_attribute in variation_attributes:
            attribute_val = variation_attribute.get("option")
            attribute_name = variation_attribute.get("name")
            product_attribute = odoo_attribute_lines.filtered(
                lambda x: x.display_name == variation_attribute.get("name")).attribute_id
            if not product_attribute:
                product_attribute = product_attribute_obj.search([("name", "=ilike", attribute_name)], limit=1)
            if product_attribute:
                product_attribute_value = product_attribute_value_obj.get_attribute_values(attribute_val,
                                                                                           product_attribute.id)
            if product_attribute_value:
                product_attribute_value = product_attribute_value[0]
                template_attribute_value_id = product_template_attribute_value_obj.search(
                    [("product_attribute_value_id", "=", product_attribute_value.id),
                     ("attribute_id", "=", product_attribute.id), ("product_tmpl_id", "=", product_template.id)],
                    limit=1)
                template_attribute_value_id and template_attribute_value_ids.append(template_attribute_value_id.id)

        return template_attribute_value_ids

    def search_odoo_product_and_set_sku_barcode(self, template_attribute_value_ids, variation, product_template):
        """ This method is used to search odoo product base on a prepared domain and set SKU and barcode on that
            product.
            :param template_attribute_value_ids: Record of product template attribute value ids.
            :param variation: Response of product variant which received from shopify store.
            :param product_template: Record of Odoo product template.
            @return: odoo_product
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 21 October 2020 .
            Task_id: 167537
        """
        odoo_product_obj = self.env["product.product"]
        sku = variation.get("sku")
        barcode = variation.get("barcode") or False
        if barcode and barcode.__eq__("false"):
            barcode = False
        odoo_product = False

        domain = []
        for template_attribute_value in template_attribute_value_ids:
            tpl = ("product_template_attribute_value_ids", "=", template_attribute_value)
            domain.append(tpl)

        domain and domain.append(("product_tmpl_id", "=", product_template.id))
        if domain:
            odoo_product = odoo_product_obj.search(domain)
        if odoo_product and sku:
            odoo_product.write({"default_code": sku})
        if barcode and odoo_product:
            odoo_product.write({"barcode": barcode})

        return odoo_product

    def prepare_shopify_product_for_update_export(self, new_product, template, instance, is_set_basic_detail,
                                                  is_publish, is_set_price):
        """
        This method will be used for both Export and Updating product in Shopify.
        @author: Maulik Barad on Date 21-Sep-2020.
        """
        if is_set_basic_detail or is_publish:
            self.shopify_set_template_value_in_shopify_obj(new_product, template, is_publish, is_set_basic_detail)
        if is_set_basic_detail or is_set_price:
            variants = []
            for variant in template.shopify_product_ids:
                variant_vals = self.shopify_prepare_variant_vals(instance, variant, is_set_price,
                                                                 is_set_basic_detail)
                variants.append(variant_vals)
            new_product.variants = variants
        if is_set_basic_detail:
            self.prepare_export_update_product_attribute_vals(template, new_product)
        return True

    def shopify_export_products(self, instance, is_set_basic_detail, is_set_price, is_set_images, is_publish,
                                templates):
        """
        This method used to Export the shopify product from Odoo to Shopify.
        :param instance:Record of the instance
        :param is_set_basic_detail: It exports the product basic details if it is True.
        :param is_set_price: If true it is the export price with the product else not the export price with the product.
        :param is_set_images: If true it is the export images with the product else not the export images with the
        product.
        :param is_publish: If true it publishes the product in the Shopify store.
        @author: Nilesh Parmar @Emipro Technologies Pvt. Ltd on date 19/11/2019.
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        model = "shopify.product.product.ept"
        if instance.use_graphql_api:
            client = instance.get_graphql_client()
            product_helper = shopify_graphql.ProductQueryHelper(client)
            pub_id_dict = product_helper._get_online_store_publication_id()
            translation_pairs = []
            for template in templates:
                create_product_input = product_helper.build_product_input_from_template(template, instance,
                                                                                        is_set_price,
                                                                                        is_set_basic_detail)
                resp = product_helper.set_product(create_product_input)
                errors = resp.get("data", {}).get("productSet", {}).get("userErrors") or resp.get("errors")
                if errors:
                    for err in errors:
                        self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                                    module="shopify_ept",
                                                                                    message=f"ProductSet Error: {err}",
                                                                                    model_name="shopify.product.template.ept")
                    continue
                product_data = resp.get("data", {}).get("productSet", {}).get("product")
                product_id = product_data.get("id")
                variant_nodes = product_data.get("variants", {}).get("nodes", [])
                result_dict = product_helper.convert_graphql_product_to_rest_dict(product_data)
                product_helper._publish_graphql(pub_id_dict, template, is_publish, product_global_id=product_id)
                self.update_products_details_shopify_third_layer(result_dict, template, is_publish)
                self.update_hsn_code_for_variants(instance, [template])
                if is_set_images:
                    product_helper.export_product_images_gql(template, product_id, variant_nodes)
                    updated_at = datetime.now()
                    template.write({"updated_at": updated_at})
                    template.shopify_product_ids.write({"updated_at": updated_at})
                    self.env.cr.commit()
                if instance.is_translations_needed():
                    translation_pairs.append((template, product_id))

            if translation_pairs:
                self.env["shopify.product.template.ept"].batch_export_product_translations(
                    instance, translation_pairs)
        else:
            instance.connect_in_shopify()
            for template in templates:
                new_product = shopify.Product()

                self.prepare_shopify_product_for_update_export(new_product, template, instance, is_set_basic_detail,
                                                               is_publish, is_set_price)

                result = new_product.save()

                if not result:
                    message = "Product %s not exported in Shopify Store." % template.name
                    common_log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, message=message,
                                                                   module="shopify_ept",
                                                                   model_name=model)
                if result:
                    self.update_products_details_shopify_third_layer(new_product, template, is_publish)
                    self.update_hsn_code_for_variants(instance, [template])
                if new_product and is_set_images:
                    self.export_product_images(instance, shopify_template=template)
                self.env.cr.commit()

        return True

    def update_hsn_code_for_variants(self, instance, templates):
        """
        Common method to update HSN code (harmonized_system_code) for all variants in templates.
        Handles both REST and GraphQL flows based on instance.use_graphql_api.
        """
        if instance.use_graphql_api:
            client = instance.get_graphql_client()
            inventory_item_helper = InventoryItemHelper(client)
            for template in templates:
                for variant in template.shopify_product_ids:
                    if variant.shopify_hsn_code and variant.inventory_item_id:
                        try:
                            inventory_item_gid = f"gid://shopify/InventoryItem/{variant.inventory_item_id}"
                            resp = inventory_item_helper.update_inventory_item(inventory_item_gid, variant.shopify_hsn_code)
                            user_errors = resp.get("data", {}).get("inventoryItemUpdate", {}).get("userErrors", [])
                            if user_errors:
                                for err in user_errors:
                                    _logger.warning(f"InventoryItemUpdate Error: {err}")
                        except Exception as e:
                            _logger.error(
                                f"Exception updating HSN code for inventory item {variant.inventory_item_id} via GraphQL: {e}")
        else:
            for template in templates:
                for variant in template.shopify_product_ids:
                    if variant.shopify_hsn_code and variant.inventory_item_id:
                        try:
                            inventory_item = shopify.InventoryItem.find(variant.inventory_item_id)
                            inventory_item.harmonized_system_code = variant.shopify_hsn_code
                            result = inventory_item.save()
                            if not result:
                                _logger.warning(
                                    f"Failed to update HSN code for inventory item {variant.inventory_item_id} via shopify.InventoryItem.save()")
                        except Exception as e:
                            _logger.error(
                                f"Exception updating HSN code for inventory item {variant.inventory_item_id} via REST: {e}")

    def prepare_export_update_product_attribute_vals(self, template, new_product):
        """This method is used to set product attribute vals while export/update products from Odoo to Shopify store.
        @change : pass lang_id on context by Nilam Kubavat for task id : 190111 at 19/05/2022
        """
        if template.product_tmpl_id.attribute_line_ids:
            attribute_list = []
            attribute_position = 1
            product_attribute_line_obj = self.env["product.template.attribute.line"]
            instance = template.shopify_instance_id
            product_attribute_lines = product_attribute_line_obj.search(
                [("id", "in",
                  template.with_context(lang=instance.shopify_lang_id.code).product_tmpl_id.attribute_line_ids.ids)],
                order="attribute_id")

            for attribute_line in product_attribute_lines.filtered(lambda x: x.attribute_id.create_variant == "always"):
                info = {}
                attribute = attribute_line.attribute_id
                value_names = []
                for value in attribute_line.value_ids:
                    value_names.append(value.with_context(lang=instance.shopify_lang_id.code).name)

                info.update(
                    {"name": attribute.with_context(lang=instance.shopify_lang_id.code).name, "values": value_names,
                     "position": attribute_position})
                attribute_list.append(info)
                attribute_position = attribute_position + 1
            new_product.options = attribute_list
        return True

    def update_products_in_shopify(self, instance, templates, is_set_price, is_set_images, is_publish,
                                   is_set_basic_detail):
        """
        This method is used to Update product in shopify store.
        :param instance: shopify instance id.
        :param is_set_price: if true then update price in shopify store.
        :param is_set_images: if true then update image in shopify store.
                                variant_media_map = collections.defaultdict(list)  # {variant_sku: [media_id, ...]}
                                general_media_ids = []  # images not linked to any variant
        :param is_set_basic_detail: if true then update product basic detail.
        :param templates: Record of shopify templates.
        @author: Nilesh Parmar @Emipro Technologies Pvt. Ltd on date 15/11/2019.
        """
        model = "shopify.product.product.ept"
        instance.connect_in_shopify()

        for template in templates:
            new_product = self.with_context(unlink_not_found_template=True).request_for_shopify_template(
                template, model, instance)
            if not new_product:
                continue

            self.prepare_shopify_product_for_update_export(new_product, template, instance, is_set_basic_detail,
                                                           is_publish, is_set_price)

            result = new_product.save()

            if result:
                self.update_products_details_shopify_third_layer(new_product, template, is_publish)
                self.update_hsn_code_for_variants(instance, [template])
            if is_set_images:
                self.update_product_images(shopify_template=template)

            updated_at = datetime.now()
            template.write({"updated_at": updated_at})
            template.shopify_product_ids.write({"updated_at": updated_at})

        return True

    def check_available_products_in_shopify(self, instance):
        """
        This method is used to check product is available in shopify store.
        @param templates: Record of shopify templates.
        @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 01/06/2022.
        """
        results = shopify.Product().find(status='active, draft', limit=250)
        data_dict = results
        if len(results) >= 250:
            catch = ""
            while results:
                page_info = ""
                link = (results.metadata.get('headers', {}).get('Link') or
                        results.metadata.get('headers', {}).get('link') or
                        shopify.ShopifyResource.connection.response.headers.get('Link') or
                        shopify.ShopifyResource.connection.response.headers.get('link'))

                if not link or not isinstance(link, str):
                    continue
                for page_link in link.split(","):
                    if page_link.find("next") > 0:
                        page_info = page_link.split(";")[0].strip("<>").split("page_info=")[1]
                        try:
                            results = shopify.Product().find(page_info=page_info, limit=250)
                            data_dict += results
                        except ClientError as error:
                            if hasattr(error,
                                       "response") and error.response.code == 429 and error.response.msg == "Too Many Requests":
                                time.sleep(int(float(error.response.headers.get('Retry-After', 5))))
                                results = shopify.Product().find(page_info=page_info, limit=250)
                                data_dict += results
                        except Exception as error:
                            continue
                if catch == page_info:
                    break

        available_product_ids = [str(result.id) for result in data_dict]
        shopify_template_ids = self.env['shopify.product.template.ept'].search(
            [('exported_in_shopify', '=', True), ('shopify_instance_id', '=', instance.id)])
        layer_templates = shopify_template_ids.filtered(
            lambda template: template.shopify_tmpl_id not in available_product_ids)
        shopify_templates = shopify_template_ids.filtered(lambda template: template.id not in layer_templates.ids)
        if layer_templates:
            layer_templates.unlink()
        return shopify_templates

    def request_for_shopify_template(self, template, model, instance):
        """ This method is used to request for the shopify product from Odoo to Shopify store.
            :param template: Record of shopify template.
            @return: new_product
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 21 October 2020 .
            Task_id: 167537
        """
        if not template.shopify_tmpl_id:
            return False
        try:
            new_product = shopify.Product().find(template.shopify_tmpl_id)
        except ClientError as error:
            if hasattr(error, "response") and error.response.code == 429 and error.response.msg == "Too Many Requests":
                time.sleep(int(float(error.response.headers.get('Retry-After', 5))))
                new_product = shopify.Product().find(template.shopify_tmpl_id)
            else:
                error_code = error.response.code if hasattr(error, "response") else "N/A"
                error_msg = error.response.msg if hasattr(error, "response") else str(error)
                message = ("System tried to update product details from Odoo to the Shopify store for product template ID: %s but could not update.\n"
                              "Error: [%s] %s\n"
                              "Action Items:\n"
                              "- Ensure the product exists on the Shopify store.\n"
                              "- If the product has been deleted from Shopify, archive it in the Shopify product layer in Odoo.") % (
                              template.shopify_tmpl_id, error_code, error_msg)
                _logger.error("ClientError while fetching Shopify product %s: [%s] %s",
                              template.shopify_tmpl_id, error_code, error_msg)
                self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                            module="shopify_ept",
                                                                            message=message,
                                                                            model_name=model)
                if error_code == 404 and self.env.context.get('unlink_not_found_template'):
                    _logger.info("Product template ID %s not found in Shopify (404). Unlinking from Odoo.",
                                 template.shopify_tmpl_id)
                    template.unlink()
                return False
        except Exception as error:
            message = ("System tried to update product details from Odoo to the Shopify store for product template ID: %s but can not updated.\n"
                          "Action Items:\n"
                          "- Ensure the product exists on the Shopify store.\n"
                          "- If the product has been deleted from Shopify, archive it in the Shopify product layer in Odoo.") % (
                          template.shopify_tmpl_id)
            self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                        module="shopify_ept",
                                                                        message=message,
                                                                        model_name=model)
            return False
        return new_product

    def shopify_set_template_value_in_shopify_obj(self, new_product, template, is_publish, is_set_basic_detail):
        """
        This method is used to set the shopify product template values.
        :param new_product: shopify product object
        :param template: shopify product template product template
        :param is_publish: if true then publish product in shop[ify store
        :param is_set_basic_detail: if true then set the basic detail in shopify product
        @author: Nilesh Parmar @Emipro Technologies Pvt. Ltd on date 15/11/2019.
        @change : pass lang_id on context by Nilam Kubavat for task id : 190111 at 19/05/2022
        """
        published_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if is_publish == "unpublish_product":
            new_product.published_at = None
            new_product.published_scope = "null"
        elif is_publish == "publish_product_global":
            new_product.published_scope = "global"
            new_product.published_at = published_at
        else:
            new_product.published_scope = "web"
            new_product.published_at = published_at

        instance = template.shopify_instance_id
        if is_set_basic_detail:
            if template.description:
                # new_product.body_html = template.description
                new_product.body_html = template.with_context(lang=instance.shopify_lang_id.code).description
            if template.product_tmpl_id.seller_ids:
                new_product.vendor = template.product_tmpl_id.seller_ids[0].display_name
            new_product.product_type = template.shopify_product_category.name
            new_product.tags = [tag.name for tag in template.tag_ids]
            if template.template_suffix:
                new_product.template_suffix = template.template_suffix
            # new_product.title = template.name
            new_product.title = template.with_context(lang=instance.shopify_lang_id.code).name

        return True

    def shopify_prepare_variant_vals(self, instance, variant, is_set_price, is_set_basic_detail):
        """This method used to prepare variant vals for export product variant from
            shopify third layer to shopify store.
            :param variant: Record of shopify product product(shopify product variant)
            @return: variant_vals
            @author: Nilesh Parmar @Emipro Technologies Pvt. Ltd on date 15/11/2019.
        """
        variant_vals = {}
        if variant.variant_id:
            variant_vals.update({"id": variant.variant_id})
        if is_set_price:
            price = instance.shopify_pricelist_id._get_product_price(variant.product_id, 1.0, partner=False,
                                                                     uom_id=variant.product_id.uom_id.id)
            variant_vals.update({"price": float(price)})

            if instance.shopify_compare_pricelist_id:
                compare_at_price = instance.shopify_compare_pricelist_id._get_product_price(variant.product_id, 1.0,
                                                                                            partner=False,
                                                                                            uom_id=variant.product_id.uom_id.id)
                variant_vals.update({"compare_at_price": float(compare_at_price)})
        if is_set_basic_detail:
            variant_vals = self.prepare_vals_for_product_basic_details(variant_vals, variant, instance)

        if variant.inventory_management == "shopify":
            variant_vals.update({"inventory_management": "shopify"})
        else:
            variant_vals.update({"inventory_management": None})

        if variant.check_product_stock == "continue":
            variant_vals.update({"inventory_policy": "continue"})
        else:
            variant_vals.update({"inventory_policy": "deny"})

        return variant_vals

    def prepare_vals_for_product_basic_details(self, variant_vals, variant, instance):
        """ This method is used to prepare a vals for the product basic details.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 21 October 2020 .
            Task_id: 167537
        """
        variant_vals.update({"barcode": variant.product_id.barcode or "",
                             "grams": int(variant.product_id.weight * 1000),
                             "weight": variant.product_id.weight,
                             "weight_unit": "kg",
                             "requires_shipping": "true", "sku": variant.default_code,
                             "taxable": variant.taxable and "true" or "false",
                             "title": variant.with_context(lang=instance.shopify_lang_id.code).name,
                             })
        option_index = 0
        option_index_value = ["option1", "option2", "option3"]
        attribute_value_obj = self.env["product.template.attribute.value"]
        att_values = attribute_value_obj.search(
            [("id", "in", variant.product_id.product_template_attribute_value_ids.ids)],
            order="attribute_id")
        for att_value in att_values:
            if option_index > 2:
                continue
            variant_vals.update(
                {option_index_value[option_index]: att_value.with_context(lang=instance.shopify_lang_id.code).name})
            option_index = option_index + 1

        return variant_vals

    def update_products_details_shopify_third_layer(self, new_product, template, is_publish):
        """
        this method is used to update the shopify product id, created date, update date,
        public date in shopify third layer
        :param new_product: shopify store product
        :param template: shopify template
        :param is_publish: if true then update public date of shopify product
        @author: Nilesh Parmar @Emipro Technologies Pvt. Ltd on date 19/11/2019.
        """
        # Accept either a shopify.Product-like object (with to_dict)
        # or a plain dict (converted from GraphQL responses).
        if hasattr(new_product, "to_dict") and callable(getattr(new_product, "to_dict")):
            result_dict = new_product.to_dict()
        elif isinstance(new_product, dict):
            result_dict = new_product
        else:
            _logger.error("Unsupported product type passed to update_products_details_shopify_third_layer: %s",
                          type(new_product))
            return False
        created_at = datetime.now()
        updated_at = datetime.now()
        tmpl_id = result_dict.get("id")
        total_variant = 1
        if result_dict.get("variants"):
            total_variant = len(result_dict.get("variants" or False))
        template.write({"total_variants_in_shopify": total_variant})
        template_vals = {"created_at": created_at, "updated_at": updated_at,
                         "shopify_tmpl_id": tmpl_id,
                         "exported_in_shopify": True,
                         }
        if is_publish == "unpublish_product":
            template.write({"published_at": False, "website_published": "unpublished"})
        elif is_publish == 'publish_product_global':
            template.write({'published_at': updated_at, 'website_published': 'published_global'})
        else:
            template.write({'published_at': updated_at, 'website_published': 'published_web'})
        if not template.exported_in_shopify:
            template.write(template_vals)
        self.write_variant_response_in_shopify_variant(result_dict, template)
        return True

    def write_variant_response_in_shopify_variant(self, result_dict, template):
        """ This method is used to write the variation response values in the Shopify variant.
            :param result_dict: Response of the product.
            :param template: Record of shopify template.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 21 October 2020 .
            Task_id: 167537
        """
        for variant_dict in result_dict.get("variants", []):
            updated_at = datetime.now()
            created_at = datetime.now()
            inventory_item_id = variant_dict.get("inventory_item_id") or False
            variant_id = variant_dict.get("id")
            odoo_product_variant = template.shopify_search_odoo_product_variant(template.shopify_instance_id,
                                                                                variant_id,
                                                                                variant_dict.get("sku"),
                                                                                variant_dict.get("barcode"))
            if not odoo_product_variant:
                continue
            shopify_variant = odoo_product_variant[0]
            if shopify_variant and not shopify_variant.exported_in_shopify:
                shopify_variant.write({
                    "variant_id": variant_id,
                    "updated_at": updated_at,
                    "created_at": created_at,
                    "inventory_item_id": inventory_item_id,
                    "exported_in_shopify": True
                })

    def export_product_images(self, instance, shopify_template):
        """
        This method use for the export images in to shopify store
        :param instance: use for the shopify instance
        :param shopify_template: use for the shopify template
        Author: Bhavesh Jadav  @Emipro Technologies Pvt. Ltd on date 18/12/2019.
        """
        instance.connect_in_shopify()
        if not shopify_template.shopify_image_ids:
            return False
        if len(shopify_template.shopify_product_ids) == 1:
            s_images = shopify_template.shopify_image_ids.filtered(lambda v: not v.shopify_variant_id)
        else:
            s_images = shopify_template.shopify_image_ids
        for image in s_images:
            if image.odoo_image_id.image:
                shopify_image = shopify.Image()
                shopify_image.product_id = shopify_template.shopify_tmpl_id
                shopify_image.attachment = image.odoo_image_id.image.decode("utf-8")
                if image.odoo_image_id.template_id and image.odoo_image_id.product_id:
                    shopify_image.variant_ids = [int(image.shopify_variant_id.variant_id)]
                result = shopify_image.save()
                if result:
                    image.write({"shopify_image_id": shopify_image.id})

        return True

    def update_product_images(self, shopify_template):
        """
        This method is used for the update Shopify product images if image is new in product then export image in
        shopify store.
        :param shopify_template: use for the shopify template.
        Author:Bhavesh Jadav 18/12/2019
        """
        if not shopify_template.shopify_image_ids:
            return False
        shopify_images = self.request_for_shopify_product_images(shopify_template)
        position = 0
        for image in shopify_template.shopify_image_ids:
            if image.odoo_image_id.image:
                position += 1
                if not image.shopify_image_id:
                    shopify_image = shopify.Image()
                    shopify_image.product_id = shopify_template.shopify_tmpl_id
                    shopify_image.attachment = image.odoo_image_id.image.decode("utf-8")
                    shopify_image.position = position
                    if image.shopify_variant_id:
                        shopify_image.variant_ids = [int(image.shopify_variant_id.variant_id)]
                    result = shopify_image.save()
                    if result:
                        image.write({"shopify_image_id": shopify_image.id})
                else:
                    ############################################
                    # Need to discuss update binary data or not
                    ############################################
                    if not shopify_images:
                        continue
                    for shop_image in shopify_images:
                        if int(image.shopify_image_id) == shop_image.id:
                            shopify_image = shop_image
                            shopify_image.attachment = image.odoo_image_id.image.decode("utf-8")
                            shopify_image.position = position
                            shopify_image.save()
        return True

    def request_for_shopify_product_images(self, shopify_template):
        """ This method is used to request for product images from Shopify store to Odoo.
            @return: shopify_images
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 21 October 2020 .
            Task_id: 167537
        """
        shopify_images = False
        try:
            shopify_images = shopify.Image().find(product_id=int(shopify_template.shopify_tmpl_id))
        except ClientError as error:
            if hasattr(error, "response") and error.response.code == 429 and error.response.msg == "Too Many Requests":
                time.sleep(int(float(error.response.headers.get('Retry-After', 5))))
                shopify_images = shopify.Image().find(product_id=shopify_template.shopify_tmpl_id)

        return shopify_images

    @api.model
    def export_stock_in_shopify(self, instance, product_ids):
        """
        Find products with below condition
            1. shopify_instance_id = instance.id
            2. exported_in_shopify = True
            3. product_id in products
        Find Shopify location for the particular instance
        Check export_stock_warehouse_ids is configured in location or not
        Get the total stock of the product with configured warehouses and update that stock in shopify location
        here we use InventoryLevel shopify API for export stock
        @author: Maulik Barad on Date 15-Sep-2020.
        """
        product_obj = self.env["product.product"]
        model = "shopify.product.product.ept"
        log_lines = []
        all_products = self.search_shopify_product_for_export_stock(instance, product_ids)

        if self.env.context.get('is_process_from_selected_product'):
            shopify_products = all_products
        else:
            if instance.shopify_last_date_update_stock:
                shopify_products = all_products.filtered(lambda x: not x.last_stock_update_date or
                                                                   x.last_stock_update_date <= instance.shopify_last_date_update_stock)
            else:
                shopify_products = all_products.filtered(lambda x: not x.last_stock_update_date)

        if not shopify_products:
            return False
        last_export_date = all_products[0].last_stock_update_date or datetime.now()

        if not shopify_products:
            return True

        instance.connect_in_shopify()
        location_ids = self.env["shopify.location.ept"].search(
            [("instance_id", "=", instance.id), ('legacy', '=', False)])
        if not location_ids:
            message = ("System tried to update stock from Odoo to the Shopify store, but the Shopify location was not found for the %s instance.\n"
                          "Action Items:\n"
                          "- Verify the Shopify location under: Shopify → Configuration → Shopify Locations.\n"
                          "- If the locations are not available in Odoo, import them using the operation wizard.") % instance.name
            log_lines.append(self.shopify_create_log(instance, message, model))

        shopify_templates = self.check_available_products_in_shopify(instance)
        if shopify_templates:
            shopify_template_ids = shopify_products.mapped('shopify_template_id')
            shopify_products = shopify_template_ids.filtered(
                lambda template: template.id in shopify_templates.ids).shopify_product_ids

        for location_id in location_ids:
            shopify_location_warehouse = location_id.export_stock_warehouse_ids or False
            if not shopify_location_warehouse:
                message = ("System tried to find the warehouse for Shopify location %s to export stock but it was not found.\n"
                              "Action Items:\n"
                              "- Verify the Shopify location under: Shopify → Configuration → Shopify Locations.\n"
                              "- Set the appropriate export stock warehouse mapping.") % location_id.name
                log_lines.append(self.shopify_create_log(instance, message, model))
                continue

            odoo_product_ids = shopify_products.product_id.ids
            product_stock = self.check_stock(instance, odoo_product_ids, product_obj,
                                             location_id.export_stock_warehouse_ids)
            commit_count = 0
            for shopify_product in shopify_products:
                if commit_count == 50:
                    self.env.cr.commit()
                    commit_count = 0
                commit_count += 1
                odoo_product = shopify_product.product_id
                if odoo_product.type == "consu" and odoo_product.is_storable:
                    if not shopify_product.inventory_item_id:
                        message = ("System tried to export stock for Shopify product variant ID: %s with name %s for "
                                   "the %s instance, but the Inventory Item was not found.\n"
                                   "Action Items:\n"
                                   "- Go to: Shopify → Products → Product Variants and check if the Inventory Item ID is set.\n"
                                   "- If not, sync the product using the Import Specific Product functionality from the "
                                   "operation wizard to set the Inventory Item ID.") % (shopify_product.id,
                                    shopify_product.name, instance.name)
                        log_lines.append(self.shopify_create_log(instance, message, model))
                        continue

                    quantity = self.compute_qty_for_export_stock(product_stock, shopify_product, odoo_product)
                    export_log_msg = "System tried to export stock but received an error from the Shopify store " \
                                     "with Product ID: %s and name: %s for the %s instance.\n" \
                                     "Action Items:\n" \
                                     "- Verify the product's existence on the Shopify store using the given name and Product ID.\n" \
                                     "- If it has been deleted, archive the product from the Shopify product layer " \
                                     "in Odoo."
                    try:
                        shopify.InventoryLevel.set(location_id.shopify_location_id, shopify_product.inventory_item_id,
                                                   int(quantity))
                    except ResourceNotFound as error:
                        if hasattr(error, "response"):
                            message = (export_log_msg) %(odoo_product.id, odoo_product.name, instance.name)
                            log_lines.append(self.shopify_create_log(instance, message, model))
                    except ClientError as error:
                        if hasattr(error,
                                   "response") and error.response.code == 429 and error.response.msg == "Too Many Requests":
                            time.sleep(int(float(error.response.headers.get('Retry-After', 5))))
                            shopify.InventoryLevel.set(location_id.shopify_location_id,
                                                       shopify_product.inventory_item_id,
                                                       int(quantity))
                            continue
                        elif error.response.code == 422 and error.response.msg == "Unprocessable Entity":
                            if json.loads(error.response.body.decode()).get("errors")[
                                0] == 'Inventory item does not have inventory tracking enabled':
                                shopify_product.write({'inventory_management': "Dont track Inventory"})
                            continue
                        message = (export_log_msg) %(odoo_product.id, odoo_product.name, instance.name)
                        log_lines.append(self.shopify_create_log(instance, message, model))
                    except Exception as error:
                        message = (export_log_msg) %(odoo_product.id, odoo_product.name, instance.name)
                        log_lines.append(self.shopify_create_log(instance, message, model))

                    if not self.env.context.get('is_process_from_selected_product'):
                        shopify_product.write({
                            'last_stock_update_date': last_export_date if not shopify_product.last_stock_update_date else datetime.now()})

        if log_lines and instance.is_shopify_create_schedule:
            message = []
            count = 0
            for log_line in log_lines:
                count += 1
                if count <= 5:
                    message.append('<' + 'li' + '>' + log_line.message + '<' + '/' + 'li' + '>')
            if count >= 5:
                message.append(
                    '<' + 'p' + '>' + 'Please refer the logbook' + '  ' + log_line.name + '  '
                    + 'check it in more detail' + '<' + '/' + 'p' + '>')
            note = "\n".join(message)

            self.env['sale.order'].create_schedule_activity_against_loglines(log_lines, note)
        return all_products

    @api.model
    def export_stock_queue(self, instance, product_ids):
        """
        Find products with below condition
            1. shopify_instance_id = instance.id
            2. exported_in_shopify = True
            3. product_id in products
        Find Shopify location for the particular instance
        Check export_stock_warehouse_ids is configured in location or not
        Get the total stock of the product with configured warehouses and update that stock in shopify location
        here we use InventoryLevel shopify API for export stock
        @author: Maulik Barad on Date 15-Sep-2020.
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        product_obj = self.env["product.product"]
        export_stock_obj = self.env['shopify.export.stock.queue.ept']
        model = "shopify.product.product.ept"
        all_products = self.search_shopify_product_for_export_stock(instance, product_ids)

        if self.env.context.get('is_process_from_selected_product'):
            shopify_products = all_products
        else:
            if instance.shopify_last_date_update_stock:
                shopify_products = all_products.filtered(lambda x: not x.last_stock_update_date or
                                                                   x.last_stock_update_date <= instance.shopify_last_date_update_stock)
            else:
                shopify_products = all_products.filtered(lambda x: not x.last_stock_update_date)

        if not shopify_products:
            return False
        shopify_products = all_products
        last_export_date = all_products[0].last_stock_update_date or datetime.now()

        if not shopify_products:
            return True

        instance.connect_in_shopify()
        location_ids = self.env["shopify.location.ept"].search(
            [("instance_id", "=", instance.id), ('legacy', '=', False)])
        if not location_ids:
            message = ("System tried to update stock from Odoo to the Shopify store, but the Shopify location was not found for the %s instance.\n"
                       "Action Items:\n"
                       "- Verify the Shopify location under: Shopify → Configuration → Shopify Locations.\n"
                       "- If the locations are not available in Odoo, import them using the operation wizard.") % instance.name
            self.shopify_create_log(instance, message, model)

        # if not self._context.get('is_process_from_selected_product'):
        #     shopify_templates = self.check_available_products_in_shopify(instance)
        #     if shopify_templates:
        #         shopify_products = shopify_templates.shopify_product_ids.filtered(
        #             lambda p: p.inventory_management == 'shopify')

        shopify_products = shopify_products.filtered(
            lambda l: l.product_id.id in product_ids and l.inventory_management == 'shopify')
        export_stock_data = []
        for location_id in location_ids:
            shopify_location_warehouse = location_id.export_stock_warehouse_ids or False
            if not shopify_location_warehouse:
                message = ("System tried to find the warehouse for Shopify location %s to export stock but it was not found.\n"
                              "Action Items:\n"
                              "- Verify the Shopify location under: Shopify → Configuration → Shopify Locations.\n"
                              "- Set the appropriate export stock warehouse mapping.") % location_id.name
                self.shopify_create_log(instance, message, model)
                continue

            odoo_product_ids = shopify_products.product_id.ids
            product_stock = self.check_stock(instance, odoo_product_ids, product_obj,
                                             location_id.export_stock_warehouse_ids)
            commit_count = 0
            for shopify_product in shopify_products:
                if commit_count == 50:
                    self.env.cr.commit()
                    commit_count = 0
                commit_count += 1
                odoo_product = shopify_product.product_id
                if odoo_product.type == "consu":
                    if not shopify_product.inventory_item_id:
                        message = "Inventory Item Id did not found for Shopify Product Variant ID " \
                                  "%s with name %s for instance %s while Export stock" % (
                                      shopify_product.id, shopify_product.name, instance.name)
                        self.shopify_create_log(instance, message, model)
                        continue
                    if not shopify_product.fixed_stock_export:
                        quantity = self.compute_qty_for_export_stock(product_stock, shopify_product, odoo_product)
                    else:
                        quantity = shopify_product.fixed_stock_export_value

                    export_stock_data.append({'product_name': shopify_product.default_code,
                                              'shopify_product_id': shopify_product,
                                              'location_id': location_id.shopify_location_id,
                                              'inventory_item_id': shopify_product.inventory_item_id,
                                              'quantity': int(quantity)})

                    # if not self._context.get('is_process_from_selected_product'):

        export_stock_queue = export_stock_obj.create_export_stock_queue(instance, export_stock_data)
        if export_stock_queue:
            shopify_products.write({
                'last_stock_update_date': datetime.now() - timedelta(hours=0.5)})
            instance.write({
                'shopify_last_date_update_stock': datetime.now() - timedelta(hours=0.5)})
            queue_cron = self.sudo().env.ref("shopify_ept.process_shopify_export_stock_queue")
            if not queue_cron.active:
                _logger.info("Active the Export stock data process queue cron job")
                queue_cron.write(
                    {'active': True, 'nextcall': datetime.now() + timedelta(seconds=120)})
        if not export_stock_queue.export_stock_queue_line_ids:
            export_stock_queue.unlink()
            self.env.cr.commit()
            return False
        return export_stock_queue

    def compute_qty_for_export_stock(self, product_stock, shopify_product, odoo_product):
        """ This method is used to find qty base on the configuration of Shopify.
            :param product_stock: Dictionary of the odoo product with qty.
            :param shopify_product: Record of shopify product product.
            :param odoo_product: Record of odoo product.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 30 October 2020 .
            Task_id:167537
        """
        quantity = product_stock.get(odoo_product.id, 0)
        if shopify_product.fix_stock_type == 'fix':
            if shopify_product.fix_stock_value < quantity:
                quantity = shopify_product.fix_stock_value
        elif shopify_product.fix_stock_type == 'percentage':
            percentage_stock = int((quantity * shopify_product.fix_stock_value) / 100.0)
            if percentage_stock < quantity:
                quantity = percentage_stock

        return quantity

    def search_shopify_product_for_export_stock(self, instance, product_ids):
        """ This method is used to search shopify product for export stock.
            :param product_ids: Record of Odoo product ids
            @return: shopify_products
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 21 October 2020 .
            Task_id: 167537
        """
        shopify_products = self.search([("shopify_instance_id", "=", instance.id),
                                        ("exported_in_shopify", "=", True),
                                        ("product_id", "in", product_ids), ('inventory_management', '=', 'shopify')],
                                       order='last_stock_update_date')
        return shopify_products

    def check_stock(self, instance, product_ids, prod_obj, warehouse):
        """
        This Method relocates check type of stock.
        :param instance: This arguments relocates instance of Shopify.
        :param product_ids: This arguments product listing id of odoo.
        :param prod_obj: This argument relocates product object of common connector.
        :param warehouse:This arguments relocates warehouse of shopify export location.
        :return: This Method return product listing stock.
        """
        product_stock = {}

        if product_ids:
            if instance.sudo().shopify_stock_field.name == "free_qty":
                product_stock = prod_obj.get_free_qty_ept(warehouse, product_ids)

            elif instance.sudo().shopify_stock_field.name == "virtual_available":
                product_stock = prod_obj.get_forecasted_qty_ept(warehouse, product_ids)

            elif instance.sudo().shopify_stock_field.name == "qty_available":
                product_stock = prod_obj.get_onhand_qty_ept(warehouse, product_ids)

        return product_stock

    def import_shopify_stock(self, instance, validate_inventory):
        """
        This method is used to import product stock from shopify store to Odoo.
        @author: Angel Patel @Emipro Technologies Pvt. Ltd.
        Migration done by Meera Sidapara on 30/09/2021
        """
        stock_inventory_obj = self.env["stock.quant"]
        stock_inventory_name_obj = self.env["stock.inventory.adjustment.name"]
        model = "shopify.product.product.ept"
        # inventory_records = []
        templates = self.search([("shopify_instance_id", "=", instance.id), ("exported_in_shopify", "=", True)])
        if templates:
            instance.connect_in_shopify()
            location_ids = self.search_shopify_location_for_import_stock(instance, model)
            if not location_ids:
                return False

            for location_id in location_ids:
                shopify_location_warehouse = location_id.import_stock_warehouse_id or False
                if not shopify_location_warehouse:
                    message = ("System tried to find the warehouse for Shopify location %s to export stock but it was not found.\n"
                               "Action Items:\n"
                               "- Verify the Shopify location under: Shopify → Configuration → Shopify Locations.\n"
                               "- Set the appropriate export stock warehouse mapping.") % location_id.name
                    self.shopify_create_log(instance, message, model)
                    _logger.info(message)
                    continue

                inventory_levels = self.request_for_the_inventory_level(location_id, instance, model)

                if not inventory_levels:
                    continue

                if instance.use_graphql_api:
                    stock_inventory_array = self.prepare_graphql_val_for_stock_inventory(inventory_levels, instance)
                else:
                    stock_inventory_array = self.prepare_val_for_stock_inventory(inventory_levels, instance)

                if len(stock_inventory_array) > 0:
                    inventory_name = 'Inventory For Instance "%s" And Shopify Location "%s"' % (
                        instance.name, location_id.name)
                    inventories = stock_inventory_obj.with_company(
                        instance.shopify_company_id.id).create_inventory_adjustment_ept(
                        stock_inventory_array, location_id.import_stock_warehouse_id.lot_stock_id, validate_inventory,
                        inventory_name)
                    if inventories:
                        stock_inventory_name_obj.write({'name': inventory_name})
                        _logger.info("Created %s." % inventory_name)
        # return inventory_records

    def search_shopify_location_for_import_stock(self, instance, model):
        """ This method is used to search a shopify location for import stock from shopify to Odoo.
            :param log_line_array: Blank list of for log line.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 21 October 2020 .
            Task_id: log_line_array
        """
        location_ids = self.env["shopify.location.ept"].search(
            [("legacy", "=", False), ("instance_id", "=", instance.id)])
        if not location_ids:
            message = ("System tried to import stock from Odoo to the Shopify store, but the Shopify location was not found for the %s instance.\n"
                          "Action Items:\n"
                          "- Verify the Shopify location under: Shopify → Configuration → Shopify Locations.\n"
                          "- If the locations are not available in Odoo, import them using the operation wizard.") % instance.name
            self.shopify_create_log(instance, message, model)
            _logger.info(message)
            return False

        return location_ids

    def request_for_the_inventory_level(self, location_id, instance, model):
        """ This method is used to request for inventory level from Odoo to shopify.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 21 October 2020 .
            Task_id: 167537
        """
        if instance.use_graphql_api:
            inventory_levels = {}
            try:
                # Use client-based GraphQL execution (no active shopify session)
                client = instance.get_graphql_client()
                inventory_helper = InventoryQueryHelper(client)
                # Use helper to fetch all pages and return combined response
                inventory_levels = inventory_helper.get_all_inventory_levels(location_id.shopify_location_id, first=250)
            except Exception as error:
                message = ("System tried to import stock but received an unknown error for the Shopify %s instance.\n"
                           "Action Items:\n"
                           "- Verify the inventory levels in the Shopify store.") % instance.name
                self.shopify_create_log(instance, message, model)
                _logger.info(message)
                return False
        else:
            try:
                inventory_levels = shopify.InventoryLevel.find(location_ids=location_id.shopify_location_id,
                                                               limit=250)
                if len(inventory_levels) == 250:
                    inventory_levels = self.shopify_list_all_inventory_level(inventory_levels)
            except Exception as error:
                message = ("System tried to import stock but received an unknown error for the Shopify %s instance.\n"
                           "Action Items:\n"
                           "- Verify the inventory levels in the Shopify store.") % instance.name
                self.shopify_create_log(instance, message, model)
                _logger.info(message)
                return False

            _logger.info("Length of the total inventory item id : %s", len(inventory_levels))

        return inventory_levels

    def _skip_bom_product_stock_import(self, product_id, instance):
        """
        Decides whether stock import should be skipped for a product having a BOM.
        Kit BOMs are always skipped, since their stock is auto-computed from components in Odoo.
        Manufacture/Subcontract BOMs are skipped unless the instance setting
        `shopify_import_stock_for_bom_products` is enabled.
        @param product_id: product.product record to check for BOM/Kit.
        @param instance: shopify.instance.ept record being processed.
        @return: True if the stock import should be skipped for this product, else False.
        @author: Gopal Chouhan @Emipro Technologies Pvt. Ltd on date 20 July 2026.
        """
        if not (hasattr(product_id, 'bom_count') and product_id.bom_count):
            return False
        if product_id.with_company(instance.shopify_company_id.id).is_kits:
            _logger.info(f"Skipping Kit Product ID: {product_id}, kit stock is auto-computed from its components")
            return True
        if not instance.shopify_import_stock_for_bom_products:
            _logger.info(f"Skipping Product ID: {product_id}, its BOM Count is : {product_id.bom_count} and "
                         f"'Import Stock for BOM Products' is disabled")
            return True
        return False

    def prepare_graphql_val_for_stock_inventory(self, inventory_levels, instance):
        """
        Override this method to Prepare the data based on GraphQL API response.
        """
        stock_inventory_array = {}
        product_ids_list = []
        inventory_levels = self._get_data_and_pageinfo_inventory_lineitem('nodes', inventory_levels)
        for inventory_level in inventory_levels:
            inventory_data = inventory_level.get('inventoryLevel') and inventory_level.get('inventoryLevel').get(
                'quantities') and inventory_level.get('inventoryLevel').get('quantities')[0]
            if inventory_data:
                inventory_line_id = inventory_data.get('id')
                inventory_line_id = re.search(r'inventory_item_id=(\d+)', inventory_line_id)
                inventory_item_id = inventory_line_id.group(1)
                if inventory_item_id:
                    qty = inventory_data.get('quantity')

                    shopify_product = self.env["shopify.product.product.ept"].search(
                        [("inventory_item_id", "=", inventory_item_id), ("exported_in_shopify", "=", True),
                         ("shopify_instance_id", "=", instance.id)], limit=1)
                    if shopify_product:
                        product_id = shopify_product.product_id
                        if self._skip_bom_product_stock_import(product_id, instance):
                            continue
                        if product_id not in product_ids_list and product_id.type not in [
                            'service'] and product_id.is_storable:
                            stock_inventory_line = {
                                product_id.id: qty,
                            }
                            stock_inventory_array.update(stock_inventory_line)
                            product_ids_list.append(product_id)

        return stock_inventory_array

    def _get_data_and_pageinfo_inventory_lineitem(self, data, inventory_levels):
        """
        This method is used to Get the data based on nodel and page info.
        """
        data_list = {}
        if data == 'nodes':
            data_list = inventory_levels and inventory_levels.get('data') and inventory_levels.get('data').get(
                'inventoryItems') and inventory_levels.get('data').get(
                'inventoryItems').get('nodes')
        elif data == 'pageinfo':
            data_list = inventory_levels and inventory_levels.get(
                'data') and inventory_levels.get('data').get(
                'inventoryItems') and inventory_levels.get('data').get(
                'inventoryItems').get('pageInfo')
        return data_list

    def prepare_val_for_stock_inventory(self, inventory_levels, instance):
        """ This method is used to search the shopify product base on the inventory id which receive from the
            inventory level dict.
            @return: stock_inventory_line
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 21 October 2020 .
            Task_id: 167537
            Migration done by Meera Sidapara on 30/09/2021
        """
        stock_inventory_array = {}
        product_ids_list = []
        for inventory_level in inventory_levels:
            inventory_level = inventory_level.to_dict()
            inventory_item_id = inventory_level.get("inventory_item_id")
            qty = inventory_level.get("available")

            shopify_product = self.env["shopify.product.product.ept"].search(
                [("inventory_item_id", "=", inventory_item_id), ("exported_in_shopify", "=", True),
                 ("shopify_instance_id", "=", instance.id)], limit=1)
            if shopify_product:
                product_id = shopify_product.product_id
                if self._skip_bom_product_stock_import(product_id, instance):
                    continue
                if product_id not in product_ids_list and product_id.type not in [
                    'service'] and product_id.is_storable:
                    stock_inventory_line = {
                        product_id.id: qty,
                    }
                    stock_inventory_array.update(stock_inventory_line)
                    product_ids_list.append(product_id)

        return stock_inventory_array

    def shopify_list_all_inventory_level(self, result):
        """
            This method used to call the page wise data import for product stock from Shopify to Odoo.
            @param : self, result
            @author: Angel Patel @Emipro Technologies Pvt. Ltd on date 21/12/2019.
            Modify by Haresh Mori on 28/12/2019 API and Pagination changes
        """
        sum_inventory_list = []
        catch = ""
        while result:
            page_info = ""
            sum_inventory_list += result
            link = shopify.ShopifyResource.connection.response.headers.get(
                "Link") if shopify.ShopifyResource.connection.response.headers.get(
                "Link") else shopify.ShopifyResource.connection.response.headers.get("link")
            if not link or not isinstance(link, str):
                return sum_inventory_list
            for page_link in link.split(","):
                if page_link.find("next") > 0:
                    page_info = page_link.split(";")[0].strip("<>").split("page_info=")[1]
                    try:
                        result = shopify.InventoryLevel.find(page_info=page_info, limit=250)
                    except ClientError as error:
                        if hasattr(error,
                                   "response") and error.response.code == 429 and error.response.msg == "Too Many Requests":
                            time.sleep(int(float(error.response.headers.get('Retry-After', 5))))
                            result = shopify.InventoryLevel.find(page_info=page_info, limit=250)
                    except Exception as error:
                        raise UserError(error)
            if catch == page_info:
                break
        return sum_inventory_list

    def shopify_create_log(self, instance, message=False, model=False):
        """
        This method is used to prepare a vals for log line.
        @author: Angel Patel @Emipro Technologies Pvt. Ltd on date 14/11/2019.
        @Task ID: 157623
        """
        log_line = self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=instance.id,
                                                                               module="shopify_ept",
                                                                               message=message,
                                                                               model_name=model)
        return log_line

    def update_products_in_shopify_via_graphql(self, instance, templates, is_set_price, is_set_images, is_publish,
                                               is_set_basic_detail):
        """
        This method is used to update products in shopify via graphql api.
        :param instance: Shopify Instance
        :param templates: Shopify Templates
        """
        client = instance.get_graphql_client()
        product_helper = shopify_graphql.ProductQueryHelper(client)
        pub_id_dict = product_helper._get_online_store_publication_id()
        translation_pairs = []
        for template in templates:
            product_data = {}
            if is_set_basic_detail or is_set_price:
                product_input = product_helper._prepare_graphql_productset_input(
                    template, instance, is_set_price, is_set_basic_detail)
    
                variables = {"input": product_input}
    
                resp = client.execute(product_helper.PRODUCT_SET_MUTATION, variables)
                errors = resp.get("data", {}).get("productSet", {}).get("userErrors", [])
                if errors:
                    for err in errors:
                        self.env["common.log.lines.ept"].create_common_log_line_ept(
                            shopify_instance_id=instance.id,
                            module="shopify_ept",
                            message=f"ProductSet Error: {err}",
                            model_name="shopify.product.template.ept")
                        if err.get('code') and err.get('code') == 'PRODUCT_DOES_NOT_EXIST':
                            template.unlink()
                    continue
                product_data = resp.get("data", {}).get("productSet", {}).get("product")
            if not product_data:
                product_data = product_helper.get_product([template.shopify_tmpl_id])[0]
                variant_nodes = product_data.get("variants", {})
            else:
                variant_nodes = product_data.get("variants", {}).get("nodes", [])
            product_id = product_data.get('id')
            if is_publish:
                product_helper._publish_graphql(pub_id_dict, template, is_publish)

            if is_set_images:
                product_helper.export_product_images_gql(template, product_id, variant_nodes)

            result_dict = product_helper.convert_graphql_product_to_rest_dict(product_data)
            self.update_products_details_shopify_third_layer(result_dict, template, is_publish)
            self.update_hsn_code_for_variants(instance, [template])
            updated_at = datetime.now()
            template.write({"updated_at": updated_at})
            template.shopify_product_ids.write({"updated_at": updated_at})
            if instance.is_translations_needed():
                translation_pairs.append((template, product_id))

        if translation_pairs:
            self.env["shopify.product.template.ept"].batch_export_product_translations(
                instance, translation_pairs)
        return True


class ShopifyTag(models.Model):
    _name = "shopify.tags"
    _description = "Shopify Tags"

    name = fields.Char(required=True)
    sequence = fields.Integer(required=True)
