# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.exceptions import ValidationError
from ..shopify_graphql.queries.payment_terms import PaymentTermsQueryHelper


class ShopifyPaymentTermsEpt(models.Model):
    _name = "shopify.payment.terms.ept"
    _description = "Shopify Payment Terms"

    name = fields.Char(string="Name")
    instance_id = fields.Many2one("shopify.instance.ept", string="Instance")
    shopify_payment_term_id = fields.Integer(string="Shopify Payment Term ID", required=True)
    shopify_payment_term_gid = fields.Char(string="Shopify Payment Term GID", required=True)
    odoo_payment_term_id = fields.Many2one("account.payment.term", string="Odoo Payment Term")
    payment_term_type = fields.Char(string="Payment Term Type")
    default = fields.Boolean(string="Default")

    @api.constrains("instance_id", "odoo_payment_term_id")
    def _check_unique_instance_odoo_payment_term(self):
        """
        Constraint to ensure that the combination of instance and Odoo payment term is unique.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        for record in self:
            if not record.instance_id or not record.odoo_payment_term_id:
                continue
            duplicate = self.search([
                ("id", "!=", record.id),
                ("instance_id", "=", record.instance_id.id),
                ("odoo_payment_term_id", "=", record.odoo_payment_term_id.id),
            ], limit=1)
            if duplicate:
                raise ValidationError(_(
                    "Odoo Payment Term must be unique per instance.\n"
                    "Instance: %s\n"
                    "Payment Term: %s"
                ) % (record.instance_id.display_name, record.odoo_payment_term_id.display_name))

    @api.constrains("instance_id", "default")
    def _check_single_default_per_instance(self):
        """
        Constraint to ensure that only one payment term can be marked as default per instance.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        for record in self:
            if not record.instance_id or not record.default:
                continue
            duplicate = self.search([
                ("id", "!=", record.id),
                ("instance_id", "=", record.instance_id.id),
                ("default", "=", True),
            ], limit=1)
            if duplicate:
                raise ValidationError(_(
                    "Only one payment term can be marked as Default per instance.\n"
                    "Instance: %s"
                ) % record.instance_id.display_name)

    def action_sync_shopify_payment_terms(self):
        """
        Action method to synchronize Shopify payment terms with Odoo.
        This method can be called from a button in the Odoo interface to trigger the synchronization process.
        :return: client action

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        instance_obj = self.env["shopify.instance.ept"]
        instances = instance_obj.search([
            ("use_graphql_api", "=", True),
            ("shopify_company_id", "=", self.env.company.id),
        ])
        if not instances:
            raise UserError(_("No Shopify instance found with GraphQL API enabled."))

        for instance in instances:
            if not instance.shopify_password or not instance.shopify_host:
                continue

            # get payment terms from Shopify
            client = instance.get_graphql_client()
            helper = PaymentTermsQueryHelper(client)
            result = helper.get_payment_terms()
            self._check_for_errors(result)
            payment_terms = helper.get_payment_terms_nodes(result)

            # Create or update payment terms in Odoo based on Shopify data
            for term in payment_terms:
                gid = term.get("id")
                extracted_id = client._extract_id_from_gid(gid)
                if not gid or not extracted_id:
                    continue
                values = {
                    "name": term.get("name", ""),
                    "instance_id": instance.id,
                    "payment_term_type": term.get("paymentTermsType", ""),
                    "shopify_payment_term_gid": gid,
                    "shopify_payment_term_id": client._extract_id_from_gid(gid) or 0,
                }
                existing_term = self.search(
                    [("shopify_payment_term_gid", "=", gid), ("instance_id", "=", instance.id)],
                    limit=1
                )
                if existing_term:
                    existing_term.write(values)
                else:
                    self.create(values)
        return {"type": "ir.actions.client", "tag": "reload"}

    @staticmethod
    def _check_for_errors(result: object):
        """
        Helper method to check for errors in the GraphQL response.
        :param result: response result

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if result.get("errors"):
            error_message = "\n".join(
                [str(error.get("message")) for error in result.get("errors", []) if error.get("message")]
            )
            raise UserError(_("Failed to sync Shopify payment terms.\nError: %s") % error_message)
