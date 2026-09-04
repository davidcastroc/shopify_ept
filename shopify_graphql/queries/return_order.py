"""
-*- coding: utf-8 -*-
Part of Odoo. See LICENSE file for full copyright and licensing details.
"""


class ReturnQueryHelper:
    """
    Helper class to query Shopify Return Orders.
    """

    def __init__(self, client: object):
        """
        Initializes the ReturnQueryHelper with a ShopifyGraphQLClient instance.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        self.client = client

    def get_returnable_fulfillments(self, order_gid: str, first: int = 50):
        """
        Fetches Return Fulfillments for a Shopify Order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        query = """
        query ReturnableFulfillments($orderId: ID!, $first: Int!) {
          returnableFulfillments(orderId: $orderId, first: $first) {
            nodes {
              id
              returnableFulfillmentLineItems(first: 250) {
                nodes { quantity fulfillmentLineItem { id lineItem { id } } }
              }
            }
          }
        }
        """
        return self.client.execute(query, variables={"orderId": order_gid, "first": first})

    def return_create(self, return_input: dict):
        """
        Creates a return for Shopify Order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation ReturnCreate($returnInput: ReturnInput!) {
          returnCreate(returnInput: $returnInput) {
            return { id status order { id } }
            userErrors { field message }
          }
        }
        """
        return self.client.execute(mutation, variables={"returnInput": return_input})

    def get_return_for_processing(self, return_gid: str):
        """
        Fetches returnable line items for a Shopify Order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        query = """
        query GetReturnForProcessing($id: ID!) {
          return(id: $id) {
            id status
            returnLineItems(first: 250) {
              nodes { id quantity }
            }
            reverseFulfillmentOrders(first: 50) {
              nodes { id lineItems(first: 250) { nodes { id totalQuantity } } }
            }
          }
        }
        """
        return self.client.execute(query, variables={"id": return_gid})

    def return_process(self, return_process_input: dict):
        """
        Processes return for Shopify Order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation ReturnProcess($input: ReturnProcessInput!) {
          returnProcess(input: $input) {
            return { id status }
            userErrors { field message }
          }
        }
        """
        return self.client.execute(mutation, variables={"input": return_process_input})

    def return_close(self, return_gid: str):
        """
        Closes return for Shopify Order.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation ReturnClose($id: ID!) {
          returnClose(id: $id) {
            return { id status }
            userErrors { field message }
          }
        }
        """
        return self.client.execute(mutation, variables={"id": return_gid})

    def get_returnable_line_items(self, result: dict):
        """
        Returns map: {shopify_line_item_id(str): [{'fulfillment_line_item_id': gid, 'quantity': int}, ...]}

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mapping = {}
        nodes = result.get("data", {}).get("returnableFulfillments", {}).get("nodes", []) or []
        for node in nodes:
            line_items = node.get("returnableFulfillmentLineItems", {}).get("nodes", []) or []
            for line in line_items:
                fulfillment_line_item = line.get("fulfillmentLineItem") or {}
                line_item_gid = (fulfillment_line_item.get("lineItem") or {}).get("id")
                fulfillment_line_item_gid = fulfillment_line_item.get("id")
                qty = int(line.get("quantity") or 0)
                line_item_id = self.client._extract_id_from_gid(line_item_gid)
                if not line_item_id or not fulfillment_line_item_gid or qty <= 0:
                    continue
                mapping.setdefault(str(line_item_id), []).append(
                    {"fulfillment_line_item_id": fulfillment_line_item_gid, "quantity": qty}
                )
        return mapping
