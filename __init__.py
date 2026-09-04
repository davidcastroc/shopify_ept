# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
from . import models
from . import wizard
from . import report
from . import controllers


def _shopify_ept_post_init(env):
    env['ir.config_parameter'].sudo().set_param(
        'shopify_ept.graphql_enabled_on_install', 'True'
    )

def _shopify_ept_uninstall_hook(env):
    env['ir.config_parameter'].sudo().search([
        ('key', '=', 'shopify_ept.graphql_enabled_on_install')
    ]).unlink()