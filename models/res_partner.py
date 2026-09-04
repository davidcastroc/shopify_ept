# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

import logging
from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError
from ..shopify_graphql.queries.customer import CustomerQueryHelper

_logger = logging.getLogger("Shopify Partner")


class ResPartner(models.Model):
    _inherit = "res.partner"

    is_shopify_customer = fields.Boolean(string="Is Shopify Customer?", default=False,
                                         help="Used for identified that the customer is imported from Shopify store.")
    shopify_segment_ids = fields.Many2many(comodel_name="shopify.customer.segment.ept",
                                           relation="shopify_partner_segment_rel", column1="partner_id",
                                           column2="segment_id", string="Shopify Segments",
                                           help="Shopify segments to which the customer belongs.")

    @api.model
    def create_shopify_pos_customer(self, order_response, instance):
        """
        Creates customer from POS Order.
        @author: Maulik Barad on Date 27-Feb-2020.
        """
        address = {}
        shopify_partner_obj = self.env["shopify.res.partner.ept"]
        partner_obj = self.env["res.partner"]
        customer_data = partner_obj.remove_special_chars_from_partner_vals(order_response.get("customer"))

        if customer_data.get("default_address"):
            address = customer_data.get("default_address")

        customer_id = customer_data.get("id")
        first_name = customer_data.get("first_name") if customer_data.get("first_name") else ''
        last_name = customer_data.get("last_name") if customer_data.get("last_name") else ''
        phone = customer_data.get("phone")
        email = customer_data.get("email")

        shopify_partner = shopify_partner_obj.search([("shopify_customer_id", "=", customer_id),
                                                      ("shopify_instance_id", "=", instance.id)],
                                                     limit=1)

        partner_vals = shopify_partner_obj.shopify_prepare_partner_vals(address, instance)
        partner_vals = self.update_name_in_partner_vals(partner_vals, first_name, last_name, email, phone)
        if shopify_partner:
            parent_id = shopify_partner.partner_id.id
            partner_vals.update(parent_id=parent_id)
            key_list = list(partner_vals.keys())
            res_partner = self._find_partner_ept(partner_vals, key_list, [])
            if not res_partner:
                del partner_vals["parent_id"]
                key_list = list(partner_vals.keys())
                res_partner = self._find_partner_ept(partner_vals, key_list, [])
                if not res_partner:
                    partner_vals.update(
                        {'is_company': False, 'type': 'invoice', 'customer_rank': 0, 'is_shopify_customer': True})
                    res_partner = self.create(partner_vals)
            return res_partner

        res_partner = self

        res_partner = self.search_partner_by_email_phone(res_partner, email, phone)

        if res_partner:
            partner_vals.update({"is_shopify_customer": True, "type": "invoice", "parent_id": res_partner.id})
            res_partner = self.create(partner_vals)
        else:
            key_list = list(partner_vals.keys())
            res_partner = self._find_partner_ept(partner_vals, key_list, [])
            if res_partner:
                res_partner.write({"is_shopify_customer": True})
            else:
                partner_vals.update({"is_shopify_customer": True, "type": "contact"})
                res_partner = self.create(partner_vals)

        shopify_partner_obj.create({"shopify_instance_id": instance.id,
                                    "shopify_customer_id": customer_id,
                                    "partner_id": res_partner.id})
        return res_partner

    def update_name_in_partner_vals(self, partner_vals, first_name, last_name, email, phone):
        """ This method is used to update the name of the pos customer if the first name and last name
            do not exist in response.
            @return: partner_vals
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 2 November 2020 .
            Task_id: 167537
        """
        name = ("%s %s" % (first_name, last_name)).strip()
        if name == "":
            if email:
                name = email
            elif phone:
                name = phone

        partner_vals.update({"name": name})

        return partner_vals

    def search_partner_by_email_phone(self, res_partner, email, phone):
        """ This method is used to search res partner record base on email and phone.
            @return:res_partner
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 2 November 2020 .
            Task_id:167537
        """

        if email:
            res_partner = self.search([("email", "=", email)], limit=1)
        if not res_partner and phone:
            res_partner = self.search([("phone", "=", phone)], limit=1)
        if res_partner and res_partner.parent_id:
            res_partner = res_partner.parent_id

        return res_partner

    def create_or_search_tag(self, tag):
        """
        :param tag:
        :return:
        """
        res_partner_category_obj = self.env['res.partner.category']

        exists_tag = res_partner_category_obj.search([('name', '=ilike', tag)], limit=1)

        if not exists_tag:
            exists_tag = res_partner_category_obj.sudo().create({'name': tag})
        return exists_tag.id

    def _get_customer_helper(self, instance: object):
        """
        Get an instance of CustomerQueryHelper for the given Shopify instance.
        :param instance: Shopify instance record
        :return: CustomerQueryHelper instance

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not instance:
            raise UserError("Shopify Instance is required.")
        if not instance.use_graphql_api:
            raise UserError("GraphQL API is not enabled for this Shopify Instance.")
        client = instance.get_graphql_client()
        return CustomerQueryHelper(client)

    def _search_shopify_customer_by_email_graphql(self, helper: object, email: str = None):
        """
        Search Shopify customer by email using GraphQL.
        :param helper: An instance of CustomerQueryHelper
        :param email: optional email, defaults to current partner email
        :return: first exact matched customer node or False

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        search_email = (email or self.email or "").strip()
        if not search_email:
            return False

        # Find customers with exact email match on Shopify
        customers = helper.search_customer_by_email(search_email, first=10)
        if isinstance(customers, str):
            raise ValidationError(customers)
        return customers[0] if customers else False

    def sync_shopify_customer_mapping_graphql(self, instance: object):
        """
        Search customer in Shopify by email, then create/update Shopify partner mapping.
        :param instance: Shopify instance record
        :return: shopify.res.partner.ept record

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        helper = self._get_customer_helper(instance)
        email = (self.email or "").strip()
        customer_data = self._search_shopify_customer_by_email_graphql(helper, email=email)
        if not customer_data:
            # Create customer on Shopify if not found and then create mapping
            return self.export_contact_to_shopify_graphql(instance)

        shopify_customer_gid = customer_data.get("id")
        shopify_customer_id = helper.client._extract_id_from_gid(shopify_customer_gid)
        if not shopify_customer_id:
            raise ValidationError("Shopify customer id was not found in GraphQL response.")

        shopify_partner_obj = self.env["shopify.res.partner.ept"]
        shopify_partner = shopify_partner_obj.search(
            [("partner_id", "=", self.id), ("shopify_instance_id", "=", instance.id)],
            limit=1
        )
        if shopify_partner:
            if str(shopify_partner.shopify_customer_id) != str(shopify_customer_id):
                shopify_partner.write({"shopify_customer_id": str(shopify_customer_id)})
        else:
            shopify_partner = shopify_partner_obj.create({
                "partner_id": self.id,
                "shopify_instance_id": instance.id,
                "shopify_customer_id": str(shopify_customer_id),
            })

        if not self.is_shopify_customer:
            self.write({"is_shopify_customer": True})
        return shopify_partner

    def export_contact_to_shopify_graphql(self, instance: object):
        """
        Export current Odoo contact to Shopify and create/update mapping.
        :param instance: Shopify instance record
        :return: shopify.res.partner.ept record

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        shopify_partner_obj = self.env["shopify.res.partner.ept"]

        # Find existing mapping for partner and instance
        existing_mapping = shopify_partner_obj.search(
            [("partner_id", "=", self.id), ("shopify_instance_id", "=", instance.id)],
            limit=1
        )
        if existing_mapping and existing_mapping.shopify_customer_id:
            if not self.is_shopify_customer:
                self.write({"is_shopify_customer": True})
            return existing_mapping

        # Export contact to Shopify and get the Shopify customer ID
        shopify_customer_id = self._export_to_shopify(instance)

        if existing_mapping:
            existing_mapping.write({"shopify_customer_id": str(shopify_customer_id)})
            mapping = existing_mapping
        else:
            mapping = shopify_partner_obj.create({
                "partner_id": self.id,
                "shopify_instance_id": instance.id,
                "shopify_customer_id": str(shopify_customer_id),
            })

        if not self.is_shopify_customer:
            self.write({"is_shopify_customer": True})
        return mapping

    def _export_to_shopify(self, instance: object):
        """
        This method is used to export the contact to Shopify.
        :param instance: Shopify instance record
        :return: Shopify customer ID

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        helper = self._get_customer_helper(instance)
        # Export customer data to Shopify
        payload = self._prepare_shopify_customer_input_graphql()
        payload.pop("addresses", None)  # Addresses are handled separately after customer creation
        customer_data = self._export_customer(instance, helper, payload)

        shopify_customer_gid = customer_data.get("id")
        shopify_customer_id = helper.client._extract_id_from_gid(shopify_customer_gid)
        if not shopify_customer_id:
            raise ValidationError("Shopify customer id was not found in GraphQL response.")

        # Export address data to Shopify if there is any address to export
        addresses_payload = self._prepare_shopify_customer_addresses_graphql()
        address_result = self._export_address(instance, helper, addresses_payload, shopify_customer_gid)
        if isinstance(address_result, str):
            raise ValidationError(address_result)
        return shopify_customer_id

    def _export_customer(self, instance: object, helper: object, payload: dict):
        """
        This method is used to export the customer to Shopify.
        :param instance: Shopify instance record
        :param helper: An instance of CustomerQueryHelper
        :param payload: Shopify customer input payload
        :return: Shopify customer data

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        customer_data = helper.create_customer(payload)
        if isinstance(customer_data, str):
            raise ValidationError(customer_data)
        return customer_data

    def _export_address(self, instance: object, helper: object, payload: dict, shopify_customer_gid: str):
        """
        This method is used to export the address to Shopify.
        :param instance: Shopify instance record
        :param helper: An instance of CustomerQueryHelper
        :param payload: Shopify customer address input payload
        :param shopify_customer_gid: Shopify customer GID
        :return: boolean or string (error message)

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if self._has_address_to_export_shopify(payload):
            return helper.update_customer_addresses(shopify_customer_gid, payload)
        return False

    @staticmethod
    def _has_address_to_export_shopify(address_payload: dict):
        """
        This method checks if there is any address information to export to Shopify
         by looking for non-empty values in key address fields.
        :param address_payload: address payload data
        :return: boolean

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not address_payload:
            return False
        keys_to_check = ["address1", "address2", "city", "zip", "phone", "company", "provinceCode", "countryCode"]
        if isinstance(address_payload, list):
            return any(any(bool(address.get(key)) for key in keys_to_check) for address in address_payload)
        return any(bool(address_payload.get(key)) for key in keys_to_check)

    def _prepare_shopify_customer_input_graphql(self):
        """
        Prepare GraphQL customer input payload from partner record.
        :return: dict

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        if not self.email:
            raise ValidationError("Email is required to export customer to Shopify.")

        first_name, last_name = self._get_name()
        return {
            "firstName": first_name,
            "lastName": last_name,
            "email": self.email.strip(),
            "phone": self.phone or "",
            "tags": [categ.name for categ in self.category_id if categ.name],
            "addresses": [],
        }

    def _prepare_shopify_customer_addresses_graphql(self):
        """
        Prepare customer addresses list for Shopify customerUpdate mutation.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.ensure_one()
        addresses = []
        main_address = self._prepare_shopify_address_graphql()
        if self._has_address_to_export_shopify(main_address):
            addresses.append(main_address)
        return addresses

    def _prepare_shopify_address_graphql(self):
        """
        This method prepares the address dictionary for Shopify GraphQL API based on the given partner record.
        :return: address dict

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not self:
            return False
        self.ensure_one()
        first_name, last_name = self._get_name()
        vals = {
            "firstName": first_name,
            "lastName": last_name,
            "address1": self.street or "",
            "address2": self.street2 or "",
            "city": self.city or "",
            "zip": self.zip or "",
            "phone": self.phone or "",
            "company": self.company_name or "",
            "provinceCode": self.state_id.code if self.state_id else "",
            "countryCode": self.country_id.code if self.country_id else "",
        }
        vals = self.remove_special_chars_from_partner_vals(vals)
        vals = {key: value for key, value in vals.items() if value}
        return vals

    def _get_name(self):
        """
        This method is used to get the name of the partner.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not self.name:
            first_name, last_name = "", ""
        else:
            names = self.name.split()
            if len(names) > 1:
                first_name = names[0]
                last_name = " ".join(names[1:])
            else:
                first_name = self.name
                last_name = ""
        return first_name, last_name
