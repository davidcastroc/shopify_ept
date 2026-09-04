# -*- coding: utf-8 -*-
import logging
from odoo import fields, models, api, _
from ..shopify_graphql.queries import ChannelQueryHelper

_logger = logging.getLogger("Shopify Channel")

# Predefined channel → analytic account name / code mapping.
# Keys match the app name (lowercased) or the name-derived handle slug.
CHANNEL_ANALYTIC_MAP = {
    "online store": ("Shopify - Online Store", "SHP-ONLINE"),
    "online_store": ("Shopify - Online Store", "SHP-ONLINE"),
    "web": ("Shopify - Online Store", "SHP-ONLINE"),
    "point of sale": ("Shopify - POS", "SHP-POS"),
    "pos": ("Shopify - POS", "SHP-POS"),
    "facebook": ("Shopify - Facebook", "SHP-FB"),
    "instagram": ("Shopify - Instagram", "SHP-INSTA"),
    "facebook_instagram": ("Shopify - Facebook", "SHP-FB"),
}

# Plan name that groups all Shopify channel analytic accounts
SHOPIFY_CHANNEL_PLAN_NAME = "Shopify Channels"


class ShopifyChannelEpt(models.Model):
    _name = "shopify.channel.ept"
    _description = "Shopify Sales Channel"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    
    name = fields.Char(string="Channel Name", required=True)
    shopify_id = fields.Char(string="Shopify ID", copy=False,
                             help="Numeric ID of the Shopify Channel/Publication.")
    app_id = fields.Char(string="Shopify Channel ID", copy=False,
                         help="Numeric ID of the app backing this Shopify Channel/Publication. "
                              "Used to match Order.channelInformation.app.id during order import.")
    handle = fields.Char(string="Channel Handle",
                         help="Derived slug (e.g. 'online_store', 'pos'). Used as a display "
                              "label and fallback key; primary matching uses app_id.")
    shopify_instance_id = fields.Many2one(
        "shopify.instance.ept", string="Shopify Instance",
        required=True, ondelete="cascade", index=True,
    )
    analytic_account_id = fields.Many2one(
        "account.analytic.account", string="Analytic Account",
        help="Odoo analytic account for orders from this channel.",
        tracking=True
    )
    exclude_from_import = fields.Boolean(
        string="Exclude Orders from Import",
        default=False,
        tracking=True,
        help="When enabled, orders placed through this sales channel will be skipped "
             "during the Shopify order import process and will not be created in Odoo.",
    )

    @api.model
    def _get_or_create_channel_analytic_plan(self):
        """
        This method is used for find or create analytic plan for shopify channel.
        """
        plan_obj = self.env['account.analytic.plan']
        plan = plan_obj.search([('name', '=', SHOPIFY_CHANNEL_PLAN_NAME)], limit=1)
        if not plan:
            plan = plan_obj.create({'name': SHOPIFY_CHANNEL_PLAN_NAME})
            _logger.info("Created analytic plan '%s'.", SHOPIFY_CHANNEL_PLAN_NAME)
        return plan
    
    @api.model
    def _get_or_create_analytic_account(self, account_name, account_code, plan, company):
        """
        This method is used for find or crete analytic account for shopify channel.
        """
        analytic_obj = self.env['account.analytic.account']
        domain = [('code', '=', account_code), ('plan_id', '=', plan.id)]
        if company:
            domain += ['|', ('company_id', '=', False), ('company_id', '=', company.id)]
        account = analytic_obj.search(domain, limit=1)
        if not account:
            vals = {'name': account_name, 'code': account_code, 'plan_id': plan.id}
            if company:
                vals['company_id'] = company.id
            account = analytic_obj.create(vals)
            _logger.info("Auto-created analytic account '%s' (%s).", account_name, account_code)
        return account
    
    def action_sync_channels(self):
        """
        This method is used for sync shopify channel.
        """
        instance_ids = self.env.context.get('active_ids') or []
        if not instance_ids:
            instances = self.env["shopify.instance.ept"].search([('active', '=', True)])
        else:
            instances = self.env["shopify.instance.ept"].browse(instance_ids)
        for instance in instances:
            self.sync_shopify_channels(instance)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Shopify Channels Synced"),
                'message': _("Sales channels have been synchronised successfully."),
                'sticky': False,
                'type': 'success',
            }
        }
    
    @api.model
    def sync_shopify_channels(self, instance):
        """
        Fetch all channels from Shopify and create/update shopify.channel.ept
        records.  Stores app.id so that order matching can use
        channelInformation.app.id (the only cross-type stable identifier).
        """
        client = instance.get_graphql_client()
        nodes = ChannelQueryHelper(client).fetch_all_channels()
        
        if not nodes:
            _logger.info("No channel data returned for instance %s.", instance.name)
            return 0
        
        plan = self._get_or_create_channel_analytic_plan()
        company = instance.shopify_company_id or False
        
        for node in nodes:
            raw_id = self.gid_to_int(node.get("id", ""))
            name = node.get("name") or ""
            
            app_data = node.get("app") or {}
            raw_app_id = self.gid_to_int(app_data.get("id", "")) if app_data.get("id") else None
            
            # Derive a display handle from the name (for UI + CHANNEL_ANALYTIC_MAP lookup)
            name_lower = name.lower().strip()
            predefined = CHANNEL_ANALYTIC_MAP.get(name_lower)
            if predefined:
                handle = name_lower.replace(" ", "_")
                analytic_name, analytic_code = predefined
            else:
                handle = name_lower.replace("&", "").replace(" ", "_").replace("-", "_")
                while "__" in handle:
                    handle = handle.replace("__", "_")
                handle = handle.strip("_")
                safe = handle.upper().replace("_", "-")[:12]
                analytic_name = "Shopify - %s" % name
                analytic_code = "SHP-%s" % safe
            
            analytic_account = self._get_or_create_analytic_account(
                analytic_name, analytic_code, plan, company
            )
            
            existing = self.search([
                ('shopify_instance_id', '=', instance.id),
                ('shopify_id', '=', raw_id),
            ], limit=1)
            
            vals = {
                'name': name,
                'shopify_id': raw_id,
                'app_id': raw_app_id,
                'handle': handle,
                'shopify_instance_id': instance.id,
            }
            if existing:
                if not existing.analytic_account_id:
                    vals['analytic_account_id'] = analytic_account.id
                existing.write(vals)
                _logger.info("Updated channel '%s' (app_id: %s) for instance %s.",
                             name, raw_app_id, instance.name)
            else:
                vals['analytic_account_id'] = analytic_account.id
                self.create(vals)
                _logger.info("Created channel '%s' (app_id: %s, analytic: %s) for instance %s.",
                             name, raw_app_id, analytic_account.name, instance.name)
        
        _logger.info("Synced %d channel(s) for instance '%s'.", len(nodes), instance.name)
        return len(nodes)
    
    @api.model
    def get_channel_analytic(self, instance, channel_handle, channel_app_id=None):
        """
        Return the analytic_account_id for the given order channel.
        :param instance: shopify.instance.ept record
        :param channel_handle: str handle from channelInformation.channelDefinition.handle
        :param channel_app_id: int/str app id from channelInformation.app.id
        :return: account.analytic.account record or False
        """
        if not instance.shopify_is_use_analytic_account:
            return False

        if not instance.use_channel_analytic_account:
            return False

        channel = False

        # 1. Match by app_id (most reliable)
        if channel_app_id:
            channel = self.search([
                ('shopify_instance_id', '=', instance.id),
                ('app_id', '=', str(channel_app_id)),
            ], limit=1)
            if channel:
                _logger.info("Channel matched by app_id=%s → '%s'", channel_app_id, channel.name)

        # 2. Fallback: match by handle
        if not channel and channel_handle:
            channel = self.search([
                ('shopify_instance_id', '=', instance.id),
                ('handle', '=', channel_handle),
            ], limit=1)
            if channel:
                _logger.info("Channel matched by handle='%s' → '%s'", channel_handle, channel.name)

        if channel and channel.analytic_account_id:
            return channel.analytic_account_id

        if not channel:
            _logger.info(
                "No channel match (app_id=%s, handle=%s) for instance '%s'. "
                "Falling back to instance analytic.",
                channel_app_id, channel_handle, instance.name,
            )
        return False
    
    @api.model
    def is_channel_excluded_for_order_ept(self, instance, channel_handle, channel_app_id=None):
        """
        Return True when the order's sales channel is configured to be excluded from import.
        Matching priority:
          1. app_id  (most reliable cross-type identifier)
          2. handle  (fallback)
        :param instance: shopify.instance.ept record
        :param channel_handle: str – channelInformation.channelDefinition.handle
        :param channel_app_id: int/str – channelInformation.app.id (numeric)
        :return: bool
        """
        channel = False
        if channel_app_id:
            channel = self.search([('shopify_instance_id', '=', instance.id), ('app_id', '=', str(channel_app_id)),
                                   ('exclude_from_import', '=', True), ], limit=1)

        if not channel and channel_handle:
            channel = self.search([('shopify_instance_id', '=', instance.id), ('handle', '=', channel_handle),
                                   ('exclude_from_import', '=', True)], limit=1)

        if channel:
            _logger.info("Sales channel '%s' (app_id=%s, handle=%s) is configured to exclude orders from import.",
                         channel.name, channel_app_id, channel_handle)
            return True
        return False

    @staticmethod
    def gid_to_int(gid):
        """Convert Shopify GID → integer ID."""
        if not gid:
            return None
        try:
            return int(gid.split("/")[-1])
        except Exception:
            return gid
