# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
from odoo import models, fields, api, _, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.addons.html_editor.tools import get_video_embed_code
from .. import shopify


class ShopifyInstanceConfig(models.TransientModel):
    _name = "res.config.shopify.instance"
    _description = "Shopify Instance Configuration"

    name = fields.Char(help="Any User friendly name to identify the Shopify store")
    shopify_api_key = fields.Char("API Key", required=True, help="Shopify API Key. You can find "
                                                                 "it under Shopify store control "
                                                                 "panel.")
    shopify_password = fields.Char("Password", required=True, help="Shopify API Password. You can "
                                                                   "find it under Shopify store control panel")
    shopify_shared_secret = fields.Char("Secret Key", required=True, help="Shopify API Shared Secret. You can find it "
                                                                          "under Shopify store control panel")
    shopify_host = fields.Char("Host", required=True,
                               help="Add your shopify store URL, for example, https://my-shopify-store.myshopify.com")
    shopify_company_id = fields.Many2one("res.company", string="Store Company",
                                         help="Orders and Invoices will be generated of this company.")

    shopify_instance_video_url = fields.Char('Instance Video URL',
                                             default='https://www.youtube.com/watch?v=kWmHTIujBmQ&list=PLZGehiXauylZAowR8580_18UZUyWRjynd&index=2',
                                             help='URL of a video for showcasing by instance.')
    shopify_instance_video_embed_code = fields.Html(compute="_compute_shopify_instance_video_embed_code",
                                                    sanitize=False)

    shopify_api_video_url = fields.Char('API Video URL',
                                        default='https://www.youtube.com/watch?v=8QgZ4bp-7MA&list=PLZGehiXauylZAowR8580_18UZUyWRjynd&index=1&t=4s',
                                        help='URL of a video for showcasing by instance.')
    shopify_api_video_embed_code = fields.Html(compute="_compute_shopify_instance_video_embed_code",
                                               sanitize=False)

    @api.depends('shopify_instance_video_url', 'shopify_api_video_url')
    def _compute_shopify_instance_video_embed_code(self):
        for image in self:
            image.shopify_instance_video_embed_code = get_video_embed_code(image.shopify_instance_video_url)
            image.shopify_api_video_embed_code = get_video_embed_code(image.shopify_api_video_url)

    def create_pricelist(self, shop_currency):
        """
        This method creates pricelist from currency of the Shopify store.
        @author: Maulik Barad on Date 25-Sep-2020.
        @param shop_currency: Currency got from shopify store.
        """
        currency_obj = self.env["res.currency"]
        pricelist_obj = self.env["product.pricelist"]

        currency_id = currency_obj.search([("name", "=", shop_currency)], limit=1)

        if not currency_id:
            currency_id = currency_obj.search([("name", "=", shop_currency), ("active", "=", False)], limit=1)
            currency_id.write({"active": True})
        if not currency_id:
            currency_id = self.env.user.currency_id

        price_list_name = self.name + " " + "PriceList"
        pricelist = pricelist_obj.search([("name", "=", price_list_name),
                                          ("currency_id", "=", currency_id.id),
                                          ("company_id", "=", self.shopify_company_id.id)],
                                         limit=1)
        if not pricelist:
            pricelist = pricelist_obj.create({"name": price_list_name,
                                              "currency_id": currency_id.id,
                                              "company_id": self.shopify_company_id.id})

        return pricelist.id

    def shopify_test_connection(self):
        """This method used to verify whether Odoo is capable of connecting with Shopify store or not.
            @return : Action of type reload.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 04/10/2019.
        """
        instance_obj = self.env["shopify.instance.ept"]
        shopify_location_obj = self.env["shopify.location.ept"]
        payment_gateway_obj = self.env["shopify.payment.gateway.ept"]
        financial_status_obj = self.env["sale.auto.workflow.configuration.ept"]

        config = self.env['ir.config_parameter'].sudo()
        params = config.search(['|', ('key', 'ilike', 'shopify_pending_'),
                                ('key', 'ilike', 'shopify_client_')])
        params.unlink()

        instance_id = instance_obj.with_context(active_test=False).search(
            ["|", ("shopify_api_key", "=", self.shopify_api_key),
             ("shopify_host", "=", self.shopify_host)], limit=1)
        if instance_id:
            raise UserError(_(
                "An instance already exists for the given details \nShopify API key : '%s' \nShopify Host : '%s'" % (
                    self.shopify_api_key, self.shopify_host)))
        if not self.shopify_host.__contains__('myshopify') or not self.shopify_host.__contains__('https'):
            raise UserError(
                _("A host should contain both 'https' and 'myshopify', for example: 'https://odoo-v17.myshopify.com'. You can refer the host in your Shopify store: Shopify => Settings => Domains"))

        shop_url = instance_obj.prepare_shopify_shop_url(self.shopify_host, self.shopify_api_key, self.shopify_password)

        shopify.ShopifyResource.set_site(shop_url)

        try:
            shop_id = shopify.Shop.current()
        except Exception as error:
            raise UserError(error)

        shop_detail = shop_id.to_dict()

        vals = self.prepare_val_for_instance_creation(shop_detail)

        shopify_instance = instance_obj.create(vals)
        shopify_instance.create_update_correct_shipping_product()
        shopify_location_obj.import_shopify_locations(shopify_instance)

        payment_gateway_obj.import_payment_gateway(shopify_instance)
        financial_status_obj.create_financial_status(shopify_instance, "paid")

        self.shopify_create_analytic_plan_account(shopify_instance)

        if self.env.context.get('is_calling_from_onboarding_panel', False):
            company = shopify_instance.shopify_company_id
            shopify_instance.write({'is_instance_create_from_onboarding_panel': True})
            self.env['onboarding.onboarding.step'].sudo().action_validate_step(
                "shopify_ept.onboarding_onboarding_step_shopify")
            company.write({'is_create_shopify_more_instance': True})

        if self.env.context.get('from_oauth_flow'):
            action = self.env["ir.actions.act_window"]._for_xml_id("shopify_ept.action_shopify_instance_ept")
            if action:
                res = self.env.ref('shopify_ept.shopify_instance_form_view_ept', raise_if_not_found=False)
                action.update({
                    'res_id': shopify_instance.id,
                    'views': [(res and res.id or False, 'form')]
                })
                return action

        return {
            "type": "ir.actions.client",
            "tag": "reload",
        }

    def prepare_val_for_instance_creation(self, shop_detail):
        """ This method is used to prepare a vals for instance creation.
            :param shop_detail: Response of shopify.
            @return: vals
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 26 October 2020 .
            Task_id: 167537 - Code refactoring
        """
        warehouse_obj = self.env['stock.warehouse']
        ir_model_obj = self.env["ir.model.fields"]
        shop_currency = shop_detail.get("currency")
        warehouse = warehouse_obj.search([('company_id', '=', self.shopify_company_id.id)], limit=1, order='id')
        pricelist_id = self.create_pricelist(shop_currency)

        stock_field = ir_model_obj.search(
            [("model_id.model", "=", "product.product"), ("name", "=", "free_qty")],
            limit=1)
        vals = {
            "name": self.name,
            "shopify_api_key": self.shopify_api_key,
            "shopify_password": self.shopify_password,
            "shopify_shared_secret": self.shopify_shared_secret,
            "shopify_host": self.shopify_host,
            "shopify_company_id": self.shopify_company_id.id,
            "shopify_warehouse_id": warehouse.id,
            "shopify_store_time_zone": shop_detail.get("iana_timezone"),
            "shopify_pricelist_id": pricelist_id or False,
            "apply_tax_in_order": "create_shopify_tax",
            "shopify_stock_field": stock_field and stock_field.id or False
        }
        return vals

    @api.model
    def action_shopify_open_shopify_instance_wizard(self):
        """ Called by onboarding panel above the Instance."""
        action = self.env["ir.actions.actions"]._for_xml_id(
            "shopify_ept.shopify_on_board_instance_configuration_action")
        action['context'] = {'is_calling_from_onboarding_panel': True}
        instance = self.env['shopify.instance.ept'].search_shopify_instance()
        if instance:
            action.get('context').update({
                'default_name': instance.name,
                'default_shopify_host': instance.shopify_host,
                'default_shopify_api_key': instance.shopify_api_key,
                'default_shopify_password': instance.shopify_password,
                'default_shopify_shared_secret': instance.shopify_shared_secret,
                'default_shopify_company_id': instance.shopify_company_id.id,
                'is_already_instance_created': True,
            })
            company = instance.shopify_company_id
            # if company.shopify_instance_onboarding_state != 'done':
            #     company.set_onboarding_step_done('shopify_instance_onboarding_state')
        return action

    def reset_credentials(self):
        """
        This method set the new credentials and check if connection can be made properly.
        @author: Maulik Barad on Date 01-Oct-2020.
        """
        shopify_instance_obj = self.env["shopify.instance.ept"]
        context = self.env.context
        instance_id = context.get("shopify_instance_id")

        instance = shopify_instance_obj.browse(instance_id)
        if instance.shopify_api_key == self.shopify_api_key or instance.shopify_password == self.shopify_password or \
                instance.shopify_shared_secret == self.shopify_shared_secret:
            raise UserError(_("Entered credentials are same as previous.\nPlease verify the credentials once."))

        vals = {"shopify_api_key": self.shopify_api_key,
                "shopify_password": self.shopify_password,
                "shopify_shared_secret": self.shopify_shared_secret}
        instance.shopify_test_connection(vals)
        if context.get("test_connection"):
            return {"type": "ir.actions.client", "tag": "display_notification",
                    "params": {"title": "Shopify",
                               "message": "New Credentials are working properly!",
                               "sticky": False}}
        instance.write(vals)

        return True

    def shopify_create_analytic_plan_account(self, shopify_instance):
        """
        This method is use to create analytic plan and account
        @author: Haresh Mori on Date 03-Nov-2023.
        """
        if self.env.user.has_group('analytic.group_analytic_accounting') and shopify_instance:
            if not shopify_instance.shopify_analytic_account_id:
                analytic_account_plan = self.shopify_search_or_create_analytic_account_plan()
                analytic_account_id = self.shopify_search_or_create_analytic_account(shopify_instance.name,
                                                                                     analytic_account_plan)
            else:
                analytic_account_id = shopify_instance.shopify_analytic_account_id
            shopify_instance.write({'shopify_analytic_account_id': analytic_account_id.id})
        return True

    def shopify_search_or_create_analytic_account_plan(self):
        """
        Define this method for search or create Amazon analytic plan.
        :return: account.analytic.plan()
        """
        analytic_account_plan_obj = self.env['account.analytic.plan']
        analytic_account_plan = analytic_account_plan_obj.search([('name', '=', 'Shopify')], limit=1)
        if not analytic_account_plan:
            analytic_account_plan = analytic_account_plan_obj.create({'name': 'Shopify'})
        return analytic_account_plan

    def shopify_search_or_create_analytic_account(self, account_name, analytic_account_plan):
        """
        Define this method for search or create analytic account.
        :param: account_name: str
        :param: analytic_account_plan: account.analytic.plan()
        :return: account.analytic.account()
        """
        analytic_account_obj = self.env['account.analytic.account']
        analytic_account = analytic_account_obj.search([('name', '=', account_name),
                                                        ('plan_id', '=', analytic_account_plan.id),
                                                        ('company_id', '=', self.env.user.company_id.id)], limit=1)
        if not analytic_account:
            analytic_account = analytic_account_obj.create({'name': account_name, 'plan_id': analytic_account_plan.id})
        return analytic_account


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    def _get_shopify_default_financial_statuses(self):
        if self.env.context.get('default_shopify_instance_id', False):
            financial_status_ids = self.env['sale.auto.workflow.configuration.ept'].search(
                [('shopify_instance_id', '=', self.env.context.get('default_shopify_instance_id', False))]).ids
            return [(6, 0, financial_status_ids)]

    @api.model
    def _default_buy_with_prime_tag_ids(self):
        """ Set default tag for buy with prime order.
            @author: Nilam Kubavat @Emipro Technologies Pvt. Ltd on date 18 December 2023.
            Task_id: 4726
        """
        tag_ids = self.env.ref('shopify_ept.shopify_product_tag_buy_with_prime')
        return [(6, 0, [tag_ids.id])] if tag_ids else False

    # @api.model
    # def _get_default_return_location(self):
    #     shopify_location_obj = self.env['shopify.location.ept']
    #     shopify_warehouse = shopify_location_obj.search([]).warehouse_for_order
    #     locations = self.env['stock.location'].search([('location_id', 'child_of', shopify_warehouse.lot_stock_id.ids)])
    #     return [('id', 'in', locations.ids)]

    shopify_instance_id = fields.Many2one("shopify.instance.ept", "Shopify Store",
                                          help="The Shopify store you are configuring. "
                                               "All settings on this page apply only to the selected store.")
    shopify_config_tab = fields.Selection([
        ('all', 'All'),
        ('global', 'Global'),
        ('general', 'General'),
        ('product', 'Product'),
        ('order', 'Order'),
        ('tax', 'Tax'),
        ('inventory', 'Inventory'),
        ('webhook', 'Webhook'),
        ('activities', 'Activities & Financials'),
    ], string="Configuration Section", default='all')

    @api.onchange('shopify_instance_id')
    def _onchange_shopify_instance_id_reset_tab(self):
        """Reset configuration tab to 'All' whenever the active instance changes."""
        self.shopify_config_tab = 'all'

    shopify_company_id = fields.Many2one("res.company", string="Store Company",
                                         help="Company that owns this Shopify store. All orders, invoices, and stock moves are created under this company.")
    shopify_warehouse_id = fields.Many2one("stock.warehouse", string="Shopify Warehouse",
                                           domain="[('company_id', '=',shopify_company_id)]",
                                           help="All sales orders imported from this store are routed to this warehouse. "
                                                "Stock availability checks also use this warehouse.")
    auto_import_product = fields.Boolean(string="Auto-Create Missing Products",
                                         help="When enabled: a new Odoo product is created automatically if no match is found by SKU or barcode during import. "
                                              "When disabled: unmatched products cause the queue line to fail, prompting manual product mapping. "
                                              "Use with care on multi-channel setups to avoid duplicate products.")
    shopify_sync_product_with = fields.Selection([("sku", "Internal Reference (SKU)"), ("barcode", "Barcode"),
                                                  ("sku_or_barcode", "Internal Reference (SKU) and Barcode")],
                                                 string="Product Matching Method", default="sku",
                                                 help="How the connector identifies existing Odoo products during import:\n"
                                                      "• Internal Reference (SKU): matches on the product's Internal Reference field.\n"
                                                      "• Barcode: matches on the EAN/barcode field.\n"
                                                      "• SKU and Barcode: tries both, uses the first match found.")
    shopify_pricelist_id = fields.Many2one("product.pricelist", string="Shopify Pricelist",
                                           help="Used for product price import and export. "
                                                "Also applied to imported orders when the order currency matches the pricelist currency.")
    shopify_compare_pricelist_id = fields.Many2one("product.pricelist", string="Shopify Compare At Pricelist",
                                                   help="Populates the 'Compare At Price' (strike-through price) field during product import and export. "
                                                        "Useful for displaying original prices alongside sale prices in your Shopify store.")
    shopify_stock_field = fields.Many2one("ir.model.fields", string="Stock Quantity Field",
                                          help="Determines which Odoo quantity is pushed to Shopify during inventory sync:\n"
                                               "• Forecast Qty: On Hand − Outgoing + Incoming (includes future moves).\n"
                                               "• Free to Use: On Hand − Reserved (physically available stock only).\n"
                                               "Choose based on how conservatively you want to publish available stock.")
    shopify_import_stock_for_bom_products = fields.Boolean(
        string="Import Stock for BOM Products",
        help="If checked, stock will be imported/updated from Shopify for products having a Bill of "
             "Materials of type 'Manufacture this product' or 'Subcontracting'.\n"
             "This setting has no effect on Kit type BOMs: a kit's stock is always auto-computed from "
             "its components in Odoo and cannot be set directly, so kit products are always skipped "
             "regardless of this setting.")
    shopify_section_id = fields.Many2one("crm.team", string="Shopify Sales Team",
                                         help="Default sales team assigned to all orders imported from this store. "
                                              "Used for sales reporting, performance tracking, and team-based routing.")
    shopify_is_use_default_sequence = fields.Boolean(string="Use Odoo Default Sequence",
                                                     help="When enabled: Odoo's standard sale order numbering (e.g. S00001) is used for all Shopify orders. "
                                                          "When disabled: a custom prefix is prepended to the Shopify order number, "
                                                          "keeping order references traceable back to the Shopify dashboard.")
    shopify_order_prefix = fields.Char(size=10, string="Order Number Prefix",
                                       help="Prefix added to Shopify order numbers in Odoo (e.g. 'SHO-' produces 'SHO-1001'). "
                                            "Helps distinguish Shopify orders from orders placed directly in Odoo.")
    shopify_apply_tax_in_order = fields.Selection(
        [("odoo_tax", "Odoo Default Tax Behaviour"), ("create_shopify_tax", "Create New Tax If Not Found")],
        copy=False, default="create_shopify_tax",
        string="Tax Application Method",
        help="Controls how taxes are applied to imported Shopify orders:\n"
             "• Odoo Default: taxes are set by Odoo's fiscal position rules — no new taxes are created.\n"
             "• Create New Tax: Shopify tax data is searched in Odoo; a new tax is auto-created if not found.\n"
             "Impact: 'Create New Tax' requires configuring the Invoice and Credit Note tax accounts below.")
    shopify_order_export_tax_policy = fields.Selection(
        [("shopify_tax", "Use Shopify Tax (Recalculate in Shopify)"),
         ("odoo_tax", "Use Odoo Tax (Fixed Amounts)")],
        copy=False, default="shopify_tax",
        string="Order Export Tax Policy",
        help="Controls how tax amounts are sent when exporting orders to Shopify:\n"
             "• Use Shopify Tax: only untaxed unit price and quantity are exported; Shopify recalculates tax. "
             "Best when Shopify manages tax rules.\n"
             "• Use Odoo Tax: fixed tax amounts from Odoo are exported and treated as final in Shopify. "
             "Best when Odoo is the tax authority and Shopify must not override amounts.")
    shopify_invoice_tax_account_id = fields.Many2one("account.account", string="Shopify Invoice Tax Account",
                                                     help="Account used as the tax account on invoices for Shopify-created taxes. "
                                                          "Required when the Tax Application Method is set to 'Create New Tax'.")
    shopify_credit_tax_account_id = fields.Many2one("account.account", string="Shopify Credit Note Tax Account",
                                                    help="Account used as the tax account on credit notes for Shopify-created taxes. "
                                                         "Required when the Tax Application Method is set to 'Create New Tax'.")
    shopify_notify_customer = fields.Boolean(string="Notify Customer on Status Change",
                                             help="When enabled: customers receive an email notification whenever their order status is updated from Odoo. "
                                                  "When disabled: status updates happen silently with no outbound customer communication.")
    shopify_user_ids = fields.Many2many("res.users", "shopify_res_config_settings_res_users_rel",
                                        "res_config_settings_id", "res_users_id",
                                        string="Responsible Users",
                                        help="Activities created on sync errors are assigned to these users. "
                                             "At least one user is required when error activity creation is enabled.")
    shopify_activity_type_id = fields.Many2one("mail.activity.type", string="Shopify Activity Type",
                                               help="Type of activity created on sync errors (e.g. To-Do, Email, Phone Call). "
                                                    "Choose the type that matches your team's standard follow-up workflow.")
    shopify_date_deadline = fields.Integer(string="Deadline Lead Days", default=1,
                                           help="Number of days from today used to calculate the activity deadline. "
                                                "Example: a value of 1 means activities are due tomorrow.")
    is_shopify_create_schedule = fields.Boolean(string="Create Activity on Error", default=False,
                                                help="When enabled: a scheduled activity is automatically created whenever a sync error occurs "
                                                     "(queue line failure, export error, stock update failure). "
                                                     "Ensures assigned users are alerted before issues slip through. "
                                                     "When disabled: errors are recorded in logs only.")
    shopify_sync_product_with_images = fields.Boolean(string="Sync Product Images", default=False,
                                                      help="When enabled: product images are downloaded and attached to Odoo products during import. "
                                                           "When disabled: products are imported without images, making the initial sync significantly faster.")
    shopify_sync_product_weight = fields.Boolean(string="Sync Product Weight", default=True,
                                                help="When enabled: the product weight is synced from Shopify to Odoo while importing or updating products. "
                                                     "When disabled: the weight received from Shopify is ignored.")
    create_shopify_products_webhook = fields.Boolean(string="Sync Products via Webhook",
                                                     help="When enabled: Shopify registers webhooks for all product events (create, update, delete). "
                                                          "Product changes in Shopify are pushed to Odoo in real time. "
                                                          "When disabled: all product webhooks are deactivated; changes must be synced manually.")
    create_shopify_customers_webhook = fields.Boolean(string="Sync Customers via Webhook",
                                                      help="When enabled: customer create/update events are pushed from Shopify to Odoo immediately. "
                                                           "When disabled: customer records must be imported manually or via a scheduled sync.")
    create_shopify_orders_webhook = fields.Boolean(string="Sync Orders via Webhook",
                                                   help="When enabled: order events (created, paid, fulfilled, refunded) are pushed from Shopify to Odoo in real time. "
                                                        "When disabled: orders must be imported using the manual import action or a scheduled cron job.")
    shopify_default_pos_customer_id = fields.Many2one("res.partner", string="Shopify Default POS Customer",
                                                      help="Assigned to POS orders from Shopify that have no associated customer record. "
                                                           "Prevents import failures and keeps anonymous POS orders trackable in Odoo.",
                                                      domain="[('customer_rank','>', 0)]")
    shopify_settlement_report_journal_id = fields.Many2one("account.journal", string="Shopify Payout Report Journal",
                                                           help="Bank or cash journal used when generating bank statements from Shopify Payout Reports. "
                                                                "Applies only to the Shopify Payments payment method.")
    shopify_payout_last_date_import = fields.Date(string="Last Payout Import Date",
                                                  help="Tracks the date of the last successfully imported Shopify Payout Report. "
                                                       "Updated automatically after each import run.")
    shopify_financial_status_ids = fields.Many2many('sale.auto.workflow.configuration.ept',
                                                    'shopify_sale_auto_workflow_conf_rel',
                                                    'financial_onboarding_status_id', 'workflow_id',
                                                    string='Financial Status Workflows',
                                                    default=_get_shopify_default_financial_statuses,
                                                    help="Auto-workflow rules that determine how imported orders are processed "
                                                         "(confirm, invoice, pay) based on their Shopify payment status. "
                                                         "Configure these from Shopify › Configuration › Financial Status.")
    shopify_set_sales_description_in_product = fields.Boolean(
        string="Use Odoo Sales Description",
        config_parameter="shopify_ept.set_sales_description",
        help="When enabled: the Odoo product's Sales Description field is synced to/from Shopify as the product description. "
             "Note: Shopify sends HTML; Odoo stores plain text — rich formatting may be stripped on import. "
             "Applies to all instances.")
    shopify_order_status_ids = fields.Many2many('import.shopify.order.status',
                                                'shopify_config_settings_order_status_rel',
                                                'shopify_config_id', 'status_id',
                                                string="Import Order Status",
                                                help="Only Shopify orders carrying one of these statuses are imported into Odoo. "
                                                     "Typical choice: 'open' or 'any'. Restricting to specific statuses reduces queue volume.")
    auto_fulfill_gift_card_order = fields.Boolean(
        string="Auto-Fulfill Gift Card Line", default=True,
        help="When enabled: the connector automatically fulfills gift card line items on orders that contain only gift cards, "
             "matching Shopify's native auto-fulfillment behaviour (Settings › Checkout › Order processing). "
             "When disabled: gift card fulfillment must be triggered manually from Odoo.")

    shopify_import_order_after_date = fields.Datetime(
        string="Import Orders After Date",
        help="Only orders created after this date/time are imported from Shopify. "
             "Set this on first setup to avoid pulling in years of historical data. "
             "Leave blank to import all available orders.")

    # Analytic
    shopify_is_use_analytic_account = fields.Boolean(
        string="Enable Analytic Accounting",
        help="When enabled: an analytic account is assigned to all sales orders and invoices created by this connector. "
             "Note: Odoo's Analytic Default Rules will not apply to Shopify invoices when this is active.")
    shopify_analytic_account_id = fields.Many2one('account.analytic.account', string='Shopify Analytic Account',
                                                  domain="['|', ('company_id', '=', False), ('company_id', '=', shopify_company_id)]",
                                                  help="Analytic account applied to all Shopify sales orders and invoices for this store. "
                                                       "Used for per-store profitability tracking in Odoo's analytic reports.")
    # shopify_analytic_tag_ids = fields.Many2many('account.analytic.tag', 'shopify_res_config_analytic_account_tag_rel',
    #                                             string='Shopify Analytic Tags',
    #                                             domain="['|', ('company_id', '=', False),
    #                                             ('company_id', '=', shopify_company_id)]")
    shopify_lang_id = fields.Many2one('res.lang', string='Store Language',
                                      help="Language applied when creating new contacts from Shopify. "
                                           "Also used for translating product content where applicable.")
    # presentment currency
    order_visible_currency = fields.Boolean(string="Import in Customer's Currency",
                                            help="When enabled: orders are imported using the currency the customer actually paid in "
                                                 "(presentment currency), not the store's base currency. "
                                                 "When disabled: the store's base currency is always used. "
                                                 "Affects how amounts are stored and displayed on the sale order.")

    is_delivery_fee = fields.Boolean(string='Colorado Delivery Fee (US Only)',
                                     help="Enable only for stores selling to Colorado, USA. "
                                          "When enabled: a delivery fee line is added to qualifying Colorado orders per state tax rules. "
                                          "The fee label below must exactly match the label shown on Shopify orders.")
    delivery_fee_name = fields.Char(string='Colorado Fee Label',
                                    help="Exact text of the delivery fee as it appears on Shopify orders for Colorado. "
                                         "A mismatch will cause the fee line to be skipped during import.")

    show_net_profit_report = fields.Boolean(config_parameter="shopify_ept.show_net_profit_report")
    skip_api_logs = fields.Boolean(string="Skip REST API Logs",
                                   config_parameter="shopify_ept.skip_api_logs",
                                   help="When enabled: suppresses INFO-level pyactiveresource HTTP log lines from Shopify REST API calls. "
                                        "ERROR and WARNING entries are always preserved. GraphQL-based instances are unaffected.")
    is_shopify_digest = fields.Boolean(
        string="Send Periodic KPI Digest",
        help="When enabled: a periodic summary email is sent with key Shopify KPIs (orders, revenue, etc.) for this store. "
             "Configure the send frequency from Accounting › Reporting › Digest Emails.")
    is_delivery_multi_warehouse = fields.Boolean(string="Multi-Warehouse Delivery",
                                                 help="When enabled: Shopify fulfillment status is updated based on deliveries across multiple Odoo warehouses "
                                                      "(e.g. split-shipment orders). "
                                                      "When disabled: only the primary instance warehouse is considered for fulfillment status updates.")
    import_customer_as_company = fields.Boolean(string="Import Customer as Company",
                                                help="When enabled: if a customer provides a company name, a Company-type partner is created in Odoo "
                                                     "and the individual is linked as a contact underneath it. "
                                                     "When disabled: all customers are created as individual contacts regardless of company name.")
    shopify_product_uom_id = fields.Many2one('uom.uom', string='Weight Unit of Measure',
                                             help="Unit of measure used to convert Shopify product weight values to Odoo. "
                                                  "Ensure this matches your Shopify store's weight unit (kg, lb, g, oz) "
                                                  "to avoid incorrect weights on delivery labels.")
    use_default_terms_and_condition_of_odoo = fields.Boolean(
        string="Use Odoo Default Terms & Conditions",
        config_parameter="shopify_ept.use_default_terms_and_condition_of_odoo",
        help="When enabled: Odoo's default Terms & Conditions text is added to the order note on all imported Shopify orders. "
             "When disabled: the order note is populated directly from the Shopify order response.")
    ship_order_webhook = fields.Boolean(string="Auto-Ship Orders",
                                        help="When enabled: as soon as Shopify marks an order as shipped, the corresponding Odoo delivery order is automatically validated. "
                                             "When disabled: delivery validation must be performed manually in Odoo.")
    forcefully_reserve_stock_webhook = fields.Boolean(string="Force Transfer on Auto-Ship",
                                                      help="When enabled: the delivery is force-validated even when on-hand stock is insufficient. "
                                                           "Caution: not suitable for Lot/Serial number tracked products — bypasses traceability checks.")
    refund_order_webhook = fields.Boolean(string="Auto-Refund Orders",
                                          help="When enabled: a credit note is automatically created in Odoo when a refund webhook is received from Shopify. "
                                               "When disabled: refunds must be processed manually in Odoo.")
    customer_order_webhook = fields.Boolean(string="Update Customer Info on Orders",
                                            help="When enabled: customer details (name, address, email) on open Odoo orders are automatically updated when changed in Shopify. "
                                                 "Not applied to orders that are already shipped or invoiced.")
    update_qty_order_webhook = fields.Boolean(string="Update Order Line Quantities",
                                              help="When enabled: if quantities are manually changed on a Shopify order, the corresponding Odoo order lines are updated automatically. "
                                                   "Not applied to orders that are already shipped.")
    add_new_product_order_webhook = fields.Boolean(string="Add New Products to Existing Orders",
                                                   help="When enabled: if a product is added to an existing Shopify order, a new order line is automatically added in Odoo. "
                                                        "When disabled: post-order product additions from Shopify are ignored.")
    import_buy_with_prime_shopify_order = fields.Boolean(string="Enable Buy with Prime Orders",
                                                         help="When enabled: orders placed through Amazon 'Buy with Prime' and flowing through Shopify are imported into Odoo "
                                                              "and routed to the dedicated Buy with Prime warehouse.")
    buy_with_prime_warehouse_id = fields.Many2one("stock.warehouse", string="Buy with Prime Warehouse",
                                                  domain="[('company_id', '=',shopify_company_id)]",
                                                  help="Dedicated warehouse for Buy with Prime orders. "
                                                       "Keep separate from your standard Shopify warehouse for clean inventory and fulfillment separation.")
    buy_with_prime_tag_ids = fields.Many2many("shopify.tags", "buy_with_prime_shopify_tags_rel",
                                              "product_tmpl_id", "tag_id",
                                              string="Buy with Prime Order Tags",
                                              default=_default_buy_with_prime_tag_ids,
                                              help="Only Shopify orders carrying all of these tags are treated as Buy with Prime orders. "
                                                   "The default tag is pre-configured; add more tags if your store uses custom tagging.")
    Force_transfer_move_of_buy_with_prime_orders = fields.Boolean(
        string="Force Transfer for Buy with Prime",
        help="When enabled: stock moves for Buy with Prime orders are force-validated even if on-hand inventory is insufficient. "
             "Amazon handles physical fulfillment, so Odoo stock may not reflect actual availability.")
    return_picking_order = fields.Boolean(string="Auto-Return on Refund",
                                          help="When enabled: a stock return picking is automatically created in Odoo when a Shopify refund triggers a credit note. "
                                               "When disabled: returns must be created manually by warehouse staff.")
    stock_validate_for_return = fields.Boolean(string="Auto-Validate Return",
                                               help="When enabled: the return picking is automatically validated after it is created via webhook. "
                                                    "When disabled: the return is created in draft and must be reviewed and validated manually.")
    # return_location_id = fields.Many2one('stock.location', 'Return Location',
    #                                      domain=lambda self: self._get_default_return_location())
    update_qty_to_invoice_order_webhook = fields.Boolean(
        string="Auto Invoice/Credit Note on Qty Change",
        help="When enabled: an invoice or credit note is automatically generated in Odoo when order line quantities are updated via webhook. "
             "When disabled: invoicing after quantity changes must be handled manually.")
    credit_note_register_payment = fields.Boolean(
        string="Register Credit Note Payment",
        help="When enabled: a payment is automatically registered on the credit note created via webhook, "
             "using the journal configured below. "
             "When disabled: payment registration on credit notes must be done manually.")
    credit_note_payment_journal = fields.Many2one("account.journal", string="Shopify Credit Note Payment Journal",
                                                  help="Bank or cash journal used when automatically registering payment on credit notes. "
                                                       "Must belong to the same company as this Shopify instance.")

    auto_create_product_category = fields.Boolean(string="Auto-Create Product Category", default=True,
                                                  help="When enabled: product categories found in Shopify are automatically created in Odoo if they don't already exist. "
                                                       "When disabled: a single fallback category (configured below) is assigned to all imported products.")

    shopify_instance_product_category = fields.Many2one("product.category", string="Shopify Default Product Category",
                                                        help="Fallback category assigned to all imported Shopify products when auto-create is disabled. "
                                                             "Useful for keeping your product catalogue organised from the start.")

    shopify_tax_grid_id = fields.Many2one(comodel_name='account.account.tag', string="Shopify Tax Grid",
                                          ondelete='restrict',
                                          domain=[('applicability', '=', 'taxes')],
                                          help="Tax repartition line tag applied when new taxes are auto-created from Shopify data. "
                                               "Ensures newly created taxes are correctly categorised in tax reports.")
    is_create_tax_group = fields.Boolean(string="Auto-Create Tax Group", default=False, copy=False,
                                         help="When enabled: a Tax Group is also created in Odoo whenever a new tax is auto-generated from Shopify data. "
                                              "Tax Groups are used to group related taxes together on invoices and in tax reports.")
    auto_sync_product_tag = fields.Boolean(string="Auto-Create Product Tags", default=False,
                                           help="When enabled: product tags found in Shopify are automatically created in Odoo during product import. "
                                                "When disabled: unrecognised tags are silently skipped.")

    tax_group_payable_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Shopify Tax Group Payable Account',
        help="Counterpart account for the Tax Closing Entry when the tax balance is in favour of the tax authorities "
             "(i.e. your company owes tax to the government).")
    tax_group_receivable_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Shopify Tax Group Receivable Account',
        help="Counterpart account for the Tax Closing Entry when the tax balance is in favour of your company "
             "(i.e. your company is owed a tax refund).")

    is_shopify_market_enabled = fields.Boolean(
        string="Enable Shopify Markets",
        help="When enabled: the Shopify Markets feature is active for this store. "
             "Markets allow you to sell to multiple regions with different currencies, languages, "
             "and pricing rules configured per market directly in Shopify.")



    @api.onchange("shopify_instance_id")
    def onchange_shopify_instance_id(self):
        instance = self.shopify_instance_id or False
        if instance:
            self.shopify_company_id = instance.shopify_company_id and instance.shopify_company_id.id or False
            self.shopify_warehouse_id = instance.shopify_warehouse_id and instance.shopify_warehouse_id.id or False
            self.auto_import_product = instance.auto_import_product or False
            self.shopify_sync_product_with = instance.shopify_sync_product_with
            self.shopify_pricelist_id = instance.shopify_pricelist_id and instance.shopify_pricelist_id.id or False
            self.shopify_compare_pricelist_id = instance.shopify_compare_pricelist_id and instance.shopify_compare_pricelist_id.id or False
            self.shopify_stock_field = instance.shopify_stock_field and instance.shopify_stock_field.id or False
            self.shopify_import_stock_for_bom_products = instance.shopify_import_stock_for_bom_products or False
            self.shopify_section_id = instance.shopify_section_id.id or False
            self.shopify_order_prefix = instance.shopify_order_prefix
            self.shopify_is_use_default_sequence = instance.is_use_default_sequence
            self.shopify_apply_tax_in_order = instance.apply_tax_in_order
            self.shopify_order_export_tax_policy = instance.shopify_order_export_tax_policy
            self.shopify_invoice_tax_account_id = instance.invoice_tax_account_id and \
                                                  instance.invoice_tax_account_id.id or False
            self.shopify_credit_tax_account_id = instance.credit_tax_account_id and \
                                                 instance.credit_tax_account_id.id or False
            self.shopify_notify_customer = instance.notify_customer
            self.shopify_user_ids = instance.shopify_user_ids or False
            self.shopify_activity_type_id = instance.shopify_activity_type_id or False
            self.shopify_date_deadline = instance.shopify_date_deadline or False
            self.is_shopify_create_schedule = instance.is_shopify_create_schedule or False
            self.shopify_sync_product_with_images = instance.sync_product_with_images or False
            self.shopify_sync_product_weight = instance.shopify_sync_product_weight or False
            self.create_shopify_products_webhook = instance.create_shopify_products_webhook
            self.create_shopify_customers_webhook = instance.create_shopify_customers_webhook
            self.create_shopify_orders_webhook = instance.create_shopify_orders_webhook
            self.shopify_default_pos_customer_id = instance.shopify_default_pos_customer_id
            self.shopify_payout_last_date_import = instance.payout_last_import_date or False
            self.shopify_settlement_report_journal_id = instance.shopify_settlement_report_journal_id or False
            self.shopify_order_status_ids = instance.shopify_order_status_ids.ids
            # self.auto_fulfill_gift_card_order = instance.auto_fulfill_gift_card_order
            self.shopify_import_order_after_date = instance.import_order_after_date or False
            self.shopify_is_use_analytic_account = instance.shopify_is_use_analytic_account or False
            self.shopify_analytic_account_id = instance.shopify_analytic_account_id.id or False
            # self.shopify_analytic_tag_ids = instance.shopify_analytic_tag_ids.ids
            self.shopify_lang_id = instance.shopify_lang_id and instance.shopify_lang_id.id or False
            self.order_visible_currency = instance.order_visible_currency or False
            self.is_shopify_digest = instance.is_shopify_digest or False
            self.is_delivery_fee = instance.is_delivery_fee or False
            self.delivery_fee_name = instance.delivery_fee_name
            self.is_delivery_multi_warehouse = instance.is_delivery_multi_warehouse or False
            self.import_customer_as_company = instance.import_customer_as_company or False
            self.shopify_product_uom_id = instance.shopify_product_uom_id and instance.shopify_product_uom_id.id or False
            self.ship_order_webhook = instance.ship_order_webhook
            self.forcefully_reserve_stock_webhook = instance.forcefully_reserve_stock_webhook
            self.refund_order_webhook = instance.refund_order_webhook
            self.customer_order_webhook = instance.customer_order_webhook
            self.update_qty_order_webhook = instance.update_qty_order_webhook
            self.add_new_product_order_webhook = instance.add_new_product_order_webhook
            self.import_buy_with_prime_shopify_order = instance.import_buy_with_prime_shopify_order
            self.buy_with_prime_warehouse_id = instance.buy_with_prime_warehouse_id and instance.buy_with_prime_warehouse_id.id or False
            self.Force_transfer_move_of_buy_with_prime_orders = instance.Force_transfer_move_of_buy_with_prime_orders
            self.buy_with_prime_tag_ids = instance.buy_with_prime_tag_ids.ids
            self.return_picking_order = instance.return_picking_order
            self.stock_validate_for_return = instance.stock_validate_for_return
            # self.return_location_id = instance.return_location_id and instance.return_location_id.id or False
            self.update_qty_to_invoice_order_webhook = instance.update_qty_to_invoice_order_webhook
            self.credit_note_register_payment = instance.credit_note_register_payment or False
            self.credit_note_payment_journal = instance.credit_note_payment_journal or False
            self.auto_create_product_category = instance.auto_create_product_category or False
            self.shopify_instance_product_category = instance.shopify_instance_product_category or False
            self.shopify_tax_grid_id = instance.shopify_tax_grid_id or False
            self.is_create_tax_group = instance.is_create_tax_group or False
            self.tax_group_payable_account_id = instance.tax_group_payable_account_id or False
            self.tax_group_receivable_account_id = instance.tax_group_receivable_account_id or False
            self.auto_sync_product_tag = instance.auto_sync_product_tag or False
            self.is_shopify_market_enabled = instance.is_shopify_market_enabled or False

    def execute(self):
        """This method used to set value in an instance of configuration.
            @param : self
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 04/10/2019.
        """
        instance = self.shopify_instance_id
        values = {}
        res = super(ResConfigSettings, self).execute()
        IrModule = self.env['ir.module.module']
        exist_module = IrModule.search([('name', '=', 'shopify_net_profit_report_ept'), ('state', '=', 'installed')])
        if instance:
            values["shopify_warehouse_id"] = self.shopify_warehouse_id and self.shopify_warehouse_id.id or False
            values["auto_import_product"] = self.auto_import_product or False
            values["shopify_sync_product_with"] = self.shopify_sync_product_with
            values["shopify_pricelist_id"] = self.shopify_pricelist_id and self.shopify_pricelist_id.id or False
            values[
                "shopify_compare_pricelist_id"] = self.shopify_compare_pricelist_id and self.shopify_compare_pricelist_id.id or False
            values["shopify_stock_field"] = self.shopify_stock_field and self.shopify_stock_field.id or False
            values["shopify_import_stock_for_bom_products"] = self.shopify_import_stock_for_bom_products or False
            values["shopify_section_id"] = self.shopify_section_id and self.shopify_section_id.id or False
            values["shopify_order_prefix"] = self.shopify_order_prefix
            values["is_use_default_sequence"] = self.shopify_is_use_default_sequence
            values["apply_tax_in_order"] = self.shopify_apply_tax_in_order
            values["shopify_order_export_tax_policy"] = self.shopify_order_export_tax_policy
            values["invoice_tax_account_id"] = self.shopify_invoice_tax_account_id and \
                                               self.shopify_invoice_tax_account_id.id or False
            values["credit_tax_account_id"] = self.shopify_credit_tax_account_id and \
                                              self.shopify_credit_tax_account_id.id or False
            values["notify_customer"] = self.shopify_notify_customer
            values["shopify_activity_type_id"] = self.shopify_activity_type_id and self.shopify_activity_type_id.id \
                                                 or False
            values["shopify_date_deadline"] = self.shopify_date_deadline or False
            values.update({"shopify_user_ids": [(6, 0, self.shopify_user_ids.ids)]})
            values["is_shopify_create_schedule"] = self.is_shopify_create_schedule
            values["sync_product_with_images"] = self.shopify_sync_product_with_images or False
            values["shopify_sync_product_weight"] = self.shopify_sync_product_weight or False
            values["create_shopify_products_webhook"] = self.create_shopify_products_webhook
            values["create_shopify_customers_webhook"] = self.create_shopify_customers_webhook
            values["create_shopify_orders_webhook"] = self.create_shopify_orders_webhook
            values["shopify_default_pos_customer_id"] = self.shopify_default_pos_customer_id.id
            values["payout_last_import_date"] = self.shopify_payout_last_date_import or False
            values["shopify_settlement_report_journal_id"] = self.shopify_settlement_report_journal_id or False
            values['shopify_order_status_ids'] = [(6, 0, self.shopify_order_status_ids.ids)]
            # values["auto_fulfill_gift_card_order"] = self.auto_fulfill_gift_card_order
            values["import_order_after_date"] = self.shopify_import_order_after_date
            values["shopify_is_use_analytic_account"] = self.shopify_is_use_analytic_account or False
            values["shopify_analytic_account_id"] = self.shopify_analytic_account_id and \
                                                    self.shopify_analytic_account_id.id or False
            # values["shopify_analytic_tag_ids"] = [(6, 0, self.shopify_analytic_tag_ids.ids)]
            values['shopify_lang_id'] = self.shopify_lang_id and self.shopify_lang_id.id or False
            values['order_visible_currency'] = self.order_visible_currency or False
            values['is_shopify_digest'] = self.is_shopify_digest or False
            values["is_delivery_fee"] = self.is_delivery_fee
            values["delivery_fee_name"] = self.delivery_fee_name
            values["is_delivery_multi_warehouse"] = self.is_delivery_multi_warehouse or False
            values["import_customer_as_company"] = self.import_customer_as_company or False
            values['shopify_product_uom_id'] = self.shopify_product_uom_id and self.shopify_product_uom_id.id or False
            values['ship_order_webhook'] = self.ship_order_webhook
            values['forcefully_reserve_stock_webhook'] = self.forcefully_reserve_stock_webhook
            values['refund_order_webhook'] = self.refund_order_webhook
            values['customer_order_webhook'] = self.customer_order_webhook
            values['update_qty_order_webhook'] = self.update_qty_order_webhook
            values['add_new_product_order_webhook'] = self.add_new_product_order_webhook
            values['import_buy_with_prime_shopify_order'] = self.import_buy_with_prime_shopify_order
            values[
                "buy_with_prime_warehouse_id"] = self.buy_with_prime_warehouse_id and self.buy_with_prime_warehouse_id.id or False
            values['Force_transfer_move_of_buy_with_prime_orders'] = self.Force_transfer_move_of_buy_with_prime_orders
            values['buy_with_prime_tag_ids'] = [(6, 0, self.buy_with_prime_tag_ids.ids)]
            values['return_picking_order'] = self.return_picking_order
            values['stock_validate_for_return'] = self.stock_validate_for_return
            # values["return_location_id"] = self.return_location_id and self.return_location_id.id or False
            values['update_qty_to_invoice_order_webhook'] = self.update_qty_to_invoice_order_webhook
            values['credit_note_register_payment'] = self.credit_note_register_payment
            values['credit_note_payment_journal'] = self.credit_note_payment_journal or False
            values['auto_create_product_category'] = self.auto_create_product_category or False
            values['shopify_instance_product_category'] = self.shopify_instance_product_category or False
            values['shopify_tax_grid_id'] = self.shopify_tax_grid_id or False
            values['is_create_tax_group'] = self.is_create_tax_group or False
            values['tax_group_payable_account_id'] = self.tax_group_payable_account_id or False
            values['tax_group_receivable_account_id'] = self.tax_group_receivable_account_id or False
            values['auto_sync_product_tag'] = self.auto_sync_product_tag or False
            values['is_shopify_market_enabled'] = self.is_shopify_market_enabled or False

            # added this condition to set analytic account based on configuration.
            if not self.shopify_is_use_analytic_account:
                values["shopify_analytic_account_id"] = False

            # added this condition to set tax grid based on configuration.
            if self.shopify_apply_tax_in_order != 'create_shopify_tax':
                values["shopify_tax_grid_id"] = False
                values["is_create_tax_group"] = False
                values["tax_group_payable_account_id"] = False
                values["tax_group_receivable_account_id"] = False
            if self.shopify_apply_tax_in_order == 'create_shopify_tax' and not self.is_create_tax_group:
                values["tax_group_payable_account_id"] = False
                values["tax_group_receivable_account_id"] = False

            product_webhook_changed = customer_webhook_changed = order_webhook_changed = False
            if instance.create_shopify_products_webhook != self.create_shopify_products_webhook:
                product_webhook_changed = True
            if instance.create_shopify_customers_webhook != self.create_shopify_customers_webhook:
                customer_webhook_changed = True
            if instance.create_shopify_orders_webhook != self.create_shopify_orders_webhook:
                order_webhook_changed = True
            instance.write(values)

            if product_webhook_changed:
                instance.configure_shopify_product_webhook()
            if customer_webhook_changed:
                instance.configure_shopify_customer_webhook()
            if order_webhook_changed:
                instance.configure_shopify_order_webhook()

        if not self.show_net_profit_report and exist_module:
            exist_module.with_user(SUPERUSER_ID).button_immediate_uninstall()

        return res

    def download_shopify_net_profit_report_module(self):
        """
        This Method relocates download zip file of Shopify Net Profit Report module.
        @return: This Method return file download file.
        @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 08 August 2022.
        """
        attachment = self.env['ir.attachment'].search(
            [('name', '=', 'shopify_net_profit_report_ept.zip')])
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'new',
            'nodestroy': False,
        }

    @api.onchange("is_shopify_digest")
    def onchange_is_shopify_digest(self):
        """
        This method is used to create digest record based on Shopify instance.
        @author: Meera Sidapara on date 13-July-2022.
        @task: 194458 - Digest email development
        """
        try:
            digest_exist = self.env.ref('shopify_ept.digest_shopify_instance_%d' % self.shopify_instance_id.id)
        except:
            digest_exist = False
        if self.is_shopify_digest:
            shopify_cron = self.env['shopify.cron.configuration.ept']
            vals = self.prepare_shopify_val_for_digest()
            if digest_exist:
                vals.update({'name': digest_exist.name})
                digest_exist.write(vals)
            else:
                core_record = shopify_cron.check_core_shopify_cron(
                    "common_connector_library.connector_digest_digest_default")

                new_instance_digest = core_record.copy(default=vals)
                name = 'digest_shopify_instance_%d' % (self.shopify_instance_id.id)
                self.create_shopify_digest_data(name, new_instance_digest)
        else:
            if digest_exist:
                digest_exist.write({'state': 'deactivated'})

    def prepare_shopify_val_for_digest(self):
        """ This method is used to prepare a vals for the digest configuration.
            @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 13 July 2022.
            Task_id: 194458 - Digest email development
        """
        vals = {'state': 'activated',
                'name': 'Shopify : ' + self.shopify_instance_id.name + ' Periodic Digest',
                'module_name': 'shopify_ept',
                'shopify_instance_id': self.shopify_instance_id.id,
                'company_id': self.shopify_instance_id.shopify_company_id.id}
        return vals

    def create_shopify_digest_data(self, name, new_instance_digest):
        """ This method is used to create a digest record of ir model data
            @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 13 July 2022.
            Task_id: 194458 - Digest email development
        """
        self.env['ir.model.data'].create({'module': 'shopify_ept',
                                          'name': name,
                                          'model': 'digest.digest',
                                          'res_id': new_instance_digest.id,
                                          'noupdate': True})

    @api.model
    def action_shopify_open_basic_configuration_wizard(self):
        """
           Usage: return the action for open the basic configurations wizard
           @Task:   166992 - Shopify Onboarding panel
           @author: Dipak Gogiya
           :return: True
        """
        try:
            view_id = self.env.ref('shopify_ept.shopify_basic_configurations_onboarding_wizard_view')
        except:
            return True
        return self.shopify_res_config_view_action(view_id)

    @api.model
    def action_shopify_open_financial_status_configuration_wizard(self):
        """
           Usage: return the action for open the basic configurations wizard
           @Task:   166992 - Shopify Onboarding panel
           @author: Dipak Gogiya
           :return: True
        """
        try:
            view_id = self.env.ref('shopify_ept.shopify_financial_status_onboarding_wizard_view')
        except:
            return True
        return self.shopify_res_config_view_action(view_id)

    def shopify_res_config_view_action(self, view_id):
        """
           Usage: return the action for open the configurations wizard
           @Task:   166992 - Shopify Onboarding panel
           @author: Dipak Gogiya
           :return: True
        """
        action = self.env["ir.actions.actions"]._for_xml_id(
            "shopify_ept.action_shopify_instance_config")
        action_data = {'view_id': view_id.id, 'views': [(view_id.id, 'form')], 'target': 'new',
                       'name': 'Configurations'}
        instance = self.env['shopify.instance.ept'].search_shopify_instance()
        if instance:
            action['context'] = {'default_shopify_instance_id': instance.id}
        else:
            action['context'] = {}
        action.update(action_data)
        return action

    def shopify_save_basic_configurations(self):
        """
           Usage: Save the basic configuration changes in the instance
           @Task:   166992 - Shopify Onboarding panel
           @author: Dipak Gogiya
           :return: True
        """
        instance = self.shopify_instance_id
        if instance:
            basic_configuration_dict = {
                'shopify_company_id': self.shopify_company_id and self.shopify_company_id.id or False,
                'shopify_warehouse_id': self.shopify_warehouse_id and self.shopify_warehouse_id.id or False,
                'auto_import_product': self.auto_import_product or False,
                'sync_product_with_images': self.shopify_sync_product_with_images or False,
                'shopify_sync_product_with': self.shopify_sync_product_with or False,
                'shopify_pricelist_id': self.shopify_pricelist_id and self.shopify_pricelist_id.id or False,
                'shopify_compare_pricelist_id': self.shopify_compare_pricelist_id and self.shopify_compare_pricelist_id.id or False,
                'shopify_section_id': self.shopify_section_id and self.shopify_section_id.id or False,
                'is_use_default_sequence': self.shopify_is_use_default_sequence,
                'shopify_order_prefix': self.shopify_order_prefix or False,
                'shopify_default_pos_customer_id': self.shopify_default_pos_customer_id.id,
                'apply_tax_in_order': self.shopify_apply_tax_in_order,
                'invoice_tax_account_id': self.shopify_invoice_tax_account_id and
                                          self.shopify_invoice_tax_account_id.id or False,
                'credit_tax_account_id': self.shopify_credit_tax_account_id and
                                         self.shopify_credit_tax_account_id.id or False,
                'import_order_after_date': self.shopify_import_order_after_date,
                'shopify_is_use_analytic_account': self.shopify_is_use_analytic_account or False,
                'shopify_analytic_account_id': self.shopify_analytic_account_id.id or False,
                # 'shopify_analytic_tag_ids': self.shopify_analytic_tag_ids.ids or False,
                'shopify_lang_id': self.shopify_lang_id and self.shopify_lang_id.id or False,
                'is_delivery_fee': self.is_delivery_fee,
                'delivery_fee_name': self.delivery_fee_name,
                'is_delivery_multi_warehouse': self.is_delivery_multi_warehouse or False,
                'import_customer_as_company': self.import_customer_as_company or False,
                'order_visible_currency': self.order_visible_currency or False,
                'shopify_product_uom_id': self.shopify_product_uom_id and self.shopify_product_uom_id.id or False,
                'shopify_sync_product_weight': self.shopify_sync_product_weight or False,
                'is_shopify_market_enabled': self.is_shopify_market_enabled or False,
            }

            instance.write(basic_configuration_dict)
            company = instance.shopify_company_id
            self.env['onboarding.onboarding.step'].sudo().action_validate_step(
                "shopify_ept.onboarding_onboarding_step_shopify_configure")
        return True

    def shopify_save_financial_status_configurations(self):
        """
            Usage: Save the changes in the Instance.
            @Task:   166992 - Shopify Onboarding panel
            @author: Dipak Gogiya, 22/09/2020
            :return: True
        """
        instance = self.shopify_instance_id
        if instance:
            product_webhook_changed = customer_webhook_changed = order_webhook_changed = False
            if instance.create_shopify_products_webhook != self.create_shopify_products_webhook:
                product_webhook_changed = True
            if instance.create_shopify_customers_webhook != self.create_shopify_customers_webhook:
                customer_webhook_changed = True
            if instance.create_shopify_orders_webhook != self.create_shopify_orders_webhook:
                order_webhook_changed = True

            instance.write({
                'shopify_stock_field': self.shopify_stock_field,
                'notify_customer': self.shopify_notify_customer,
                'shopify_settlement_report_journal_id': self.shopify_settlement_report_journal_id or False,
                'create_shopify_products_webhook': self.create_shopify_products_webhook,
                'create_shopify_customers_webhook': self.create_shopify_customers_webhook,
                'create_shopify_orders_webhook': self.create_shopify_orders_webhook,
                'is_shopify_digest': self.is_shopify_digest or False,
                "is_shopify_create_schedule": self.is_shopify_create_schedule,
                "shopify_user_ids": [(6, 0, self.shopify_user_ids.ids)],
                "shopify_activity_type_id": self.shopify_activity_type_id and self.shopify_activity_type_id.id or False,
                "shopify_date_deadline": self.shopify_date_deadline or False
            })

            if product_webhook_changed:
                instance.configure_shopify_product_webhook()
            if customer_webhook_changed:
                instance.configure_shopify_customer_webhook()
            if order_webhook_changed:
                instance.configure_shopify_order_webhook()

            company = instance.shopify_company_id
            self.env['onboarding.onboarding.step'].sudo().action_validate_step(
                "shopify_ept.onboarding_onboarding_step_financial_status_configure")
            financials_status = self.env['sale.auto.workflow.configuration.ept'].search(
                [('shopify_instance_id', '=', instance.id)])
            unlink_for_financials_status = financials_status - self.shopify_financial_status_ids
            unlink_for_financials_status.unlink()
        return True

    @api.onchange('group_analytic_accounting')
    def onchange_analytic_accounting(self):
        res_config_shopify_instance = self.env['res.config.shopify.instance']
        shopify_isntance_obj = self.env['shopify.instance.ept']
        res = super().onchange_analytic_accounting()
        if self.group_analytic_accounting:
            instances = shopify_isntance_obj.search([('active', '=', True)])
            for instance in instances:
                if instance.shopify_is_use_analytic_account:
                    res_config_shopify_instance.shopify_create_analytic_plan_account(instance)
        return res

    def action_open_shopify_auth_wizard_ept(self):
        """
        Opens the wizard to generate the Shopify OAuth authorization link for custom app installation.
        """
        return {
            'type': 'ir.actions.act_window',
            'name': _('Set Shopify Authorization Details'),
            'res_model': 'shopify.auth.process.ept',
            'view_mode': 'form',
            'target': 'new',
            'context': dict(self.env.context),
        }
