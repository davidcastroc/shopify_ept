# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import uuid


class ShopifyAuthProcessEpt(models.TransientModel):
    _name = 'shopify.auth.process.ept'
    _description = 'Shopify OAuth Link Generator Wizard'

    # ── State control ──────────────────────────────────────────────
    state = fields.Selection([
        ('draft', 'Setup'),
        ('token_generated', 'Token Generated'),
    ], default='draft', readonly=True)

    # ── Step 1: Input fields ───────────────────────────────────────
    shopify_host = fields.Char(
        string="Shopify Store Domain",
        help="e.g. mystore.myshopify.com — no 'https://' or trailing slash."
    )
    shopify_client_id = fields.Char(
        string="API Key (Client ID)",
        help="API Key from your Shopify custom app."
    )
    shopify_secret_id = fields.Char(
        string="API Secret Key",
        help="API Secret Key from your Shopify custom app."
    )

    # ── Step 2: Result fields (readonly, shown after token generation) ──
    access_token = fields.Char(string="Access Token", readonly=True)
    token_status = fields.Char(string="Status", readonly=True)

    # ── Computed helper URLs ───────────────────────────────────────
    redirect_url = fields.Char(
        string='Callback / Redirect URL',
        readonly=True,
        compute='_compute_urls'
    )
    app_url = fields.Char(
        string='App URL',
        readonly=True,
        compute='_compute_urls'
    )
    show_help = fields.Boolean(default=False)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        config = self.env['ir.config_parameter'].sudo()
        user_id = self.env.user.id
        nonce = self.env.context.get('params', {}).get('shopify_unique_key')
        if not nonce:
            nonce = config.get_param(f'shopify_pending_nonce_uid_{user_id}')
        else:
            config.set_param(f'shopify_pending_nonce_uid_{user_id}', False)
        if not nonce:
            self._consume_nonce()
            return res

        token_key = f'shopify_pending_{nonce}_token'
        shop_key = f'shopify_pending_{nonce}_shop'
        client_id_key = f'shopify_pending_{nonce}_client_id'
        secret_key = f'shopify_pending_{nonce}_secret'

        access_token = config.get_param(token_key)
        shop = config.get_param(shop_key)
        client_id = config.get_param(client_id_key)
        secret = config.get_param(secret_key)
        if not access_token or not shop:
            return res
        # config.set_param(f'shopify_pending_nonce_uid_{user_id}', False)
        # config.set_param(f'shopify_active_nonce_uid_{user_id}', nonce)
        res.update({
            'shopify_host': shop,
            'access_token': access_token,
            'token_status': '✅ Token generated successfully',
            'state': 'token_generated',
            'shopify_client_id': client_id,
            'shopify_secret_id': secret,
        })
        return res

    @api.depends('shopify_host')
    def _compute_urls(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for rec in self:
            rec.redirect_url = f"{base_url}/ept_shopify/oauth/callback"
            rec.app_url = f"{base_url}/ept_shopify/launch"

    def _consume_nonce(self):
        """Clean up all pending nonce params for the current user."""
        config = self.env['ir.config_parameter'].sudo()
        params = config.search(['|', ('key', 'ilike', 'shopify_pending_'),
                                ('key', 'ilike', 'shopify_client_')])
        if params:
            params.unlink()

    # ── Actions ───────────────────────────────────────────────────

    def generate_auth_link(self):
        """Validate inputs, persist credentials, redirect to Shopify OAuth."""
        self.ensure_one()

        if not self.shopify_host or not self.shopify_client_id or not self.shopify_secret_id:
            raise UserError(_("Shopify Host, Client ID, and Secret Key are all required."))
        if 'myshopify' not in self.shopify_host:
            raise UserError(_(
                "Host must contain 'myshopify', e.g. 'demo.myshopify.com'.\n"
                "Find it in: Shopify → Settings → Domains"
            ))

        # domain = self.shopify_host.strip().lower().replace('https://', '').replace('/', '')
        unique_key = str(uuid.uuid4())
        param_prefix = f"shopify_client_{unique_key}"
        config = self.env['ir.config_parameter'].sudo()
        # base_url = config.get_param('web.base.url')

        # Persist credentials and wizard ID so the callback can restore this record
        config.set_param(f'{param_prefix}_client_id', self.shopify_client_id)
        config.set_param(f'{param_prefix}_secret_id', self.shopify_secret_id)
        # Store wizard record ID so callback can update it directly
        # config.set_param(f'{param_prefix}_wizard_id', str(self.id))

        scopes =  (
            'read_assigned_fulfillment_orders', 'write_assigned_fulfillment_orders',
            'read_customers', 'write_customers', 'read_discounts', 'write_discounts',
            'write_draft_orders', 'read_draft_orders', 'read_files', 'write_files',
            'read_fulfillments', 'write_fulfillments', 'write_inventory', 'read_inventory',
            'write_locations', 'read_locations',
            'read_merchant_managed_fulfillment_orders', 'write_merchant_managed_fulfillment_orders',
            'read_metaobject_definitions', 'write_metaobject_definitions',
            'read_metaobjects', 'write_metaobjects', 'read_orders', 'write_orders',
            'read_products', 'write_products', 'read_shipping', 'write_shipping',
            'read_third_party_fulfillment_orders', 'write_third_party_fulfillment_orders',
            'read_shopify_payments_payouts', 'read_returns','write_returns'
        )

        authorize_url = (
            f"https://{self.shopify_host}/admin/oauth/authorize"
            f"?client_id={self.shopify_client_id}"
            f"&scope={scopes}"
            f"&redirect_uri={self.redirect_url}"
            f"&state={unique_key}"
        )
        return {
            'type': 'ir.actions.act_url',
            'url': authorize_url,
            'target': 'self',
        }

    def action_create_instance(self):
        self.ensure_one()
        if not self.access_token or not self.shopify_host:
            raise UserError(_("Token or shop data is missing. Please re-authenticate."))

        # Consume nonce — delete params entirely
        self._consume_nonce()

        host = self.shopify_host
        if 'https' not in host:
            host = f"https://{host}"

        instance_wizard = self.env['res.config.shopify.instance'].create({
            'shopify_host': host,
            'shopify_password': self.access_token,
            'shopify_api_key': self.shopify_client_id,
            'shopify_shared_secret': self.shopify_secret_id,
        })

        return {
            'type': 'ir.actions.act_window',
            'name': _('Create Shopify Instance'),
            'res_model': 'res.config.shopify.instance',
            'view_mode': 'form',
            'res_id': instance_wizard.id,
            'target': 'new',
            'context': {'from_oauth_flow': True},
        }

    def toggle_show_help(self):
        self.ensure_one()
        self.show_help = not self.show_help
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }

    def action_reset_auth(self):
        """Clear stale OAuth cache and reset wizard to draft state."""
        self.ensure_one()
        self._consume_nonce()
        self.write({
            'state': 'draft',
            'access_token': False,
            'token_status': False,
            'shopify_host': False,
            'shopify_client_id': False,
            'shopify_secret_id': False,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }

    def action_close(self):
        """Called by Close button on token_generated state."""
        self.ensure_one()
        self._consume_nonce()
        action = self.env["ir.actions.act_window"]._for_xml_id("shopify_ept.shopify_kanban_action_ept")
        if action:
            return action
        return {'type': 'ir.actions.act_window_close'}