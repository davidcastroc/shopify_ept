import logging

_logger = logging.getLogger(__name__)


class PayoutQueryHelper:

    @staticmethod
    def gql_node_to_rest(node):
        """
        Convert a GQL payout node to REST-like dict, mapping all fields.
        """
        rest = {}
        for k, v in node.items():
            if k == 'legacyResourceId':
                rest['id'] = int(v)
            elif k == 'status':
                rest['status'] = v.lower() if isinstance(v, str) else v
            elif k == 'issuedAt':
                rest['date'] = v[:10] if v else None
            elif k == 'net':
                rest['amount'] = v.get('amount')
                rest['currency'] = v.get('currencyCode')
            elif k == 'summary' and isinstance(v, dict):
                summary = {}
                for gql_key, rest_key in [
                    ('adjustmentsFee', 'adjustments_fee_amount'), ('adjustmentsGross', 'adjustments_gross_amount'),
                    ('chargesFee', 'charges_fee_amount'), ('chargesGross', 'charges_gross_amount'),
                    ('refundsFee', 'refunds_fee_amount'), ('refundsFeeGross', 'refunds_gross_amount'),
                    ('reservedFundsFee', 'reserved_funds_fee_amount'),
                    ('reservedFundsGross', 'reserved_funds_gross_amount'),
                    ('retriedPayoutsFee', 'retried_payouts_fee_amount'),
                    ('retriedPayoutsGross', 'retried_payouts_gross_amount'),
                    ('advanceFees', 'advance_fees_amount'), ('advanceGross', 'advance_gross_amount'),
                ]:
                    summary[rest_key] = v.get(gql_key, {}).get('amount', '0.00')
                rest['summary'] = summary
            else:
                rest[k] = v
        return rest

    def __init__(self, client, **kwargs):
        """
        Helper for Shopify Payout GraphQL queries.
        :param client: GraphQL client (should handle access token, endpoint)
        :param kwargs: Additional settings (e.g., payout_visible_currency)
        """
        self.client = client
        self.use_presentment = kwargs.get('payout_visible_currency', False)
        self.settings = kwargs

    def parse_payout_data(self, response_data):
        """
        Convert GraphQL payout edges to a list of REST-like dicts for Odoo compatibility.
        """
        payout_reports = []
        for node in response_data:
            rest_node = self.gql_node_to_rest(node)
            rest_node_object = self.client.graphql_object(rest_node)
            payout_reports.append(rest_node_object)
        return payout_reports

    def get_payouts(self, first=10, **kwargs):
        """
        Fetch payouts with flexible filters via kwargs (e.g., status, date_min, date_max), handling pagination using the client's common method.
        """
        query_name = "GetPayouts"
        object_name = "shopifyPaymentsAccount"
        connection_name = "payouts"
        query_filters = kwargs.get('query_filter', [])
        fields = '''
            id status transactionType issuedAt legacyResourceId
            net { amount currencyCode }
            summary { adjustmentsFee { amount } chargesFee { amount } refundsFee { amount } reservedFundsFee { amount } 
                    retriedPayoutsFee { amount } adjustmentsGross { amount } advanceFees { amount } advanceGross { amount } 
                    chargesGross { amount } refundsFeeGross { amount } reservedFundsGross { amount } retriedPayoutsGross { amount } }
        '''
        all_data, has_next, end_cursor = self.client.fetch_all_connection_data(
            query_name=query_name,
            object_name=object_name,
            connection_name=connection_name,
            fields=fields,
            filters=query_filters,
            first=first
        )
        if all_data:
            return self.parse_payout_data(all_data)
        return []
