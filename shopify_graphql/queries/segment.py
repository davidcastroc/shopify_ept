class SegmentQueryHelper:

    def __init__(self, client, **kwargs):
        """
        Helper for Shopify Customer Segment GraphQL queries.
        :param client: ShopifyGraphQLClient instance
        """
        self.client = client
        self.settings = kwargs

    SEGMENT_FIELDS = """
            id
            name
            query
            creationDate
            lastEditDate
        """

    SEGMENT_MEMBER_FIELDS = """
            id
            displayName
            firstName
            lastName
            lastOrderId
            numberOfOrders
            amountSpent {
                amount
                currencyCode
            }
            defaultEmailAddress {
                emailAddress
            }
        """

    def get_shopify_segments(self, first=250, last_edit_date_from=None):
        """
        Fetch customer segments from Shopify.
        :param last_edit_date_from: Optional datetime; when provided, only segments
               with lastEditDate >= this value are fetched (ISO-8601 string).
        :return: List of segment dicts
        """
        filters = f'last_edit_date:>={last_edit_date_from}' if last_edit_date_from else None
        segments, _has_next, _cursor = self.client.fetch_all_connection_data(
            query_name="GetCustomerSegments",
            connection_name="segments",
            fields=self.SEGMENT_FIELDS,
            first=first,
            filters=filters,
            use_nodes=True
        )
        return segments

    def get_segment_members(self, segment_gid, first=250):
        """
        Fetch all members of a specific Shopify customer segment using
        customerSegmentMembers query (edges/node pattern, paginated).
        :param segment_gid: Shopify segment GID (e.g. "gid://shopify/Segment/123")
        :param first: Page size
        :return: List of customer member dicts with keys: id, firstName, lastName, defaultEmailAddress
        """
        all_members = []
        after = None

        while True:
            after_str = f', after: "{after}"' if after else ""
            query = f"""
            query GetCustomerSegmentMembers {{
                customerSegmentMembers(first: {first}, segmentId: "{segment_gid}"{after_str}) {{
                    edges {{
                        node {{
                            {self.SEGMENT_MEMBER_FIELDS}
                        }}
                    }}
                    pageInfo {{
                        endCursor
                        hasNextPage
                    }}
                }}
            }}
            """
            result = self.client.execute(query)
            if "errors" in result:
                raise Exception(f"Shopify GraphQL error: {result['errors']}")

            conn = result.get("data", {}).get("customerSegmentMembers", {})
            page_info = conn.get("pageInfo", {})
            edges = conn.get("edges", [])
            all_members.extend(edge.get("node", {}) for edge in edges)

            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")

        return all_members