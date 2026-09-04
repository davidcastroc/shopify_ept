# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

import json
import logging

from calendar import monthrange
from datetime import date, datetime, timedelta
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from ..shopify_graphql.client import ShopifyGraphQLClient
from ..shopify_graphql.queries.bulk_order_helper import ShopifyBulkOrderHelper
from ..shopify_graphql.queries.translation import TranslationQueryHelper
from .. import shopify
from ..shopify.pyactiveresource.connection import ForbiddenAccess, set_suppress_connection_info_logs
from .. import shopify_graphql

SHOPIFY_API_VERSION = "2026-01"  # Shopify REST API version — update when Shopify releases a new version

_logger = logging.getLogger("Shopify Instance")
_secondsConverter = {
    'days': lambda interval: interval * 24 * 60 * 60,
    'hours': lambda interval: interval * 60 * 60,
    'weeks': lambda interval: interval * 7 * 24 * 60 * 60,
    'minutes': lambda interval: interval * 60,
}


class ShopifyInstanceEpt(models.Model):
    _name = "shopify.instance.ept"
    _description = 'Shopify Instance'

    @api.model
    def _get_default_warehouse(self):
        """
        This method is used to set the default warehouse in an instance.
        """
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.shopify_company_id.id)], limit=1,
                                                       order='id')
        return warehouse.id if warehouse else False

    @api.model
    def _default_stock_field(self):
        """
        This method is used to set the default stock field in an instance.
        """
        stock_field = self.env['ir.model.fields'].search(
            [('model_id.model', '=', 'product.product'), ('name', '=', 'virtual_available')], limit=1)
        return stock_field.id if stock_field else False

    @api.model
    def _default_discount_product(self):
        """
        This method is used to set the discount product in an instance.
        @author: Haresh Mori on Date 16-Dec-2019.
        """
        discount_product = self.env.ref('shopify_ept.shopify_discount_product', False)
        return discount_product

    @api.model
    def _default_duties_product(self):
        """
        This method is used to set the duties product in an instance.
        @author: Nilam kubavat on Date 03-06-2022
        @Task_id : 191580
        """
        duties_product = self.env.ref('shopify_ept.shopify_duties_product', False)
        return duties_product

    @api.model
    def _default_shipping_product(self):
        """
        This method is used to set the shipping product in an instance.
        @author: Maulik Barad on Date 01-Oct-2020.
        """
        shipping_product = self.env.ref('shopify_ept.shopify_shipping_product', False)
        return shipping_product

    @api.model
    def _default_gift_card_product(self):
        """
        This method is used to set the gift card product in an instance.
        @author: Maulik Barad on Date 01-Oct-2020.
        """
        gift_card_product = self.env.ref('shopify_ept.shopify_gift_card_product', False)
        return gift_card_product

    @api.model
    def _default_custom_service_product(self):
        """
        This method is used to set the custom service product in an instance.
        @author: Haresh Mori on Date 15-Apr-2021.
        """
        custom_service_product = self.env.ref('shopify_ept.shopify_custom_service_product', False)
        return custom_service_product

    @api.model
    def _default_custom_storable_product(self):
        """
        This method is used to set the gift card product in an instance.
        @author: Haresh Mori on Date 15-Apr-2021.
        """
        custom_storable_product = self.env.ref('shopify_ept.shopify_custom_storable_product', False)
        return custom_storable_product

    @api.model
    def _default_refund_adjustment_product(self):
        """
        This method is used to set the gift card product in an instance.
        @author: Haresh Mori on Date 15-Apr-2021.
        """
        refund_adjustment_product = self.env.ref('shopify_ept.shopify_refund_adjustment_product', False)
        return refund_adjustment_product

    @api.model
    def _default_tip_product(self):
        """
        This method is used to set the gift card product in an instance.
        @author: Haresh Mori on Date 15-Apr-2021.
        """
        tip_product = self.env.ref('shopify_ept.shopify_tip_product', False)
        return tip_product

    @api.model
    def _default_order_status(self):
        """ Return default status of shopify order, for importing the particular orders having this status.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 30 December 2020 .
            Task_id: 169381
        """
        order_status = self.env.ref('shopify_ept.unshipped')
        return [(6, 0, [order_status.id])] if order_status else False

    @api.model
    def _default_shopify_import_after_date(self):
        """ This method is use to set the defaut import after date.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 28 September 2021 .
            Task_id: 178358
        """
        order_after_date = datetime.now() - timedelta(30)
        return order_after_date

    @api.model
    def _default_UOM_category(self):
        product_weight_in_lbs_param = self.env['ir.config_parameter'].sudo().get_param('product.weight_in_lbs')
        if product_weight_in_lbs_param and product_weight_in_lbs_param == '1':
            uom = self.env.ref('uom.product_uom_lb')
        else:
            uom = self.env.ref('uom.product_uom_kgm')
        return uom

    @api.model
    def _get_default_language(self):
        lang_code = self.env.user.lang
        language = self.env["res.lang"].search([('code', '=', lang_code)])
        return language.id if language else False

    @api.model
    def _default_use_graphql_api_value(self):
        graphql_enabled_on_install = self.env['ir.config_parameter'].sudo().get_param(
            'shopify_ept.graphql_enabled_on_install')
        if graphql_enabled_on_install and graphql_enabled_on_install == 'True':
            return True
        return False

    name = fields.Char(size=120, required=True)
    shopify_company_id = fields.Many2one('res.company', string='Company', required=True,
                                         default=lambda self: self.env.company)
    shopify_warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse', default=_get_default_warehouse,
                                           domain="[('company_id', '=',shopify_company_id)]",
                                           help="Selected Warehouse will be set in your Shopify "
                                                "orders.", required=True)
    shopify_pricelist_id = fields.Many2one('product.pricelist', string='Pricelist',
                                           help="1.During product sync operation, prices will be Imported/Exported "
                                                "using this Pricelist.\n"
                                                "2.During order sync operation, this pricelist "
                                                "will be set in the order if the order currency from store and the "
                                                "currency from the pricelist set here, matches.")

    shopify_compare_pricelist_id = fields.Many2one('product.pricelist', string='Compare At Pricelist',
                                                   help="During product sync, Import and Export operation, prices will be Imported/Exported "
                                                        "using this Pricelist.")

    shopify_order_prefix = fields.Char(size=10, string='Order Prefix',
                                       help="Enter your order prefix")
    shopify_api_key = fields.Char("API Key", required=True)
    shopify_password = fields.Char("Password", required=True)
    shopify_shared_secret = fields.Char("Secret Key", required=True)
    shopify_host = fields.Char("Host", required=True)
    shopify_last_date_customer_import = fields.Datetime(string="Last Customer Import",
                                                        help="it is used to store last import customer date")
    shopify_last_date_update_stock = fields.Datetime(string="Last Stock Update",
                                                     help="it is used to store last update inventory stock date")
    shopify_last_date_product_import = fields.Datetime(string="Last Product Import",
                                                       help="it is used to store last import product date")
    auto_import_product = fields.Boolean(string="Auto Create Product if not found?")
    shopify_sync_product_with = fields.Selection([('sku', 'Internal Reference(SKU)'),
                                                  ('barcode', 'Barcode'),
                                                  ('sku_or_barcode',
                                                   'Internal Reference or Barcode'),
                                                  ], string="Sync Product With", default='sku')
    update_category_in_odoo_product = fields.Boolean(string="Update Category In Odoo Product ?",
                                                     default=False)
    shopify_stock_field = fields.Many2one('ir.model.fields', string='Stock Field')
    shopify_import_stock_for_bom_products = fields.Boolean(
        string="Import Stock for BOM Products",
        help="If checked, stock will be imported/updated from Shopify for products having a Bill of "
             "Materials of type 'Manufacture this product' or 'Subcontracting'.\n"
             "This setting has no effect on Kit type BOMs: a kit's stock is always auto-computed from "
             "its components in Odoo and cannot be set directly, so kit products are always skipped "
             "regardless of this setting.")
    last_date_order_import = fields.Datetime(string="Last Date Of Unshipped Order Import",
                                             help="Last date of sync orders from Shopify to Odoo")
    shopify_section_id = fields.Many2one('crm.team', 'Sales Team')
    is_use_default_sequence = fields.Boolean("Use Odoo Default Sequence?",
                                             help="If checked,Then use default sequence of odoo while create sale "
                                                  "order.")
    # Account field
    shopify_store_time_zone = fields.Char("Store Time Zone",
                                          help='This field used to import order process')
    discount_product_id = fields.Many2one("product.product", "Discount",
                                          domain=[('type', '=', 'service')],
                                          default=_default_discount_product,
                                          help="This is used for set discount product in a sale order lines")

    apply_tax_in_order = fields.Selection(
        [("odoo_tax", "Odoo Default Tax Behaviour"), ("create_shopify_tax",
                                                      "Create new tax If Not Found")],
        copy=False, help=""" For Shopify Orders :- \n
                    1) Odoo Default Tax Behaviour - The Taxes will be set based on Odoo's
                                 default functional behaviour i.e. based on Odoo's Tax and Fiscal Position configurations. \n
                    2) Create New Tax If Not Found - System will search the tax data received 
                    from Shopify in Odoo, will create a new one if it fails in finding it.""")
    invoice_tax_account_id = fields.Many2one('account.account', string='Invoice Tax Account')
    credit_tax_account_id = fields.Many2one('account.account', string='Credit Tax Account')
    notify_customer = fields.Boolean("Notify Customer about Update Order Status?",
                                     help="If checked,Notify the customer via email about Update Order Status")
    color = fields.Integer(string='Color Index')

    # fields for kanban view
    product_ids = fields.One2many('shopify.product.template.ept', 'shopify_instance_id',
                                  string="Products")

    shopify_user_ids = fields.Many2many('res.users', 'shopify_instance_ept_res_users_rel',
                                        'res_config_settings_id', 'res_users_id',
                                        string='Responsible User')
    shopify_activity_type_id = fields.Many2one('mail.activity.type',
                                               string="Activity Type")
    shopify_date_deadline = fields.Integer('Deadline lead days',
                                           help="its add number of  days in schedule activity deadline date ")
    is_shopify_create_schedule = fields.Boolean("Create Schedule Activity ? ", default=False,
                                                help="If checked, Then Schedule Activity create on order data queues"
                                                     " will any queue line failed.")
    active = fields.Boolean(default=True)
    sync_product_with_images = fields.Boolean("Sync Images?",
                                              help="Check if you want to import images along with "
                                                   "products",
                                              default=True)
    shopify_sync_product_weight = fields.Boolean("Sync Weight?",
                                                  help="Check if you want to sync the weight of the "
                                                       "product from Shopify to Odoo, while importing "
                                                       "or updating products.",
                                                  default=True)

    webhook_ids = fields.One2many("shopify.webhook.ept", "instance_id", "Webhooks")
    create_shopify_products_webhook = fields.Boolean("Manage Products via Webhooks",
                                                     help="True : It will create all product related webhooks.\n"
                                                          "False : All product related webhooks will be deactivated.")

    create_shopify_customers_webhook = fields.Boolean("Manage Customers via Webhooks",
                                                      help="True : It will create all customer related webhooks.\n"
                                                           "False : All customer related webhooks will be deactivated.")
    create_shopify_orders_webhook = fields.Boolean("Manage Orders via Webhooks",
                                                   help="True : It will create all order related webhooks.\n"
                                                        "False : All order related webhooks will be deactivated.")
    shopify_default_pos_customer_id = fields.Many2one("res.partner", "Default POS customer",
                                                      help="This customer will be set in POS order, when"
                                                           "customer is not found.")
    # Auto Import Shipped Order
    auto_import_shipped_order = fields.Boolean(default=False)

    # Shopify Payout Report
    transaction_line_ids = fields.One2many("shopify.payout.account.config.ept", "instance_id",
                                           string="Transaction Line")
    shopify_settlement_report_journal_id = fields.Many2one('account.journal',
                                                           string='Payout Report Journal')
    payout_last_import_date = fields.Date(string="Last Date of Payout Import")
    last_shipped_order_import_date = fields.Datetime(string="Last Date Of Shipped Order Import",
                                                     help="Last date of sync orders from Shopify to Odoo")
    last_cancel_order_import_date = fields.Datetime(string="Last Date Of Cancel Order Import",
                                                    help="Last date of sync orders from Shopify to Odoo")
    last_return_sync_date = fields.Datetime(string="Last Date Of Return Import",
                                            help="Last date of sync returns from Shopify to Odoo")
    last_buy_with_prime_order_import_date = fields.Datetime(string="Last Date Of Buy with Prime Order Import",
                                                            help="Last date of sync orders from Shopify to Odoo")
    is_instance_create_from_onboarding_panel = fields.Boolean(default=False)
    is_onboarding_configurations_done = fields.Boolean(default=False)
    shipping_product_id = fields.Many2one("product.product", domain=[('type', '=', 'service')],
                                          default=_default_shipping_product,
                                          help="This is used for set shipping product in a Carrier.")
    shopify_order_data = fields.Text(compute="_compute_kanban_shopify_order_data")
    shopify_order_status_ids = fields.Many2many('import.shopify.order.status', 'shopify_instance_order_status_rel',
                                                'instance_id', 'status_id', "Shopify Import Order Status",
                                                default=_default_order_status,
                                                help="Select order status in which "
                                                     "you want to import the orders from Shopify to Odoo.")
    gift_card_product_id = fields.Many2one("product.product", domain=[('type', '=', 'service')],
                                           default=_default_gift_card_product,
                                           help="This is used to manage the gift card in sale order")
    auto_fulfill_gift_card_order = fields.Boolean(
        "Automatically fulfill only the gift cards of the order", default=True,
        help="If unchecked, It will fulfill qty from Odoo to shopify in update order status process")

    import_order_after_date = fields.Datetime(help="Connector only imports those orders which have created after a "
                                                   "given date.", default=_default_shopify_import_after_date)

    custom_service_product_id = fields.Many2one("product.product", "Custom Service Product",
                                                domain=[('type', '=', 'service')],
                                                default=_default_custom_service_product,
                                                help="This is used for set custom service products in sale order "
                                                     "lines while receiving the custom item in order response.")
    custom_storable_product_id = fields.Many2one("product.product", "Custom Storable Product",
                                                 domain=[('type', '=', 'consu'), ('is_storable', '=', True)],
                                                 default=_default_custom_storable_product,
                                                 help="This is used for set custom storable products in sale order "
                                                      "lines while receiving the custom item as required shipping in "
                                                      "order response.")
    refund_adjustment_product_id = fields.Many2one("product.product", "Refund Adjustment",
                                                   domain=[('type', '=', 'service')],
                                                   default=_default_refund_adjustment_product,
                                                   help="This is used for set refund adjustment in a credit note")
    tip_product_id = fields.Many2one("product.product", "Tip",
                                     domain=[('type', '=', 'service')],
                                     default=_default_tip_product,
                                     help="This is used for set Tip product in a sale order lines")
    # Analytic
    shopify_is_use_analytic_account = fields.Boolean(default=True)
    shopify_analytic_account_id = fields.Many2one('account.analytic.account', string='Analytic Account',
                                                  domain="['|', ('company_id', '=', False), "
                                                         "('company_id', '=', shopify_company_id)]")
    # shopify_analytic_tag_ids = fields.Many2many('account.analytic.tag', string='Analytic Tags',
    #                                             domain="['|', ('company_id', '=', False),
    #                                             ('company_id', '=', shopify_company_id)]")
    shopify_lang_id = fields.Many2one('res.lang', string='Language', default=_get_default_language)

    enable_multi_lang_sync = fields.Boolean(string='Enable Multi-Language Product Sync?', default=False)
    import_translation_in_odoo_product = fields.Boolean(string='Write Translations into Odoo Product?', default=True,
                                                        help="When enabled, translations imported from Shopify will also be written to the corresponding Odoo product")
    shopify_lang_mapping_ids = fields.One2many(comodel_name='shopify.lang.mapping.ept',
                                               inverse_name='shopify_instance_id', string='Language Mappings',
                                               help="Map each Shopify locale code to an Odoo language")

    enable_multi_currency_payout_report = fields.Boolean(string='Enable Multi-currency Payout Report ?', default=False,
                                                         help="Enable this option if Shopify Payments Multi-Currency Payouts are configured for this store.")
    shopify_payout_journal_configuration_ids = fields.One2many("shopify.payout.journal.config.ept", "instance_id",
                                                               string="Payout Journal Configuration")

    # presentment currency
    order_visible_currency = fields.Boolean(string="Import order in customer visible currency?")

    duties_product_id = fields.Many2one("product.product", "Duties",
                                        domain=[('type', '=', 'service')],
                                        default=_default_duties_product,
                                        help="This is used for set duties product in a sale order lines")

    is_shopify_digest = fields.Boolean(string="Set Shopify Digest?")
    is_delivery_fee = fields.Boolean(string='Are you selling for Colorado State(US)')
    delivery_fee_name = fields.Char(string='Delivery fee name')

    is_delivery_multi_warehouse = fields.Boolean(string="Is Delivery from Multiple warehouse?")
    import_customer_as_company = fields.Boolean(string="Import customer as a Company")
    shopify_product_uom_id = fields.Many2one('uom.uom', string='Unit of Measure',
                                             default=_default_UOM_category,
                                             )
    ship_order_webhook = fields.Boolean("Want to ship order", help="If checked, it will fulfill order in odoo")
    forcefully_reserve_stock_webhook = fields.Boolean("ForceFully Reserve Stock",
                                                      help="If checked, It will forcefully reserve stock in the picking")
    refund_order_webhook = fields.Boolean("Want to refund order", help="If checked, it will create a refund in odoo")
    customer_order_webhook = fields.Boolean("Want to update customer",
                                            help="If checked, it will update the customer in order")
    update_qty_order_webhook = fields.Boolean("Want to update Quantity",
                                              help="If checked, it will update the customer in order")
    add_new_product_order_webhook = fields.Boolean("Want to Add New Product",
                                                   help="If checked, it will add new product in the order if receive in the webhook")
    import_buy_with_prime_shopify_order = fields.Boolean(string="Import Buy with Prime Orders",
                                                         help="If checked, it will import order of buy with prime orders")
    buy_with_prime_warehouse_id = fields.Many2one("stock.warehouse", string="Shopify Warehouse",
                                                  domain="[('company_id', '=',shopify_company_id)]")
    buy_with_prime_tag_ids = fields.Many2many("shopify.tags", "shopify_instance_buy_with_prime_shopify_tags_rel",
                                              "product_tmpl_id", "tag_id",
                                              "Tags for import buy with prime orders")
    Force_transfer_move_of_buy_with_prime_orders = fields.Boolean(string="Force Transfer",
                                                                  help="If checked, it will forcefully done the stock move while stock also not there.")
    return_picking_order = fields.Boolean("Want to return picking", help="If checked, it will create a return in odoo")
    stock_validate_for_return = fields.Boolean("Want to validate return picking",
                                               help="If checked, it will validate a return picking")
    # return_location_id = fields.Many2one('stock.location', 'Return Location')
    update_qty_to_invoice_order_webhook = fields.Boolean("Want to changes to invoice as per update Quantity",
                                                         help="If checked, it will update invoice based on updated quantity")
    credit_note_register_payment = fields.Boolean("Want to create register payment for credit note",
                                                  help="If checked, it will create a payment for credit note")
    credit_note_payment_journal = fields.Many2one("account.journal", string="Credit Note Payment Journal",
                                                  help=" Selected Journal will be set in Credit note Payment journal.")

    auto_create_product_category = fields.Boolean(string="Auto Create Product Category?", default=True,
                                                  help="If checked, it will create a product category in odoo.")

    shopify_instance_product_category = fields.Many2one("product.category", string="Default product category",
                                                        help="Select product category")

    shopify_tax_grid_id = fields.Many2one(comodel_name='account.account.tag', string="Tax Grids",
                                          ondelete='restrict',
                                          domain=[('applicability', '=', 'taxes')],
                                          help="Tax distribution line that caused the creation of this move line, if any")

    is_create_tax_group = fields.Boolean(string="Auto Create Tax Group?", default=False, copy=False)

    tax_group_payable_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Tax Payable Account',
        help="Tax current account used as a counterpart to the Tax Closing Entry when in favor of the authorities.")
    tax_group_receivable_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Tax Receivable Account',
        help="Tax current account used as a counterpart to the Tax Closing Entry when in favor of the company.")

    use_graphql_api = fields.Boolean(default=_default_use_graphql_api_value, string='Use GraphQL API ?')
    shopify_enable_gql_debug = fields.Boolean(default=False, string='Enable GraphQL Debug?')
    shopify_order_export_tax_policy = fields.Selection(
        selection=[
            ("shopify_tax", "Use Shopify tax for exporting order"),
            ("odoo_tax", "Use Odoo tax for exporting amount"),
        ],
        string="Order Export Tax Policy",
        default="shopify_tax",
        help=(
            "Use Shopify tax for exporting order: Odoo exports only untaxed unit price and quantity for products. "
            "It does not export Odoo tax lines, regardless of whether Odoo taxes are inclusive or exclusive. "
            "When quantity is changed in Odoo and updated to Shopify, only quantity is updated; "
            "price and tax are calculated in Shopify.\n\n"
            "Use Odoo tax for exporting amount: Odoo exports tax amounts to Shopify at order export time. "
            "Those exported taxes are treated as fixed and are not expected to be recalculated/changed in Shopify."
        ),
    )
    auto_sync_product_tag = fields.Boolean(string="Auto Create Product Tags?", default=False,
                                           help="If checked, it will set a product tag in odoo.")
    enable_metafield_sync = fields.Boolean(
        string="Enable Metafield Synchronization?",
        default=False,
        help="Enable this option to synchronize Shopify metafields during product import and export operations."
    )
    is_create_refund = fields.Boolean(string="Auto Create Refund?", default=False, copy=False,
                                      help="If checked, it will automatically create refund in odoo for the returned orders in shopify.")
    last_customer_segment_import_date = fields.Datetime(string="Last Date of Customer Segment Import",
                                                     help="it is used to store last update customer segment date")

    use_channel_analytic_account = fields.Boolean(
        string="Use Channel-wise Analytic Account?",
        default=False,
        help="When enabled, the system will use the analytic account mapped on each Shopify "
             "channel record instead of the instance-level analytic account."
    )

    shopify_channel_ids = fields.One2many(
        "shopify.channel.ept",
        "shopify_instance_id",
        string="Sales Channels",
    )
    shopify_channel_count = fields.Integer(
        string="Channel Count",
        compute="_compute_shopify_channel_count",
    )

    def _compute_shopify_channel_count(self):
        for record in self:
            record.shopify_channel_count = self.env["shopify.channel.ept"].search_count(
                [("shopify_instance_id", "=", record.id)]
            )

    def action_open_channels(self):
        """Open the Sales Channels list filtered for this instance."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sales Channels"),
            "res_model": "shopify.channel.ept",
            "view_mode": "list,form",
            "domain": [("shopify_instance_id", "=", self.id)],
            "context": {"default_shopify_instance_id": self.id},
        }

    def action_sync_shopify_channels(self):
        """Sync channels from Shopify for this instance (button on form)."""
        self.ensure_one()
    
        def _notify(title, message, n_type, sticky=False):
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": title, "message": message, "sticky": sticky, "type": n_type},
            }
    
        try:
            synced_count = self.env["shopify.channel.ept"].sync_shopify_channels(self)
        except Exception as e:
            return _notify(
                _("Channel Sync Failed"),
                _("Could not sync Shopify channels for '%s': %s") % (self.name, str(e)),
                "danger", sticky=True,
            )

        if synced_count:
            return _notify(
                _("Channels Synced"),
                _("%d Shopify sales channel(s) have been synchronised for '%s'.") % (synced_count, self.name),
                "success",
            )
    
        return _notify(
            _("No Channels Found"),
            _("No sales channels were returned from Shopify for '%s'. Please check your instance configuration.") % self.name,
            "warning",
        )

    
    # ── Markets ────────────────────────────────────────────────────────────────
    market_ids = fields.One2many(
        "shopify.market.ept",
        "shopify_instance_id",
        string="Shopify Markets",
    )
    market_count = fields.Integer(
        string="Markets",
        compute="_compute_market_count_ept",
    )

    is_shopify_market_enabled = fields.Boolean(
        string="Enable Shopify Markets?",
        default=False,
        help="If checked, it will enable the Shopify Markets feature for this instance, allowing you to manage multiple markets with different settings and configurations.",
    )

    def _compute_market_count_ept(self):
        for rec in self:
            rec.market_count = self.env["shopify.market.ept"].search_count(
                [("shopify_instance_id", "=", rec.id)]
            )

    def action_sync_shopify_markets_ept(self):
        """Button action: sync markets from Shopify for this instance."""
        self.ensure_one()
        market_obj = self.env["shopify.market.ept"]
        synced = market_obj.sync_markets_from_shopify_ept(self)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Markets Synced"),
                "message": _("%d market(s) synced from Shopify.") % len(synced),
                "sticky": False,
                "type": "success",
            },
        }

    def action_open_markets_ept(self):
        """Stat button: open markets list for this instance."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Shopify Markets"),
            "res_model": "shopify.market.ept",
            "view_mode": "list,form",
            "domain": [("shopify_instance_id", "=", self.id)],
            "context": {"default_shopify_instance_id": self.id},
        }
    _unique_host = models.Constraint('unique(shopify_host)',
                         "Instance already exists for given host. Host must be Unique for the instance!")

    def _compute_kanban_shopify_order_data(self):
        if not self.env.context.get('sort'):
            context = dict(self.env.context)
            context.update({'sort': 'week'})
            self.env = self.env(context=context)
        for record in self:
            # Prepare values for Graph
            values = record.get_graph_data(record)
            data_type, comparison_value = record.get_compare_data(record)
            # Total sales
            total_sales = round(sum([key['y'] for key in values]), 2)
            # Order count query
            order_data = record.get_total_orders()
            # Product count query
            product_data = record.get_total_products()
            # Order shipped count query
            order_shipped = record.get_shipped_orders()
            # Customer count query
            customer_data = record.get_customers()
            # refund count query
            refund_data = record.get_refund()
            record.shopify_order_data = json.dumps({
                "values": values,
                "title": "",
                "key": "Order: Untaxed amount",
                "area": True,
                "color": "#875A7B",
                "is_sample_data": False,
                "total_sales": total_sales,
                "order_data": order_data,
                "product_date": product_data,
                "customer_data": customer_data,
                "order_shipped": order_shipped,
                "refund_data": refund_data,
                "refund_count": refund_data.get('refund_count'),
                "sort_on": self.env.context.get('sort'),
                "currency_symbol": record.shopify_company_id.currency_id.symbol or '',
                "graph_sale_percentage": {'type': data_type, 'value': comparison_value}
            })

    def get_graph_data(self, record):
        """
        Use: To get the details of shopify sale orders and total amount month wise or year wise to prepare the graph
        Task: 167063
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 29/10/20
        :return: shopify sale order date or month and sum of sale orders amount of current instance
        """

        def get_current_week_date(record):
            self.env.cr.execute("""SELECT to_char(date(d.day),'DAY'), t.amount_untaxed as sum
                                FROM  (
                                   SELECT day
                                   FROM generate_series(date(date_trunc('week', (current_date)))
                                    , date(date_trunc('week', (current_date)) + interval '6 days')
                                    , interval  '1 day') day
                                   ) d
                                LEFT   JOIN
                                (SELECT date(date_order)::date AS day, sum(amount_untaxed) as amount_untaxed
                                   FROM   sale_order
                                   WHERE  date(date_order) >= (select date_trunc('week', date(current_date)))
                                   AND    date(date_order) <= (select date_trunc('week', date(current_date))
                                   + interval '6 days')
                                   AND shopify_instance_id=%s and state in ('sale','done')
                                   GROUP  BY 1
                                   ) t USING (day)
                                ORDER  BY day""", (record.id,))
            return self.env.cr.dictfetchall()

        def graph_of_current_month(record):
            self.env.cr.execute("""select EXTRACT(DAY from date(date_day)) :: integer,sum(amount_untaxed) from (
                        SELECT
                          day::date as date_day,
                          0 as amount_untaxed
                        FROM generate_series(date(date_trunc('month', (current_date)))
                            , date(date_trunc('month', (current_date)) + interval '1 MONTH - 1 day')
                            , interval  '1 day') day
                        union all
                        SELECT date(date_order)::date AS date_day,
                        sum(amount_untaxed) as amount_untaxed
                          FROM   sale_order
                        WHERE  date(date_order) >= (select date_trunc('month', date(current_date)))
                        AND date(date_order)::date <= (select date_trunc('month', date(current_date))
                        + '1 MONTH - 1 day')
                        and shopify_instance_id = %s and state in ('sale','done')
                        group by 1
                        )foo
                        GROUP  BY 1
                        ORDER  BY 1""", (record.id,))
            return self.env.cr.dictfetchall()

        def graph_of_current_year(record):
            self.env.cr.execute("""select TRIM(TO_CHAR(DATE_TRUNC('month',month),'MONTH')),sum(amount_untaxed) from
                                (SELECT DATE_TRUNC('month',date(day)) as month,
                                  0 as amount_untaxed
                                FROM generate_series(date(date_trunc('year', (current_date)))
                                , date(date_trunc('year', (current_date)) + interval '1 YEAR - 1 day')
                                , interval  '1 MONTH') day
                                union all
                                SELECT DATE_TRUNC('month',date(date_order)) as month,
                                sum(amount_untaxed) as amount_untaxed
                                  FROM   sale_order
                                WHERE  date(date_order) >= (select date_trunc('year', date(current_date))) AND
                                date(date_order)::date <= (select date_trunc('year', date(current_date))
                                + '1 YEAR - 1 day')
                                and shopify_instance_id = %s and state in ('sale','done')
                                group by DATE_TRUNC('month',date(date_order))
                                order by month
                                )foo
                                GROUP  BY foo.month
                                order by foo.month""", (record.id,))
            return self.env.cr.dictfetchall()

        def graph_of_all_time(record):
            self.env.cr.execute("""select TRIM(TO_CHAR(DATE_TRUNC('month',date_order),'YYYY-MM')),sum(amount_untaxed)
                                from sale_order where shopify_instance_id = %s and state in ('sale','done')
                                group by DATE_TRUNC('month',date_order) 
                                order by DATE_TRUNC('month',date_order)""", (record.id,))
            return self.env.cr.dictfetchall()

        # Prepare values for Graph
        if self.env.context.get('sort') == 'week':
            result = get_current_week_date(record)
        elif self.env.context.get('sort') == "month":
            result = graph_of_current_month(record)
        elif self.env.context.get('sort') == "year":
            result = graph_of_current_year(record)
        else:
            result = graph_of_all_time(record)
        values = [{"x": ("{}".format(data.get(list(data.keys())[0]))), "y": data.get('sum') or 0.0} for data in result]
        return values

    def get_compare_data(self, record):
        """
        :param record: Shopify instance
        :return: Comparison ratio of orders (weekly,monthly and yearly based on selection)
        """
        data_type = False
        total_percentage = 0.0

        def get_compared_week_data(record):
            current_total = 0.0
            previous_total = 0.0
            day_of_week = date.weekday(date.today())
            self.env.cr.execute("""select sum(amount_untaxed) as current_week from sale_order
                                where date(date_order) >= (select date_trunc('week', date(current_date))) and
                                shopify_instance_id=%s and state in ('sale','done')""", (record.id,))
            current_week_data = self.env.cr.dictfetchone()
            if current_week_data:
                current_total = current_week_data.get('current_week') if current_week_data.get('current_week') else 0
            # Previous week data
            self.env.cr.execute("""select sum(amount_untaxed) as previous_week from sale_order
                            where date(date_order) between (select date_trunc('week', current_date) - interval '7 day') 
                            and (select date_trunc('week', (select date_trunc('week', current_date) - interval '7
                            day')) + interval '%s day')
                            and shopify_instance_id=%s and state in ('sale','done')
                            """, (day_of_week, record.id))
            previous_week_data = self.env.cr.dictfetchone()
            if previous_week_data:
                previous_total = previous_week_data.get('previous_week') if previous_week_data.get(
                    'previous_week') else 0
            return current_total, previous_total

        def get_compared_month_data(record):
            current_total = 0.0
            previous_total = 0.0
            day_of_month = date.today().day - 1
            self.env.cr.execute("""select sum(amount_untaxed) as current_month from sale_order
                                where date(date_order) >= (select date_trunc('month', date(current_date)))
                                and shopify_instance_id=%s and state in ('sale','done')""", (record.id,))
            current_data = self.env.cr.dictfetchone()
            if current_data:
                current_total = current_data.get('current_month') if current_data.get('current_month') else 0
            # Previous week data
            self.env.cr.execute("""select sum(amount_untaxed) as previous_month from sale_order where date(date_order)
                            between (select date_trunc('month', current_date) - interval '1 month') and
                            (select date_trunc('month', (select date_trunc('month', current_date) - interval
                            '1 month')) + interval '%s days')
                            and shopify_instance_id=%s and state in ('sale','done')
                            """, (day_of_month, record.id))
            previous_data = self.env.cr.dictfetchone()
            if previous_data:
                previous_total = previous_data.get('previous_month') if previous_data.get('previous_month') else 0
            return current_total, previous_total

        def get_compared_year_data(record):
            current_total = 0.0
            previous_total = 0.0
            year_begin = date.today().replace(month=1, day=1)
            year_end = date.today()
            delta = (year_end - year_begin).days - 1
            self.env.cr.execute("""select sum(amount_untaxed) as current_year from sale_order
                                where date(date_order) >= (select date_trunc('year', date(current_date)))
                                and shopify_instance_id=%s and state in ('sale','done')""", (record.id,))
            current_data = self.env.cr.dictfetchone()
            if current_data:
                current_total = current_data.get('current_year') if current_data.get('current_year') else 0
            # Previous week data
            self.env.cr.execute("""select sum(amount_untaxed) as previous_year from sale_order where date(date_order)
                            between (select date_trunc('year', date(current_date) - interval '1 year')) and 
                            (select date_trunc('year', date(current_date) - interval '1 year') + interval '%s days') 
                            and shopify_instance_id=%s and state in ('sale','done')
                            """, (delta, record.id))
            previous_data = self.env.cr.dictfetchone()
            if previous_data:
                previous_total = previous_data.get('previous_year') if previous_data.get('previous_year') else 0
            return current_total, previous_total

        if self.env.context.get('sort') == 'week':
            current_total, previous_total = get_compared_week_data(record)
        elif self.env.context.get('sort') == "month":
            current_total, previous_total = get_compared_month_data(record)
        elif self.env.context.get('sort') == "year":
            current_total, previous_total = get_compared_year_data(record)
        else:
            current_total, previous_total = 0.0, 0.0
        if current_total > 0.0:
            if current_total >= previous_total:
                data_type = 'positive'
                total_percentage = (current_total - previous_total) * 100 / current_total
            if previous_total > current_total:
                data_type = 'negative'
                total_percentage = (previous_total - current_total) * 100 / current_total
        return data_type, round(total_percentage, 2)

    # -----------------------------------------------------------------------
    # Shared helper — date-range filtering for kanban graph queries
    # -----------------------------------------------------------------------
    _SORT_DATE_TRUNC = {
        'week': "date_trunc('week', date(current_date))",
        'month': "date_trunc('month', date(current_date))",
        'year': "date_trunc('year', date(current_date))",
    }

    def _filter_query_by_sort(self, base_query, date_column):
        """Append a date-range WHERE clause to *base_query* and execute it.

        The time window is read from ``self.env.context['sort']``:
        * ``'week'``  – current calendar week
        * ``'month'`` – current calendar month
        * ``'year'``  – current calendar year
        * anything else (default) – all time (no extra filter)

        :param base_query: str  – SQL that already contains ``WHERE`` clauses
                                  and expects a single ``%s`` bind value for
                                  ``shopify_instance_id`` (or equivalent).
        :param date_column: str – fully-qualified column name to filter on,
                                  e.g. ``'date_order'`` or ``'so.date_order'``
                                  or ``'invoice_date'``.
        :return: list[dict] – rows returned by ``dictfetchall``.
        """
        sort = self.env.context.get('sort')
        trunc_expr = self._SORT_DATE_TRUNC.get(sort)
        if trunc_expr:
            query = base_query + f" AND date({date_column}) >= (SELECT {trunc_expr})"
        else:
            query = base_query
        self.env.cr.execute(query, (self.id,))
        return self.env.cr.dictfetchall()

    def get_total_orders(self):
        """
        Use: To get the list of shopify sale orders month wise or year wise
        Task: 167063
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 29/10/20
        :return: total number of shopify sale orders ids and action for sale orders of current instance
        """
        order_query = """
            SELECT id
            FROM sale_order
            WHERE shopify_instance_id = %s AND state IN ('sale', 'done')
        """
        result = self._filter_query_by_sort(order_query, 'date_order')
        order_ids = [data.get('id') for data in result]
        view = self.env.ref('shopify_ept.action_shopify_sales_order').sudo().read()[0]
        action = self.prepare_action(view, [('id', 'in', order_ids)])
        return {'order_count': len(order_ids), 'order_action': action}

    def get_shipped_orders(self):
        """
        Use: To get the list of shopify shipped orders month wise or year wise
        Task: 167063
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 29/10/20
        Migrated by: Shubham Kumar
        Date: 17 Sep 2025
        :return: total number of shopify shipped orders ids and action for shipped orders of current instance
        """
        shipped_query = """
            SELECT so.id
            FROM stock_picking sp
            INNER JOIN stock_move sm
                ON sm.picking_id = sp.id
            INNER JOIN stock_reference_move_rel smr
                ON smr.move_id = sm.id
            INNER JOIN stock_reference_sale_rel srsr
                ON srsr.reference_id = smr.reference_id
            INNER JOIN sale_order so
                ON so.id = srsr.sale_id
            INNER JOIN stock_location sl
                ON sl.id = sp.location_dest_id
               AND sl.usage = 'customer'
            WHERE sp.updated_in_shopify = TRUE
              AND sp.state != 'cancel'
              AND so.shopify_instance_id = %s
        """
        result = self._filter_query_by_sort(shipped_query, 'so.date_order')
        order_ids = [data.get('id') for data in result]
        view = self.env.ref('shopify_ept.action_shopify_sales_order').sudo().read()[0]
        action = self.prepare_action(view, [('id', 'in', order_ids)])
        return {'order_count': len(order_ids), 'order_action': action}

    def get_total_products(self):
        """
        Use: To get the list of products exported from shopify instance
        Task: 167063
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 29/10/20
        :return: total number of shopify products ids and action for products
        """
        product_data = {}
        self.env.cr.execute("""select count(id) as total_count from shopify_product_template_ept where
                        exported_in_shopify = True and shopify_instance_id = %s""", (self.id,))
        result = self.env.cr.dictfetchall()
        if result:
            total_count = result[0].get('total_count')
        view = self.env.ref('shopify_ept.action_shopify_product_exported_ept').sudo().read()[0]
        action = self.prepare_action(view, [('exported_in_shopify', '=', True), ('shopify_instance_id', '=', self.id)])
        product_data.update({'product_count': total_count, 'product_action': action})
        return product_data

    def get_customers(self):
        """
        Use: To get the list of customers with shopify instance for current shopify instance
        Task: 167063
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 29/10/20
        :return: total number of customer ids and action for customers
        """
        customer_data = {}
        self.env.cr.execute("""select partner_id from shopify_res_partner_ept where shopify_instance_id = %s"""
                         , (self.id,))
        result = self.env.cr.dictfetchall()
        customer_ids = [data.get('partner_id') for data in result]
        view = self.env.ref('shopify_ept.action_shopify_partner_form').sudo().read()[0]
        action = self.prepare_action(view, [('id', 'in', customer_ids), ('active', 'in', [True, False])])
        customer_data.update({'customer_count': len(customer_ids), 'customer_action': action})
        return customer_data

    def get_refund(self):
        """
        Use: To get the list of refund orders of shopify instance for current shopify instance
        Task: 167349
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 03/11/20
        :return: total number of refund order ids and action for customers
        """
        refund_query = """
            SELECT id 
            FROM account_move 
            WHERE shopify_instance_id = %s 
            AND move_type = 'out_refund'
        """
        result = self._filter_query_by_sort(refund_query, 'invoice_date')
        refund_ids = [data.get('id') for data in result]
        view = self.env.ref('shopify_ept.action_refund_shopify_invoices').sudo().read()[0]
        action = self.prepare_action(view, [('id', 'in', refund_ids)])
        return {'refund_count': len(refund_ids), 'refund_action': action}

    def prepare_action(self, view, domain):
        """
        Use: To prepare action dictionary
        Task: 167063
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 29/10/20
        :return: action details
        """
        action = {
            'name': view.get('name'),
            'type': view.get('type'),
            'domain': domain,
            'view_mode': view.get('view_mode'),
            'view_id': view.get('view_id')[0] if view.get('view_id') else False,
            'views': view.get('views'),
            'res_model': view.get('res_model'),
            'target': view.get('target'),
        }

        if 'tree' in action['views'][0]:
            action['views'][0] = (action['view_id'], 'list')
        return action

    @api.model
    def perform_operation(self, record_id):
        """
        Use: To prepare shopify operation action
        Task: 167063
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 29/10/20
        :return: shopify operation action details
        """
        view = self.env.ref('shopify_ept.action_wizard_shopify_instance_import_export_operations').sudo().read()[0]
        action = self.prepare_action(view, [])
        action.update({'context': {'default_shopify_instance_id': record_id}})
        return action

    @api.model
    def open_report(self, record_id):
        """
        Use: To prepare shopify report action
        Task: 167063
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 29/10/20
        :return: shopify report action details
        """
        sale_report_obj = self.env['sale.report']
        view = sale_report_obj.shopify_sale_report()
        # view = self.env.ref('shopify_ept.shopify_sale_report_action_dashboard').sudo().read()[0]
        action = self.prepare_action(view, [('shopify_instance_id', '=', record_id)])
        action.update({'context': {'search_default_shopify_instances': record_id, 'search_default_Sales': 1,
                                   'search_default_filter_date': 1}})
        return action

    @api.model
    def open_logs(self, record_id):
        """
        Use: To prepare shopify logs action
        Task: 167063
        Added by: Preet Bhatti @Emipro Technologies
        Added on: 29/10/20
        :return: shopify logs action details
        """
        view = self.env.ref('shopify_ept.action_shopify_common_log_line_ept').sudo().read()[0]
        return self.prepare_action(view, [('shopify_instance_id', '=', record_id)])

    @api.model_create_multi
    def create(self, vals):
        """
        Inherited for creating generic POS customer.
        :param vals: It contains value of instance fields.
        @author: Maulik Barad on date 25-Feb-2020.
        """
        res_partner_obj = self.env["res.partner"]
        for val in vals:
            if val.get("shopify_host").endswith('/'):
                val["shopify_host"] = val.get("shopify_host").rstrip('/')

            customer_vals = {"name": "POS Customer(%s)" % val.get("name"), "customer_rank": 1}
            customer = res_partner_obj.create(customer_vals)

            sales_team = self.create_sales_channel(val.get('name'))

            val.update({"shopify_default_pos_customer_id": customer.id, "shopify_section_id": sales_team.id})
        return super(ShopifyInstanceEpt, self).create(vals)

    def create_sales_channel(self, name):
        """
        It creates new sales team for Shopify instance.
        :param name: Name of sale channel and it always the name of the instance.
        @author: Maulik Barad on Date 09-Jan-2019.
        """
        crm_team_obj = self.env['crm.team']
        vals = {
            'name': name,
        }
        return crm_team_obj.sudo().create(vals)

    def shopify_test_connection(self, vals=False):
        """This method used to check the connection between Odoo and Shopify.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 04/10/2019.
        """
        self.connect_in_shopify(vals)
        try:
            shop_id = shopify.Shop.current()
        except ForbiddenAccess as error:
            if error.response.body:
                errors = json.loads(error.response.body.decode())
                raise UserError(_("%s\n%s\n%s" % (error.response.code, error.response.msg, errors.get("errors"))))
        except Exception as error:
            raise UserError(error)
        shop_detail = shop_id.to_dict()
        self.write({"shopify_store_time_zone": shop_detail.get("iana_timezone")})
        title = _("Shopify")
        message = _("Connection Test Succeeded!")
        self.env['bus.bus']._sendone(self.env.user.partner_id, 'simple_notification',
                                     {'title': title, 'message': message, 'sticky': False, 'warning': True})
        self.env['onboarding.onboarding.step'].sudo().action_validate_step(
            "shopify_ept.onboarding_onboarding_step_shopify")
        return True

    def connect_in_shopify(self, vals=False):
        """
        This method used to connect with Odoo to Shopify.
        @param vals: Dictionary of api_key and password.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 07/10/2019.
        @change: Maulik Barad on Date 01-Oct-2020.
        """
        if self.env.context.get('skip_check_use_graphql_api'):
            return True
        # Skip pyactiveresource.connection INFO logs (GET/POST/--> lines) when
        # skip_api_logs = True in Shopify Settings.
        skip = self.env['ir.config_parameter'].sudo().get_param(
            'shopify_ept.skip_api_logs', 'False'
        )
        set_suppress_connection_info_logs(skip == 'True')
        if vals:
            api_key = vals.get("shopify_api_key")
            password = vals.get("shopify_password")
        else:
            api_key = self.shopify_api_key
            password = self.shopify_password

        shop_url = self.prepare_shopify_shop_url(self.shopify_host, api_key, password)

        shopify.ShopifyResource.set_site(shop_url)
        return True

    def prepare_shopify_shop_url(self, host, api_key, password):
        """ This method is used to prepare a shop URL.
            @return shop_url
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 26 October 2020 .
            Task_id: 167537 - Code refactoring
        """
        shop = host.split("//")
        if len(shop) == 2:
            shop_url = shop[0] + "//" + api_key + ":" + password + "@" + shop[1] + "/admin/api/" + SHOPIFY_API_VERSION
        else:
            shop_url = "https://" + api_key + ":" + password + "@" + shop[0] + "/admin/api/" + SHOPIFY_API_VERSION

        return shop_url

    def toggle_active(self):
        """
        Method overridden for archiving the instance from the action menu.
        @author: Maulik Barad on Date 06-Oct-2020.
        """
        action = self[0].with_context(active_ids=self.ids).action_shopify_active_archive_instance() if self else True
        return action

    def shopify_action_archive_unarchive(self):
        """
        This method used to active and unarchive instance and base on the active/unarchive instance-related
        data also, archive/unarchive.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 07/10/2019.
        """
        domain = [("shopify_instance_id", "=", self.id)]
        shopify_template_obj = self.env["shopify.product.template.ept"]
        sale_auto_workflow_configuration_obj = self.env["sale.auto.workflow.configuration.ept"]
        shopify_payment_gateway_obj = self.env["shopify.payment.gateway.ept"]
        shopify_webhook_obj = self.env["shopify.webhook.ept"]
        shopify_location_obj = self.env["shopify.location.ept"]
        data_queue_mixin_obj = self.env['data.queue.mixin.ept']
        if self.active:
            activate = {"active": False}
            domain_for_webhook_location = [("instance_id", "=", self.id)]

            self.write(activate)
            self.change_auto_cron_status()
            shopify_webhook_obj.search(domain_for_webhook_location).unlink()
            shopify_location_obj.search(domain_for_webhook_location).write(activate)
            data_queue_mixin_obj.delete_data_queue_ept(is_delete_queue=True)
        else:
            self.shopify_test_connection()
            activate = {"active": True}
            domain.append(("active", "=", False))
            self.write(activate)
            shopify_location_obj.import_shopify_locations(self)

        shopify_template_obj.search(domain).write(activate)
        sale_auto_workflow_configuration_obj.search(domain).write(activate)
        shopify_payment_gateway_obj.search(domain).write(activate)
        return True

    def change_auto_cron_status(self):
        """
        After connect or disconnect the shopify instance disable all the Scheduled Actions.
        @author: Angel Patel @Emipro Technologies Pvt. Ltd.
        Task Id : 157716
        """
        try:
            stock_cron_exist = self.env.ref('shopify_ept.ir_cron_shopify_auto_export_inventory_instance_%d' % self.id)
        except Exception:
            stock_cron_exist = False
            _logger.exception("Could not find stock export cron for instance %d", self.id)
        try:
            order_cron_exist = self.env.ref('shopify_ept.ir_cron_shopify_auto_import_order_instance_%d' % self.id)
        except Exception:
            order_cron_exist = False
            _logger.exception("Could not find order import cron for instance %d", self.id)
        try:
            order_status_cron_exist = self.env.ref(
                'shopify_ept.ir_cron_shopify_auto_update_order_status_instance_%d' % self.id)
        except Exception:
            order_status_cron_exist = False
            _logger.exception("Could not find order status cron for instance %d", self.id)

        if stock_cron_exist:
            stock_cron_exist.write({'active': False})
        if order_cron_exist:
            order_cron_exist.write({'active': False})
        if order_status_cron_exist:
            order_status_cron_exist.write({'active': False})

    def cron_configuration_action(self):
        """
        Open wizard of "Configure Schedulers" on button click in the instance form view.
        @author: Maulik Barad on Date 28-Sep-2020.
        """
        action = self.env.ref('shopify_ept.action_wizard_shopify_cron_configuration_ept').read()[0]
        action['context'] = {'shopify_instance_id': self.id}
        return action

    def action_redirect_to_ir_cron(self):
        """
        Redirect to ir.cron model with cron name like shopify
        :return:  action
        @author: Angel Patel @Emipro Technologies Pvt. Ltd.
        Task Id : 157716
        """
        action = self.env.ref('base.ir_cron_act').read()[0]
        action['domain'] = [('name', 'ilike', self.name), ('name', 'ilike', 'shopify'), ('active', '=', True)]
        return action

    def list_of_topic_for_webhook(self, event):
        """
        This method is prepare the list of all the event topic while the webhook create, and return that list of topic.
        :param event: having 'product' or 'customer' or 'order'
        :return: topic_list
        @author: Angel Patel on Date 17/01/2020.
        """
        topic_list = []
        if event == 'product':
            topic_list = ["products/create", "products/update", "products/delete"]
        if event == 'customer':
            topic_list = ["customers/create", "customers/update"]
        if event == 'order':
            topic_list = ["orders/updated"]
        return topic_list

    def configure_shopify_product_webhook(self):
        """
        Creates or activates all product related webhooks, when it is True.
        Inactive all product related webhooks, when it is False.
        @author: Haresh Mori on Date 09-Jan-2020.
        :Modify by Angel Patel on date 17/01/2020, call list_of_topic_for_webhook method for get 'product' list events
        """
        topic_list = self.list_of_topic_for_webhook("product")
        self.configure_webhooks(topic_list)

    def configure_shopify_customer_webhook(self):
        """
        Creates or activates all customer related webhooks, when it is true from shopify configuration.
        Inactive all customer related webhooks, when it is false from shopify configuration.
        @author: Angel Patel on Date 10/01/2020.
        :Modify by Angel Patel on date 17/01/2020, call list_of_topic_for_webhook method for get 'customer' list events
        """
        topic_list = self.list_of_topic_for_webhook("customer")
        self.configure_webhooks(topic_list)

    def configure_shopify_order_webhook(self):
        """
        Creates or activates all order related webhooks, when it is true from shopify configuration.
        Inactive all order related webhooks, when it is false from shopify configuration.
        @author: Haresh Mori on Date 10/01/2020.
        """
        topic_list = self.list_of_topic_for_webhook("order")
        self.configure_webhooks(topic_list)

    def configure_webhooks(self, topic_list):
        """
        This method is used to create/active and inactive webhooks base on Shopify configuration.
        @author: Haresh Mori on Date 09/01/2020.
        """
        webhook_obj = self.env["shopify.webhook.ept"]

        resource = topic_list[0].split('/')[0]
        instance_id = self.id
        available_webhooks = webhook_obj.search(
            [("webhook_action", "in", topic_list), ("instance_id", "=", instance_id)])

        if getattr(self, "create_shopify_%s_webhook" % resource):
            if available_webhooks:
                available_webhooks.write({'state': 'active'})
                _logger.info("%s Webhooks are activated of instance %s.", resource, self.name)
                topic_list = list(set(topic_list) - set(available_webhooks.mapped("webhook_action")))

            for topic in topic_list:
                webhook_obj.create({"webhook_name": self.name + "_" + topic.replace("/", "_"),
                                    "webhook_action": topic, "instance_id": instance_id})
                _logger.info("Webhook for %s of instance %s created.", topic, self.name)
        else:
            if available_webhooks:
                available_webhooks.write({'state': 'inactive'})
                _logger.info("%s Webhooks are paused of instance %s.", resource, self.name)

    def refresh_webhooks(self):
        """
        Refreshes webhooks by comparing Shopify webhooks with Odoo records.
        Uses GraphQL API if enabled, otherwise falls back to REST API.
        """
        webhook_ids = []
        if self.use_graphql_api:
            # Use GraphQL API
            client = self.get_graphql_client()
            query = shopify_graphql.WebhookQueryHelper(client).build_get_webhook_subscriptions_query(first=100)
            response = client.execute(query)
            # Parse webhook IDs from GraphQL response
            edges = response.get('data', {}).get('webhookSubscriptions', {}).get('edges', [])
            for edge in edges:
                node = edge.get('node', {})
                webhook_id = node.get('id')
                if webhook_id:
                    webhook_ids.append(webhook_id)
            _logger.info("Emipro-Webhook: Current webhook present in shopify (GraphQL) is %s", webhook_ids)
        else:
            # Use REST API (legacy)
            self.connect_in_shopify()
            shopify_webhook = shopify.Webhook()
            responses = shopify_webhook.find()
            for webhook in responses:
                webhook_ids.append(str(webhook.id))
            _logger.info("Emipro-Webhook: Current webhook present in shopify (REST) is %s", webhook_ids)

        webhook_obj = self.env['shopify.webhook.ept'].search(
            [('instance_id', '=', self.id), ('webhook_id', 'not in', webhook_ids)])
        _logger.info("Emipro-Webhook: Webhook not present in odoo is %s", webhook_obj)

        if webhook_obj:
            _logger.info("Emipro-Webhook: deleting webhook records from Odoo via ORM: %s", webhook_obj.ids)
            webhook_obj.unlink()
        _logger.info("Emipro-Webhook: refresh process done")
        return True

    def search_shopify_instance(self):
        """ This method used to search the shopify instance.
            :return: Record of shopify instance
            @author: Dipak Gogiya, 26/09/2020
            @Task:   166992
        """
        company = self.env.company or self.env.user.company_id
        instance = self.search(
            [('is_instance_create_from_onboarding_panel', '=', True),
             ('is_onboarding_configurations_done', '=', False),
             ('shopify_company_id', '=', company.id)], limit=1, order='id desc')
        if not instance:
            instance = self.search([('shopify_company_id', '=', company.id),
                                    ('is_onboarding_configurations_done', '=', False)],
                                   limit=1, order='id desc')
            instance.write({'is_instance_create_from_onboarding_panel': True})
        return instance

    def open_reset_credentials_wizard(self):
        """
        Open wizard for reset credentials.
        @author: Maulik Barad on Date 01-Oct-2020.
        """
        view_id = self.env.ref('shopify_ept.view_reset_credentials_form').id
        action = self.env.ref('shopify_ept.res_config_action_shopify_instance').read()[0]
        action.update({"name": "Reset Credentials",
                       "context": {'shopify_instance_id': self.id,
                                   "default_name": self.name,
                                   "default_shopify_host": self.shopify_host},
                       "view_id": (view_id, "Reset Credentials"),
                       "views": [(view_id, "form")]})
        return action

    def action_shopify_active_archive_instance(self):
        """
        This method is used to open a wizard to display the information related to how many data will be
        archived/deleted while instance Active/Archive.
        @author: Maulik Barad on Date 20-Nov-2020.
        """
        view = self.env.ref('shopify_ept.view_active_archive_shopify_instance')
        return {
            'name': _('Instance Active/Archive Details'),
            'type': 'ir.actions.act_window',
            'res_model': 'shopify.queue.process.ept',
            'views': [(view.id, 'form')],
            'target': 'new',
            'context': self.env.context,
        }

    def get_shopify_cron_execution_time(self, cron_name):
        """
        This method is used to get the interval time of the cron.
        @param cron_name: External ID of the Cron.
        @return: Interval time in seconds.
        @author: Maulik Barad on Date 25-Nov-2020.
        """
        process_queue_cron = self.env.ref(cron_name, False)
        if not process_queue_cron:
            raise UserError(_("Please upgrade the module. \n Maybe the job has been deleted, it will be recreated at "
                              "the time of module upgrade."))
        # Set static time as per odoo timeout limit for resolve issue related time more than 15 minutes(900s)
        interval_in_seconds = 840
        return interval_in_seconds

    @api.model
    def create_update_correct_shipping_product(self):
        """
            This method searchs for the instance shipping product and if not found then
            create a new shipiing product for it.
            @param : instance - obj of the Instance.
            @author : Gopal Chouhan on 20 May,2025
        """
        shipping_product = self.env.ref('shopify_ept.shopify_shipping_product', False)
        all_instances = self.search([])
        for inst in all_instances:
            instance_shipping_product = self.env.ref(f'shopify_ept.shopify_shipping_product{inst.id}', False)
            if instance_shipping_product:
                continue
            vals = {
                'name': f'{shipping_product.name} {inst.id}',
                'default_code': f'{shipping_product.default_code}{inst.id}'
            }
            new_shipping_product = shipping_product.copy(default=vals)
            message = (f'Shipping Product created for Shopify Instance [{inst.name}].\n '
                       f'Do Not change the internal Reference manually for this product')
            new_shipping_product.message_post(body=message)
            new_shipping_product.product_tmpl_id.message_post(body=message)
            name = f'shopify_shipping_product{inst.id}'
            self.create_ir_module_data_rec(name, 'product.product',
                                           new_shipping_product)

            inst.shipping_product_id = new_shipping_product
        return True

    def create_ir_module_data_rec(self, name, model, record):

        """ This method is used to create the Ir model data record for any record of any model exist in database.
            @param: name - name of the data record
            @param : model - model for which this record to be created.
            @param : record : record required for the res id.
            @author : Gopal chouhan on may 20, 2025
        """
        self.env['ir.model.data'].create({'module': 'shopify_ept',
                                          'name': name,
                                          'model': model,
                                          'res_id': record.id,
                                          'noupdate': True})
        
    def shopify_bulk_order_import(self, shop_domain, access_token, fields=None, poll_interval=10, timeout=600):
        """
        Run Shopify bulk order import and download the result file.
        :param shop_domain: Shopify shop domain
        :param access_token: Shopify access token
        :param fields: List of order fields to fetch
        :param poll_interval: Seconds between status checks
        :param timeout: Max seconds to wait for completion
        :return: Path to downloaded file or None
        """
        client = ShopifyGraphQLClient(access_token, shop_domain)
        helper = ShopifyBulkOrderHelper(client)
        file_path = helper.run_bulk_order_export(fields=fields, poll_interval=poll_interval, timeout=timeout)
        return file_path

    def get_graphql_client(self):
        self.ensure_one()
        return shopify_graphql.ShopifyGraphQLClient(self.shopify_password, self.shopify_host,
                                                    self.shopify_enable_gql_debug)

    def is_translations_needed(self):
        """
        Returns True when multi-language product sync is enabled, GraphQL is on,
        and at least 2 language mappings are configured (one primary + one secondary).
        """
        self.ensure_one()
        return (self.enable_multi_lang_sync and self.use_graphql_api and len(self.shopify_lang_mapping_ids) >= 2)

    def get_primary_locale(self):
        """Return the primary Shopify locale code (e.g. 'en')."""
        self.ensure_one()
        primary = self.shopify_lang_mapping_ids.filtered('is_primary')[:1]
        if primary:
            return primary.shopify_locale
        return self.shopify_lang_id.code.split('_')[0] if self.shopify_lang_id else 'en'

    def get_secondary_lang_mappings(self):
        """Return all non-primary language mapping records."""
        self.ensure_one()
        return self.shopify_lang_mapping_ids.filtered(lambda m: not m.is_primary)

    def action_sync_shopify_languages(self):
        """
        Fetch published locales from the Shopify store via GraphQL and
        auto-create / update shopify.lang.mapping.ept records for this instance.
        """
        self.ensure_one()

        try:
            client = self.get_graphql_client()
            helper = TranslationQueryHelper(client)
            shop_locales = helper.fetch_shop_locales()
        except Exception as e:
            raise UserError(_("Failed to fetch languages from Shopify:\n%s") % str(e))

        if not shop_locales:
            raise UserError(_("No published languages were returned by Shopify."))

        LangMapping = self.env['shopify.lang.mapping.ept']
        created, updated = [], []

        for loc in shop_locales:
            locale_code = (loc.get('locale') or '').strip()
            is_primary = bool(loc.get('primary'))

            if not locale_code:
                continue

            existing = LangMapping.search(
                [('shopify_instance_id', '=', self.id), ('shopify_locale', '=', locale_code)], limit=1)

            if existing:
                existing.write({'is_primary': is_primary})
                updated.append(locale_code)
            else:
                LangMapping.create({
                    'shopify_instance_id': self.id,
                    'shopify_locale': locale_code,
                    'is_primary': is_primary,
                })
                created.append(locale_code)

        msg_parts = []
        if created:
            msg_parts.append(_("Created mappings for: %s\n" "Please set the Odoo Language for each new row.") % ', '.join(created))
        if updated:
            msg_parts.append(_("Updated primary flag for: %s") % ', '.join(updated))
        if not msg_parts:
            msg_parts.append(_("No changes were made."))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Shopify Language Sync'),
                'message': '\n'.join(msg_parts),
                'type': 'success' if created else 'info',
                'sticky': bool(created),
            },
        }

    def get_payout_journal(self, currency):
        """
        This method is used to search the payout journal based on configuration.
        """
        self.ensure_one()
        default_journal = self.shopify_settlement_report_journal_id
        if not self.enable_multi_currency_payout_report:
            if not default_journal:
                raise UserError(_("Please configure the 'Payout Report Journal' in the Shopify Instance."))

            journal_currency = (default_journal.currency_id or default_journal.company_id.currency_id)
            if journal_currency != currency:
                raise UserError(_(
                    "A payout with currency '%(currency)s' was received, but the "
                    "configured payout journal uses '%(journal_currency)s'.\n\n"
                    "If your Shopify store uses Shopify Payments Multi-Currency "
                    "Payouts, please enable 'Multi-Currency Payout Report' and "
                    "configure the required journals under the "
                    "'Payout Journal Configuration' tab."
                ) % {"currency": currency.display_name, "journal_currency": journal_currency.display_name, })
            return default_journal
        mapping = self.shopify_payout_journal_configuration_ids.filtered(lambda rec: rec.currency_id == currency)[:1]
        if mapping:
            return mapping.journal_id
        raise UserError(_(
            "No payout journal is configured for currency '%s'.\n\n"
            "Please configure a bank journal for this currency in the "
            "'Payout Journal Configuration' tab."
        ) % currency.display_name)