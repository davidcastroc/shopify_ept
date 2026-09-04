

class CustomerQueryHelper:

    def __init__(self, client, **kwargs):
        """
        Helper for Shopify Customer GraphQL queries.
        :param client: GraphQL client (should handle access token, endpoint)
        :param kwargs: Additional settings
        """
        self.client = client
        self.settings = kwargs

    DEFAULT_FIELDS = """
                id createdAt updatedAt firstName lastName email phone tags
                defaultAddress { id firstName lastName city company address1 address2 country name phone
                    province provinceCode zip countryCodeV2
                }
                addresses {
                    id firstName lastName company address1 address2 province
                    provinceCode country zip name countryCodeV2 phone
                }
    """

    def convert_customer_response_graphql_to_rest_ept(self, customers):
        """Main function to convert GraphQL product → REST-like dict."""
        customer_res_list = []
        for customer_res in customers:
            customer_data = self.convert_customer_single_response_to_rest_ept(customer_res)
            customer_res_list.append(customer_data)
        return customer_res_list

    def convert_customer_single_response_to_rest_ept(self, customer_res):
        """Main function to convert GraphQL product → REST-like dict."""
        customer_id = self.gid_to_int_ept(customer_res["id"])
        rest_customer = {
            "id": customer_id,
            "created_at": customer_res.get("createdAt"),
            "updated_at": customer_res.get("updatedAt"),
            "first_name": customer_res.get("firstName"),
            "last_name": customer_res.get("lastName"),
            "email": customer_res.get("email"),
            "phone": customer_res.get("phone"),
            "tags": ", ".join(customer_res.get("tags") or []),
            "admin_graphql_api_id": customer_res.get("id"),
            "default_address": {},
            "addresses": [],
            "metafields": []
        }
        default_addr = customer_res.get("defaultAddress", {})
        if default_addr:
            rest_customer['default_address'] = {
                'id': self.extract_id_from_str_gid(default_addr.get("id")),
                'first_name': default_addr.get("firstName"),
                'last_name': default_addr.get("lastName"),
                'city': default_addr.get("city"),
                'company': default_addr.get("company"),
                'address1': default_addr.get("address1"),
                'address2': default_addr.get("address2"),
                'country': default_addr.get("country"),
                'name': default_addr.get("name"),
                'phone': default_addr.get("phone"),
                'province': default_addr.get("province"),
                'province_code': default_addr.get("provinceCode"),
                'zip': default_addr.get("zip"),
                'country_code': default_addr.get("countryCodeV2")}

        for address in customer_res.get("addresses", []):
            rest_customer['addresses'].append({
                'id': self.extract_id_from_str_gid(address.get("id")),
                'first_name': address.get("firstName"),
                'last_name': address.get("lastName"),
                'city': address.get("city"),
                'company': address.get("company"),
                'address1': address.get("address1"),
                'address2': address.get("address2"),
                'country': address.get("country"),
                'name': address.get("name"),
                'phone': address.get("phone"),
                'province': address.get("province"),
                'province_code': address.get("provinceCode"),
                'zip': address.get("zip"),
                'country_code': address.get("countryCodeV2")})

        metafields_data = customer_res.get("metafields", {}).get("edges", [])
        for metafield_edge in metafields_data:
            node = metafield_edge.get("node", {})
            rest_customer['metafields'].append({
                'id': self.extract_id_from_str_gid(node.get("id")),
                'namespace': node.get("namespace"),
                'key': node.get("key"),
                'value': node.get("value")
            })
            
        return rest_customer

    @staticmethod
    def gid_to_int_ept(gid):
        """Convert Shopify GID → integer ID."""
        if not gid:
            return None
        try:
            return int(gid.split("/")[-1])
        except Exception:
            return gid

    @staticmethod
    def extract_id_from_str_gid(gid):
        """
        Extracts the integer ID from a Shopify GID string.
        """
        if not gid:
            return None
        try:
            # Remove query params if present
            gid = gid.split("?")[0]
            return int(gid.split("/")[-1])
        except Exception:
            return gid

    def get_shopify_customers(self, first=250, **kwargs):
        """
        Fetch customers from Shopify with optional filtering by last updated date and pagination.
        :param last_date_customer_import: Optional datetime string to filter customers updated after this date.
        :param after_cursor: Optional cursor for pagination.
        :return: GraphQL response with customer data.
        """
        query_name = "GetCustomers"
        connection_name = "customers"
        query_filters = kwargs.get('query_filter', [])
        query_fields = CustomerQueryHelper.DEFAULT_FIELDS
        is_metafield_sync_enabled = self.settings.get('is_enable_metafield_sync', False)
        
        if is_metafield_sync_enabled:
            query_fields += " metafields(first: 50) { edges { node { id namespace key value } } }"

        customers, has_next, end_cursor = self.client.fetch_all_connection_data(
            query_name=query_name,
            connection_name=connection_name,
            fields=query_fields,
            filters=query_filters,
            first=first,
            use_nodes=True,
            no_pagination=kwargs.get('no_pagination', False),
            extra_connection_args=kwargs.get('extra_connection_args', False)
        )
        if customers:
            customer_response = self.convert_customer_response_graphql_to_rest_ept(customers)
            return customer_response, has_next, end_cursor
        return [], has_next, end_cursor

    def search_customer_by_email(self, email, first=10):
        """
        Search Shopify customers by email and return exact-matched nodes.
        :param email: Customer email to search
        :param first: Number of records to fetch
        :return: list of exact matched customer nodes or error string when no customer is found

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not email:
            return "Customer email is required to search in Shopify."
        query = """
        query SearchCustomers($first: Int!, $query: String!) {
          customers(first: $first, query: $query) {
            nodes { id firstName lastName email phone }
          }
        }
        """
        query_filter = f"email:{email}"
        response = self.client.execute(query, variables={"first": first, "query": query_filter})
        if response.get("errors"):
            error_message = "\n".join(
                [str(error["message"]) for error in response.get("errors", []) if error.get("message")]
            )
            return f"Shopify returned errors while searching customer by email.\nError: {error_message}"

        nodes = self._extract_graphql_customers(response)
        normalized_email = email.strip().lower()
        exact_nodes = [
            node for node in nodes
            if (node.get("email") or "").strip().lower() == normalized_email
        ]
        if not exact_nodes:
            return False
        return exact_nodes

    def create_customer(self, customer_input):
        """
        Create a customer in Shopify using GraphQL.
        :param customer_input: CustomerInput payload
        :return: created customer node dict, or error string

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        mutation = """
        mutation customerCreate($input: CustomerInput!) {
          customerCreate(input: $input) {
            customer { id firstName lastName email phone }
            userErrors { field message }
          }
        }
        """
        response = self.client.execute(mutation, variables={"input": customer_input})
        self.client._handle_graphql_cost_throttle(response)

        if response.get("errors"):
            error_message = "\n".join(
                [str(error["message"]) for error in response.get("errors", []) if error.get("message")]
            )
            return (f"Shopify returned errors while creating customer.\nError: {error_message}\n"
                    f"Customer Name: {customer_input.get('firstName')} {customer_input.get('lastName')}")

        data = response.get("data", {}).get("customerCreate", {})
        user_errors = data.get("userErrors") or []
        if user_errors:
            error_message = "\n".join(
                [str(error.get("message")) for error in user_errors if error.get("message")]
            )
            return (f"Shopify returned user errors while creating customer.\nError: {error_message}\n"
                    f"Customer Name: {customer_input.get('firstName')} {customer_input.get('lastName')}")

        customer = data.get("customer")
        if not customer:
            return "Shopify customer was not returned after customer create operation."
        return customer

    def update_customer_addresses(self, customer_id, addresses):
        """
        Update customer addresses using Admin GraphQL customerUpdate mutation.
        :param customer_id: Shopify customer numeric id or gid
        :param addresses: list of MailingAddressInput payloads
        :return: updated customer dict or error string

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        if not customer_id:
            return "Shopify customer id is required to update customer addresses."
        if not addresses:
            return False

        customer_gid = customer_id if str(customer_id).startswith("gid://") else f"gid://shopify/Customer/{customer_id}"
        mutation = """
        mutation UpdateCustomerAddresses($customerId: ID!, $addresses: [MailingAddressInput!]) {
          customerUpdate(input: { id: $customerId, addresses: $addresses }) {
            customer { id }
            userErrors { field message }
          }
        }
        """
        response = self.client.execute(mutation, variables={"customerId": customer_gid, "addresses": addresses})
        self.client._handle_graphql_cost_throttle(response)

        if response.get("errors"):
            error_message = "\n".join(
                [str(error["message"]) for error in response.get("errors", []) if error.get("message")]
            )
            return (f"Shopify returned errors while updating customer addresses.\nError: {error_message}\n"
                    f"Customer ID: {customer_id}")

        data = response.get("data", {}).get("customerUpdate", {})
        user_errors = data.get("userErrors") or []
        if user_errors:
            error_message = "\n".join(
                [str(error.get("message")) for error in user_errors if error.get("message")]
            )
            return (f"Shopify returned user errors while updating customer addresses.\nError: {error_message}\n"
                    f"Customer ID: {customer_id}")
        return data.get("customer") or {}

    @staticmethod
    def _extract_graphql_customers(response):
        """
        Extracts product nodes from a GraphQL response.

        :author: Karan Modasiya on Date 30-Mar-2026.
        """
        edges = response.get("data", {}).get("customers", {})
        return list(edges.get('nodes', []))
