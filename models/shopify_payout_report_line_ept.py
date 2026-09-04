# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
from odoo import models, fields
SHOPIFY_PAYOUT_TRANSACTION_TYPES = [
            # Standard types
            ('charge', 'Charge'),
            ('refund', 'Refund'),
            ('dispute', 'Dispute'),
            ('reserve', 'Reserve'),
            ('adjustment', 'Adjustment'),
            ('credit', 'Credit'),
            ('debit', 'Debit'),
            ('payout', 'Payout'),
            ('payout_failure', 'Payout Failure'),
            ('payout_cancellation', 'Payout Cancellation'),
            ('fees', 'Fees'),
            ('payment_refund', 'Payment Refund'),
            # ACH
            ('ach_bank_failure_debit_fee', 'ACH Bank Failure Debit Fee'),
            ('ach_bank_failure_debit_reversal_fee', 'ACH Bank Failure Debit Reversal Fee'),
            # Ads
            ('ads_publisher_credit', 'Ads Publisher Credit'),
            ('ads_publisher_credit_reversal', 'Ads Publisher Credit Reversal'),
            # Advance / Lending
            ('advance', 'Advance'),
            ('advance_funding', 'Advance Funding'),
            ('lending_capital_refund', 'Lending Capital Refund'),
            ('lending_capital_refund_reversal', 'Lending Capital Refund Reversal'),
            ('lending_capital_remittance', 'Lending Capital Remittance'),
            ('lending_capital_remittance_reversal', 'Lending Capital Remittance Reversal'),
            ('lending_credit', 'Lending Credit'),
            ('lending_credit_refund', 'Lending Credit Refund'),
            ('lending_credit_refund_reversal', 'Lending Credit Refund Reversal'),
            ('lending_credit_remittance', 'Lending Credit Remittance'),
            ('lending_credit_remittance_reversal', 'Lending Credit Remittance Reversal'),
            ('lending_credit_reversal', 'Lending Credit Reversal'),
            ('lending_debit', 'Lending Debit'),
            ('lending_debit_reversal', 'Lending Debit Reversal'),
            # Agentic Fee
            ('agentic_fee_tax_credit', 'Agentic Fee Tax Credit'),
            ('agentic_fee_tax_credit_reversal', 'Agentic Fee Tax Credit Reversal'),
            ('agentic_fee_tax_debit', 'Agentic Fee Tax Debit'),
            ('agentic_fee_tax_debit_reversal', 'Agentic Fee Tax Debit Reversal'),
            # Anomaly
            ('anomaly_credit', 'Anomaly Credit'),
            ('anomaly_credit_reversal', 'Anomaly Credit Reversal'),
            ('anomaly_debit', 'Anomaly Debit'),
            ('anomaly_debit_reversal', 'Anomaly Debit Reversal'),
            # Application
            ('application_fee_refund', 'Application Fee Refund'),
            # Balance Transfer
            ('balance_transfer_inbound', 'Balance Transfer Inbound'),
            ('balance_transfer_outbound', 'Balance Transfer Outbound'),
            # Billing
            ('billing_debit', 'Billing Debit'),
            ('billing_debit_reversal', 'Billing Debit Reversal'),
            # Channel
            ('channel_credit', 'Channel Credit'),
            ('channel_credit_reversal', 'Channel Credit Reversal'),
            ('channel_promotion_credit', 'Channel Promotion Credit'),
            ('channel_promotion_credit_reversal', 'Channel Promotion Credit Reversal'),
            ('channel_transfer_credit', 'Channel Transfer Credit'),
            ('channel_transfer_credit_reversal', 'Channel Transfer Credit Reversal'),
            ('channel_transfer_debit', 'Channel Transfer Debit'),
            ('channel_transfer_debit_reversal', 'Channel Transfer Debit Reversal'),
            # Charge
            ('charge_adjustment', 'Charge Adjustment'),
            # Chargeback
            ('chargeback_fee', 'Chargeback Fee'),
            ('chargeback_fee_refund', 'Chargeback Fee Refund'),
            ('chargeback_hold', 'Chargeback Hold'),
            ('chargeback_hold_release', 'Chargeback Hold Release'),
            ('chargeback_protection_credit', 'Chargeback Protection Credit'),
            ('chargeback_protection_credit_reversal', 'Chargeback Protection Credit Reversal'),
            ('chargeback_protection_debit', 'Chargeback Protection Debit'),
            ('chargeback_protection_debit_reversal', 'Chargeback Protection Debit Reversal'),
            # Collections
            ('collections_credit', 'Collections Credit'),
            ('collections_credit_reversal', 'Collections Credit Reversal'),
            # Customs & Import
            ('customs_duty', 'Customs Duty'),
            ('customs_duty_adjustment', 'Customs Duty Adjustment'),
            ('import_tax', 'Import Tax'),
            ('import_tax_adjustment', 'Import Tax Adjustment'),
            ('import_tax_refund', 'Import Tax Refund'),
            # Dispute
            ('dispute_reversal', 'Dispute Reversal'),
            ('dispute_withdrawal', 'Dispute Withdrawal'),
            # Marketplace
            ('marketplace_fee_credit', 'Marketplace Fee Credit'),
            ('marketplace_fee_credit_reversal', 'Marketplace Fee Credit Reversal'),
            ('markets_pro_credit', 'Markets Pro Credit'),
            # Merchant
            ('merchant_goodwill_credit', 'Merchant Goodwill Credit'),
            ('merchant_goodwill_credit_reversal', 'Merchant Goodwill Credit Reversal'),
            ('merchant_to_merchant_credit', 'Merchant To Merchant Credit'),
            ('merchant_to_merchant_credit_reversal', 'Merchant To Merchant Credit Reversal'),
            ('merchant_to_merchant_debit', 'Merchant To Merchant Debit'),
            ('merchant_to_merchant_debit_reversal', 'Merchant To Merchant Debit Reversal'),
            # Promotion
            ('promotion_credit', 'Promotion Credit'),
            ('promotion_credit_reversal', 'Promotion Credit Reversal'),
            # Referral
            ('referral_fee', 'Referral Fee'),
            ('referral_fee_tax', 'Referral Fee Tax'),
            # Refund
            ('refund_adjustment', 'Refund Adjustment'),
            ('refund_failure', 'Refund Failure'),
            # Reserved Funds
            ('reserved_funds', 'Reserved Funds'),
            ('reserved_funds_reversal', 'Reserved Funds Reversal'),
            ('reserved_funds_withdrawal', 'Reserved Funds Withdrawal'),
            # Risk
            ('risk_reversal', 'Risk Reversal'),
            ('risk_withdrawal', 'Risk Withdrawal'),
            # Seller Protection
            ('seller_protection_credit', 'Seller Protection Credit'),
            ('seller_protection_credit_reversal', 'Seller Protection Credit Reversal'),
            # Shipping Label
            ('shipping_label', 'Shipping Label'),
            ('shipping_label_adjustment', 'Shipping Label Adjustment'),
            ('shipping_label_adjustment_base', 'Shipping Label Adjustment Base'),
            ('shipping_label_adjustment_surcharge', 'Shipping Label Adjustment Surcharge'),
            ('shipping_other_carrier_charge_adjustment', 'Shipping Other Carrier Charge Adjustment'),
            ('shipping_return_to_origin_adjustment', 'Shipping Return To Origin Adjustment'),
            # Shop Cash
            ('shop_cash_billing_debit', 'Shop Cash Billing Debit'),
            ('shop_cash_billing_debit_reversal', 'Shop Cash Billing Debit Reversal'),
            ('shop_cash_campaign_billing_credit', 'Shop Cash Campaign Billing Credit'),
            ('shop_cash_campaign_billing_credit_reversal', 'Shop Cash Campaign Billing Credit Reversal'),
            ('shop_cash_campaign_billing_debit', 'Shop Cash Campaign Billing Debit'),
            ('shop_cash_campaign_billing_debit_reversal', 'Shop Cash Campaign Billing Debit Reversal'),
            ('shop_cash_credit', 'Shop Cash Credit'),
            ('shop_cash_credit_reversal', 'Shop Cash Credit Reversal'),
            ('shop_cash_refund_debit', 'Shop Cash Refund Debit'),
            ('shop_cash_refund_debit_reversal', 'Shop Cash Refund Debit Reversal'),
            # Shopify Collective
            ('shopify_collective_credit', 'Shopify Collective Credit'),
            ('shopify_collective_credit_reversal', 'Shopify Collective Credit Reversal'),
            ('shopify_collective_debit', 'Shopify Collective Debit'),
            ('shopify_collective_debit_reversal', 'Shopify Collective Debit Reversal'),
            # Shopify Source
            ('shopify_source_credit', 'Shopify Source Credit'),
            ('shopify_source_credit_reversal', 'Shopify Source Credit Reversal'),
            ('shopify_source_debit', 'Shopify Source Debit'),
            ('shopify_source_debit_reversal', 'Shopify Source Debit Reversal'),
            # Stripe
            ('stripe_fee', 'Stripe Fee'),
            # Tax Adjustment
            ('tax_adjustment_credit', 'Tax Adjustment Credit'),
            ('tax_adjustment_credit_reversal', 'Tax Adjustment Credit Reversal'),
            ('tax_adjustment_debit', 'Tax Adjustment Debit'),
            ('tax_adjustment_debit_reversal', 'Tax Adjustment Debit Reversal'),
            # Transfer
            ('transfer_cancel', 'Transfer Cancel'),
            ('transfer_failure', 'Transfer Failure'),
            ('transfer_refund', 'Transfer Refund'),
            # VAT
            ('vat_refund_credit', 'VAT Refund Credit'),
            ('vat_refund_credit_reversal', 'VAT Refund Credit Reversal'),
        ]


