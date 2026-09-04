import requests
import logging
import re
import time

_logger = logging.getLogger("Shopify GraphQL Client")


class GraphQLObject:
    """
    Generic object wrapper for GraphQL response dicts.
    Provides attribute access, .to_dict(), and .get() methods.
    """

    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getattr__(self, key):
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(key)


class ShopifyGraphQLClient:
    def __init__(self, access_token, shop_url, gql_debug=False):
        self.access_token = access_token
        self.shop_url = shop_url.rstrip('/')
        self.endpoint = f"{self.shop_url}/admin/api/2026-01/graphql.json"
        self.MAX_RETRIES = 3  # Max retries for a single API execution
        self.graphql_object = GraphQLObject
        self.gql_debug = gql_debug

    def execute(self, query, variables=None):
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.access_token,
        }
        payload = {"query": query}
        response = []
        if variables:
            payload["variables"] = variables
        for attempt in range(self.MAX_RETRIES):
            try:
                # Execute the API call
                response = requests.post(self.endpoint, json=payload, headers=headers)
                _logger.info(f"GraphQL API call :  {response}")
                response.raise_for_status()
                response = response.json()
                if self.gql_debug:
                    _logger.info(f"GraphQL Request: {payload}")
                    _logger.info(f"GraphQL Response: {response}")
                self._handle_graphql_cost_throttle(response)
                return response
            except requests.exceptions.ConnectionError as e:
                # Catch network specific errors (like Errno 101)
                if attempt < self.MAX_RETRIES - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s...
                    _logger.warning(
                        f"Shopify network connection failed (Attempt {attempt + 1}/{self.MAX_RETRIES}). Retrying after {wait_time}s. Error: {e.args[0]}"
                    )
                    time.sleep(wait_time)
                    continue  # Continue to the next attempt
                else:
                    _logger.error(f"Shopify network connection failed after {self.MAX_RETRIES} attempts. Query failed.")
                    raise e
            except requests.exceptions.HTTPError as e:
                _logger.error(f"Shopify HTTP Error {e.response.status_code}. Query failed.")
                raise e
            except Exception as e:
                # Catch other unknown exceptions
                _logger.error(f"Unexpected error during Shopify API execution: {e}.")
                raise e
        return response

    def _handle_graphql_cost_throttle(self, result):
        """Checks and waits if the GraphQL cost bucket is low."""
        cost_info = result.get('extensions', {}).get('cost', {})
        currently_available = cost_info.get('throttleStatus', {}).get('currentlyAvailable', 1000)
        requested_cost = cost_info.get('requestedQueryCost', 0)
        if currently_available < requested_cost:
            wait_time = 1 + (requested_cost - currently_available) // 50
            _logger.info("Shopify GraphQL cost bucket low (%s), sleeping %ss.", currently_available, wait_time)
            time.sleep(wait_time)

    def bulk_operation(self, query):
        bulk_query = """
        mutation {
          bulkOperationRunQuery(
            query: """ + query.replace('"', '\"') + """
          ) {
            bulkOperation {
              id
              status
            }
            userErrors {
              field
              message
            }
          }
        }
        """
        return self.execute(bulk_query)

    @staticmethod
    def _strip_managed_args(extra_connection_args, managed_keys):
        """
        Remove keys from extra_connection_args that are already handled by
        dedicated parameters in build_query, to prevent duplicate GraphQL arguments.

        :param extra_connection_args: Raw extra args string (e.g. 'after:"abc", sortKey: TITLE')
        :param managed_keys: List of keys to strip (e.g. ['first', 'after', 'query'])
        :return: Cleaned string with managed keys removed, or None if nothing remains
        """
        if not extra_connection_args:
            return extra_connection_args
        for key in managed_keys:
            extra_connection_args = re.sub(
                r',?\s*' + re.escape(key) + r'\s*:\s*("(?:[^"\\]|\\.)*"|\S+)',
                '', extra_connection_args
            ).strip().strip(',').strip()
        return extra_connection_args or None

    @staticmethod
    def build_query(query_name=None, object_name=None, connection_name=None, fields=None,
                    filters=None, first=250, after=None, extra_connection_args=None,
                    use_nodes=False, object_id=None, object_args=None):
        """
        Build a generic GraphQL query string supporting both root-level and nested queries.

        :param query_name: Name of the outer query (e.g., 'MyQuery'). Optional.
        :param object_name: Root object (e.g., 'product', 'order'). Optional for root-level connections.
        :param connection_name: Connection name under the object or at root (e.g., 'variants', 'orders').
        :param fields: Fields string or nested structure
        :param filters: dict, list, or string of filter strings (for root-level queries)
        :param first: Number of records per page
        :param after: Pagination cursor
        :param extra_connection_args: Additional arguments for the connection (e.g., 'sortKey: UPDATED_AT')
        :param use_nodes: If True, use 'nodes' instead of 'edges' for the connection
        :param object_id: ID for the object (e.g., "gid://shopify/Product/123") when querying a specific object
        :param object_args: Additional arguments for the object query (e.g., {'reverse': True})
        :return: GraphQL query string
        """
        managed_keys = []
        if first: managed_keys.append('first')
        if after: managed_keys.append('after')
        if filters: managed_keys.append('query')
        extra_connection_args = ShopifyGraphQLClient._strip_managed_args(extra_connection_args, managed_keys)
        after_str = f', after: "{after}"' if after else ''
        extra_args = f', {extra_connection_args}' if extra_connection_args else ''
        # If we have a connection_name, build connection query
        if connection_name:
            # Handle filters for root-level queries
            if filters:
                if isinstance(filters, dict):
                    filter_parts = [f"{k}:{v}" for k, v in filters.items()]
                elif isinstance(filters, list):
                    filter_parts = filters
                elif isinstance(filters, str):
                    filter_parts = [filters]
                else:
                    filter_parts = []
                filter_str = ' '.join(filter_parts)
                connection_args = f"first: {first}{after_str}, query: \"{filter_str}\"{extra_args}"
            else:
                connection_args = f"first: {first}{after_str}{extra_args}".lstrip(', ')

            # Build connection structure
            if use_nodes:
                connection_query = f"""
                {connection_name}({connection_args}) {{
                    pageInfo {{ endCursor hasNextPage }}
                    nodes {{
                        {fields}
                    }}
                }}"""
            else:
                connection_query = f"""
                {connection_name}({connection_args}) {{
                    edges {{
                        node {{
                            {fields}
                        }}
                    }}
                    pageInfo {{
                        endCursor
                        hasNextPage
                    }}
                }}"""
        else:
            # No connection, just fields directly under object
            connection_query = fields

        # Build object block if object_name is provided
        if object_name:
            # Build object arguments (e.g., id: "gid://...")
            obj_args_list = []
            if object_id:
                obj_args_list.append(f'id: "{object_id}"')
            if object_args:
                for key, val in object_args.items():
                    if isinstance(val, str):
                        obj_args_list.append(f'{key}: "{val}"')
                    elif isinstance(val, bool):
                        obj_args_list.append(f'{key}: {str(val).lower()}')
                    else:
                        obj_args_list.append(f'{key}: {val}')

            obj_args_str = f"({', '.join(obj_args_list)})" if obj_args_list else ""
            object_block = f"{object_name}{obj_args_str} {{\n{connection_query}\n}}"
        else:
            object_block = connection_query

        # Compose the full query
        if query_name:
            query = f"""
            query {query_name} {{
                {object_block}
            }}
            """
        else:
            query = f"""
            {{
                {object_block}
            }}
            """
        return query

    @staticmethod
    def parse_connection_response(response, object_name=None, connection_name=None, use_nodes=False):
        """
        Parse a Shopify GraphQL connection response.
        :param response: Raw response dict from Shopify
        :param object_name: Root object (e.g., 'shopifyPaymentsAccount'), or None for root-level
        :param connection_name: Connection name (e.g., 'orders', 'balanceTransactions')
        :param use_nodes: If True, extract from 'nodes', else from 'edges'
        :return: (has_next_page, end_cursor, data_list)
        :raises: Exception if errors are present or structure is invalid
        """
        if 'errors' in response:
            raise Exception(f"Shopify GraphQL error: {response['errors']}")
        if not response or 'data' not in response:
            raise Exception(f"Invalid or empty response from Shopify GraphQL API.\nResponse: {response}")
        data = response['data']
        if object_name:
            data = data.get(object_name, {})
            if not data: return False, None, []
        if not connection_name:
            # No connection, return the object data directly
            return False, None, [data]
        conn = data.get(connection_name, {})
        page_info = conn.get('pageInfo', {})
        has_next = page_info.get('hasNextPage', False)
        end_cursor = page_info.get('endCursor')
        if use_nodes:
            data_list = conn.get('nodes', [])
        else:
            data_list = [edge.get('node', {}) for edge in conn.get('edges', [])]
        return has_next, end_cursor, data_list

    def fetch_all_connection_data(self, query_name=None, object_name=None, connection_name=None,
                                  fields=None, filters=None, first=250, extra_connection_args=None,
                                  use_nodes=False, no_pagination=False, object_id=None, object_args=None):
        """
        Fetch all data from a Shopify GraphQL connection, handling pagination automatically.

        :param query_name: Name of the outer query (optional)
        :param object_name: Root object (optional for root-level, e.g., 'product')
        :param connection_name: Connection name (e.g., 'variants', 'orders')
        :param fields: Fields string or nested structure
        :param filters: dict or list of filter strings (for root-level queries)
        :param first: Number of records per page
        :param extra_connection_args: Additional arguments for the connection
        :param use_nodes: If True, extract from 'nodes', else from 'edges'
        :param no_pagination: If True, fetch only the first page and return pagination info
        :param object_id: ID for the object when querying a specific object (e.g., "gid://shopify/Product/123")
        :param object_args: Additional arguments for the object query
        :return: (data_list, has_next_page, end_cursor)
        """
        all_data = []
        after = None
        has_next = False
        end_cursor = None
        while True:
            query = self.build_query(
                query_name=query_name,
                object_name=object_name,
                connection_name=connection_name,
                fields=fields,
                filters=filters,
                first=first,
                after=after,
                extra_connection_args=extra_connection_args,
                use_nodes=use_nodes,
                object_id=object_id,
                object_args=object_args
            )
            result = self.execute(query)
            has_next, end_cursor, data_list = self.parse_connection_response(
                result,
                object_name=object_name,
                connection_name=connection_name,
                use_nodes=use_nodes
            )
            all_data.extend(data_list)
            if no_pagination:
                break
            if not has_next:
                break
            after = end_cursor
        return all_data, has_next, end_cursor

    @staticmethod
    def _raise_shopify_graphql_errors(response: object, operation_name: str):
        """
        This method is used to raise errors for shopify export/update process
        :param response: shopify response object
        :param operation_name: operation name for error message context

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if response.get("errors"):
            error_message = "\n".join([str(error.get("message")) for error in response.get("errors", [])
                                       if error.get("message")])
            return "%s failed.\nError: %s" % (operation_name, error_message)

        data = response.get("data", {}) or {}
        payload = next(iter(data.values()), {})
        user_errors = payload.get("userErrors") if isinstance(payload, dict) else []
        if user_errors:
            error_message = "\n".join([str(err.get("message")) for err in user_errors if err.get("message")])
            return "%s failed.\nError: %s" % (operation_name, error_message)
        return False

    @staticmethod
    def _extract_id_from_gid(gid: str):
        """
        Extracts the numerical ID from a Shopify Global ID string.
        :param gid: Shopify Global ID string
        :return: numerical ID

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not isinstance(gid, str):
            return None
        try:
            match = re.search(r'\/(\d+)$', gid)
            return int(match.group(1)) if match else None
        except Exception:
            return None
