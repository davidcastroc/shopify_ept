GQL_TO_REST_TRANSACTION_MAPPING = {
    "charge": [
        "CHARGE", "ADJUSTMENT", "BALANCE_TRANSFER_INBOUND", "ANOMALY_CREDIT", "CHANNEL_CREDIT",
        "CHANNEL_PROMOTION_CREDIT", "CHANNEL_TRANSFER_CREDIT", "COLLECTIONS_CREDIT", "MARKETS_PRO_CREDIT",
        "MERCHANT_GOODWILL_CREDIT", "PROMOTION_CREDIT", "SELLER_PROTECTION_CREDIT", "SHOPIFY_COLLECTIVE_CREDIT",
        "SHOPIFY_SOURCE_CREDIT", "ADS_PUBLISHER_CREDIT", "TAX_ADJUSTMENT_CREDIT"
    ],
    "credit": [
        "LENDING_CREDIT", "LENDING_CREDIT_REFUND", "SHOP_CASH_CREDIT",
    ],
    "refund": [
        "REFUND", "REFUND_ADJUSTMENT", "REFUND_FAILURE", "IMPORT_TAX_REFUND", "APPLICATION_FEE_REFUND",
        "CHARGEBACK_FEE_REFUND", "LENDING_CAPITAL_REFUND", "LENDING_CREDIT_REFUND_REVERSAL"
    ],
    "dispute": [
        "CHARGEBACK_HOLD", "CHARGEBACK_HOLD_RELEASE", "DISPUTE_REVERSAL", "DISPUTE_WITHDRAWAL", "CHARGE_ADJUSTMENT",
    ],
    "reserve": [
        "RESERVED_FUNDS", "RESERVED_FUNDS_REVERSAL", "RESERVED_FUNDS_WITHDRAWAL", "RISK_WITHDRAWAL", "RISK_REVERSAL",
    ],
    "payout": [
        "TRANSFER",
    ],
    "payout_failure": ["TRANSFER_FAILURE", "ACH_BANK_FAILURE_DEBIT_FEE"
                       ],
    "payout_cancellation": [
        "TRANSFER_CANCEL",
    ],
    "debit": [
        "CHARGEBACK_FEE", "BILLING_DEBIT", "LENDING_DEBIT", "LENDING_CAPITAL_REMITTANCE", "MERCHANT_TO_MERCHANT_DEBIT",
        "SHOP_CASH_BILLING_DEBIT", "SHOP_CASH_CAMPAIGN_BILLING_DEBIT", "SHOP_CASH_REFUND_DEBIT",
        "SHOPIFY_COLLECTIVE_DEBIT", "SHOPIFY_SOURCE_DEBIT", "CUSTOMS_DUTY", "IMPORT_TAX", "SHIPPING_LABEL",
        "SHIPPING_LABEL_ADJUSTMENT", "SHIPPING_OTHER_CARRIER_CHARGE_ADJUSTMENT", "SHIPPING_RETURN_TO_ORIGIN_ADJUSTMENT",
        "REFERRAL_FEE", "REFERRAL_FEE_TAX", "STRIPE_FEE", "ANOMALY_DEBIT", "CHARGEBACK_PROTECTION_DEBIT",
        "TAX_ADJUSTMENT_DEBIT", "ADS_PUBLISHER_CREDIT_REVERSAL", "ANOMALY_CREDIT_REVERSAL", "CHANNEL_CREDIT_REVERSAL",
        "COLLECTIONS_CREDIT_REVERSAL", "LENDING_CREDIT_REVERSAL", "MARKETPLACE_FEE_CREDIT_REVERSAL",
        "MERCHANT_GOODWILL_CREDIT_REVERSAL", "PROMOTION_CREDIT_REVERSAL", "SELLER_PROTECTION_CREDIT_REVERSAL",
        "SHOPIFY_COLLECTIVE_CREDIT_REVERSAL",
    ],
    "adjustment": [
        "ADVANCE", "ADVANCE_FUNDING", "CUSTOMS_DUTY_ADJUSTMENT", "IMPORT_TAX_ADJUSTMENT",
    ]
}


