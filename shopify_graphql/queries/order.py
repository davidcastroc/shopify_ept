from collections.abc import Mapping
import re
from typing import Dict, Any, List, Optional, Tuple
import logging,time
_logger = logging.getLogger(__name__)


class OrderQueryHelper:
    def __init__(self, client, **kwargs):
        self.client = client
        self.use_presentment = kwargs.get('order_visible_currency', False)
        self.settings = kwargs

    @staticmethod
    def rest_order_fields():
        """
        Returns a string of Shopify Admin GraphQL Order fields, formatted with 20 fields per line for readability.
        """
        order_fields = {
            "basic": '''
                        id app { id } clientIp cancelReason cancelledAt closedAt confirmationNumber confirmed email createdAt discountCodes displayFinancialStatus displayFulfillmentStatus edited estimatedTaxes customerJourneySummary { lastVisit { id landingPage landingPageHtml occurredAt referralCode referralInfoHtml referrerUrl source sourceDescription sourceType utmParameters { campaign content medium source term } } } 
                        name number note statusPageUrl paymentGatewayNames phone presentmentCurrencyCode processedAt sourceIdentifier sourceName tags taxExempt taxLines { channelLiable rate ratePercentage source title priceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } taxesIncluded test totalCashRoundingAdjustment { paymentSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } refundSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } 
                        totalDiscountsSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } totalPriceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } totalOutstandingSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } totalShippingPriceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } totalTaxSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } totalTipReceivedSet { shopMoney { currencyCode amount } presentmentMoney { amount currencyCode } } totalWeight updatedAt unpaid 
                        channelInformation { app { id } channelDefinition { handle } }
                        shippingLines(first: 10) { nodes { id carrierIdentifier code custom source discountedPriceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } shippingRateHandle title taxLines { title source ratePercentage rate priceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } originalPriceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } isRemoved discountAllocations { allocatedAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } } }
                        discountApplications(first: 100) { nodes { ... on DiscountCodeApplication { __typename allocationMethod code index targetSelection targetType value { ... on MoneyV2 { __typename amount currencyCode } ... on PricingPercentageValue { __typename percentage } } } } }
                    '''
            ,
            "customer_data": '''
                                id billingAddress { address1 address2 city company coordinatesValidated country countryCodeV2 firstName formatted(withCompany: false, withName: false) formattedArea id lastName latitude longitude name phone province provinceCode timeZone zip validationResultSummary } currencyCode 
                                customer { id createdAt updatedAt state lastName firstName note verifiedEmail multipassIdentifier email taxExempt tags defaultAddress { address1 address2 city company country countryCodeV2 firstName id lastName latitude longitude name phone province provinceCode timeZone validationResultSummary zip } }
                                shippingAddress { address1 address2 city company coordinatesValidated countryCodeV2 firstName formattedArea id lastName latitude longitude name phone province provinceCode timeZone validationResultSummary zip country }
                            '''
            ,
            "line_items": '''   id lineItems(first: 125) { nodes { id currentQuantity fulfillmentStatus fulfillableQuantity fulfillmentService { serviceName } isGiftCard name originalTotalSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } originalUnitPriceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } requiresShipping product { id } quantity sku variant { id sku taxable title }
                                taxLines(first: 25) { channelLiable price priceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } rate ratePercentage source title } taxable title discountAllocations { allocatedAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } discountApplication { ... on DiscountCodeApplication { __typename index } } } 
                                duties { countryCodeOfOrigin harmonizedSystemCode id price { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } taxLines { priceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } } rate ratePercentage source title } } } }
                          '''
            ,
            "refunds": '''
                              id refunds(first: 25) { createdAt id note legacyResourceId orderAdjustments(first: 50) { nodes { id reason taxAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } amountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } } duties { amountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } originalDuty { countryCodeOfOrigin harmonizedSystemCode id price { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } taxLines { channelLiable rate ratePercentage source title priceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } } } 
                              refundLineItems(first: 125) { nodes { id restocked restockType quantity location { id } subtotalSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } totalTaxSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } lineItem { id name quantity sku title currentQuantity requiresShipping } } } refundShippingLines(first: 10) { nodes { id shippingLine { id code carrierIdentifier title taxLines { channelLiable rate ratePercentage source title priceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } source shippingRateHandle custom discountAllocations { allocatedAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } } subtotalAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } taxAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } } 
                              transactions(first: 25) { nodes { id gateway kind authorizationCode createdAt accountNumber amountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } amountV2 { amount currencyCode } errorCode paymentId paymentMethod processedAt status parentTransaction { id } receiptJson test paymentDetails { ... on CardPaymentDetails { avsResultCode } } } } }
                        '''
            ,
            "transactions": '''
                                id transactions(first: 200) { accountNumber amountV2 { amount currencyCode } amountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } authorizationCode authorizationExpiresAt createdAt errorCode fees { id rate rateName taxAmount { amount currencyCode } type amount { amount currencyCode } } 
                                formattedGateway gateway id kind manualPaymentGateway manuallyCapturable maximumRefundable multiCapturable parentTransaction { id gateway kind status } 
                                paymentId processedAt receiptJson settlementCurrency settlementCurrencyRate status test 
                                totalUnsettledSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } paymentIcon { id src originalSrc } }
                            '''
            ,
            "fulfillment": '''
                                id fulfillments(first: 50) { createdAt deliveredAt displayStatus id location { id name createdAt updatedAt fulfillmentService { callbackUrl handle id inventoryManagement serviceName trackingSupport type } } originAddress { address1 address2 city countryCode provinceCode zip } requiresShipping updatedAt status totalQuantity estimatedDeliveryAt service { handle serviceName trackingSupport type } trackingInfo { company number url } 
                                fulfillmentLineItems(first: 125) { nodes { id quantity lineItem { id currentQuantity fulfillmentService { serviceName id handle type } fulfillmentStatus isGiftCard name product { id } sku variant { id sku } vendor variantTitle taxLines(first: 25) { channelLiable priceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } rate ratePercentage source title } duties { countryCodeOfOrigin harmonizedSystemCode id } discountAllocations { allocatedAmountSet { presentmentMoney { currencyCode amount } shopMoney { amount currencyCode } } } } } } }
                            '''
            ,
            "returns_and_risks": '''
                                id returns(first: 10) { nodes { id returnLineItems(first: 10) { nodes { returnReasonDefinition { name } quantity refundableQuantity refundedQuantity returnReason returnReasonNote ... on ReturnLineItem { id fulfillmentLineItem { id lineItem { id } } } } } } }
                                risk { assessments { riskLevel facts { description sentiment } provider { id title webhookApiVersion } } recommendation }

                                '''
            ,
            "fulfillment_orders": '''
                                id fulfillmentOrders(first: 10) { nodes { id orderId orderName status lineItems(first: 100) { nodes { id lineItem { id } }} assignedLocation { location { id }}      }}
                                '''
            ,
            "metafields": '''
                                id metafields(first: 150) { nodes { id namespace key value type } }
                            '''
        }
        return order_fields

    def get_order_count(self, filters):
        shopify_query = (
            f"fulfillment_status:{filters['fulfillment_status']} "
            f"updated_at:>='{filters['updated_at_min']}' "
            f"updated_at:<='{filters['updated_at_max']}'"
        )
        query = f"""
                {{
                  ordersCount(query: "{shopify_query}") {{
                    count
                  }}
                }}"""
        result = self.client.execute(query)
        if result and 'data' in result:
            return result['data'].get('ordersCount', {}).get('count', 0)
        return 0

    def get_order(self, order_ids: List[str], specific_fields_key: str = False) -> List[Dict[str, Any]]:
        fields = self.rest_order_fields()

        if specific_fields_key and specific_fields_key in fields:
            fields = {specific_fields_key: fields[specific_fields_key]}

        # Check metafield sync setting and remove metafields from query if sync is disabled
        is_metafield_sync_enabled = self.settings.get('is_enable_metafield_sync', False)

        if not is_metafield_sync_enabled and "metafields" in fields:
            fields.pop("metafields")

        if is_metafield_sync_enabled:
            # Metafields are now included under `customer_data`.
            # Merge once and remove standalone key to avoid duplicate GraphQL fields.                
            if "customer_data" in fields:
                customer_data_query = fields["customer_data"]
                customer_start = customer_data_query.find("customer {")

                if customer_start != -1:
                    open_brace_idx = customer_data_query.find("{", customer_start)
                    depth = 0
                    customer_end = None

                    for idx in range(open_brace_idx, len(customer_data_query)):
                        if customer_data_query[idx] == "{":
                            depth += 1
                        elif customer_data_query[idx] == "}":
                            depth -= 1
                            if depth == 0:
                                customer_end = idx
                                break

                    if customer_end is not None:
                        customer_block = customer_data_query[customer_start:customer_end + 1]
                        if "metafields(" not in customer_block:
                            metafields_fragment = " metafields(first: 150) { nodes { id namespace key value type } } "
                            fields["customer_data"] = (
                                customer_data_query[:customer_end]
                                + metafields_fragment
                                + customer_data_query[customer_end:]
                            )
            

        fields_str = "\n".join(value for value in fields.values())

        # Build Shopify GIDs from raw order IDs (supports int/str inputs).
        gids = []
        for oid in order_ids:
            if oid is None: continue
            oid_str = str(oid).strip()
            if not oid_str: continue
            gids.append(
                oid_str if oid_str.startswith("gid://shopify/Order/") else f"gid://shopify/Order/{oid_str}"
            )

        if not gids:
            _logger.info('No valid order IDs provided to get_order(). Input: %s', order_ids)
            return []

        # Fetch all orders in a single GraphQL call using the nodes(ids) query
        query = f'''
        query GetOrdersByIds($ids: [ID!]!) {{
          nodes(ids: $ids) {{
            ... on Order {{
              {fields_str}
            }}
          }}
        }}
        '''
        result = self.client.execute(query, {"ids": gids})
        nodes = result.get('data', {}).get('nodes', [])
        if not nodes:
            _logger.info(f'No Order received by IDs. Response: {result}')
        response = []
        for raw_graphql_order in nodes:
            if raw_graphql_order:
                rest_order_data = self.convert_order_graphql_to_rest(raw_graphql_order)
                response.append(rest_order_data)
        return response

    def list_orders(self, filters):
        """
        Fetches all orders in pages for each field group, merges data by order ID.
        Returns: {order_id: [order_data_dict, ...]} (list contains dicts from each field group/page)

        metafield_sync: If enabled, includes metafields in the query; if disabled, excludes them to optimize performance.
        """
        order_fields = self.rest_order_fields()

        is_metafield_sync_enabled = self.settings.get('is_enable_metafield_sync', False)

        if not is_metafield_sync_enabled and "metafields" in order_fields:
            order_fields.pop("metafields")
        
        if is_metafield_sync_enabled:
            # Metafields are now included under `customer_data`.
            # Merge once and remove standalone key to avoid duplicate GraphQL fields.                
            if "customer_data" in order_fields:
                customer_data_query = order_fields["customer_data"]
                customer_start = customer_data_query.find("customer {")

                if customer_start != -1:
                    open_brace_idx = customer_data_query.find("{", customer_start)
                    depth = 0
                    customer_end = None

                    for idx in range(open_brace_idx, len(customer_data_query)):
                        if customer_data_query[idx] == "{":
                            depth += 1
                        elif customer_data_query[idx] == "}":
                            depth -= 1
                            if depth == 0:
                                customer_end = idx
                                break

                    if customer_end is not None:
                        customer_block = customer_data_query[customer_start:customer_end + 1]
                        if "metafields(" not in customer_block:
                            metafields_fragment = " metafields(first: 150) { nodes { id namespace key value type } } "
                            order_fields["customer_data"] = (
                                customer_data_query[:customer_end]
                                + metafields_fragment
                                + customer_data_query[customer_end:]
                            )

        shopify_query = (
            f"fulfillment_status:{filters['fulfillment_status']} "
            f"updated_at:>='{filters['updated_at_min']}' "
            f"updated_at:<='{filters['updated_at_max']}'"
            # f"status:{filters['status']}"
        )
        merged_orders = {}
        start_time = time.time()
        for group, fields in order_fields.items():
            group_orders = self._fetch_all_orders_for_fields(fields, shopify_query, filters.get("limit", 100))
            for order_id, order_list in group_orders.items():
                if order_id not in merged_orders:
                    merged_orders[order_id] = []
                merged_orders[order_id].extend(order_list)
        response = []
        for order_id, order_list in merged_orders.items():
            merged_order = self.deep_merge_dicts(order_list)
            rest_order_data = self.convert_order_graphql_to_rest(merged_order)
            response.append(rest_order_data)
        end_time = time.time()
        _logger.info(
            f"Total time taken to fetch and merge orders: {end_time - start_time} seconds for date range {filters['updated_at_min']} to {filters['updated_at_max']}")
        return response

    def get_order_returns_by_date(self, updated_at_min: str, updated_at_max: str,
                                  allowed_statuses: Optional[List[str]] = None,
                                  first: int = 100) -> List[Dict[str, Any]]:
        """
        Fetch return/refund payloads grouped by order for a date range.
        Returns flattened payload list consumable by sale.order return import flow.
        """
        
        query_filter = (
            f"updated_at:>='{updated_at_min}' "
            f"updated_at:<='{updated_at_max}' "           
            f"financial_status:refunded OR financial_status:partially_refunded"
            f" OR return_status:returned"
        )

        fields = """
                id name number displayFinancialStatus displayFulfillmentStatus updatedAt returnStatus
                refunds(first: 25) { createdAt id note legacyResourceId orderAdjustments(first: 50) { nodes { id reason taxAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } amountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } } duties { amountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } originalDuty { countryCodeOfOrigin harmonizedSystemCode id price { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } taxLines { channelLiable rate ratePercentage source title priceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } } } 
                                      refundLineItems(first: 25) { nodes { id restocked restockType quantity location { id } subtotalSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } totalTaxSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } lineItem { id name quantity sku title currentQuantity requiresShipping } } } refundShippingLines(first: 50) { nodes { id shippingLine { id code carrierIdentifier title taxLines { channelLiable rate ratePercentage source title priceSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } source shippingRateHandle custom discountAllocations { allocatedAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } } subtotalAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } taxAmountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } } } 
                                      transactions(first: 25) { nodes { id gateway kind authorizationCode createdAt accountNumber amountSet { presentmentMoney { amount currencyCode } shopMoney { amount currencyCode } } amountV2 { amount currencyCode } errorCode paymentId paymentMethod processedAt status parentTransaction { id } receiptJson test paymentDetails { ... on CardPaymentDetails { avsResultCode } } } }  return { id returnLineItems(first: 10) { nodes { returnReasonNote returnReasonDefinition { id name } id ... on ReturnLineItem { id fulfillmentLineItem { lineItem { id } } } } } }}
                                """


        orders, has_next, end_cursor = self.client.fetch_all_connection_data(
            query_name="GetOrdersWithReturns",
            connection_name="orders",
            fields=fields,
            filters=query_filter,
            first=first,
            use_nodes=True,
            extra_connection_args="sortKey: UPDATED_AT"
        )

        _logger.info(
            "Fetched %s order(s) with returns for range %s -> %s. has_next=%s end_cursor=%s",
            len(orders), updated_at_min, updated_at_max, has_next, end_cursor
        )

        response = []
        for raw_graphql_order in orders:
            rest_order_data = self.convert_order_graphql_to_rest(raw_graphql_order)
            response.append(rest_order_data)
        return response

    @staticmethod
    def _ensure_nodes(data):
        if not data:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            nodes = data.get("nodes")
            if isinstance(nodes, list):
                return nodes
            if nodes:
                return [nodes]
            edges = data.get("edges")
            if isinstance(edges, list):
                return [
                    edge.get("node")
                    for edge in edges
                    if isinstance(edge, dict) and edge.get("node")
                ]
        return [data]

    def _fetch_all_orders_for_fields(self, fields, shopify_query, limit):
        """
        Internal helper to fetch all pages for given fields, returns {order_id: [order_data_dict, ...]}
        """
        orders_by_id = {}
        query_name = "GetOrders"
        # object_name = "shopifyPaymentsAccount"
        connection_name = "orders"
        nodes, has_next, end_cursor = self.client.fetch_all_connection_data(
            query_name=query_name,
            connection_name=connection_name,
            fields=fields,
            filters=shopify_query,
            first=limit,
            extra_connection_args="sortKey: UPDATED_AT",
            use_nodes=True
        )
        for order in nodes:
            order_id = order.get('id')
            if not order_id:
                continue
            if order_id not in orders_by_id:
                orders_by_id[order_id] = []
            orders_by_id[order_id].append(order)
        return orders_by_id

    def deep_merge_dicts(self, dicts):
        """
        Merge a list of dicts into one dict (deep merge).
        """
        def merge(a, b):
            for k, v in b.items():
                if k in a and isinstance(a[k], dict) and isinstance(v, Mapping):
                    merge(a[k], v)
                else:
                    a[k] = v
            return a
        result = {}
        for d in dicts:
            merge(result, d)
        return result

    @staticmethod
    def _extract_id_from_gid(gid: str) -> Optional[int]:
        """Extracts the numerical ID from a Shopify Global ID string."""
        if not isinstance(gid, str):
            return None
        match = re.search(r'\/(\d+)$', gid)
        return int(match.group(1)) if match else None

    # In OrderQueryHelper class
    def _flatten_money_set(self, key: str, value: Dict[str, Any]) -> Tuple[str, str, str]:
        """
        Flattens GraphQL MoneySet objects into the REST key/value pair.
        Conditionally selects shopMoney or presentmentMoney based on self.use_presentment.
        """
        # Determine the REST-style key (snake_case, removing 'Set')
        new_key_rest = self._convert_money_set_key_name(key)
        # Select amount based on presentment preference
        amount_to_use = self._get_money_amount(value)
        set_key = new_key_rest + '_set'
        return new_key_rest, set_key, amount_to_use

    def _convert_money_set_key_name(self, key: str) -> str:
        """Convert MoneySet key to REST format key name."""
        new_key_rest = key.replace('Set', '')
        new_key_rest = re.sub(r'(?<!^)(?=[A-Z])', '_', new_key_rest).lower()
        # Handle specific REST key names
        if new_key_rest in ('totalprice', 'totaldiscounts'):
            new_key_rest = new_key_rest.replace('total', 'total_')
        if new_key_rest in ('original_unit_price', 'original_price'):
            new_key_rest = 'price'
        if new_key_rest == 'allocated_amount':
            new_key_rest = 'amount'

        return new_key_rest

    def _get_money_amount(self, value: Dict[str, Any]) -> str:
        """Extract amount from MoneySet based on presentment preference."""
        if self.use_presentment:
            return value['presentmentMoney']['amount']
        return value['shopMoney']['amount']

    def _get_currency_code(self, value: Dict[str, Any]) -> str:
        """Extract currency code from MoneySet based on presentment preference."""
        if self.use_presentment:
            return value['presentmentMoney']['currencyCode']
        return value['shopMoney']['currencyCode']

    def _convert_money_set_to_snake_case(self, value: Dict[str, Any]) -> Dict[str, Any]:
        """Convert MoneySet nested structure to snake_case."""
        converted_set = {}
        for money_key, money_val in value.items():
            snake_money_key = self._to_snake_case(money_key)
            if isinstance(money_val, dict):
                converted_money = {}
                for inner_key, inner_val in money_val.items():
                    snake_inner_key = self._to_snake_case(inner_key)
                    converted_money[snake_inner_key] = inner_val
                converted_set[snake_money_key] = converted_money
            else:
                converted_set[snake_money_key] = money_val
        return converted_set

    def _should_add_currency_field(self, key: str) -> bool:
        """Check if currency field should be added for this key."""
        currency_fields = {
            'amount', 'total_price', 'subtotal_price', 'price',
            'total_discounts', 'total_shipping_price', 'total_tax',
            'total_outstanding', 'total_tip_received', 'discounted_price'
        }
        return key in currency_fields

    def _get_fields_to_always_convert(self) -> set:
        """Return set of field names that should always be converted to snake_case."""
        return {
            'currencyCode', 'presentmentCurrencyCode', 'countryCodeV2',
            'displayFinancialStatus', 'displayFulfillmentStatus',
            'provinceCode', 'countryCode', 'timeZone', 'validationResultSummary',
            'coordinatesValidated', 'formattedArea', 'clientIp', 'checkoutId',
            'checkoutToken', 'cartToken', 'merchantBusinessEntityId',
            'merchantOfRecordAppId', 'orderNumber', 'orderStatusUrl',
            'sourceIdentifier', 'sourceName', 'sourceUrl', 'statusPageUrl',
            'poNumber', 'contactEmail', 'customerLocale', 'deviceId',
            'estimatedTaxes', 'taxExempt', 'taxesIncluded', 'dutiesIncluded',
            'landingSite', 'landingSiteRef', 'locationId', 'confirmationNumber',
            'multipassIdentifier', 'verifiedEmail', 'defaultAddress',
            'amountV2', 'authorizationCode', 'authorizationExpiresAt',
            'errorCode', 'formattedGateway', 'manualPaymentGateway',
            'manuallyCapturable', 'maximumRefundable', 'multiCapturable',
            'parentTransaction', 'paymentIcon', 'paymentId', 'processedAt',
            'receiptJson', 'settlementCurrency', 'settlementCurrencyRate',
            'totalUnsettled', 'totalUnsettledSet', 'totalQuantity',
            'trackingInfo', 'shippingRateHandle', 'carrierIdentifier',
            'isRemoved', 'discountAllocations', 'requestedFulfillmentServiceId',
            'currentQuantity', 'fulfillableQuantity', 'fulfillmentStatus',
            'isGiftCard', 'requiresShipping', 'taxLines', 'channelLiable',
            'ratePercentage', 'productExists', 'totalDiscount', 'totalDiscountSet',
            'variantInventoryManagement', 'variantTitle', 'giftCard',
            'accountNumber', 'cancelReason', 'cancelledAt', 'closedAt',
            'browserIp', 'buyerAcceptsMarketing', 'clientDetails',
            'discountCodes', 'originalTotalAdditionalFeesSet',
            'originalTotalDutiesSet', 'paymentGatewayNames',
            'referringSite', 'totalWeight', 'updatedAt', 'createdAt',
            'userId', 'billingAddress', 'shippingAddress', 'shippingLines',
            'lineItems', 'emailMarketingConsent', 'smsMarketingConsent',
            'customerJourneySummary', 'lastVisit', 'fulfillmentOrders',
            'deliveredAt', 'estimatedDeliveryAt', 'displayStatus',
            'fulfillmentLineItems', 'originAddress', 'requiresShipping',
            'serviceName', 'trackingSupport', 'legacyResourceId',
            'attributedStaffs', 'trackingCompany', 'trackingNumber',
            'trackingNumbers', 'trackingUrl', 'trackingUrls', 'shipmentStatus'
        }

    def _get_fields_to_exclude(self) -> set:
        """
        Return set of GraphQL-only field names that should be excluded from REST.
        IMPORTANT: Only exclude fields that are truly not needed and would cause issues.
        Keep most extra fields as they might be useful.
        """
        return {
            # These fields are truly GraphQL-only and cause structure issues
            'formatted',  # GraphQL formatting helper, not in REST
            'coordinatesValidated',  # GraphQL validation field, not in REST
        }

    def _to_snake_case(self, key: str) -> str:
        """Convert a camelCase or PascalCase string to snake_case."""
        snake = re.sub(r'(?<!^)(?=[A-Z])', '_', key).lower()
        # Apply specific transformations
        snake = snake.replace('_code_v2', '_code')
        snake = snake.replace('_v2', '')
        snake = snake.replace('client_ip', 'browser_ip')
        snake = snake.replace('legacy_resource_id', 'id')
        # Handle specific field mappings
        if snake == 'display_financial_status':
            return 'financial_status'
        if snake == 'display_fulfillment_status':
            return 'fulfillment_status'
        return snake

    def _should_convert_to_lowercase(self, key: str) -> bool:
        """Check if the value for this key should be converted to lowercase."""
        lowercase_fields = {
            'kind', 'status', 'source_name',
            'financial_status', 'fulfillment_status', 'state'
        }
        return key in lowercase_fields

    def _process_nodes_structure(self, value: list) -> list:
        """Process GraphQL 'nodes' list structure."""
        return [self._convert_graphql_to_rest_fields(item) for item in value]

    def _process_embedded_list_wrapper(self, key: str, value: Dict[str, Any],
                                       new_data: Dict[str, Any]) -> bool:
        """
        Process embedded list wrappers (e.g., lineItems: { nodes: [...] }).
        Returns True if processing was done, False otherwise.
        """
        if isinstance(value, dict) and 'nodes' in value:
            new_key = self._to_snake_case(key)
            new_data[new_key] = self._convert_graphql_to_rest_fields(
                value.get('nodes', [])
            )
            return True
        return False

    def _process_money_set_field(self, key: str, value: Dict[str, Any],
                                 new_data: Dict[str, Any]) -> bool:
        """
        Process MoneySet fields and flatten them to REST format.
        Returns True if processing was done, False otherwise.
        """
        if key.endswith('Set') and isinstance(value, dict) and 'shopMoney' in value:
            new_key_rest, set_key, amount = self._flatten_money_set(key, value)
            new_data[new_key_rest] = amount
            new_data[set_key] = self._convert_money_set_to_snake_case(value)
            # Add currency field if needed
            if self._should_add_currency_field(new_key_rest):
                new_data['currency'] = self._get_currency_code(value)
            return True
        return False

    def _process_gid_field(self, key: str, value: str, new_data: Dict[str, Any]) -> bool:
        """
        Process GraphQL Global ID fields.
        Returns True if processing was done, False otherwise.
        """
        if key == 'id' and isinstance(value, str) and value.startswith('gid://shopify/'):
            numeric_id = self._extract_id_from_gid(value)
            new_data[key] = numeric_id
            new_data['admin_graphql_api_id'] = value
            return True
        return False

    def _process_nested_product_field(self, key: str, value: Dict[str, Any],
                                      new_data: Dict[str, Any]) -> bool:
        """
        Process nested product field.
        Returns True if processing was done, False otherwise.
        """
        if key == 'product' and isinstance(value, dict) and 'id' in value:
            new_data['product_id'] = self._extract_id_from_gid(value['id'])
            return False  # Don't skip further processing
        return False

    def _process_nested_variant_field(self, key: str, value: Dict[str, Any],
                                      new_data: Dict[str, Any], data: Dict[str, Any] = None) -> bool:
        """
        Process nested variant field.
        Returns True if processing was done, False otherwise.
        """
        if key == 'variant' and isinstance(value, dict):
            new_data['variant_id'] = self._extract_id_from_gid(value.get('id'))
            # Prefer the line item's own direct 'sku' field (present as a sibling of
            # 'variant' in the query) since it survives even when Shopify returns
            # variant: null for a deleted/archived product; fall back to variant.sku.
            direct_sku = data.get('sku') if data else None
            new_data['sku'] = direct_sku if direct_sku is not None else value.get('sku')
            new_data['variant_title'] = value.get('title')
            if 'taxable' in value:
                new_data['taxable'] = value.get('taxable')
            return True  # Skip further processing for this field
        return False

    def _process_fulfillment_service_field(self, key: str, value: Dict[str, Any],
                                           new_data: Dict[str, Any]) -> bool:
        """
        Process fulfillment service field.
        Returns True if processing was done, False otherwise.
        """
        if key == 'fulfillment_service' and isinstance(value, dict) and 'service_name' in value:
            new_data['fulfillment_service'] = value.get('service_name')
            return True
        return False

    def _process_special_fields(self, key: str, value: Any, data: Dict[str, Any],
                                new_data: Dict[str, Any]) -> bool:
        """
        Process special fields that need custom handling.
        Returns True if processing was done, False otherwise.
        """
        # Skip originalTotalSet as it's handled by originalUnitPriceSet
        if key == 'originalTotalSet' and isinstance(data, dict):
            return True
        # Handle order name field
        elif key == 'number' and isinstance(data, dict) and 'id' in data:
            new_data['order_number'] = value
        elif key == 'parentTransaction' and isinstance(value, dict) and value:
            parent_gid = value.get('id')
            if parent_gid:
                new_data['parent_id'] = self._extract_id_from_gid(parent_gid)
            # Still process the full parent_transaction object
            new_data['parent_transaction'] = self._convert_graphql_to_rest_fields(value)
            return True
        elif key == 'parentTransaction' and value is None:
            new_data['parent_id'] = None
            new_data['parent_transaction'] = None
            return True
        return False

    def _convert_field_key_and_value(self, key: str, value: Any) -> Tuple[str, Any]:
        """Convert field key to REST format and apply value transformations."""
        always_convert = self._get_fields_to_always_convert()
        new_key = key
        new_value = value
        # Convert to snake_case if needed
        if key in always_convert or key not in ('admin_graphql_api_id',):
            new_key = self._to_snake_case(key)
            # Convert enum-like values to lowercase
            if self._should_convert_to_lowercase(new_key) and isinstance(value, str):
                new_value = value.lower()
        return new_key, new_value

    def _convert_graphql_to_rest_fields(self, data: Any) -> Any:
        """
        Recursively processes the GraphQL response to convert GIDs, flatten money sets,
        handle list nodes, and flatten line item specific nested fields for REST compatibility.
        @author: Gopal Chouhan @Emipro Technologies Pvt. Ltd on date 27/.
        """
        if isinstance(data, dict):
            return self._convert_dict_to_rest(data)
        if isinstance(data, list):
            return self._convert_list_to_rest(data)
        return data

    def _convert_dict_to_rest(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a dictionary from GraphQL to REST format."""
        new_data = {}
        for key, value in data.items():
            # Handle 'nodes' list structure
            if key == 'nodes' and isinstance(value, list):
                return self._process_nodes_structure(value)
            # Handle discountApplications
            if key == 'discountApplications' and isinstance(value, dict):
                new_data['discount_applications'] = self._convert_discount_applications(value.get('nodes', []))
                continue
            # Handle discountApplication (inside discountAllocations) — extract index
            if key == 'discountApplication' and isinstance(value, dict):
                index = value.get('index')
                if index is not None:
                    new_data['discount_application_index'] = index
                continue
            # Handle embedded list wrappers
            if self._process_embedded_list_wrapper(key, value, new_data):
                continue
            # Handle MoneySet fields
            if self._process_money_set_field(key, value, new_data):
                continue
            # Handle GID fields
            if self._process_gid_field(key, value, new_data):
                continue
            # Handle special fields
            if self._process_special_fields(key, value, data, new_data):
                continue
            # Handle nested structures
            self._process_nested_product_field(key, value, new_data)
            if self._process_nested_variant_field(key, value, new_data, data):
                continue
            if self._process_fulfillment_service_field(key, value, new_data):
                continue
            # Generic field conversion
            new_key, new_value = self._convert_field_key_and_value(key, value)
            new_data[new_key] = self._convert_graphql_to_rest_fields(new_value)
        return new_data

    def _convert_list_to_rest(self, data: list) -> list:
        """Convert a list from GraphQL to REST format."""
        return [self._convert_graphql_to_rest_fields(item) for item in data]

    def _convert_discount_applications(self, nodes: list) -> list:
        """
        Convert GraphQL discountApplications nodes to REST discount_applications format.
        """
        result = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            typename = node.get('__typename', '')
            # Determine type
            type_map = {
                'DiscountCodeApplication': 'discount_code',
                'ManualDiscountApplication': 'manual',
                'ScriptDiscountApplication': 'script',
                'AutomaticDiscountApplication': 'automatic',
            }
            app_type = type_map.get(typename, typename.lower().replace('discountapplication', '').replace('application', '') if typename else 'unknown')

            value_obj = node.get('value', {}) or {}
            value_typename = value_obj.get('__typename', '')
            if value_typename == 'PricingPercentageValue':
                value_type = 'percentage'
                value = str(value_obj.get('percentage', '0'))
            elif value_typename == 'MoneyV2':
                value_type = 'fixed_amount'
                value = str(value_obj.get('amount', '0'))
            else:
                value_type = 'unknown'
                value = '0'

            app = {
                'target_type': self._to_snake_case(node.get('targetType', '')).lower() if node.get('targetType') else '',
                'type': app_type,
                'value': value,
                'value_type': value_type,
                'allocation_method': node.get('allocationMethod', '').lower() if node.get('allocationMethod') else '',
                'target_selection': node.get('targetSelection', '').lower() if node.get('targetSelection') else '',
            }
            if 'code' in node:
                app['code'] = node.get('code', '')
            if 'title' in node:
                app['title'] = node.get('title', '')
            result.append(app)
        return result

    def convert_order_graphql_to_rest(self, graphql_order_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Public method to convert a single GraphQL order to REST format.
        This is the recommended entry point for order conversion.
        Args:
            graphql_order_data: GraphQL order data (after merging if needed)
        Returns:
            REST-formatted order data with proper field names and structure
        """
        rest_order_data = self._convert_graphql_to_rest_fields(graphql_order_data)
        customer_data = rest_order_data.get('customer')
        if isinstance(customer_data, dict):
            if customer_data.get('default_address') is None:
                customer_data.pop('default_address', None)
            if isinstance(customer_data.get('tags'), list):
                customer_data['tags'] = ", ".join(customer_data.get('tags'))
        if isinstance(rest_order_data.get('tags'), list):
            rest_order_data['tags'] = ", ".join(rest_order_data.get('tags'))
        rest_order_data['order_api_name'] = 'fetched_via_graphql'
        if 'transactions' in rest_order_data:
            rest_order_data['transaction'] = rest_order_data.pop('transactions')
        if rest_order_data.get('presentment_currency_code'):
            rest_order_data['presentment_currency'] = rest_order_data.get('presentment_currency_code')
        return rest_order_data