class ShopifyPayoutReportLineEpt(models.Model):
    _name = "shopify.payout.report.line.ept"
    _description = "Shopify Payout Report Line"
    _rec_name = "transaction_id"

    payout_id = fields.Many2one('shopify.payout.report.ept', string="Payout ID", ondelete="cascade")
    transaction_id = fields.Char(string="Transaction ID", help="The unique identifier of the transaction.")
    source_order_id = fields.Char(string="Order Reference ID", help="The id of the Order that this transaction  "
                                                                    "ultimately originated from")
    transaction_type = fields.Selection(SHOPIFY_PAYOUT_TRANSACTION_TYPES, help="The type of the balance transaction",
                                        string="Balance Transaction Type")
    currency_id = fields.Many2one('res.currency', string='Currency', help="currency code of the payout.")
    source_type = fields.Selection(
        [('charge', 'Charge'), ('refund', 'Refund'), ('dispute', 'Dispute'),
         ('reserve', 'Reserve'), ('adjustment', 'Adjustment'), ('payout', 'Payout'), ],
        help="The type of the balance transaction", string="Resource Leading Transaction")
    amount = fields.Float(string="Amount", help="The gross amount of the transaction.")
    fee = fields.Float(string="Fees", help="The total amount of fees deducted from the transaction amount.")
    net_amount = fields.Float(string="Net Amount", help="The net amount of the transaction.")
    order_id = fields.Many2one('sale.order', string="Order Reference")
    is_processed = fields.Boolean("Processed?")
    is_remaining_statement = fields.Boolean(string="Is Remaining Statement?")
