"""
-*- coding: utf-8 -*-
Part of Odoo. See LICENSE file for full copyright and licensing details.
"""
PaymentTermsTemplateFields = "id name paymentTermsType dueInDays"

class PaymentTermsQueryHelper:
    """
    Helper class to construct and execute GraphQL queries for fetching payment terms from Shopify.
    """
    def __init__(self, client: object):
        """
        Initializes the PaymentTermsQueryHelper with a ShopifyGraphQLClient instance.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.client = client

    def get_payment_term(self, payment_term_template_gid: str):
        """
        Fetches payment term for given payment_term_template_gid.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        query = f"""
        query GetPaymentTermsTemplateById($id: ID!) {{
          node(id: $id) {{
            ... on PaymentTermsTemplate {{ {PaymentTermsTemplateFields} }}
          }}
        }}
        """
        result = self.client.execute(query, variables={"id": payment_term_template_gid})
        return result

    def get_payment_terms(self):
        """
        Fetches payment terms for given payment_term_template_gid.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        query = f"""
        query GetAllPaymentTermsTemplates {{
          paymentTermsTemplates {{ {PaymentTermsTemplateFields} }}
        }}
        """
        result = self.client.execute(query)
        return result

    @staticmethod
    def get_payment_terms_nodes(result):
        """
        Helper method to return payment terms nodes for given payment_term_template_gid.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not isinstance(result, dict):
            return []
        data = result.get("data") or {}
        payment_terms = data.get("paymentTermsTemplates")
        if isinstance(payment_terms, list):
            return payment_terms
        node = data.get("node")
        return [node] if node else []
