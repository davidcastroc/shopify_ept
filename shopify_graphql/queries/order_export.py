"""
-*- coding: utf-8 -*-
Part of Odoo. See LICENSE file for full copyright and licensing details.
"""
import json


class OrderExportQueryHelper:
    """
    Helper class to construct and execute GraphQL queries for exporting orders from Shopify.
    """
    def __init__(self, client: object):
        """
        Initializes the OrderExportQueryHelper with a ShopifyGraphQLClient instance.
        :param client: An instance of ShopifyGraphQLClient to execute GraphQL queries.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.client = client

    @staticmethod
    def _serialize_input(value: dict):
        """
        Recursively serialize a Python dictionary into a GraphQL input string.
        :param value: The input value to serialize (dict, list, bool, int, float, or str)
        :return: A string representation of the input suitable for GraphQL queries

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if isinstance(value, dict):
            parts = []
            for key, val in value.items():
                if val is None:
                    continue
                parts.append(f"{key}: {OrderExportQueryHelper._serialize_input(val)}")
            return "{" + ", ".join(parts) + "}"
        if isinstance(value, list):
            return "[" + ", ".join(OrderExportQueryHelper._serialize_input(item) for item in value) + "]"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        return json.dumps(value)

    def order_create(self, order_input: dict, options_input: dict = None, fields: str = None):
        """
        Constructs and executes a GraphQL mutation to create an order in Shopify.
        :param order_input: A dictionary representing the order input for the mutation
        :param options_input: A dictionary representing the options input for the mutation (optional)
        :param fields: A string specifying which fields to return in the response (optional)
        :return: The result of the GraphQL mutation execution

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        fields = fields or (
            "order { id name number displayFinancialStatus displayFulfillmentStatus paymentGatewayNames "
            "lineItems(first: 250) { nodes { id quantity title variant { id } } } } "
            "userErrors { field message }"
        )
        mutation = f"""
        mutation orderCreate($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {{
          orderCreate(order: $order, options: $options) {{
            {fields}
          }}
        }}
        """
        variables = {"order": order_input, "options": options_input}
        return self.client.execute(mutation, variables=variables)

    def draft_order_create(self, draft_order_input: dict, fields: str = None):
        """
        Creates a draft order on Shopify store.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        fields = fields or (
            "draftOrder { id } "
            "userErrors { field message }"
        )
        mutation = f"""
        mutation DraftOrderCreate($input: DraftOrderInput!) {{
          draftOrderCreate(input: $input) {{
            {fields}
          }}
        }}
        """
        return self.client.execute(mutation, variables={"input": draft_order_input})

    def draft_order_complete(self, draft_order_id: str, fields: str = None):
        """
        Completes draft order on Shopify store.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        fields = fields or (
            "draftOrder { "
            "id "
            "order { id number name displayFinancialStatus displayFulfillmentStatus paymentGatewayNames "
            "lineItems(first: 250) { nodes { id quantity title variant { id } } } } } "
            "userErrors { field message }"
        )
        mutation = f"""
        mutation DraftOrderComplete($id: ID!) {{
          draftOrderComplete(id: $id) {{
            {fields}
          }}
        }}
        """
        return self.client.execute(mutation, variables={"id": draft_order_id})


    def order_mark_as_paid(self, order_id: str):
        """
        Marks Shopify order as paid.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation OrderMarkAsPaid($input: OrderMarkAsPaidInput!) {
          orderMarkAsPaid(input: $input) {
            order { id displayFinancialStatus }
            userErrors { field message }
          }
        }
        """
        return self.client.execute(mutation, variables={"input": {"id": order_id}})

    def get_order_for_update(self, order_id: str, first=250):
        """
        Gets Shopify order details from Shopify store.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        query = """
        query GetOrderForUpdate($id: ID!, $first: Int!) {
          order(id: $id) {
            id name number displayFinancialStatus displayFulfillmentStatus paymentGatewayNames note tags email
            shippingAddress { address1 address2 city provinceCode zip countryCodeV2 firstName lastName phone company }
            billingAddress { address1 address2 city provinceCode zip countryCodeV2 firstName lastName phone company }
            lineItems(first: $first) { nodes { id quantity variant { id } discountedUnitPriceSet { shopMoney { amount currencyCode } } } }
          }
        }
        """
        return self.client.execute(query, variables={"id": order_id, "first": first})

    def order_edit_begin(self, order_id: str):
        """
        Starts edit session for Shopify order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation BeginOrderEdit($id: ID!) {
          orderEditBegin(id: $id) {
            calculatedOrder { id lineItems(first: 250) { nodes { id quantity variant { id } } } }
            userErrors { field message }
          }
        }
        """
        return self.client.execute(mutation, variables={"id": order_id})

    def order_edit_add_variant(self, calculated_order_id: str, variant_id: str, quantity: int):
        """
        Adds variant line item on Shopify order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation AddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
          orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
            calculatedLineItem { id }
            userErrors { field message }
          }
        }
        """
        variables = {"id": calculated_order_id, "variantId": variant_id, "quantity": quantity}
        return self.client.execute(mutation, variables)

    def order_edit_set_quantity(self, calculated_order_id: str, line_item_id: str, quantity: int):
        """
        Updates qty of unfulfilled line item on Shopify order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation SetQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!) {
          orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity) {
            calculatedLineItem { id }
            userErrors { field message }
          }
        }
        """
        variables = {"id": calculated_order_id, "lineItemId": line_item_id, "quantity": quantity}
        return self.client.execute(mutation, variables=variables)

    def order_edit_commit(self, calculated_order_id: str, notify_customer=False):
        """
        Commits order updates on Shopify order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation CommitOrderEdit($id: ID!, $notifyCustomer: Boolean!, $staffNote: String) {
          orderEditCommit(id: $id, notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
            order { id lineItems(first: 250) { nodes { id quantity title variant { id } } } }
            userErrors { field message }
          }
        }
        """
        return self.client.execute(mutation, variables={"id": calculated_order_id, "notifyCustomer": notify_customer})

    def order_update(self, order_input: dict):
        """
        Updates order details on Shopify order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation UpdateOrder($input: OrderInput!) {
          orderUpdate(input: $input) {
            order { id }
            userErrors { field message }
          }
        }
        """
        return self.client.execute(mutation, variables={"input": order_input})