class TransactionQueryHelper:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def _to_int(val):
        if val is None:
            return None
        try:
            # Shopify GIDs: 'gid://shopify/Order/1234567890' -> 1234567890
            if isinstance(val, str) and val.startswith('gid://'):
                return int(val.split('/')[-1])
            return int(val)
        except Exception:
            return None

    @staticmethod
    def _to_str_amount(val):
        if val is None:
            return None
        try:
            return str(val)
        except Exception:
            return None

    @staticmethod
    def _adj_orders(adj_orders):
        if not adj_orders:
            return None
        return adj_orders

    def _gql_to_rest_transactions(self, response_data):
        """
        Helper to convert GraphQL response to REST transactions response format.
        """

        # Set type according to GQL_TO_REST_TRANSACTION_MAPPING
        def map_gql_type_to_rest_type(gql_t_type):
            if not gql_t_type:
                return "other"
            for rest_t_type, gql_types in GQL_TO_REST_TRANSACTION_MAPPING.items():
                if gql_t_type in gql_types:
                    return rest_t_type
            return gql_t_type.lower() if isinstance(gql_t_type, str) else "other"

        transactions = []
        if not response_data:
            return transactions
        for node in response_data:
            associated_order = node.get("associatedOrder")
            associated_payout = node.get("associatedPayout")
            gql_type = node.get("type")
            rest_type = map_gql_type_to_rest_type(gql_type)
            txn = {
                "id": self._to_int(node.get("id")),
                "type": rest_type,
                "gql_type": gql_type,  # keep original for reference/debugging
                "test": node.get("test", False),
                "payout_id": self._to_int(associated_payout["id"]) if associated_payout and associated_payout.get(
                    "id") else None,
                "payout_status": associated_payout.get("status") if associated_payout else None,
                "currency": node.get("amount", {}).get("currencyCode"),
                "amount": self._to_str_amount(node.get("amount", {}).get("amount")),
                "fee": self._to_str_amount(node.get("fee", {}).get("amount")),
                "net": self._to_str_amount(node.get("net", {}).get("amount")),
                "source_id": self._to_int(node.get("sourceId")),
                "source_type": node.get("sourceType").lower() if node.get("sourceType") else None,
                "source_order_id": self._to_int(associated_order["id"]) if associated_order and associated_order.get(
                    "id") else None,
                "source_order_transaction_id": self._to_int(node.get("sourceOrderTransactionId")),
                "processed_at": node.get("transactionDate"),
                "adjustment_order_transactions": self._adj_orders(node.get("adjustmentsOrders")),
                "adjustment_reason": node.get("adjustmentReason"),
            }
            transactions.append(self.client.graphql_object(txn))
        return transactions

    def get_transactions(self, payout_id=None, filters=None, first=250, fields=None, rest_format=False):
        """
        Fetch all balance transactions filtered by payout_id and optional additional filters, handling pagination.
        :param payout_id: The payments_transfer_id to filter by (string or int, optional)
        :param filters: dict of additional field:value pairs to filter by
        :param first: number of records to fetch per page
        :param fields: string of fields to fetch (default: all relevant fields)
        :param rest_format: if True, return in REST response format
        :return: list of transaction dicts or REST response dict
        """
        query_name = "MyQuery"
        object_name = "shopifyPaymentsAccount"
        connection_name = "balanceTransactions"
        fields = fields or '''
          id adjustmentReason fee { amount currencyCode } net { amount currencyCode } 
          adjustmentsOrders { amount { amount currencyCode } fees { amount currencyCode } link name orderTransactionId net { amount currencyCode } } 
          amount { amount currencyCode } associatedOrder { id name } associatedPayout { id status } 
          sourceId sourceOrderTransactionId sourceType test transactionDate type
        '''
        filters = filters or {}
        if payout_id is not None:
            filters = dict(filters)
            filters["payments_transfer_id"] = payout_id
        all_data, has_next, end_cursor = self.client.fetch_all_connection_data(
            query_name=query_name,
            object_name=object_name,
            connection_name=connection_name,
            fields=fields,
            filters=filters,
            first=first
        )
        if rest_format:
            return self._gql_to_rest_transactions(all_data)
        return all_data
