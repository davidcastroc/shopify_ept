from urllib.parse import urlparse, parse_qs
import time
import requests
import base64
import mimetypes
import collections
import logging

_logger = logging.getLogger("Graphql Product")


class ProductQueryHelper:

    def __init__(self, client, **kwargs):
        self.client = client
        self.setting = kwargs

    VARIANT_DEFAULT_FIELDS = """
            id title price position compareAtPrice createdAt updatedAt taxable barcode sku
            inventoryPolicy
            selectedOptions { name value }
            inventoryItem { measurement { weight { unit value } } requiresShipping tracked legacyResourceId harmonizedSystemCode }
            image { id src }
    """

    DEFAULT_FIELDS = f"""       
        id title descriptionHtml vendor createdAt handle
        updatedAt publishedAt templateSuffix tags status
        variants(first: 250) {{
          edges {{
            node {{
              {VARIANT_DEFAULT_FIELDS}
            }}
          }}
          pageInfo {{ endCursor hasNextPage }}
        }}
        options {{ values position name id }}         
        media(first: 100) {{ 
          edges {{ 
              node {{ 
                  ... on MediaImage {{ id image {{ url }} }}
              }}
          }}
        }}
    """

    @staticmethod
    def build_date_filter(status, import_based_on, from_date, to_date):
        filter_parts = []
        if status:
            filter_parts.append(f"status:{status}")
        if import_based_on == "create_date" and from_date and to_date:
            filter_parts.append(f"created_at:>='{from_date}'")
            filter_parts.append(f"created_at:<='{to_date}'")
        elif import_based_on == "update_date" and from_date and to_date:
            filter_parts.append(f"updated_at:>='{from_date}'")
            filter_parts.append(f"updated_at:<='{to_date}'")
        return " ".join(filter_parts)

    @staticmethod
    def convert_graphql_image_ept(media_node, product_id, position):
        """Convert GraphQL media → REST-like image dict."""
        media_id = media_node.get("id")
        if not media_id:
            return False
        image_id = ProductQueryHelper.gid_to_int_ept(media_id)
        img = media_node.get("image", {})

        if not img.get("url"):
            return False

        return {
            "id": image_id,
            "position": position,
            "product_id": product_id,
            "admin_graphql_api_id": media_node.get("id"),
            "src": img.get("url"),
            "variant_ids": [],  # Fill later when mapping variant images
        }

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
    def get_v_value(url):
        params = parse_qs(urlparse(url).query)
        return params.get("v", [None])[0]

    @staticmethod
    def convert_graphql_variant_ept(node, product_id):
        """Convert GraphQL variant → REST-like variant dict."""
        variant_id = ProductQueryHelper.gid_to_int_ept(node["id"])
        inv_item = node.get("inventoryItem", {})
        # weight unit normalization
        weight_info = inv_item.get("measurement", {}).get("weight", {})
        weight_value = weight_info.get("value")
        weight_unit = weight_info.get("unit", "").lower()  # "KILOGRAMS" → "kilograms"
        
        metafield_edges = node.get("metafields", {}).get("edges", [])
        metafields = []
        for edge in metafield_edges:
            metafield_node = edge.get("node", {})
            # Get odoo configured metafields for product and check if the metafield from Shopify matches with any of the configured metafield
            # If matches then add the metafield value in rest_product dict with key as namespace.key
            # Example: If namespace is "odoo" and key is "color" then the metafield value will be added in rest_product dict with key "odoo.color"
            metafields.append({
                "namespace": metafield_node.get("namespace"),
                "key": metafield_node.get("key"),
                "value": metafield_node.get("value"),
            })

        return {
            "id": variant_id,
            "product_id": product_id,
            "title": node["title"],
            "price": node.get("price"),
            "position": node.get("position"),
            "inventory_policy": node.get("inventoryPolicy", "").lower(),  # DENY → deny
            "compare_at_price": node.get("compareAtPrice"),
            "created_at": node.get("createdAt"),
            "updated_at": node.get("updatedAt"),
            "taxable": node.get("taxable"),
            "barcode": node.get("barcode"),
            "sku": node.get("sku"),
            "fulfillment_service": "manual",
            "grams": int(weight_value * 1000) if weight_value else 0,
            "inventory_management": "shopify" if inv_item.get("tracked") else None,
            "requires_shipping": inv_item.get("requiresShipping"),
            "weight": weight_value,
            "weight_unit": "kg" if weight_unit.startswith("kilogram") else weight_unit,
            "inventory_item_id": ProductQueryHelper.gid_to_int_ept(inv_item.get("legacyResourceId")),
            "inventory_quantity": None,  # You must fetch Locations/InventoryLevels using GraphQL separately
            "old_inventory_quantity": None,
            "admin_graphql_api_id": node.get("id"),
            "harmonizedSystemCode": inv_item.get("harmonizedSystemCode"),
            "option1": node["selectedOptions"][0]["value"] if node.get("selectedOptions") and len(
                node["selectedOptions"]) > 0 else None,
            "option2": node["selectedOptions"][1]["value"] if node.get("selectedOptions") and len(
                node["selectedOptions"]) > 1 else None,
            "option3": node["selectedOptions"][2]["value"] if node.get("selectedOptions") and len(
                node["selectedOptions"]) > 2 else None,
            "image_id": ProductQueryHelper.gid_to_int_ept(node["image"]["id"]) if node.get("image") else None,
            "metafields":metafields
        }

    def get_product(self, product_ids=[], **kwargs):
        """
        Fetch products by a list of product IDs. Returns a list of product dicts.
        """
        if not product_ids:
            return []
        product_data = []
        convert_to_rest = kwargs.get('convert_to_rest', False)
        query_name = "GetProduct"
        fields = self.DEFAULT_FIELDS
        # Ensure metafields are included in variant fields if requested
        fetch_metafields = kwargs.get('fetch_metafields', False)        
        if fetch_metafields:
            # Add metafields to variant fields if not present
            metafields = "metafields(first: 150) {edges { node {id namespace key value type} } }"
            if "metafields" not in self.VARIANT_DEFAULT_FIELDS:
                fields = fields.replace(
                    "image { id src }",
                    f"image {{ id src }} {metafields}"
                )
            # Add metafields to product fields if not present
            # product_metafields = "metafields(first: 50) {edges { node {id namespace key value type} } }"
            fields += f"\n{metafields}"

        for pid in product_ids:
            if not isinstance(pid, (int, str)):
                raise ValueError(f"Invalid product ID: {pid}. Must be int or str.")
            all_data, has_next, end_cursor = self.client.fetch_all_connection_data(
                query_name=query_name,
                object_name="product",
                object_id=f"gid://shopify/Product/{pid}",
                fields=fields)
            product_data.extend(all_data)
        self._get_all_variants(product_data, fetch_metafields=fetch_metafields)
        if convert_to_rest:
            product_data = self.convert_response_graphql_to_rest_ept(product_data)
        if kwargs.get('gql_object_form', False):
            rest_node_obj_list = [self.client.graphql_object(p) for p in product_data]
            product_data = rest_node_obj_list

        return product_data

    def get_products(self, first=250, **kwargs):
        """
        Execute the products query with the given parameters using the helper's client.
        Returns the parsed GraphQL response dict.
        """
        query_filter = kwargs.get('query_filter', None)
        query_name = "GetProducts"
        connection_name = "products"
        fields = kwargs.get('fields', self.DEFAULT_FIELDS)
        fetch_metafields = kwargs.get('fetch_metafields', False)
        no_pagination = kwargs.get('no_pagination', False)
        convert_to_rest = kwargs.get('convert_to_rest', False)
        all_data, has_next, end_cursor = self.client.fetch_all_connection_data(
            query_name=query_name,
            connection_name=connection_name,
            fields=fields,
            filters=query_filter,
            first=first,
            use_nodes=True,
            no_pagination=no_pagination,
            extra_connection_args=kwargs.get('extra_connection_args')
        )
        self._get_all_variants(all_data, fetch_metafields=fetch_metafields)
        if fetch_metafields:
            all_data = self._get_all_product_metafields(all_data, no_pagination=no_pagination,
             query_filter=query_filter, first=first,
              extra_connection_args=kwargs.get('extra_connection_args'))

        if convert_to_rest:
            all_data = self.convert_response_graphql_to_rest_ept(all_data)
        if no_pagination:
            return all_data, has_next, end_cursor
        return all_data

    def _get_all_variants(self, products, fetch_metafields=False):
        """
        For each product in the list, fetch all variants if there are more than 250 variants.
        Modifies the products list in place to ensure all variants are included.
        """
        query_name = "GetProductVariants"
        connection_name = "variants"
        fields = self.VARIANT_DEFAULT_FIELDS
        # Ensure metafields are included in variant fields if requested
        if fetch_metafields:
            metafields = "metafields(first: 100) {edges { node {id namespace key value type} } }"
            if "metafields" not in self.VARIANT_DEFAULT_FIELDS:
                fields += f"\n{metafields}"
        for product in products:
            variants_conn = product.get('variants', {})
            all_variants = [edge.get('node', {}) for edge in variants_conn.get('edges', [])]
            page_info = variants_conn.get('pageInfo', {})
            if page_info.get('hasNextPage'):
                after_cursor = page_info.get('endCursor')
                extra_connection_args = f'after:"{after_cursor}"'
                product_gid = product.get('id')
                all_data, has_next, end_cursor = self.client.fetch_all_connection_data(
                    query_name=query_name,
                    connection_name=connection_name,
                    object_name="product",
                    object_id=product_gid,
                    fields=fields,
                    extra_connection_args=extra_connection_args)
                all_variants.extend(all_data)
            product['variants'] = all_variants

    def _get_all_product_metafields(self, products,no_pagination,query_filter,first,extra_connection_args):
        """Fetch all metafields for each product and its variants if there are more than 50 metafields."""
        query_name = "GetProducts"
        connection_name = "products"
        fields = {"product": """
                id metafields(first: 150) {edges { node {id namespace key value type} } }""",

            }

        for product in fields.keys():
            all_data, has_next, end_cursor = self.client.fetch_all_connection_data(
                query_name=query_name,
                connection_name=connection_name,
                fields=fields.get(product),
                filters=query_filter,
                first=first,
                use_nodes=True,
                no_pagination=no_pagination,
                extra_connection_args=extra_connection_args
                )            
            if product == "product":
                metafield_map = {m['id']: m.get('metafields', {}) for m in all_data}

                for p in products:
                    p['metafields'] = metafield_map.get(p['id'], [])

            # if product == "variants":
            #     variant_map = {
            #             v['id']: v.get('metafields', {})
            #             for p in all_data
            #             for v in p.get('variants', {}).get('nodes', [])
            #         }
            #
            #     for p in products:
            #         for v in p.get('variants', {}):
            #             v['metafields'] = variant_map.get(v['id'], [])

        variant_ids = [v['id'] for p in products for v in p.get('variants', {})]
        variables = {
            "ids": variant_ids
        }
        metafield_data= self.client.execute(self.VARIANT_METAFIELD_QUERY, variables)
        if metafield_data:
            data =metafield_data.get("data") or {}
            variant_map = {
                field['id']: field.get('metafields', {})
                for field in data.get('nodes', [])

            }
            for product in products:
                for v in product.get('variants', {}):
                    v['metafields'] = variant_map.get(v['id'], {})
        return products

    VARIANT_METAFIELD_QUERY = """
        query GetVariantMetafields($ids: [ID!]!) {
          nodes(ids: $ids) {
            ... on ProductVariant {
              id
              metafields(first: 150) {
                edges {
                  node {
                    id
                    namespace
                    key
                    value
                    type
                  }
                }
              }
            }
          }
        }
        """

    def convert_response_graphql_to_rest_ept(self, graphql_response):
        """Main function to convert GraphQL product → REST-like dict."""
        product_res_list = []
        for product_res in graphql_response:
            product_data = self.convert_single_response_graphql_to_rest_ept(product_res)
            product_res_list.append(product_data)
        return product_res_list

    def convert_single_response_graphql_to_rest_ept(self, product_res):
        """Main function to convert GraphQL product → REST-like dict."""
        product_id = ProductQueryHelper.gid_to_int_ept(product_res["id"])

        rest_product = {
            "id": product_id,
            "title": product_res.get("title"),
            "body_html": product_res.get("descriptionHtml"),
            "vendor": product_res.get("vendor"),
            "product_type": "",
            "created_at": product_res.get("createdAt"),
            "handle": product_res.get("handle"),
            "updated_at": product_res.get("updatedAt"),
            "published_at": product_res.get("publishedAt"),
            "template_suffix": product_res.get("templateSuffix"),
            "published_scope": "global",
            "tags": ", ".join(product_res.get("tags", [])),
            "status": product_res.get("status", "").lower(),
            "admin_graphql_api_id": product_res.get("id"),
            "variants": [],
            "options": [],
            "metafields": [],
            "images": [],
            "image": None,
        }
        for opt in product_res.get("options", []):
            rest_product["options"].append({
                "id": ProductQueryHelper.gid_to_int_ept(opt["id"]),
                "product_id": product_id,
                "name": opt["name"],
                "position": opt["position"],
                "values": opt.get("values", []),
            })
        variants = product_res.get("variants", {})
        image_variant_map = {}  # {url_path: [variant_int_id, ...]}
        for variant in variants:
            variant_image = variant.get("image") or {}
            src = variant_image.get("src")
            if src:
                variant_int_id = ProductQueryHelper.gid_to_int_ept(variant["id"])
                url_path = urlparse(src).path  # strip ?v=… query param
                image_variant_map.setdefault(url_path, []).append(variant_int_id)
            rest_product["variants"].append(
                ProductQueryHelper.convert_graphql_variant_ept(variant, product_id)
            )
            
        media_edges = product_res.get("media", {}).get("edges", [])
        images = []
        for pos, m in enumerate(media_edges, start=1):
            img_dict = ProductQueryHelper.convert_graphql_image_ept(m["node"], product_id, pos)
            if not img_dict:
                continue
            url_path = urlparse(img_dict.get("src", "")).path
            if url_path in image_variant_map:
                img_dict["variant_ids"] = list(image_variant_map[url_path])
            images.append(img_dict)

        rest_product["images"] = images
        rest_product["image"] = images[0] if images else None

        
        metafield_edges = product_res.get("metafields", {}).get("edges", [])
        for edge in metafield_edges:
            node = edge.get("node", {})
            # Get odoo configured metafields for product and check if the metafield from Shopify matches with any of the configured metafield
            # If matches then add the metafield value in rest_product dict with key as namespace.key
            # Example: If namespace is "odoo" and key is "color" then the metafield value will be added in rest_product dict with key "odoo.color"
            rest_product["metafields"].append({
                "namespace": node.get("namespace"),
                "key": node.get("key"),
                "value": node.get("value"),
            })


        return rest_product

    PRODUCT_SET_MUTATION = """
        mutation productSet($input: ProductSetInput!) {
          productSet(input: $input) {
            product { id
                      title
                      variants(first: 250) {
                            nodes {
                            id
                            sku
                            barcode
                            inventoryItem { id }
                            }
                               }
                    }
            userErrors { field message code }
          }
        }
        """

    PUBLICATIONS_QUERY = """
    {
      publications(first: 10) {
        edges {
          node {
            id
            name
          }
        }
      }
    }
    """

    PUBLISH_MUTATION = """
    mutation pub($id: ID!, $inputs: [PublicationInput!]!) {
          publishablePublish(
        id: $id
        input: $inputs
      ) {
        userErrors { field message }
      }
    }
        """

    UNPUBLISH_MUTATION = """
    mutation pub($id: ID!, $inputs: [PublicationInput!]!) {
      publishableUnpublish(
        id: $id
        input: $inputs
      ) {
        userErrors { field message }
      }
    }
    """

    def set_product(self, input_dict):
        """
        Execute the productSet mutation with the given input dict using the helper's client.
        Returns the parsed GraphQL response dict.
        """
        variables = {"input": input_dict}
        return self.client.execute(self.PRODUCT_SET_MUTATION, variables)

    def publish_product(self, product_gid: str, publication_gid: str):
        # Some Shopify API versions don't expose a named input type for the
        # publish mutation when using variables. Use an inline mutation with
        # the IDs embedded as literals to avoid the variable type validation
        # error (variableRequiresValidType).
        mutation = f"""
        mutation {{
          publishablePublish(
            id: "{product_gid}"
            input: {{publicationId: "{publication_gid}"}}
          ) {{
            userErrors {{ field message }}
          }}
        }}
        """
        return self.client.execute(mutation)

    def unpublish_product(self, product_gid: str, publication_gid: str):
        mutation = f"""
        mutation {{
          publishableUnpublish(
            id: "{product_gid}"
            input: {{publicationId: "{publication_gid}"}}
          ) {{
            userErrors {{ field message }}
          }}
        }}
        """
        return self.client.execute(mutation)

    @staticmethod
    def build_product_input_from_template(template, instance, is_set_price, is_set_basic_detail):
        """
        Build ProductSetInput dict from a shopify template record.
        This mirrors the previous `_prepare_graphql_product_create_input` logic.
        """
        input_data = {
            "title": template.with_context(lang=instance.shopify_lang_id.code).name,
            "status": "ACTIVE",
            "tags": ",".join([tag.name for tag in template.tag_ids]),
            "productType": template.shopify_product_category.name if template.shopify_product_category else "",
            "vendor": template.product_tmpl_id.seller_ids[
                :1].display_name if template.product_tmpl_id.seller_ids else "",
            "descriptionHtml": template.with_context(lang=instance.shopify_lang_id.code).description or "",
            "productOptions": [],
            "variants": [],
        }

        option_map = {}
        for variant in template.shopify_product_ids:
            for ptav in variant.product_id.product_template_attribute_value_ids:
                option_map.setdefault(ptav.attribute_id.name, set()).add(ptav.name)

        if option_map:
            for opt_name, values in option_map.items():
                input_data["productOptions"].append({
                    "name": opt_name,
                    "values": [{"name": v} for v in values]
                })
        else:
            input_data["productOptions"] = [{
                "name": "Title",
                "values": [{"name": "Default Title"}]
            }]

        for variant in template.shopify_product_ids:
            # compute basic details similar to model's helper
            variant_vals = {
                "sku": variant.default_code,
                "barcode": variant.product_id.barcode or "",
                "taxable": "true" if variant.taxable else "false",
            }

            price_val = 0
            compare_price_val = 0
            if is_set_price:
                price_val = str(float(instance.shopify_pricelist_id._get_product_price(
                    variant.product_id, 1.0, partner=False, uom_id=variant.product_id.uom_id.id
                )))
                if instance.shopify_compare_pricelist_id:
                    compare_price_val = str(float(instance.shopify_compare_pricelist_id._get_product_price(
                        variant.product_id, 1.0, partner=False, uom_id=variant.product_id.uom_id.id
                    )))

            variant_input = {
                "sku": variant_vals["sku"],
                "barcode": variant_vals.get("barcode"),
                "price": price_val,
                "compareAtPrice": compare_price_val,
                "taxable": variant_vals.get("taxable") == "true",
                "inventoryPolicy": "CONTINUE" if variant.check_product_stock == "continue" else "DENY",
                "optionValues": []
            }
            if option_map:
                # Standard: Map existing attributes
                for ptav in variant.product_id.product_template_attribute_value_ids:
                    variant_input["optionValues"].append({
                        "name": ptav.name,
                        "optionName": ptav.attribute_id.name,
                    })
            else:
                # Simple product variant must point to "Default Title"
                variant_input["optionValues"] = [{
                    "name": "Default Title",
                    "optionName": "Title"
                }]
            variant_input["inventoryItem"] = {
                "tracked": True if variant.inventory_management == "shopify" else False
            }
            input_data["variants"].append(variant_input)
        return input_data

    @staticmethod
    def convert_graphql_product_to_rest_dict(gql_product):
        """
        Convert GraphQL product representation into a REST-like dict with ids and variants.
        """

        def gid_to_int(gid):
            if not gid:
                return None
            try:
                return int(gid.split('/')[-1])
            except Exception:
                return gid

        result = {
            "id": gid_to_int(gql_product.get("id")),
            "variants": []
        }
        gql_variants = gql_product.get("variants", {})
        if isinstance(gql_variants, dict) and "nodes" in gql_variants:
            gql_variants = gql_variants["nodes"]
        for node in gql_variants:
            inventory_id = None
            if node.get("inventoryItem"):
                inventory_id = gid_to_int(node["inventoryItem"].get("id"))

            result["variants"].append({
                "id": gid_to_int(node.get("id")),
                "sku": node.get("sku"),
                "barcode": node.get("barcode"),
                "inventory_item_id": inventory_id,
            })

        return result

    PRODUCT_MEDIA_COUNT_QUERY = """
    query GetProductMediaCount($id: ID!) {
      product(id: $id) {
        media(first: 250) {
          edges {
            node {
              ... on MediaImage { id }
            }
          }
        }
      }
    }
    """

    def get_product_existing_media_ids(self, product_id):
        """Return the set of all media GIDs already on the product before any new upload."""
        resp = self.client.execute(self.PRODUCT_MEDIA_COUNT_QUERY, {"id": product_id})
        edges = (
            resp.get("data", {}).get("product", {}).get("media", {}).get("edges", [])
        )
        return {edge["node"]["id"] for edge in edges if edge.get("node", {}).get("id")}

    def create_product_media_gql(self, product_id, resource_url):
        query = """
        mutation productCreateMedia($media: [CreateMediaInput!]!, $productId: ID!) {
          productCreateMedia(media: $media, productId: $productId) {
            media { id status }
            userErrors { field message }
          }
        } """
        variables = {
            "productId": product_id,
            "media": [{"mediaContentType": "IMAGE", "originalSource": resource_url}]
        }
        return self.client.execute(query, variables)

    def create_product_media_bulk_gql(self, product_id, media_inputs):
        """
        Attach multiple media to a product in a single API call using productUpdate.
        media_inputs: list of CreateMediaInput dicts (originalSource, alt, mediaContentType).
        Returns the full productUpdate response including all media on the product.
        """
        query = """
        mutation UpdateProductWithNewMedia($product: ProductUpdateInput!, $media: [CreateMediaInput!]) {
          productUpdate(product: $product, media: $media) {
            product {
              id
              media(first: 250) {
                edges {
                  node {
                    ... on MediaImage {
                      id
                    }
                  }
                }
              }
            }
            userErrors { field message }
          }
        }
        """
        variables = {
            "product": {"id": product_id},
            "media": media_inputs,
        }
        return self.client.execute(query, variables)

    def append_media_to_variant_bulk(self, product_id, variant_media_map):
        """
        Attach multiple media to multiple variants in a single API call using ProductVariantSetMediaBulk mutation.
        variant_media_map: dict of {variant_gid: [media_id, ...], ...}
        NOTE: productVariantsBulkUpdate accepts a single `mediaId` per variant.
              This implementation takes the first media_id from the list for each variant.
        """
        mutation = """
        mutation ProductVariantSetMediaBulk(
          $productId: ID!
          $variants: [ProductVariantsBulkInput!]!
        ) {
          productVariantsBulkUpdate(
            productId: $productId
            variants: $variants
          ) {
            product {
              id
            }
            productVariants { id title
              media(first: 5) {
                nodes { id alt mediaContentType }
              }
            }
            userErrors { field message }
          }
        }
        """

        # Build the variants payload: one ProductVariantsBulkInput per variant
        variants_payload = []
        for variant_gid, media_ids in variant_media_map.items():
            if not media_ids:
                continue

            # Take the first media_id for this variant
            media_id = media_ids[0]
            variants_payload.append({
                "id": variant_gid,
                "mediaId": media_id,
            })

        response = None
        batch_size = 100

        for i in range(0, len(variants_payload), batch_size):
            batch = variants_payload[i:i + batch_size]
            variables = {
                "productId": product_id,
                "variants": batch,
            }
            response = self.client.execute(mutation, variables)

        return response
        # variables = {
        #     'productId': product_id,
        #     'variantMedia': variant_media
        # }
        # return self.client.execute(mutation, variables)

    def get_staged_upload_params_bulk(self, files):
        """
        Call stagedUploadsCreate with a list of file dicts, as required by Shopify's API for batch image upload.
        Each dict in files should have: filename, mimeType, fileSize, resource (usually 'PRODUCT_IMAGE'), etc.
        """
        query = """
        mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
          stagedUploadsCreate(input: $input) {
            stagedTargets { url resourceUrl parameters { name value } }
            userErrors { field message }
          }
        }
        """
        # Ensure all file dicts have required fields and correct resource type
        staged_inputs = []
        for f in files:
            staged_inputs.append({
                "resource": f.get("resource", "PRODUCT_IMAGE"),
                "filename": f["filename"],
                "mimeType": f["mimeType"],
                "httpMethod": "POST",
                "fileSize": str(f["fileSize"])
            })
        variables = {"input": staged_inputs}
        return self.client.execute(query, variables)

    def _get_online_store_publication_id(self):
        resp = self.client.execute(ProductQueryHelper.PUBLICATIONS_QUERY)
        pubs = resp["data"]["publications"]["edges"]
        pub_ids = {}
        for pub in pubs:
            name = pub["node"]["name"]
            pub_ids[name] = pub["node"]["id"]
        return pub_ids

    def _prepare_graphql_productset_input(self, template, instance, is_set_price, is_set_basic_detail):
        """
        Build ProductSetInput payload for Shopify GraphQL update/export.
        Rectified for clarity, efficiency, and Shopify GraphQL requirements.
        """
        shopify_product_obj = template.env['shopify.product.product.ept']
        input_data = {"id": f"gid://shopify/Product/{template.shopify_tmpl_id}",
                      "status": "ACTIVE",
                      "productOptions": [],
                      "variants": []
                      }
        # Prepare product options (attributes)
        option_map = {}
        for variant in template.shopify_product_ids:
            for ptav in variant.product_id.product_template_attribute_value_ids:
                option_map.setdefault(ptav.attribute_id.name, set()).add(ptav.name)
        if not option_map:
            # Fallback for simple products: Use Product Name as the option value
            # Using "Title" as the option name is a Shopify standard that keeps the UI clean
            input_data["productOptions"] = [{
                "name": "Title",
                "values": [{"name": "Default Title"}],
                "position": 1
            }]
        else:
            for idx, (attr_name, values) in enumerate(sorted(option_map.items()), start=1):
                input_data["productOptions"].append({"name": attr_name,
                                                     "values": [{"name": v} for v in sorted(values)],
                                                     "position": idx})
        
        # Set product-level details if required
        if is_set_basic_detail:
            input_data.update({"title": template.with_context(lang=instance.shopify_lang_id.code).name,
                               "descriptionHtml": template.with_context(
                                   lang=instance.shopify_lang_id.code).description or "",
                               "vendor": template.product_tmpl_id.seller_ids[
                                   :1].display_name if template.product_tmpl_id.seller_ids else "",
                               "productType": template.shopify_product_category.name if template.shopify_product_category else "",
                               "tags": ",".join([tag.name for tag in template.tag_ids])})
            
        # Prepare variants
        if is_set_basic_detail or is_set_price:
            for variant in template.shopify_product_ids:
                variant_vals = shopify_product_obj.shopify_prepare_variant_vals(instance, variant, is_set_price,
                                                                                is_set_basic_detail)
                # Initialize the variant dictionary
                v = {
                    "inventoryPolicy": "CONTINUE" if variant.check_product_stock == "continue" else "DENY",
                }
                if variant.variant_id and variant.variant_id not in [0, '0', False]:
                    v["id"] = f"gid://shopify/ProductVariant/{variant.variant_id}"
                if is_set_price:
                    v["price"] = str(float(variant_vals.get("price", 0)))
                    if instance.shopify_compare_pricelist_id:
                        v["compareAtPrice"] = str(float(variant_vals.get("compare_at_price", 0)))
                if is_set_basic_detail:
                    v.update({
                        "sku": variant_vals.get("sku"),
                        "barcode": variant_vals.get("barcode"),
                        "taxable": variant_vals.get("taxable") == "true"})
                # Always include optionValues for GraphQL
                ptavs = variant.product_id.product_template_attribute_value_ids
                if not ptavs:
                    # Simple product variant points to the fallback "Title" option
                    v["optionValues"] = [{"name": "Default Title", "optionName": "Title"}]
                else:
                    # Multi-variant product points to specific attributes
                    v["optionValues"] = [{"name": ptav.name, "optionName": ptav.attribute_id.name} for ptav in ptavs]
                input_data["variants"].append(v)
        return input_data

    def _publish_graphql(self, pub_id_dict, template, is_publish, product_global_id=None):
        """
        Publishes or unpublishes a Shopify product via GraphQL API.
        Calls client.execute only once per logical operation, preparing variables as needed.
        """
        product_gid = product_global_id or f"gid://shopify/Product/{template.shopify_tmpl_id}"
        mutation = None
        variables = None
        if is_publish == "unpublish_product":
            inputs = [
                {"publicationId": pub_id}
                for pub_name, pub_id in pub_id_dict.items()
                if pub_name in ("Point of Sale", "Online Store") and pub_id
            ]
            if inputs:
                mutation = self.UNPUBLISH_MUTATION
                variables = {"id": product_gid, "inputs": inputs}
        # Publish to all known publications
        elif is_publish == "publish_product_global":
            inputs = [
                {"publicationId": pub_id}
                for pub_name, pub_id in pub_id_dict.items()
                if pub_name in ("Online Store", "Point of Sale") and pub_id
            ]
            if inputs:
                mutation = self.PUBLISH_MUTATION
                variables = {"id": product_gid, "inputs": inputs}
        # Default: publish to Online Store, unpublish from Point of Sale
        else:
            pos_pub_id = pub_id_dict.get("Point of Sale")
            online_pub_id = pub_id_dict.get("Online Store")
            # Unpublish from POS if present
            if pos_pub_id:
                unpublish_mutation = ProductQueryHelper.UNPUBLISH_MUTATION
                unpublish_variables = {"id": product_gid, "inputs": [{"publicationId": pos_pub_id}]}
                self.client.execute(unpublish_mutation, unpublish_variables)
            # Publish to Online Store if present
            if online_pub_id:
                mutation = ProductQueryHelper.PUBLISH_MUTATION
                variables = {"id": product_gid, "inputs": [{"publicationId": online_pub_id}]}
        # Only call client.execute if mutation and variables are set
        if mutation and variables:
            self.client.execute(mutation, variables)
        return True

    def export_product_images_gql(self, template, product_id, gql_variant_data=None):
        """
        Batch uploads images via staged upload and assigns them to variants using
        productVariantAppendMedia.

        Flow:
          1. Fetch existing media IDs on the product (pre-upload snapshot).
          2. stagedUploadsCreate  – one GraphQL call for all images.
          3. HTTP POST             – one request per image (Shopify requirement).
          4. productUpdate(media)  – one GraphQL call to create all media on the product.
             alt = template name (SEO). New media identified by excluding pre-existing IDs.
          5. productVariantAppendMedia – one (batched) call to map media → variants.
        """
        # Build SKU → variant GID lookup
        _logger.info(f"Starting Image creation for product : {template.name}")
        sku_variant_gid_map = {}
        if gql_variant_data:
            for v in gql_variant_data:
                if v.get("sku"):
                    sku_variant_gid_map[v["sku"]] = v["id"]

        # --- 1. Collect images that still need uploading ---
        images_to_upload = []   # input dicts for stagedUploadsCreate
        image_objs = []         # parallel list: (image_obj, image_data, filename, mime_type)

        for image_obj in template.shopify_image_ids:
            if not image_obj.image or image_obj.shopify_image_id:
                # Skip images that have no data or are already uploaded
                continue
            image_data = base64.b64decode(image_obj.image)
            filename = getattr(image_obj, 'shopify_image_filename',
                               f"{image_obj.odoo_image_id.name}_{image_obj.id}.png")
            mime_type = mimetypes.guess_type(filename)[0] or "image/png"
            file_size = len(image_data)
            images_to_upload.append({
                "filename": filename,
                "mimeType": mime_type,
                "fileSize": file_size,
                "resource": "IMAGE",
            })
            image_objs.append((image_obj, image_data, filename, mime_type))

        if not images_to_upload:
            return

        # --- 2. Snapshot existing media IDs before any upload ---
        # This lets us reliably identify *newly* created media in the productUpdate
        # response without relying on fragile positional assumptions.
        existing_media_ids = self.get_product_existing_media_ids(product_id)

        # --- 3. One stagedUploadsCreate call for all images ---
        staged_resp = self.get_staged_upload_params_bulk(images_to_upload)
        staged_data = staged_resp.get("data", {}).get("stagedUploadsCreate", {})
        if staged_data.get("userErrors"):
            _logger.info("Staged upload error: %s", staged_data["userErrors"])
            return

        staged_targets = staged_data.get("stagedTargets", [])
        if len(staged_targets) != len(image_objs):
            _logger.info("Mismatch in staged targets (%d) and images (%d)",
                          len(staged_targets), len(image_objs))
            return

        # --- 4. HTTP upload – one POST per file (Shopify's signed-URL requirement) ---
        # uploaded_pairs keeps (image_obj, resource_url) only for successful uploads,
        # preserving the submission order needed for positional matching below.
        uploaded_pairs = []   # list of (image_obj, resource_url)

        for idx, (image_obj, image_data, filename, mime_type) in enumerate(image_objs):
            target = staged_targets[idx]
            upload_url = target["url"]
            params = {p["name"]: p["value"] for p in target.get("parameters", [])}
            try:
                response = requests.post(
                    upload_url,
                    data=params,
                    files={"file": (filename, image_data, mime_type)},
                )
                if response.status_code not in (200, 201):
                    _logger.info(
                        "HTTP upload failed (status %s) for image %s",
                        response.status_code, image_obj.id,
                    )
                    continue
                uploaded_pairs.append((image_obj, target["resourceUrl"]))
            except Exception as e:
                _logger.exception("HTTP upload error for image %s: %s", image_obj.id, e)

        if not uploaded_pairs:
            return

        # --- 5. One productUpdate call to create all media on the product ---
        # alt = template name for SEO; it is the same for all images on this product.
        # Matching is done by excluding pre-existing media IDs (snapshot from step 2),
        # not by alt value — so alt stays clean and SEO-friendly.
        product_name = template.name or ""
        media_inputs = [
            {
                "mediaContentType": "IMAGE",
                "originalSource": resource_url,
                "alt": product_name,
            }
            for _, resource_url in uploaded_pairs
        ]
        media_resp = self.create_product_media_bulk_gql(product_id, media_inputs)
        media_payload = media_resp.get("data", {}).get("productUpdate", {})
        if media_payload.get("userErrors"):
            _logger.info("productUpdate media error: %s", media_payload["userErrors"])
            return

        # --- 6. Identify newly created media ---
        # Filter out pre-existing IDs; remaining nodes are the new ones.
        # Shopify appends new media in submission order, so positional matching
        # against uploaded_pairs is safe within this filtered list.
        all_media_edges = (
            media_payload.get("product", {}).get("media", {}).get("edges", [])
        )
        new_media_nodes = [
            edge["node"]
            for edge in all_media_edges
            if edge.get("node", {}).get("id")                          # must have an id (MediaImage only)
            and edge["node"]["id"] not in existing_media_ids            # must be newly created
        ]

        if len(new_media_nodes) != len(uploaded_pairs):
            _logger.warning(
                "Expected %d new media nodes but found %d after filtering pre-existing; "
                "some images may not be mapped to variants.",
                len(uploaded_pairs), len(new_media_nodes),
            )

        # --- 7. Map new media IDs → variants and persist shopify_image_id ---
        variant_media_map = collections.defaultdict(list)

        for i, (image_obj, resource_url) in enumerate(uploaded_pairs):
            if i >= len(new_media_nodes):
                _logger.warning("No new media node at index %d for image %s", i, image_obj.id)
                continue
            media_id = new_media_nodes[i].get("id")
            if not media_id:
                _logger.warning("Empty media node at index %d for image %s", i, image_obj.id)
                continue

            # Map media → variant(s)
            if image_obj.shopify_variant_id:
                for sku in image_obj.shopify_variant_id.mapped("default_code"):
                    variant_gid = sku_variant_gid_map.get(sku)
                    if variant_gid:
                        variant_media_map[variant_gid].append(media_id)

            image_obj.write({"shopify_image_id": ProductQueryHelper.gid_to_int_ept(media_id)})

        # --- 8. Attach media to variants in one batched call ---
        if variant_media_map:
            resp = self.append_media_to_variant_bulk(product_id, variant_media_map)
            errors = resp.get("data", {}).get("productVariantsBulkUpdate", {}).get("userErrors")
            if errors:
                _logger.info("Variant media append error: %s", errors)

