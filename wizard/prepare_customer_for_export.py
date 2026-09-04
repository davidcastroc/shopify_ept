import logging

from odoo.exceptions import UserError

from odoo import models, fields, _

_logger = logging.getLogger("Shopify Export Customer")


class PrepareCustomerForExport(models.TransientModel):
    """
    Model for exporting Odoo customers to Shopify.
    """
    _name = "shopify.prepare.customer.for.export.ept"
    _description = "Prepare customer for export in Shopify"

    shopify_instance_id = fields.Many2one("shopify.instance.ept", string="Shopify Instance", required=True)
    partner_ids = fields.Many2many("res.partner", string="Customers")

    def export_customer_to_shopify(self):
        """
        This method is used to export Odoo customers to Shopify.
        :return: client action

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not self.partner_ids:
            raise UserError(_("Please select at least one customer to export."))
        action_name = "shopify_ept.action_shopify_synced_customer_data"
        form_view_name = "shopify_ept.shopify_synced_customer_data_form_view_ept"

        instance = self.shopify_instance_id
        shopify_partner_ept = self.env['shopify.res.partner.ept']
        shopify_partners = shopify_partner_ept.search([('shopify_instance_id', '=', instance.id)]).partner_id
        partners = self.partner_ids.filtered(lambda p: p not in shopify_partners and not p.parent_id and p.email)
        process_import_export = self.env["shopify.process.import.export"].create({"shopify_instance_id": instance.id})
        queue_ids = process_import_export.create_export_customer_data_queues(partners)
        if queue_ids:
            action = self.env.ref(action_name).sudo().read()[0]
            form_view = self.sudo().env.ref(form_view_name)

            if len(queue_ids) == 1:
                action.update({"view_id": (form_view.id, form_view.name), "res_id": queue_ids[0],
                               "views": [(form_view.id, "form")]})
            else:
                action["domain"] = [("id", "in", queue_ids)]
            return action
        return {
            "type": "ir.actions.client",
            "tag": "reload",
        }
