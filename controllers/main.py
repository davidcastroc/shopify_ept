# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

import base64
import hashlib
import hmac
import logging
import requests
import uuid
from odoo import http
from odoo.http import request
from .. import shopify
from werkzeug.utils import redirect

_logger = logging.getLogger("Shopify Controller")


class Main(http.Controller):

    @http.route(['/shopify_odoo_webhook_for_product_create', '/shopify_odoo_webhook_for_product_update',
                 '/shopify_odoo_webhook_for_product_delete'], csrf=False,
                auth="public", type="jsonrpc")
    def create_update_delete_product_webhook(self):
        """
        Route for handling the product create/update/delete webhook of Shopify. This route calls while any new product
        create or update or delete in the Shopify store.
        @author: Dipak Gogiya on Date 10-Jan-2020.
        """
        webhook_route = request.httprequest.path.split('/')[1]  # Here we receive two type of route
        # 1) Update and create product (shopify_odoo_webhook_for_product_update)
        # 2) Delete product (shopify_odoo_webhook_for_product_delete)

        res, instance = self.get_basic_info(webhook_route)

        if not res:
            return

        _logger.info("%s call for product: %s", webhook_route, res.get("title"))

        shopify_template = request.env["shopify.product.template.ept"].sudo().with_context(active_test=False).search(
            [("shopify_tmpl_id", "=", res.get("id")), ("shopify_instance_id", "=", instance.id)], limit=1)

        if webhook_route in ['shopify_odoo_webhook_for_product_update',
                             'shopify_odoo_webhook_for_product_create'] and shopify_template or res.get("published_at"):
            # when new product is created via webhook, response does not have require_shipping field
            # to get all data about the product, external api is being called.
            instance.connect_in_shopify()
            shopify_product = shopify.Product().find(ids=str(res.get('id')))[0]
            request.env["shopify.product.data.queue.ept"].sudo().create_shopify_product_queue_from_webhook(
                shopify_product,
                instance)

        if webhook_route == 'shopify_odoo_webhook_for_product_delete' and shopify_template:
            shopify_template.write({"active": False})
        return

    @http.route(['/shopify_odoo_webhook_for_customer_create', '/shopify_odoo_webhook_for_customer_update'], csrf=False,
                auth="public", type="jsonrpc")
    def customer_create_or_update_webhook(self):
        """
        Route for handling customer create/update webhook for Shopify. This route calls while new customer create
        or update customer values in the Shopify store.
        @author: Dipak Gogiya on Date 10-Jan-2020.
        """
        webhook_route = request.httprequest.path.split('/')[1]  # Here we receive two type of route
        # 1) Create Customer (shopify_odoo_webhook_for_customer_create)
        # 2) Update Customer(shopify_odoo_webhook_for_customer_update)

        res, instance = self.get_basic_info(webhook_route)
        if not res:
            return
        if res.get("first_name") or res.get("last_name"):
            _logger.info(f"{webhook_route} call for Customer: {res.get('first_name')} {res.get('last_name')}")
            self.customer_webhook_process(res, instance)
        return

    def customer_webhook_process(self, response, instance):
        """
        This method used for call child method of customer create process.
        @author: Maulik Barad on Date 23-Sep-2020.
        """
        process_import_export_model = request.env["shopify.process.import.export"].sudo()
        process_import_export_model.webhook_customer_create_process(response, instance)
        return True

    @http.route("/shopify_odoo_webhook_for_orders_partially_updated", csrf=False, auth="public", type="jsonrpc")
    def order_create_or_update_webhook(self):
        """
        Route for handling the order update webhook of Shopify. This route calls while new order create
        or update in the Shopify store.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 13-Jan-2020.
        """
        res, instance = self.get_basic_info("shopify_odoo_webhook_for_orders_partially_updated")
        sale_order = request.env["sale.order"]
        if not res:
            return

        _logger.info("UPDATE ORDER WEBHOOK call for order: %s", res.get("name"))

        fulfillment_status = res.get("fulfillment_status") or "unfulfilled"
        if sale_order.sudo().search_read([("shopify_instance_id", "=", instance.id),
                                          ("shopify_order_id", "=", res.get("id")),
                                          ("shopify_order_number", "=",
                                           res.get("order_number"))],
                                         ["id"]):
            sale_order.sudo().process_shopify_order_via_webhook(res, instance, True)
        elif fulfillment_status in ["fulfilled", "unfulfilled", "partial"]:
            res["fulfillment_status"] = fulfillment_status
            sale_order.sudo().with_context({'is_new_order': True}).process_shopify_order_via_webhook(res,
                                                                                                     instance)
        return

    def verify_hmac_signature(self, instance):
        """
        Verifies the HMAC-SHA256 signature attached to every Shopify webhook request.

        Shopify computes:  Base64( HMAC-SHA256( shared_secret, raw_request_body ) )
        and sends the result in the  X-Shopify-Hmac-Sha256  request header.

        Steps:
        1. Read the raw (unmodified) request body from the Werkzeug request object.
           get_data() caches the bytes internally so the body can still be read later.
        2. Re-compute the HMAC-SHA256 digest using the instance's shared secret.
        3. Compare with the header value using hmac.compare_digest() to prevent
           timing-based side-channel attacks.

        Returns True when the signature is valid, False otherwise.
        """
        shopify_hmac_header = request.httprequest.headers.get("X-Shopify-Hmac-Sha256")
        if not shopify_hmac_header:
            _logger.warning(
                "Shopify webhook HMAC check FAILED for instance '%s': "
                "X-Shopify-Hmac-Sha256 header is missing.",
                instance.name,
            )
            return False

        shared_secret = instance.shopify_shared_secret
        if not shared_secret:
            _logger.warning(
                "Shopify webhook HMAC check SKIPPED for instance '%s': "
                "No shared secret is configured on the instance.",
                instance.name,
            )
            # Treat a missing secret as a configuration error → reject the request.
            return False

        # Read the raw bytes BEFORE Odoo/Werkzeug parses the JSON body.
        # Werkzeug caches the result so subsequent reads still work correctly.
        raw_body = request.httprequest.get_data()

        # Compute HMAC-SHA256 and Base64-encode to match Shopify's format.
        digest = hmac.new(
            shared_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).digest()
        computed_hmac = base64.b64encode(digest).decode("utf-8")

        # Use constant-time comparison to avoid timing attacks.
        if not hmac.compare_digest(computed_hmac, shopify_hmac_header):
            _logger.warning(
                "Shopify webhook HMAC check FAILED for instance '%s': "
                "Signature mismatch. Request may be forged or tampered with.",
                instance.name,
            )
            return False

        _logger.debug(
            "Shopify webhook HMAC check PASSED for instance '%s'.", instance.name
        )
        return True

    def get_basic_info(self, route):
        """
        This method is used to check that instance and webhook are active or not. If yes then return response and
        instance, If no then return response as False and instance.
        Also verifies the HMAC-SHA256 signature sent by Shopify to ensure the request
        originated from Shopify and has not been tampered with.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 10-Jan-2020.
        """
        res = request.get_json_data()
        host = request.httprequest.headers.get("X-Shopify-Shop-Domain")
        normalized_host = host if host.startswith(('http://', 'https://')) else 'https://%s' % host
        instance = request.env["shopify.instance.ept"].sudo().with_context(active_test=False).search(
            [("shopify_host", "=", normalized_host)], limit=1)
        if not instance:
            instance = request.env["shopify.instance.ept"].sudo().with_context(active_test=False).search(
                [("shopify_host", "ilike", host)], limit=1)

        webhook = request.env["shopify.webhook.ept"].sudo().search([("delivery_url", "ilike", route),
                                                                    ("instance_id", "=", instance.id)], limit=1)

        if not instance.active or not webhook.state == "active":
            _logger.info("The method is skipped. It appears the instance:%s is not active or that "
                         "the webhook %s is not active and receive host from webhook %s.", instance.name, webhook.webhook_name, host)
            res = False
            return res, instance

        # ── Security: reject requests that fail HMAC-SHA256 signature verification ──
        if not self.verify_hmac_signature(instance):
            _logger.warning(
                "Webhook request rejected due to invalid HMAC signature "
                "(route: %s, shop: %s).", route, host,
            )
            res = False

        return res, instance

    @http.route('/ept_shopify/launch', type='http', auth='public', csrf=False)
    def shopify_launch_ept(self, **kwargs):
        shop = kwargs.get("shop")

        # If no shop param in URL, extract from the host in embedded app
        if not shop:
            encoded_host = kwargs.get("host")
            if encoded_host:
                decoded = base64.b64decode(encoded_host).decode()
                # decoded looks like "admin.shopify.com/store-name"
                store_name = decoded.split("/")[-1]
                shop = f"{store_name}.myshopify.com"

        if not shop:
            return request.render('shopify_ept.oauth_error_template', {
                'error': 'Missing shop parameter',
                'error_description': 'Shop parameter missing in callback.',
            })

        # Fetch client_id from config parameter
        domain = shop.strip().lower().replace('https://', '').replace('/', '')

        # Check if Shopify instance already exists for this host
        instance = request.env['shopify.instance.ept'].sudo().search([('shopify_host', 'ilike', domain)], limit=1)
        if instance:
            return request.render('shopify_ept.oauth_error_template', {
                'error': 'Instance Exists',
                'error_description': 'A Shopify instance for this store already exists in Odoo. Token generation is not required.',
            })

        param_prefix = f"shopify_{domain}"
        config = request.env['ir.config_parameter'].sudo()
        client_id = config.get_param(f'{param_prefix}_client_id')
        if not client_id:
            return request.render('shopify_ept.oauth_error_template', {
                'error': 'Missing App Credentials',
                'error_description': 'No client_id found for this shop domain.',
            })

        # Use Odoo's base url for redirect_uri
        base_url = config.get_param('web.base.url')
        redirect_uri = f"{base_url}/ept_shopify/oauth/callback"

        # You may want to make scopes configurable
        scope = (
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
            'read_shopify_payments_payouts', 'read_returns', 'write_returns'
        )

        authorize_url = (
            f"https://{shop}/admin/oauth/authorize?"
            f"client_id={client_id}&scope={scope}&redirect_uri={redirect_uri}"
        )

        return redirect(authorize_url)

    @http.route('/ept_shopify/oauth/callback', type='http', auth='public', csrf=False)
    def shopify_oauth_callback(self, **kwargs):
        """
            Shopify OAuth callback endpoint. Handles the redirect from Shopify after user authorization.
            Exchanges the code for an access token and displays the result.
        """
        shop = kwargs.get('shop')
        code = kwargs.get('code')
        error = kwargs.get('error')
        state = kwargs.get('state')

        if error:
            return request.render('shopify_ept.oauth_error_template', {
                'error': error,
                'error_description': kwargs.get('error_description'),
            })

        if not shop or not code or not state:
            return request.render('shopify_ept.oauth_error_template', {
                'error': 'Missing data',
                'error_description': 'Shop or code or Unique state parameter missing in callback.',
            })
        param_prefix = f"shopify_client_{state}"
        config = request.env['ir.config_parameter'].sudo()
        client_id = config.get_param(f'{param_prefix}_client_id')
        client_secret = config.get_param(f'{param_prefix}_secret_id')

        if not client_id or not client_secret:
            return request.render('shopify_ept.oauth_error_template', {
                'error': 'Missing App Credentials',
                'error_description': 'Credentials not found. Please re-run the setup wizard.',
            })
        try:
            response = requests.post(
                f'https://{shop}/admin/oauth/access_token',
                json={'client_id': client_id, 'client_secret': client_secret, 'code': code},
                timeout=10
            )
            response.raise_for_status()
            access_token = response.json().get('access_token')

            # ── Clean up credentials immediately ──
            params = config.search([('key', 'ilike', f'{param_prefix}_')])
            if params:
                params.sudo().unlink()

            # ── Store token under a secure one-time nonce ──
            nonce = str(uuid.uuid4())
            config.set_param(f'shopify_pending_{nonce}_token', access_token)
            config.set_param(f'shopify_pending_{nonce}_shop', shop)
            config.set_param(f'shopify_pending_{nonce}_client_id', client_id)
            config.set_param(f'shopify_pending_{nonce}_secret', client_secret)

            user_id = request.env.user.id
            config.set_param(f'shopify_pending_nonce_uid_{user_id}', nonce)

            # ── Redirect — nonce is in URL, token never exposed ──
            base_url = config.get_param('web.base.url')
            wizard_action = request.env["ir.actions.actions"].sudo()._for_xml_id(
                "shopify_ept.action_shopify_auth_process_ept")
            wizard_id = wizard_action.get('id', "shopify_ept.action_shopify_auth_process_ept")
            wizard_url = (
                f"{base_url}/odoo/action-{wizard_id}"
                f"?shopify_unique_key={nonce}"
            )
            return redirect(wizard_url)

        except Exception as e:
            _logger.error('Shopify OAuth token exchange failed: %s', e)
            return request.render('shopify_ept.oauth_error_template', {
                'error': 'Token Exchange Failed',
                'error_description': str(e),
            })
