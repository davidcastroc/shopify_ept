# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
import json
import logging

_logger = logging.getLogger("Shopify Fulfillment GraphQL")


class FulfillmentQueryHelper:
    def __init__(self, client):
        self.client = client

    def get_fulfillment(self, fulfillment_id, fields=None):
        fields = fields or "id status trackingInfo"
        query = f'''
        {{
          fulfillment(id: "gid://shopify/Fulfillment/{fulfillment_id}") {{
            {fields}
          }}
        }}
        '''
        return self.client.execute(query)

    def create_fulfillment(self, fulfillment_data_list):
        """
        Create multiple fulfillment in a single GraphQL request using aliases.
        `fulfillment_data_list` is a list of fulfillment_data dicts (same format as before).
        Returns GraphQL response dict.
        """
        mutation_blocks = []

        for idx, fulfillment_data in enumerate(fulfillment_data_list):
            # Build lineItemsByFulfillmentOrder
            line_items_str_list = []
            for fo_data in fulfillment_data.get('line_items_by_fulfillment_order', []):
                fo_id = fo_data.get('fulfillment_order_id')
                if fo_id and not str(fo_id).startswith('gid://'):
                    fo_id = f'gid://shopify/FulfillmentOrder/{fo_id}'

                fo_line_items = fo_data.get('fulfillment_order_line_items', []) or []
                line_items_parts = []
                for li in fo_line_items:
                    li_id = li.get('id')
                    if li_id and not str(li_id).startswith('gid://'):
                        li_id = f'gid://shopify/FulfillmentOrderLineItem/{li_id}'
                    li_qty = int(li.get('quantity', 0))
                    line_items_parts.append(f'{{ id: "{li_id}", quantity: {li_qty} }}')

                line_items_joined = ', '.join(line_items_parts)
                line_items_str_list.append(
                    f'{{ fulfillmentOrderId: "{fo_id}", fulfillmentOrderLineItems: [{line_items_joined}] }}'
                )

            line_items_str = '[' + ', '.join(line_items_str_list) + ']'

            # Build trackingInfo (object)
            tracking_str = ""
            if fulfillment_data.get('tracking_info'):
                tracking = fulfillment_data['tracking_info']
                tracking_parts = []
                if tracking.get('company'):
                    tracking_parts.append(f'company: {json.dumps(tracking["company"])}')
                if tracking.get('number'):
                    tracking_parts.append(f'number: {json.dumps(tracking["number"])}')
                if tracking.get('url'):
                    tracking_parts.append(f'url: {json.dumps(tracking["url"])}')
                if tracking_parts:
                    tracking_str = f', trackingInfo: {{ {", ".join(tracking_parts)} }}'

            notify = "true" if fulfillment_data.get('notify_customer', False) else "false"

            # Add alias for each fulfillment
            mutation_blocks.append(f'''
            f{idx}: fulfillmentCreate(
                fulfillment: {{
                    lineItemsByFulfillmentOrder: {line_items_str},
                    notifyCustomer: {notify}
                    {tracking_str}
                }}
            ) {{
                fulfillment {{
                    id
                    name
                    status
                    createdAt
                    updatedAt
                    trackingInfo {{
                        company
                        number
                        url
                    }}
                    deliveredAt
                    estimatedDeliveryAt
                    inTransitAt
                }}
                userErrors {{
                    field
                    message
                }}
            }}
            ''')

        mutation_body = '\n'.join(mutation_blocks)
        full_mutation = f"mutation CreateMultipleFulfillments {{\n{mutation_body}\n}}"

        _logger.debug("Creating multiple fulfillments with mutation: %s", full_mutation)
        result = self.client.execute(full_mutation)

        # Log errors individually
        if result and 'errors' not in result:
            data = result.get('data', {}) or {}
            for idx in range(len(fulfillment_data_list)):
                alias = f"f{idx}"
                fulfillment_create = data.get(alias, {}) or {}
                user_errors = fulfillment_create.get('userErrors', [])
                if user_errors:
                    _logger.error("Fulfillment creation errors for alias %s: %s", alias, user_errors)
                else:
                    fulfillment = fulfillment_create.get('fulfillment', {})
                    _logger.info("Fulfillment created successfully for alias %s: %s", alias, fulfillment.get('id'))
        else:
            _logger.error("Fulfillment creation failed: %s", result.get('errors', 'Unknown error'))

        return result

    def get_fulfillment_orders(self, order_id):
        """
        Get fulfillment orders for a given numeric order id (no gid).
        Returns GraphQL response dict.
        """
        query = f'''
        {{
          order(id: "gid://shopify/Order/{order_id}") {{
            id
            name
            fulfillmentOrders(first: 50) {{
              edges {{
                node {{
                  id
                  status
                  requestStatus
                  deliveryMethod {{
                    methodType
                  }}
                  assignedLocation {{
                    location {{
                      id
                      name
                      address {{
                        address1
                        city
                        province
                        country
                        zip
                      }}
                    }}
                  }}
                  lineItems(first: 200) {{
                    edges {{
                      node {{
                        id
                        lineItem {{
                          id
                          sku
                          name
                          variant {{
                            id
                          }}
                        }}
                        remainingQuantity
                        totalQuantity
                      }}
                    }}
                  }}
                  supportedActions {{
                    action
                    externalUrl
                  }}
                }}
              }}
            }}
          }}
        }}
        '''
        _logger.debug("Fetching fulfillment orders for order %s", order_id)
        result = self.client.execute(query)

        if result and 'errors' not in result:
            order_data = result.get('data', {}).get('order', {}) or {}
            fo_count = len(order_data.get('fulfillmentOrders', {}).get('edges', []))
            _logger.info("Found %s fulfillment orders for order %s", fo_count, order_id)
        else:
            _logger.error("Failed to get fulfillment orders: %s", result.get('errors', 'Unknown error'))

        return result

    def find_order(self, sale_order):
        order_query = '''
        {
          order(id: "gid://shopify/Order/%s") {
            id
            name
            displayFulfillmentStatus
            cancelledAt
            cancelReason
            currentTotalPriceSet {
              shopMoney {
                amount
              }
            }
            fulfillmentOrders(first: 10) {
              edges {
                node {
                  id
                  status
                }
              }
            }
          }
        }
        ''' % sale_order.shopify_order_id

        response = self.client.execute(order_query)

        if 'errors' in response:
            _logger.error("GraphQL order query error for %s: %s", sale_order.name, response['errors'])
            return {}

        order_data = response.get('data', {}).get('order', {})
        if not order_data:
            _logger.warning("Order not found: %s", sale_order.shopify_order_id)
            return {}

        return order_data
