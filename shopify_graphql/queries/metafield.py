import logging


_logger = logging.getLogger("Graphql Metafield")


class MetafieldQueryHelper:
    """
    Shopify GraphQL Metafield Helper
    Handles:
        - Fetch Metafield Definitions
        - Fetch Metafield Values
        - Create / Update Metafields
    """

    def __init__(self, client, **kwargs):
        self.client = client
        self.setting = kwargs

    # =========================================================
    # 1️⃣ Fetch Metafield Definitions by Owner Type
    # =========================================================

    METAFIELD_DEFINITION_QUERY = """
    query getMetafieldDefinitions($ownerType: MetafieldOwnerType!, $cursor: String) {
      metafieldDefinitions(first: 250, ownerType: $ownerType, after: $cursor) {
        edges {
          node {
            id
            name
            namespace
            key
            description
            type {
              name
            }
            ownerType
            validations {
                name
                type
                value
            }
            validationStatus
            metafieldsCount
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
    """

    def fetch_metafield_definitions(self, owner_type):
        """
        Fetch all metafield definitions for given owner type
        """
        definitions = []
        cursor = None

        while True:
            variables = {
                "ownerType": owner_type,
                "cursor": cursor
            }

            response = self.client.execute(self.METAFIELD_DEFINITION_QUERY, variables)

            if not response or "errors" in response:
                _logger.error("Error fetching metafield definitions: %s", response)
                break

            data = response.get("data", {}).get("metafieldDefinitions", {})
            edges = data.get("edges", [])

            for edge in edges:
                definitions.append(edge.get("node"))

            if not data.get("pageInfo", {}).get("hasNextPage"):
                break

            cursor = data.get("pageInfo", {}).get("endCursor")

        _logger.info("Fetched %s metafield definitions for %s", len(definitions), owner_type)
        return definitions

    # =========================================================
    # 2️⃣ Fetch Metafield Definition by Namespace & Key
    # =========================================================

    METAFIELD_DEFINITION_BY_KEY_QUERY = """
        query getMetafieldDefinition(
            $ownerType: MetafieldOwnerType!,
            $namespace: String!,
            $key: String!
        ) {
            metafieldDefinitions(
              first: 10,
              ownerType: $ownerType,
              namespace: $namespace,
              key: $key
            ) {
              edges {
                node {
                  id
                  name
                  namespace
                  key
                  description
                  type {
                    name
                  }
                  ownerType
                  validations {
                      name
                      type
                      value
                  }
                  validationStatus
                  metafieldsCount
                }        
              }
          }
        }
        """


    def get_definition_by_key(self, owner_type, key, namespace="custom"):
        """
          Public method to fetch metafield definition.
        """
        definitions = []
        variables = {
            "ownerType": owner_type,
            "namespace": namespace,
            "key": key,
        }

        response = self.client.execute(self.METAFIELD_DEFINITION_BY_KEY_QUERY, variables=variables)        

        if not response:
            _logger.error("Empty response from Shopify.")
            # raise UserError("No response received from Shopify.")

        if response.get("errors"):
            _logger.error("Definition Fetch Error: %s", response["errors"])
            return False

        data = response.get("data", {}).get("metafieldDefinitions", {})
        edges = data.get("edges", [])

        for edge in edges:
            definitions.append(edge.get("node"))

        return definitions or False 

    # =========================================================
    # 3️⃣ Update Metafields values by GraphQL Mutation
    # =========================================================
    def set_metafields(self, payload):
      """
        Execute Shopify metafieldsSet mutation.

        :param metafields: either a GraphQL formatted string OR
                          list of dicts with keys:
                          owner_id, namespace, key, type, value
                          Example:
                          [
                              {
                                  'owner_id': '123456789',
                                  'namespace': 'custom',
                                  'key': 'material',
                                  'type': 'single_line_text_field',
                                  'value': 'Steel'
                              }
                          ]
        :return: parsed JSON response
      """     
      normalized_metafields = []
      for metafield in payload:
          if not isinstance(metafield, dict):
              continue                  
          normalized_metafields.append({
              "ownerId": metafield.get("ownerId"),
              "namespace": metafield.get("namespace"),
              "key": metafield.get("key"),
              "type": metafield.get("type"),
              "value": "" if metafield.get("value") is None else str(metafield.get("value")),
          })

      mutation = """
      mutation MetafieldsSet($metafields: [MetafieldsSetInput!]!) {
        metafieldsSet(metafields: $metafields) {
          metafields {
            id
            namespace
            key
          }
          userErrors {
            field
            message
            code
          }
        }
      }
      """

      return self.client.execute(mutation, {"metafields": normalized_metafields})

    
    
    # =========================================================
    # 4️⃣ Fetch Metafields by Owner ID
    # =========================================================
    METAFIELDS_BY_OWNER_QUERY = """
    query getMetafields($ownerId: ID!) {
      node(id: $ownerId) {
      ... on HasMetafields {
        metafields(first: 100) {
        edges {
          node {
          id
          namespace
          key
          value
          type 
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
        }
      }
      }
    }
    """

    def get_metafields_by_owner(self, owner_id, namespace=None,is_import_product=False):
      """
      Fetch metafields for a given owner ID and optional namespace.
      """
      metafields = []
      cursor = None

      while True:
        variables = {
          "ownerId": owner_id,
        }
        response = self.client.execute(self.METAFIELDS_BY_OWNER_QUERY, variables)
        if not response or "errors" in response:
          _logger.error("Error fetching metafields: %s", response)
          break
        node = response.get("data", {}).get("node", {})
        metafields_data = node.get("metafields", {})
        if is_import_product:
           return metafields_data        
        
        edges = metafields_data.get("edges", [])

        for edge in edges:
          metafields.append(edge.get("node"))

        page_info = metafields_data.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
          break
        cursor = page_info.get("endCursor")
        # Update variables for pagination
        variables["cursor"] = cursor

      _logger.info("Fetched %s metafields for owner %s", len(metafields), owner_id)
      return metafields
      