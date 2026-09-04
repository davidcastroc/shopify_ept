import time
import requests

class ShopifyBulkOrderHelper:
    """
    Helper class to run Shopify bulk order export, poll status, and download results.
    """
    def __init__(self, client):
        self.client = client

    def run_bulk_order_export(self, fields=None, poll_interval=10, timeout=600):
        if fields is None:
            fields = [
                "id",
                "name",
                "createdAt",
                "email",
                "totalPriceSet { shopMoney { amount currencyCode } }",
                "displayFinancialStatus",
                "displayFulfillmentStatus",
                "lineItems { edges { node { title quantity originalUnitPriceSet { shopMoney { amount currencyCode } } } } }"
            ]
        query_fields = "\n".join(fields)
        bulk_query = f"""
        {{
          orders {{
            edges {{
              node {{
                {query_fields}
              }}
            }}
          }}
        }}
        """
        mutation = f'''
        mutation {{
          bulkOperationRunQuery(
            query: """{bulk_query}"""
          ) {{
            bulkOperation {{
              id
              status
            }}
            userErrors {{
              field
              message
            }}
          }}
        }}
        '''
        result = self.client.execute(mutation)
        errors = result.get("data", {}).get("bulkOperationRunQuery", {}).get("userErrors", [])
        if errors:
            raise Exception(f"Bulk operation error: {errors}")
        print("Bulk operation started.")

        start_time = time.time()
        url = None
        while time.time() - start_time < timeout:
            status_query = '''
            {
              currentBulkOperation {
                id
                status
                errorCode
                objectCount
                fileSize
                url
                createdAt
                completedAt
              }
            }
            '''
            status_result = self.client.execute(status_query)
            op = status_result.get("data", {}).get("currentBulkOperation", {})
            status = op.get("status")
            print(f"Bulk operation status: {status}")
            if status == "COMPLETED":
                url = op.get("url")
                break
            elif status in ("FAILED", "CANCELED"):
                raise Exception(f"Bulk operation failed: {op.get('errorCode')}")
            time.sleep(poll_interval)
        if not url:
            raise TimeoutError("Bulk operation did not complete in time.")

        response = requests.get(url)
        file_path = "shopify_orders_bulk.jsonl"
        with open(file_path, "wb") as f:
            f.write(response.content)
        print(f"Downloaded bulk orders to {file_path}")
        return file_path
