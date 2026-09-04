# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger("Shopify Market")

MARKET_QUERY = """
query GetMarkets($first: Int!, $after: String) {
  markets(first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      name
      handle
      primary
      enabled
      currencySettings {
        baseCurrency {
          currencyCode
        }
      }
      regions(first: 250) {
        nodes {
          ... on MarketRegionCountry {
            code
            name
          }
        }
      }
      webPresence {
        domain {
          host
        }
        subfolderSuffix
      }
    }
  }
}
"""


class ShopifyMarketEpt(models.Model):
    _name = "shopify.market.ept"
    _description = "Shopify Market"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"
    _order = "primary desc, name asc"

    # ─── Identity Fields (auto-filled from Shopify) ────────────────────────────
    name = fields.Char(
        string="Market Name",
        required=True,
        tracking=True,
        help="Name of the Shopify market (e.g., United States, Europe).",
    )
    shopify_market_id = fields.Char(
        string="Shopify Market ID",
        copy=False,
        help="Shopify GraphQL Global ID for this market.",
    )
    shopify_instance_id = fields.Many2one(
        "shopify.instance.ept",
        string="Instance",
        required=True,
        ondelete="cascade",
        help="Shopify instance this market belongs to.",
    )
    handle = fields.Char(
        string="Handle",
        help="URL-friendly slug for the market (e.g., 'us', 'europe').",
    )
    primary = fields.Boolean(
        string="Primary Market?",
        default=False,
        tracking=True,
        help="If True, this is the default/primary Shopify market.",
    )
    enabled = fields.Boolean(
        string="Enabled in Shopify?",
        default=True,
        tracking=True,
        help="Reflects whether this market is active in Shopify.",
    )
    country_ids = fields.Many2many(
        "res.country",
        "shopify_market_country_rel",
        "market_id",
        "country_id",
        string="Countries",
        help="Countries/regions that belong to this market.",
    )
    web_presence_domain = fields.Char(
        string="Web Domain",
        help="Domain or subfolder used for this market (e.g., store.com/eu).",
    )
    active = fields.Boolean(default=True)

    # ─── Mapping Fields (manually configured by user) ──────────────────────────
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        tracking=True,
        help="Odoo company that will own orders from this market. "
             "Set this FIRST as it filters all other fields.",
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Warehouse",
        tracking=True,
        domain="[('company_id', '=', company_id)]",
        help="Warehouse used for orders from this market.",
    )
    pricelist_id = fields.Many2one(
        "product.pricelist",
        string="Pricelist",
        tracking=True,
        domain="[('company_id', '=', company_id)]",
        help="Pricelist applied to orders from this market.",
    )
    fiscal_position_id = fields.Many2one(
        "account.fiscal.position",
        string="Fiscal Position",
        tracking=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Fiscal position applied to orders from this market. "
             "Automatically remaps taxes on order lines.",
    )
    lang_id = fields.Many2one(
        "res.lang",
        string="Language",
        help="Language used for customer-facing documents (invoices, pickings).",
    )
    auto_workflow_id = fields.Many2one(
        "sale.workflow.process.ept",
        string="Auto Workflow",
        tracking=True,
        help="Auto invoice/payment workflow applied to orders from this market.",
    )
    financial_status_configuration_ids = fields.One2many(
        "market.auto.workflow.configuration.ept",
        "shopify_market_id",
        string="Financial Status Configurations",tracking=True,
        help="Market-specific mapping of financial status, gateway and order status to auto workflow.",
    )
    apply_tax_in_order = fields.Selection(
        selection=[
            ("odoo_tax", "Odoo Default Tax Behaviour"),
            ("create_shopify_tax", "Create New Tax If Not Found"),
        ],
        string="Tax Behaviour",
        tracking=True,
        help="Override the instance-level tax behaviour for orders from this market.\n"
             "• Leave empty to inherit the setting from the Shopify instance.\n"
             "• Odoo Default: taxes are determined by Odoo's fiscal position / product configuration.\n"
             "• Create New Tax: taxes are taken from Shopify's order response and created in Odoo if missing.",
    )

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
    
    invoice_tax_account_id = fields.Many2one('account.account', string='Invoice Tax Account')
    credit_tax_account_id = fields.Many2one('account.account', string='Credit Tax Account')

    order_count = fields.Integer(
        string="Orders",
        compute="_compute_order_count_ept",
    )

    credit_note_payment_journal = fields.Many2one("account.journal", tracking=True, string="Credit Note Payment Journal",
                                                  help=" Selected Journal will be set in Credit note Payment journal.")
    # ─── Constraints ───────────────────────────────────────────────────────────
    _unique_shopify_market_instance = models.Constraint(
        "UNIQUE(shopify_market_id, shopify_instance_id)",
        "A market with this Shopify ID already exists for this instance.",
    )

    @api.constrains("company_id")
    def _check_company_change_with_orders(self):
        """
        Prevent changing the company on a market that already has
        sale orders linked to it. This protects data integrity — orders
        belong to the company they were created in and cannot be
        silently re-routed by changing the market's company.
        """
        for rec in self:
            if not rec.company_id:
                continue
            order_count = self.env["sale.order"].search_count(
                [("shopify_market_id", "=", rec.id)]
            )
            if order_count:
                raise ValidationError(
                    _(
                        "You cannot change the company on market '%(market)s' because "
                        "%(count)d order(s) are already linked to it.\n\n"
                        "Orders were created under the previous company and cannot be "
                        "re-assigned. If you need a different company for future orders, "
                        "please archive this market and create a new one.",
                        market=rec.name,
                        count=order_count,
                    )
                )

    # ─── Onchange ──────────────────────────────────────────────────────────────
    @api.onchange("company_id")
    def _onchange_company_id(self):
        """Reset company-dependent fields when company changes."""
        self.warehouse_id = False
        self.pricelist_id = False
        self.fiscal_position_id = False
        # self.auto_workflow_id = False
        self.financial_status_configuration_ids = False

    # ─── Business Logic ────────────────────────────────────────────────────────
    @api.model
    def sync_markets_from_shopify_ept(self, instance):
        """
        Fetch all markets from Shopify via GraphQL and create/update
        shopify.market.ept records.

        :param instance: shopify.instance.ept record
        :return: list of created/updated market records
        """
        client = instance.get_graphql_client()
        markets_data = self._fetch_all_markets_ept(client)
        if not markets_data:
            _logger.info("No markets found in Shopify for instance %s", instance.name)
            return []

        synced_markets = []
        for market_data in markets_data:
            market = self._create_or_update_market_ept(market_data, instance)
            if market:
                synced_markets.append(market)

        _logger.info(
            "Synced %d markets for instance %s", len(synced_markets), instance.name
        )
        return synced_markets

    def _fetch_all_markets_ept(self, client):
        """Paginate through all Shopify markets using GraphQL cursor."""
        all_markets = []
        has_next = True
        cursor = None
        page = 0
        max_pages = 50  # safety guard

        while has_next and page < max_pages:
            variables = {"first": 50}
            if cursor:
                variables["after"] = cursor

            try:
                response = client.execute(MARKET_QUERY, variables=variables)
            except Exception as error:
                _logger.error("Error fetching markets from Shopify: %s", error)
                raise UserError(_("Error fetching markets from Shopify: %s") % str(error))

            errors = response.get("errors")
            if errors:
                _logger.error("GraphQL errors fetching markets: %s", errors)
                raise UserError(_("Shopify GraphQL error: %s") % str(errors))

            markets_resp = response.get("data", {}).get("markets", {})
            nodes = markets_resp.get("nodes", [])
            all_markets.extend(nodes)

            page_info = markets_resp.get("pageInfo", {})
            has_next = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")
            page += 1

        return all_markets

    def _create_or_update_market_ept(self, market_data, instance):
        """Create or update a single shopify.market.ept record."""
        shopify_gid = market_data.get("id", "")
        name = market_data.get("name", "")
        handle = market_data.get("handle", "")
        primary = market_data.get("primary", False)
        enabled = market_data.get("enabled", True)

        # Countries — regions nodes can be None
        regions_data = market_data.get("regions") or {}
        region_nodes = regions_data.get("nodes") or []
        region_codes = [
            r.get("code", "")
            for r in region_nodes
            if r and r.get("code")
        ]
        countries = self.env["res.country"].search([("code", "in", region_codes)]) if region_codes else self.env["res.country"]

        # Web domain — webPresence and domain can both be None
        web_presence = market_data.get("webPresence") or {}
        domain_obj = web_presence.get("domain") or {}
        web_domain = domain_obj.get("host", "") or web_presence.get("subfolderSuffix", "") or ""

        vals = {
            "name": name,
            "shopify_market_id": shopify_gid,
            "shopify_instance_id": instance.id,
            "handle": handle,
            "primary": primary,
            "enabled": enabled,
            "country_ids": [(6, 0, countries.ids)],
            "web_presence_domain": web_domain,
            "active": True,
        }

        # Search existing market
        existing = self.search(
            [
                ("shopify_market_id", "=", shopify_gid),
                ("shopify_instance_id", "=", instance.id),
            ],
            limit=1,
        )

        if existing:
            # Update only the fields that come from Shopify (identity/sync fields).
            # Never overwrite fields the admin has manually configured
            # (company_id, warehouse_id, pricelist_id, fiscal_position_id, etc.).
            existing.write(vals)
            _logger.info("Updated market: %s (instance: %s)", name, instance.name)
            return existing
        else:
            # New market: create with Shopify data only.
            # Do NOT auto-assign company, warehouse or pricelist here.
            # The admin must configure these manually in the market form.
            market = self.create(vals)
            _logger.info("Created market: %s (instance: %s)", name, instance.name)
            return market

    def unlink(self):
        """ Prevent deletion of markets that have linked sale orders (except in draft/cancelled state).
         This protects data integrity — orders must always be linked to a valid market. 
         If the market needs to be retired, it can be archived instead of deleted.
        """
        sale_order_obj = self.env["sale.order"]
        blocked_order = sale_order_obj.search(
            [
                ("shopify_market_id", "in", self.ids),
                ("state", "not in", ["draft", "cancel"]),
            ],
            limit=1,
        )
        if blocked_order:
            raise ValidationError(
                _(
                    "You cannot delete this market because it has linked sale orders "
                    "that are not in Draft or Cancelled state."
                )
            )
        return super().unlink()

    def find_market_for_order_ept(self, instance, country_code=False, currency_code=False):
        """
        Find the best-matching market for an order based on priority:
        1. Country code match
        2. Currency match (via market's own pricelist currency)
        3. Primary market
        4. None (caller falls back to instance defaults)

        :param instance: shopify.instance.ept record
        :param country_code: ISO 2-letter country code string
        :param currency_code: Currency code string (e.g. 'USD')
        :return: shopify.market.ept record or empty recordset
        """
        base_domain = [
            ("shopify_instance_id", "=", instance.id),
            ("active", "=", True),
            ("enabled", "=", True),
            ("company_id", "!=", False),
        ]

        # Priority 1: Match by country code
        if country_code:
            country = self.env["res.country"].search(
                [("code", "=", country_code.upper())], limit=1
            )
            if country:
                market = self.search(
                    base_domain + [("country_ids", "in", [country.id])], limit=1
                )
                if market:
                    _logger.info(
                        "Market found by country '%s': %s", country_code, market.name
                    )
                    return market

        # Priority 2: Match by the market's own pricelist currency.
        # Only markets that have a pricelist configured with the matching currency are considered.
        # No new records are searched or created — only existing market configuration is used.
        if currency_code:
            currency = self.env["res.currency"].search(
                [("name", "=", currency_code.upper())], limit=1
            )
            if currency:
                market = self.search(
                    base_domain + [("pricelist_id.currency_id", "=", currency.id)], limit=1
                )
                if market:
                    _logger.info(
                        "Market found by pricelist currency '%s': %s",
                        currency_code,
                        market.name,
                    )
                    return market

        # # Priority 3: Primary market
        # market = self.search(base_domain + [("primary", "=", True)], limit=1)
        # if market:
        #     _logger.info(
        #         "No specific market found — using primary market: %s", market.name
        #     )
        #     return market

        _logger.warning(
            "No market found for instance '%s', country '%s', currency '%s'. "
            "Falling back to instance defaults.",
            instance.name,
            country_code,
            currency_code,
        )
        return self.env["shopify.market.ept"]

    def action_view_orders_ept(self):
        """Smart button: view sale orders linked to this market."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Orders"),
            "res_model": "sale.order",
            "view_mode": "list,form",
            "domain": [("shopify_market_id", "=", self.id)],
            "context": {"create": False},
        }

    def _compute_order_count_ept(self):
        for rec in self:
            rec.order_count = self.env["sale.order"].search_count(
                [("shopify_market_id", "=", rec.id)]
            )
